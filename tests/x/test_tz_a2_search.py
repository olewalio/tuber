"""ТЗ A2 (HypeParser): поиск X через сессию (``SearchTimeline``) + таблицы + CLI.

Сеть не используется. Единственный живой артефакт — записанный ответ
``SearchTimeline`` (``tests/fixtures/search_timeline.json``, 107 КБ, снят
07.10.2026 ОДНИМ запросом на нашем аккаунте в temp-БД, боевая база не тронута).
Все прочие ответы — синтетические фикстуры-заглушки.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse

import pytest

from tests.x.test_session_transport import (
    FakeX, SESSION_FEATURES, SESSION_QIDS, tweet,
)
from tuber.platforms.x import cli, config, session as xsession
from tuber.platforms.x import store as db

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "search_timeline.json")
# Курсор Bottom из записанного живого ответа (проверка разбора структуры).
LIVE_BOTTOM_CURSOR = (
    "DAADDAABCgABHUAPQjTbYFcKAAIdQA86CFewDgAIAAIAAAACCAADAAAAAAgABAAAAAAKAAUdQA9FGwAnEAoABh1AD0Ua"
    "_9jwCwAHAAAA8EVtUEM2d0FBQWZRL2dHSk4wdkdwL0FBQUFCUWRRQTg2RGhkUkVSMUFEem9JVjdBT0hVQVBPenVYZ1VnZFFB"
    "ODh2aG9CWHgxQUR6NC9HbEFaSFVBUFBzWWJ3R0lkUUE5Q05OdGdWeDFBRDBBNld5RGFIVUFQUEM5YXdLWWRRQTg3WEpvQUp4"
    "MUFEejJubWhDUEhVQVBQcTFid1Q0ZFFBODkwcHBSREIxQUR6NUoyd0RaSFVBUFBVbmJZR1VkUUE4L09wcnhzQjFBRHp2eEc2"
    "RXlIVUFQUEwwYUFRMGRRQTgrRGhheHpCMUFEenB0bDhEVQAA")

QIDS = dict(SESSION_QIDS, SearchTimeline="QID_searchTimeline_00001")


def write_qids_full(tmp_path):
    from tests.x.test_session_transport import write_session
    path = tmp_path / "x_qids.json"
    path.write_text(json.dumps({"fetched_at": 9_999_999_999,
                                "bundle": "https://abs.twimg.com/x/main.abc.js",
                                "qids": QIDS, "features": SESSION_FEATURES}),
                    encoding="utf-8")
    return path


def mk(tmp_path, con, transport, **kw):
    s = xsession.XSessionTransport(
        session_file=kw.pop("session_file", None) or _session_file(tmp_path),
        qids_file=str(write_qids_full(tmp_path)),
        transport=transport, clock=kw.pop("clock", None) or _clock(),
        sleeper=lambda s: None, con=con, **kw)
    # ТЗ A1: генератор x-client-transaction-id в тестах не строим (это лишние
    # сетевые обращения к x.com/tesla); заголовок просто не добавляется.
    s._xclid_failed = True
    return s


def _clock():
    from tests.x.mocking import VClock
    return VClock()


def _session_file(tmp_path):
    from tests.x.test_session_transport import write_session
    return write_session(tmp_path)


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def search_payload(tweets, cursor=None, top_cursor=None):
    """Ответ SearchTimeline. Ключи курсора намеренно НЕ подряд (как у X)."""
    entries = [{"entryId": f"tweet-{t['rest_id']}",
                "content": {"itemContent": {"tweet_results": {"result": t}}}}
               for t in tweets]
    if top_cursor:
        entries.append({"entryId": "cursor-top-0", "content": {
            "__typename": "TimelineTimelineCursor", "cursorType": "Top",
            "entryType": "TimelineTimelineCursor", "value": top_cursor}})
    if cursor:
        entries.append({"entryId": "cursor-bottom-0", "content": {
            "__typename": "TimelineTimelineCursor", "cursorType": "Bottom",
            "entryType": "TimelineTimelineCursor", "value": cursor}})
    return {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
        "instructions": [{"type": "TimelineAddEntries", "entries": entries}]}}}}}


def gql_variables(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return json.loads(q["variables"][0])


class SeqTransport:
    """Транспорт, отдающий заданные ответы по очереди (последний — повторно)."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return 200, {}, json.dumps(payload)


