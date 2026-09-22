"""Тесты сессионного транспорта X (ТЗ-43A/44) — без живого интернета.

Все внешние ответы — фикстуры-заглушки; транспорт X подменяется функцией,
как это сделано в ``tests/x/mocking.py`` для Nitter.
"""
from __future__ import annotations

import json
import os
import re
import time

import pytest

from tests.x.mocking import VClock
from tuber.platforms.x import config, collect as xcollect, session as xsession
from tuber.platforms.x import store as db
from tuber.platforms.x.broker import NitterBroker

SESSION_QIDS = {
    "UserByScreenName": "QID_byScreenName_000001",
    "UserTweets": "QID_userTweets__00000002",
    "TweetDetail": "QID_tweetDetail_00000003",
    "TweetResultsByRestIds": "QID_byRestIds_000000004",
    "ExplorePage": "QID_explorePage_00000005",
}
SESSION_FEATURES = {"view_counts_everywhere_api_enabled": True,
                    "responsive_web_graphql_timeline_navigation_enabled": True}


# ------------------------------------------------------------------ фикстуры
def write_session(tmp_path, token="tok-secret", ct0="csrf-secret", mode=0o600):
    path = tmp_path / "x_session.json"
    path.write_text(json.dumps({"auth_token": token, "ct0": ct0}), encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def write_qids(tmp_path):
    path = tmp_path / "x_qids.json"
    path.write_text(json.dumps({"fetched_at": int(time.time()),
                                "bundle": "https://abs.twimg.com/x/main.abc.js",
                                "qids": SESSION_QIDS,
                                "features": SESSION_FEATURES}), encoding="utf-8")
    return str(path)


def tweet(tid, handle, text="hello", views=100, created="Wed Oct 10 20:19:24 +0000 2018",
          likes=7, replies=2, retweets=1, quotes=0, reply_to=None,
          media=None, is_quote=False, quoted_handle=None, rt_handle=None,
          followers=1234, author_id=None):
    lg = {"full_text": text, "created_at": created, "favorite_count": likes,
          "reply_count": replies, "retweet_count": retweets, "quote_count": quotes,
          "in_reply_to_status_id_str": reply_to, "lang": "en",
          "is_quote_status": is_quote,
          "entities": {"urls": [{"expanded_url": "https://ex.test/a"}],
                       "user_mentions": [{"screen_name": "friend"}],
                       "hashtags": [{"text": "ai"}]}}
    if media:
        lg["extended_entities"] = {"media": [{"type": media}]}
    if rt_handle:
        lg["retweeted_status_id_str"] = "999"
        lg["retweeted_status_result"] = {"result": {
            "core": {"user_results": {"result": {"core": {"screen_name": rt_handle}}}}}}
    tw = {
        "rest_id": tid,
        "legacy": lg,
        "views": {"count": views},
        "core": {"user_results": {"result": {
            "rest_id": author_id or f"id_{handle}",
            "core": {"screen_name": handle},
            "relationship_counts": {"followers": followers},
            "legacy": {"followers_count": followers},
        }}},
    }
    if quoted_handle:
        tw["quoted_status_result"] = {"result": {
            "core": {"user_results": {"result": {"core": {"screen_name": quoted_handle}}}}}}
    return tw


def profile_payload(handle="nasa", followers=555, uid="4242"):
    return {"data": {"user": {"result": {
        "rest_id": uid,
        "core": {"screen_name": handle, "name": "NASA",
                 "created_at": "Wed Oct 10 20:19:24 +0000 2018"},
        "relationship_counts": {"followers": followers, "following": 3},
        "tweet_counts": {"tweets": 100},
        "profile_bio": {"description": "био"},
        "verification": {"verified": True},
        "privacy": {"protected": False},
    }}}}


def timeline_payload(tweets, cursor=None):
    entries = [{"content": {"itemContent": {"tweet_results": {"result": t}}}}
               for t in tweets]
    if cursor:
        entries.append({"content": {"cursorType": "Bottom", "value": cursor}})
    return {"data": {"user": {"result": {"timeline": {"timeline": {
        "instructions": [{"type": "TimelineAddEntries", "entries": entries}],
    }}}}}}


def thread_payload(tweets, nested=()):
    entries = []
    for t in tweets:
        entries.append({"content": {"itemContent": {"tweet_results": {"result": t}}}})
    if nested:
        # Часть треда приходит в content.items[].item.itemContent (ТЗ-43A п.5).
        entries.append({"content": {"items": [
            {"item": {"itemContent": {"tweet_results": {"result": t}}}}
            for t in nested]}})
    return {"data": {"threaded_conversation_with_injections_v2": {"instructions": [
        {"type": "TimelineAddEntries", "entries": entries}],
    }}}


def batch_payload(tweets):
    return {"data": {"tweetResult": [{"result": t} for t in tweets]
                     + [{"result": {"__typename": "TweetUnavailable"}}]}}


class FakeX:
    """Подменяемый транспорт: маршрутизирует по URL, пишет журнал вызовов."""

    def __init__(self, clock=None, home_html="<html>main.abc.js</html>",
                 bundle_js="", routes=None, fail_ops=()):
        self.clock = clock
        self.calls = []          # (url, clock_time)
        self.header_calls = []   # (url, headers)
        self.home_html = home_html
        self.bundle_js = bundle_js
        self.routes = routes or {}
        self.fail_ops = set(fail_ops)
        self.gql_hits = {}

    def __call__(self, url, headers):
        self.calls.append((url, self.clock() if self.clock else 0.0))
        self.header_calls.append((url, headers))
        if url.startswith("https://abs.twimg.com"):
            return 200, {}, self.bundle_js
        if url == xsession.HOME:
            return 200, {}, self.home_html
        for op, payload in self.routes.items():
            if f"/{op}" in url:
                self.gql_hits[op] = self.gql_hits.get(op, 0) + 1
                if op in self.fail_ops and self.gql_hits[op] == 1:
                    return 404, {}, "query not found"
                return 200, {}, json.dumps(payload)
        return 404, {}, "unknown"


def mk_session(con, tmp_path, transport, vclock=None, **kw):
    vclock = vclock or VClock()
    return xsession.XSessionTransport(
        session_file=kw.pop("session_file", write_session(tmp_path)),
        qids_file=kw.pop("qids_file", write_qids(tmp_path)),
        transport=transport, clock=vclock, sleeper=vclock.sleep,
        con=con, **kw)


# ------------------------------------------------------------------ разбор
def test_profile_timeline_thread_batch(con, tmp_path):
    tw = tweet("111", "nasa", text="текст поста", views=4321)
    reply = tweet("222", "nasa", text="ответ", reply_to="111", views=10)
    routes = {
        "UserByScreenName": profile_payload("nasa", followers=777),
        "UserTweets": timeline_payload([tw, reply], cursor="CURSOR-1"),
        "TweetDetail": thread_payload([tw], nested=[reply]),
        "TweetResultsByRestIds": batch_payload([tw]),
    }
    fake = FakeX(routes=routes)
    s = mk_session(con, tmp_path, fake)

    prof = s.profile("nasa")
    assert prof["followers"] == 777 and prof["id"] == "4242"

    posts, cursor = s.timeline("4242")
    assert cursor == "CURSOR-1"
    assert posts[0]["views"] == 4321, "просмотры не должны теряться"
    assert posts[0]["likes"] == 7 and posts[0]["retweets"] == 1
    assert posts[0]["links"] == ["https://ex.test/a"]
    assert posts[0]["mentions"] == ["friend"] and posts[0]["hashtags"] == ["ai"]
    assert posts[0]["author"] == "nasa" and posts[0]["author_followers"] == 1234
    assert posts[1]["is_reply"] is True and posts[1]["reply_to"] == "111"

    thread = s.thread("111")
    ids = {t["id"] for t in thread}
    assert "111" in ids and "222" in ids, "ответы из content.items[] тоже в треде"

    batch = s.tweets_by_ids(["111"])
    assert [b["id"] for b in batch] == ["111"], "батч читает СПИСОК data.tweetResult"


def test_feed_posts_match_contract(con, tmp_path):
    rt = tweet("333", "nasa", text="rt", views=5, rt_handle="spacex")
    quote = tweet("444", "nasa", text="q", is_quote=True, quoted_handle="openai")
    routes = {"UserByScreenName": profile_payload("nasa"),
              "UserTweets": timeline_payload([rt, quote])}
    s = mk_session(con, tmp_path, FakeX(routes=routes))
    posts = s.fetch_feed("nasa")
    assert len(posts) == 2
    required = ("tweet_id", "owner_handle", "orig_handle", "published_at_utc",
                "published_src", "text", "links", "mentions", "hashtags",
                "is_retweet", "is_quote", "is_reply", "media_kind", "cursor_next")
    metrics = ("likes", "replies", "reposts", "views", "quotes",
               "author_followers", "author_id")
    for p in posts:
        for key in required + metrics:
            assert key in p, f"нет ключа {key}"
        assert p["owner_handle"] == "nasa"
        assert p["published_src"] == "x_session"
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", p["published_at_utc"])
    assert posts[0]["is_retweet"] == 1 and posts[0]["orig_handle"] == "spacex"
    assert posts[1]["is_quote"] == 1 and posts[1]["orig_handle"] == "openai"
    assert posts[1]["views"] == 100


def test_suffix_retweet_and_media(con, tmp_path):
    tw = tweet("555", "nasa", views=9, media="video")
    routes = {"UserByScreenName": profile_payload("nasa"),
              "UserTweets": timeline_payload([tw])}
    posts = mk_session(con, tmp_path, FakeX(routes=routes)).fetch_feed("nasa")
    assert posts[0]["media_kind"] == "video"


# ------------------------------------------------------ самовосстановление 404
def test_query_id_self_heal_on_404(con, tmp_path):
    html = ('<html>main.abc.js '
            'https://abs.twimg.com/responsive-web/client-web/main.999.js</html>')
    bundle = ('{queryId:"QID_fresh_byScreenName_01",operationName:"UserByScreenName"}'
              '["view_counts_everywhere_api_enabled","some_other_feature"]')
    routes = {"UserByScreenName": profile_payload("nasa", followers=42)}
    fake = FakeX(home_html=html, bundle_js=bundle, routes=routes,
                 fail_ops=("UserByScreenName",))
    s = mk_session(con, tmp_path, fake)

    prof = s.profile("nasa")           # первый gql → 404, перекачка, повтор → 200
    assert prof["followers"] == 42
    assert fake.gql_hits["UserByScreenName"] == 2, "после 404 ровно один повтор"
    bundle_calls = [u for u, _ in fake.calls if u.startswith("https://abs.twimg.com")]
    assert len(bundle_calls) == 1, "бандл перекачан один раз"
    assert s.qids["UserByScreenName"] == "QID_fresh_byScreenName_01"


# --------------------------------------------------- отсутствие сессии = фолбэк
def test_missing_session_falls_back_to_nitter(con, tmp_path, monkeypatch):
    from tests.x.mocking import FakeNitter
    monkeypatch.setattr(config, "X_SESSION_ENABLED", True)
    # ТЗ-43C: ленты через сессию включаются осознанно; проверяем сам фолбэк.
    monkeypatch.setattr(config, "X_SESSION_FEDS_ENABLED", True)
    monkeypatch.setattr(config, "X_SESSION_FILE", str(tmp_path / "нет.json"))
    monkeypatch.setattr(config, "X_QIDS_FILE", str(tmp_path / "нет_qids.json"))
    xsession.reset_session_transport()
    vclock = VClock()
    fake_nitter = FakeNitter(clock=vclock)
    b = NitterBroker(instances=["https://one.test"], db_path=config.DB_PATH,
                     transport=fake_nitter, clock=vclock,
                     sleeper=lambda s: vclock.advance(s), async_mode=False)
    try:
        posts = b.fetch_feed("nasa")
        assert posts, "Nitter-путь обязан отдать ленту"
        assert b._session is False
        # Признак «сессия недоступна» НЕ отказ Nitter.
        assert all(v == 0 for v in b._collect_fail.values())
        assert b._collect_fail_events == 0
    finally:
        b.close()
        xsession.reset_session_transport()


def test_unavailable_session_does_not_raise(con, tmp_path):
    fake = FakeX()
    with pytest.raises(xsession.SessionUnavailable):
        xsession.XSessionTransport(session_file=str(tmp_path / "нет.json"),
                                   qids_file=write_qids(tmp_path),
                                   transport=fake, con=con)


def test_session_permissions_wider_than_600(con, tmp_path):
    path = write_session(tmp_path, mode=0o644)
    with pytest.raises(xsession.SessionUnavailable):
        xsession.XSessionTransport(session_file=path, qids_file=write_qids(tmp_path),
                                   transport=FakeX(), con=con)


# ----------------------------------------------------------------- изоляция сети
def test_session_module_has_no_direct_network():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "tuber", "platforms", "x", "session.py")
    text = open(path, encoding="utf-8").read()
    for pat in (r"\burlopen\b", r"urllib\.request", r"\brequests\.(get|post|Session)\b",
                r"\bhttp\.client\b"):
        assert not re.search(pat, text), f"session.py содержит запрещённое: {pat}"


