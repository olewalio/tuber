"""Объединённая выдача по трём платформам из ОДНОЙ базы (ТЗ-5 §2).

    python3 -m tuber report [--db data/tuber.db] [--days 10] [--json]
                            [--compact] [--save] [--out DIR]

Отчёт читает единое ядро (``content`` / ``metric_snapshot`` / ``content_latest``
/ ``score`` / ``story_member`` / ``source``) и печатает четыре секции:

1. **YouTube** — топ по просмотрам в сутки за окно (по умолчанию 10 дней) с
   отсечкой по порогу показов: 50 000 для не-русских видео и 10 000 для русских
   (``content.lang = 'ru'``). Порог и окно — не «улучшение» формул, а отсечка
   микровыборки: так делал прежний предиктор поиска tuber-os
   (``discover_videos.py``: шортсы 50 000 / обычные 10 000 просмотров, см.
   ``docs/os/AUDIT-OLD-CONTOUR.md``), поэтому значения взяты оттуда, а не
   выдуманы.
2. **X** — без ретвитов, по лайкам и лайкам/час.
3. **Telegram** — свежие посты окна. Значимость (``significance``) НЕсравнима
   между каналами (у каждого своя база нормировки), поэтому ранжирование
   внутриканальное: каналы идут по алфавиту, внутри канала — по значимости,
   вместе с нормированной на канал осью ``eng_channel``.
4. **Сквозной сюжет** — материалы ОДНОГО сюжета с РАЗНЫХ платформ (через
   ``story_member`` + ``content.platform``). Это то, чего не могло быть при
   трёх раздельных базах.

Ссылка берётся из ``content.url`` (заполняется при сборе/переносе, см. долг
D-45). Если колонка пуста, честно печатается «нет ссылки (content.url пуст)» —
выдача больше НЕ собирает ссылку на месте и не выдумывает handle. Идентификаторы
не выдумываются.

Честные оговорки о неполноте данных печатаются в подвале и считаются по факту
(пустой ``viral_index``, мёртвая ось ``spread`` в X и т.п.).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tuber import config
from tuber.core import db

# --- Отсечки YouTube (взяты из прежнего предиктора поиска tuber-os) ----------
#: Порог показов для не-русских видео.
YOUTUBE_MIN_VIEWS = 50_000
#: Порог показов для русскоязычных видео (content.lang='ru').
YOUTUBE_MIN_VIEWS_RU = 10_000
#: Сколько позиций показывать в секции YouTube.
YOUTUBE_LIMIT = 15
#: Сколько позиций показывать в секции X.
X_LIMIT = 15
#: Сколько постов показывать на канал в секции Telegram.
TELEGRAM_PER_CHANNEL = 3
#: Сколько каналов показывать в секции Telegram (иначе секция необъятна).
TELEGRAM_CHANNEL_LIMIT = 12
#: Длина «короткого описания» в символах.
DESC_LIMIT = 200

# --- Компактная сводка владельцу (ТЗ-5-доп-2, ТЗ-5-доп-3) --------------------
#: Жёсткий лимит сводки в знаках (лимит Telegram 4096, берём с запасом).
#: Считается по длине UTF-8 (байтам), поэтому и ``wc -c`` укладывается в лимит.
COMPACT_LIMIT = 3500


# --------------------------------------------------------------------------- #
# Ссылки
# --------------------------------------------------------------------------- #
#: Честная оговорка вместо ссылки, когда ``content.url`` пуст (D-45).
NO_URL = "нет ссылки (content.url пуст)"


def _link(url: str | None) -> str:
    """Ссылка из ``content.url`` или честная оговорка (без синтеза на месте)."""
    return url or NO_URL


def _telegram_message_id(external_id: str | None) -> int | None:
    if not external_id:
        return None
    _, _, mid = str(external_id).partition("/")
    try:
        return int(mid)
    except (TypeError, ValueError):
        return None


def _short(text: str | None, limit: int = DESC_LIMIT) -> str:
    t = " ".join((text or "").split())
    if not t:
        return "(без текста)"
    return t[:limit] + ("…" if len(t) > limit else "")


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value, nd: int = 1) -> str:
    try:
        return f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _shorten_desc(desc: str, limit: int) -> str:
    """Укоротить описание до ``limit`` знаков с многоточием (ТЗ-5-доп-3)."""
    if limit <= 0:
        return ""
    if len(desc) <= limit:
        return desc
    return desc[:limit].rstrip() + "…"


class CompactItem:
    """Пункт сводки с отделённым описанием (ТЗ-5-доп-3).

    Пункт делится на три части: ``head`` (маркер/канал), ``desc`` (собственно
    описание — единственная часть, которую МОЖНО укоротить) и ``tail`` (цифры и
    ПОЛНАЯ ссылка — неприкосновенны). Так при нехватке места под резерв
    укорачивается только описание, а числа и ссылка не меняются.
    """

    __slots__ = ("head", "desc", "tail")

    def __init__(self, head: str, desc: str, tail: str) -> None:
        self.head = head
        self.desc = desc
        self.tail = tail

    @property
    def text(self) -> str:
        return f"{self.head}{self.desc}{self.tail}"

    def render(self, desc_limit: int | None = None) -> str:
        if desc_limit is None:
            return self.text
        return f"{self.head}{_shorten_desc(self.desc, desc_limit)}{self.tail}"


def _now_iso(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# Секция 1. YouTube
# --------------------------------------------------------------------------- #
def youtube_items(conn, cutoff: str) -> tuple[list[str], dict]:
    """Пункты секции YouTube (топ по просмотрам/сутки с отсечкой порога)."""
    sql = """
        SELECT c.id AS content_id, c.external_id, c.title, c.lang, c.published_at,
               c.url, s.handle AS channel_handle, s.title AS channel_title,
               m.views, m.views_per_day, m.likes, m.comments, m.captured_at,
               cl.title_ru
        FROM metric_snapshot m
        JOIN content c ON c.id = m.content_id
        JOIN (
            SELECT content_id, MAX(captured_at) AS mc
            FROM metric_snapshot
            WHERE captured_at >= ? AND interval_quality = 'ok'
                  AND views_per_day IS NOT NULL
            GROUP BY content_id
        ) t ON t.content_id = m.content_id AND t.mc = m.captured_at
        LEFT JOIN source s ON s.id = c.source_id
        LEFT JOIN classification cl ON cl.content_id = c.id
        WHERE c.platform = 'youtube' AND c.deleted_at IS NULL
          AND m.interval_quality = 'ok' AND m.views_per_day IS NOT NULL
    """
    rows = conn.execute(sql, (cutoff,)).fetchall()

    def is_ru(r) -> bool:
        return (r["lang"] or "").lower().startswith("ru")

    above = [r for r in rows if (r["views"] or 0) >= (
        YOUTUBE_MIN_VIEWS_RU if is_ru(r) else YOUTUBE_MIN_VIEWS)]
    above.sort(key=lambda r: -(r["views_per_day"] or 0))
    shown = above[:YOUTUBE_LIMIT]

    items = []
    for r in shown:
        link = _link(r["url"])
        title = r["title_ru"] or r["title"] or "(без заголовка)"
        items.append(CompactItem(
            head="   ",
            desc=_short(title),
            tail=(f" — канал {r['channel_title'] or r['channel_handle'] or '?'}"
                  f"; просмотры/сутки {_fmt_int(r['views_per_day'])}"
                  f", просмотры {_fmt_int(r['views'])}"
                  f", лайки {_fmt_int(r['likes'])}"
                  f"; {link or 'нет ссылки'}"),
        ))
    return items, {"considered": len(rows), "above": len(above), "shown": len(shown)}


def youtube_section(conn, cutoff: str) -> tuple[list[str], dict]:
    """Топ по просмотрам/сутки с отсечкой порога показов (50k / 10k ru)."""
    items, stats = youtube_items(conn, cutoff)
    lines = [
        "1. YouTube — топ по просмотрам в сутки за окно.",
        f"   Правило: окно {stats['considered']} видео с замерами; отсечка показов "
        f"{_fmt_int(YOUTUBE_MIN_VIEWS)} (не-ru) / "
        f"{_fmt_int(YOUTUBE_MIN_VIEWS_RU)} (ru, lang='ru'); "
        "ранжирование по views_per_day убыв.",
    ]
    if not items:
        lines.append("   нет видео, прошедших порог показов за окно "
                     f"(всего с замерами {stats['considered']}, из них выше порога 0).")
        return lines, stats
    lines.extend(i.text for i in items)
    return lines, stats


# --------------------------------------------------------------------------- #
# Секция 2. X
# --------------------------------------------------------------------------- #
def x_items(conn, cutoff: str) -> tuple[list[str], dict]:
    """Пункты секции X: без ретвитов, по лайкам и лайкам/час."""
    sql = """
        SELECT c.id, c.external_id, c.published_at, c.text, c.url, c.lang,
               c.author_handle, s.handle AS source_handle,
               cl.likes
        FROM content c
        LEFT JOIN content_latest cl ON cl.content_id = c.id
        LEFT JOIN source s ON s.id = c.source_id
        WHERE c.platform = 'x' AND c.deleted_at IS NULL
          AND c.published_at >= ?
          AND COALESCE(c.is_repost, 0) = 0
        ORDER BY cl.likes DESC NULLS LAST
        LIMIT ?
    """
    rows = conn.execute(sql, (cutoff, X_LIMIT)).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM content WHERE platform='x' AND deleted_at IS NULL"
        " AND published_at >= ? AND COALESCE(is_repost,0)=0", (cutoff,)).fetchone()[0]

    now = datetime.now(timezone.utc)
    items = []
    for r in rows:
        likes = r["likes"]
        handle = r["author_handle"] or r["source_handle"] or "i"
        lph = "—"
        pub = r["published_at"]
        if likes is not None and pub:
            try:
                t = datetime.strptime(pub[:19], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
                hours = max((now - t).total_seconds() / 3600.0, 1.0)
                lph = _fmt_num(likes / hours)
            except ValueError:
                lph = "—"
        link = _link(r["url"])
        items.append(CompactItem(
            head=f"   @{handle}: ",
            desc=_short(r["text"]),
            tail=(f" — лайки {_fmt_int(likes)}"
                  f", лайки/час {lph}; {link or 'нет ссылки'}"),
        ))
    return items, {"considered": total, "shown": len(rows)}


def x_section(conn, cutoff: str) -> tuple[list[str], dict]:
    """X: без ретвитов, по лайкам и лайкам/час."""
    items, stats = x_items(conn, cutoff)
    lines = [
        "2. X — посты за окно без ретвитов.",
        "   Правило: ретвиты исключены (is_repost=0); ранжирование по лайкам "
        "убыв., в строке — лайки/час от момента публикации.",
    ]
    if not items:
        lines.append("   нет постов за окно.")
        return lines, stats
    lines.extend(i.text for i in items)
    return lines, stats


# --------------------------------------------------------------------------- #
# Секция 3. Telegram
# --------------------------------------------------------------------------- #
def telegram_items(conn, cutoff: str) -> tuple[list[str], dict]:
    """Пункты секции Telegram; ранжирование ВНУТРИ канала (значимость несравнима)."""
    sql = """
        SELECT c.id, c.external_id, c.published_at, c.text, c.url,
               s.handle AS channel, s.title AS channel_title,
               cl.views, cl.forwards, sc.significance, sc.axes_json
        FROM content c
        LEFT JOIN source s ON s.id = c.source_id
        LEFT JOIN content_latest cl ON cl.content_id = c.id
        LEFT JOIN score sc ON sc.content_id = c.id
             AND sc.computed_at = (
                 SELECT MAX(computed_at) FROM score WHERE content_id = c.id)
        WHERE c.platform = 'telegram' AND c.deleted_at IS NULL
          AND c.published_at >= ?
        ORDER BY s.handle ASC, sc.significance DESC NULLS LAST
    """
    rows = conn.execute(sql, (cutoff,)).fetchall()

    # Сначала выбираем КАНАЛЫ (по лучшей внутриканальной значимости), затем
    # внутри выбранных каналов берём верхние посты. Так секция остаётся
    # обозримой, а ранжирование не выходит за пределы канала.
    best: dict[str, float] = {}
    for r in rows:
        ch = r["channel"] or "(без канала)"
        sig = r["significance"]
        key = float(sig) if isinstance(sig, (int, float)) else float("-inf")
        if ch not in best or key > best[ch]:
            best[ch] = key
    selected = [
        ch for ch, _ in sorted(
            best.items(), key=lambda kv: (-(kv[1] if kv[1] != float("-inf") else -1e18), kv[0]))
    ][:TELEGRAM_CHANNEL_LIMIT]

    channels: dict[str, list] = {}
    for r in rows:
        ch = r["channel"] or "(без канала)"
        if ch not in selected:
            continue
        bucket = channels.setdefault(ch, [])
        if len(bucket) < TELEGRAM_PER_CHANNEL:
            bucket.append(r)

    items = []
    for ch in sorted(channels):
        for r in channels[ch]:
            link = _link(r["url"])
            eng_channel = None
            try:
                eng_channel = json.loads(r["axes_json"] or "{}").get("eng_channel")
            except (ValueError, TypeError):
                eng_channel = None
            items.append(CompactItem(
                head=f"   [{r['channel_title'] or ch}] ",
                desc=_short(r["text"]),
                tail=(f" — просмотры {_fmt_int(r['views'])}"
                      f", forwards {_fmt_int(r['forwards'])}"
                      f", significance {_fmt_num(r['significance'], 3)}"
                      f", eng_channel {_fmt_num(eng_channel, 3)}; {link or 'нет ссылки'}"),
            ))
    stats = {"considered": len(rows), "channels": len(channels),
             "selected": len(selected), "have": len(best)}
    return items, stats


def telegram_section(conn, cutoff: str) -> tuple[list[str], dict]:
    """Telegram: свежие посты; ранжирование ВНУТРИ канала (значимость несравнима)."""
    items, stats = telegram_items(conn, cutoff)
    lines = [
        "3. Telegram — свежие посты за окно.",
        "   Правило: significance НЕсравнима между каналами (у каждого своя база "
        "нормировки), поэтому сортировка ВНУТРИ канала по significance; каналы "
        "по алфавиту; eng_channel — нормированная на канал вовлечённость.",
    ]
    if not items:
        lines.append("   нет постов за окно.")
        return lines, {"considered": 0, "channels": 0}
    if stats["have"] > stats["selected"]:
        lines.append(
            f"   показаны {stats['selected']} каналов из {stats['have']} "
            "(отобраны по лучшей внутриканальной значимости).")
    lines.extend(i.text for i in items)
    return lines, {"considered": stats["considered"], "channels": stats["channels"]}


# --------------------------------------------------------------------------- #
# Секция 4. Сквозной сюжет
# --------------------------------------------------------------------------- #
def cross_story_section(conn) -> tuple[list[str], dict]:
    """Сюжеты, у которых материалы есть на РАЗНЫХ платформах."""
    sql = """
        SELECT st.id AS story_id, st.topic, st.title,
               c.platform, c.external_id, c.url, c.published_at,
               c.author_handle, s.handle AS source_handle
        FROM story_member sm
        JOIN content c ON c.id = sm.content_id
        JOIN story st ON st.id = sm.story_id
        LEFT JOIN source s ON s.id = c.source_id
        WHERE sm.story_id IN (
            SELECT sm2.story_id
            FROM story_member sm2
            JOIN content c2 ON c2.id = sm2.content_id
            GROUP BY sm2.story_id
            HAVING COUNT(DISTINCT c2.platform) >= 2)
        ORDER BY st.id, c.platform
    """
    rows = conn.execute(sql).fetchall()
    stories: dict[int, dict] = {}
    for r in rows:
        entry = stories.setdefault(r["story_id"], {"topic": r["topic"],
                                                    "title": r["title"],
                                                    "platforms": {}})
        entry["platforms"].setdefault(r["platform"], []).append(r)

    lines = [
        "4. Сквозной сюжет — материалы одного сюжета с РАЗНЫХ платформ.",
        "   Правило: сюжет попадает сюда, если в story_member есть контент "
        "минимум двух разных platform; платформы идут по алфавиту.",
    ]
    if not stories:
        lines.append("   нет сюжетов, объединяющих материалы разных платформ.")
        return lines, {"stories": 0, "members": len(rows)}

    for sid in sorted(stories):
        entry = stories[sid]
        label = entry["title"] or entry["topic"] or f"сюжет {sid}"
        platforms = ", ".join(sorted(entry["platforms"]))
        lines.append(f"   Сюжет {sid} [{label}] — платформы: {platforms}")
        for platform in sorted(entry["platforms"]):
            for r in entry["platforms"][platform]:
                link = _link(r["url"])
                lines.append(f"     {platform}: {link}")
    return lines, {"stories": len(stories), "members": len(rows)}


# --------------------------------------------------------------------------- #
# Подвал: честные оговорки
# --------------------------------------------------------------------------- #
def caveats(conn) -> list[str]:
    """Оговорки о неполноте данных, посчитанные по факту (не выдуманные)."""
    lines = ["ПОДВАЛ: честные оговорки (что в данных неполно)"]

    yt_null = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='youtube'"
        " AND json_extract(sc.axes_json, '$.viral_index') IS NULL").fetchone()[0]
    yt_all = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='youtube'").fetchone()[0]
    lines.append(
        f"   YouTube: viral_index пуст у {yt_null} из {yt_all} строк score — "
        "композитный индекс на этих видео не построен.")

    x_spread_zero = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='x' AND (sc.spread IS NULL OR sc.spread=0)").fetchone()[0]
    x_all = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='x'").fetchone()[0]
    lines.append(
        f"   X: score.spread = 0/NULL у {x_spread_zero} из {x_all} строк score "
        "(ось наполнена, D-48 закрыт: ноль — честное «тему не подхватил второй "
        "автор реестра», а не отсутствие расчёта).")

    url_null = conn.execute(
        "SELECT COUNT(*) FROM content WHERE url IS NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM content").fetchone()[0]
    lines.append(
        f"   Ссылки: content.url пуст у {url_null} из {total} строк — у этих "
        "материалов ссылка честно не показана (не выдумывается).")

    tg_sig = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='telegram' AND sc.significance IS NOT NULL").fetchone()[0]
    lines.append(
        f"   Telegram: significance есть у {tg_sig} строк — значение сравнимо "
        "только внутри одного канала, межканальные сравнения не делаются.")

    cross = conn.execute(
        "SELECT COUNT(*) FROM (SELECT sm.story_id FROM story_member sm"
        " JOIN content c ON c.id=sm.content_id GROUP BY sm.story_id"
        " HAVING COUNT(DISTINCT c.platform)>=2)").fetchone()[0]
    lines.append(
        f"   Сквозных сюжетов в story_member: {cross} — если 0, связывание "
        "сюжетов между платформами ещё не наполнено.")
    return lines


# --------------------------------------------------------------------------- #
# Сборка отчёта
# --------------------------------------------------------------------------- #
def build(conn, *, days: int = 10, db_path: str | None = None) -> str:
    cutoff, now = _now_iso(days)
    parts = [
        f"tuber report — объединённая выдача (единая база: {db_path or '(ядро)'})",
        f"окно: последние {days} дней (с {cutoff} UTC), сформировано {now} UTC",
        "=" * 72,
    ]
    yt_lines, yt_stats = youtube_section(conn, cutoff)
    x_lines, x_stats = x_section(conn, cutoff)
    tg_lines, tg_stats = telegram_section(conn, cutoff)
    st_lines, st_stats = cross_story_section(conn)
    for block in (yt_lines, x_lines, tg_lines, st_lines):
        parts.append("")
        parts.extend(block)
    parts.append("")
    parts.extend(["=" * 72])
    parts.extend(caveats(conn))
    return "\n".join(parts)


def build_json(conn, *, days: int = 10, db_path: str | None = None) -> str:
    """Машинный вывод: те же секции, но текстом (используется приёмкой)."""
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    payload = {"db": db_path, "days": days}
    for name, fn in (("youtube", youtube_section), ("x", x_section),
                     ("telegram", telegram_section)):
        _, stats = fn(conn, cut.strftime("%Y-%m-%d %H:%M:%S"))
        payload[name] = stats
    _, stats = cross_story_section(conn)
    payload["cross_story"] = stats
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_compact(conn, *, days: int = 10, db_path: str | None = None,
                  report_path: str | None = None,
                  per_platform: int | None = None,
                  limit: int = COMPACT_LIMIT) -> str:
    """Компактная сводка для доставки владельцу (ТЗ-5-доп-2, ТЗ-5-доп-3).

    Печатается в stdout обёрткой ``scripts/common/tuber_report.sh``: планировщик
    Hermes для заданий ``no_agent`` доставляет владельцу ровно stdout. Сводка:

    * заголовок: что это, окно (``--days``), откуда (путь к базе), дата;
    * по каждой платформе — пункты в том же виде, что и в полном отчёте (русское
      описание, цифры, ПОЛНАЯ ссылка); правило ранжирования НЕ печатается —
      вместо него одна строка в подвале;
    * сквозной сюжет — одной строкой (сколько сюжетов или «нет»);
    * подвал: сколько пунктов скрыто по каждой платформе + путь к полному файлу
      (или пометка, что файл не сохранялся).

    Распределение мест (ТЗ-5-доп-3). Бюджет ``limit`` байт UTF-8 раздаётся
    СПРАВЕДЛИВО, а не «обрезкой с конца»:

    1. **резерв** — каждый раздел, у которого есть пункты за окно, получает
       минимум 1 пункт; если резерв не влезает целиком, укорачивается только
       ОПИСАНИЕ пункта (см. :class:`CompactItem`) — ссылка и цифры неизменны;
    2. **round-robin** — остаток бюджета раздаётся по кругу: на каждом шаге
       раздел получает следующий по порядку пункт, если тот влезает целиком;
       порядок внутри раздела сохраняется (сначала верхние).

    Строка «все пункты скрыты» не печатается никогда: раздел без данных честно
    сообщает «нет данных за окно» (пунктов нет в данных, а не в бюджете).
    Счётчики скрытых печатаются всегда, и «показано + скрыто» по каждому разделу
    сходится с полным отчётом. ``per_platform`` — необязательный верхний предел
    пунктов на раздел (``None`` — без предела, решает бюджет).
    """
    cutoff, now = _now_iso(days)
    yt_items, _ = youtube_items(conn, cutoff)
    x_items_, _ = x_items(conn, cutoff)
    tg_items, _ = telegram_items(conn, cutoff)
    _, st_stats = cross_story_section(conn)

    raw = [
        ("YouTube", "1. YouTube — топ по просмотрам/сутки:", yt_items),
        ("X", "2. X — топ по лайкам:", x_items_),
        ("Telegram", "3. Telegram — топ постов за окно:", tg_items),
    ]
    sections: list[dict] = []
    for name, title, items in raw:
        if per_platform is not None:
            items = items[:per_platform]
        sections.append({"name": name, "title": title,
                         "items": list(items), "shown": []})

    def hidden(s: dict) -> int:
        return len(s["items"]) - len(s["shown"])

    header = [
        "Tuber — объединённая выдача: сводка владельцу",
        f"Окно: последние {days} дней (с {cutoff} UTC); база: {db_path or '(ядро)'}; "
        f"сформировано {now} UTC",
    ]
    if st_stats.get("stories"):
        story_line = (f"4. Сквозной сюжет: сюжетов с материалами разных платформ — "
                      f"{st_stats['stories']} (детали — в полном отчёте).")
    else:
        story_line = "4. Сквозной сюжет: нет сюжетов с материалами разных платформ."

    def render() -> str:
        parts = list(header)
        for s in sections:
            parts.append("")
            parts.append(s["title"])
            vis = s["shown"]
            if vis:
                for i, (item, desc_limit) in enumerate(vis, 1):
                    parts.append(f"{i}. {item.render(desc_limit).strip()}")
            else:
                parts.append("   нет данных за окно.")
            parts.append(f"   скрыто пунктов: {hidden(s)}")
        parts.append("")
        parts.append(story_line)
        parts.append("")
        parts.append("Правила ранжирования и полный список — в полном отчёте.")
        if report_path:
            parts.append(f"Полный отчёт: {report_path}")
        else:
            parts.append("Полный отчёт файлом не сохранялся (--save не запрошен).")
        parts.append("Скрыто по платформам: " + ", ".join(
            f"{s['name']} {hidden(s)}" for s in sections) + ".")
        return "\n".join(parts)

    def over_limit() -> bool:
        return len(render().encode("utf-8")) + 1 > limit

    # 1. Резерв: по одному пункту в каждый непустой раздел.
    for s in sections:
        if s["items"]:
            s["shown"].append((s["items"][0], None))

    # Резерв не влез — укорачиваем описания (никогда не ссылку и не цифры).
    while over_limit():
        best = None  # (len(rendered desc), section, index, item)
        for s in sections:
            for idx, (item, desc_limit) in enumerate(s["shown"]):
                cur = item.desc if desc_limit is None else _shorten_desc(
                    item.desc, desc_limit)
                if cur and (best is None or len(cur) > best[0]):
                    best = (len(cur), s, idx, item)
        if best is None:
            break  # укорачивать больше нечего: раздел всё равно показан
        _, s, idx, item = best
        new_limit = max(0, best[0] - max(2, best[0] // 3))
        s["shown"][idx] = (item, new_limit)

    # 2. Round-robin: остаток бюджета — по одному следующему пункту за шаг.
    next_idx = {s["name"]: 1 for s in sections}
    while True:
        progressed = False
        for s in sections:
            i = next_idx[s["name"]]
            if i >= len(s["items"]):
                continue
            s["shown"].append((s["items"][i], None))
            if over_limit():
                s["shown"].pop()
            else:
                next_idx[s["name"]] = i + 1
                progressed = True
        if not progressed:
            break

    return render()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuber report",
                                     description="Объединённая выдача по трём платформам")
    parser.add_argument("--db", dest="db_path", default=None,
                        help="путь к единой базе (по умолчанию data/tuber.db)")
    parser.add_argument("--days", type=int, default=10, help="окно, дней")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--compact", action="store_true",
                        help="компактная сводка владельцу (≤3500 байт) вместо полного текста")
    parser.add_argument("--save", action="store_true",
                        help="дополнительно сохранить отчёт в файл")
    parser.add_argument("--out", default=None,
                        help="каталог/файл для --save (по умолчанию reports/)")
    args = parser.parse_args(argv)

    db_path = config.db_path(args.db_path)
    if not Path(db_path).exists():
        print(f"ошибка: базы нет: {db_path}", file=sys.stderr)
        return 2
    conn = db.connect(db_path, readonly=True)
    try:
        if args.json:
            print(build_json(conn, days=args.days, db_path=db_path))
            return 0
        text = build(conn, days=args.days, db_path=db_path)
        saved_path = None
        if args.save:
            out = args.out
            if out and Path(out).suffix:
                path = Path(out)
            else:
                base = Path(out) if out else (config.ROOT / "reports")
                path = base / f"report-{datetime.now(timezone.utc):%Y-%m-%d}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
            saved_path = path
        if args.compact:
            # Владельцу уходит ровно сводка; полный отчёт — файлом (--save).
            print(build_compact(conn, days=args.days, db_path=db_path,
                                report_path=str(saved_path) if saved_path else None))
        else:
            print(text)
            if saved_path:
                print(f"\n(сохранено: {saved_path})")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
