"""Тесты композитного индекса виральности (tuber.viral). Сеть не используется."""

from __future__ import annotations

import json

import pytest

from tuber.platforms.youtube import store as db, viral

NOW = 1_800_000_000
DAY = 86400


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "viral_test.db")
    db.init_db(c)
    yield c
    c.close()


def add_video(conn, vid, cid="c1", published_at=None):
    db.upsert_channel(conn, {"channel_id": cid, "title": "Канал", "first_seen": 1})
    db.upsert_video(
        conn,
        {
            "video_id": vid,
            "channel_id": cid,
            "title": f"Видео {vid}",
            "published_at": published_at if published_at is not None else NOW - DAY,
            "first_seen": 1,
        },
    )


def score(conn, vid, computed_at=NOW, **fields):
    db.save_score(conn, vid, computed_at, **fields)


# --- 1. чистая формула ------------------------------------------------------


def test_index_none_with_fewer_than_two_axes():
    assert viral.index_from_axes({}, 0.0, 7.0) is None
    assert viral.index_from_axes({"views": 5.0}, 0.0, 7.0) is None


def test_index_is_geometric_mean_of_axes():
    # sqrt(4 * 9) = 6, свежее видео — распад 1.0.
    value = viral.index_from_axes({"views": 4.0, "likes": 9.0}, 0.0, 7.0)
    assert value == pytest.approx(6.0)


def test_index_decays_by_half_life():
    # Ровно период полураспада — индекс вдвое меньше.
    value = viral.index_from_axes({"views": 4.0, "likes": 9.0}, 7.0, 7.0)
    assert value == pytest.approx(3.0)


def test_decay_zero_half_life_is_neutral():
    assert viral.freshness_decay(100.0, 0.0) == 1.0


def test_index_rejects_nonpositive_axis():
    assert viral.index_from_axes({"views": 4.0, "likes": 0.0}, 0.0, 7.0) is None


# --- 2. оси и база канала ---------------------------------------------------


def test_axes_marks_zero_likes_and_drops_axis(conn):
    add_video(conn, "v1")
    score(conn, "v1", outlier_score=4.0, likes_per_1000=0.0, packaging_score=50.0)
    rows = viral.latest_score_rows(conn)
    medians = viral.channel_medians(rows, NOW)
    info = viral.axes_for(rows[0], medians)
    assert info["likes_zero"] is True
    assert info["likes_null"] is False
    assert "likes" not in info["axes"]


def test_axes_null_likes_is_missing_not_zero(conn):
    add_video(conn, "v1")
    score(conn, "v1", outlier_score=4.0, likes_per_1000=None, packaging_score=50.0)
    rows = viral.latest_score_rows(conn)
    info = viral.axes_for(rows[0], viral.channel_medians(rows, NOW))
    assert info["likes_null"] is True
    assert info["likes_zero"] is False
    assert "likes" not in info["axes"]


def test_channel_median_requires_min_base_and_window(conn):
    for vid in ("v1", "v2", "v3"):
        add_video(conn, vid)
        score(conn, vid, likes_per_1000=10.0)
    # Четвёртое видео старше окна — в медиану не идёт.
    add_video(conn, "old", published_at=NOW - 200 * DAY)
    score(conn, "old", likes_per_1000=1000.0)
    rows = viral.latest_score_rows(conn)
    medians = viral.channel_medians(rows, NOW)
    assert medians[("c1", "likes")] == pytest.approx(10.0)
    # Меньше min_base — медианы нет.
    assert viral.channel_medians(rows[:2], NOW).get(("c1", "likes")) is None


def test_likes_axis_uses_channel_median_not_mean(conn):
    # Один выброс (1000) не должен сдвинуться медиану (10, 10, 10 → 10).
    for vid in ("v1", "v2", "v3", "v4"):
        add_video(conn, vid)
        score(conn, vid, likes_per_1000=10.0)
    db.upsert_video(
        conn,
        {"video_id": "v4", "channel_id": "c1", "title": "v4",
         "published_at": NOW - DAY, "first_seen": 1},
    )
    score(conn, "v4", likes_per_1000=1000.0)
    rows = viral.latest_score_rows(conn)
    medians = viral.channel_medians(rows, NOW)
    assert medians[("c1", "likes")] == pytest.approx(10.0)


# --- 3. запись и разбивка ---------------------------------------------------


def test_refresh_writes_index_to_latest_row(conn):
    add_video(conn, "v1")
    score(conn, "v1", outlier_score=4.0, likes_per_1000=40.0, packaging_score=50.0)
    # Медиана лайков канала = 10 (три видео: 40, 10, 10), значит ось лайков = 4.
    for vid in ("v2", "v3"):
        add_video(conn, vid)
        score(conn, vid, likes_per_1000=10.0, outlier_score=1.0, packaging_score=50.0)

    stats = viral.refresh(conn, now=NOW)
    assert stats["indexed"] >= 1
    row = conn.execute(
        "SELECT viral_index, score_parts FROM video_scores WHERE video_id='v1'"
    ).fetchone()
    # Оси v1: views=4, likes=40/10=4, упаковка=50/50=1. Возраст 1 день, T=7.
    expected = (4.0 * 4.0 * 1.0) ** (1.0 / 3.0) * (0.5 ** (1.0 / 7.0))
    assert row["viral_index"] == pytest.approx(expected, rel=1e-6)
    parts = json.loads(row["score_parts"])
    assert parts[viral.VIRAL_PARTS_KEY]["axes"]["likes"] == pytest.approx(4.0)
    assert parts[viral.VIRAL_PARTS_KEY]["axes"]["views"] == pytest.approx(4.0)


