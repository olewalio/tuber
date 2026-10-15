"""Р7.2 — лимитер: не более 6 запросов / 60 с на инстанс, >= 2 с между запросами."""
from tuber.platforms.x import config
from tuber.platforms.x.broker import RateLimiter
from tests.x.mocking import VClock, make_feed


def _windows_ok(times, limit=6, window=60.0):
    """Ни в одном окне длины window не больше limit запросов."""
    for i, t in enumerate(times):
        n = sum(1 for x in times if t <= x < t + window)
        if n > limit:
            return False, (t, n)
    return True, None


def test_ratelimiter_core():
    clock = VClock()
    rl = RateLimiter(clock=clock)
    times = []
    for _ in range(20):
        d = rl.next_delay()
        assert d >= 0
        clock.advance(d)
        rl.record()
        times.append(clock())
    ok, bad = _windows_ok(times, config.RATE_MAX_REQUESTS, config.RATE_WINDOW_SEC)
    assert ok, f"превышен лимит {config.RATE_MAX_REQUESTS}/{config.RATE_WINDOW_SEC}с: {bad}"
    for a, b in zip(times, times[1:]):
        assert b - a >= config.MIN_REQUEST_INTERVAL_SEC - 1e-9, "интервал < 2 с"


def test_ratelimiter_never_exceeds_in_window():
    clock = VClock()
    rl = RateLimiter(clock=clock)
    times = []
    for _ in range(60):
        clock.advance(rl.next_delay())
        rl.record()
        times.append(clock())
        assert rl.count_in_window() <= config.RATE_MAX_REQUESTS
    ok, bad = _windows_ok(times, config.RATE_MAX_REQUESTS, config.RATE_WINDOW_SEC)
    assert ok, bad


def test_broker_series_20_requests(make_broker):
    """Серия из 20 запросов через брокер с подменённым транспортом."""
    b = make_broker(instances=["https://one.test"])
    tr = b._fake_transport
    for i in range(20):
        posts = b.fetch_feed(f"acct{i}", force=True)
        assert len(posts) == 20
    times = tr.feed_call_times()
    assert len(times) == 20
    ok, bad = _windows_ok(times, config.RATE_MAX_REQUESTS, config.RATE_WINDOW_SEC)
    assert ok, f"брокер превысил лимит: {bad}"
    for a, b2 in zip(times, times[1:]):
        assert b2 - a >= config.MIN_REQUEST_INTERVAL_SEC - 1e-9


def test_two_instances_split_load(make_broker):
    """С двумя инстансами серия идёт параллельно: суммарное время меньше."""
    b1 = make_broker(instances=["https://one.test"], transport=None)
    for i in range(12):
        b1.fetch_feed(f"t{i}", force=True)
    single = b1._vclock.t - 1_000_000.0

    b2 = make_broker(instances=["https://one.test", "https://two.test"])
    for i in range(12):
        b2.fetch_feed(f"t{i}", force=True)
    double = b2._vclock.t - 1_000_000.0
    assert double < single, "второй инстанс не разгрузил лимит"


def test_cache_prevents_repeat_requests(make_broker):
    """Р3.6: один и тот же URL не чаще раза в 600 с, кроме force=True."""
    b = make_broker(instances=["https://one.test"])
    tr = b._fake_transport
    b.fetch_feed("acct")
    n1 = len(tr.feed_call_times())
    b.fetch_feed("acct")
    assert len(tr.feed_call_times()) == n1, "кеш не сработал"
    b.fetch_feed("acct", force=True)
    assert len(tr.feed_call_times()) == n1 + 1, "force=True не обошёл кеш"
