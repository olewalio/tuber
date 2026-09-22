"""ТЗ-C: рост реестра X — путь (а) в bootstrap, env-пороги, повтор, резерв.

Тесты изолированы: сеть не используется (подменный транспорт), живая БД не
затрагивается (фикстура `con` на временной копии схемы).
"""
import importlib

from tuber.platforms.x import config, registry
from tuber.platforms.x.broker import parse_rss
from tests.x.mocking import make_feed


# --------------------------------------------------------------------- helpers
def _seed_provisional(con, handle, *, mentions=(), ai_density=0.6, posts=12):
    con.execute(
        "INSERT INTO accounts (handle, tier, status, added_by, posts_collected,"
        " ai_density, ai_density_src, provisional_since)"
        " VALUES (?, 'C', 'provisional', 'test', ?, ?, 'heuristic', ?)",
        (handle, posts, ai_density, _now(con)))
    con.commit()
    aid = con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()[0]
    for i, m in enumerate(mentions):
        con.execute(
            "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
            " mentions, text) VALUES (?,?,?,'rss',?,?)",
            (aid, f"{handle}-{i}", "2026-09-01T00:00:00", f'["{m}"]', f"cc @{m}"))
    con.commit()
    return aid


def _now(con):
    from tuber.platforms.x import store as db
    return db.utcnow_iso()


class StubRouter:
    """Роутер-заглушка: Nitter деградировал, x_ssr отдаёт ленту."""

    def __init__(self, posts):
        self._posts = posts
        self.ssr_calls = []

    def nitter_degraded(self):
        return True

    def fetch_ssr(self, handle):
        self.ssr_calls.append(handle)
        return self._posts


# ================================================== 1. путь (а) в bootstrap
def test_promote_path_a_during_bootstrap(con):
    """ТЗ-C Р1: при active < BOOTSTRAP_MIN_ACTIVE provisional промоутится по (а)."""
    for core in ("core1", "core2"):
        con.execute("INSERT INTO accounts (handle, tier, status, added_by)"
                    " VALUES (?, 'A', 'active', 'test')", (core,))
    con.commit()
    cid = {h: con.execute("SELECT id FROM accounts WHERE handle=?", (h,)).fetchone()[0]
           for h in ("core1", "core2")}
    for core in ("core1", "core2"):
        con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc,"
                    " published_src, mentions, text) VALUES (?,?,?,'rss',?,?)",
                    (cid[core], f"{core}-p1", "2026-09-01T00:00:00", '["p1"]', "cc @p1"))
    con.commit()
    _seed_provisional(con, "p1", ai_density=0.6)
    assert registry.bootstrap_phase(con) is True
    promoted = registry.promote_provisional(con)
    assert [(p["handle"], p["by"]) for p in promoted] == [("p1", "core_mentions")], promoted
    assert con.execute("SELECT status FROM accounts WHERE handle='p1'"
                       ).fetchone()[0] == "active"


# ================================================== 2. env-пороги
def test_tenure_days_and_max_verify_from_env(monkeypatch):
    """ТЗ-C Р2: TUBER_X_TENURE_DAYS / TUBER_X_MAX_VERIFY переопределяют config."""
    orig_db = config.DB_PATH
    monkeypatch.setenv("TUBER_X_TENURE_DAYS", "4")
    monkeypatch.setenv("TUBER_X_MAX_VERIFY", "60")
    try:
        importlib.reload(config)
        assert config.TENURE_DAYS == 4
        assert config.DISCOVERY_MAX_VERIFY_DEFAULT == 60
    finally:
        monkeypatch.delenv("TUBER_X_TENURE_DAYS", raising=False)
        monkeypatch.delenv("TUBER_X_MAX_VERIFY", raising=False)
        importlib.reload(config)
        config.DB_PATH = orig_db
    # без переменных — прежние дефолты (откат)
    assert config.TENURE_DAYS == 14
    assert config.DISCOVERY_MAX_VERIFY_DEFAULT == 30


# ================================================== 3. повтор при items_lt_15
def test_verify_retries_then_rejects(con, make_broker):
    """ТЗ-C Р5: 1-я и 2-я неудача -> new/retry; 3-я -> rejected."""
    b = make_broker()
    b._fake_transport.set_feed("cand", body=make_feed(10, handle="cand", jitter=1.0))
    registry.add_account(con, "cand", "C")

    for attempt in (1, 2):
        res = registry.verify_account(con, b, "cand")
        assert res["status"] == "candidate" and res["reason"] == "retry", res
        assert res["verify_attempts"] == attempt
        assert con.execute("SELECT status FROM accounts WHERE handle='cand'"
                           ).fetchone()[0] == "candidate"
        assert con.execute("SELECT validated FROM candidates WHERE handle='cand'"
                           ).fetchone()[0] == "pending"

    res = registry.verify_account(con, b, "cand")
    assert res["status"] == "rejected" and res["reason"] == "items_lt_15", res
    assert con.execute("SELECT status FROM accounts WHERE handle='cand'"
                       ).fetchone()[0] == "rejected"
    assert con.execute("SELECT validated FROM candidates WHERE handle='cand'"
                       ).fetchone()[0] == "reject"


# ================================================== 4. резерв при деградации
def test_verify_uses_reserve_when_nitter_degraded(con, make_broker):
    """ТЗ-C Р4: Nitter отдал < 15 — при деградации лента добирается через x_ssr."""
    b = make_broker()
    # Nitter отдаёт мало записей
    b._fake_transport.set_feed("cand", body=make_feed(5, handle="cand", jitter=1.0))
    ssr_posts = parse_rss(make_feed(20, handle="cand", jitter=1.0), host="https://x.test")
    router = StubRouter(ssr_posts)
    registry.add_account(con, "cand", "C")

    res = registry.verify_account(con, b, "cand", router=router)
    assert router.ssr_calls == ["cand"], router.ssr_calls
    assert res["reserve_used"] is True
    assert res["items"] == 20, res
    assert res["status"] == "provisional", res


def test_verify_no_reserve_when_nitter_healthy(con, make_broker):
    """ТЗ-C Р4: без деградации Nitter резерв не вызывается."""
    b = make_broker()
    b._fake_transport.set_feed("cand", body=make_feed(5, handle="cand", jitter=1.0))
    router = StubRouter(parse_rss(make_feed(20, handle="cand", jitter=1.0),
                                  host="https://x.test"))
    router.nitter_degraded = lambda: False
    registry.add_account(con, "cand", "C")

    res = registry.verify_account(con, b, "cand", router=router)
    assert router.ssr_calls == []
    assert res["reserve_used"] is False
    assert res["status"] == "candidate" and res["reason"] == "retry"
