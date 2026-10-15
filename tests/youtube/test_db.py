"""Тесты адаптера хранения YouTube (``tuber.platforms.youtube.store``).

Прежний ``tests/test_db.py`` проверял внутренности legacy-схемы ``tuber-os``
(создание таблиц ``videos``/``snapshots``/…, миграции ``ALTER TABLE``). В
монорепозитории этих таблиц нет: хранение — единое ядро, а адаптер
(``store.py``) лишь раскладывает legacy-форму по ядру. Поэтому тесты ниже
проверяют наблюдаемое поведение адаптера, а не DDL legacy. Легаси-тесты
схемы перечислены как осознанно выброшенные в отчёте ТЗ-2.

Сеть не используется.
"""

from __future__ import annotations

import sqlite3

import pytest

from tuber.core import schema
from tuber.platforms.youtube import config, store as db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "tuber_test.db")
    db.init_db(c)
    yield c
    c.close()


def _add_video(conn, video_id="v1", channel_id="c1", published_at=1_000_000, **extra):
    db.upsert_channel(conn, {"channel_id": channel_id, "title": "ch", "first_seen": 1})
    data = {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": "t",
        "published_at": published_at,
        "first_seen": 1,
    }
    data.update(extra)
    db.upsert_video(conn, data)


# --- схема ядра -------------------------------------------------------------

def test_init_db_idempotent_and_schema_complete(tmp_path):
    c = db.connect(tmp_path / "x.db")
    db.init_db(c)
    db.init_db(c)  # повторный вызов не должен падать
    assert schema.verify_schema(c) == []
    c.close()


