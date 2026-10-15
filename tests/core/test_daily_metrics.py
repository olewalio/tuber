"""ТЗ-53 ч.2: суточная сводка ``metrics_daily`` для Telegram и YouTube.

Проверяем: строка появляется, повторный прогон не плодит дубли, недоступные
полям источники дают ``NULL`` (а не ``0``), бэкфилл за N суток пишет N строк.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.core import daily_metrics, db, schema, storage

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
DAY = TODAY + " 10:00:00"


def _init(path):
    conn = db.connect(path)
    schema.init_schema(conn)
    return conn


def _seed(conn):
    with db.write_tx(conn):
        sid = storage.upsert_source(conn, "telegram", "@chan", external_id="100")
        storage.upsert_content(
            conn, "telegram", "m1", source_id=sid, kind="post",
            published_at=DAY, first_seen_at=DAY)
        storage.upsert_content(
            conn, "telegram", "m2", source_id=sid, kind="post",
            published_at=DAY, first_seen_at=DAY)
        ysid = storage.upsert_source(conn, "youtube", "@yt", external_id="UC1")
        ycid = storage.upsert_content(
            conn, "youtube", "v1", source_id=ysid, kind="video",
            published_at=DAY, first_seen_at=DAY)
        storage.add_snapshot(conn, ycid, DAY, views=1000, likes=50)
    conn.execute(
        "INSERT INTO run(platform, started_at, ok_count, fail_count, items_new,"
        " items_upd, errors) VALUES ('telegram', ?, 8, 2, 10, 5, 1)", (DAY,))
    conn.commit()


def test_daily_rows_appear_for_telegram_and_youtube(tmp_path):
    conn = _init(str(tmp_path / "d.db"))
    try:
        _seed(conn)
        daily_metrics.update_metrics_daily(conn, platforms=("telegram", "youtube"))
        rows = conn.execute(
            "SELECT platform, day, items_ingested, coverage, likes_median FROM metrics_daily"
            " WHERE day=? ORDER BY platform", (TODAY,)).fetchall()
        assert [r["platform"] for r in rows] == ["telegram", "youtube"]

        tg = next(r for r in rows if r["platform"] == "telegram")
        yt = next(r for r in rows if r["platform"] == "youtube")
        # Telegram: прогоны есть -> coverage посчитан; материалы посчитаны.
        assert tg["items_ingested"] == 2
        assert tg["coverage"] == 0.8
        # YouTube: журнала прогонов нет -> coverage честно NULL, но материалы есть.
        assert yt["items_ingested"] == 1
        assert yt["coverage"] is None
        assert yt["likes_median"] == 50.0
    finally:
        conn.close()


def test_daily_repeat_is_idempotent(tmp_path):
    conn = _init(str(tmp_path / "d.db"))
    try:
        _seed(conn)
        daily_metrics.update_metrics_daily(conn, platforms=("telegram", "youtube"))
        daily_metrics.update_metrics_daily(conn, platforms=("telegram", "youtube"))
        n = conn.execute(
            "SELECT COUNT(*) FROM metrics_daily WHERE day=?", (TODAY,)).fetchone()[0]
        assert n == 2, "повторный прогон не должен плодить строки"
    finally:
        conn.close()


def test_youtube_run_fields_null_not_zero(tmp_path):
    """Нет источника -> NULL, а не 0 (ТЗ-53 ч.2.2)."""
    conn = _init(str(tmp_path / "d.db"))
    try:
        _seed(conn)
        fields = daily_metrics.compute_daily(conn, TODAY, "youtube")
        for key in ("coverage", "fail_rate", "dup_rate", "latency_p95_min",
                    "instances_alive", "cdn_429_count", "synd_429_count", "ssr_used"):
            assert fields[key] is None, f"youtube.{key} должен быть NULL, не 0"
        assert fields["items_ingested"] == 1
    finally:
        conn.close()


def test_daily_backfill_writes_requested_window(tmp_path):
    conn = _init(str(tmp_path / "d.db"))
    try:
        _seed(conn)
        daily_metrics.update_metrics_daily(
            conn, TODAY, platforms=("telegram", "youtube"), days=3)
        days = [r[0] for r in conn.execute(
            "SELECT day FROM metrics_daily WHERE platform='telegram' ORDER BY day")]
        assert len(days) == 3
        assert days[-1] == TODAY
        expected_first = (
            datetime.fromisoformat(TODAY + "T00:00:00").replace(tzinfo=timezone.utc)
            - timedelta(days=2)).strftime("%Y-%m-%d")
        assert days[0] == expected_first
    finally:
        conn.close()
