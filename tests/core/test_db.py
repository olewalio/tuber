"""Тесты слоя соединения и транзакций (ТЗ-1 §4)."""

from __future__ import annotations

import sqlite3

import pytest

from tuber.core import db


def _make(path):
    conn = db.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    return conn


def test_pragmas_applied(tmp_path):
    conn = _make(str(tmp_path / "p.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    finally:
        conn.close()


def test_readonly_connection_rejects_writes(tmp_path):
    path = str(tmp_path / "ro.db")
    writer = _make(path)
    writer.execute("INSERT INTO t VALUES (1)")
    writer.close()

    ro = db.connect(path, readonly=True)
    try:
        assert db.is_readonly(ro)
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t VALUES (2)")
    finally:
        ro.close()


def test_write_tx_commits(tmp_path):
    conn = _make(str(tmp_path / "c.db"))
    try:
        with db.write_tx(conn):
            conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_write_tx_rolls_back_on_exception(tmp_path):
    conn = _make(str(tmp_path / "r.db"))
    try:
        with pytest.raises(ValueError):
            with db.write_tx(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                raise ValueError("boom")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_write_tx_retries_when_locked(tmp_path):
    path = str(tmp_path / "lock.db")
    a = _make(path)
    b = db.connect(path)
    try:
        # Короткий busy_timeout, чтобы тест не ждал 15 c на каждой попытке.
        a.execute("PRAGMA busy_timeout=200")
        b.execute("PRAGMA busy_timeout=100")
        # Первая транзакция держит блокировку записи.
        a.execute("BEGIN IMMEDIATE")
        a.execute("INSERT INTO t VALUES (1)")

        slept = {"n": 0}

        def sleeper(_delay):
            # Отпускаем блокировку из "другого процесса" на первой паузе.
            slept["n"] += 1
            a.execute("COMMIT")

        with db.write_tx(b, retries=5, sleep=sleeper):
            b.execute("INSERT INTO t VALUES (2)")

        assert slept["n"] >= 1
        assert b.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    finally:
        a.close()
        b.close()


def test_write_tx_gives_up_after_retries(tmp_path):
    path = str(tmp_path / "lock2.db")
    a = _make(path)
    b = db.connect(path)
    try:
        a.execute("PRAGMA busy_timeout=200")
        b.execute("PRAGMA busy_timeout=100")
        a.execute("BEGIN IMMEDIATE")
        a.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(sqlite3.OperationalError):
            with db.write_tx(b, retries=3, sleep=lambda _d: None):
                b.execute("INSERT INTO t VALUES (2)")
    finally:
        a.execute("ROLLBACK")
        a.close()
        b.close()