def test_video_scores_view_columns(conn):
    """D-05/D-14: без freshness/viral_score, но с viral_index и осями."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(video_scores)")]
    assert len(cols) == 11
    assert "freshness" not in cols
    assert "viral_score" not in cols
    assert "viral_index" in cols
    assert {"vpd", "vpd_ratio", "comment_velocity"} <= set(cols)


def test_topics_seeded(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM topics")}
    assert set(config.TOPICS) <= names


# --- замеры -----------------------------------------------------------------

def test_first_snapshot_has_no_deltas(conn):
    _add_video(conn)
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 100, likes=5, comments=1)
    row = conn.execute("SELECT * FROM snapshots WHERE video_id='v1'").fetchone()
    assert row["delta_views"] is None
    assert row["interval_quality"] == "first"


def test_views_per_day_two_snapshots_known_interval(conn):
    _add_video(conn)
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 1000)
    db.insert_snapshot(conn, "v1", 2_000_000 + 3600, "h1", 1300)
    row = conn.execute(
        "SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
    assert row["interval_quality"] == "ok"
    assert row["delta_views"] == 300
    assert row["views_per_day"] == pytest.approx(300 * 24)
    assert row["views_per_hour"] == pytest.approx(300)


def test_interval_under_600_seconds_has_no_deltas(conn):
    _add_video(conn)
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 1000)
    db.insert_snapshot(conn, "v1", 2_000_000 + 300, "h1", 1100)
    row = conn.execute("SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
    assert row["delta_views"] is None
    assert row["interval_quality"] == "short"


def test_interval_748_seconds_short_speed_null_delta_kept(conn):
    _add_video(conn)
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 1000)
    db.insert_snapshot(conn, "v1", 2_000_000 + 748, "h1", 1200)
    row = conn.execute("SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
    assert row["delta_views"] == 200
    assert row["views_per_day"] is None
    assert row["interval_quality"] == "short"


def test_interval_3599_seconds_is_short(conn):
    _add_video(conn)
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 1000)
    db.insert_snapshot(conn, "v1", 2_000_000 + 3599, "h1", 1200)
    row = conn.execute("SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
    assert row["interval_quality"] == "short"
    assert row["views_per_day"] is None


def test_migration_interval_quality_zeroes_short_and_is_idempotent(conn):
    _add_video(conn)
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 1000)
    db.insert_snapshot(conn, "v1", 2_000_000 + 600, "h1", 1300)
    conn.execute("UPDATE snapshots SET interval_quality=NULL, views_per_day=999, views_per_hour=999")
    conn.commit()
    changed = db.migrate_interval_quality(conn, config)
    assert changed >= 1
    row = conn.execute(
        "SELECT interval_quality, views_per_day FROM snapshots ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()
    assert row["interval_quality"] == "short"
    assert row["views_per_day"] is None
    assert db.migrate_interval_quality(conn, config) == 0


def test_migration_respects_cfg_threshold(conn):
    _add_video(conn)
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 1000)
    db.insert_snapshot(conn, "v1", 2_000_000 + 1800, "h1", 1600)

    class Cfg:
        MIN_INTERVAL_FOR_SPEED_SECONDS = 1

    changed = db.migrate_interval_quality(conn, Cfg)
    assert changed >= 1
    row = conn.execute(
        "SELECT interval_quality, views_per_day FROM snapshots ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()
    assert row["interval_quality"] == "ok"


def test_negative_delta_written_and_flagged(conn):
    _add_video(conn)
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 5000)
    db.insert_snapshot(conn, "v1", 2_000_000 + 3600, "h1", 4000)
    row = conn.execute("SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
    assert row["delta_views"] == -1000
    assert row["is_anomaly"] == 1


def test_snapshot_slot_is_unique(conn):
    _add_video(conn)
    for _ in range(3):
        db.insert_snapshot(conn, "v1", 2_000_000, "h0", 100)
    assert conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"] == 1


# --- запись видео/каналов ---------------------------------------------------

def test_upsert_video_idempotent_first_seen_kept(conn):
    _add_video(conn, published_at=1000)
    conn.execute("UPDATE videos SET first_seen=123 WHERE video_id='v1'")
    conn.commit()
    db.upsert_video(conn, {"video_id": "v1", "channel_id": "c1", "title": "new",
                           "published_at": 1000, "first_seen": 999})
    rows = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "new"
    assert rows[0]["first_seen"] == 123


def test_recompute_shorts_converts_61_180_and_is_idempotent(conn):
    _add_video(conn, "v61", duration_seconds=61, is_shorts=0)
    _add_video(conn, "v180", duration_seconds=180, is_shorts=0)
    _add_video(conn, "v181", duration_seconds=181, is_shorts=1)
    changed = db.recompute_shorts(conn, config)
    assert changed == 3
    got = {r["video_id"]: r["is_shorts"] for r in conn.execute("SELECT * FROM videos")}
    assert got == {"v61": 1, "v180": 1, "v181": 0}
    assert db.recompute_shorts(conn, config) == 0


def test_recompute_shorts_respects_cfg_threshold(conn):
    _add_video(conn, "v1", duration_seconds=200, is_shorts=0)

    class Cfg:
        SHORTS_MAX_SECONDS = 300

    assert db.recompute_shorts(conn, Cfg) == 1
    assert conn.execute(
        "SELECT is_shorts FROM videos WHERE video_id='v1'").fetchone()["is_shorts"] == 1


# --- квота и LLM ------------------------------------------------------------

def test_log_quota_and_llm_usage(conn):
    db.log_quota(conn, 86400, "k1", 3, 7, "search", project="p1", ts=100000)
    q = conn.execute(
        "SELECT key_id, SUM(units) AS u, MAX(project) AS pr FROM quota_log GROUP BY key_id"
    ).fetchone()
    assert q["key_id"] == "k1"
    assert q["u"] == 7
    assert q["pr"] == "p1"
    db.log_llm_usage(conn, "classify", "deepseek", 10, 20, 0.5, 100000)
    u = conn.execute("SELECT * FROM llm_usage").fetchone()
    assert u["tokens_in"] == 10
    assert u["created_at"] == 100000


# --- классификация и скоры --------------------------------------------------

def test_save_classification_and_score(conn):
    _add_video(conn)
    db.save_classification(conn, "v1", is_ai=1, topic="прочее", title_ru="Т",
                           summary_ru="С", reason="ok", model="m", classified_at=555)
    c = conn.execute("SELECT * FROM video_classification WHERE video_id='v1'").fetchone()
    assert c["is_ai"] == 1
    assert c["title_ru"] == "Т"
    assert c["classified_at"] == 555

    db.save_score(conn, "v1", 1000, outlier_score=2.0, vpd=10.0,
                  likes_per_1000=5.0, packaging_score=3.0, score_parts='{"x":1}')
    s = conn.execute("SELECT * FROM video_scores WHERE video_id='v1'").fetchone()
    assert s["outlier_score"] == 2.0
    assert s["score_parts"] == '{"x":1}'
    assert s["viral_index"] is None
    db.set_viral_indices(conn, [("v1", 1000, 7.5, '{"a":1}')])
    s2 = conn.execute("SELECT * FROM video_scores WHERE video_id='v1'").fetchone()
    assert s2["viral_index"] == 7.5


def test_get_unclassified(conn):
    _add_video(conn, "v1")
    _add_video(conn, "v2")
    db.save_classification(conn, "v1", is_ai=1, topic="прочее", classified_at=1)
    rows = db.get_unclassified(conn)
    assert [r["video_id"] for r in rows] == ["v2"]


# --- кандидаты --------------------------------------------------------------

def test_channel_candidate_upsert_bumps_mentions(conn):
    db.upsert_channel_candidate(conn, {"channel_id": "UC1", "source": "chart"})
    db.upsert_channel_candidate(conn, {"channel_id": "UC1", "source": "chart"})
    row = conn.execute(
        "SELECT * FROM channel_candidates WHERE channel_id='UC1'").fetchone()
    assert row["mentions"] == 2


def test_channel_candidate_status_not_reset_by_upsert(conn):
    db.upsert_channel_candidate(conn, {"channel_id": "UC1", "source": "chart"})
    db.set_candidate_status(conn, "UC1", "rejected", "нет")
    db.upsert_channel_candidate(conn, {"channel_id": "UC1", "source": "chart"})
    row = conn.execute(
        "SELECT status, reject_reason FROM channel_candidates WHERE channel_id='UC1'").fetchone()
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "нет"


def test_query_candidate_upsert_bumps_hits(conn):
    db.upsert_query_candidate(conn, {"query": "ai", "source": "term_mining"})
    db.upsert_query_candidate(conn, {"query": "ai", "source": "term_mining"})
    row = conn.execute("SELECT hits FROM query_candidates WHERE query='ai'").fetchone()
    assert row["hits"] == 2


def test_get_query_candidates_filters_by_kind(conn):
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {"query": "ai", "kind": "query"})
    db.upsert_query_candidate(conn, {"query": "chart:RU:28", "kind": "chart"})
    queries = db.get_query_candidates(conn, kind="query")
    assert {r["query"] for r in queries} == {"ai"}


def test_mark_candidate_unresolved_then_rejected(conn):
    db.upsert_channel_candidate(conn, {"channel_id": "UC1", "source": "mention"})
    assert db.mark_candidate_unresolved(conn, "UC1", "нет handle", 2) == "unresolved"
    assert db.mark_candidate_unresolved(conn, "UC1", "нет handle", 2) == "rejected"
    row = conn.execute(
        "SELECT status, resolve_attempts FROM channel_candidates WHERE channel_id='UC1'"
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["resolve_attempts"] == 2


# --- отсев мёртвых фраз -----------------------------------------------------

def test_migrate_drop_dead_queries_marks_unusable_rows(conn):
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {"query": "нейросети", "kind": "query", "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "chart:RU:28", "kind": "query", "status": "accepted"})
    db.migrate_drop_dead_queries(conn)
    got = {r["query"]: r["status"] for r in conn.execute("SELECT * FROM query_candidates")}
    assert got["нейросети"] == "accepted"
    assert got["chart:RU:28"] == "dropped"


def test_migrate_restore_usable_queries_returns_misclosed_rows(conn):
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {"query": "ии", "kind": "query", "status": "accepted"})
    conn.execute(
        "UPDATE query_candidates SET status='dropped', reject_reason=?",
        (db._DEAD_QUERY_REASON,),
    )
    conn.commit()
    db.migrate_restore_usable_queries(conn)
    row = conn.execute("SELECT status FROM query_candidates WHERE query='ии'").fetchone()
    assert row["status"] == "accepted"


def test_install_compat_is_reentrant(tmp_path):
    """Повторная установка слоя совместимости не падает и не плодит объектов."""
    c = db.connect(tmp_path / "re.db")
    db.init_db(c)
    db.install_compat(c)
    db.install_compat(c)
    assert c.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    c.close()