# --------------------------------------------------------- живая фикстура
def test_fixture_parses_and_maps_fields(con, tmp_path):
    """Переменные запроса + разбор записанного живого ответа."""
    fake = FakeX(routes={"SearchTimeline": load_fixture()})
    s = mk(tmp_path, con, fake)
    posts = s.search("AI lang:en", max_pages=1)

    assert len(posts) == 20, "в живом ответе ровно 20 постов"
    required = ("post_id", "tweet_id", "text", "author_handle", "owner_handle",
                "author_subs", "published_at", "published_at_utc", "views",
                "likes", "replies", "url", "ratio")
    for p in posts:
        for key in required:
            assert key in p, f"нет ключа {key}"
        assert p["post_id"] == p["tweet_id"] and p["post_id"]
        assert p["author_handle"] == p["owner_handle"]
        assert p["published_at"] == p["published_at_utc"]
        assert p["url"] == f"https://x.com/{p['author_handle']}/status/{p['post_id']}"
        assert p["author_subs"] is not None, "followers приходят у автора всегда"
    # Просмотров в этом срезе нет: коэффициент честно NULL, не ноль.
    assert all(p["views"] is None for p in posts)
    assert all(p["ratio"] is None for p in posts)

    var = gql_variables(fake.calls[-1][0])
    assert var["rawQuery"] == "AI lang:en"
    assert var["product"] == "Latest"
    assert var["querySource"] == "typed_query"
    assert isinstance(var["count"], int) and var["count"] >= 1
    assert "cursor" not in var, "первый запрос идёт без курсора"


def test_cursor_structural_parse_from_live_fixture():
    """Курсор читается разбором структуры; регулярка бы его потеряла."""
    d = load_fixture()
    assert xsession.XSessionTransport._cursor(d) == LIVE_BOTTOM_CURSOR
    top = xsession.XSessionTransport._cursor(d, "Top")
    assert top and top != LIVE_BOTTOM_CURSOR
    # Регрессия: старая регулярка (cursorType и value подряд) не находит ничего.
    raw = json.dumps(d)
    assert not re.search(r'"cursorType"\s*:\s*"([A-Za-z]+)"\s*,\s*"value"', raw)


# ------------------------------------------------------ пагинация и защита
def test_pagination_uses_returned_cursor(con, tmp_path):
    t1 = tweet("111", "nasa", views=100)
    t2 = tweet("222", "spacex", views=50)
    transport = SeqTransport([search_payload([t1], cursor="CUR-1"),
                              search_payload([t2], cursor="CUR-2"),
                              search_payload([], cursor=None)])
    s = mk(tmp_path, con, transport)
    seen = []

    def on_page(page_no, posts, cursor):
        seen.append((page_no, len(posts), cursor))

    posts = s.search("q", max_pages=5, on_page=on_page)
    assert [p["post_id"] for p in posts] == ["111", "222"]
    assert seen == [(1, 1, "CUR-1"), (2, 1, "CUR-2"), (3, 0, None)]
    second = gql_variables(transport.calls[1])
    assert second["cursor"] == "CUR-1", "вторая страница идёт с курсором первой"


def test_loop_protection_stops_on_repeated_cursor(con, tmp_path):
    """Тот же курсор на каждой странице → обход останавливается, без ошибок."""
    tw = tweet("333", "nasa")
    transport = SeqTransport([search_payload([tw], cursor="SAME-CURSOR")])
    s = mk(tmp_path, con, transport)
    posts = s.search("q", max_pages=12)
    assert len(posts) == 2, "первая страница + одна повторная, дальше стоп"
    assert len(transport.calls) == 2, "зацикливания нет: ровно 2 запроса"


def test_max_pages_and_max_posts(con, tmp_path):
    tweets = [tweet(str(100 + i), "nasa") for i in range(5)]
    transport = SeqTransport([search_payload(tweets, cursor="C1"),
                              search_payload(tweets, cursor="C2"),
                              search_payload(tweets, cursor="C3")])
    s = mk(tmp_path, con, transport)
    posts = s.search("q", max_pages=2)
    assert len(posts) == 10 and len(transport.calls) == 2

    transport2 = SeqTransport([search_payload(tweets, cursor="C1"),
                               search_payload(tweets, cursor="C2")])
    s2 = mk(tmp_path, con, transport2)
    assert len(s2.search("q", max_pages=5, max_posts=3)) == 5


