"""Часть 3 «волна 2»: связь по стадии, причины закрытия, reconcile.

Проверяется:
  * ``_nearest_snapshot`` сначала ищет снимок с ``bucket = stage`` и лишь потом
    падает на поиск по времени;
  * ``sweep_overdue`` проставляет ``measured`` / ``measured_late``;
  * ``close_reason`` различает ``no_snapshot_ever`` / ``grace_expired`` /
    ``attempts_exhausted``;
  * ``reconcile_closed`` идемпотентно возвращает ``measured`` там, где снимок
    стадии теперь находится, и уточняет причину остальным.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.core import db, metrics, schema

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _db(tmp_path, name="w2.db"):
    con = db.connect(str(tmp_path / name))
    schema.init_schema(con)
    return con


def _source(con, sid, platform="telegram"):
    con.execute("INSERT INTO source(id, platform, handle, status)"
                " VALUES (?,?,?, 'active')", (sid, platform, f"ch{sid}"))


def _content(con, cid, published, sid=1, platform="telegram"):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at)"
        " VALUES (?,?,?,?,?)",
        (cid, platform, sid, f"{platform}-{cid}", _iso(published)))


def _snap(con, cid, captured, *, views=100, bucket=None, platform="telegram"):
    con.execute(
        "INSERT INTO metric_snapshot(content_id, platform, captured_at, bucket, views)"
        " VALUES (?,?,?,?,?)", (cid, platform, _iso(captured), bucket, views))


def test_nearest_snapshot_prefers_bucket_then_time(tmp_path):
    con = _db(tmp_path)
    try:
        _source(con, 1)
        pub = NOW - timedelta(hours=20)
        _content(con, 1, pub)
        due = pub + timedelta(hours=6)          # срок стадии 6h
        # Снимок по времени далеко (10 ч), но bucket='6h' — окно стадии ±12 ч.
        _snap(con, 1, due + timedelta(hours=10), bucket="6h")
        got = metrics._nearest_snapshot(con, 1, _iso(due), 3.0, stage="6h")
        assert got is not None and got["linked_by"] == "bucket"
        # Без bucket — срабатывает прежний поиск по времени.
        got2 = metrics._nearest_snapshot(con, 1, _iso(due), 3.0, stage=None)
        assert got2 is None, "снимок вне ±3 ч по времени не должен связаться"
    finally:
        con.close()


def test_sweep_links_by_bucket_and_marks_late(tmp_path):
    con = _db(tmp_path)
    try:
        _source(con, 1)
        pub = NOW - timedelta(hours=10)
        _content(con, 1, pub)
        # bucket='1h', но на 8 ч позже due (допуск ±3 ч) -> measured_late,
        # при этом связь по стадии честная (в окне ±12 ч).
        _snap(con, 1, pub + timedelta(hours=9), bucket="1h", views=500)
        metrics.plan_stages(con, now=NOW, lookback_hours=96)
        stats = metrics.sweep_overdue(con, now=NOW, fetch=None)
        assert stats["linked_by_bucket"] >= 1
        row = con.execute(
            "SELECT status, done_at FROM metric_schedule WHERE stage='1h'").fetchone()
        assert row["status"] == metrics.STAGE_STATUS_MEASURED_LATE
        assert row["done_at"] == _iso(pub + timedelta(hours=9))
    finally:
        con.close()


def test_close_reason_no_snapshot_ever(tmp_path):
    con = _db(tmp_path)
    try:
        _source(con, 1)
        _content(con, 1, NOW - timedelta(hours=200))
        metrics.plan_stages(con, now=NOW, lookback_hours=240)
        metrics.sweep_overdue(con, now=NOW, fetch=None)
        reasons = [r[0] for r in con.execute(
            "SELECT close_reason FROM metric_schedule")]
        assert reasons and set(reasons) == {metrics.CLOSE_REASON_NEVER}
    finally:
        con.close()


def test_close_reason_grace_expired(tmp_path):
    con = _db(tmp_path)
    try:
        _source(con, 1)
        pub = NOW - timedelta(hours=200)
        _content(con, 1, pub)
        # Снимок есть (has_any), но не той стадии и не в окне -> не linked.
        _snap(con, 1, pub + timedelta(hours=50), bucket=None)
        metrics.plan_stages(con, now=NOW, lookback_hours=240)
        metrics.sweep_overdue(con, now=NOW, fetch=None)
        reasons = {r[0] for r in con.execute(
            "SELECT close_reason FROM metric_schedule WHERE status='closed_no_data'")}
        assert reasons == {metrics.CLOSE_REASON_GRACE}
    finally:
        con.close()


def test_close_reason_attempts_exhausted(tmp_path):
    con = _db(tmp_path)
    try:
        _source(con, 1)
        pub = NOW - timedelta(hours=3)
        _content(con, 1, pub)
        # Есть хоть один снимок, но далеко и без bucket -> время не свяжет.
        _snap(con, 1, NOW - timedelta(hours=100), bucket=None)
        metrics.plan_stages(con, now=NOW, lookback_hours=96)
        con.execute("UPDATE metric_schedule SET attempt=? WHERE stage='1h'",
                    (metrics.MAX_ATTEMPTS - 1,))
        con.commit()
        metrics.sweep_overdue(con, now=NOW, fetch=None)
        row = con.execute(
            "SELECT status, close_reason FROM metric_schedule WHERE stage='1h'").fetchone()
        assert row["status"] == metrics.STAGE_STATUS_CLOSED_NO_DATA
        assert row["close_reason"] == metrics.CLOSE_REASON_ATTEMPTS
    finally:
        con.close()


def test_reconcile_closed_reactivates_and_idempotent(tmp_path):
    con = _db(tmp_path)
    try:
        _source(con, 1)
        _source(con, 2)
        pub = NOW - timedelta(hours=30)
        _content(con, 1, pub, sid=1)
        _content(con, 2, pub, sid=2)
        due = _iso(pub + timedelta(hours=1))
        # content 1: снимок стадии есть -> должен стать measured.
        _snap(con, 1, pub + timedelta(hours=6), bucket="1h")
        # content 2: снимков нет -> остаётся закрытым с no_snapshot_ever.
        con.executemany(
            "INSERT INTO metric_schedule(content_id, platform, stage, due_at,"
            " done_at, attempt, status) VALUES (?,?,?,?,?,?,?)",
            [(1, "telegram", "1h", due, due, 1, "closed_no_data"),
             (2, "telegram", "1h", due, due, 1, "closed_no_data")])
        con.commit()
        first = metrics.reconcile_closed(con, now=NOW)
        assert first["reactivated"] == 1
        rows = dict(con.execute(
            "SELECT content_id, status FROM metric_schedule"))
        assert rows[1] == metrics.STAGE_STATUS_MEASURED
        assert rows[2] == metrics.STAGE_STATUS_CLOSED_NO_DATA
        assert first["by_reason"][metrics.CLOSE_REASON_NEVER] == 1
        # Идемпотентность.
        second = metrics.reconcile_closed(con, now=NOW)
        assert second["reactivated"] == 0
        assert second["reason_changed"] == 0
    finally:
        con.close()


def test_coverage_by_platform_marks_youtube_own_queue(tmp_path):
    from tuber.analysis import coverage

    con = _db(tmp_path, "cov.db")
    try:
        _source(con, 1)
        _content(con, 1, NOW - timedelta(hours=2))
        con.execute(
            "INSERT INTO metric_schedule(content_id, platform, stage, due_at,"
            " done_at, attempt, status) VALUES (1,'telegram','1h',?,?,1,'measured')",
            (_iso(NOW), _iso(NOW)))
        con.commit()
        data = coverage.coverage_by_platform(con)
        assert data["telegram"]["queue"] == "metric_schedule"
        assert data["telegram"]["measured"] == 1
        assert data["youtube"]["queue"] == "own_slots"
        assert "вне единой очереди" in data["youtube"]["note"]
        lines = coverage.format_coverage(data)
        assert any("вне единой очереди" in line for line in lines)
    finally:
        con.close()
