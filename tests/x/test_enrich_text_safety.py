"""ТЗ-4 2.2: обогащение метриками и ГЛАВНЫЙ питфолл — обрезанный текст CDN.

Правило: текст из CDN можно писать ТОЛЬКО если в базе пусто (или мусор короче
40 символов). Полный текст Nitter (2871 символ у @sama) перезаписывать нельзя.
"""
from types import SimpleNamespace

import pytest

from tuber.platforms.x import config, store as db, enrich
from tests.x.mocking import make_cdn_payload


class StubRouter:
    """Роутер-заглушка: отдаёт заранее заготовленные ответы канала cdn_tweet."""

    def __init__(self, mapping, cdn_429=0):
        self.mapping = mapping
        self.cdn = SimpleNamespace(requests_429=cdn_429)

    def enrich_tweet(self, tweet_id):
        return self.mapping[str(tweet_id)]


def add_account(con, handle="sama", tier="A"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,?,'active')",
                (handle, tier))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def add_post(con, account_id, tid, text=None, published="2026-09-14T10:00:00",
             is_retweet=0):
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
        " text, is_retweet) VALUES (?,?,?,'rss',?,?)",
        (account_id, str(tid), published, text, is_retweet))
    con.commit()


NITTER_FULL = "полный текст " * 240          # ~2871 символ, как у @sama в Nitter
CDN_TRUNC = "обрезанный " * 28               # ~280 символов, как отдаёт CDN


def test_cdn_text_never_overwrites_full_nitter_text(con):
    acc = add_account(con)
    tid = "1234567890123456789"
    add_post(con, acc, tid, text=NITTER_FULL)
    fields = {"likes": 339548, "replies": 1200, "has_quote": 0, "is_long": 1,
              "lang": "en", "text": CDN_TRUNC, "author_verified": 0}
    router = StubRouter({tid: ("ok", fields, 200)})
    s = enrich.enrich_batch(con, router)
    assert s["ok"] == 1 and s["text_filled"] == 0 and s["text_preserved"] == 1
    row = con.execute("SELECT text, likes, replies, metrics_src, metrics_at"
                      " FROM posts WHERE tweet_id=?", (tid,)).fetchone()
    assert len(row["text"]) == len(NITTER_FULL), "текст Nitter перезаписан CDN!"
    assert row["likes"] == 339548 and row["replies"] == 1200
    assert row["metrics_src"] == "cdn" and row["metrics_at"]


def test_cdn_text_fills_only_empty(con):
    acc = add_account(con)
    add_post(con, acc, "1114567890123456789", text=None)
    fields = {"likes": 1, "replies": 0, "has_quote": 0, "is_long": 0,
              "lang": "en", "text": "короткий текст из CDN",
              "author_verified": 1}
    s1 = enrich.enrich_batch(con, StubRouter(
        {"1114567890123456789": ("ok", fields, 200)}))
    assert s1["text_filled"] == 1
    row = con.execute("SELECT text, author_verified FROM posts WHERE tweet_id=?",
                      ("1114567890123456789",)).fetchone()
    assert row["text"] == "короткий текст из CDN"
    assert row["author_verified"] == 1


def test_history_row_and_idempotency(con):
    acc = add_account(con)
    tid = "2224567890123456789"
    add_post(con, acc, tid, text=NITTER_FULL)
    fields = {"likes": 10, "replies": 2, "has_quote": 1, "is_long": 0,
              "lang": "en", "text": CDN_TRUNC, "author_verified": 0}
    router = StubRouter({tid: ("ok", fields, 200)})
    enrich.enrich_batch(con, router)
    hist = con.execute("SELECT age_hours, likes, replies, src FROM"
                       " post_metrics_history WHERE tweet_id=?", (tid,)).fetchall()
    assert len(hist) == 1 and hist[0]["age_hours"] is not None
    # повтор: 0 новых строк, метрики не трогаются
    before = con.execute("SELECT metrics_at FROM posts WHERE tweet_id=?",
                         (tid,)).fetchone()["metrics_at"]
    s2 = enrich.enrich_batch(con, router)
    after = con.execute("SELECT metrics_at FROM posts WHERE tweet_id=?",
                        (tid,)).fetchone()["metrics_at"]
    assert s2["selected"] == 0 and s2["ok"] == 0
    assert before == after
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


def test_retweet_metrics_src_cdn_rt(con):
    acc = add_account(con)
    tid = "3334567890123456789"
    add_post(con, acc, tid, text=None, is_retweet=1)
    fields = {"likes": 0, "replies": 0, "has_quote": 0, "is_long": 0,
              "lang": "en", "text": "RT text", "author_verified": 0}
    enrich.enrich_batch(con, StubRouter({tid: ("ok", fields, 200)}))
    row = con.execute("SELECT metrics_src FROM posts WHERE tweet_id=?",
                      (tid,)).fetchone()
    assert row["metrics_src"] == "cdn_rt"


def test_404_and_tombstone_mark_deleted_not_error(con):
    acc = add_account(con)
    add_post(con, acc, "4444567890123456789", text="x")
    add_post(con, acc, "5554567890123456789", text="y")
    router = StubRouter({
        "4444567890123456789": ("not_found", None, 404),
        "5554567890123456789": ("deleted", None, 200),
    })
    s = enrich.enrich_batch(con, router)
    assert s["errors"] == 0 and s["not_found"] == 1 and s["deleted"] == 1
    rows = con.execute("SELECT tweet_id, deleted_at FROM posts ORDER BY tweet_id"
                       ).fetchall()
    assert all(r["deleted_at"] for r in rows)
    # удалённые больше не выбираются
    assert enrich.select_pending(con) == []


def test_is_long_without_full_text_goes_to_nitter_queue(con):
    acc = add_account(con)
    tid = "6664567890123456789"
    add_post(con, acc, tid, text=None)
    fields = {"likes": 5, "replies": 0, "has_quote": 0, "is_long": 1,
              "lang": "en", "text": CDN_TRUNC, "author_verified": 0}
    s = enrich.enrich_batch(con, StubRouter({tid: ("ok", fields, 200)}))
    assert s["needs_nitter"] == 1
    log = con.execute("SELECT msg FROM run_log WHERE level='WARN'").fetchall()
    assert any("Nitter" in r["msg"] for r in log)


def test_backfill_days_limits_scope(con):
    acc = add_account(con)
    from datetime import datetime, timedelta, timezone
    old = db.iso(datetime.now(timezone.utc) - timedelta(days=40))
    new = db.iso(datetime.now(timezone.utc) - timedelta(days=1))
    add_post(con, acc, "7774567890123456789", text="a", published=old)
    add_post(con, acc, "8884567890123456789", text="b", published=new)
    rows = enrich.select_pending(con, backfill_days=7)
    assert [r["tweet_id"] for r in rows] == ["8884567890123456789"]
    assert len(enrich.select_pending(con)) == 2


def test_pending_sorted_fresh_first_then_tier(con):
    a = add_account(con, "tierA", "A")
    c = add_account(con, "tierC", "C")
    add_post(con, c, "9994567890123456789", text="old", published="2026-09-10T10:00:00")
    add_post(con, a, "9994567890123456788", text="new", published="2026-09-14T10:00:00")
    rows = enrich.select_pending(con)
    assert rows[0]["tweet_id"] == "9994567890123456788"