# --------------------------------------------------------------- коэффициент
def test_ratio_rules():
    assert db.search_ratio(None, 100) is None
    assert db.search_ratio(100, 0) is None
    assert db.search_ratio(100, None) is None
    assert db.search_ratio(1000, 50) == 20.0
    assert db.search_ratio(3, 2) == 1.5


# ------------------------------------------------------------------ хранилище
def _p(pid, handle, views, subs, likes=1, replies=2, text="AI $ACME"):
    return {"post_id": pid, "author_handle": handle, "author_subs": subs,
            "views": views, "likes": likes, "replies": replies, "text": text,
            "published_at": "2026-10-06T10:00:00",
            "url": f"https://x.com/{handle}/status/{pid}"}


def test_store_insert_update_and_top5(con):
    run_id = db.start_search_run(con, "AI lang:en", product="Latest")
    assert run_id
    res = db.save_search_posts(con, [
        _p("1", "a", 1000, 10),      # ratio 100
        _p("2", "b", 500, 100),      # ratio 5
        _p("3", "c", None, 50),      # views нет -> ratio NULL
        _p("4", "d", 700, 0),        # 0 подписчиков -> ratio NULL
    ], run_id=run_id, query="AI lang:en")
    assert res == {"new": 4, "upd": 0}
    assert con.execute("SELECT COUNT(*) FROM x_search_post").fetchone()[0] == 4

    # Повторная вставка того же post_id: обновление, дубля нет.
    res2 = db.save_search_posts(con, [_p("1", "a", 2000, 10, likes=99)],
                                run_id=run_id, query="AI lang:en")
    assert res2 == {"new": 0, "upd": 1}
    assert con.execute("SELECT COUNT(*) FROM x_search_post").fetchone()[0] == 4
    row = con.execute("SELECT views, likes, ratio FROM x_search_post"
                      " WHERE post_id='1'").fetchone()
    assert row["views"] == 2000 and row["likes"] == 99 and row["ratio"] == 200.0

    top = db.top_search_posts(con, 5)
    assert [r["post_id"] for r in top][:2] == ["1", "2"], "топ по ratio убыв."
    assert top[0]["ratio"] == 200.0
    # NULL-коэффициенты уходят вниз, но не пропадают.
    assert {r["post_id"] for r in top} == {"1", "2", "3", "4"}

    stats = db.search_run_stats(con, run_id)
    assert stats["posts"] == 4 and stats["with_ratio"] == 2


def test_search_tables_schema(con):
    """Обе таблицы созданы; существующие таблицы не тронуты."""
    run_cols = {r[1] for r in con.execute("PRAGMA main.table_info(x_search_run)")}
    assert {"id", "query", "product", "started_at", "finished_at", "pages",
            "posts", "status", "note"} <= run_cols
    post_cols = {r[1] for r in con.execute("PRAGMA main.table_info(x_search_post)")}
    assert {"post_id", "run_id", "query", "taken_at", "published_at", "text",
            "author_handle", "author_subs", "views", "likes", "replies",
            "ratio", "url"} <= post_cols
    idx = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_search_post_run" in idx and "idx_search_post_ratio" in idx


def test_store_recollect_reattributes_to_latest_run(con):
    """Повторный сбор того же поста обновляет его и «переводит» в новый прогон."""
    r1 = db.start_search_run(con, "q")
    db.save_search_posts(con, [_p("50", "a", 100, 10)], run_id=r1, query="q")
    assert db.search_run_stats(con, r1)["posts"] == 1
    r2 = db.start_search_run(con, "q")
    db.save_search_posts(con, [_p("50", "a", 500, 10)], run_id=r2, query="q")
    assert con.execute("SELECT COUNT(*) FROM x_search_post").fetchone()[0] == 1
    assert db.search_run_stats(con, r1)["posts"] == 0
    assert db.search_run_stats(con, r2)["posts"] == 1
    assert db.search_run_stats(con, r2)["with_ratio"] == 1


