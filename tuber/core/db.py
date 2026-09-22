"""Слой соединения с базой.

Требования §4 ТЗ-1:

* ``connect(path, readonly=False)`` — открыть БД и выставить PRAGMA из §3;
* для legacy — ТОЛЬКО read-only (``file:<path>?mode=ro`` + ``-readonly``),
  чтобы боевые базы физически не могли быть изменены;
* ``write_tx(conn)`` — контекстный менеджер ``BEGIN IMMEDIATE`` с повтором при
  ``database is locked`` (до 5 попыток, пауза 0.5→4 c). В единой базе пишут три
  платформы из разных процессов, WAL допускает одного писателя за раз, поэтому
  повтор при блокировке — обязательное требование, а не украшение.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Callable, Iterator

from tuber import config

# Максимальное число попыток захвата блокировки записи.
WRITE_RETRIES = 5
WRITE_BASE_DELAY = 0.5


def connect(path: str, readonly: bool = False, *, create: bool = True,
            factory: type[sqlite3.Connection] | None = None) -> sqlite3.Connection:
    """Открыть соединение с базой и применить PRAGMA.

    ``readonly=True`` открывает файл по URI ``file:<path>?mode=ro`` — это
    жёсткая гарантия, что миграция не изменит legacy-базу. Для readonly PRAGMA
    записи (``journal_mode``) не применяются.

    ``factory`` — необязательный подкласс соединения (адаптеры платформ
    передают сюда соединение с трансляцией legacy-имён, ТЗ-3c).
    """
    factory = factory or sqlite3.Connection
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                               timeout=config.BUSY_TIMEOUT_MS / 1000, factory=factory)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=%d" % config.BUSY_TIMEOUT_MS)
        conn.execute("PRAGMA foreign_keys=ON")
        # Режим доступа только для чтения на уровне драйвера.
        try:
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.DatabaseError:
            pass
        return conn

    conn = sqlite3.connect(path, isolation_level=None,
                           timeout=config.BUSY_TIMEOUT_MS / 1000, factory=factory)
    conn.row_factory = sqlite3.Row
    apply_pragmas(conn)
    return conn


def apply_pragmas(conn: sqlite3.Connection) -> None:
    """PRAGMA из §3 для записываемой базы."""
    try:
        conn.execute(f"PRAGMA journal_mode={config.JOURNAL_MODE}")
    except sqlite3.DatabaseError:
        # :memory: не поддерживает WAL — для тестов это нормально.
        pass
    conn.execute(f"PRAGMA synchronous={config.SYNCHRONOUS}")
    conn.execute("PRAGMA busy_timeout=%d" % config.BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA foreign_keys=ON")


@contextmanager
def write_tx(
    conn: sqlite3.Connection,
    *,
    retries: int = WRITE_RETRIES,
    base_delay: float = WRITE_BASE_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[sqlite3.Connection]:
    """Транзакция записи ``BEGIN IMMEDIATE`` с повтором при блокировке.

    Пауза растёт 0.5 → 1 → 2 → 4 c (до ``retries`` попыток). При любом
    исключении внутри блока — откат и повторное возбуждение исключения.
    """
    attempt = 0
    delay = base_delay
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            attempt += 1
            if attempt >= retries:
                raise
            sleep(delay)
            delay *= 2

    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


def is_readonly(conn: sqlite3.Connection) -> bool:
    """Проверка, что соединение действительно только для чтения."""
    try:
        row = conn.execute("PRAGMA query_only").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])
