"""Часть 2 «волна 2»: дедуп алертов и кросс-платформенный сторож."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.core import db, health_all, health_dedup, schema, timeutil


# ---------------------------------------------------------------------------
# Дедуп
# ---------------------------------------------------------------------------

def test_dedup_two_calls_one_line(tmp_path):
    seen = str(tmp_path / "seen.json")
    now = 1_000_000.0
    first = health_dedup.filter_alerts(["проблема A"], seen, now=now)
    second = health_dedup.filter_alerts(["проблема A"], seen, now=now + 60)
    assert first == ["проблема A"]
    assert second == [], "одинаковая проблема не должна дублироваться в окне"
    state = health_dedup.load_seen(seen)
    assert len(state) == 1


def test_dedup_key_reset_when_problem_gone(tmp_path):
    seen = str(tmp_path / "seen.json")
    now = 2_000_000.0
    health_dedup.filter_alerts(["проблема A"], seen, now=now)
    # Проблема исчезла: текущий набор пуст -> ключ удаляется.
    assert health_dedup.filter_alerts([], seen, now=now + 60) == []
    assert health_dedup.load_seen(seen) == {}
    # Новая вспышка снова доложит.
    assert health_dedup.filter_alerts(["проблема A"], seen, now=now + 120) == \
        ["проблема A"]


def test_dedup_reemits_after_window(tmp_path):
    seen = str(tmp_path / "seen.json")
    now = 3_000_000.0
    health_dedup.filter_alerts(["проблема A"], seen, now=now)
    within = health_dedup.filter_alerts(["проблема A"], seen,
                                        now=now + 6 * 3600 - 1)
    beyond = health_dedup.filter_alerts(["проблема A"], seen,
                                        now=now + 6 * 3600 + 1)
    assert within == []
    assert beyond == ["проблема A"]


def test_dedup_state_file_written_atomically(tmp_path):
    seen = str(tmp_path / "nested" / "seen.json")
    health_dedup.filter_alerts(["A"], seen, now=1.0)
    import os
    assert os.path.exists(seen)
    # Каталог создаётся сам; временные файлы не остаются.
    left = [n for n in os.listdir(os.path.dirname(seen)) if n.startswith(".health_seen")]
    assert left == []


# ---------------------------------------------------------------------------
# Кросс-платформенный сторож
# ---------------------------------------------------------------------------

def _init(path):
    conn = db.connect(path)
    schema.init_schema(conn)
    return conn


def _content(conn, cid, platform, first_seen):
    conn.execute(
        "INSERT INTO content(id, platform, source_id, external_id,"
        " published_at, first_seen_at)"
        " VALUES (?,?,?,?,?,?)",
        (cid, platform, None, f"{platform}-{cid}", first_seen, first_seen))


def _old_iso(hours):
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime(timeutil.ISO_FMT)


def test_freshness_ok_when_recent(tmp_path):
    conn = _init(str(tmp_path / "h.db"))
    try:
        _content(conn, 1, "telegram", timeutil.iso_now())
        _content(conn, 2, "youtube", _old_iso(1))
        res = health_all.check_platform_freshness(conn, "telegram", 6.0)
        assert res["alert"] is False
        res_yt = health_all.check_platform_freshness(conn, "youtube", 30.0)
        assert res_yt["alert"] is False
    finally:
        conn.close()


def test_freshness_alert_when_stale(tmp_path):
    conn = _init(str(tmp_path / "h2.db"))
    try:
        _content(conn, 1, "telegram", _old_iso(10))
        res = health_all.check_platform_freshness(conn, "telegram", 6.0)
        assert res["alert"] is True and res["severity"] == "ALERT"
        assert res["name"] == "freshness_telegram"
    finally:
        conn.close()


def test_freshness_alert_when_no_content(tmp_path):
    conn = _init(str(tmp_path / "h3.db"))
    try:
        res = health_all.check_platform_freshness(conn, "youtube", 30.0)
        assert res["alert"] is True
        assert "нет вообще" in res["msg"]
    finally:
        conn.close()


def test_open_runs_alert_with_count(tmp_path):
    conn = _init(str(tmp_path / "h4.db"))
    try:
        conn.execute(
            "INSERT INTO run(platform, started_at, finished_at, mode)"
            " VALUES ('x', ?, NULL, 'discover:all:120')", (_old_iso(5),))
        conn.execute(
            "INSERT INTO run(platform, started_at, finished_at, mode)"
            " VALUES ('telegram', ?, NULL, 'collect')", (_old_iso(4),))
        res = health_all.check_open_runs(conn, 3.0)
        assert res["alert"] is True and res["value"] == 2
        # Свежий незакрытый прогон не считается.
        conn.execute(
            "INSERT INTO run(platform, started_at, finished_at, mode)"
            " VALUES ('x', ?, NULL, 'collect')", (timeutil.iso_now(),))
        assert health_all.check_open_runs(conn, 3.0)["value"] == 2
    finally:
        conn.close()


def test_overdue_measures_alert_with_count(tmp_path):
    conn = _init(str(tmp_path / "h5.db"))
    try:
        _content(conn, 1, "telegram", _old_iso(100))
        conn.execute(
            "INSERT INTO metric_schedule(content_id, platform, stage, due_at,"
            " attempt, status) VALUES (1, 'telegram', '1h', ?, 0, 'pending')",
            (_old_iso(20),))
        conn.execute(
            "INSERT INTO metric_schedule(content_id, platform, stage, due_at,"
            " attempt, status) VALUES (1, 'telegram', '6h', ?, 0, 'pending')",
            (_old_iso(1),))
        res = health_all.check_overdue_measures(conn, 6.0)
        assert res["alert"] is True and res["value"] == 1
    finally:
        conn.close()


def test_run_all_shape_and_alerts(tmp_path):
    conn = _init(str(tmp_path / "h6.db"))
    try:
        _content(conn, 1, "telegram", _old_iso(50))
        res = health_all.run_all(conn, freshness_hours={"telegram": 6.0},
                                 open_run_hours=3.0, overdue_hours=6.0)
        assert res["ok"] is False
        names = {c["name"] for c in res["alerts"]}
        assert "freshness_telegram" in names
        # История пишется в run_log с platform='all'.
        n = conn.execute("SELECT COUNT(*) FROM run_log WHERE platform='all'").fetchone()[0]
        assert n >= 1
        assert health_all.format_alerts(res).startswith("ALERT:")
    finally:
        conn.close()
