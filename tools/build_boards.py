#!/usr/bin/env python3
"""«Борды залёта»: витрина самых виральных материалов X, YouTube и Telegram.

Одна самодостаточная HTML-страница с тремя досками подряд (X / YouTube /
Телеграм) и сеткой квадратных карточек. Отбор — ТОТ ЖЕ, что в полном отчёте
(:mod:`tuber.analysis.report`): генератор не пишет свой SQL, а берёт пулы, полы
и нормы отчёта через публичные обёртки. Меняются только окно, кэп на автора и
размер доски (``--limit``): доска — это ТОП по кратности среди позиций,
прошедших полы отчёта.

Доска YouTube (ТЗ-3) строится ТОЛЬКО из строк ``select_viral_youtube`` (блоки 1
и 2): полы (подписчиков ≥ 1000, просмотров ≥ 5000, охвата ≥ 3.0) действуют и на
список карточек, поэтому число карточек-кандидатов сходится со счётчиком шапки.
Сырой пул служит лишь знаменателем «рассмотрено» и нормой канала. Главное число
карточки — кратность к норме канала (просмотры ÷ медиана канала в окне), охват
на подписчика остаётся справочно во второй строке.

Честность чисел:

* всё только из базы, только чтение (``file:…?mode=ro``);
* в сеть генератор не ходит, LLM не зовёт (никакого живого перевода): русские
  описания берутся лишь из уже готовых полей ``title_ru``/``summary_ru``;
* нет подписчиков/медианы — кратность не выдумывается (прочерк), и такая
  карточка в топ по кратности не лезет;
* язык канала (``source.lang``) выводится меткой на карточке и разбивкой в
  шапке; пусто — метки нет, ничего не дорисовывается.

Запуск::

    python3 tools/build_boards.py --days 10 --limit 50 \\
        --out reports/boards/index.html

Рядом с HTML пишется ``boards.json`` с теми же данными (отладка/повторное
использование). Выход — полностью статичный файл: CSS внутри, без внешних
CDN, шрифтов и скриптов.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Пакет проекта доступен и при запуске файлом из корня репозитория.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tuber import config
from tuber.analysis import report as report_mod
from tuber.analysis import slivki as slivki_mod
from tuber.core import db as core_db

#: Обрезка описания карточки (ТЗ §4: ~160 знаков, многоточие).
DESC_LIMIT = 160
#: Не больше стольких карточек на канал/автора — как в отчёте.
PER_KEY = 2
#: Сколько подтем показывать в разделе «ИИ-инструменты». Это НЕ карточки: список
#: ярлыков намеренно не привязан к ``--limit`` (у панели свой размер выдачи).
SUBTOPICS_SHOWN = 10
#: Цвет-акцент платформы и подписи (интерфейс по-русски).
PLATFORMS = (
    ("x", "X", "#0b0d0f", "#f5f5f5"),
    ("youtube", "YouTube", "#b3261e", "#ffffff"),
    ("telegram", "Телеграм", "#20649b", "#ffffff"),
)

#: Окна витрины ТЗ-12: (дни, подкаталог, подпись). Каждое окно — отдельная
#: статичная страница ``/boards/<slug>/``; корневой ``/boards/`` остаётся
#: 10-дневным (обратная совместимость).
PERIODS = (
    (1, "1d", "24 часа"),
    (3, "3d", "3 дня"),
    (5, "5d", "5 дней"),
    (10, "10d", "10 дней"),
)


# --------------------------------------------------------------------------- #
# Форматирование чисел и дат (по-русски, без латиницы в подписях)
# --------------------------------------------------------------------------- #
def _fmt_mult(value) -> str:
    """Кратность вида «×12,4» (запятая — десятичный разделитель)."""
    if value is None:
        return "—"
    return "×" + f"{float(value):.1f}".replace(".", ",")


def _fmt_int(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _fmt_date(published_at) -> str:
    """ISO UTC ядра → «ДД.ММ.ГГГГ»; пусто → честное «дата неизвестна»."""
    if not published_at:
        return "дата неизвестна"
    try:
        return date.fromisoformat(str(published_at)[:10]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "дата неизвестна"


def _short(text, limit: int = DESC_LIMIT) -> str:
    t = " ".join((text or "").split())
    if not t:
        return "(без текста)"
    return t[:limit].rstrip() + ("…" if len(t) > limit else "")


#: Служебные ярлыки вложений: медиа-пост без настоящего текста. В любом
#: регистре, допускается хвост из цифр/пробелов (ТЗ-2 §2).
_SERVICE_LABEL_RE = re.compile(
    r"^(?:image|video|photo|gif|медиа|untitled)[\s\d]*$", re.IGNORECASE)


def _is_blank_or_service_title(text) -> bool:
    """Пустой заголовок или ярлык-заглушка вложения («Image», «GIF 2»…)."""
    t = " ".join((text or "").split())
    return not t or bool(_SERVICE_LABEL_RE.match(t))


#: Русские подписи для двух основных языков; прочие языки печатаем кодом как
#: есть (ТЗ-3 §4.2): «рус», «англ» или код языка. Пусто — метки нет.
_MAIN_LANG_LABELS = {"ru": "рус", "en": "англ"}


def _lang_base(code) -> str | None:
    """Языковой код без региона (``en-US`` → ``en``); пусто → ``None``."""
    if not code:
        return None
    return str(code).split("-")[0].strip().lower() or None


def _lang_label(code) -> str:
    """Метка языка канала: «рус», «англ» или код языка как есть."""
    base = _lang_base(code)
    if not base:
        return ""
    return _MAIN_LANG_LABELS.get(base, str(code))


def _lang_bucket(code) -> str:
    """Группа для шапки доски: ``ru`` / ``en`` / ``other`` / ``unknown``."""
    base = _lang_base(code)
    if not base:
        return "unknown"
    return base if base in ("ru", "en") else "other"



def _msk(now_utc: datetime) -> datetime:
    """МСК = UTC+3 (постоянно, без перехода на летнее время)."""
    return now_utc + timedelta(hours=3)


# --------------------------------------------------------------------------- #
# Карточки
# --------------------------------------------------------------------------- #
def _card(*, platform: str, title: str, channel: str, date: str, url,
          main: str, main_value, absolute: str, base: str, note: str = "",
          lang_label: str = "", origin: str = "original") -> dict:
    return {
        "platform": platform,
        "title": title,
        "channel": channel,
        "lang_label": lang_label,
        "date": date,
        "url": url or "",
        "main": main,
        "main_value": main_value,
        "absolute": absolute,
        "base": base,
        "note": note,
        # ТЗ-8 §1.3: «original» / «retelling» (агрегатор показан, но помечен).
        "origin": origin,
    }


def _cap_per_key(cards: list, limit: int) -> list:
    """Не больше :data:`PER_KEY` карточек на канал/автора и не больше ``limit``."""
    counts: dict = {}
    out: list = []
    for c in cards:
        key = c["channel"]
        if counts.get(key, 0) >= PER_KEY:
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _sort_cards(cards: list) -> None:
    """По кратности (убывание), карточки с прочерком — в конец, затем по абсолюту."""
    cards.sort(key=lambda c: (c["main_value"] is None, -(c["main_value"] or 0.0),
                              -(c["abs_value"] or 0)))


def _finish(platform: str, cards: list, considered: int, passed: int,
            limit: int, skips: dict | None = None) -> dict:
    """Кэп на автора, затем обрезка до ``limit``.

    ``found`` — сколько карточек осталось ПОСЛЕ кэпа на автора, но ДО лимита:
    именно из него берётся ``min(limit, found)``. ``shown`` — сколько реально
    на доске. ``skips`` — счётчики отсева ТЗ-8 (§1.4): ``skipped_ad``,
    ``skipped_promo_source``, ``skipped_candidate`` (можно и ``skipped_status``).
    """
    _sort_cards(cards)
    capped = _cap_per_key(cards, len(cards))
    shown = capped[:limit]
    board = {
        "platform": platform,
        "considered": considered,
        "passed": passed,
        "found": len(capped),
        "shown": len(shown),
        "cards": shown,
    }
    for key in ("skipped_ad", "skipped_promo_source", "skipped_candidate",
                "skipped_status"):
        board[key] = int((skips or {}).get(key, 0))
    return board


# --------------------------------------------------------------------------- #
# Отбор досок (пулы и нормы — из отчёта, без своего SQL)
# --------------------------------------------------------------------------- #
def _x_board(conn, cutoff: str, x_cutoff: str, limit: int) -> dict:
    pool = report_mod.viral_x_pool(conn, x_cutoff)
    ratios = report_mod.viral_x_ratio_map(pool)
    # ТЗ-8 §1.2: промо-источники считаем один раз по пулу X и передаём в отбор.
    promo_sources = report_mod.viral_promo_sources(
        conn, {report_mod._row_source_id(r) for r in pool}, cutoff=cutoff)
    _, stats = report_mod.viral_x_items(pool, cutoff,
                                        promo_sources=promo_sources)
    # Счётчики отсева — из отбора (пост-полы), а не из «сырого» цикла ниже.
    skips = {k: stats.get(k, 0) for k in report_mod.SKIP_COUNTER_KEYS}
    # Русский заголовок X — тем же путём, что в отчёте: summary_ru → title_ru →
    # кэш report_text → оригинал. Дополнительно (один текст живёт в базе в
    # нескольких content-строках) берём перевод у «соседней» строки по text_hash
    # через report.x_ru_hash_map — SQL отчёта, без дублирования и БЕЗ живого
    # перевода (allow_live=False): только то, что уже есть в базе.
    ru_map = report_mod.x_ru_hash_map(conn, [r["text_hash"] for r in pool])
    cards: list = []
    for r in pool:
        if not report_mod._in_window(r["published_at"], cutoff):
            continue
        likes = r["likes"]
        if likes is None:
            continue  # нечего показывать абсолютом/кратностью
        key = r["author_handle"] or r["source_handle"] or "(без автора)"
        pair = ratios.get(key)
        median = None
        ratio = None
        if pair:
            median, group_n = pair
            # Кратность считаем тем же правилом, что и отчёт: норма автора
            # осмысленна только при достаточном числе постов и медиане ≥ пола.
            if (group_n >= report_mod.VIRAL_X_MIN_AUTHOR_POSTS
                    and median >= report_mod.VIRAL_X_MIN_AUTHOR_MEDIAN
                    and median > 0):
                ratio = likes / median
        ru_pair = ru_map.get(r["text_hash"]) if r["text_hash"] else None
        summary_ru = r["summary_ru"] or (ru_pair[0] if ru_pair else None)
        title_ru = r["title_ru"] or (ru_pair[1] if ru_pair else None)
        # Пустой или служебный ярлык вложения («Image»…) — медиа-пост без текста:
        # карточку не выводим даже при наличии переводной сводки (ТЗ-2 §2).
        if _is_blank_or_service_title(r["title"]):
            continue
        # ТЗ-8 §1.1–1.2: реклама/промо/статус — ДО построения карточки.
        reason = report_mod.viral_quality_skip(
            status=report_mod._row_source_status(r),
            source_id=report_mod._row_source_id(r),
            text=" ".join(p for p in (r["title"], summary_ru, title_ru) if p),
            promo_sources=promo_sources)
        if reason:
            continue
        desc = report_mod._x_description_from(
            conn, summary_ru, title_ru, r["text_hash"], r["title"],
            allow_live=False)
        card = _card(
            platform="x",
            title=_short(desc),
            channel=key,
            date=_fmt_date(r["published_at"]),
            url=r["url"],
            main=_fmt_mult(ratio),
            main_value=ratio,
            absolute=f"{_fmt_int(likes)} лайков",
            base=f"медиана {_fmt_int(median)}",
            origin=report_mod.ads.classify_origin(r["title"] or "", r["url"]),
        )
        card["abs_value"] = int(likes)
        cards.append(card)
    return _finish("x", cards, len(pool), stats["passed"], limit, skips=skips)


def _youtube_board(conn, cutoff: str, limit: int) -> dict:
    """Доска YouTube из строк, ПРОШЕДШИХ полы отчёта (ТЗ-3).

    Сырой пул ``fresh`` нужен только для знаменателя «рассмотрено» и нормы
    канала. Карточки строятся ровно из строк ``select_viral_youtube`` (блоки 1
    и 2, БЕЗ кэпа/лимита отчёта) — поэтому список карточек сходится со
    счётчиком шапки, а полы (подписчики/просмотры/охват) действуют и на список,
    а не только на число. Главное число — кратность к норме канала.
    """
    pool = report_mod.viral_youtube_pool(conn, cutoff)
    fresh = [r for r in pool if report_mod._in_window(r["published_at"], cutoff)]
    norm = report_mod._norm_map(fresh, lambda r: r["source_id"],
                                lambda r: r["views"])
    # ТЗ-8 §1.2: промо-источники исключаются до ранжирования.
    promo_sources = report_mod.viral_promo_sources(
        conn, {report_mod._row_source_id(r) for r in pool}, cutoff=cutoff)
    sel_product, st1 = report_mod.select_viral_youtube(
        pool, fun=False, norm=norm, cutoff=cutoff, cap=False,
        promo_sources=promo_sources)
    sel_fun, st2 = report_mod.select_viral_youtube(
        pool, fun=True, norm=norm, cutoff=cutoff, cap=False,
        promo_sources=promo_sources)
    selected = sel_product + sel_fun

    # Язык канала: source.lang (после бэкфилла, ТЗ-3 §4.1); если пусто —
    # падаем на язык самого материала, чтобы метка была, а не пропадала.
    lang_by_source = {
        str(r["source_id"]): r["source_lang"]
        for r in fresh if r["source_id"] is not None
    }
    # Первоисточники (ТЗ-47 / ТЗ-3 §4.4): только каналы с first_moves > 0 и
    # отрицательным медианным лидом (опережает остальных). Нет строки — нет метки.
    first_movers = {
        str(row[0]) for row in conn.execute(
            "SELECT source_id FROM first_mover "
            "WHERE first_moves > 0 AND lead_time_median < 0")
        if row[0] is not None
    }

    cards: list = []
    buckets = {"ru": 0, "en": 0, "other": 0, "unknown": 0}
    for it in selected:
        views = int(it.views or 0)
        subs = it.subs
        coverage = it.coverage
        base = it.norm
        base_n = it.norm_n or 0
        # Главное число — кратность к норме канала (просмотры ÷ медиана канала),
        # та же семантика, что у X и Telegram. Норма осмысленна лишь при
        # достаточном числе материалов канала (иначе шум из 1–2 замеров).
        if base and base_n >= report_mod.VIRAL_YT_MIN_NORM_SAMPLE:
            norm_ratio = it.ratio
        else:
            norm_ratio = None
        # Вторая строка: подписчики, справочно — охват на подписчика и медиана.
        if subs:
            base_txt = f"подписчиков {_fmt_int(subs)}"
        else:
            base_txt = "подписчиков —"
        if coverage is not None:
            base_txt += f" · охват {_fmt_mult(coverage)} на подписчика"
        if base is not None and base_n >= report_mod.VIRAL_YT_MIN_NORM_SAMPLE:
            base_txt += f" · медиана {_fmt_int(base)}"
        elif base is not None:
            base_txt += " · медиана: мало данных"
        # Язык канала: лейбл на карточке + разбивка для шапки доски.
        channel_lang = lang_by_source.get(it.channel_key) or it.lang
        buckets[_lang_bucket(channel_lang)] += 1
        note = "первоисточник" if it.channel_key in first_movers else ""
        card = _card(
            platform="youtube",
            title=_short(it.title),
            channel=it.channel,
            lang_label=_lang_label(channel_lang),
            date=_fmt_date(it.published_at),
            url=it.url,
            main=_fmt_mult(norm_ratio),
            main_value=norm_ratio,
            absolute=f"{_fmt_int(views)} просмотров",
            base=base_txt,
            note=note,
            origin=it.origin,
        )
        card["abs_value"] = views
        # Числовые поля для проверок/отладки (в HTML не печатаются): по ним
        # тест доказывает, что полы отчёта действуют и на список карточек.
        card["views"] = views
        card["subs"] = subs
        card["coverage"] = coverage
        cards.append(card)

    skips = report_mod._new_skip_stats()
    for key in report_mod.SKIP_COUNTER_KEYS:
        skips[key] = st1.get(key, 0) + st2.get(key, 0)
    board = _finish("youtube", cards, len(pool),
                    st1["passed"] + st2["passed"], limit, skips=skips)
    # Кандидатов — ДО кэпа на канала и лимита доски: должно совпадать со
    # счётчиком шапки («прошло полы отчёта»).
    board["candidates"] = len(cards)
    board["lang_breakdown"] = buckets
    return board


def _telegram_board(conn, cutoff: str, limit: int) -> dict:
    pool = report_mod.viral_tg_pool(conn, cutoff)
    # ТЗ-8 §1.2: промо-источники (в основном Альфа-Выгодно) — вон из борды.
    promo_sources = report_mod.viral_promo_sources(
        conn, {report_mod._row_source_id(r) for r in pool}, cutoff=cutoff)
    _, stats = report_mod.viral_telegram_items(pool, cutoff,
                                               promo_sources=promo_sources)
    # Счётчики отсева — из отбора (пост-полы), а не из «сырого» цикла ниже.
    skips = {k: stats.get(k, 0) for k in report_mod.SKIP_COUNTER_KEYS}
    cards: list = []
    for r in pool:
        if not report_mod._in_window(r["published_at"], cutoff):
            continue
        views = r["views"]
        if views is None:
            continue
        base = r["median_views"]
        ratio = (views / base) if base and base > 0 else None
        key = r["channel_handle"] or r["channel_title"] or "(без канала)"
        # ТЗ-8 §1.1–1.3: статус/промо-источник/реклама — ДО карточки.
        text = r["title"] or ""
        reason = report_mod.viral_quality_skip(
            status=report_mod._row_source_status(r),
            source_id=report_mod._row_source_id(r), text=text,
            promo_sources=promo_sources)
        if reason:
            continue
        card = _card(
            platform="telegram",
            title=_short(text),
            channel=r["channel_title"] or key,
            date=_fmt_date(r["published_at"]),
            url=r["url"],
            main=_fmt_mult(ratio),
            main_value=ratio,
            absolute=f"{_fmt_int(views)} просмотров",
            base=f"медиана {_fmt_int(base)}",
            origin=report_mod.ads.classify_origin(text, r["url"]),
        )
        card["abs_value"] = int(views)
        cards.append(card)
    return _finish("telegram", cards, len(pool), stats["passed"], limit,
                   skips=skips)


# --------------------------------------------------------------------------- #
# Новые разделы ТЗ-4: «Темы окна», «Залетевшие сюжеты», «ИИ-инструменты»
# --------------------------------------------------------------------------- #
#: Тема третьего раздела (ТЗ-4 §3): материалы этой темы отбираются по полам
#: платформ из :mod:`tuber.analysis.report`.
AI_TOOLS_TOPIC = "ИИ-инструменты для обычных людей"
#: Ярлык-заглушка темы: в разделе тем его не считаем (ТЗ-4 §1).
_TOPIC_BLANK = "(без темы)"


def _topic_ok(topic) -> bool:
    """Годная тема: не NULL/пусто и не ярлык-заглушка «(без темы)»."""
    t = " ".join((topic or "").split())
    return bool(t) and t.lower() != _TOPIC_BLANK


def _window_starts(days: int, now: datetime | None = None) -> tuple[str, str]:
    """Начала текущего и предыдущего КАЛЕНДАРНЫХ окон (UTC, 00:00).

    Окно раздела тем — календарное, а не «катящееся»: замеры приёмки ТЗ-4
    (2877/2271/2051…) воспроизводятся именно при начале окна в полночь
    ``сегодня − days`` суток назад. Предыдущее окно — ровно такой же длины
    перед текущим. Возвращает ``(начало_текущего, начало_предыдущего)``.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    cur = (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    prev = (today - timedelta(days=2 * days)).strftime("%Y-%m-%d 00:00:00")
    return cur, prev


def _story_topic_majority(conn) -> dict:
    """Тема каждого сюжета — большинством голосов участников (ТЗ-4 §1/§2).

    Возвращает ``{story_id: topic}``. Голосуют только участники
    ``story_member``, у которых ``classification.topic`` непустой. Поле
    ``story.topic`` в базе пустое у всех строк и НЕ используется. Сюжеты без
    размеченных участников в карту не попадают.
    """
    rows = conn.execute(
        "WITH votes AS ("
        "  SELECT sm.story_id AS story_id, cl.topic AS topic, COUNT(*) AS n"
        "  FROM story_member sm"
        "  JOIN classification cl ON cl.content_id = sm.content_id"
        "  WHERE cl.topic IS NOT NULL AND TRIM(cl.topic) <> ''"
        "  GROUP BY sm.story_id, cl.topic)"
        " SELECT story_id, topic FROM ("
        "  SELECT story_id, topic, ROW_NUMBER() OVER ("
        "    PARTITION BY story_id ORDER BY n DESC, topic) AS rn FROM votes)"
        " WHERE rn = 1"
    ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def collect_topics(conn, days: int, now: datetime | None = None,
                   top: int = 10) -> dict:
    """Раздел «Темы окна»: топ тем окна, сюжеты и ускорение (ТЗ-4 §1)."""
    cur_start, prev_start = _window_starts(days, now)

    def counts(lo: str, hi: str | None = None) -> dict:
        sql = ("SELECT cl.topic, COUNT(*) FROM classification cl "
               "JOIN content c ON c.id = cl.content_id "
               "WHERE c.published_at >= ? AND cl.topic IS NOT NULL "
               "AND TRIM(cl.topic) <> ''")
        args: list = [lo]
        if hi is not None:
            sql += " AND c.published_at < ?"
            args.append(hi)
        sql += " GROUP BY cl.topic"
        return {r[0]: int(r[1]) for r in conn.execute(sql, args)}

    cur = {t: n for t, n in counts(cur_start).items() if _topic_ok(t)}
    prev = {t: n for t, n in counts(prev_start, cur_start).items()
            if _topic_ok(t)}
    majority = _story_topic_majority(conn)
    stories: dict = {}
    for t in majority.values():
        stories[t] = stories.get(t, 0) + 1
    # Набор — топ по объёму окна; порядок вывода — по ускорению (ТЗ-4 §1).
    top_ids = sorted(cur, key=lambda t: (-cur[t], t))[:top]
    items = []
    for t in top_ids:
        p = prev.get(t)
        items.append({
            "topic": t,
            "materials": cur[t],
            "stories": stories.get(t, 0),
            "previous": p,
            # Предыдущее окно пусто — честный прочерк, ноль НЕ подставляем.
            "accel": (cur[t] / p) if p else None,
        })
    items.sort(key=lambda r: (r["accel"] is None, -(r["accel"] or 0.0),
                              -r["materials"], r["topic"]))
    return {
        "window_start": cur_start,
        "prev_start": prev_start,
        "prev_end": cur_start,
        "top": top,
        "items": items,
    }


def collect_stories(conn, days: int, now: datetime | None = None,
                    top: int = 10) -> dict:
    """Раздел «Залетевшие сюжеты»: топ сюжетов окна (ТЗ-4 §2)."""
    cur_start, _ = _window_starts(days, now)
    rows = conn.execute(
        "SELECT id, canonical_content_id, first_content_id, first_mover_source_id,"
        " source_count, content_count, lead_time_min, first_pub_at"
        " FROM story WHERE first_pub_at >= ?"
        " ORDER BY source_count DESC, content_count DESC, id DESC LIMIT ?",
        (cur_start, top)).fetchall()
    majority = _story_topic_majority(conn)

    # Батч по контенту: заголовок и ссылка канонического (иначе первого).
    cids = set()
    for r in rows:
        c = r["canonical_content_id"] or r["first_content_id"]
        if c is not None:
            cids.add(int(c))
    content: dict = {}
    if cids:
        q = ",".join("?" * len(cids))
        for row in conn.execute(
                "SELECT c.id, c.url, c.title, c.text, cl.title_ru"
                " FROM content c LEFT JOIN classification cl"
                " ON cl.content_id = c.id WHERE c.id IN (" + q + ")",
                tuple(cids)):
            content[int(row["id"])] = row
    srcids = {int(r["first_mover_source_id"]) for r in rows
              if r["first_mover_source_id"] is not None}
    sources: dict = {}
    if srcids:
        q = ",".join("?" * len(srcids))
        for row in conn.execute(
                "SELECT id, handle, title FROM source WHERE id IN (" + q + ")",
                tuple(srcids)):
            sources[int(row["id"])] = row

    items = []
    for r in rows:
        cid = r["canonical_content_id"] or r["first_content_id"]
        c = content.get(int(cid)) if cid is not None else None
        title = url = ""
        if c is not None:
            # Русский из базы, иначе оригинал как есть (без живого перевода).
            title = (c["title_ru"] or c["title"] or c["text"] or "").strip()
            url = c["url"] or ""
        fm = (sources.get(int(r["first_mover_source_id"]))
              if r["first_mover_source_id"] is not None else None)
        lead = r["lead_time_min"]
        items.append({
            "story_id": int(r["id"]),
            "title": " ".join(title.split()) or "(без заголовка)",
            "topic": majority.get(int(r["id"])) or "",
            "source_count": int(r["source_count"] or 0),
            "content_count": int(r["content_count"] or 0),
            # lead_time_min — в МИНУТАХ; печатаем часы (ТЗ-4 §2).
            "lead_hours": (float(lead) / 60.0) if lead is not None else None,
            "first_mover": ((fm["handle"] or fm["title"]) if fm else "") or "",
            "url": url,
        })
    return {"window_start": cur_start, "top": top, "items": items}


def collect_ai_tools(conn, days: int, now: datetime | None = None,
                     limit: int = 10, subtopics_days: int = 7) -> dict:
    """Раздел «ИИ-инструменты»: отбор по полам платформ + подтемы (ТЗ-4 §3).

    Полы и нормы НЕ дублируются: YouTube и Telegram прогоняются через
    :func:`report.select_viral_youtube` / :func:`report.viral_telegram_items`;
    X — через публичную карту норм :func:`report.viral_x_ratio_map` и те же
    константы полов (как это уже делает доска X). Материалы — только темы
    :data:`AI_TOOLS_TOPIC`.
    """
    cur_start, _ = _window_starts(days, now)
    base = now or datetime.now(timezone.utc)
    x_cutoff = (base - timedelta(
        days=max(days, report_mod.VIRAL_X_AUTHOR_WINDOW_DAYS))
    ).strftime("%Y-%m-%d %H:%M:%S")

    # YouTube: пул темы + норма канала из ПОЛНОГО пула окна.
    yt_pool = report_mod.viral_youtube_pool(conn, cur_start)
    ai_yt = [r for r in yt_pool if (r["topic"] or "") == AI_TOOLS_TOPIC]
    yt_fresh = [r for r in yt_pool
                if report_mod._in_window(r["published_at"], cur_start)]
    norm = report_mod._norm_map(yt_fresh, lambda r: r["source_id"],
                                lambda r: r["views"])
    yt_items, yt_st = report_mod.select_viral_youtube(
        ai_yt, fun=True, norm=norm, cutoff=cur_start, cap=False,
        promo_sources=report_mod.viral_promo_sources(
            conn, {report_mod._row_source_id(r) for r in yt_pool}, cutoff=cur_start))

    # Telegram: база канала (source_baseline) не зависит от состава пула.
    tg_pool = report_mod.viral_tg_pool(conn, cur_start)
    ai_tg = [r for r in tg_pool if (r["topic"] or "") == AI_TOOLS_TOPIC]
    tg_items, tg_st = report_mod.viral_telegram_items(
        ai_tg, cur_start,
        promo_sources=report_mod.viral_promo_sources(
            conn, {report_mod._row_source_id(r) for r in tg_pool}, cutoff=cur_start))

    # X: норма автора — из полного пула (30 дней), полы — из отчёта.
    x_pool = report_mod.viral_x_pool(conn, x_cutoff)
    x_items: list = []
    x_considered = x_passed = 0
    for r in x_pool:
        if (r["topic"] or "") != AI_TOOLS_TOPIC:
            continue
        x_considered += 1
    if x_considered:
        ratios = report_mod.viral_x_ratio_map(x_pool)
        promo_x = report_mod.viral_promo_sources(
            conn, {report_mod._row_source_id(r) for r in x_pool}, cutoff=cur_start)
        for r in x_pool:
            if (r["topic"] or "") != AI_TOOLS_TOPIC:
                continue
            key = r["author_handle"] or r["source_handle"] or "(без автора)"
            pair = ratios.get(key)
            if pair is None:
                continue
            median, group_n = pair
            likes = r["likes"]
            if likes is None or likes < report_mod.VIRAL_X_MIN_LIKES:
                continue
            if group_n < report_mod.VIRAL_X_MIN_AUTHOR_POSTS:
                continue
            if median < report_mod.VIRAL_X_MIN_AUTHOR_MEDIAN or median <= 0:
                continue
            if not report_mod._in_window(r["published_at"], cur_start):
                continue
            ratio = likes / median
            if ratio < report_mod.VIRAL_X_MIN_RATIO:
                continue
            if report_mod.viral_quality_skip(
                    status=report_mod._row_source_status(r),
                    source_id=report_mod._row_source_id(r),
                    text=" ".join(p for p in (r["title"], r["title_ru"],
                                              r["summary_ru"]) if p),
                    promo_sources=promo_x):
                continue
            x_items.append({
                "content_id": r["content_id"],
                "title": (r["title_ru"] or r["title"] or "(без текста)"),
                "channel": key, "url": r["url"], "ratio": ratio,
                "likes": int(likes), "median": median,
            })
            x_passed += 1

    # Подтемы: сырые ярлыки базы за окно и за «живые» последние N суток.
    def subtopic_counts(lo: str) -> list:
        return [
            {"name": " ".join((row[0] or "").split()), "count": int(row[1])}
            for row in conn.execute(
                "SELECT cl.subtopic, COUNT(*) FROM classification cl"
                " JOIN content c ON c.id = cl.content_id"
                " WHERE cl.topic = ? AND c.published_at >= ?"
                "   AND cl.subtopic IS NOT NULL AND TRIM(cl.subtopic) <> ''"
                " GROUP BY cl.subtopic ORDER BY COUNT(*) DESC, cl.subtopic",
                (AI_TOOLS_TOPIC, lo))
        ]

    sub_7_start = (base.date() - timedelta(days=subtopics_days)
                   ).strftime("%Y-%m-%d 00:00:00")
    subtopic_by_content = {
        int(row[0]): " ".join((row[1] or "").split())
        for row in conn.execute(
            "SELECT cl.content_id, cl.subtopic FROM classification cl"
            " JOIN content c ON c.id = cl.content_id"
            " WHERE cl.topic = ? AND c.published_at >= ?"
            "   AND cl.subtopic IS NOT NULL AND TRIM(cl.subtopic) <> ''",
            (AI_TOOLS_TOPIC, cur_start))
    }

    cards: list = []
    for it in yt_items:
        ratio = it.ratio if (it.norm_n or 0) >= report_mod.VIRAL_YT_MIN_NORM_SAMPLE else None
        cards.append({
            "platform": "youtube",
            "title": _short(it.title),
            "channel": it.channel,
            "subtopic": subtopic_by_content.get(int(it.content_id), ""),
            "main_value": ratio,
            "absolute": f"{_fmt_int(it.views)} просмотров",
            "base": (f"медиана {_fmt_int(it.norm)}" if it.norm else "медиана —"),
            "url": it.url or "",
        })
    for it in tg_items:
        cards.append({
            "platform": "telegram",
            "title": _short(it.title),
            "channel": it.channel,
            "subtopic": subtopic_by_content.get(int(it.content_id), ""),
            "main_value": it.ratio,
            "absolute": f"{_fmt_int(it.views)} просмотров",
            "base": (f"медиана {_fmt_int(it.norm)}" if it.norm else "медиана —"),
            "url": it.url or "",
        })
    for it in x_items:
        cards.append({
            "platform": "x",
            "title": _short(it["title"]),
            "channel": it["channel"],
            "subtopic": subtopic_by_content.get(int(it["content_id"]), ""),
            "main_value": it["ratio"],
            "absolute": f"{_fmt_int(it['likes'])} лайков",
            "base": f"медиана {_fmt_int(it['median'])}",
            "url": it["url"] or "",
        })
    cards.sort(key=lambda c: (c["main_value"] is None,
                              -(c["main_value"] or 0.0), c["title"]))
    total = conn.execute(
        "SELECT COUNT(*) FROM classification cl JOIN content c"
        " ON c.id = cl.content_id WHERE cl.topic = ? AND c.published_at >= ?",
        (AI_TOOLS_TOPIC, cur_start)).fetchone()[0]
    return {
        "window_start": cur_start,
        "topic": AI_TOOLS_TOPIC,
        "materials": int(total),
        "limit": limit,
        "cards": cards[:limit],
        "found": len(cards),
        "stats": {
            "youtube": {"considered": len(ai_yt), "passed": yt_st["passed"]},
            "telegram": {"considered": len(ai_tg), "passed": tg_st["passed"]},
            "x": {"considered": x_considered, "passed": x_passed},
        },
        "subtopics": subtopic_counts(cur_start),
        "subtopics_7d": subtopic_counts(sub_7_start),
        "subtopics_days": subtopics_days,
    }


# --------------------------------------------------------------------------- #
# ТЗ-9: «Растущие каналы» — источник slivki._rising (та же метка и тот же отбор)
# --------------------------------------------------------------------------- #
#: Сколько карточек «растущих каналов» максимум (ТЗ-9 §1.1: разумный топ 15–20).
RISING_LIMIT = 20
#: Пояснение единиц главного числа карточки (ТЗ-9 §1.1).
RISING_GROWTH_UNIT = "рост подписчиков за 7 дней"


def _fmt_growth(g7) -> str:
    """Рост 7д в процентах: «+12,34%» (запятая — десятичный разделитель)."""
    return f"{g7 * 100:+.2f}%".replace(".", ",")


def _rising_growth(record: dict) -> tuple[str, float | None]:
    """Рост записи сливок: ``(статус, доля)``; ``unknown`` — числа нет.

    Нового роста здесь не считается. ``growth_status`` берётся как есть, а при
    его отсутствии выводится по наличию ``g7`` — ровно как в отборе кандидатов
    :mod:`tuber.analysis.slivki` (``g7 is None`` → рост 7д не измерим).
    """
    status = record.get("growth_status")
    g7 = record.get("g7")
    if status is None:
        status = "verified" if g7 is not None else "unknown"
    if status == "unknown" or g7 is None:
        return "unknown", None
    return "verified", g7


def rising_cards(records, *, topics=None, statuses=None, promo_ids=None,
                 limit: int = RISING_LIMIT) -> dict:
    """Карточки растущих каналов из ``slivki.build()['rising']`` (ТЗ-9).

    Чистая функция: рост берётся готовым (``g7``), новый не считается. Отсев —
    правила ТЗ-8 (:func:`tuber.analysis.report.viral_quality_skip`): кандидаты,
    иные неактивные статусы и промо-источники в карточки не идут. Канал без
    измеримого роста 7д в карточки не попадает (число не выдумывается), но
    считается в ``unknown``. Возвращает ``items`` (топ по росту), счётчики
    ``found``/``shown``/``unknown``/``total`` и отсев ``skip_*``.
    """
    topics = topics or {}
    statuses = statuses or {}
    promo_ids = promo_ids or set()
    skipped: dict[str, int] = {}
    items: list[dict] = []
    unknown = 0
    for rec in records:
        sid = rec.get("source_id")
        reason = report_mod.viral_quality_skip(
            status=statuses.get(sid), source_id=sid, text="",
            promo_sources=promo_ids)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        growth, g7 = _rising_growth(rec)
        if growth == "unknown":
            unknown += 1
            continue
        vp = rec.get("viral_post") or {}
        outlier = vp.get("outlier")
        items.append({
            "source_id": sid,
            "handle": rec.get("handle"),
            "platform": rec.get("platform"),
            "channel": rec.get("title") or rec.get("handle") or "(без имени)",
            "url": rec.get("url"),
            "subs": rec.get("subs"),
            "size_class": rec.get("size_class"),
            "g7": g7,
            "growth_text": _fmt_growth(g7),
            "growth_unit": RISING_GROWTH_UNIT,
            "breakout": (round(rec["breakout"], 2)
                         if rec.get("breakout") is not None else None),
            "topic": topics.get(sid) or None,
            "outlier": round(float(outlier), 2) if outlier else None,
            "outlier_url": vp.get("url"),
        })
    items.sort(key=lambda c: (c["g7"] if c["g7"] is not None else -1e9,
                              c["subs"] or 0), reverse=True)
    shown = items[:limit]
    return {
        "items": shown,
        "found": len(items),
        "shown": len(shown),
        "unknown": unknown,
        "total": len(records),
        "skip_candidate": skipped.get("candidate", 0),
        "skip_status": skipped.get("status", 0),
        "skip_promo_source": skipped.get("promo_source", 0),
    }


def _source_topics(conn, source_ids) -> dict:
    """Главная тема канала: самая частая ``classification.topic`` (ТЗ-9 §1.1).

    Тот же источник темы, что в разделе «Темы окна» (``classification.topic``),
    но сгруппированный по каналу. Пустая тема и ярлык-заглушка не считаются.
    Тема не выдумывается: нет размеченных материалов — канала нет в карте.
    """
    ids = [int(s) for s in source_ids if s is not None]
    best: dict[int, tuple[int, str]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT c.source_id AS sid, cl.topic AS topic, COUNT(*) AS n"
            " FROM classification cl JOIN content c ON c.id = cl.content_id"
            f" WHERE c.source_id IN ({placeholders})"
            " AND cl.topic IS NOT NULL AND TRIM(cl.topic) <> ''"
            " GROUP BY c.source_id, cl.topic", tuple(chunk)).fetchall()
        for row in rows:
            sid, topic, n = int(row["sid"]), row["topic"], int(row["n"])
            cur = best.get(sid)
            if cur is None or n > cur[0] or (n == cur[0] and topic < cur[1]):
                best[sid] = (n, topic)
    return {sid: pair[1] for sid, pair in best.items()}


def collect_rising(conn, *, now: datetime, limit: int = RISING_LIMIT) -> dict:
    """Секция «Растущие каналы» (ТЗ-9): записи и отбор — из ``slivki._rising``.

    Рост не пересчитывается: ``slivki.build`` возвращает готовый список
    ``rising`` (метка «восходящий»). К нему добавляются тема канала и отсев
    ТЗ-8 (статус/промо), затем ``rising_cards`` формирует карточки.
    """
    data = slivki_mod.build(conn, now=now, limit=limit)
    records = data.get("rising") or []
    ids = [r["source_id"] for r in records]
    statuses: dict[int, str | None] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT id, status FROM source WHERE id IN ({placeholders})",
                tuple(chunk)):
            statuses[int(row["id"])] = row["status"]
    promo_ids = report_mod.viral_promo_sources(conn, ids)
    topics = _source_topics(conn, ids)
    out = rising_cards(records, topics=topics, statuses=statuses,
                       promo_ids=promo_ids, limit=limit)
    out["day"] = data.get("day")
    out["days_available"] = data.get("rising_days_available")
    out["days_required"] = data.get("rising_days_required")
    return out


def collect_boards(conn, days: int, limit: int) -> dict:
    """Собрать данные трёх досок и трёх новых разделов (только чтение)."""
    cutoff, now_iso = report_mod._now_iso(days)
    x_cutoff, _ = report_mod._now_iso(
        max(days, report_mod.VIRAL_X_AUTHOR_WINDOW_DAYS))
    now_dt = datetime.fromisoformat(now_iso).replace(tzinfo=timezone.utc)
    boards = {
        "x": _x_board(conn, cutoff, x_cutoff, limit),
        "youtube": _youtube_board(conn, cutoff, limit),
        "telegram": _telegram_board(conn, cutoff, limit),
    }
    last_metric = conn.execute(
        "SELECT MAX(captured_at) FROM metric_snapshot").fetchone()[0]
    return {
        "generated_at": now_iso,
        "days": days,
        "limit": limit,
        "cutoff": cutoff,
        "last_metric": last_metric,
        "boards": boards,
        "topics": collect_topics(conn, days, now_dt, top=limit),
        "stories": collect_stories(conn, days, now_dt, top=limit),
        "ai_tools": collect_ai_tools(conn, days, now_dt, limit=limit),
        # ТЗ-9: «Растущие каналы» — те же данные и отбор, что в slivki._rising.
        "rising_channels": collect_rising(conn, now=now_dt, limit=limit),
    }


# --------------------------------------------------------------------------- #
# HTML (шаблон инлайн: один самодостаточный файл)
# --------------------------------------------------------------------------- #
_CSS = """
:root{
  --x:#0b0d0f; --youtube:#b3261e; --telegram:#20649b;
  --ink:#15181c; --muted:#4d555f; --line:#d7dce2; --bg:#eef1f5;
  --card:#ffffff; --radius:16px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  font-size:18px; line-height:1.45; overflow-wrap:break-word;
}
.wrap{max-width:1200px; margin:0 auto; padding:20px 16px 56px}
.hero{padding:22px 0 6px}
.hero h1{margin:0 0 10px; font-size:34px; line-height:1.15; letter-spacing:-.5px}
.hero p{margin:2px 0; color:var(--muted); font-size:17px}
.hero p b{color:var(--ink)}
nav.jump{display:flex; flex-wrap:wrap; gap:10px; margin:18px 0 6px}
nav.jump a{
  text-decoration:none; color:var(--ink); background:var(--card);
  border:1px solid var(--line); border-radius:999px; padding:8px 16px;
  font-weight:600; font-size:17px;
}
nav.period-nav{display:flex; flex-wrap:wrap; gap:8px; margin:18px 0 6px}
nav.period-nav a{
  text-decoration:none; color:var(--ink); background:#fff;
  border:1px solid #d7dce2; border-radius:8px; padding:8px 16px;
  font-weight:600; font-size:17px;
}
nav.period-nav a.active{background:#15181c; color:#fff; border-color:#15181c}
.board{margin-top:30px}
.board__head{display:flex; align-items:baseline; gap:12px; flex-wrap:wrap;
  border-bottom:3px solid var(--line); padding-bottom:10px}
.board__dot{width:16px; height:16px; border-radius:5px; align-self:center}
.board__title{margin:0; font-size:28px; letter-spacing:-.3px}
.board--x .board__title{color:var(--x)}
.board--youtube .board__title{color:var(--youtube)}
.board--telegram .board__title{color:var(--telegram)}
.board__note{margin:8px 0 0; color:var(--muted); font-size:16px}
.grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr));
  gap:14px; margin-top:16px}
.card{
  display:flex; flex-direction:column; gap:8px; background:var(--card);
  border:1px solid var(--line); border-radius:var(--radius); padding:14px;
  color:inherit; text-decoration:none; min-height:190px;
}
a.card:hover{border-color:var(--muted)}
.card__top{display:flex; align-items:center; justify-content:space-between;
  gap:8px; font-size:14px; color:var(--muted)}
.chip{font-weight:700; font-size:13px; letter-spacing:.3px; padding:3px 9px;
  border-radius:8px; color:#fff; background:var(--x)}
.board--x .card .chip{background:var(--x)}
.board--youtube .card .chip{background:var(--youtube)}
.board--telegram .card .chip{background:var(--telegram)}
.card__main{font-size:40px; font-weight:800; line-height:1.05;
  letter-spacing:-1px}
.board--x .card__main{color:var(--x)}
.board--youtube .card__main{color:var(--youtube)}
.board--telegram .card__main{color:var(--telegram)}
.card__sub{font-size:16px; color:var(--ink)}
.card__sub span{color:var(--muted)}
.card__note{font-size:14px; color:var(--muted); font-weight:600;
  align-self:flex-start; padding:2px 8px; border:1px solid var(--line);
  border-radius:999px}
.card__desc{margin:2px 0 0; font-size:16px; color:var(--ink); flex:1 1 auto}
.card__who{font-size:15px; color:var(--muted); font-weight:600}
.card__lang{font-weight:700; font-size:13px; color:var(--muted);
  border:1px solid var(--line); border-radius:6px; padding:1px 6px;
  margin-left:4px; white-space:nowrap}
.empty{margin-top:16px; padding:18px; background:var(--card);
  border:1px dashed var(--line); border-radius:var(--radius); color:var(--muted)}
footer{margin-top:40px; padding-top:16px; border-top:1px solid var(--line);
  color:var(--muted); font-size:15px}
.panel{margin-top:34px}
.panel__head{border-bottom:3px solid var(--line); padding-bottom:10px}
.panel__title{margin:0; font-size:28px; letter-spacing:-.3px}
.panel__note{margin:8px 0 0; color:var(--muted); font-size:16px}
.charts{display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
  gap:14px; margin-top:16px}
.charts img{width:100%; height:auto; background:var(--card);
  border:1px solid var(--line); border-radius:var(--radius)}
.row{display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
  background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:10px 14px; font-size:17px}
.rows{display:flex; flex-direction:column; gap:8px; margin-top:16px}
.row__rank{min-width:26px; font-weight:800; color:var(--muted)}
.row__name{font-weight:700; flex:1 1 220px}
.row__name a{color:inherit}
.row__num{color:var(--muted)}
.row__num b{color:var(--ink)}
.row__accel{margin-left:auto; font-weight:800}
.row__topic{font-size:14px; font-weight:700; color:var(--muted);
  border:1px solid var(--line); border-radius:999px; padding:2px 8px}
.row__ratio{margin-left:auto; font-weight:800; font-size:22px}
.chip--x{background:var(--x)}
.chip--youtube{background:var(--youtube)}
.chip--telegram{background:var(--telegram)}
.subtopics{display:flex; flex-wrap:wrap; gap:8px; margin-top:12px}
.subtopic{background:var(--card); border:1px solid var(--line);
  border-radius:999px; padding:6px 12px; font-size:15px; color:var(--muted)}
.subtopic b{color:var(--ink); font-weight:800}
.muted{color:var(--muted)}
.rcards{display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
  gap:14px; margin-top:16px}
.rcard{display:flex; flex-direction:column; gap:6px; background:var(--card);
  border:1px solid var(--line); border-radius:var(--radius); padding:14px;
  color:inherit; text-decoration:none}
a.rcard:hover{border-color:var(--muted)}
.rcard__top{display:flex; align-items:center; gap:8px; flex-wrap:wrap;
  font-size:14px; color:var(--muted)}
.rcard__name{font-size:20px; font-weight:800; line-height:1.2}
.rcard__growth{font-size:38px; font-weight:800; letter-spacing:-1px;
  line-height:1.05; color:#127a3d}
.rcard__growth small{display:block; font-size:14px; font-weight:600;
  letter-spacing:0; color:var(--muted)}
.rcard__meta{font-size:15px; color:var(--muted)}
.rcard__meta b{color:var(--ink)}
.rcard__topic{font-size:15px; color:var(--ink)}
@media (max-width:420px){
  .hero h1{font-size:29px}
  .card__main{font-size:36px}
  .rcard__growth{font-size:32px}
}
"""


def _card_html(card: dict) -> str:
    tag = "a" if card["url"] else "div"
    href = f' href="{html.escape(card["url"], quote=True)}"' if card["url"] else ""
    close = "a" if card["url"] else "div"
    platform = card["platform"]
    chip = {p[0]: p[1] for p in PLATFORMS}[platform]
    sub = html.escape(card["absolute"])
    if card["base"]:
        sub += ' <span>· ' + html.escape(card["base"]) + "</span>"
    note = (f'<div class="card__note">{html.escape(card["note"])}</div>'
            if card["note"] else "")
    lang_label = card.get("lang_label") or ""
    lang_html = (f' <span class="card__lang">{html.escape(lang_label)}</span>'
                 if lang_label else "")
    # ТЗ-8 §1.3: агрегатор/пересказ не удаляем, а помечаем на карточке.
    origin_html = (' <span class="card__lang">пересказ</span>'
                   if card.get("origin") == "retelling" else "")
    return (
        f'<{tag} class="card" data-platform="{platform}"'
        f' data-channel="{html.escape(card["channel"], quote=True)}"{href}>'
        f'<div class="card__top"><span class="chip">{html.escape(chip)}</span>'
        f'<time>{html.escape(card["date"])}</time></div>'
        f'<div class="card__main">{html.escape(card["main"])}</div>'
        f'<div class="card__sub">{sub}</div>'
        f'{note}'
        f'<p class="card__desc">{html.escape(card["title"])}</p>'
        f'<div class="card__who">{html.escape(card["channel"])}{lang_html}'
        f'{origin_html}</div>'
        f'</{close}>'
    )


#: Графики, которые кладёт рядом ``tools/build_charts.py`` (ТЗ-4 §4).
TOPIC_CHARTS = (
    ("topics_daily.png", "Динамика топ-5 тем по дням за окно"),
    ("topics_bar.png", "Топ-12 тем по числу материалов за окно"),
)
#: Имя графика поступления по платформам (ТЗ-4 §4).
PLATFORM_CHART = ("platform_daily.png",
                  "Поступление материалов по платформам по дням")


def _fmt_accel(value) -> str:
    """Ускорение темы вида «×1,2» (или честный прочерк)."""
    if value is None:
        return "—"
    return "×" + f"{float(value):.1f}".replace(".", ",")


def _fmt_lead(hours) -> str:
    """Опережение сюжета: «первый на 7,6 ч раньше» (или прочерк)."""
    if hours is None:
        return "—"
    return "первый на " + f"{float(hours):.1f}".replace(".", ",") + " ч раньше"


def _date_range_label(start: str, now_utc: datetime) -> str:
    """Диапазон окна по фактическим данным: «28.09.2026 — 08.10.2026»."""
    try:
        d0 = date.fromisoformat(str(start)[:10]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        d0 = str(start)
    return f"{d0} — {now_utc.strftime('%d.%m.%Y')}"


def _panel_head(title: str, note: str) -> str:
    return (f'<div class="panel__head"><h2 class="panel__title">'
            f'{html.escape(title)}</h2></div>'
            f'<p class="panel__note">{html.escape(note)}</p>')


def _section_topics(t: dict, now_utc: datetime) -> str:
    rows = []
    for i, r in enumerate(t["items"], 1):
        rows.append(
            '<div class="row">'
            f'<span class="row__rank">{i}</span>'
            f'<span class="row__name">{html.escape(r["topic"])}</span>'
            f'<span class="row__num"><b>{_fmt_int(r["materials"])}</b> '
            f'материалов</span>'
            f'<span class="row__num">{_fmt_int(r["stories"])} сюжетов</span>'
            f'<span class="row__accel">{html.escape(_fmt_accel(r["accel"]))}'
            f'</span></div>')
    charts = "".join(
        f'<img src="{name}" alt="{html.escape(alt, quote=True)}">'
        for name, alt in TOPIC_CHARTS)
    note = (f"Окно {_date_range_label(t['window_start'], now_utc)} UTC. "
            f"Ускорение — материалы окна ÷ материалы предыдущего такого же окна "
            f"(с {t['prev_start'][:10]}); пустое прошлое окно — прочерк, ноль не "
            f"подставляется. Сюжеты — большинство участников с этой темой.")
    return (
        '<section class="panel" id="topics">'
        + _panel_head("Темы окна — что разогревается", note)
        + f'<div class="charts">{charts}</div>'
        + f'<div class="rows">{"".join(rows)}</div></section>')


def _section_stories(s: dict, now_utc: datetime) -> str:
    rows = []
    for i, r in enumerate(s["items"], 1):
        name = html.escape(_short(r["title"], 150))
        if r["url"]:
            name = f'<a href="{html.escape(r["url"], quote=True)}">{name}</a>'
        topic = (f'<span class="row__topic">{html.escape(r["topic"])}</span>'
                 if r["topic"] else "")
        fm = (f' · первоисточник {html.escape(r["first_mover"])}'
              if r["first_mover"] else "")
        rows.append(
            '<div class="row">'
            f'<span class="row__rank">{i}</span>'
            f'<span class="row__name">{name}</span>{topic}'
            f'<span class="row__num"><b>{_fmt_int(r["source_count"])}</b> '
            f'источников</span>'
            f'<span class="row__num">{_fmt_int(r["content_count"])} материалов'
            f'</span>'
            f'<span class="row__num">{html.escape(_fmt_lead(r["lead_hours"]))}'
            f'{fm}</span></div>')
    note = (f"Окно {_date_range_label(s['window_start'], now_utc)} UTC. Топ по "
            f"числу источников, затем по числу материалов. Заголовок — русский "
            f"из базы (иначе оригинал как есть, без живого перевода); тема — "
            f"большинство участников сюжета.")
    return ('<section class="panel" id="stories">'
            + _panel_head("Залетевшие сюжеты — что подхватили все", note)
            + f'<div class="rows">{"".join(rows)}</div></section>')


def _section_ai_tools(a: dict, now_utc: datetime) -> str:
    rows = []
    for i, c in enumerate(a["cards"], 1):
        name = html.escape(c["title"])
        if c["url"]:
            name = f'<a href="{html.escape(c["url"], quote=True)}">{name}</a>'
        chip_name = {p[0]: p[1] for p in PLATFORMS}[c["platform"]]
        sub = (f'<span class="row__topic">{html.escape(c["subtopic"])}</span>'
               if c["subtopic"] else "")
        rows.append(
            '<div class="row">'
            f'<span class="row__rank">{i}</span>'
            f'<span class="chip chip--{c["platform"]}">'
            f'{html.escape(chip_name)}</span>'
            f'<span class="row__name">{name}</span>{sub}'
            f'<span class="row__num">{html.escape(c["absolute"])} · '
            f'{html.escape(c["base"])}</span>'
            f'<span class="row__ratio">'
            f'{html.escape(_fmt_mult(c["main_value"]))}</span></div>')
    if not rows:
        rows.append('<div class="empty">За это окно материалов темы, прошедших '
                    'полы платформ, не нашлось.</div>')
    subs = "".join(
        f'<span class="subtopic">{html.escape(s["name"])} '
        f'<b>{_fmt_int(s["count"])}</b></span>'
        for s in a["subtopics_7d"][:SUBTOPICS_SHOWN])
    if not subs:
        subs = ('<span class="subtopic">Подтем с данными нет — прочерк, '
                'не выдумываем</span>')
    st = a["stats"]
    note = (f"Тема «{a['topic']}»: {_fmt_int(a['materials'])} материалов за окно. "
            f"Отбор — теми же полами, что у досок: YouTube прошло "
            f"{_fmt_int(st['youtube']['passed'])} из "
            f"{_fmt_int(st['youtube']['considered'])}, Telegram "
            f"{_fmt_int(st['telegram']['passed'])} из "
            f"{_fmt_int(st['telegram']['considered'])}, X "
            f"{_fmt_int(st['x']['passed'])} из {_fmt_int(st['x']['considered'])}. "
            f"Подтемы — за последние {a['subtopics_days']} суток (сырые ярлыки "
            f"базы, без нормализации).")
    return (
        '<section class="panel" id="ai-tools">'
        + _panel_head("ИИ-инструменты — что залетает среди инструментов", note)
        + f'<div class="rows">{"".join(rows)}</div>'
        + f'<div class="subtopics">{subs}</div></section>')


def _rising_card_html(card: dict) -> str:
    """Карточка растущего канала: главное — рост 7д, без вложенных ссылок."""
    card_url = card["url"]
    tag = "a" if card_url else "div"
    href = (f' href="{html.escape(card_url, quote=True)}"' if card_url else "")
    chip = {p[0]: p[1] for p in PLATFORMS}.get(card["platform"],
                                               card["platform"] or "")
    topic = (f'<div class="rcard__topic">Тема: '
             f'{html.escape(card["topic"])}</div>' if card["topic"] else "")
    virus = ""
    if card["outlier"] and card["outlier_url"]:
        post = ("" if card_url else
                f' · <a href="{html.escape(card["outlier_url"], quote=True)}">'
                f'лучший пост</a>')
        virus = (f'<div class="rcard__meta">Выброс: '
                 f'<b>{html.escape(_fmt_mult(card["outlier"]))}</b> к норме'
                 f'{post}</div>')
    breakout = (f'<div class="rcard__meta">breakout '
                f'<b>{card["breakout"]:g}</b></div>'
                if card["breakout"] is not None else "")
    return (
        f'<{tag} class="rcard" data-channel='
        f'"{html.escape(card["channel"], quote=True)}"{href}>'
        f'<div class="rcard__top"><span class="chip">{html.escape(chip)}</span>'
        f'<span>{html.escape(card["size_class"] or "размер неизвестен")}</span>'
        f'</div>'
        f'<div class="rcard__name">{html.escape(card["channel"])}</div>'
        f'<div class="rcard__growth">{html.escape(card["growth_text"])}'
        f'<small>{html.escape(card["growth_unit"])}</small></div>'
        f'<div class="rcard__meta">Подписчиков: '
        f'<b>{html.escape(_fmt_int(card["subs"]))}</b></div>'
        f'{topic}{virus}{breakout}</{tag}>')


def _section_rising(r: dict, now_utc: datetime) -> str:
    """Секция «Растущие каналы» (ТЗ-9). Пустой список — секции нет."""
    if not r.get("items"):
        return ""
    cards = "".join(_rising_card_html(c) for c in r["items"])
    days, req = r.get("days_available"), r.get("days_required")
    window = (f"окно метки — {days} дн из {req}"
              if days is not None and req is not None else "окно метки")
    note = (f"Отбор — существующая метка «восходящий» из slivki (breakout "
            f"≥ +2σ размерного класса площадки, 2 дня из 3; {window}). Показано "
            f"{_fmt_int(r['shown'])} из {_fmt_int(r['found'])} каналов с "
            f"измеримым ростом (метка всего у {_fmt_int(r['total'])}). Порядок — "
            f"по росту подписчиков за 7 дней. Число не выдумывается.")
    honest = []
    if r.get("unknown"):
        honest.append(f"у {_fmt_int(r['unknown'])} рост 7д не измерим (нет ряда "
                      f"истории)")
    if r.get("skip_candidate"):
        honest.append(f"{_fmt_int(r['skip_candidate'])} — кандидаты (не "
                      f"верифицированы)")
    if r.get("skip_status"):
        honest.append(f"{_fmt_int(r['skip_status'])} — неактивный статус "
                      f"источника")
    if r.get("skip_promo_source"):
        honest.append(f"{_fmt_int(r['skip_promo_source'])} — промо-источники")
    if honest:
        note += " Оговорки: " + "; ".join(honest) + "."
    return (
        '<section class="panel" id="rising">'
        + _panel_head("Растущие каналы — кто набирает ход", note)
        + f'<div class="rcards">{cards}</div></section>')


def _section_platform_chart() -> str:
    name, alt = PLATFORM_CHART
    return (
        '<section class="panel" id="platforms">'
        + _panel_head("Поступление по платформам",
                      "Сколько материалов вышло в каждый день окна — по X, "
                      "YouTube и Telegram.")
        + f'<div class="charts"><img src="{name}" '
        f'alt="{html.escape(alt, quote=True)}"></div></section>')


def _period_nav(days: int) -> str:
    """Переключатель окон ТЗ-12: ссылки на статичные ``/boards/<slug>/``.

    Ссылки абсолютные (домен фиксирован), активный пункт — текущее окно.
    """
    links = []
    for value, slug, label in PERIODS:
        cls = ' class="active"' if value == int(days) else ' class=""'
        links.append(f'<a href="/boards/{slug}/"{cls}>{label}</a>')
    return '<nav class="period-nav">' + "".join(links) + "</nav>"


def render_html(data: dict) -> str:
    """Собрать самодостаточный HTML из собранных данных."""
    now_utc = datetime.fromisoformat(data["generated_at"]).replace(
        tzinfo=timezone.utc)
    built_utc = now_utc.strftime("%d.%m.%Y %H:%M")
    built_msk = _msk(now_utc).strftime("%d.%m.%Y %H:%M")
    last_metric_txt = (_fmt_date(data["last_metric"]) + " "
                       + data["last_metric"][11:16]
                       if data["last_metric"] else "нет данных")

    # ТЗ-12 §3: окно 1 день читается как «24 часа»; остальные — «N дней».
    days = int(data["days"])
    window_label = ("<b>24 часа</b>" if days == 1
                    else f"<b>{days}</b> дней")

    nav = ""
    if data.get("rising_channels", {}).get("items"):
        nav += '<a href="#rising">Растущие каналы</a>'
    nav += (
        '<a href="#topics">Темы окна</a>'
        '<a href="#ai-tools">ИИ-инструменты</a>'
        '<a href="#stories">Залетевшие сюжеты</a>'
    )
    nav += "".join(f'<a href="#{pid}">{html.escape(name)}</a>'
                   for pid, name, _, _ in PLATFORMS)

    new_sections = (
        _section_rising(data.get("rising_channels", {}), now_utc)
        + _section_topics(data["topics"], now_utc)
        + _section_ai_tools(data["ai_tools"], now_utc)
        + _section_stories(data["stories"], now_utc)
        + _section_platform_chart()
    )

    sections = []
    for pid, name, accent, _ in PLATFORMS:
        b = data["boards"][pid]
        cards = b["cards"]
        note = (f"Рассмотрено {_fmt_int(b['considered'])}, прошло полы отчёта "
                f"{_fmt_int(b['passed'])}. Найдено {_fmt_int(b['found'])} "
                f"(после отбраковки пустых и служебных заголовков и кэпа на "
                f"автора); на доске — {b['shown']} "
                f"(не больше {PER_KEY} на автора).")
        if "candidates" in b:
            note += (f" Карточек-кандидатов (до кэпа и лимита): "
                     f"{_fmt_int(b['candidates'])}.")
        br = b.get("lang_breakdown")
        if br:
            note += (f" Язык каналов прошедших: ru {_fmt_int(br['ru'])}, "
                     f"en {_fmt_int(br['en'])}, прочие {_fmt_int(br['other'])}")
            if br.get("unknown"):
                note += f", язык не определён {_fmt_int(br['unknown'])}"
            note += "."
        if cards:
            grid = "".join(_card_html(c) for c in cards)
        else:
            grid = ('<div class="empty">За это окно материалов, прошедших '
                    'отбор, не нашлось.</div>')
        sections.append(
            f'<section class="board board--{pid}" id="{pid}">'
            f'<div class="board__head">'
            f'<span class="board__dot" style="background:{accent}"></span>'
            f'<h2 class="board__title">{html.escape(name)}</h2></div>'
            f'<p class="board__note">{html.escape(note)}</p>'
            f'<div class="grid">{grid}</div></section>'
        )

    return (
        "<!doctype html>\n"
        '<html lang="ru">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        "<title>Что залетело</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n<div class=\"wrap\">\n"
        '<header class="hero">\n<h1>Что залетело</h1>\n'
        f'<p>Окно: последние {window_label} '
        f'(с {html.escape(data["cutoff"])} UTC).</p>\n'
        f'<p>Собрано: {built_utc} UTC · {built_msk} МСК.</p>\n'
        f'<p>Последний замер метрик: {html.escape(last_metric_txt)} '
        f'(UTC).</p>\n</header>\n'
        f'{_period_nav(data["days"])}\n'
        f'<nav class="jump">{nav}</nav>\n<main>\n'
        + new_sections + "\n".join(sections) +
        "\n</main>\n<footer>"
        "Источник — боевая база Tuber, только чтение. Метрики те же, что в "
        "полном отчёте: кратность к норме автора/канала. Без подписчиков или "
        "медианы кратность не выдумывается (прочерк). Графики — из "
        "tools/build_charts.py (matplotlib), кладутся рядом с этой страницей."
        "</footer>\n</div>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# Сборка и CLI
# --------------------------------------------------------------------------- #
def build(db_path: str, days: int, limit: int, out_path: str) -> dict:
    """Прочитать базу, собрать страницу и ``boards.json``; вернуть данные."""
    conn = core_db.connect(db_path, readonly=True)
    try:
        data = collect_boards(conn, days, limit)
    finally:
        conn.close()

    page = render_html(data)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    (out.parent / "boards.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return data


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_boards",
        description="Собрать страницу «Что залетело» — три доски виральности")
    parser.add_argument("--days", type=int, default=10,
                        help="окно отбора в днях (по умолчанию 10)")
    parser.add_argument("--limit", type=int, default=50,
                        help="карточек на доску (по умолчанию 50)")
    parser.add_argument("--out", default=str(_ROOT / "reports" / "boards"
                                             / "index.html"),
                        help="куда писать HTML (по умолчанию "
                             "reports/boards/index.html)")
    parser.add_argument("--db", dest="db_path", default=None,
                        help="путь к единой базе (по умолчанию боевая)")
    args = parser.parse_args(argv)

    # ТЗ-12 §2: жёсткий потолок 10 дней — посты старше никогда не попадут
    # на страницу даже при ошибке в обёртке или ручном запуске.
    if args.days > 10:
        print("Ошибка: максимальное окно — 10 дней", file=sys.stderr)
        return 2

    db_path = config.db_path(args.db_path)
    if not Path(db_path).exists():
        print(f"ошибка: базы нет: {db_path}", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc)
    data = build(db_path, args.days, args.limit, args.out)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    size = Path(args.out).stat().st_size

    print(f"страница: {args.out}")
    print(f"JSON:     {Path(args.out).parent / 'boards.json'}")
    print(f"размер:   {size} байт, сборка {elapsed:.2f} с")
    for pid, name, _, _ in PLATFORMS:
        b = data["boards"][pid]
        print(f"{name}: карточек {b['shown']} "
              f"(рассмотрено {b['considered']}, прошло полы {b['passed']})")
    print(f"Темы окна: {len(data['topics']['items'])} тем "
          f"(окно с {data['topics']['window_start']})")
    print(f"Залетевшие сюжеты: {len(data['stories']['items'])} сюжетов")
    rc = data["rising_channels"]
    print(f"Растущие каналы: {rc['shown']} карточек "
          f"(всего с меткой {rc['total']}, с измеримым ростом {rc['found']}, "
          f"рост не измерим у {rc['unknown']})")
    ai = data["ai_tools"]
    print(f"ИИ-инструменты: {len(ai['cards'])} карточек "
          f"(материалов темы {ai['materials']}, подтем за "
          f"{ai['subtopics_days']} суток {len(ai['subtopics_7d'])})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
