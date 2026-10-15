"""ТЗ-43: очередь замеров 1/6/24/72 ч для X и Telegram (контур 1 «Скорость»).

Детерминированные проверки без сети и без боевой базы:

* стадии планируются от ``published_at`` и повторный прогон не плодит строк;
* ``bucket``/``delta_*`` заполняются у X и Telegram на фикстуре;
* отрицательная дельта не пишется, снимок помечается ``is_anomaly``;
* ``share_6h``/``accel`` считаются на числах, включая деление на ноль;
* медиана автора берётся НА ТОМ ЖЕ возрасте поста (стадия 6h), и посты другого
  возраста её не сдвигают;
* пустой день — молчание;
* CLI-гейт и сухой прогон.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tuber import cli
from tuber.core import db, metrics, schema

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _db(tmp_path):
    path = str(tmp_path / "mt.db")
    con = db.connect(path)
    schema.init_schema(con)
    return path, con


def _source(con, sid, platform, handle="ch"):
    con.execute(
        "INSERT INTO source(id, platform, handle, status) VALUES (?,?,?, 'active')",
        (sid, platform, f"{handle}{sid}"))


def _content(con, cid, platform, sid, published: datetime, external_id=None):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at)"
        " VALUES (?,?,?,?,?)",
        (cid, platform, sid, external_id or f"{platform}-{cid}", _iso(published)))


def _snap(con, cid, platform, captured: datetime, **fields):
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    con.execute(
        f"INSERT INTO metric_snapshot(content_id, platform, captured_at, {cols})"
        f" VALUES (?,?,?,{marks})",
        (cid, platform, _iso(captured), *fields.values()))


# ---------------------------------------------------------------------------
# Планирование
# ---------------------------------------------------------------------------

def test_plan_stages_from_published_at(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pub = NOW - timedelta(hours=10)
        _content(con, 1, "telegram", 1, pub)
        stats = metrics.plan_stages(con, now=NOW)
        assert stats["inserted"] == 4
        rows = {r["stage"]: r["due_at"] for r in con.execute(
            "SELECT stage, due_at FROM metric_schedule ORDER BY stage")}
        assert rows["1h"] == _iso(pub + timedelta(hours=1))
        assert rows["6h"] == _iso(pub + timedelta(hours=6))
        assert rows["24h"] == _iso(pub + timedelta(hours=24))
        assert rows["72h"] == _iso(pub + timedelta(hours=72))
        # К добору — только наступившие (1h, 6h): 10 ч назад опубликован.
        assert stats["due_now"] == 2
    finally:
        con.close()


def test_plan_stages_is_idempotent(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "x")
        _content(con, 1, "x", 1, NOW - timedelta(hours=2))
        first = metrics.plan_stages(con, now=NOW)
        second = metrics.plan_stages(con, now=NOW)
        assert first["inserted"] == 4
        assert second["inserted"] == 0 and second["existing"] == 4
        total = con.execute("SELECT COUNT(*) FROM metric_schedule").fetchone()[0]
        assert total == 4
    finally:
        con.close()


def test_stage_for_age_boundaries():
    assert metrics.stage_for_age(0.5) == "1h"
    assert metrics.stage_for_age(4.0) == "6h"
    assert metrics.stage_for_age(15.0) == "24h"
    assert metrics.stage_for_age(60.0) == "72h"
    assert metrics.stage_for_age(None) is None


# ---------------------------------------------------------------------------
# Дельты и корзина (X и Telegram)
# ---------------------------------------------------------------------------

def test_bucket_and_delta_filled_for_telegram(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pub = NOW - timedelta(hours=25)
        _content(con, 1, "telegram", 1, pub)
        _snap(con, 1, "telegram", pub + timedelta(minutes=30), views=1000)
        _snap(con, 1, "telegram", pub + timedelta(hours=6), views=5000)
        _snap(con, 1, "telegram", pub + timedelta(hours=24), views=9000)
        stats = metrics.backfill_snapshots(con, platforms=("telegram",))
        assert stats["snapshots"] == 3
        rows = list(con.execute(
            "SELECT bucket, delta_views, interval_quality FROM metric_snapshot"
            " ORDER BY captured_at"))
        assert [r["bucket"] for r in rows] == ["1h", "6h", "24h"]
        assert rows[0]["delta_views"] is None and rows[0]["interval_quality"] == "first"
        assert rows[1]["delta_views"] == 4000
        assert rows[2]["delta_views"] == 4000
    finally:
        con.close()


def test_bucket_and_delta_filled_for_x_likes(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "x")
        pub = NOW - timedelta(hours=7)
        _content(con, 1, "x", 1, pub)
        _snap(con, 1, "x", pub + timedelta(minutes=40), likes=100, views=None)
        _snap(con, 1, "x", pub + timedelta(hours=6), likes=250, views=None)
        metrics.backfill_snapshots(con, platforms=("x",))
        rows = list(con.execute(
            "SELECT bucket, delta_likes, interval_quality FROM metric_snapshot"
            " ORDER BY captured_at"))
        assert rows[0]["bucket"] == "1h"
        assert rows[1]["bucket"] == "6h"
        assert rows[1]["delta_likes"] == 150
        assert rows[1]["interval_quality"] in ("ok", "short")
    finally:
        con.close()


def test_negative_delta_not_written_and_marked_anomaly(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pub = NOW - timedelta(hours=8)
        _content(con, 1, "telegram", 1, pub)
        _snap(con, 1, "telegram", pub + timedelta(hours=1), views=500)
        _snap(con, 1, "telegram", pub + timedelta(hours=6), views=400)
        metrics.backfill_snapshots(con, platforms=("telegram",))
        last = con.execute(
            "SELECT delta_views, is_anomaly FROM metric_snapshot"
            " ORDER BY captured_at DESC LIMIT 1").fetchone()
        assert last["delta_views"] is None, "отрицательная дельта не должна писаться"
        assert last["is_anomaly"] == 1
    finally:
        con.close()


def test_backfill_is_idempotent(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pub = NOW - timedelta(hours=3)
        _content(con, 1, "telegram", 1, pub)
        _snap(con, 1, "telegram", pub + timedelta(minutes=30), views=100)
        first = metrics.backfill_snapshots(con, platforms=("telegram",))
        second = metrics.backfill_snapshots(con, platforms=("telegram",))
        assert first["snapshots"] == 1
        assert second["snapshots"] == 0, "повторный бэкфилл ничего не находит (bucket уже есть)"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# v / share_6h / accel
# ---------------------------------------------------------------------------

def _early_fixture(con, *, cid, sid, m1, m6, m24, platform="telegram",
                   published=None, ext=None):
    published = published or (NOW - timedelta(hours=25))
    _content(con, cid, platform, sid, published, external_id=ext)
    _snap(con, cid, platform, published + timedelta(hours=1), views=m1)
    _snap(con, cid, platform, published + timedelta(hours=6), views=m6)
    _snap(con, cid, platform, published + timedelta(hours=24), views=m24)


def test_share_and_accel_numbers(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        _early_fixture(con, cid=1, sid=1, m1=100, m6=400, m24=900)
        metrics.backfill_snapshots(con, platforms=("telegram",))
        data = metrics.build_early(con, now=NOW, limit=5)
        assert len(data["items"]) == 1
        it = data["items"][0]
        assert abs(it["v"] - (900 - 100) / 23) < 1e-9
        assert abs(it["share_6h"] - 300 / 800) < 1e-9
        # accel = ((900-400)/18) / ((400-100)/5)
        assert abs(it["accel"] - ((500 / 18) / 60.0)) < 1e-9
    finally:
        con.close()


def test_share_and_accel_division_by_zero(tmp_path):
    """m24 == m1 → base 0: share_6h честно None, accel тоже None (ТЗ-53).

    Раньше делитель подпирался EPSILON, и выдача печатала 1e11. Теперь
    нулевой ранний темп означает «accel не определён».
    """
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        _early_fixture(con, cid=1, sid=1, m1=100, m6=100, m24=100)
        metrics.backfill_snapshots(con, platforms=("telegram",))
        data = metrics.build_early(con, now=NOW, limit=5)
        it = data["items"][0]
        assert it["share_6h"] is None
        assert it["accel"] is None
    finally:
        con.close()


def test_author_median_is_same_age_only(tmp_path):
    """Медиана автора считается на стадии 6h; снимок 24h её не сдвигает."""
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        # Три поста автора на 6-м часу: 10, 20, 30 → медиана 20.
        for i, val in enumerate((10, 20, 30), start=1):
            pub = NOW - timedelta(hours=30 + i)
            _content(con, i, "telegram", 1, pub)
            _snap(con, i, "telegram", pub + timedelta(hours=6), views=val)
        # Целевой пост: m6=100 ≥ 3×20 → разгоняется. Снимок 24h у него = 999999
        # не должен попасть в медиану стадии 6h.
        _early_fixture(con, cid=10, sid=1, m1=5, m6=100, m24=999999)
        metrics.backfill_snapshots(con, platforms=("telegram",))
        data = metrics.build_early(con, now=NOW, limit=5)
        target = next(it for it in data["items"] if it["content_id"] == 10)
        assert target["author_median_6h"] == 20
        assert target["grows"] is True
    finally:
        con.close()


def test_empty_day_is_silent(tmp_path):
    path, con = _db(tmp_path)
    try:
        data = metrics.build_early(con, now=NOW, limit=5)
        assert data["items"] == []
        assert metrics.format_early(data) == ""
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Добор просроченных
# ---------------------------------------------------------------------------

def test_sweep_links_existing_snapshot(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pub = NOW - timedelta(hours=7)
        _content(con, 1, "telegram", 1, pub)
        _snap(con, 1, "telegram", pub + timedelta(hours=1), views=1000)
        metrics.plan_stages(con, now=NOW)
        stats = metrics.sweep_overdue(con, now=NOW, fetch=None)
        assert stats["linked"] == 1
        done = con.execute(
            "SELECT done_at FROM metric_schedule WHERE stage='1h'").fetchone()
        assert done["done_at"] == _iso(pub + timedelta(hours=1))
        snap = con.execute(
            "SELECT bucket FROM metric_snapshot WHERE content_id=1").fetchone()
        assert snap["bucket"] == "1h"
    finally:
        con.close()


def test_sweep_records_fresh_with_fetcher(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram", handle="ch")
        pub = NOW - timedelta(hours=6)
        _content(con, 1, "telegram", 1, pub, external_id="ch/777")
        metrics.plan_stages(con, now=NOW)
        calls = []

        def fetch(platform, external_id, handle):
            calls.append((platform, external_id, handle))
            return {"views": 1000 if len(calls) == 1 else 5000}

        # 1-я стадия (1h) — ровно сейчас относительно публикации? Сдвинем now.
        s1 = metrics.sweep_overdue(con, now=pub + timedelta(hours=1), fetch=fetch)
        assert s1["recorded"] == 1
        s2 = metrics.sweep_overdue(con, now=pub + timedelta(hours=6), fetch=fetch)
        assert s2["recorded"] == 1
        last = con.execute(
            "SELECT bucket, delta_views FROM metric_snapshot"
            " ORDER BY captured_at DESC LIMIT 1").fetchone()
        assert last["bucket"] == "6h"
        assert last["delta_views"] == 4000
        assert ("telegram", "ch/777", "ch1") in calls
    finally:
        con.close()


def test_sweep_closes_stale_without_data(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pub = NOW - timedelta(hours=200)
        _content(con, 1, "telegram", 1, pub)
        metrics.plan_stages(con, now=NOW, lookback_hours=240)
        stats = metrics.sweep_overdue(con, now=NOW, fetch=None)
        assert stats["missed"] == 4, stats
        left = con.execute(
            "SELECT COUNT(*) FROM metric_schedule WHERE done_at IS NULL").fetchone()[0]
        assert left == 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_plan_and_early(tmp_path, capsys):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        _early_fixture(con, cid=1, sid=1, m1=100, m6=400, m24=900)
        metrics.backfill_snapshots(con, platforms=("telegram",))
    finally:
        con.close()
    rc = cli.main(["metrics", "plan", "--db", path, "--now", _iso(NOW)])
    out = capsys.readouterr().out
    assert rc == 0 and "план стадий" in out
    rc = cli.main(["metrics", "early", "--db", path, "--now", _iso(NOW)])
    out = capsys.readouterr().out
    assert rc == 0 and "Раннее" in out and "share_6h" in out


def test_cli_early_json_and_empty(tmp_path, capsys):
    path, con = _db(tmp_path)
    con.close()
    rc = cli.main(["metrics", "early", "--db", path, "--now", _iso(NOW), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["items"] == []
    rc = cli.main(["metrics", "early", "--db", path, "--now", _iso(NOW)])
    out = capsys.readouterr().out
    assert rc == 0 and out == ""


def test_cli_backfill_dry_run_writes_nothing(tmp_path, capsys):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pub = NOW - timedelta(hours=2)
        _content(con, 1, "telegram", 1, pub)
        _snap(con, 1, "telegram", pub + timedelta(minutes=30), views=100)
    finally:
        con.close()
    rc = cli.main(["metrics", "backfill", "--db", path, "--dry-run"])
    assert rc == 0
    con = db.connect(path)
    try:
        bucket = con.execute("SELECT bucket FROM metric_snapshot").fetchone()[0]
        assert bucket is None
    finally:
        con.close()
