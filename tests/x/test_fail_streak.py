"""Р7.8 — 5 отказов подряд переводят аккаунт в dead, обход других не прерывается."""
from tuber.platforms.x import collect, store as db, registry
from tests.x.mocking import make_feed


def test_fail_streak_marks_dead_and_does_not_stop_others(con, make_broker,
                                                         fixture_feed):
    registry.add_account(con, "good", "C")
    registry.add_account(con, "bad", "C")
    b = make_broker()
    tr = b._fake_transport
    tr.set_feed("good", body=fixture_feed)
    # P0-1: пустая лента больше НЕ отказ аккаунта, поэтому реальный отказ
    # моделируем транспортной ошибкой сервера (500), а не пустым RSS.
    tr.set_feed("bad", status=500, body="server error")

    for i in range(5):
        s = collect.collect_tier(con, b, "C")
        assert s["accounts_ok"] == 1, f"прогон {i}: хороший аккаунт не обойдён"
        assert s["accounts_fail"] == 1
        # отказ не прервал обход остальных: второй аккаунт собран в том же прогоне
        assert con.execute("SELECT COUNT(*) FROM posts p JOIN accounts a"
                           " ON a.id=p.account_id WHERE a.handle='good'"
                           ).fetchone()[0] == 7
        assert s["details"], "нет деталей прогона"
        assert any(d["ok"] and d["handle"] == "good" for d in s["details"])
        assert any((not d["ok"]) and d["handle"] == "bad" for d in s["details"])

    bad = con.execute("SELECT * FROM accounts WHERE handle='bad'").fetchone()
    assert bad["fail_streak"] == 5
    assert bad["status"] == "dead"
    assert bad["last_error"]
    warn = con.execute(
        "SELECT COUNT(*) FROM run_log WHERE level='WARN' AND handle='bad'"
        " AND msg LIKE '5 отказов%'").fetchone()[0]
    assert warn == 1

    # dead-аккаунт больше не обходится
    s = collect.collect_tier(con, b, "C")
    assert s["accounts_total"] == 1
    assert s["details"][0]["handle"] == "good"


def test_fail_streak_resets_on_success(con, make_broker, fixture_feed):
    registry.add_account(con, "flaky", "C")
    b = make_broker()
    tr = b._fake_transport
    # P0-1: реальный транспортный отказ, а не пустая лента.
    tr.set_feed("flaky", status=500, body="server error")
    collect.collect_tier(con, b, "C")
    assert con.execute("SELECT fail_streak FROM accounts WHERE handle='flaky'"
                       ).fetchone()[0] == 1
    b._cache.clear()
    tr.set_feed("flaky", body=fixture_feed)
    collect.collect_tier(con, b, "C")
    row = con.execute("SELECT * FROM accounts WHERE handle='flaky'").fetchone()
    assert row["fail_streak"] == 0
    assert row["status"] == "candidate"
    assert row["last_success_at"] is not None


def test_empty_feed_never_marks_account_dead(con, fixture_feed):
    """P0-1 (правка 1): заглушка зеркала (200 + 0 item) НЕ убивает аккаунт.

    Брокер отдаёт пустую ленту (EmptyFeedError) — как зеркало-заглушка.
    Даже после многих прогонов fail_streak/status/last_error не меняются:
    транспортный симптом зеркала не списывается на аккаунт.
    """
    from tuber.platforms.x.broker import EmptyFeedError

    class EmptyFeedBroker:
        def fetch_feed(self, handle, **kwargs):
            raise EmptyFeedError(f"лента пуста (0 item): {handle}")

    registry.add_account(con, "silent", "C")
    broker = EmptyFeedBroker()

    for _ in range(7):
        s = collect.collect_tier(con, broker, "C")
        assert s["empty_feeds"] == 1, s
        assert any(d.get("empty_feed") for d in s["details"]), s["details"]
        assert s["details"][0].get("nitter_failure") is False

    row = con.execute("SELECT * FROM accounts WHERE handle='silent'").fetchone()
    assert row["status"] != "dead", "пустая лента убила аккаунт"
    assert (row["fail_streak"] or 0) == 0, row["fail_streak"]
    assert row["last_attempt_at"] is not None, "last_attempt_at не обновлён"
    assert row["last_error"] is None


def test_broker_error_does_not_break_run(con, make_broker):
    """Ошибка брокера (нет живых инстансов) не прерывает обход."""
    registry.add_account(con, "a1", "C")
    registry.add_account(con, "a2", "C")
    b = make_broker()
    b._fake_transport.set_health(status=403)   # живых нет
    s = collect.collect_tier(con, b, "C")
    assert s["accounts_ok"] == 0 and s["accounts_fail"] == 2
    assert len(s["details"]) == 2
    assert con.execute("SELECT COUNT(*) FROM accounts WHERE fail_streak=1"
                       ).fetchone()[0] == 2
