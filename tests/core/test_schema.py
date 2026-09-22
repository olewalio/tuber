"""Тесты единой схемы ядра (ТЗ-1 §7)."""

from __future__ import annotations

import sqlite3

from tuber.core import db, schema


def _fresh(tmp_path) -> sqlite3.Connection:
    conn = db.connect(str(tmp_path / "schema.db"))
    schema.init_schema(conn)
    return conn


def test_all_required_objects_present(tmp_path):
    conn = _fresh(tmp_path)
    try:
        assert schema.verify_schema(conn) == []
    finally:
        conn.close()


def test_required_tables_exist(tmp_path):
    conn = _fresh(tmp_path)
    try:
        actual = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table in schema.REQUIRED_TABLES:
            assert table in actual, table
    finally:
        conn.close()


def test_required_indexes_and_views_exist(tmp_path):
    conn = _fresh(tmp_path)
    try:
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        views = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
        for name in schema.REQUIRED_INDEXES:
            assert name in idx, name
        for name in schema.REQUIRED_VIEWS:
            assert name in views, name
    finally:
        conn.close()


def test_columns_match_spec(tmp_path):
    conn = _fresh(tmp_path)
    try:
        for table, cols in schema.required_columns().items():
            actual = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            for col in cols:
                assert col in actual, f"{table}.{col}"
    finally:
        conn.close()


def test_platform_seed_and_schema_version(tmp_path):
    conn = _fresh(tmp_path)
    try:
        codes = {r[0] for r in conn.execute("SELECT code FROM platform").fetchall()}
        # 'cross' — признак сквозного сюжета (story.platform='cross'), не
        # платформа-источник; добавляется тем же идемпотентным сидом.
        # 'web' — веб-фид/RSS, четвёртый тип цели рёбер (ТЗ-8 Р3.3).
        assert codes == {"youtube", "x", "telegram", "cross", "web"}
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
        assert version == schema.SCHEMA_VERSION
    finally:
        conn.close()


def test_negative_missing_column_is_detected(tmp_path):
    """Негативный тест: удалили колонку в fixture-схеме — проверка падает."""
    conn = _fresh(tmp_path)
    try:
        assert schema.verify_schema(conn) == []
        # Имитируем «сломанную» схему: колонка rubric отсутствует.
        conn.execute("ALTER TABLE candidate DROP COLUMN rubric")
        problems = schema.verify_schema(conn)
        assert any("candidate: нет колонки rubric" in p for p in problems)
    finally:
        conn.close()


def test_negative_missing_view_is_detected(tmp_path):
    conn = _fresh(tmp_path)
    try:
        conn.execute("DROP VIEW v_tg_scores")
        problems = schema.verify_schema(conn)
        assert any("v_tg_scores" in p for p in problems)
    finally:
        conn.close()


def test_init_schema_is_idempotent(tmp_path):
    conn = db.connect(str(tmp_path / "s2.db"))
    try:
        schema.init_schema(conn)
        schema.init_schema(conn)
        assert schema.verify_schema(conn) == []
    finally:
        conn.close()