# ----------------------------------------------------------------- ограничитель
def test_rate_limiter_caps_requests_in_window(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "X_SESSION_RATE_MAX", 2)
    monkeypatch.setattr(config, "X_SESSION_RATE_WINDOW_SEC", 60.0)
    monkeypatch.setattr(config, "X_SESSION_MIN_INTERVAL", 0.0)
    vclock = VClock(start=1_000_000.0)
    fake = FakeX(clock=vclock, routes={"UserByScreenName": profile_payload("nasa")})
    s = mk_session(con, tmp_path, fake, vclock=vclock)
    for _ in range(6):
        assert s.profile("nasa")
    stamps = [t for url, t in fake.calls if "/UserByScreenName" in url]
    assert len(stamps) == 6
    # В любом окне 60 с фактических запросов не больше лимита (2).
    for i, t0 in enumerate(stamps):
        in_window = [t for t in stamps if t0 - 60.0 < t <= t0]
        assert len(in_window) <= 2, f"перебор лимита у {t0}: {in_window}"


# ------------------------------------------------------------- подписчики (ТЗ-44)
def test_follower_snapshot_series(con, tmp_path):
    sid = _seed_source(con, "nasa")
    h1 = db.set_follower_snapshot(con, sid, 100, avg_views=10,
                                  taken_at="2026-09-20 00:00:00")
    h2 = db.set_follower_snapshot(con, sid, 150, avg_views=20,
                                  taken_at="2026-09-21 00:00:00")
    assert [p["subs"] for p in h2] == [100, 150]
    row = con.execute("SELECT subs, subs_at, avg_views FROM source WHERE id=?",
                      (sid,)).fetchone()
    assert row["subs"] == 150 and row["avg_views"] == 20
    history = json.loads(con.execute("SELECT meta_json FROM source WHERE id=?",
                                     (sid,)).fetchone()["meta_json"])["followers_history"]
    assert len(history) == 2 and h1 and len(history) == 2


