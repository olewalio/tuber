"""ТЗ-B6 D-70: клэмп монотонности просмотров на уровне записи снимка метрик.

Инвариант «просмотры не уменьшаются» раньше жил только на путях очереди
(``metrics._write_snapshot``) и в Telegram-триггерах; общий писатель
``storage.add_snapshot`` и X-путь могли записать убывание. Клэмп перенесён в
``storage.add_snapshot``, факт срабатывания считается счётчиком
``storage.views_clamp_count``.
"""
from __future__ import annotations

from tuber.core import db, schema, storage


def _init(path):
    conn = db.connect(path)
    schema.init_schema(conn)
    return conn


def _seed_content(conn):
    sid = storage.upsert_source(conn, "youtube", "@a", external_id="UC1")
    cid = storage.upsert_content(conn, "youtube", "vid1", source_id=sid, kind="video",
                                 published_at="2026-01-01 00:00:00")
    return cid


def _views(conn, cid, captured_at):
    row = conn.execute(
        "SELECT views FROM metric_snapshot WHERE content_id=? AND captured_at=?",
        (cid, captured_at)).fetchone()
    return row["views"] if row else None


def test_lower_views_write_does_not_reduce_stored_value(tmp_path):
    conn = _init(str(tmp_path / "d70.db"))
    try:
        cid = _seed_content(conn)
        storage.reset_views_clamp_count()
        storage.add_snapshot(conn, cid, "2026-01-01 01:00:00", views=100)
        storage.add_snapshot(conn, cid, "2026-01-01 02:00:00", views=50)   # меньше — клэмп
        assert _views(conn, cid, "2026-01-01 02:00:00") == 100
        assert storage.views_clamp_count() == 1
    finally:
        conn.close()


def test_higher_views_write_passes_through(tmp_path):
    conn = _init(str(tmp_path / "d70b.db"))
    try:
        cid = _seed_content(conn)
        storage.reset_views_clamp_count()
        storage.add_snapshot(conn, cid, "2026-01-01 01:00:00", views=100)
        storage.add_snapshot(conn, cid, "2026-01-01 02:00:00", views=250)
        assert _views(conn, cid, "2026-01-01 02:00:00") == 250
        assert storage.views_clamp_count() == 0
    finally:
        conn.close()


def test_rewrite_same_timestamp_lower_is_clamped(tmp_path):
    conn = _init(str(tmp_path / "d70c.db"))
    try:
        cid = _seed_content(conn)
        storage.reset_views_clamp_count()
        storage.add_snapshot(conn, cid, "2026-01-01 01:00:00", views=100)
        storage.add_snapshot(conn, cid, "2026-01-01 01:00:00", views=10)
        assert _views(conn, cid, "2026-01-01 01:00:00") == 100
        assert storage.views_clamp_count() == 1
    finally:
        conn.close()


def test_clamp_does_not_block_first_write_with_null_previous(tmp_path):
    conn = _init(str(tmp_path / "d70d.db"))
    try:
        cid = _seed_content(conn)
        storage.reset_views_clamp_count()
        storage.add_snapshot(conn, cid, "2026-01-01 01:00:00", views=None)
        storage.add_snapshot(conn, cid, "2026-01-01 02:00:00", views=5)
        assert _views(conn, cid, "2026-01-01 02:00:00") == 5
        assert storage.views_clamp_count() == 0
    finally:
        conn.close()
