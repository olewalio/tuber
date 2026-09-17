"""ТЗ-10: аварийный резерв включается по факту отказов сбора, без потери аккаунтов.

Все проверки — без сети: транспорт Nitter и резерва подменяется (П7).
Сценарии соответствуют приёмке П1–П6:

  П1 Nitter здоров               — резерв не используется;
  П2 Nitter недоступен с начала  — КАЖДЫЙ аккаунт через резерв, fail=0, потерь 0;
  П3 отказ только по одному      — через резерв ровно один аккаунт;
  П4 404 и пустая лента          — счётчик отказов не растёт, резерв не включается;
  П5 успешный фид                — счётчик обнулён, деградация снята, резерва нет;
  П6 потолок XSSR_MAX_PER_RUN    — второй аккаунт в резерв не идёт.
"""
import urllib.parse
from datetime import datetime, timedelta, timezone

from tuber.platforms.x import channels, collect, config, store as db, health
from tuber.platforms.x.broker import NitterBroker
from tests.x.mocking import FakeSsrTransport, VClock, make_feed


class NitterTransport:
    """Подменный транспорт Nitter: health всегда жив, фиды — по правилам.

    `modes`: handle -> "ok" | "dead" | "empty" | "404". По умолчанию "dead".
    """

    def __init__(self, clock=None, modes=None):
        self.clock = clock or VClock()
        self.modes = dict(modes or {})
        self.calls = []

    def set_mode(self, handle, mode):
        self.modes[handle.lower()] = mode

    def __call__(self, url, timeout):
        self.calls.append((url, timeout, self.clock()))
        path = urllib.parse.urlparse(url).path
        if path.endswith("/nasa/rss"):
            return 200, {}, make_feed(20)
        handle = path.strip("/").split("/")[0].lower()
        mode = self.modes.get(handle, "dead")
        if mode == "ok":
            return 200, {}, make_feed(20, handle=handle)
        if mode == "empty":
            return 200, {}, make_feed(0, handle=handle)
        if mode == "404":
            return 404, {}, "<html>not found</html>"
        return 0, {}, "__transport_error__:ConnectionRefusedError: refused"

    def feed_calls(self, handle=None):
        out = []
        for url, _t, _ts in self.calls:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/nasa/rss"):
                continue
            h = path.strip("/").split("/")[0].lower()
            if handle is None or h == handle.lower():
                out.append(url)
        return out


def _account(con, handle, tier="A"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,?,'active')",
                (handle, tier))
    con.commit()


def _router(db_path, nitter_tr, ssr_tr, instances=None, clock=None):
    return channels.ChannelRouter(
        db_path=db_path, instances=instances or ["https://one.test"],
        clock=clock or nitter_tr.clock, sleeper=(clock or nitter_tr.clock).advance,
        nitter_transport=nitter_tr, ssr_transport=ssr_tr)


def _ssr(db_path, handles, clock=None):
    tr = FakeSsrTransport(clock=clock or VClock())
    for h in handles:
        tr.set(h)
    return tr


# ======================================================================= П1
def test_p1_healthy_nitter_never_uses_reserve(db_path):
    con = db.connect(db_path)
    _account(con, "openai")
    _account(con, "anthropic")
    clock = VClock()
    nitter = NitterTransport(clock=clock, modes={"openai": "ok", "anthropic": "ok"})
    ssr = _ssr(db_path, ["openai", "anthropic"], clock=clock)
    router = _router(db_path, nitter, ssr, clock=clock)
    try:
        rid = db.start_run(con, "collect:A")
        s = collect.collect_tier(con, router.nitter, "A", run_id=rid, router=router)
        assert s["reserve_used"] == 0 and s["ssr_used"] == 0
        assert s["accounts_ok"] == 2 and s["accounts_lost"] == 0
        assert s["nitter_fails"] == 0
        assert ssr.calls == [], "резерв вызван при здоровом Nitter"
        assert router.nitter.all_degraded() is False
        assert db.reserve_active_since(con) is None
    finally:
        router.close()
        con.close()


# ======================================================================= П2
def test_p2_nitter_dead_every_account_via_reserve(db_path):
    con = db.connect(db_path)
    _account(con, "openai")
    _account(con, "anthropic")
    _account(con, "justinsuntron")
    clock = VClock()
    nitter = NitterTransport(clock=clock)          # все фиды — отказ
    ssr = _ssr(db_path, ["openai", "anthropic", "justinsuntron"], clock=clock)
    router = _router(db_path, nitter, ssr, clock=clock)
    try:
        rid = db.start_run(con, "collect:A")
        s = collect.collect_tier(con, router.nitter, "A", run_id=rid, router=router)
        assert s["accounts_total"] == 3
        assert s["reserve_used"] == 3, s
        assert s["accounts_fail"] == 0 and s["accounts_lost"] == 0, s
        assert s["accounts_ok"] == 3
        assert s["nitter_fails"] >= config.MAX_JOB_ATTEMPTS
        assert "деградировал" in s["reserve_reason"], s["reserve_reason"]
        assert len(ssr.calls) == 3
        assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] > 0
        # отметка «резерв активен» появилась (для сторожа)
        assert db.reserve_active_since(con) is not None
    finally:
        router.close()
        con.close()


