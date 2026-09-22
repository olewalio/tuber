"""ТЗ-4 2.1/П16: аварийный дублёр x_ssr включается при деградации Nitter."""
from tuber.platforms.x import channels, collect, config, store as db
from tuber.platforms.x.broker import NoLiveInstance
from tests.x.mocking import FakeSsrTransport, VClock


class FailingNitter:
    def fetch_feed(self, handle, cursor=None, priority="collect", force=False):
        raise NoLiveInstance("nitter down")


class StubRouter:
    """Роутер-заглушка: Nitter деградировал, SSR отдаёт посты."""

    def __init__(self, posts):
        self._posts = posts
        self.ssr_calls = []

    def nitter_degraded(self):
        return True

    def fetch_ssr(self, handle):
        self.ssr_calls.append(handle)
        return self._posts

    def fetch_feed(self, handle, cursor=None, priority="collect", force=False):
        raise NoLiveInstance("nitter down")


def _account(con, handle, tier="A"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,?,'active')",
                (handle, tier))
    con.commit()


def test_parse_ssr_profile_extracts_ids_and_dates():
    from tests.x.mocking import make_ssr_html
    posts = channels.parse_ssr_profile(make_ssr_html(), "OpenAI")
    assert len(posts) == 6
    assert all(p["tweet_id"].isdigit() for p in posts)
    assert all(p["published_at_utc"] for p in posts)
    assert all(p["published_src"] == "snowflake" for p in posts)


def test_collect_falls_back_to_ssr(db_path):
    con = db.connect(db_path)
    _account(con, "openai")
    from datetime import datetime, timezone
    from tests.x.mocking import snowflake_id
    dt = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    posts = [{"tweet_id": snowflake_id(dt, seq=i), "owner_handle": "openai",
              "published_at_utc": "2026-09-14T10:00:00", "published_src": "snowflake",
              "text": None, "links": [], "mentions": [], "hashtags": [],
              "is_retweet": 0, "is_quote": 0, "is_reply": 0, "media_kind": None,
              "cursor_next": None} for i in range(3)]
    router = StubRouter(posts)
    run_id = db.start_run(con, "collect:A")
    summary = collect.collect_tier(con, FailingNitter(), "A", run_id=run_id,
                                   router=router)
    assert summary["ssr_used"] == 1
    assert summary["accounts_ok"] == 1 and summary["accounts_fail"] == 0
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 3
    assert router.ssr_calls == ["openai"]
    con.close()


def test_collect_without_router_still_fails_cleanly(db_path):
    con = db.connect(db_path)
    _account(con, "openai")
    run_id = db.start_run(con, "collect:A")
    summary = collect.collect_tier(con, FailingNitter(), "A", run_id=run_id)
    assert summary["ssr_used"] == 0
    assert summary["accounts_fail"] == 1
    con.close()


def test_metrics_daily_ssr_used_counter(db_path):
    """П16: `metrics_daily.ssr_used` > 0 после захода в дублёр."""
    con = db.connect(db_path)
    clock = VClock()
    ssr_tr = FakeSsrTransport(clock=clock)
    ssr_tr.set("openai")
    router = channels.ChannelRouter(db_path=db_path, instances=["https://one.test"],
                                    clock=clock, sleeper=clock.advance,
                                    ssr_transport=ssr_tr)
    posts = router.fetch_ssr("openai")
    assert posts and router.ssr_used == 1
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('openai','A','active')")
    con.commit()
    from tuber.platforms.x import collect as _c
    _c.update_metrics_daily(con)
    row = con.execute("SELECT ssr_used FROM metrics_daily ORDER BY day DESC LIMIT 1"
                      ).fetchone()
    assert row["ssr_used"] >= 1
    router.close()
    con.close()


# =========================================================================== #
# ТЗ-Tuber ч.3: флаг резерва снимается по факту (Nitter восстановился)
# =========================================================================== #
def _age_reserve(con, hours=7):
    from datetime import datetime, timedelta, timezone
    old = db.iso(datetime.now(timezone.utc) - timedelta(hours=hours))
    db.mark_reserve_active(con, when=old)
    return old


def test_reserve_flag_cleared_when_nitter_recovers(db_path):
    """Резерв был активен, Nitter восстановился → флаг снят, ALERT не печатается."""
    from tuber.platforms.x import health
    con = db.connect(db_path)
    _age_reserve(con)
    assert db.reserve_active_since(con) is not None
    # Успешный фид Nitter за последние 30 минут (status=200, items>0).
    con.execute("INSERT INTO requests (host, ts, kind, status, items)"
                " VALUES ('nitter.jaydenha.uk', ?, 'feed', 200, 20)",
                (db.utcnow_iso(),))
    con.commit()
    res = health.check_reserve_active(con)
    assert res["alert"] is False, res
    assert "Nitter отдаёт фиды" in res["msg"]
    assert db.reserve_active_since(con) is None, "флаг reserve_since не снят"
    con.close()


def test_reserve_cleared_when_nitter_alive_and_no_ssr_calls(db_path):
    """Ложное срабатывание: живой Nitter и нет обращений к x_ssr → alert=False."""
    from tuber.platforms.x import health
    con = db.connect(db_path)
    _age_reserve(con)
    # Живое зеркало Nitter, обращений к x_ssr за окно нет вовсе.
    con.execute("INSERT INTO instances (host, healthy) VALUES ('nitter.live', 1)")
    con.commit()
    res = health.check_reserve_active(con)
    assert res["alert"] is False, res
    assert db.reserve_active_since(con) is None
    con.close()


def test_reserve_alert_stays_when_no_recovery_evidence(db_path):
    """Без восстановления Nitter и без живых зеркал ALERT сохраняется."""
    from tuber.platforms.x import health
    con = db.connect(db_path)
    _age_reserve(con, hours=7)
    res = health.check_reserve_active(con)
    assert res["alert"] is True
    assert "7" in res["msg"]
    assert db.reserve_active_since(con) is not None
    con.close()


def test_reserve_recent_ssr_call_keeps_alert(db_path):
    """Живое зеркало есть, но резерв только что вызывали → считаем активным."""
    from tuber.platforms.x import health
    con = db.connect(db_path)
    _age_reserve(con, hours=7)
    con.execute("INSERT INTO instances (host, healthy) VALUES ('nitter.live', 1)")
    con.execute("INSERT INTO requests (host, ts, kind, status, items)"
                " VALUES ('x.com', ?, 'x_ssr', 200, 5)", (db.utcnow_iso(),))
    con.commit()
    res = health.check_reserve_active(con)
    assert res["alert"] is True, res
    assert db.reserve_active_since(con) is not None
    con.close()
