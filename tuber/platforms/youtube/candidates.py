"""Внешние кандидаты: экспорт Telegram/X из описаний и импорт YouTube-каналов.

Часть B (ТЗ-16). Мост к реестрам Telegram и X: в непустых описаниях видео
лежат тысячи уникальных каналов Telegram и аккаунтов X, которые не нужно
искать платной квотой. Команда ``candidates-export`` выгружает их в JSONL,
``candidates-import`` тянет YouTube-каналы из фида через ``videos.list``.

Сеть в экспорте не используется: читаем только описания/заголовки/теги.
Импорт ходит в сеть (``videos.list``, 1 unit за вызов) и пишет только в
``channel_candidates``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config, store as db, api as yt

# --- общее -----------------------------------------------------------------

# Источник по умолчанию для экспортированных кандидатов.
EXTERNAL_SOURCE = "tuber-os:video-descriptions"
# Источник кандидатов, пришедших из фида Telegram (kind="youtube").
FEED_SOURCE = "tg:tuber-telegram"
# Каталог обмена по умолчанию.
EXCHANGE_DIR = config.TUBER_DIR / "data" / "exchange"
DEFAULT_OUT = EXCHANGE_DIR / "external_candidates.jsonl"
# Сколько примеров-цитат хранить на кандидата.
MAX_EXAMPLES = 3
# Ограничение длины примера, знаков.
EXAMPLE_MAX_LEN = 120
# Окно вокруг ссылки, в котором ищем ИИ-термины, знаков в каждую сторону.
AI_WINDOW = 80


class CandidatesError(Exception):
    """Понятная ошибка разбора фида/файла без трейсбека владельцу."""


# --- разбор ссылок ---------------------------------------------------------

# Ссылка на Telegram: t.me/<...> или telegram.me/<...>.
_TELEGRAM_LINK_RE = re.compile(
    r"(?:t|telegram)\.me/([^\s)\]\"'<>|,;]+)", re.IGNORECASE
)
# Ссылка на X: x.com/<...> или twitter.com/<...>.
_X_LINK_RE = re.compile(
    r"(?:x|twitter)\.com/([^\s)\]\"'<>|,;]+)", re.IGNORECASE
)
# Шаблон публичного имени Telegram-канала.
_TELEGRAM_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
# Шаблон аккаунта X.
_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# Служебные первые сегменты пути Telegram (t.me/s/, /c/, /joinchat/).
_TELEGRAM_SERVICE = frozenset({"s", "c", "joinchat"})
# Служебные пути X, которые не являются аккаунтами.
_X_RESERVED = frozenset({
    "i", "home", "intent", "share", "search", "hashtag", "status", "explore",
    "settings", "login", "signup", "messages", "notifications", "tos", "privacy",
})


def _first_segment(raw: str) -> str:
    """Первый сегмент пути без query/fragment, слэшей и концевого мусора."""
    raw = raw.split("?", 1)[0].split("#", 1)[0].strip("/")
    seg = raw.split("/", 1)[0] if raw else ""
    # Имя канала/аккаунта состоит из [A-Za-z0-9_]: срезаем концевой мусор
    # (точка, скобка и т.п.), который попал из предложения вокруг ссылки.
    while seg and not (seg[-1].isalnum() or seg[-1] == "_"):
        seg = seg[:-1]
    return seg


def extract_mentions(text: str) -> list[tuple[str, str]]:
    """Извлечь (kind, handle) из текста. Без дедупликации (для счёта)."""
    return [(kind, handle) for kind, handle, _s, _e in _iter_links(text)]


# --- ИИ-подсказка (ai_hint) ------------------------------------------------

# ИИ-термины: закрытый список тем проекта (config.TOPICS) плюс базовые
# термины про ИИ/машинное обучение. Никаких вызовов LLM — только сравнение.
AI_HINT_TERMS: tuple[str, ...] = tuple(sorted(set(config.TOPICS) | {
    "ai", "ии", "ml", "agi", "llm", "gpt", "chatgpt", "openai", "anthropic",
    "claude", "gemini", "midjourney", "stable diffusion", "copilot",
    "нейросет", "нейронн", "машинное обучение", "machine learning",
    "deep learning", "искусственный интеллект", "artificial intelligence",
}))

def _term_pattern(term: str) -> str:
    """Regex одного ИИ-термина: левая граница у всех, правая — у не-стемов.

    Русские термины-основы («нейросет», «нейронн») должны ловить словоформы
    («нейросети», «нейросетей»), поэтому для кириллицы правая граница не
    ставится. Для ASCII-сокращений («ai», «ml», «gpt») правая граница нужна,
    иначе «ai» ловится внутри «air»/«said».
    """
    prefix = r"(?<![0-9A-Za-zА-Яа-яЁё_])"
    if re.search(r"[А-Яа-яЁё]", term):
        return prefix + re.escape(term)
    return prefix + re.escape(term) + r"(?![0-9A-Za-zА-Яа-яЁё_])"


_AI_HINT_RE = re.compile(
    "|".join(_term_pattern(t) for t in AI_HINT_TERMS),
    re.IGNORECASE,
)


def has_ai_hint(text: str | None) -> bool:
    """Есть ли ИИ-термин в тексте (без вызовов LLM)."""
    if not text:
        return False
    return _AI_HINT_RE.search(text) is not None


# --- агрегация экспорта ----------------------------------------------------


def _iso(ts: int | None) -> str | None:
    """Unixtime → ISO 8601 UTC (или None)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _iter_links(text: str) -> Iterable[tuple[str, str, int, int]]:
    """Найти ссылки в тексте: (kind, handle, start, end) по порядку.

    Позиции (start/end) нужны, чтобы взять окрестность ссылки для ai_hint и
    примера. Дедупликация не делается — вызывающий код считает упоминания.
    """
    if not text:
        return
    found: list[tuple[str, str, int, int]] = []
    for m in _TELEGRAM_LINK_RE.finditer(text):
        handle = _first_segment(m.group(1)).lower()
        if not handle or handle.startswith("+") or handle in _TELEGRAM_SERVICE:
            continue
        if _TELEGRAM_HANDLE_RE.match(handle):
            found.append(("telegram", handle, m.start(), m.end()))
    for m in _X_LINK_RE.finditer(text):
        handle = _first_segment(m.group(1)).lower()
        if not handle or handle in _X_RESERVED:
            continue
        if _X_HANDLE_RE.match(handle):
            found.append(("x", handle, m.start(), m.end()))
    found.sort(key=lambda t: t[2])
    return found


