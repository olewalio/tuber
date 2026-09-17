"""Тесты расписания замеров (tuber.schedule). Сеть не используется: мок tuber.yt."""

from __future__ import annotations

import pytest

from tuber.platforms.youtube import config, store as db, schedule

NOW = 1_800_000_000


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "schedule_test.db")
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
    # Разбор проставляем явно: план строится только по is_ai = 1.
    db.save_classification(conn, video_id, is_ai=is_ai, topic=None, confidence=1.0)


# --- план замеров ----------------------------------------------------------


def test_plan_slots_for_1h_3d_20d(conn):
    _add_video(conn, "v1h", NOW - 3600)  # 1 час
    _add_video(conn, "v3d", NOW - 3 * 86400)  # 3 дня
    _add_video(conn, "v20d", NOW - 20 * 86400)  # 20 дней

    plan = dict(schedule.plan_snapshots(conn, NOW, config))
    assert plan["v1h"] == "h0"  # свежее -> часовой слот
    assert plan["v3d"] == "d"  # 2-10 дней -> суточный слот
    assert plan["v20d"] == "d"  # старше 10 дней -> суточный слот


def test_plan_excludes_older_than_max_track_days(conn):
    _add_video(conn, "v_old", NOW - (config.MAX_TRACK_DAYS + 1) * 86400)
    plan = schedule.plan_snapshots(conn, NOW, config)
    assert plan == []


def test_plan_does_not_repeat_existing_slot(conn):
    _add_video(conn, "v1h", NOW - 3600)
    db.insert_snapshot(conn, "v1h", NOW - 100, "h0", views=100)
    assert schedule.plan_snapshots(conn, NOW, config) == []


def test_daily_slot_interval_twice_vs_once(conn):
    # 5 дней: интервал 12 часов.
    _add_video(conn, "young", NOW - 5 * 86400)
    db.insert_snapshot(conn, "young", NOW - 13 * 3600, "d", views=100)
    # 20 дней: интервал 24 часа, 13 часов мало — не планируем.
    _add_video(conn, "old", NOW - 20 * 86400)
    db.insert_snapshot(conn, "old", NOW - 13 * 3600, "d", views=100)

    plan = dict(schedule.plan_snapshots(conn, NOW, config))
    assert plan.get("young") == "d"  # прошло 13ч >= 12ч
    assert "old" not in plan  # прошло 13ч < 24ч

    # Через сутки и старый снова в плане.
    plan_later = dict(schedule.plan_snapshots(conn, NOW + 86400, config))
    assert plan_later.get("old") == "d"


def test_fresh_video_bucket_grows_with_age(conn):
    _add_video(conn, "v", NOW - 3600 - 3 * 3600)  # 4 часа
    plan = dict(schedule.plan_snapshots(conn, NOW, config))
    assert plan["v"] == "h1"  # смещение 3ч <= 4ч < 6ч


# --- прогон замеров --------------------------------------------------------


class FakeClient:
    """Мок: первый батч падает, второй отдаёт статистику."""

    def __init__(self, fail_first=True):
        self.fail_first = fail_first
        self.calls = []

    def videos_by_ids(self, ids, parts="snippet,statistics,contentDetails"):
        ids = list(ids)
        self.calls.append(ids)
        if self.fail_first and len(self.calls) == 1:
            from tuber.platforms.youtube import api as yt

            raise yt.YouTubeError(500, {"error": {}}, "videos")
        return [
            {
                "id": vid,
                "statistics": {"viewCount": "1000", "likeCount": "10", "commentCount": "2"},
            }
            for vid in ids
        ]


def test_run_snapshots_captures_and_isolates_failed_batch(conn, monkeypatch):
    for i in range(60):  # 60 свежих -> 2 батча (50 + 10)
        _add_video(conn, f"v{i:02d}", NOW - 3600, channel_id="ch1")

    fake = FakeClient(fail_first=True)
    monkeypatch.setattr(schedule.yt, "YouTubeClient", lambda conn=None: fake)

    summary = schedule.run_snapshots(conn, config, now=NOW)
    assert summary["planned"] == 60
    assert summary["batches"] == 2
    assert summary["failed_batches"] == 1
    assert summary["captured"] == 10  # второй батч прошёл несмотря на сбой первого
    assert summary["errors"]

    rows = conn.execute("SELECT * FROM snapshots").fetchall()
    assert len(rows) == 10
    assert all(r["bucket"] == "h0" for r in rows)
    assert all(r["source"] == config.SOURCE_FRESH for r in rows)
    assert rows[0]["views"] == 1000


