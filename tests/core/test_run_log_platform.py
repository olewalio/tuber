"""D-24 (ТЗ-3b): ядровая ``run_log`` различает платформы явной колонкой.

До правки принадлежность строки платформе выводилась «по допущению»: строки с
``run_id IS NULL`` (их писал плоский код X вне прогона — предупреждения брокера,
потолки, cooldown) считались X-овыми, потому что больше в ``run_log`` никто не
писал. При переносе Telegram это допущение стало бы ложью.

Здесь проверяются: наличие колонки/index, идемпотентный бэкфилл (по ``run_id`` и
по провенансу ``legacy_map`` для строк без связи), правило parity без догадки и
план запроса без ``MATERIALIZE``.
"""
from __future__ import annotations

import sqlite3

from tuber.core import db, schema
from tuber.tools import parity_report


def _fresh(tmp_path) -> sqlite3.Connection:
    conn = db.connect(str(tmp_path / "runlog.db"))
    schema.init_schema(conn)
    conn.commit()
    return conn


def test_run_log_has_platform_column_and_index(tmp_path):
    conn = _fresh(tmp_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA main.table_info(run_log)")}
        assert "platform" in cols
        assert "platform" in schema.required_columns()["run_log"]
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "idx_run_log_platform" in idx
        assert schema.verify_schema(conn) == []
    finally:
        conn.close()


def test_backfill_fills_platform_from_run(tmp_path):
    """Строка внутри прогона получает платформу прогона."""
    conn = _fresh(tmp_path)
    try:
        conn.execute("INSERT INTO run(platform, mode) VALUES ('x', 'collect')")
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO run_log(run_id, ts, level, ref, msg) VALUES (?,?,?,?,?)",
            (run_id, "2026-09-15 00:00:00", "INFO", "h", "m"))
        conn.execute("INSERT INTO run_log(run_id, ts, level, msg) VALUES (NULL,?,?,?)",
                     ("2026-09-15 00:00:01", "WARN", "вне прогона"))
        conn.commit()

        res = schema.backfill_run_log_platform(conn)
        conn.commit()
        assert res["total"] == 2
        assert res["by_run"] == 1              # строка с run_id
        # Строка без run_id и без legacy_map остаётся NULL: платформу не выдумываем
        assert res["remaining"] == 1
        row = conn.execute("SELECT platform FROM run_log WHERE run_id IS NOT NULL").fetchone()
        assert row[0] == "x"
    finally:
        conn.close()


def test_backfill_fills_unlinked_rows_from_legacy_map(tmp_path):
    """Строка БЕЗ связи (``run_id IS NULL``) получает платформу по провенансу.

    Это ровно тот класс строк, который раньше «по допущению» считался X-овым:
    ``legacy_map`` знает, из какой legacy-базы строка приехала, — точнее догадки
    по времени.
    """
    conn = _fresh(tmp_path)
    try:
        cur = conn.execute(
            "INSERT INTO run_log(run_id, ts, level, ref, msg) VALUES (NULL,?,?,?,?)",
            ("2026-09-15 00:00:00", "WARN", "h", "telegram-строка"))
        log_id = cur.lastrowid
        conn.execute(
            "INSERT INTO legacy_map(legacy_db, legacy_table, legacy_id, target_table,"
            " target_id, migrated_at) VALUES ('tg','run_log','7','run_log',?,?)",
            (str(log_id), "2026-09-15 00:00:00"))
        conn.commit()

        res = schema.backfill_run_log_platform(conn)
        conn.commit()
        assert res["by_run"] == 0
        assert res["by_legacy_map"] == 1
        assert res["remaining"] == 0
        assert conn.execute("SELECT platform FROM run_log WHERE id=?", (log_id,)
                            ).fetchone()[0] == "telegram"
    finally:
        conn.close()


def test_backfill_is_idempotent(tmp_path):
    """Повторный прогон ничего не меняет (0 строк на каждом шаге)."""
    conn = _fresh(tmp_path)
    try:
        conn.execute("INSERT INTO run(platform, mode) VALUES ('telegram','collect')")
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO run_log(run_id, ts, level) VALUES (?,?,?)",
                     (rid, "2026-09-15 00:00:00", "INFO"))
        conn.commit()

        first = schema.backfill_run_log_platform(conn)
        conn.commit()
        second = schema.backfill_run_log_platform(conn)
        conn.commit()
        assert first["by_run"] == 1
        assert second["by_run"] == 0 and second["by_legacy_map"] == 0
        assert second["remaining"] == 0
    finally:
        conn.close()


def test_migrated_fixture_has_no_null_platform(migrated):
    """После миграции синтетического набора ни одной строки без платформы."""
    target, _stats = migrated
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM run_log WHERE platform IS NULL"
                            ).fetchone()[0] == 0
        platforms = {r[0] for r in conn.execute(
            "SELECT DISTINCT platform FROM run_log").fetchall()}
        assert platforms <= {"x", "telegram", "youtube"}
        assert "x" in platforms and "telegram" in platforms
    finally:
        conn.close()


def test_parity_run_log_rules_use_platform_column():
    """Правила parity больше не выводят платформу из ``run`` (без допущения)."""
    rules = {r.id: r for r in parity_report.RULES}
    for rule_id, platform in (("x.run_log", "x"), ("tg.run_log", "telegram")):
        sql = rules[rule_id].core_sql
        assert f"platform='{platform}'" in sql
        assert "JOIN run" not in sql
        assert "run_id IS NULL" not in sql


def test_platform_filter_plan_has_no_materialize(migrated):
    """``WHERE platform=...`` по ``run_log`` — SEARCH по индексу, без MATERIALIZE."""
    target, _stats = migrated
    conn = db.connect(target)
    try:
        plan = " | ".join(
            r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM run_log WHERE platform='x'"))
        assert "MATERIALIZE" not in plan.upper()
        assert "idx_run_log_platform" in plan
    finally:
        conn.close()
