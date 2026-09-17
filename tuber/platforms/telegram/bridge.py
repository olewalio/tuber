#!/usr/bin/env python3
"""Общий модуль моста источников Tuber-Telegram (ТЗ-17).

Один источник правды для трёх скриптов:
  * ``feeds_import``  (было: scripts/import_candidates.py)   — импорт кандидатов из фида tuber-os;
  * ``discover``      (было: scripts/discover_from_posts.py) — дискавери t.me-хендлов из своих постов;
  * ``feeds_export``  (было: scripts/export_candidates.py)   — экспорт X/YouTube-кандидатов.

Здесь: загрузка конфига фильтра, нормализация хендлов, извлечение t.me / x.com /
youtube-идентификаторов из текста и ссылок, фильтр и дедуплицирующий импорт.

Только stdlib. Сеть не используется. Модуль не трогает рабочую БД, если его
функции вызывают с явно переданным соединением.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from . import config as _config
from . import store as db

ROOT = _config.ROOT
DEFAULT_DB = _config.DEFAULT_DB
DEFAULT_CONFIG = _config.BRIDGE_CONFIG
DEFAULT_FEED = "/root/tuber-os/data/exchange/external_candidates.jsonl"

MAX_EXAMPLE_LEN = 80
MAX_EXAMPLES = 2


# ---------------------------------------------------------------------------
# Конфиг фильтра
# ---------------------------------------------------------------------------
def load_config(path: str | None = None) -> dict:
    """Читает JSON-конфиг фильтра; понятная ошибка при проблеме."""
    path = path or DEFAULT_CONFIG
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"конфиг фильтра не найден: {path}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"конфиг фильтра битый (не JSON): {path}: {exc}")
    if not isinstance(cfg, dict):
        raise ConfigError(f"конфиг фильтра должен быть JSON-объектом: {path}")
    try:
        re.compile(cfg["handle_regex"])
    except (KeyError, re.error) as exc:
        raise ConfigError(f"конфиг фильтра: плохой handle_regex: {exc}")
    return cfg


class ConfigError(RuntimeError):
    """Ошибка конфигурации/входа — печатается владельцу без трейсбека."""


# ---------------------------------------------------------------------------
# Нормализация и извлечение
# ---------------------------------------------------------------------------
def normalize_handle(raw: str | None) -> str:
    """Нижний регистр, без @, без пробелов и обрамляющих слэшей."""
    if not raw:
        return ""
    h = raw.strip().lstrip("@").strip().strip("/")
    return h.lower()


_TG_HOST_RE = re.compile(
    r"(?:^|//|\.)(?:t\.me|telegram\.me|telegram\.dog|telegram\.org)/",
    re.IGNORECASE,
)
_TG_PATH_RE = re.compile(
    r"(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_TG_BARE_RE = re.compile(r"(?:^|[\s(«\"'])@([A-Za-z0-9_]{3,32})")


def _looks_like_tg_path(seg: str) -> bool:
    # t.me/<seg> — сегмент без '?', '#' и не '+' (инвайт-хэш)
    return "/" not in seg


def extract_telegram_handles(text: str | None, links: str | None = None) -> set[str]:
    """Все t.me/telegram.me-хендлы в тексте и в списке ссылок (JSON или строка)."""
    out: set[str] = set()
    sources = []
    if text:
        sources.append(text)
    if links:
        sources.append(links_to_text(links))

    for src in sources:
        for m in _TG_PATH_RE.finditer(src):
            seg = m.group(1)
            # ссылка t.me/<handle>/<msgid> — хендл это первый сегмент
            out.add(normalize_handle(seg))
    # голые @упоминания в тексте
    for m in _TG_BARE_RE.finditer(text or ""):
        out.add(normalize_handle(m.group(1)))
    out.discard("")
    return out


_X_PATH_RE = re.compile(
    r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?:[/?#\"'<\s)]|$)",
    re.IGNORECASE,
)
X_RESERVED = {
    "i", "intent", "home", "search", "hashtag", "share", "explore",
    "notifications", "messages", "settings", "compose", "login", "signup",
    "tos", "privacy", "about", "xx", "widgets", "embed", "status",
}
_YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


def extract_x_handles(text: str | None, links: str | None = None) -> set[str]:
    """Аккаунты X (x.com/twitter.com) из текста и ссылок."""
    out: set[str] = set()
    blob = " ".join(x for x in (text or "", links_to_text(links or "")) if x)
    for m in _X_PATH_RE.finditer(blob):
        h = m.group(1).lower()
        if h not in X_RESERVED:
            out.add(h)
    return out


def extract_youtube_ids(text: str | None, links: str | None = None) -> set[str]:
    """video_id YouTube (watch/embed/shorts/live, youtu.be) из текста и ссылок."""
    out: set[str] = set()
    blob = " ".join(x for x in (text or "", links_to_text(links or "")) if x)
    for m in _YT_RE.finditer(blob):
        out.add(m.group(1))
    return out


def links_to_text(links) -> str:
    """links может быть JSON-массивом строк или готовой строкой — привести к строке."""
    if not links:
        return ""
    if isinstance(links, (list, tuple)):
        return " ".join(str(x) for x in links)
    s = str(links)
    if s[:1] == "[":
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return " ".join(str(x) for x in arr)
        except (json.JSONDecodeError, TypeError):
            pass
    return s


def truncate(s: str, n: int = MAX_EXAMPLE_LEN) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def build_notes(mentions: int, ai_hint, examples) -> str:
    """Краткая выжимка для channels.notes: mentions, ai_hint, до 2 примеров ≤80 знаков."""
    parts = [f"mentions={mentions}", f"ai_hint={1 if ai_hint else 0}"]
    ex = [truncate(e, MAX_EXAMPLE_LEN) for e in (examples or []) if str(e).strip()]
    if ex:
        parts.append("ex: " + " | ".join(f"«{e}»" for e in ex[:MAX_EXAMPLES]))
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Фильтр
# ---------------------------------------------------------------------------
def filter_reason(handle: str, mentions: int, ai_hint, min_mentions: int, cfg: dict):
    """Вернуть причину отбраковки или None, если кандидат проходит.

    Порядок важен: служебное → бот → гигант → паттерн → порог. Гиганты проверяются
    раньше паттерна, иначе короткие хендлы (cnn/bbc/rt) считались бы «паттерном» и
    статистика «сколько гигантов отсеяно» была бы неверной.
    """
    h = normalize_handle(handle)
    if h in {p.lower() for p in cfg.get("reserved_paths", [])}:
        return "reserved"
    bot_suffix = (cfg.get("bot_suffix") or "bot").lower()
    if bot_suffix and h.endswith(bot_suffix) and not ai_hint:
        return "bot"
    if h in {g.lower() for g in cfg.get("news_giants", [])}:
        return "news_giant"
    if not re.match(cfg["handle_regex"], h):
        return "pattern"
    if mentions < min_mentions and not ai_hint:
        return "low_mentions"
    return None


# ---------------------------------------------------------------------------
# Дедуплицирующий импорт
# ---------------------------------------------------------------------------
def existing_handles(con) -> set[str]:
    """Все известные хендлы во ВСЕХ статусах: реестр (``channels``) + ``candidate``.

    Дедупликация обязана видеть обе половины единой базы: канал, попавший в
    реестр, не должен импортироваться повторно как кандидат, и наоборот.
    """
    return db.known_handles(con)


def import_candidates(con, candidates: dict, *, source: str, dry: bool,
                      limit: int, min_mentions: int, cfg: dict) -> dict:
    """Импорт кандидатов с дедупликацией по всем статусам.

    candidates: {handle: {"mentions": int, "ai_hint": bool, "examples": [str]}}.
    Существующие записи (active/private/dead/candidate) НЕ трогаются — только
    пропускаются со счётчиком. Порядок: фильтр → дедуп → лимит.
    """
    known = existing_handles(con)
    stats = {"imported": 0, "skipped_existing": 0, "skipped_filter": 0,
             "skipped_limit": 0, "filter_reasons": {}}

    ordered = sorted(
        candidates.items(),
        key=lambda kv: (-int(kv[1].get("mentions") or 0), normalize_handle(kv[0])),
    )

    for raw_handle, info in ordered:
        h = normalize_handle(raw_handle)
        if not h:
            stats["skipped_filter"] += 1
            stats["filter_reasons"]["empty"] = stats["filter_reasons"].get("empty", 0) + 1
            continue
        mentions = int(info.get("mentions") or 0)
        ai_hint = info.get("ai_hint")

        reason = filter_reason(h, mentions, ai_hint, min_mentions, cfg)
        if reason is not None:
            stats["skipped_filter"] += 1
            stats["filter_reasons"][reason] = stats["filter_reasons"].get(reason, 0) + 1
            continue
        if h in known:
            stats["skipped_existing"] += 1
            continue
        if stats["imported"] >= limit:
            stats["skipped_limit"] += 1
            continue

        notes = build_notes(mentions, ai_hint, info.get("examples"))
        if not dry:
            # Канонический обмен единой базы — таблица ``candidate`` (ТЗ-4 §0):
            # JSONL-фид остаётся рабочим представлением, но строки живут здесь.
            db.import_candidate(con, h, found_via=source, notes=notes,
                                meta={"mentions": mentions, "ai_hint": bool(ai_hint)})
            con.commit()
        known.add(h)
        stats["imported"] += 1
    return stats


# ---------------------------------------------------------------------------
# Чтение фида
# ---------------------------------------------------------------------------
def read_feed(path: str, kind: str = "telegram") -> list[dict]:
    """JSONL-фид tuber-os → список объектов kind=<kind>. Понятные ошибки."""
    if not os.path.exists(path):
        raise ConfigError(f"фид не найден: {path}")
    if not os.path.isfile(path):
        raise ConfigError(f"фид — не файл: {path}")
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ConfigError(f"фид битый: {path}, строка {n}: {exc.msg}")
                if not isinstance(obj, dict):
                    raise ConfigError(f"фид битый: {path}, строка {n}: не JSON-объект")
                if obj.get("kind") == kind:
                    records.append(obj)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"фид битый (не UTF-8): {path}: {exc}")
    return records


def feed_examples(obj: dict) -> list[str]:
    out = []
    for e in obj.get("examples") or []:
        if isinstance(e, dict):
            out.append(e.get("text") or e.get("quote") or json.dumps(e, ensure_ascii=False))
        else:
            out.append(str(e))
    return out


def feed_to_candidates(records, cfg: dict) -> dict:
    """JSONL-записи фида → словарь кандидатов с агрегацией по хендлу."""
    cands: dict[str, dict] = {}
    for obj in records:
        h = normalize_handle(obj.get("handle"))
        if not h:
            continue
        mentions = obj.get("mentions")
        if mentions is None:
            mentions = len(obj.get("examples") or []) or 1
        try:
            mentions = int(mentions)
        except (TypeError, ValueError):
            mentions = 1
        cur = cands.setdefault(h, {"mentions": 0, "ai_hint": False, "examples": []})
        cur["mentions"] += mentions
        if obj.get("ai_hint") in (1, True, "1"):
            cur["ai_hint"] = True
        for e in feed_examples(obj):
            if e and (not cur["examples"] or cur["examples"][-1] != e):
                cur["examples"].append(e)
    return cands


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_ts(value):
    """Привести любую дату из БД к ISO-UTC. В posts.date_utc встречаются и
    '2026-09-12T11:16:20+00:00', и '2026-09-12 11:16:20' — без нормализации
    строковое сравнение first/last_seen даёт неверный порядок."""
    if not value:
        return value
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