# ======================================================================= П3
def test_p3_single_account_failure_uses_reserve_only_for_it(db_path):
    con = db.connect(db_path)
    _account(con, "good_one")
    _account(con, "bad_one")
    _account(con, "good_two")
    clock = VClock()
    nitter = NitterTransport(clock=clock, modes={"good_one": "ok", "good_two": "ok",
                                                 "bad_one": "dead"})
    ssr = _ssr(db_path, ["bad_one"], clock=clock)
    router = _router(db_path, nitter, ssr, clock=clock)
    try:
        rid = db.start_run(con, "collect:A")
        s = collect.collect_tier(con, router.nitter, "A", run_id=rid, router=router)
        assert s["reserve_used"] == 1, s
        assert s["accounts_ok"] == 3 and s["accounts_lost"] == 0
        reserved = [c[0] for c in ssr.calls]
        assert len(reserved) == 1 and "bad_one" in reserved[0], reserved
        # остальные два собраны через Nitter (фиды к ним ходили)
        assert nitter.feed_calls("good_one") and nitter.feed_calls("good_two")
        assert nitter.feed_calls("bad_one"), "Nitter не был первым для bad_one"
    finally:
        router.close()
        con.close()


# ======================================================================= П4
def test_p4_404_and_empty_feed_are_not_nitter_failures(db_path):
    con = db.connect(db_path)
    _account(con, "missing")
    _account(con, "silent")
    clock = VClock()
    nitter = NitterTransport(clock=clock, modes={"missing": "404", "silent": "empty"})
    ssr = FakeSsrTransport(clock=clock)
    router = _router(db_path, nitter, ssr, clock=clock)
    try:
        rid = db.start_run(con, "collect:A")
        s = collect.collect_tier(con, router.nitter, "A", run_id=rid, router=router)
        assert s["reserve_used"] == 0 and ssr.calls == []
        assert s["nitter_fails"] == 0, "404/пустая лента увеличили счётчик отказов"
        assert s["accounts_ok"] == 0 and s["accounts_lost"] == 2
        assert s["reserve_capped"] is False
        row = con.execute("SELECT COALESCE(SUM(collect_fail_streak),0) n FROM instances"
                          ).fetchone()
        assert row["n"] == 0, "счётчик отказов вырос на 404/пустой ленте"
        assert router.nitter.all_degraded() is False
        assert db.reserve_active_since(con) is None
    finally:
        router.close()
        con.close()


# ======================================================================= П5
def test_p5_successful_feed_resets_counter_and_degradation(db_path):
    con = db.connect(db_path)
    _account(con, "first")
    _account(con, "second")
    clock = VClock()
    nitter = NitterTransport(clock=clock)         # сначала всё мертво
    ssr = _ssr(db_path, ["first", "second"], clock=clock)
    router = _router(db_path, nitter, ssr, clock=clock)
    try:
        # прогон 1: Nitter мёртв -> аккаунт через резерв, деградация включена
        rid = db.start_run(con, "collect:A")
        s1 = collect.collect_tier(con, router.nitter, "A", max_accounts=1,
                                  run_id=rid, router=router)
        assert s1["reserve_used"] == 1
        assert db.reserve_active_since(con) is not None
        assert router.nitter.all_degraded() is True

        # Nitter ожил
        nitter.set_mode("first", "ok")
        nitter.set_mode("second", "ok")
        con.execute("UPDATE accounts SET last_success_at=NULL")
        con.commit()
        rid2 = db.start_run(con, "collect:A")
        s2 = collect.collect_tier(con, router.nitter, "A", run_id=rid2, router=router)
        assert s2["reserve_used"] == 0 and s2["accounts_lost"] == 0
        assert router.nitter.all_degraded() is False
        assert db.reserve_active_since(con) is None, "признак деградации не снят"
        row = con.execute("SELECT COALESCE(SUM(collect_fail_streak),0) n FROM instances"
                          ).fetchone()
        assert row["n"] == 0
    finally:
        router.close()
        con.close()


# ======================================================================= П6
def test_p6_reserve_cap_per_run(db_path, monkeypatch):
    con = db.connect(db_path)
    _account(con, "one")
    _account(con, "two")
    monkeypatch.setattr(config, "XSSR_MAX_PER_RUN", 1)
    clock = VClock()
    nitter = NitterTransport(clock=clock)         # всё мертво
    ssr = _ssr(db_path, ["one", "two"], clock=clock)
    router = _router(db_path, nitter, ssr, clock=clock)
    try:
        rid = db.start_run(con, "collect:A")
        s = collect.collect_tier(con, router.nitter, "A", run_id=rid, router=router)
        assert s["reserve_used"] == 1, s
        assert s["reserve_cap"] == 1 and s["reserve_capped"] is True
        assert len(ssr.calls) == 1, "потолок XSSR_MAX_PER_RUN нарушен"
        assert s["accounts_lost"] == 1
    finally:
        router.close()
        con.close()