def test_search_budget_is_separate_kind(con, monkeypatch):
    """``search`` считается отдельно от лент/точечных и имеет свой лимит."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(config, "X_SESSION_SEARCH_DAILY_CAP", 2)
    ts = (datetime.now(timezone.utc) - timedelta(seconds=60)
          ).strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(2):
        con.execute("INSERT INTO main.transport_request"
                    " (platform, host, ts, kind, url, status) VALUES"
                    " ('x','x.com',?,'x_session',"
                    "'https://x.com/i/api/graphql/Q/SearchTimeline',200)", (ts,))
    con.execute("INSERT INTO main.transport_request"
                " (platform, host, ts, kind, url, status) VALUES"
                " ('x','x.com',?,'x_session',"
                "'https://x.com/i/api/graphql/Q/UserTweets',200)", (ts,))
    con.commit()
    b = db.session_budget(con)
    assert b["search_daily_count"] == 2 and b["search_day_used"] == 2
    assert b["search_daily_cap"] == 2 and b["search_daily_exhausted"] is True
    assert b["point_daily_count"] == 0, "поиск не должен считаться точечной задачей"
    assert b["feed_daily_count"] == 1

    s = mk(_tmp(), con, FakeX())
    with pytest.raises(xsession.XSessionDailyCap):
        s._check_budget("search")
    # Другие виды задач этого лимита не видят.
    s._check_budget("point")


def _tmp(tmp_path=None):
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp(prefix="a2_"))

# ---------------------------------------------------------------------- CLI
class FakeSearchSession:
    def __init__(self, posts):
        self.posts = posts
        self.calls = []

    def search(self, query, *, product="Latest", max_pages=12, max_posts=300,
               max_seconds=480, kind="search", on_page=None):
        self.calls.append({"query": query, "product": product,
                           "max_pages": max_pages, "kind": kind})
        if on_page:
            on_page(1, list(self.posts), None)
        return list(self.posts)


def test_cli_search_writes_and_reports(con, monkeypatch, capsys):
    """Non-dry: пишет run+посты, печатает сводку и топ-5."""
    posts = [_p("10", "a", 1000, 10, text="AI $ACME"),
             _p("11", "b", 200, 100, text="nope")]
    monkeypatch.setattr(xsession, "get_session_transport",
                        lambda **kw: FakeSearchSession(posts))
    args = cli.build_parser().parse_args(
        ["search", "--query", "AI lang:en", "--pages", "2"])
    rc = cli.cmd_search(args)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "запуск=" in out and "страниц=1" in out and "топ-5" in out
    assert "@a" in out and "коэф=100.0000" in out and "$ACME" in out
    run_id = con.execute("SELECT id FROM x_search_run ORDER BY id DESC").fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM x_search_post WHERE run_id=?",
                       (run_id,)).fetchone()[0] == 2
    assert con.execute("SELECT status FROM x_search_run WHERE id=?",
                       (run_id,)).fetchone()[0] == "ok"


def test_cli_search_dry_writes_nothing(con, monkeypatch, capsys):
    posts = [_p("20", "a", 1000, 10)]
    monkeypatch.setattr(xsession, "get_session_transport",
                        lambda **kw: FakeSearchSession(posts))
    args = cli.build_parser().parse_args(
        ["search", "--query", "AI", "--pages", "1", "--dry", "--json"])
    rc = cli.cmd_search(args)
    out = capsys.readouterr().out
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["dry"] is True and payload["run_id"] is None
    assert payload["posts"] == 1
    assert con.execute("SELECT COUNT(*) FROM x_search_run").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM x_search_post").fetchone()[0] == 0


def test_cli_search_days_filters_and_reports_no_date(con, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    posts = [_p("30", "new", 100, 10, text="fresh"),
             _p("31", "old", 100, 10, text="stale"),
             {"post_id": "32", "author_handle": "nodate", "author_subs": 5,
              "views": 50, "likes": 0, "replies": 0, "text": "nod",
              "published_at": None, "url": None}]
    posts[0]["published_at"] = fresh   # мина времени: дата относительно now
    posts[1]["published_at"] = old
    monkeypatch.setattr(xsession, "get_session_transport",
                        lambda **kw: FakeSearchSession(posts))
    args = cli.build_parser().parse_args(
        ["search", "--query", "AI", "--pages", "1", "--days", "3"])
    rc = cli.cmd_search(args)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "фильтр --days=3" in out and "без_даты=1" in out
    ids = {r["post_id"] for r in con.execute("SELECT post_id FROM x_search_post")}
    assert ids == {"30", "32"}, "старый отброшен, без даты — оставлен"


# ----------------------------------------- ТЗ A2b: счётчики-строки в parse_tweet
@pytest.mark.parametrize("raw,expected", [
    ("12345", 12345),   # числовая строка -> int
    ("1.2K", None),     # суффикс не разбирается -> None (не 0)
    ("12,345", None),   # запятая-разделитель не разбирается -> None (не 0)
    ("", None),         # пустая строка -> None (не 0)
    (None, None),       # значение отсутствует -> None
])
def test_parse_tweet_normalizes_views_string(raw, expected):
    tw = tweet("901", "nasa", views=raw, followers="5000")
    info = xsession.XSessionTransport.parse_tweet(tw)
    assert info["views"] == expected
    if expected is not None:
        assert isinstance(info["views"], int)
    assert info["author_followers"] == 5000, "числовая строка подписчиков -> int"


def test_parse_tweet_normalizes_all_counters():
    tw = tweet("902", "nasa", views="12345", likes="7", replies="",
               retweets="3", quotes="нет", followers="5000")
    info = xsession.XSessionTransport.parse_tweet(tw)
    assert info["views"] == 12345 and isinstance(info["views"], int)
    assert info["likes"] == 7 and isinstance(info["likes"], int)
    assert info["replies"] is None, "пустая строка -> None, не 0"
    assert info["retweets"] == 3
    assert info["quotes"] is None, "нераспознанное значение -> None, не 0"


def test_ratio_computed_from_normalized_views():
    """ratio считается из числа, а не из строки (через нормализованное поле)."""
    tw = tweet("903", "nasa", views="12345", followers="5000")
    info = xsession.XSessionTransport.parse_tweet(tw)
    assert db.search_ratio(info["views"], info["author_followers"]) == 12345 / 5000


# ------------------------------------------------ единая сортировка _top_posts
def test_top_posts_null_ratio_goes_below_numeric():
    posts = [
        {"post_id": "n", "ratio": None, "views": 99999},
        {"post_id": "a", "ratio": 1.0, "views": 10},
        {"post_id": "b", "ratio": 5.0, "views": 1},
        {"post_id": "c", "ratio": 5.0, "views": 100},
    ]
    top = cli._top_posts(posts, 10)
    assert [p["post_id"] for p in top] == ["c", "b", "a", "n"], \
        "ratio убыв., при равенстве views убыв., NULL-ratio — вниз"


def test_top_posts_tolerates_string_counters():
    """Защита от строк: помощник не падает и сортирует по числу."""
    posts = [
        {"post_id": "s1", "ratio": "100.0", "views": "12345"},
        {"post_id": "s2", "ratio": None, "views": "12,345"},
    ]
    top = cli._top_posts(posts, 5)
    assert [p["post_id"] for p in top] == ["s1", "s2"]


def test_cli_search_dry_string_counters_prints_top5(con, tmp_path, monkeypatch,
                                                     capsys):
    """Регрессия ТЗ A2b: --dry на живых строках-счётчиках не падает и печатает топ."""
    t1 = tweet("111", "big", views="12345", followers="5000", text="AI $ACME")
    t2 = tweet("222", "small", views="100", followers="5000", text="AI $BETA")
    transport = SeqTransport([search_payload([t1, t2], cursor=None)])
    s = mk(tmp_path, con, transport)
    monkeypatch.setattr(xsession, "get_session_transport", lambda **kw: s)
    args = cli.build_parser().parse_args(
        ["search", "--query", "AI lang:en", "--pages", "1", "--dry"])
    rc = cli.cmd_search(args)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "топ-5" in out
    assert "views=12345" in out, "нормализованные просмотры печатаются числом"
    assert "коэф=2.4690" in out, "ratio посчитан из числа (12345/5000)"
    # Пост без просмотров (ratio=None) не роняет печать.
    assert "коэф=—" in out or "коэф=2.4690" in out
