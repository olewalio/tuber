"""Р7.1 — разбор фикстуры RSS."""
from tuber.platforms.x.broker import count_items, parse_rss

HOST = "https://nitter.example.org"


def _by_id(posts):
    return {p["tweet_id"]: p for p in posts}


def test_fixture_parses_all_items(fixture_feed):
    posts = parse_rss(fixture_feed, host=HOST)
    assert count_items(fixture_feed) == 8
    assert len(posts) == 8


def test_retweet_owner_vs_original(fixture_feed):
    p = _by_id(parse_rss(fixture_feed, host=HOST))["2099052964869046272"]
    assert p["is_retweet"] == 1
    assert p["owner_handle"] == "openai"      # владелец ленты, из "RT by @OpenAI:"
    assert p["orig_handle"] == "evayzh"       # автор оригинала, из dc:creator
    assert p["media_kind"] == "video"
    assert "@bea_yankelevich" in p["text"] or "bea_yankelevich" in p["mentions"]


def test_quote(fixture_feed):
    p = _by_id(parse_rss(fixture_feed, host=HOST))["2098883095557046272"]
    assert p["is_quote"] == 1
    assert p["is_retweet"] == 0
    assert p["owner_handle"] == "quoter"
    assert p["orig_handle"] == "janeresearcher"
    assert p["text"] == "This is the most important safety result of the year."
    assert "janeresearcher" not in p["text"]  # текст цитаты не приклеен к посту


def test_links_mentions_hashtags_media(fixture_feed):
    p = _by_id(parse_rss(fixture_feed, host=HOST))["2099438001976246272"]
    assert p["links"] == ["https://go.nasa.gov/4dvXZXN"]
    assert p["mentions"] == ["esa"]
    assert p["hashtags"] == ["AIAgents"]
    assert p["media_kind"] == "photo"
    # ссылки-упоминания и хештеги не попадают в links
    assert not any("nitter.example.org" in l for l in p["links"])


def test_gif_and_old_guid(fixture_feed):
    p = _by_id(parse_rss(fixture_feed, host=HOST))["2002302954501046272"]
    assert p["media_kind"] == "gif"
    assert p["published_src"] == "rss"


def test_snowflake_fallback_dates(fixture_feed):
    posts = _by_id(parse_rss(fixture_feed, host=HOST))
    assert posts["2098286665528246272"]["published_src"] == "snowflake"   # pubDate пуст
    assert posts["2098286665528246272"]["published_at_utc"] == "2026-09-11T05:45:00"
    assert posts["2098018649502646272"]["published_src"] == "snowflake"   # 1970
    assert posts["2098018649502646272"]["published_at_utc"] == "2026-09-10T12:00:00"
    assert posts["2097808514872246272"]["published_src"] == "snowflake"   # будущее
    assert posts["2097808514872246272"]["published_at_utc"] == "2026-09-09T22:05:00"


def test_unknown_date_stays_unknown(fixture_feed):
    p = _by_id(parse_rss(fixture_feed, host=HOST))["000000"]
    assert p["published_at_utc"] is None
    assert p["published_src"] == "unknown"


def test_cursor_next_from_header(make_broker, fixture_feed):
    """Р3.9: cursor_next берётся из заголовка min-id (нижний регистр)."""
    b = make_broker()
    tr = b._fake_transport
    tr.set_feed("acct", body=fixture_feed, headers={"min-id": "CURSOR-42"})
    posts = b.fetch_feed("acct")
    assert len(posts) == 8
    assert all(p["cursor_next"] == "CURSOR-42" for p in posts)