def _seed_source(con, handle):
    con.execute("INSERT OR IGNORE INTO source (platform, handle, status)"
                " VALUES ('x', ?, 'active')", (handle,))
    con.commit()
    return con.execute("SELECT id FROM source WHERE platform='x' AND handle=?",
                       (handle,)).fetchone()["id"]


# ------------------------------------------------------ метрики в metric_snapshot
def test_record_session_metrics_writes_views(con, tmp_path):
    sid = _seed_source(con, "nasa")
    con.execute(
        "INSERT INTO content (platform, source_id, external_id, published_at)"
        " VALUES ('x', ?, '111', '2026-09-20 10:00:00')", (sid,))
    con.commit()
    post = {"tweet_id": "111", "views": 999, "likes": 5, "replies": 2,
            "reposts": 1, "quotes": 0}
    n = db.record_session_metrics(con, [post, {"tweet_id": "нет"}],
                                  taken_at="2026-09-21 12:00:00")
    assert n == 1
    row = con.execute(
        "SELECT views, likes, replies, reposts, quotes, source FROM metric_snapshot"
        " WHERE external_id='111'").fetchone()
    assert row["views"] == 999 and row["source"] == "x_session"
    assert row["reposts"] == 1 and row["quotes"] == 0
    # D-66: просмотры доходят до `content_latest` (ось «охват на подписчика»).
    latest = con.execute(
        "SELECT views, likes FROM content_latest WHERE external_id='111'").fetchone()
    assert latest is not None and latest["views"] == 999
    assert latest["likes"] == 5


