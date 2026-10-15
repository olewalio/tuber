"""Часть 4 «волна 2»: журнал пропущенных слотов YouTube и охват по своим слотам."""

from __future__ import annotations

import pytest

from tuber.platforms.youtube import config, store as db, schedule

NOW = 1_800_000_000


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "wave2_yt.db")
    db.init_db(c)
    yield c
    c.close()


def _add_video(conn, video_id, published_at, channel_id="ch1", is_ai=1, is_shorts=None):
    db.upsert_channel(conn, {"channel_id": channel_id, "title": "ch", "first_seen": 1})
    data = {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": "t",
        "published_at": published_at,
        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/maxres.jpg",
        "first_seen": 1,
    }
    if is_shorts is not None:
        data["is_shorts"] = is_shorts
    db.upsert_video(conn, data)
    db.save_classification(conn, video_id, is_ai=is_ai, topic=None, confidence=1.0)


def test_missed_fresh_slot_lands_in_journal(conn):
    _add_video(conn, "fresh", NOW - 5 * 3600)   # возраст 5 ч: слоты h0 (0ч) и h1 (3ч) наступили
    # Снимков нет — оба слота пропущены.
    n = schedule.record_missed_slots(conn, NOW, config)
    assert n == 2, n
    rows = conn.execute(
        "SELECT bucket, due_at FROM youtube_slot_miss WHERE video_id='fresh'"
        " ORDER BY due_at").fetchall()
    assert [r["bucket"] for r in rows] == ["h0", "h1"]
    # Идемпотентно: повторный прогон не плодит строк.
    assert schedule.record_missed_slots(conn, NOW, config) == 0
    assert conn.execute("SELECT COUNT(*) FROM youtube_slot_miss").fetchone()[0] == 2


def test_captured_slot_not_in_journal(conn):
    _add_video(conn, "fresh", NOW - 5 * 3600)
    db.insert_snapshot(conn, "fresh", NOW - 4 * 3600, "h0", views=100)
    schedule.record_missed_slots(conn, NOW, config)
    buckets = {r[0] for r in conn.execute(
        "SELECT bucket FROM youtube_slot_miss WHERE video_id='fresh'")}
    assert buckets == {"h1"}, buckets


def test_slot_coverage_marks_own_queue(conn):
    _add_video(conn, "fresh", NOW - 5 * 3600)
    db.insert_snapshot(conn, "fresh", NOW - 4 * 3600, "h0", views=100)
    cov = schedule.slot_coverage(conn, NOW, config)
    assert cov["queue"] == "own_slots"
    assert "вне единой очереди" in cov["note"]
    assert cov["slots_expected"] == 2      # h0, h1
    assert cov["slots_missed"] == 1        # h1
    assert cov["slots_captured"] == 1
    assert abs(cov["coverage"] - 0.5) < 1e-9


def test_daily_missed_slot_recorded(conn):
    _add_video(conn, "long", NOW - 5 * 86400)   # 5 дней, интервал 12 ч, снимков нет
    schedule.record_missed_slots(conn, NOW, config)
    daily = conn.execute(
        "SELECT COUNT(*) FROM youtube_slot_miss WHERE video_id='long'"
        " AND bucket='d'").fetchone()[0]
    assert daily >= 1
