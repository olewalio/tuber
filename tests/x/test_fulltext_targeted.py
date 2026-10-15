"""D-72: точечный добор полного текста X по id через `api.fxtwitter.com`.

Сеть НЕ зовётся: `fetch`/`opener` подменяются. Все проверки — на КОПИИ базы
(фикстура ``con`` из ``tests/x/conftest.py`` создаёт временную БД).
"""
from __future__ import annotations

from tuber.platforms.x import config, collect, enrich, fxtwitter as fx


# --------------------------------------------------------------------- helpers
def add_account(con, handle="sama", tier="A"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,?,'active')",
                (handle, tier))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def add_post(con, acc, tid, text, *, is_long=1, text_src=None,
             published="2026-09-14T10:00:00"):
    """Пост, попадающий в очередь добора (is_long=1 с коротким текстом или cdn)."""
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
        " text, is_long, text_src) VALUES (?,?,?,'rss',?,?,?)",
        (acc, str(tid), published, text, is_long, text_src))
    con.commit()


def ok_result(text, **kw):
    r = fx._blank_result()
    r.update({"ok": True, "http": 200, "text": text})
    r.update(kw)
    return r


def not_found_result():
    r = fx._blank_result()
    r.update({"http": 404, "not_found": True, "error": "not_found"})
    return r


def noop_sleep(_sec):
    return None


def post_row(con, tid):
    return con.execute(
        "SELECT text, text_src, is_long, fulltext_attempts, fulltext_checked_at,"
        " likes, replies, views, quotes, bookmarks, metrics_at, metrics_src,"
        " retweet_count, author_handle, author_verified, lang"
        " FROM posts WHERE tweet_id=?", (str(tid),)).fetchone()


LONG_FULL = "полный текст " * 240          # ~2880 символов, много длиннее 300


# ------------------------------------------------------------------- test 1
def test_full_text_longer_than_base_written_and_leaves_queue(con):
    acc = add_account(con)
    tid = "1111111111111111111"
    base = "обрезанный " * 20               # 220 символов, is_long=1
    add_post(con, acc, tid, base, is_long=1)
    assert enrich.needs_full_text_count(con) == 1

    def fetch(tweet_id, sleep=None):
        assert str(tweet_id) == tid
        return ok_result(LONG_FULL, lang="ru", likes=1402, replies=3, reposts=7,
                         quotes=0, bookmarks=1, views=999,
                         author_handle="@sama", author_verified=True)

    s = collect.refetch_fulltext_by_id(con, fetch=fetch, sleep=noop_sleep)
    assert s["ok"] == 1 and s["full_text_gained"] == 1
    assert s["fetched"] == 1 and s["not_found"] == 0 and s["errors"] == 0

    row = post_row(con, tid)
    assert row["text"] == LONG_FULL
    assert row["text_src"] == "fxtwitter"
    assert row["is_long"] == 1
    assert row["fulltext_attempts"] == 1
    assert row["fulltext_checked_at"]
    assert row["likes"] == 1402 and row["views"] == 999 and row["bookmarks"] == 1
    assert row["metrics_src"] == "fxtwitter"
    assert row["retweet_count"] == 7 and row["author_handle"] == "sama"
    assert enrich.needs_full_text_count(con) == 0, "пост обязан выпасть из очереди"


# ------------------------------------------------------------------- test 2
def test_confirmed_short_text_marks_is_long_zero_and_leaves_queue(con):
    acc = add_account(con)
    tid = "2222222222222222222"
    add_post(con, acc, tid, "обрез", is_long=1)

    short = "реально короткий пост"
    s = collect.refetch_fulltext_by_id(
        con, fetch=lambda tweet_id, sleep=None: ok_result(short, likes=1),
        sleep=noop_sleep)
    assert s["ok"] == 1 and s["short_confirmed"] == 1 and s["full_text_gained"] == 0

    row = post_row(con, tid)
    assert row["text"] == short
    assert row["text_src"] == "fxtwitter"
    assert row["is_long"] == 0, "подтверждённо короткий -> is_long=0"
    assert enrich.needs_full_text_count(con) == 0


# ------------------------------------------------------------------- test 3
def test_404_keeps_post_until_attempts_exhausted(con):
    acc = add_account(con)
    tid = "3333333333333333333"
    base = "обрезанный " * 20
    add_post(con, acc, tid, base, is_long=1)

    s1 = collect.refetch_fulltext_by_id(
        con, fetch=lambda tweet_id, sleep=None: not_found_result(),
        sleep=noop_sleep)
    assert s1["not_found"] == 1 and s1["attempts_used"] == 1
    row = post_row(con, tid)
    assert row["text"] == base, "404 не трогает текст"
    assert row["fulltext_attempts"] == 1
    assert enrich.needs_full_text_count(con) == 1, "после 1 попытки ещё в очереди"

    for expected in (2, 3):
        collect.refetch_fulltext_by_id(
            con, fetch=lambda tweet_id, sleep=None: not_found_result(),
            sleep=noop_sleep)
        assert post_row(con, tid)["fulltext_attempts"] == expected

    assert enrich.needs_full_text_count(con) == 0, (
        f"после {config.FX_MAX_ATTEMPTS} попыток пост выпадает из очереди")


