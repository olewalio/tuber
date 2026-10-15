"""Чтение legacy-баз в режиме read-only.

Жёсткое правило волны ТЗ-1: боевые базы ``tuber-os`` / ``tuber-x`` /
``tuber-telegram`` только ЧИТАЮТСЯ. Все соединения здесь открываются через
``file:<path>?mode=ro`` (см. :func:`tuber.core.db.connect`), поэтому любой
случайный INSERT/UPDATE/DELETE получит ошибку драйвера, а не тихо испортит
прод.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

from tuber.core import db


def open_legacy(path: str) -> sqlite3.Connection:
    """Открыть legacy-базу только для чтения."""
    return db.connect(path, readonly=True)


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def rows(
    conn: sqlite3.Connection, table: str, *, order_by: str | None = None
) -> Iterator[sqlite3.Row]:
    """Итератор строк таблицы (детерминированный порядок при ``order_by``)."""
    sql = f"SELECT * FROM {table}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    yield from conn.execute(sql)


def scalar(conn: sqlite3.Connection, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None