def test_run_snapshots_applies_plan_and_daily_source(conn, monkeypatch):
    _add_video(conn, "v1h", NOW - 3600)
    _add_video(conn, "v5d", NOW - 5 * 86400)

    fake = FakeClient(fail_first=False)
    monkeypatch.setattr(schedule.yt, "YouTubeClient", lambda conn=None: fake)

    summary = schedule.run_snapshots(conn, config, now=NOW)
    assert summary["captured"] == 2
    rows = {
        r["video_id"]: r["source"]
        for r in conn.execute("SELECT video_id, source FROM snapshots")
    }
    assert rows["v1h"] == config.SOURCE_FRESH
    assert rows["v5d"] == config.SOURCE_DAILY

    # Повторный прогон в тот же момент ничего не планирует.
    assert schedule.run_snapshots(conn, config, now=NOW)["planned"] == 0


def test_plan_excludes_not_ai_and_null(conn):
    # is_ai=0 и NULL в план замеров не попадают.
    _add_video(conn, "v_ai", NOW - 3600, is_ai=1)
    _add_video(conn, "v_not_ai", NOW - 3600, is_ai=0)
    _add_video(conn, "v_null", NOW - 3600, is_ai=None)

    plan = dict(schedule.plan_snapshots(conn, NOW, config))
    assert "v_ai" in plan
    assert "v_not_ai" not in plan
    assert "v_null" not in plan


def test_plan_excludes_stale_null_until_reclassified(conn):
    # NULL старше суток тоже вне плана до повторного разбора.
    _add_video(conn, "v_null_old", NOW - 2 * 86400, is_ai=None)
    plan = schedule.plan_snapshots(conn, NOW, config)
    assert plan == []

    # После успешного разбора (is_ai=1) видео возвращается в план.
    db.save_classification(conn, "v_null_old", is_ai=1, topic=None, confidence=1.0)
    plan2 = dict(schedule.plan_snapshots(conn, NOW, config))
    assert plan2.get("v_null_old") == "d"


def test_plan_video_without_classification_row_excluded(conn):
    # Видео, вообще не прошедшее разбор, не меряется.
    db.upsert_channel(conn, {"channel_id": "ch1", "title": "ch", "first_seen": 1})
    db.upsert_video(
        conn,
        {
            "video_id": "no_class",
            "channel_id": "ch1",
            "title": "t",
            "published_at": NOW - 3600,
            "thumbnail_url": "https://i.ytimg.com/vi/no_class/maxres.jpg",
            "first_seen": 1,
        },
    )
    assert schedule.plan_snapshots(conn, NOW, config) == []


# --- слоты шортсов ---------------------------------------------------------


def test_fresh_short_slot_comes_earlier_than_long(conn):
    """При прочих равных свежий шортс уходит в свой слот раньше полного."""
    # Оба опубликованы 2 часа назад. Полное уже сняло исходный слот h0.
    _add_video(conn, "long", NOW - 2 * 3600, is_shorts=0)
    _add_video(conn, "short", NOW - 2 * 3600, is_shorts=1)
    db.insert_snapshot(conn, "long", NOW - 100, "h0", views=100)

    plan = dict(schedule.plan_snapshots(conn, NOW, config))
    # Полное: bucket h0 уже снят -> пропуск. Шортс: возраст ускорен x1.5,
    # 2ч * 1.5 = 3ч -> слот h1 и он ещё не снят.
    assert plan.get("short") == "h1"
    assert "long" not in plan


def test_short_and_long_both_planned_at_zero_age(conn):
    """На старте (возраст ~0) оба формата получают h0, разница только в темпе."""
    _add_video(conn, "short", NOW - 60, is_shorts=1)
    _add_video(conn, "long", NOW - 60, is_shorts=0)
    plan = dict(schedule.plan_snapshots(conn, NOW, config))
    assert plan["short"] == "h0"
    assert plan["long"] == "h0"


# --- добор метрик архивных видео (ТЗ-7 §1, долг D-47) ----------------------


def _axes_stub(monkeypatch, mapping):
    """Подменить viral.compute: video_id -> число осей."""
    def fake_compute(conn, now=None, cfg=config):
        return {vid: {"axes": {f"a{i}": 1.0 for i in range(n)}}
                for vid, n in mapping.items()}
    monkeypatch.setattr("tuber.platforms.youtube.viral.compute", fake_compute)


def test_fill_gap_selects_only_under_two_axes(conn, monkeypatch):
    _add_video(conn, "good", NOW - 10 * 86400)   # 2 оси — не кандидат
    _add_video(conn, "one", NOW - 10 * 86400)
    _add_video(conn, "zero", NOW - 10 * 86400)
    _axes_stub(monkeypatch, {"good": 2, "one": 1, "zero": 0})
    out = schedule.fill_gap_candidates(conn, 10, NOW, config)
    assert set(out) == {"one", "zero"}
    assert "good" not in out