# --------------------------------------------- заголовки: куки ⇄ bearer (грабли)
def test_headers_cookie_bearer_split(con, tmp_path):
    """HTML x.com — только куки; Bearer/csrf — только на /i/api/graphql."""
    html = ('<html>main.abc.js '
            'https://abs.twimg.com/responsive-web/client-web/main.777.js</html>')
    bundle = ('{queryId:"QID_aaaaaaaaaaaaaaaa",operationName:"UserByScreenName"}'
              '["view_counts_everywhere_api_enabled"]')
    fake = FakeX(home_html=html, bundle_js=bundle,
                 routes={"UserByScreenName": profile_payload("nasa")})
    s = mk_session(con, tmp_path, fake)
    s.refresh_query_ids()          # страница + бандл
    s.profile("nasa")              # graphql

    home_headers = dict(fake.header_calls[0][1])
    assert "Cookie" in home_headers
    assert "authorization" not in home_headers, "HTML с bearer даёт 401"
    bundle_headers = dict(fake.header_calls[1][1])
    assert "Cookie" not in bundle_headers and "authorization" not in bundle_headers
    gql_headers = dict(fake.header_calls[-1][1])
    assert gql_headers.get("authorization", "").startswith("Bearer ")
    assert gql_headers["x-csrf-token"] == "csrf-secret"
    assert gql_headers["x-twitter-auth-type"] == "OAuth2Session"
    assert gql_headers["x-twitter-active-user"] == "yes"
    assert "Cookie" in gql_headers


