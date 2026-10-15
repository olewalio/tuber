"""Р7.5 — валидация даты: пусто, 1970 и будущее отбраковываются."""
from datetime import datetime, timedelta, timezone

from tuber.platforms.x import collect, config, store as db, registry
from tuber.platforms.x.broker import (is_valid_published, normalize_published,
                                   snowflake_to_datetime)
from tests.x.mocking import FakeNitter, make_feed, make_item, snowflake_id

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_snowflake_roundtrip():
    dt = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    assert snowflake_to_datetime(snowflake_id(dt)) == dt


def test_rejects_empty_1970_and_future():
    assert is_valid_published(None, now=NOW) is False
    assert is_valid_published(datetime(1970, 1, 1, tzinfo=timezone.utc), now=NOW) is False
    assert is_valid_published(NOW + timedelta(hours=3), now=NOW) is False
    assert is_valid_published(NOW + timedelta(hours=1), now=NOW) is True


def test_empty_pubdate_falls_back_to_snowflake():
    dt = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    iso, src = normalize_published(snowflake_id(dt), "", now=NOW)
    assert src == "snowflake" and iso == "2026-09-10T06:00:00"


def test_1970_pubdate_falls_back_to_snowflake():
    dt = datetime(2026, 9, 8, 3, 0, 0, tzinfo=timezone.utc)
    iso, src = normalize_published(snowflake_id(dt), "Thu, 01 Jan 1970 00:00:00 GMT",
                                   now=NOW)
    assert src == "snowflake" and iso == "2026-09-08T03:00:00"

    iso2, src2 = normalize_published(snowflake_id(dt), "Thu, 01 Jan 1900 05:00:00 GMT",
                                     now=NOW)
    assert src2 == "snowflake"


def test_future_pubdate_falls_back_to_snowflake():
    dt = datetime(2026, 9, 9, 9, 0, 0, tzinfo=timezone.utc)
    iso, src = normalize_published(snowflake_id(dt), "Tue, 01 Jan 2030 00:00:00 GMT",
                                   now=NOW)
    assert src == "snowflake" and iso == "2026-09-09T09:00:00"


def test_undecodable_stays_unknown():
    iso, src = normalize_published("000000", "not a date", now=NOW)
    assert iso is None and src == "unknown"


def test_store_skips_posts_without_date(con):
    """Ни один пост с неразобранной датой не попадает в таблицу posts."""
    con.execute("INSERT INTO accounts (id, handle) VALUES (1, 'ghost')")
    con.commit()
    res = collect.store_posts(con, 1, [
        {"tweet_id": "1", "published_at_utc": None, "published_src": "unknown"},
        {"tweet_id": "2", "published_at_utc": "", "published_src": "unknown"},
        {"tweet_id": "3", "published_at_utc": "2026-09-14T10:00:00",
         "published_src": "rss", "text": "ok"},
    ])
    assert res == {"new": 1, "upd": 0, "skipped": 2}
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


def test_collect_never_stores_invalid_dates(con, db_path):
    """Инвариант Р2 после сбора: 0 пустых, 0 «1970-*», 0 из будущего."""
    good_dt = datetime.now(timezone.utc) - timedelta(days=3)
    good_id = snowflake_id(good_dt)

    def item(tweet_id, title, pubdate):
        return (f"<item><title>{title}</title><dc:creator>@acct</dc:creator>"
                f"<description><![CDATA[<p>{title}</p>]]></description>"
                f"<pubDate>{pubdate}</pubDate>"
                f"<guid isPermaLink=\"false\">{tweet_id}</guid>"
                f"<link>https://one.test/acct/status/{tweet_id}#m</link></item>")

    body = ("<?xml version=\"1.0\"?><rss xmlns:dc=\"http://purl.org/dc/elements/1.1/\">"
            "<channel>"
            + item("0", "no date", "not a date")
            + item("00", "epoch", "Thu, 01 Jan 1970 00:00:00 GMT")
            + item("000", "future", "Tue, 01 Jan 2030 00:00:00 GMT")
            + item(good_id, "good", good_dt.strftime("%a, %d %b %Y %H:%M:%S GMT"))
            + "</channel></rss>")
    tr = FakeNitter()
    tr.set_feed("acct", body=body)
    from tuber.platforms.x.broker import NitterBroker
    b = NitterBroker(instances=["https://one.test"], db_path=db_path, transport=tr)
    registry.add_account(con, "acct", "C")
    summary = collect.collect_tier(con, b, "C")
    b.close()
    assert summary["accounts_ok"] == 1
    bad = con.execute(
        "SELECT COUNT(*) FROM posts WHERE published_at_utc IS NULL OR"
        " published_at_utc='' OR published_at_utc LIKE '1970%' OR"
        " published_at_utc > ?", ((datetime.now(timezone.utc) + timedelta(hours=2))
                                  .strftime("%Y-%m-%dT%H:%M:%S"),)).fetchone()[0]
    assert bad == 0
    total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    assert total == 1, "должен сохраниться только пост с валидной датой"
    assert summary["skipped_no_date"] == 3
    assert summary["details"][0]["new"] == 1