# --------------------------------------------------- сохранение счётчика в БД
def test_collect_fail_streak_survives_restart(db_path):
    con = db.connect(db_path)
    clock = VClock()
    nitter = NitterTransport(clock=clock)
    b1 = NitterBroker(instances=["https://one.test"], db_path=db_path,
                      transport=nitter, clock=clock, sleeper=clock.advance,
                      async_mode=False)
    for _ in range(config.MAX_JOB_ATTEMPTS):
        try:
            b1.fetch_feed("acct", force=True)
        except Exception:
            pass
    b1.close()
    row = con.execute("SELECT collect_fail_streak FROM instances WHERE host=?",
                      ("https://one.test",)).fetchone()
    assert row["collect_fail_streak"] >= config.MAX_JOB_ATTEMPTS

    # новый процесс видит счётчик из БД (деградация переживает перезапуск)
    b2 = NitterBroker(instances=["https://one.test"], db_path=db_path,
                      transport=nitter, clock=clock, sleeper=clock.advance,
                      async_mode=False)
    try:
        assert b2.all_degraded() is True
    finally:
        b2.close()
        con.close()


def test_transport_5xx_counts_as_failure_404_does_not(db_path):
    """5xx — отказ сбора (счётчик растёт), 404 — нет (ТЗ-10 2.1/П4)."""
    con = db.connect(db_path)
    clock = VClock()

    class T:
        def __init__(self):
            self.mode = "500"

        def __call__(self, url, timeout):
            if url.endswith("/nasa/rss"):
                return 200, {}, make_feed(20)
            if self.mode == "404":
                return 404, {}, ""
            return 500, {}, "<html>oops</html>"

    t = T()
    b = NitterBroker(instances=["https://one.test"], db_path=db_path,
                     transport=t, clock=clock, sleeper=clock.advance, async_mode=False)
    try:
        try:
            b.fetch_feed("acct", force=True)
        except Exception:
            pass
        n5 = con.execute("SELECT collect_fail_streak FROM instances WHERE host=?",
                         ("https://one.test",)).fetchone()["collect_fail_streak"]
        assert n5 == config.MAX_JOB_ATTEMPTS
        t.mode = "404"
        try:
            b.fetch_feed("acct2", force=True)
        except Exception:
            pass
        n404 = con.execute("SELECT collect_fail_streak FROM instances WHERE host=?",
                           ("https://one.test",)).fetchone()["collect_fail_streak"]
        assert n404 == n5, "404 увеличил счётчик отказов сбора"
    finally:
        b.close()
        con.close()


# --------------------------------------------------------------- миграция (П8)
def test_migration_v6_columns_and_idempotent(db_path):
    con = db.connect(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(instances)")}
    assert {"collect_fail_streak", "reserve_since"} <= cols
    # Версия схемы живёт в ядре (schema_meta), а не в PRAGMA user_version;
    # legacy-номер версии сохранён в API адаптера (его читают перенесённые тесты).
    from tuber.core import schema as core_schema
    v1 = con.execute("SELECT value FROM main.schema_meta WHERE key='version'").fetchone()[0]
    assert v1 == core_schema.SCHEMA_VERSION
    assert db.SCHEMA_VERSION >= 6
    con.close()
    # повторный init идемпотентен
    con2 = db.init_db(db_path)
    con3 = db.init_db(db_path)
    assert con3.execute(
        "SELECT value FROM main.schema_meta WHERE key='version'").fetchone()[0] == v1
    cols2 = {r[1] for r in con3.execute("PRAGMA table_info(instances)")}
    assert {"collect_fail_streak", "reserve_since"} <= cols2
    con2.close()
    con3.close()


# --------------------------------------------------------------- сторож (2.3)
def test_health_reserve_active_alert(con):
    assert health.check_reserve_active(con)["alert"] is False
    old = db.iso(datetime.now(timezone.utc) - timedelta(hours=7))
    db.mark_reserve_active(con, when=old)
    res = health.check_reserve_active(con)
    assert res["alert"] is True and "7" in res["msg"]


def test_health_collect_failures_alert(con):
    assert health.check_collect_failures(con)["alert"] is False
    for _ in range(config.HEALTH_COLLECT_FAIL_PER_HOUR + 1):
        con.execute("INSERT INTO requests (host, ts, kind, status) VALUES ('h',?,?,?)",
                    (db.utcnow_iso(), "feed", 0))
    con.commit()
    res = health.check_collect_failures(con)
    assert res["alert"] is True
    # 404 не считается отказом сбора
    con.execute("DELETE FROM requests")
    for _ in range(config.HEALTH_COLLECT_FAIL_PER_HOUR + 1):
        con.execute("INSERT INTO requests (host, ts, kind, status) VALUES ('h',?,?,?)",
                    (db.utcnow_iso(), "feed", 404))
    con.commit()
    assert health.check_collect_failures(con)["alert"] is False


def test_reserve_pause_at_least_two_seconds():
    assert config.XSSR_MIN_INTERVAL_SEC >= 2.0