# ------------------------------------- ТЗ-43B: метрики сессии для ВСЕХ постов
def _sess_post(tid, **metrics):
    """Пост сессионного транспорта с метриками (контракт ТЗ-43A)."""
    p = {"tweet_id": str(tid), "published_at_utc": "2026-09-21T10:00:00",
         "published_src": "x_session", "text": "t", "views": 100, "likes": 1,
         "reposts": 0, "quotes": 0}
    p.update(metrics)
    return p


def test_update_post_gets_session_metrics(con):
    """1) Пост УЖЕ в базе (upd) с метриками сессии -> строка в metric_snapshot.

    Обратная половина: первый заход без метрик строки не создаёт.
    """
    sid = _seed_source(con, "nasa")
    xcollect.store_posts(con, sid, [{"tweet_id": "555", "text": "nitter",
                                     "published_at_utc": "2026-09-21T09:00:00"}])
    assert con.execute("SELECT COUNT(*) FROM metric_snapshot"
                       " WHERE external_id='555'").fetchone()[0] == 0

    res = xcollect.store_posts(con, sid, [_sess_post("555", views=777, likes=5,
                                                     reposts=2, quotes=1)])
    assert res == {"new": 0, "upd": 1, "skipped": 0}
    row = con.execute(
        "SELECT views, likes, reposts, quotes, source FROM metric_snapshot"
        " WHERE external_id='555'").fetchone()
    assert row["source"] == "x_session"
    assert (row["views"], row["likes"], row["reposts"], row["quotes"]) == (777, 5, 2, 1)


