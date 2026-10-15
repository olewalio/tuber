"""D-29: офлайн-бэкфилл авторов из имеющегося корпуса (без CDN).

Проверяем правило восстановления: ретвит -> orig_handle; иначе owner_handle;
без обоих -> «невосстановимо». Сеть не зовётся.
"""
from __future__ import annotations

from tuber.platforms.x import enrich


def _acc(con, handle):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?, 'A', 'active')",
                (handle,))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def _post(con, acc, tid, *, owner, orig=None, is_retweet=0, author=None,
          published="2026-09-20T10:00:00"):
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
        " text, is_retweet, owner_handle, orig_handle, author_handle)"
        " VALUES (?,?,?,'rss','x',?,?,?,?)",
        (acc, str(tid), published, is_retweet, owner, orig, author))
    con.commit()


def test_recover_retweet_uses_orig_handle(con):
    acc = _acc(con, "feedowner")
    _post(con, acc, 1, owner="feedowner", orig="realauthor", is_retweet=1)
    res = enrich.recover_authors_from_corpus(con, days=90)
    assert res["filled_retweet"] == 1 and res["filled_now"] == 1
    row = con.execute("SELECT author_handle FROM posts WHERE tweet_id='1'").fetchone()
    assert row["author_handle"] == "realauthor"


def test_recover_nonretweet_uses_owner_fallback(con):
    acc = _acc(con, "account")
    _post(con, acc, 2, owner="account")
    res = enrich.recover_authors_from_corpus(con, days=90)
    assert res["filled_owner_fallback"] == 1
    assert con.execute("SELECT author_handle FROM posts WHERE tweet_id='2'"
                       ).fetchone()["author_handle"] == "account"


def test_already_set_not_touched(con):
    acc = _acc(con, "account2")
    _post(con, acc, 3, owner="account2", author="known")
    res = enrich.recover_authors_from_corpus(con, days=90)
    assert res["already_had"] == 1 and res["filled_now"] == 0
    assert con.execute("SELECT author_handle FROM posts WHERE tweet_id='3'"
                       ).fetchone()["author_handle"] == "known"


def test_window_excludes_old_and_unrecoverable_counted(con):
    acc = _acc(con, "acct")
    # вне окна 90 дней
    _post(con, acc, 4, owner="acct", published="2020-11-04T01:42:54")
    # в окне, но без owner/orig -> невосстановимо
    _post(con, acc, 5, owner=None)
    res = enrich.recover_authors_from_corpus(con, days=90)
    assert res["total_window"] == 1           # старый за окном не считается
    assert res["unrecoverable"] == 1
    assert con.execute("SELECT author_handle FROM posts WHERE tweet_id='4'"
                       ).fetchone()["author_handle"] is None


def test_dry_run_does_not_write(con):
    acc = _acc(con, "acct2")
    _post(con, acc, 6, owner="acct2")
    res = enrich.recover_authors_from_corpus(con, days=90, dry_run=True)
    assert res["filled_now"] == 1 and res["dry_run"] is True
    assert con.execute("SELECT author_handle FROM posts WHERE tweet_id='6'"
                       ).fetchone()["author_handle"] is None
