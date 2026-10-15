"""ТЗ-4 2.3: премиальный разовый канал synd_timeline — бюджет, Referer, разбор."""
import pytest

from tuber.platforms.x import channels, config, store as db
from tests.x.mocking import FakeSyndTransport, VClock, make_synd_posts


@pytest.fixture
def synd(db_path):
    clock = VClock()
    tr = FakeSyndTransport(clock=clock)
    b = channels.SyndTimelineBroker(db_path=db_path, transport=tr, clock=clock,
                                    sleeper=clock.advance)
    b._fake = tr
    b._vclock = clock
    yield b
    b.close()


def test_parse_next_data_pinned_and_retweet(synd):
    posts = make_synd_posts(3)
    posts[1] = dict(posts[1])
    posts[1]["full_text"] = "RT @someone: original"
    synd._fake.set("openai", posts=posts)
    out = synd.snapshot("OpenAI")
    assert len(out) == 3
    assert out[0]["pinned"] == 1, "первая запись ленты — закреплённая"
    assert all(p["pinned"] == 0 for p in out[1:])
    assert out[1]["is_retweet"] == 1
    assert out[0]["retweet_count"] == 10
    assert out[0]["likes"] == 100


def test_referer_header_is_sent(synd):
    synd._fake.set("openai", posts=make_synd_posts(2))
    synd.snapshot("OpenAI")
    _url, headers, _t = synd._fake.calls[0]
    assert headers.get("Referer") == config.SYND_REFERER


def test_429_closes_window_no_repeat(synd):
    synd._fake.default = (429, "")
    with pytest.raises(channels.QuotaExceeded):
        synd.snapshot("OpenAI")
    assert len(synd._fake.calls) == 1
    # в том же окне больше не долбим канал
    with pytest.raises(channels.QuotaExceeded):
        synd.snapshot("AnthropicAI")
    assert len(synd._fake.calls) == 1
    log = synd._con.execute("SELECT msg FROM run_log WHERE level='WARN'").fetchall()
    assert any("429" in r["msg"] for r in log), "событие 429 не записано в run_log"


def test_window_budget_not_exceeded(synd):
    synd.window_budget = 2
    synd.window_pause = 1200.0
    synd.daily_budget = 100
    for h in ("a", "b"):
        synd._fake.set(h, posts=make_synd_posts(1))
        synd.snapshot(h)
    with pytest.raises(channels.QuotaExceeded):
        synd.snapshot("c")
    assert len(synd._fake.calls) == 2, "бюджет окна превышен"


def test_daily_budget_5(synd):
    synd.window_budget = 100
    synd.daily_budget = config.SYND_DAILY_BUDGET
    for i in range(config.SYND_DAILY_BUDGET):
        h = f"acc{i}"
        synd._fake.set(h, posts=make_synd_posts(1))
        synd.snapshot(h)
    assert synd._used_today() == config.SYND_DAILY_BUDGET
    with pytest.raises(channels.QuotaExceeded):
        synd.snapshot("over")
    assert len(synd._fake.calls) == config.SYND_DAILY_BUDGET


def test_request_logged_with_kind(synd):
    synd._fake.set("openai", posts=make_synd_posts(2))
    synd.snapshot("OpenAI")
    row = synd._con.execute("SELECT kind, host, status, items FROM requests"
                            " WHERE kind='synd_timeline'").fetchone()
    assert row["host"] == config.SYND_HOST
    assert row["status"] == 200 and row["items"] == 2


def test_parse_missing_next_data_raises():
    with pytest.raises(channels.SyndError):
        channels.parse_timeline_html("<html>no data</html>")