def test_fill_gap_priority_one_axis_then_zero_newest_first(conn, monkeypatch):
    # 1 ось: свежие первыми; 0 осей: тоже свежие первыми, но ПОСЛЕ всех с 1 осью.
    _add_video(conn, "one_old", NOW - 30 * 86400)
    _add_video(conn, "one_new", NOW - 3 * 86400)
    _add_video(conn, "zero_old", NOW - 40 * 86400)
    _add_video(conn, "zero_new", NOW - 2 * 86400)
    _axes_stub(monkeypatch, {"one_old": 1, "one_new": 1,
                             "zero_old": 0, "zero_new": 0})
    out = schedule.fill_gap_candidates(conn, 10, NOW, config)
    assert out == ["one_new", "one_old", "zero_new", "zero_old"]


def test_fill_gap_respects_limit(conn, monkeypatch):
    for i in range(5):
        _add_video(conn, f"one{i}", NOW - (i + 1) * 86400)
    _axes_stub(monkeypatch, {f"one{i}": 1 for i in range(5)})
    out = schedule.fill_gap_candidates(conn, 2, NOW, config)
    assert out == ["one0", "one1"]  # свежие и только N


def test_fill_gap_disabled_returns_nothing(conn, monkeypatch):
    _add_video(conn, "one", NOW - 86400)
    _axes_stub(monkeypatch, {"one": 1})
    assert schedule.fill_gap_candidates(conn, 0, NOW, config) == []


@pytest.mark.parametrize("n,expected", [(0, 0), (1, 1), (50, 1), (51, 2),
                                        (200, 4), (9111, 183)])
def test_count_requests(n, expected):
    assert schedule.count_requests(n) == expected


def test_fill_gap_n_zero_keeps_plan_unchanged(conn, monkeypatch):
    """N=0: добор выключен, обычный план по корзинам и поведение прежние."""
    _add_video(conn, "v1h", NOW - 3600)
    _add_video(conn, "v5d", NOW - 5 * 86400)

    def boom(*a, **k):  # compute не должен вызываться при N=0
        raise AssertionError("viral.compute вызван при --fill-gap 0")

    monkeypatch.setattr("tuber.platforms.youtube.viral.compute", boom)
    fake = FakeClient(fail_first=False)
    summary = schedule.run_snapshots(conn, config, now=NOW, client=fake)
    assert summary["planned"] == 2
    assert summary["fill_gap"] == 0
    assert summary["requests"] == 1
    assert summary["quota_remaining"] is None
    assert summary["captured"] == 2


def test_fill_gap_run_captures_archived_with_daily_bucket(conn, monkeypatch):
    _add_video(conn, "arch1", NOW - 100 * 86400)
    _add_video(conn, "arch2", NOW - 120 * 86400)
    _axes_stub(monkeypatch, {"arch1": 1, "arch2": 0})
    fake = FakeClient(fail_first=False)
    summary = schedule.run_snapshots(conn, config, now=NOW, client=fake,
                                     fill_gap=2)
    assert summary["fill_gap"] == 2
    assert summary["planned"] == 0  # архив вне обычного плана
    assert summary["requests"] == 1
    assert summary["captured"] == 2
    rows = {r["video_id"]: r for r in conn.execute("SELECT * FROM snapshots")}
    assert rows["arch1"]["bucket"] == "d"
    assert rows["arch1"]["source"] == config.SOURCE_DAILY
    assert rows["arch2"]["views"] == 1000


def test_fill_gap_repeat_without_new_metrics_same_axes(conn, monkeypatch):
    """Повторный прогон без новых метрик: те же оси, тот же порядок, без сбоев."""
    _add_video(conn, "arch", NOW - 100 * 86400)
    _axes_stub(monkeypatch, {"arch": 1})
    fake = FakeClient(fail_first=False)
    first = schedule.fill_gap_candidates(conn, 5, NOW, config)
    schedule.run_snapshots(conn, config, now=NOW, client=fake, fill_gap=5)
    second = schedule.fill_gap_candidates(conn, 5, NOW, config)
    assert first == second == ["arch"]


class QuotaClient(FakeClient):
    """Мок с остатком квоты: проверяем, что бюджет попадает в сводку."""

    def quota_remaining(self):
        return 1234


def test_fill_gap_summary_reports_quota_remaining(conn, monkeypatch):
    _add_video(conn, "v1h", NOW - 3600)
    fake = QuotaClient(fail_first=False)
    summary = schedule.run_snapshots(conn, config, now=NOW, client=fake)
    assert summary["quota_remaining"] == 1234