def test_refresh_writes_null_for_single_axis(conn):
    add_video(conn, "v1")
    score(conn, "v1", outlier_score=4.0, packaging_score=50.0)
    viral.refresh(conn, now=NOW)
    row = conn.execute(
        "SELECT viral_index FROM video_scores WHERE video_id='v1'"
    ).fetchone()
    assert row["viral_index"] is None


def test_refresh_keeps_packaging_parts(conn):
    add_video(conn, "v1")
    score(conn, "v1", outlier_score=4.0, likes_per_1000=1.0,
          packaging_score=50.0,
          score_parts=json.dumps({"title": 30.0, "description": 20.0}))
    viral.refresh(conn, now=NOW)
    parts = json.loads(
        conn.execute(
            "SELECT score_parts FROM video_scores WHERE video_id='v1'"
        ).fetchone()["score_parts"]
    )
    assert parts["title"] == 30.0
    assert viral.VIRAL_PARTS_KEY in parts


# --- 4. потолок отношения оси (микровыборка не раздувает индекс) ------------


def test_axis_cap_limits_likes_ratio_on_micro_sample(conn):
    """Реальный кейс: 1 лайк на 3 просмотра даёт likes_per_1000 = 333.

    Медиана канала по лайкам = 10, значит отношение 33.3; потолок 25 режет
    его до 25, а ось помечается обрезанной.
    """
    for vid in ("m1", "m2", "m3"):
        add_video(conn, vid, cid="c1")
        score(conn, vid, likes_per_1000=10.0)
    add_video(conn, "hot", cid="c1")
    # 1 лайк на 3 просмотра: likes_per_1000 = 1000/3.
    score(conn, "hot", likes_per_1000=1000.0 / 3.0, outlier_score=0.43,
          comment_velocity=1.0)
    rows = viral.latest_score_rows(conn)
    medians = viral.channel_medians(rows, NOW)
    hot = next(r for r in rows if r["video_id"] == "hot")

    info = viral.axes_for(hot, medians, cap=25.0)
    assert info["axes"]["likes"] == pytest.approx(25.0)
    assert info["capped"] == [viral.AXIS_LIKES]


def test_axis_cap_does_not_touch_views_axis(conn):
    """Ось просмотров не ограничивается: большое отношение — реальные просмотры."""
    add_video(conn, "v1", cid="c1")
    score(conn, "v1", outlier_score=1000.0, likes_per_1000=10.0)
    rows = viral.latest_score_rows(conn)
    info = viral.axes_for(rows[0], viral.channel_medians(rows, NOW), cap=25.0)
    assert info["axes"][viral.AXIS_VIEWS] == pytest.approx(1000.0)
    assert viral.AXIS_VIEWS not in info["capped"]


def test_axis_cap_limits_comments_ratio(conn):
    for vid in ("m1", "m2", "m3"):
        add_video(conn, vid, cid="c1")
        score(conn, vid, comment_velocity=2.0)
    add_video(conn, "hot", cid="c1")
    score(conn, "hot", comment_velocity=200.0, outlier_score=1.0)
    rows = viral.latest_score_rows(conn)
    hot = next(r for r in rows if r["video_id"] == "hot")
    info = viral.axes_for(hot, viral.channel_medians(rows, NOW), cap=25.0)
    assert info["axes"][viral.AXIS_COMMENTS] == pytest.approx(25.0)
    assert viral.AXIS_COMMENTS in info["capped"]


def test_axis_cap_zero_disables_cap(conn):
    for vid in ("m1", "m2", "m3"):
        add_video(conn, vid, cid="c1")
        score(conn, vid, likes_per_1000=10.0)
    add_video(conn, "hot", cid="c1")
    score(conn, "hot", likes_per_1000=1000.0 / 3.0, outlier_score=0.43)
    rows = viral.latest_score_rows(conn)
    hot = next(r for r in rows if r["video_id"] == "hot")
    info = viral.axes_for(hot, viral.channel_medians(rows, NOW), cap=0)
    assert info["axes"]["likes"] == pytest.approx(1000.0 / 3.0 / 10.0)
    assert info["capped"] == []


def test_axis_cap_null_likes_still_missing(conn):
    """При likes IS NULL ось отсутствует и потолку нечего резать (поведение прежнее)."""
    add_video(conn, "v1", cid="c1")
    score(conn, "v1", outlier_score=4.0, likes_per_1000=None, packaging_score=50.0)
    rows = viral.latest_score_rows(conn)
    info = viral.axes_for(rows[0], viral.channel_medians(rows, NOW), cap=25.0)
    assert info["likes_null"] is True
    assert "likes" not in info["axes"]
    assert info["capped"] == []


def test_refresh_reports_capped_counter(conn):
    for vid in ("m1", "m2", "m3"):
        add_video(conn, vid, cid="c1")
        score(conn, vid, likes_per_1000=10.0)
    add_video(conn, "hot", cid="c1")
    score(conn, "hot", likes_per_1000=1000.0 / 3.0, outlier_score=4.0,
          packaging_score=50.0)
    stats = viral.refresh(conn, now=NOW)
    assert stats["axis_capped"] >= 1
    parts = json.loads(
        conn.execute(
            "SELECT score_parts FROM video_scores WHERE video_id='hot'"
        ).fetchone()["score_parts"]
    )[viral.VIRAL_PARTS_KEY]
    assert parts["capped"] is True
    assert parts["axes"]["likes"] == pytest.approx(25.0)
