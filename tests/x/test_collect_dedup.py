"""Р7.7 — дедупликация: повторный обход 0 новых, posts_upd считается верно."""
from tuber.platforms.x import collect, store as db, registry
from tests.x.mocking import make_feed


def _setup(con, make_broker, fixture_feed, handle="acct"):
    registry.add_account(con, handle, "C")
    b = make_broker()
    b._fake_transport.set_feed(handle, body=fixture_feed,
                               headers={"min-id": "CURSOR-1"})
    return b


def test_collect_dedup(con, make_broker, fixture_feed):
    b = _setup(con, make_broker, fixture_feed)
    s1 = collect.collect_tier(con, b, "C")
    assert s1["accounts_ok"] == 1 and s1["accounts_fail"] == 0
    # в фикстуре 8 item, один без восстанавливаемой даты
    assert s1["posts_new"] == 7
    assert s1["posts_upd"] == 0
    assert s1["skipped_no_date"] == 1
    assert s1["details"][0]["new"] == 7

    s2 = collect.collect_tier(con, b, "C", run_id=db.start_run(con, "collect:C#2"))
    assert s2["posts_new"] == 0, "повторный обход дал новые id"
    assert s2["posts_upd"] == 7, "posts_upd посчитан неверно"
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 7

    runs = con.execute("SELECT * FROM runs ORDER BY id").fetchall()
    assert runs[0]["posts_new"] == 7 and runs[0]["posts_upd"] == 0
    assert runs[1]["posts_new"] == 0 and runs[1]["posts_upd"] == 7
    assert runs[0]["accounts_ok"] == 1 and runs[1]["accounts_ok"] == 1


def test_collect_updates_account_state(con, make_broker, fixture_feed):
    b = _setup(con, make_broker, fixture_feed)
    collect.collect_tier(con, b, "C")
    row = con.execute("SELECT * FROM accounts WHERE handle='acct'").fetchone()
    assert row["last_success_at"] is not None
    assert row["fail_streak"] == 0
    assert row["cursor"] == "CURSOR-1"
    assert row["posts_collected"] == 7
    assert row["posts_per_day"] is not None
    # CV считается только при >= 10 постах (Р4.3.4): в фикстуре их 7
    assert row["cv_interval"] is None
    cur = con.execute("SELECT * FROM cursors WHERE kind='account' AND ref='acct'").fetchone()
    assert cur["cursor"] == "CURSOR-1" and cur["items_total"] > 0


def test_dry_run_writes_nothing(con, make_broker, fixture_feed):
    b = _setup(con, make_broker, fixture_feed)
    s = collect.collect_tier(con, b, "C", dry_run=True)
    assert s["accounts_ok"] == 1
    assert s["posts_new"] == 7          # отчёт
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    row = con.execute("SELECT * FROM accounts WHERE handle='acct'").fetchone()
    assert row["last_success_at"] is None
    assert row["posts_collected"] == 0
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_metrics_daily_written(con, make_broker, fixture_feed):
    b = _setup(con, make_broker, fixture_feed)
    collect.collect_tier(con, b, "C")
    m = con.execute("SELECT * FROM metrics_daily").fetchone()
    assert m is not None
    assert m["posts_ingested"] == 7
    assert m["coverage"] == 1.0 and m["fail_rate"] == 0.0
    assert m["valid_date_ratio"] == 1.0
    assert m["instances_alive"] >= 1


def test_completeness_check_reports_zero_new(con, make_broker, fixture_feed):
    """Р6.1: повторный обход сразу после первого даёт 0 новых id."""
    b = _setup(con, make_broker, fixture_feed)
    s = collect.collect_tier(con, b, "C")
    res = collect.verify_completeness(con, b, s, "C")
    assert res["accounts_checked"] == 1
    assert res["new_ids"] == 0 and res["ok"] is True


def test_completeness_detects_lag(con, make_broker, fixture_feed):
    """Если лента отдала новые id — это фиксируется как отставание."""
    b = _setup(con, make_broker, fixture_feed)
    s = collect.collect_tier(con, b, "C")
    b._fake_transport.set_feed("acct", body=make_feed(20, handle="acct", jitter=1.0))
    res = collect.verify_completeness(con, b, s, "C")
    assert res["new_ids"] == 20 and res["ok"] is False


def test_backfill_pages_without_duplicates(con, make_broker):
    """П6: страницы идут вглубь курсором, дублей нет."""
    handle = "acct"
    registry.add_account(con, handle, "C")
    b = make_broker()
    tr = b._fake_transport
    page1 = make_feed(20, handle=handle, start=_dt("2026-09-14T12:00:00"))
    page2 = make_feed(20, handle=handle, start=_dt("2026-09-01T12:00:00"))
    page3 = make_feed(20, handle=handle, start=_dt("2026-08-01T12:00:00"))
    tr.set_feed(handle, body=page1, headers={"min-id": "C1"})
    tr.cursor_routes[(handle, "C1")] = (200, {"min-id": "C2"}, page2)
    tr.cursor_routes[(handle, "C2")] = (200, {"min-id": "C3"}, page3)
    res = collect.backfill(con, b, handle, pages=3)
    assert res["pages"] == 3
    assert res["new"] == 60
    assert res["unique_posts_total"] == 60
    assert res["upd"] == 0


def test_tier_c_limit_is_one_page(con, make_broker):
    """Р5.2: для TIER-C не более одной страницы."""
    handle = "acct"
    registry.add_account(con, handle, "C")
    b = make_broker()
    b._fake_transport.set_feed(handle, body=make_feed(20, handle=handle),
                               headers={"min-id": "C1"})
    s = collect.collect_tier(con, b, "C")
    assert s["pages"] == 1


def test_max_accounts_limit(con, make_broker, fixture_feed):
    for h in ("a1", "a2", "a3"):
        registry.add_account(con, h, "C")
        make_broker()._fake_transport.set_feed(h, body=fixture_feed)
    b = make_broker()
    for h in ("a1", "a2", "a3"):
        b._fake_transport.set_feed(h, body=fixture_feed)
    s = collect.collect_tier(con, b, "C", max_accounts=2)
    assert s["accounts_total"] == 2 and s["accounts_ok"] == 2


def _dt(value):
    from datetime import datetime, timezone
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
