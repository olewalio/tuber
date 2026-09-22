"""ТЗ-10, волна 1 — B3: счётчики fulltext проброшены в ``run`` (X-контур).

Без сети: роутер/брокер Nitter подменяются заглушкой. БД — временная
(фикстура ``con`` из ``tests/x/conftest.py``).
"""
from __future__ import annotations

import argparse

from tuber.platforms.x import cli, collect


class _StubBroker:
    def __init__(self, feed):
        self.feed = feed
        self.run_id = None
        self.closed = False

    def fetch_feed(self, handle, cursor=None, priority="collect", force=False):
        return self.feed

    def close(self):
        self.closed = True


class _StubRouter:
    def __init__(self, broker):
        self.nitter = broker

    def set_run_id(self, rid):
        pass


def _truncated_post(con):
    """Аккаунт + длинный пост с CDN-текстом (нужен добор полного текста)."""
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('sama','A','active')")
    con.commit()
    aid = con.execute("SELECT id FROM accounts WHERE handle='sama'").fetchone()["id"]
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
        " text, text_hash, is_long, metrics_src, text_src)"
        " VALUES (?, '555', '2026-09-14T10:00:00', 'rss', 'короткий', 'h', 1,"
        " 'cdn', 'cdn')",
        (aid,))
    con.commit()
    return aid


def test_refetch_fulltext_reports_honest_counters(con):
    _truncated_post(con)
    full = "полный " * 800

    class Broker:
        def fetch_feed(self, handle, cursor=None, priority="collect", force=False):
            return [{"tweet_id": "555", "published_at_utc": "2026-09-14T10:00:00",
                     "published_src": "rss", "text": full, "links": [], "mentions": [],
                     "hashtags": [], "is_retweet": 0, "is_quote": 0, "is_reply": 0,
                     "media_kind": None, "cursor_next": None}]

    s = collect.refetch_fulltext(con, Broker())
    assert s["new"] == 0, f"добор текста не должен создавать посты: {s}"
    assert s["upd"] == 1, f"обновление не посчитано: {s}"


def test_cmd_fulltext_writes_counters_to_run(con, monkeypatch):
    _truncated_post(con)
    full = "полный " * 800
    feed = [{"tweet_id": "555", "published_at_utc": "2026-09-14T10:00:00",
             "published_src": "rss", "text": full, "links": [], "mentions": [],
             "hashtags": [], "is_retweet": 0, "is_quote": 0, "is_reply": 0,
             "media_kind": None, "cursor_next": None}]
    broker = _StubBroker(feed)
    monkeypatch.setattr(cli, "_router", lambda args, run_id=None: _StubRouter(broker))

    rc = cli.cmd_fulltext(argparse.Namespace(dry_run=False, limit=None))
    assert rc == 0

    row = con.execute(
        "SELECT items_new, items_upd, ok_count FROM main.run"
        " WHERE mode='fulltext' ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "прогон fulltext не записан в run"
    assert row["items_upd"] >= 1, f"Items_upd fulltext = {row['items_upd']} (ожидалось >=1)"
    assert row["items_new"] == 0, f"items_new fulltext = {row['items_new']}"
    # фактическая разница тоже подтверждает добор
    got = con.execute("SELECT text_src FROM posts WHERE tweet_id='555'").fetchone()
    assert got["text_src"] == "nitter"
