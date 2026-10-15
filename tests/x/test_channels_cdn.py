"""ТЗ-4 2.1/2.2: канал cdn_tweet — коды, gzip, лимитер, backoff, журнал."""
import gzip
import json

import pytest

from tuber.platforms.x import channels, config, store as db
from tuber.platforms.x.broker import _decode_body, raw_http_get
from tests.x.mocking import FakeCdnTransport, VClock, make_cdn_payload


@pytest.fixture
def cdn(db_path):
    clock = VClock()
    tr = FakeCdnTransport(clock=clock)
    broker = channels.CdnTweetBroker(db_path=db_path, transport=tr, clock=clock,
                                     sleeper=clock.advance)
    broker._fake = tr
    broker._vclock = clock
    yield broker
    broker.close()


def test_classify_response_codes():
    assert channels.classify_cdn_response(400, "{}") == "invalid"
    assert channels.classify_cdn_response(404, "{}") == "not_found"
    assert channels.classify_cdn_response(429, "{}") == "rate_limited"
    assert channels.classify_cdn_response(0, "") == "network"
    assert channels.classify_cdn_response(200, "not json") == "error"
    tomb = json.dumps({"__typename": "Tombstone", "id_str": "1"})
    assert channels.classify_cdn_response(200, tomb) == "deleted"
    ok = make_cdn_payload()
    assert channels.classify_cdn_response(200, ok) == "ok"


def test_parse_tweet_result_fields():
    payload = make_cdn_payload(likes=42, replies=7, lang="en", handle="sama",
                               note="very long " * 500, quoted="OpenAI",
                               verified=True, edited=True)
    f = channels.parse_tweet_result(payload)
    assert f["likes"] == 42 and f["replies"] == 7
    assert f["is_long"] == 1 and f["has_quote"] == 1
    assert f["quoted_author"] == "OpenAI"
    assert f["author_verified"] == 1
    assert f["screen_name"] == "sama"
    assert f["created_at"] == "2026-09-14T10:00:00"


def test_parse_short_text_is_not_long():
    f = channels.parse_tweet_result(make_cdn_payload())
    assert f["is_long"] == 0 and f["has_quote"] == 0 and f["author_verified"] == 0


def test_gzip_decoded_by_single_transport():
    raw = gzip.compress("hello мир".encode("utf-8"))
    assert _decode_body(raw, {"content-encoding": "gzip"}) == "hello мир"


def test_fetch_records_request_and_returns_fields(cdn):
    tid = "1234567890123456789"
    cdn._fake.set(tid, make_cdn_payload(tid, likes=5, replies=1))
    kind, fields, status = cdn.fetch(tid)
    assert (kind, status) == ("ok", 200)
    assert fields["likes"] == 5
    row = cdn._con.execute("SELECT kind, host, status, items FROM requests"
                           " WHERE kind='cdn_tweet'").fetchone()
    assert row["kind"] == "cdn_tweet"
    assert row["host"] == config.CDN_HOST
    assert row["status"] == 200 and row["items"] == 1


def test_fetch_404_and_invalid_and_deleted(cdn):
    cdn._fake.set("1234567890123456789", status=404, body="{}")
    cdn._fake.set("9999999999999999999", status=400, body="{}")
    tomb = make_cdn_payload("1234567890123456788", tombstone=True)
    cdn._fake.set("1234567890123456788", body=tomb)
    assert cdn.fetch("1234567890123456789")[0] == "not_found"
    assert cdn.fetch("9999999999999999999")[0] == "invalid"
    assert cdn.fetch("1234567890123456788")[0] == "deleted"
    # битой длины ID даже не ходит в сеть
    before = len(cdn._fake.calls)
    assert cdn.fetch("not-a-number")[0] == "invalid"
    assert len(cdn._fake.calls) == before


def test_rate_limiter_2_per_sec_and_min_interval(cdn):
    for i in range(5):
        cdn._fake.set(str(1234567890123456700 + i), make_cdn_payload())
        cdn.fetch(str(1234567890123456700 + i))
    times = [t for _u, _h, t in cdn._fake.calls]
    gaps = [round(b - a, 3) for a, b in zip(times, times[1:])]
    assert all(g >= config.CDN_MIN_INTERVAL_SEC - 1e-9 for g in gaps), gaps
    # 3-й запрос не раньше, чем через 1 с от первого (2 зап/с)
    assert times[2] - times[0] >= 1.0 - 1e-9


def test_429_backoff_60_120_240_and_max_retries(cdn):
    tid = "1234567890123456789"
    cdn._fake.routes[tid] = (429, "{}")
    kind, fields, status = cdn.fetch(tid)
    assert kind == "rate_limited" and status == 429
    sleeps = cdn._vclock.sleeps
    assert 60 in sleeps and 120 in sleeps and 240 in sleeps, sleeps
    # 1 основной + 3 повтора
    assert len(cdn._fake.calls) == config.CDN_MAX_RETRIES + 1
    assert cdn.requests_429 == 4


def test_429_then_ok(cdn):
    tid = "1234567890123456789"
    seq = {"n": 0}

    def flaky(url, headers):
        seq["n"] += 1
        if seq["n"] == 1:
            return 429, {}, "{}"
        return 200, {}, make_cdn_payload(tid, likes=9)

    cdn._transport = flaky
    kind, fields, status = cdn.fetch(tid)
    assert kind == "ok" and fields["likes"] == 9
    assert 60 in cdn._vclock.sleeps


def test_raw_http_get_only_transport_exists():
    """Транспорт эскалируется из broker, каналы его переиспользуют."""
    import os
    assert callable(raw_http_get)
    src = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "tuber", "platforms", "x")
    assert "raw_http_get" in open(
        os.path.join(src, "channels.py"), encoding="utf-8").read()
