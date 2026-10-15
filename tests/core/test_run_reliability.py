"""Часть 1 «волна 2»: журнал прогонов не оставляет открытых строк.

Проверяется:
  * повторное закрытие идемпотентно и не портит уже записанные данные;
  * тело прогона, падающее исключением, закрывает ``finished_at`` (note
    ``error:<Тип>``);
  * дедлайн закрывает прогон с ``note='deadline'``;
  * разовая реконсиляция ``abandon_open_runs`` идемпотентна и не трогает
    свежие строки;
  * ``atexit``-реестр подхватывает прогоны, созданные через ``add_run``.
"""

from __future__ import annotations

import time

import pytest

from tuber.core import db, schema, storage, timeutil


def _init(path):
    conn = db.connect(path)
    schema.init_schema(conn)
    return conn


def test_close_run_idempotent_keeps_first_data(tmp_path):
    conn = _init(str(tmp_path / "r.db"))
    try:
        with db.write_tx(conn):
            run_id = storage.add_run(conn, "x", started_at="2026-10-03 10:00:00",
                                     mode="collect")
        assert storage.close_run(conn, run_id, note="первое", ok=7) == 1
        # Второй закрывающий вызов ничего не меняет.
        assert storage.close_run(conn, run_id, note="второе", ok=999) == 0
        row = conn.execute(
            "SELECT finished_at, note, ok_count FROM run WHERE id=?",
            (run_id,)).fetchone()
        assert row["finished_at"] is not None
        assert row["note"] == "первое"
        assert row["ok_count"] == 7
    finally:
        conn.close()


def test_run_guard_closes_on_exception(tmp_path):
    conn = _init(str(tmp_path / "g.db"))
    try:
        with db.write_tx(conn):
            run_id = storage.add_run(conn, "x", started_at="2026-10-03 10:00:00",
                                     mode="discover:all:120")
        with pytest.raises(RuntimeError):
            with storage.run_guard(conn, run_id):
                raise RuntimeError("сеть оборвалась")
        row = conn.execute(
            "SELECT finished_at, note FROM run WHERE id=?", (run_id,)).fetchone()
        assert row["finished_at"] is not None
        assert row["note"] == "error:RuntimeError"
    finally:
        conn.close()


def test_run_guard_deadline_note(tmp_path):
    conn = _init(str(tmp_path / "d.db"))
    try:
        with db.write_tx(conn):
            run_id = storage.add_run(conn, "x", started_at="2026-10-03 10:00:00",
                                     mode="discover:all:120")
        with storage.run_guard(conn, run_id, deadline_seconds=0.01):
            time.sleep(0.05)
        row = conn.execute(
            "SELECT finished_at, note FROM run WHERE id=?", (run_id,)).fetchone()
        assert row["finished_at"] is not None
        assert row["note"] == "deadline"
    finally:
        conn.close()


def test_run_guard_does_not_clobber_body_finish(tmp_path):
    conn = _init(str(tmp_path / "n.db"))
    try:
        with db.write_tx(conn):
            run_id = storage.add_run(conn, "x", started_at="2026-10-03 10:00:00",
                                     mode="collect")
        with storage.run_guard(conn, run_id, note="страховка"):
            # Тело команды само закрывает прогон со своими числами.
            storage.close_run(conn, run_id, note="тело", ok=42)
        row = conn.execute(
            "SELECT note, ok_count FROM run WHERE id=?", (run_id,)).fetchone()
        assert row["note"] == "тело"
        assert row["ok_count"] == 42
    finally:
        conn.close()


def test_abandon_open_runs_idempotent(tmp_path):
    conn = _init(str(tmp_path / "a.db"))
    try:
        old = storage.add_run(conn, "x", started_at="2026-09-15 00:00:00",
                              mode="discover:all:120")
        fresh = storage.add_run(conn, "x", started_at=timeutil.iso_now(), mode="collect")
        first = storage.abandon_open_runs(conn, older_hours=1)
        assert first["closed"] == 1
        assert first["remaining_stale"] == 0
        row = conn.execute(
            "SELECT finished_at, note FROM run WHERE id=?", (old,)).fetchone()
        assert row["finished_at"] == "2026-09-15 00:00:00"
        assert row["note"] == "abandoned: ревизия 03.10.2026"
        # Свежий прогон не тронут.
        fresh_row = conn.execute(
            "SELECT finished_at FROM run WHERE id=?", (fresh,)).fetchone()
        assert fresh_row["finished_at"] is None
        # Повторный прогон — 0 изменений.
        second = storage.abandon_open_runs(conn, older_hours=1)
        assert second["closed"] == 0
    finally:
        conn.close()


def test_add_run_registered_for_atexit_safety_net(tmp_path):
    conn = _init(str(tmp_path / "at.db"))
    try:
        with db.write_tx(conn):
            run_id = storage.add_run(conn, "cross", started_at="2026-10-03 10:00:00",
                                     mode="queue-review")
        # Без явного закрытия аварийная страховка процесса закрывает строку.
        assert storage.close_open_runs_now("error: unhandled") >= 1
        row = conn.execute(
            "SELECT finished_at, note FROM run WHERE id=?", (run_id,)).fetchone()
        assert row["finished_at"] is not None
        assert row["note"] == "error: unhandled"
    finally:
        conn.close()
