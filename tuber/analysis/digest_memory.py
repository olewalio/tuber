"""Память выдачи — материалы не повторяются изо дня в день (ТЗ-Tuber).

Жалоба владельца: «Почему однотипные посты опять выходят» — изо дня в день в
сводке те же материалы. Замер 20.09.2026: 18.09∩19.09∩20.09 = 3 одинаковых
материала; 18∩19 — 4 из 12; 19∩20 — 6 из 13.

Модуль хранит факт показа материала в сводке владельцу в таблице
``digest_sent`` (идемпотентная миграция ``CREATE TABLE IF NOT EXISTS``):

    content_id INTEGER NOT NULL,
    platform   TEXT,
    section    TEXT,
    sent_at    TEXT NOT NULL,
    PRIMARY KEY(content_id, section, sent_at)

Уникальность по ``(content_id, section, sent_at)`` делает повторный прогон за
ту же дату идемпотентным (ТЗ §2.3): ``INSERT OR IGNORE`` не плодит дубли.

Запись выдачи делает только CLI-режим с ``--save`` (см.
``analysis/report.py::main``); сухой прогон и тесты НЕ пишут. Фильтр в сводке
выключается переменной окружения ``TUBER_DIGEST_MEMORY=0`` (откат без правки
кода), глубина окна — ``DIGEST_MEMORY_DAYS`` (по умолчанию 3, переопределяется
``TUBER_DIGEST_MEMORY_DAYS``).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

#: Сколько последних суток считать «памятью выдачи»: позиции, показанные за это
#: окно, из разделов 1–3 и 5 исключаются (ТЗ §2.2).
DIGEST_MEMORY_DAYS = 3

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS digest_sent (
      content_id INTEGER NOT NULL,
      platform TEXT,
      section TEXT,
      sent_at TEXT NOT NULL,
      PRIMARY KEY (content_id, section, sent_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_digest_sent_at ON digest_sent(sent_at)",
)


def enabled() -> bool:
    """Включена ли память выдачи (ТЗ §2.5). ``TUBER_DIGEST_MEMORY=0`` — выкл."""
    raw = (os.environ.get("TUBER_DIGEST_MEMORY") or "").strip().lower()
    return raw not in ("0", "false", "no", "off", "нет")


def memory_days(default: int = DIGEST_MEMORY_DAYS) -> int:
    """Глубина окна памяти в сутках. Опечатка/0 → значение по умолчанию."""
    raw = os.environ.get("TUBER_DIGEST_MEMORY_DAYS")
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return int(default)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Идемпотентная миграция ``digest_sent`` (ТЗ §2.1)."""
    for stmt in _DDL:
        conn.execute(stmt)


def recent_ids(conn: sqlite3.Connection, days: int | None = None, *,
               now: datetime | None = None) -> set[int]:
    """ids материалов, показывавшихся за последние ``days`` суток (ТЗ §2.2).

    Таблицы может не быть (старый снимок базы, ещё не мигрировавший) — тогда
    возвращается пустое множество: память честно считается пустой, а не падает.
    """
    days = memory_days() if days is None else int(days)
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = conn.execute(
            "SELECT content_id FROM digest_sent WHERE sent_at >= ?", (since,))
    except sqlite3.OperationalError:
        return set()
    return {int(r[0]) for r in rows if r[0] is not None}


def mark_sent(conn: sqlite3.Connection, rows, sent_at: str | None = None) -> int:
    """Записать показанные позиции. ``rows`` — ``(content_id, platform, section)``.

    Идемпотентно по ``(content_id, section, sent_at)``: повторный прогон за ту же
    дату не дублирует строки. Возвращает число реально вставленных строк.
    """
    sent_at = sent_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ensure_schema(conn)
    added = 0
    for content_id, platform, section in rows:
        if content_id is None:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO digest_sent(content_id, platform, section,"
            " sent_at) VALUES (?,?,?,?)",
            (int(content_id), platform, section, sent_at))
        added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return added