def collect_external(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Собрать кандидатов из описаний/заголовков/тегов всех видео.

    Возвращает словарь (kind, handle) → агрегат с mentions, videos,
    first_seen/last_seen, examples, ai_hint. Только чтение.
    """
    agg: dict[tuple[str, str], dict] = {}
    sql = (
        "SELECT video_id, title, description, tags, published_at FROM videos "
        "WHERE (description IS NOT NULL AND description <> '') "
        "   OR (title IS NOT NULL AND title <> '') "
        "   OR (tags IS NOT NULL AND tags <> '')"
    )
    for row in conn.execute(sql):
        video_id = row["video_id"]
        published = row["published_at"]
        fields: list[tuple[str, str]] = []
        if row["description"]:
            fields.append(("description", row["description"]))
        if row["title"]:
            fields.append(("title", row["title"]))
        if row["tags"]:
            fields.append(("tags", str(row["tags"])))
        if not fields:
            continue
        # ИИ-термин в заголовке/тегах — подсказка для всех ссылок видео.
        head_hint = has_ai_hint(row["title"]) or has_ai_hint(
            str(row["tags"]) if row["tags"] else None
        )
        counted_in_video: set[tuple[str, str]] = set()
        for field, text in fields:
            for kind, handle, start, end in _iter_links(text):
                key = (kind, handle)
                entry = agg.get(key)
                if entry is None:
                    entry = {
                        "kind": kind,
                        "handle": handle,
                        "mentions": 0,
                        "videos": set(),
                        "first_seen": None,
                        "last_seen": None,
                        "ai_hint": 0,
                        "examples": [],
                    }
                    agg[key] = entry
                entry["mentions"] += 1
                if key not in counted_in_video:
                    counted_in_video.add(key)
                    entry["videos"].add(video_id)
                if published is not None:
                    p = int(published)
                    if entry["first_seen"] is None or p < entry["first_seen"]:
                        entry["first_seen"] = p
                    if entry["last_seen"] is None or p > entry["last_seen"]:
                        entry["last_seen"] = p
                # ai_hint: заголовок/теги или окрестность самой ссылки.
                hint = head_hint or has_ai_hint(_window(text, start, end))
                if hint:
                    entry["ai_hint"] = 1
                if field == "description" and len(entry["examples"]) < MAX_EXAMPLES:
                    frag = _snippet(text, start, end)
                    if frag:
                        line = f"{video_id}: {frag}"[:EXAMPLE_MAX_LEN]
                        if line not in entry["examples"]:
                            entry["examples"].append(line)
    return agg


def _snippet(text: str, start: int, end: int) -> str:
    """Короткий фрагмент вокруг ссылки для примера."""
    lo = max(0, start - AI_WINDOW)
    hi = min(len(text), end + AI_WINDOW)
    return " ".join(text[lo:hi].split())


def _window(text: str, start: int, end: int) -> str:
    """Окрестность ссылки для проверки ИИ-термина."""
    lo = max(0, start - AI_WINDOW)
    hi = min(len(text), end + AI_WINDOW)
    return text[lo:hi]


def export_external(
    conn: sqlite3.Connection,
    out: str | Path = DEFAULT_OUT,
    min_mentions: int = 1,
    limit: int = 3000,
    dry: bool = False,
) -> dict[str, Any]:
    """Выгрузить внешних кандидатов в JSONL (или только показать сводку).

    Возвращает сводку: kind_counts, ai_hint_count, written, out, top.
    В режиме ``dry`` файл не пишется.
    """
    agg = collect_external(conn)
    rows = [
        _finalize(entry)
        for entry in agg.values()
        if int(entry["mentions"]) >= int(min_mentions)
    ]
    rows.sort(key=lambda r: (-int(r["mentions"]), -int(r["videos"]),
                             str(r["kind"]), str(r["handle"])))
    if limit is not None and int(limit) >= 0:
        rows = rows[: int(limit)]

    kind_counts: dict[str, int] = {"telegram": 0, "x": 0}
    for r in rows:
        kind_counts[r["kind"]] = kind_counts.get(r["kind"], 0) + 1
    ai_hint_count = sum(1 for r in rows if r["ai_hint"] == 1)
    top = [
        {"kind": r["kind"], "handle": r["handle"], "mentions": r["mentions"],
         "videos": r["videos"]}
        for r in rows[:10]
    ]

    written = 0
    out_path = str(out)
    if not dry:
        exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            for r in rows:
                record = dict(r)
                record["exported_at"] = exported_at
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
    else:
        out_path = None

    return {
        "kind_counts": kind_counts,
        "ai_hint_count": ai_hint_count,
        "written": written,
        "out": out_path,
        "top": top,
        "rows_total": len(rows),
        "dry_run": bool(dry),
    }


def _finalize(entry: dict) -> dict:
    """Привести агрегат к формату строки файла (без exported_at)."""
    return {
        "kind": entry["kind"],
        "handle": entry["handle"],
        "mentions": int(entry["mentions"]),
        "videos": len(entry["videos"]),
        # ТЗ-21/A: список id видео, из которых пришёл кандидат (для kind=x/telegram
        # это описания YouTube-видео). `videos` остаётся ЧИСЛОМ.
        "video_ids": sorted(str(v) for v in entry["videos"]),
        "first_seen": _iso(entry["first_seen"]),
        "last_seen": _iso(entry["last_seen"]),
        "ai_hint": int(entry["ai_hint"]),
        "source": EXTERNAL_SOURCE,
        "examples": list(entry["examples"]),
    }


def format_export_summary(summary: dict[str, Any]) -> str:
    """Итог экспорта человеческой строкой."""
    kc = summary.get("kind_counts", {})
    top = summary.get("top", [])
    top_line = ", ".join(
        f"{t['kind']}:{t['handle']}={t['mentions']}" for t in top[:10]
    ) or "нет"
    if summary.get("dry_run"):
        return (
            f"[dry-run] Внешние кандидаты: telegram {kc.get('telegram', 0)}, "
            f"x {kc.get('x', 0)}, ai_hint {summary.get('ai_hint_count', 0)}; "
            f"топ-10: {top_line}."
        )
    return (
        f"Внешние кандидаты: записано {summary.get('written', 0)} в "
        f"{summary.get('out')}; telegram {kc.get('telegram', 0)}, "
        f"x {kc.get('x', 0)}, ai_hint {summary.get('ai_hint_count', 0)}; "
        f"топ-10: {top_line}."
    )


# --- импорт YouTube-каналов из фида ----------------------------------------

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _id_str(value: Any) -> str | None:
    """Первое строковое значение поля-источника id (терпимо к чужому типу).

    ТЗ-21/B: поле может прийти списком (старый фид), объектом (`{"url": ...}`)
    или одиночной строкой. Негодный тип — не повод падать: вернём ``None``,
    а строка посчитается в ``bad_fields``.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        for item in value:
            got = _id_str(item)
            if got:
                return got
        return None
    if isinstance(value, dict):
        for key in ("id", "video_id", "url", "link", "video_url", "text"):
            got = _id_str(value.get(key))
            if got:
                return got
        return None
    return None


def _video_id_from_entry(entry: dict) -> str | None:
    """Достать video_id из строки фида (поле или ссылка). Не бросает исключений.

    Порядок поиска: ``video_id`` → ``id`` → ``url`` → ``link`` →
    ``video_url`` → ``handle``. Поле ``handle`` — то, куда кладёт ссылку наш
    же экспорт внешних кандидатов, поэтому оно принимается наравне с url-полями
    (URL или голый 11-символьный id). ТЗ-21/B: значения терпимо принимаются и
    списком/объектом (берётся первое годное).
    """
    for key in ("video_id", "id"):
        val = _id_str(entry.get(key))
        if val is not None and _YOUTUBE_ID_RE.match(val.strip()):
            return val.strip()
    # TODO(debt-D-12): handle принимается как ссылка; закрыт 15.09.2026
    # (docs/TECH-DEBT.md).
    for key in ("url", "link", "video_url", "handle"):
        val = _id_str(entry.get(key))
        if val is None:
            continue
        val = val.strip()
        if _YOUTUBE_ID_RE.match(val):
            return val
        vid = _video_id_from_url(val)
        if vid:
            return vid
    return None


# Поля, которые потребитель обязан принять, не падая на чужом типе (ТЗ-21/B).
_ID_FIELDS = ("video_id", "id", "url", "link", "video_url", "handle")
_LISTY_FIELDS = ("videos", "video_ids", "sources", "examples")


def _bad_field_count(entry: dict) -> int:
    """Сколько полей строки имеют негодный тип (импорт из-за них не встаёт)."""
    bad = 0
    for key in _ID_FIELDS:
        if key in entry and entry[key] is not None and _id_str(entry[key]) is None:
            bad += 1
    for key in _LISTY_FIELDS + ("mentions",):
        if key not in entry or entry[key] is None:
            continue
        val = entry[key]
        if key == "mentions":
            try:
                int(val)
            except (TypeError, ValueError):
                bad += 1
        elif key in ("videos",):
            if not isinstance(val, (int, float, list, tuple, str)) or isinstance(val, bool):
                bad += 1
        elif not isinstance(val, (str, list, tuple)):
            bad += 1
    return bad


def _video_id_from_url(url: str) -> str | None:
    """video_id из youtube.com/watch?v=... или youtu.be/<id>."""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    return None


def read_youtube_feed(path: str | Path) -> list[str]:
    """Прочитать фид и вернуть уникальные video_id из строк kind='youtube'.

    Тонкая обёртка над :func:`_parse_feed`: та же проверка, но наружу отдаётся
    только список id. Битый/отсутствующий файл, пустой файл или файл, где все
    строки битые → CandidatesError с понятным текстом (без трейсбека).
    """
    return _parse_feed(path)["youtube_ids"]


def _parse_feed(path: str | Path) -> dict[str, Any]:
    """Разобрать фид-мост: id, счётчики битых строк и чужих kind.

    Толерантность к битым строкам обязательна: файл пишет ДРУГОЙ проект, и
    оборванная запись (упавший писатель) не должна навсегда блокировать приём.
    Битая строка пропускается и считается (``bad_lines``); строки чужого kind
    игнорируются, но считаются (``skipped_kinds``). Импорт отменяется понятной
    ошибкой только когда файл не найден/нечитаем, пуст или ВСЕ строки битые.

    Возвращает ``youtube_ids`` (уникальные, по порядку), ``bad_lines``,
    ``skipped_kinds``, ``total_lines`` (непустые строки).
    """
    src = Path(path)
    if not src.exists():
        raise CandidatesError(f"файл фида не найден: {src}")
    ids: list[str] = []
    seen: set[str] = set()
    bad_lines = 0
    bad_fields = 0
    skipped_kinds = 0
    total_lines = 0
    try:
        with src.open("r", encoding="utf-8") as fh:
            for _lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                total_lines += 1
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    bad_lines += 1
                    continue
                if not isinstance(entry, dict):
                    bad_lines += 1
                    continue
                if str(entry.get("kind", "")).lower() != "youtube":
                    skipped_kinds += 1
                    continue
                # ТЗ-21/B: чужой тип поля не роняет импорт, а считается.
                bad_fields += _bad_field_count(entry)
                vid = _video_id_from_entry(entry)
                if not vid:
                    # kind=youtube, но id не извлечён — строка негодная.
                    bad_lines += 1
                    continue
                if vid in seen:
                    continue
                seen.add(vid)
                ids.append(vid)
    except OSError as exc:
        raise CandidatesError(f"не удалось прочитать фид {src}: {exc}") from exc
    # TODO(debt-D-13): пустой файл и «все строки битые» — единственные поводы
    # отменить импорт целиком; закрыт 15.09.2026 (docs/TECH-DEBT.md).
    if total_lines == 0:
        raise CandidatesError(f"фид пуст: {src}")
    if bad_lines == total_lines:
        raise CandidatesError(
            f"фид {src}: все строки битые ({bad_lines} из {total_lines})"
        )
    return {
        "youtube_ids": ids,
        "bad_lines": bad_lines,
        "bad_fields": bad_fields,
        "skipped_kinds": skipped_kinds,
        "total_lines": total_lines,
    }


def import_youtube_feed(
    conn: sqlite3.Connection,
    feed_path: str | Path,
    client: Any,
    limit: int = 50,
    dry: bool = False,
) -> dict[str, Any]:
    """Разрешить каналы видео из фида и записать их в channel_candidates.

    Один сетевой вызов (videos.list, 1 unit) на video_id, не больше ``limit``
    за прогон. Останавливается при срабатывании общего предохранителя квоты.
    Работает толерантно к битым строкам: они пропускаются и считаются
    (``bad_lines``); строки чужого ``kind`` игнорируются со счётчиком
    (``skipped_kinds``). ТЗ-21/B: поле чужого типа (например `video_id` списком
    или `videos` числом/списком) не роняет импорт — берётся годное значение,
    а поле считается в ``bad_fields``. Импорт отменяется понятной ошибкой только
    если файл не найден/нечитаем, пуст или ВСЕ строки битые.
    """
    parsed = _parse_feed(feed_path)
    ids = parsed["youtube_ids"]
    summary: dict[str, Any] = {
        "feed": str(feed_path),
        "dry_run": bool(dry),
        "limit": int(limit),
        "youtube_ids": len(ids),
        "bad_lines": parsed["bad_lines"],
        "bad_fields": parsed["bad_fields"],
        "skipped_kinds": parsed["skipped_kinds"],
        "resolved": 0,
        "new": 0,
        "known": 0,
        "unresolved": 0,
        "calls": 0,
        "units": 0,
        "stopped_reason": None,
        "errors": [],
    }
    if dry:
        return summary

    limit = max(0, int(limit))
    units_before = _quota_total(conn)
    for vid in ids:
        if summary["calls"] >= limit:
            summary["stopped_reason"] = f"достигнут лимит вызовов ({limit})"
            break
        try:
            items = client.videos_by_ids([vid], parts="snippet")
        except yt.YouTubeError as exc:  # предохранитель квоты и сбои API
            summary["stopped_reason"] = f"остановка по квоте: {exc}"
            summary["errors"].append(f"{vid}: {exc}")
            break
        summary["calls"] += 1
        item = items[0] if items else None
        cid = None
        if item:
            cid = (item.get("snippet") or {}).get("channelId")
        if not cid:
            summary["unresolved"] += 1
            continue
        summary["resolved"] += 1
        known_before = _candidate_exists(conn, cid)
        db.upsert_channel_candidate(conn, {
            "channel_id": cid,
            "title": (item.get("snippet") or {}).get("title"),
            "description": (item.get("snippet") or {}).get("description"),
            "source": FEED_SOURCE,
            "evidence": f"tg:{vid}",
            "mentions": 1,
            "status": "new",
        })
        if known_before:
            summary["known"] += 1
        else:
            summary["new"] += 1
    summary["units"] = _quota_total(conn) - units_before
    return summary


def _candidate_exists(conn: sqlite3.Connection, channel_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM channel_candidates WHERE channel_id=?", (channel_id,)
    ).fetchone()
    return row is not None


def _quota_total(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(units),0) AS u FROM quota_log"
        ).fetchone()
        # Позиционное чтение: работает и без row_factory (tuple).
        # TODO(debt-D-11): закрыт 15.09.2026 (docs/TECH-DEBT.md).
        return int(row[0]) if row else 0
    except Exception:
        return 0


def format_import_summary(summary: dict[str, Any]) -> str:
    """Итог импорта человеческой строкой."""
    if summary.get("dry_run"):
        return (
            f"[dry-run] Фид {summary.get('feed')}: YouTube-видео "
            f"{summary.get('youtube_ids', 0)}."
        )
    text = (
        f"Импорт из фида: видео {summary.get('youtube_ids', 0)}, "
        f"разрешено {summary.get('resolved', 0)} "
        f"(новых {summary.get('new', 0)}, известных {summary.get('known', 0)}), "
        f"не разрешено {summary.get('unresolved', 0)}, "
        f"вызовов {summary.get('calls', 0)}, квоты {summary.get('units', 0)} units."
    )
    if summary.get("bad_lines"):
        text += f" Битых строк пропущено: {summary['bad_lines']}."
    if summary.get("skipped_kinds"):
        text += f" Строк чужого kind: {summary['skipped_kinds']}."
    if summary.get("stopped_reason"):
        text += f" Остановка: {summary['stopped_reason']}."
    if summary.get("errors"):
        text += f" Ошибок: {len(summary['errors'])}."
    return text


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Открыть БД только для чтения (URI mode=ro), без записи в WAL."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
