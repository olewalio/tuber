"""Р7.3 — health-проверка, cooldown 429, блокировка 403/451."""
from tuber.platforms.x import config, store as db
from tests.x.mocking import make_feed


def test_http200_with_zero_items_is_not_alive(make_broker):
    b = make_broker()
    b._fake_transport.set_health(status=200, body=make_feed(0))
    res = b.check_instance("https://one.test")
    assert res["status"] == 200
    assert res["items"] == 0
    assert res["healthy"] is False
    row = b._con.execute("SELECT * FROM instances WHERE host='https://one.test'").fetchone()
    assert row["healthy"] == 0 and row["rss_ok"] == 0
    assert "заглушка" in (row["last_error"] or "")


def test_http200_with_20_items_is_alive(make_broker):
    b = make_broker()
    b._fake_transport.set_health(status=200, body=make_feed(20))
    res = b.check_instance("https://one.test")
    assert res["healthy"] is True and res["items"] == 20


def test_http200_with_14_items_is_not_alive(make_broker):
    """Порог именно «>= 15»."""
    b = make_broker()
    b._fake_transport.set_health(status=200, body=make_feed(14))
    assert b.check_instance("https://one.test")["healthy"] is False


def test_429_gives_cooldown(make_broker):
    b = make_broker()
    b._fake_transport.set_health(status=429)
    res = b.check_instance("https://one.test")
    assert res["healthy"] is False
    st = b.stats()["https://one.test"]
    assert st["cooldown_sec"] > 0
    row = b._con.execute("SELECT * FROM instances WHERE host='https://one.test'").fetchone()
    assert row["cooldown_until"] is not None
    # вторая 429 подряд -> cooldown 900 с
    b.check_instance("https://one.test")
    st2 = b.stats()["https://one.test"]
    assert st2["cooldown_sec"] >= config.COOLDOWN_429_HARD_SEC - 5
    warn = b._con.execute(
        "SELECT COUNT(*) FROM run_log WHERE msg LIKE '%429%'").fetchone()[0]
    assert warn >= 2


def test_403_blocks_for_a_day(make_broker):
    b = make_broker()
    b._fake_transport.set_health(status=403)
    res = b.check_instance("https://one.test")
    assert res["healthy"] is False
    st = b.stats()["https://one.test"]
    assert st["blocked"] is True
    assert st["cooldown_sec"] >= config.COOLDOWN_403_SEC - 5
    row = b._con.execute("SELECT * FROM instances WHERE host='https://one.test'").fetchone()
    assert row["blocked"] == 1


def test_451_blocks_for_a_day(make_broker):
    b = make_broker()
    b._fake_transport.set_health(status=451)
    b.check_instance("https://one.test")
    assert b.stats()["https://one.test"]["cooldown_sec"] >= config.COOLDOWN_403_SEC - 5


def test_no_live_instance_raises(make_broker):
    """Р3.5: если живых нет — не долбим сеть, возвращаем NoLiveInstance."""
    from tuber.platforms.x.broker import NoLiveInstance
    b = make_broker(instances=["https://one.test", "https://two.test"])
    tr = b._fake_transport
    tr.set_health(status=403)
    try:
        b.fetch_feed("acct")
        raised = None
    except NoLiveInstance as e:
        raised = e
    assert raised is not None
    assert tr.calls, "запрос всё же ушёл"
    n_health = sum(1 for u, _t, _ts in tr.calls if u.endswith("/nasa/rss"))
    assert n_health <= 2, "брокер долбит сеть при отсутствии живых инстансов"


def test_health_check_ttl(make_broker):
    """Р3.4: проверка не чаще одного раза в 10 минут на инстанс."""
    b = make_broker()
    tr = b._fake_transport
    assert b._ensure_healthy("https://one.test") is True
    assert b._ensure_healthy("https://one.test") is True
    n = sum(1 for u, _t, _ts in tr.calls if u.endswith("/nasa/rss"))
    assert n == 1, f"health-проверок {n}, ожидалась 1 в пределах TTL"
    b._vclock.advance(config.HEALTH_TTL_SEC + 1)
    b._ensure_healthy("https://one.test")
    n = sum(1 for u, _t, _ts in tr.calls if u.endswith("/nasa/rss"))
    assert n == 2, "после истечения TTL проверка не повторилась"


def test_daily_cap_excludes_instance(make_broker):
    """Р3.3: суточный потолок исключает инстанс до конца суток + WARN в run_log."""
    b = make_broker()
    b._daily_cap = 3
    b._fake_transport.set_health(status=200, body=make_feed(20))
    assert b._ensure_healthy("https://one.test")
    for i in range(3):
        try:
            b.fetch_feed(f"cap{i}", force=True)   # health + fetch = 2, 3-й упирается
        except Exception:
            pass
    assert b.stats()["https://one.test"]["over_budget"] is True
    warn = b._con.execute(
        "SELECT COUNT(*) FROM run_log WHERE msg LIKE '%потолок%'").fetchone()[0]
    assert warn >= 1


def test_requests_table_logged(make_broker):
    """Р3.3: каждый запрос пишется в requests."""
    b = make_broker()
    b.fetch_feed("acct")
    rows = b._con.execute("SELECT * FROM requests ORDER BY id").fetchall()
    kinds = [r["kind"] for r in rows]
    assert "health" in kinds and "feed" in kinds
    for r in rows:
        assert r["host"] and r["url"] and r["status"] is not None
        assert r["latency_ms"] is not None


def test_priority_order(make_broker):
    """Р3.7: при перегрузке старшие приоритеты обслуживаются первыми."""
    import heapq
    from tuber.platforms.x import config as cfg
    from tuber.platforms.x.broker import _Job
    b = make_broker()
    assert cfg.PRIORITIES["critical"] < cfg.PRIORITIES["collect"] \
        < cfg.PRIORITIES["discover"] < cfg.PRIORITIES["backfill"]

    now = b._clock()
    jobs = []
    for key, prio in (("low", "backfill"), ("high", "critical"), ("mid", "collect")):
        j = _Job(priority=cfg.PRIORITIES[prio], not_before=now, seq=b._next_seq(),
                 kind="feed", key=key)
        jobs.append(j)
        heapq.heappush(b._pending, j)
    picked, wait = b._select(now)
    assert wait == 0 and picked.key == "high"
    b._pending.remove(picked)
    picked2, _ = b._select(now)
    assert picked2.key == "mid"
    b._pending.remove(picked2)
    picked3, _ = b._select(now)
    assert picked3.key == "low"

    # неготовый старший не блокирует готового младшего
    future = _Job(priority=cfg.PRIORITIES["critical"], not_before=now + 30,
                  seq=b._next_seq(), kind="feed", key="future-high")
    heapq.heappush(b._pending, future)
    picked4, wait4 = b._select(now)
    assert picked4.key == "low" and wait4 == 0
    b._pending.remove(picked4)
    picked5, wait5 = b._select(now)
    assert picked5.key == "future-high" and wait5 > 0
