"""Тесты функций хранилища ядра (ТЗ-1 §4)."""

from __future__ import annotations

from tuber.core import db, schema, storage


def _init(path):
    conn = db.connect(path)
    schema.init_schema(conn)
    return conn


def test_upsert_source_idempotent(tmp_path):
    conn = _init(str(tmp_path / "s.db"))
    try:
        with db.write_tx(conn):
            sid1 = storage.upsert_source(conn, "youtube", "@a", external_id="UC1", title="A")
            sid2 = storage.upsert_source(conn, "youtube", "@a", external_id="UC1", title="A2")
        assert sid1 == sid2
        assert conn.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 1
        assert conn.execute("SELECT title FROM source").fetchone()[0] == "A2"
    finally:
        conn.close()


def test_upsert_content_and_snapshot_and_latest(tmp_path):
    conn = _init(str(tmp_path / "c.db"))
    try:
        with db.write_tx(conn):
            sid = storage.upsert_source(conn, "youtube", "@a", external_id="UC1")
            cid = storage.upsert_content(
                conn, "youtube", "vid1", source_id=sid, kind="video",
                published_at="2026-01-01 00:00:00")
            cid2 = storage.upsert_content(
                conn, "youtube", "vid1", source_id=sid, kind="video",
                published_at="2026-01-01 00:00:00")
            storage.add_snapshot(conn, cid, "2026-01-01 01:00:00", views=10)
            storage.add_snapshot(conn, cid, "2026-01-01 02:00:00", views=30)
            storage.recompute_content_latest(conn, cid)
        assert cid == cid2
        assert conn.execute("SELECT COUNT(*) FROM metric_snapshot").fetchone()[0] == 2
        row = conn.execute(
            "SELECT captured_at, views FROM content_latest WHERE content_id=?", (cid,)
        ).fetchone()
        assert row["captured_at"] == "2026-01-01 02:00:00"
        assert row["views"] == 30
    finally:
        conn.close()


def test_score_upsert_and_current_view(tmp_path):
    conn = _init(str(tmp_path / "sc.db"))
    try:
        with db.write_tx(conn):
            sid = storage.upsert_source(conn, "x", "acc")
            cid = storage.upsert_content(conn, "x", "t1", source_id=sid,
                                         published_at="2026-01-01 00:00:00")
            storage.upsert_score(conn, cid, "2026-01-01 01:00:00", significance=1.0)
            storage.upsert_score(conn, cid, "2026-01-01 02:00:00", significance=2.0)
        assert conn.execute("SELECT COUNT(*) FROM score").fetchone()[0] == 2
        row = conn.execute("SELECT significance FROM v_score_current").fetchone()
        assert row["significance"] == 2.0
    finally:
        conn.close()


def test_candidate_add_and_promote(tmp_path):
    conn = _init(str(tmp_path / "cand.db"))
    try:
        with db.write_tx(conn):
            storage.add_candidate(conn, "x", "cand1", kind="handle", seen_count=3)
            storage.add_candidate(conn, "x", "cand1", kind="handle", seen_count=3)
        assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 1
        with db.write_tx(conn):
            cid = storage.promote_candidate(conn, "x", "cand1", promoted_by="test")
        row = conn.execute("SELECT status, promoted_by FROM candidate WHERE id=?", (cid,)).fetchone()
        assert row["status"] == "promoted"
        assert row["promoted_by"] == "test"
    finally:
        conn.close()


def test_legacy_map_roundtrip(tmp_path):
    conn = _init(str(tmp_path / "lm.db"))
    try:
        with db.write_tx(conn):
            storage.set_legacy_map(conn, "os", "videos", "v1", "content", 42)
            storage.set_legacy_map(conn, "os", "videos", "v1", "content", 42)
        assert storage.get_legacy_map(conn, "os", "videos", "v1") == 42
        assert storage.get_legacy_map(conn, "os", "videos", "nope") is None
        assert conn.execute("SELECT COUNT(*) FROM legacy_map").fetchone()[0] == 1
    finally:
        conn.close()


def test_jdump():
    assert storage.jdump(None) is None
    assert storage.jdump({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