# ------------------------------------------------------------------- test 4
def test_429_pauses_and_retries_bounded():
    calls = []
    sleeps = []

    def opener(url, timeout, headers):
        calls.append(url)
        return 429, {}, ""

    res = fx.fetch_status("4444444444444444444", opener=opener,
                          sleep=lambda sec: sleeps.append(sec))
    assert res["ok"] is False and res["error"] == "rate_limited"
    assert res["http"] == 429
    assert len(calls) == config.FX_MAX_RETRIES + 1, "повторов не больше FX_MAX_RETRIES"
    assert sleeps == [config.FX_429_SLEEP_SEC] * config.FX_MAX_RETRIES


# ------------------------------------------------------------------- test 5
def test_tempo_between_requests_not_less_than_min_interval(con):
    acc = add_account(con)
    add_post(con, acc, "5555555555555555551", "x" * 100, is_long=1)
    add_post(con, acc, "5555555555555555552", "y" * 100, is_long=1)

    sleeps = []
    s = collect.refetch_fulltext_by_id(
        con, fetch=lambda tweet_id, sleep=None: ok_result(LONG_FULL),
        sleep=lambda sec: sleeps.append(sec))
    assert s["fetched"] == 2
    assert sleeps, "между двумя запросами обязан быть sleep"
    assert min(sleeps) >= config.FX_MIN_INTERVAL_SEC


# ------------------------------------------------------------------- test 6
def test_dry_run_makes_no_request_and_no_write(con):
    acc = add_account(con)
    tid = "6666666666666666666"
    add_post(con, acc, tid, "обрезанный " * 28, is_long=1)
    before = enrich.needs_full_text_count(con)

    calls = []

    def fetch(tweet_id, sleep=None):
        calls.append(tweet_id)
        return ok_result(LONG_FULL)

    s = collect.refetch_fulltext_by_id(con, fetch=fetch, sleep=noop_sleep,
                                       dry_run=True)
    assert s["dry_run"] is True and s["fetched"] == 0
    assert calls == [], "dry-run не делает запросов"
    assert enrich.needs_full_text_count(con) == before
    assert post_row(con, tid)["text_src"] is None, "dry-run ничего не пишет"


# ------------------------------------------------------------------- test 7
def test_max_per_run_bounds_processed_posts(monkeypatch, con):
    acc = add_account(con)
    for i in range(5):
        add_post(con, acc, str(7000000000000000000 + i), "z" * 100, is_long=1)
    monkeypatch.setattr(config, "FX_MAX_PER_RUN", 2)

    calls = []

    def fetch(tweet_id, sleep=None):
        calls.append(tweet_id)
        return ok_result("короткий")

    s = collect.refetch_fulltext_by_id(con, fetch=fetch, sleep=noop_sleep)
    assert s["fetched"] == 2 and len(calls) == 2
    assert s["targets"] == 2


# ------------------------------------------------ fxtwitter: 404 без повторов
def test_fetch_status_404_no_retries():
    calls = []

    def opener(url, timeout, headers):
        calls.append(url)
        return 404, {}, '{"code":404,"message":"not found"}'

    res = fx.fetch_status("8888888888888888888", opener=opener, sleep=noop_sleep)
    assert res["not_found"] is True and res["ok"] is False
    assert res["error"] == "not_found"
    assert len(calls) == 1, "404 не повторяем"


def test_fetch_status_parses_ok_payload():
    body = ('{"code":200,"message":"OK","tweet":{"text":"hello","lang":"en",'
            '"is_note_tweet":true,"likes":10,"replies":2,"retweets":3,"quotes":1,'
            '"bookmarks":4,"views":500,"created_at":"2026-09-14T10:00:00.000Z",'
            '"author":{"screen_name":"@sama","verified":true}}}')
    res = fx.fetch_status("9999999999999999999",
                          opener=lambda url, timeout, headers: (200, {}, body),
                          sleep=noop_sleep)
    assert res["ok"] is True
    assert res["text"] == "hello"
    assert res["reposts"] == 3 and res["quotes"] == 1 and res["views"] == 500
    assert res["author_handle"] == "sama" and res["author_verified"] is True
    assert res["is_note_tweet"] is True