def test_session_metric_series_grows_across_runs(con, monkeypatch):
    """2a) Два прогона с разным captured_at дают ДВЕ записи (ряд растёт)."""
    sid = _seed_source(con, "nasa")
    stamps = iter(["2026-09-21 12:00:00", "2026-09-21 12:01:00"])
    monkeypatch.setattr(db, "now_iso", lambda: next(stamps))
    xcollect.store_posts(con, sid, [_sess_post("600", views=10)])
    xcollect.store_posts(con, sid, [_sess_post("600", views=20)])
    rows = con.execute("SELECT captured_at, views FROM metric_snapshot"
                       " WHERE external_id='600' ORDER BY captured_at").fetchall()
    assert [r["captured_at"] for r in rows] == ["2026-09-21 12:00:00",
                                                "2026-09-21 12:01:00"]
    assert [r["views"] for r in rows] == [10, 20]


def test_session_metric_same_captured_at_updates_one_row(con, monkeypatch):
    """2b) В пределах одного captured_at строка ОБНОВЛЯЕТСЯ, а не плодится."""
    sid = _seed_source(con, "nasa")
    monkeypatch.setattr(db, "now_iso", lambda: "2026-09-21 12:00:00")
    xcollect.store_posts(con, sid, [_sess_post("601", views=10)])
    xcollect.store_posts(con, sid, [_sess_post("601", views=33)])
    rows = con.execute("SELECT views FROM metric_snapshot"
                       " WHERE external_id='601'").fetchall()
    assert len(rows) == 1 and rows[0]["views"] == 33


def test_duplicate_tweet_in_one_run_writes_one_row(con):
    """3) Дубликат tweet_id в одном ответе -> одна запись, не две."""
    sid = _seed_source(con, "nasa")
    p = _sess_post("700", views=5)
    xcollect.store_posts(con, sid, [p, dict(p)])
    n = con.execute("SELECT COUNT(*) FROM metric_snapshot"
                    " WHERE external_id='700'").fetchone()[0]
    assert n == 1


def test_session_metrics_cap_and_run_log(con, monkeypatch):
    """4) Упор в X_SESSION_METRICS_MAX_PER_RUN -> ровно потолок + строка run_log."""
    monkeypatch.setattr(config, "X_SESSION_METRICS_MAX_PER_RUN", 3)
    sid = _seed_source(con, "nasa")
    run_id = db.start_run(con, "collect:A")
    posts = [_sess_post(800 + i, views=i) for i in range(5)]
    xcollect.store_posts(con, sid, posts, run_id=run_id)
    n = con.execute("SELECT COUNT(*) FROM metric_snapshot"
                    " WHERE source='x_session'").fetchone()[0]
    assert n == 3, f"потолок не сработал: записей {n}"
    msgs = [r["msg"] for r in con.execute("SELECT msg FROM run_log")]
    assert any("потолок" in m and "3" in m for m in msgs), msgs


def test_nitter_posts_create_no_session_snapshots(con):
    """5) У постов Nitter метрик-ключей нет -> снапшотов не создают."""
    sid = _seed_source(con, "nasa")
    xcollect.store_posts(con, sid, [
        {"tweet_id": "900", "text": "nitter", "published_src": "rss",
         "published_at_utc": "2026-09-21T09:00:00"}])
    assert con.execute("SELECT COUNT(*) FROM metric_snapshot").fetchone()[0] == 0
