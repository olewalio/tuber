"""ТЗ B: веб-страница HypeParser (``tuber/platforms/x/hype_web.py``).

Сеть не используется: сессионный транспорт подменяется, как в тестах ТЗ A2.
Проверяем предохранители (один поиск, пауза, лимиты страниц/постов/бюджета),
кэш, сортировку, маршруты и «человеческие» страницы ошибок без трассировки.
"""
from __future__ import annotations

import contextlib
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from tests.x.mocking import VClock
from tests.x.test_session_transport import FakeX, tweet, write_session
from tests.x.test_tz_a2_search import search_payload, write_qids_full
from tuber.platforms.x import config
from tuber.platforms.x import hype_web as hype
from tuber.platforms.x import session as xsession


# ------------------------------------------------------------------ помощники
#: Twitter-формат «created_at»: «Wed Oct 10 20:19:24 +0000 2018». Собираем руками,
#: чтобы не зависеть от локали (``%a``/``%b`` в C-локали или другой среде).
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def twitter_created(dt: datetime) -> str:
    return (f"{_WEEKDAYS[dt.weekday()]} {_MONTHS[dt.month - 1]} {dt.day:02d} "
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} +0000 {dt.year}")


def ago(hours: float) -> str:
    """Twitter-время момента «теперь минус hours» (без хрупких литералов)."""
    return twitter_created(datetime.now(timezone.utc) - timedelta(hours=hours))


def make_session(con, tmp_path, transport, session_clock):
    s = xsession.XSessionTransport(
        session_file=write_session(tmp_path),
        qids_file=str(write_qids_full(tmp_path)),
        transport=transport, clock=session_clock, sleeper=session_clock.sleep,
        con=con)
    s._xclid_failed = True   # генератор x-client-transaction-id в тестах не строим
    return s


def make_app(con, tmp_path, transport, *, session_clock=None, **kw):
    session = make_session(con, tmp_path, transport, session_clock or VClock())
    kw.setdefault("db_path", config.DB_PATH)
    return hype.HypeApp(session_factory=lambda: session, **kw)


@contextlib.contextmanager
def serve(app):
    httpd = hype.build_server("127.0.0.1", 0, app=app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(3)


def get(url):
    """GET → (status, body). Коды ошибок не считаем исключением."""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


class CursorTransport:
    """Транспорт с уникальным курсором на каждый запрос (проверка пагинации)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, headers):
        self.calls += 1
        tw = tweet(f"90{self.calls}", "nasa", text=f"post {self.calls}",
                   views=1000 + self.calls, followers=100)
        payload = search_payload([tw], cursor=f"CURSOR-{self.calls}")
        return 200, {}, json.dumps(payload)


# ------------------------------------------------------------------ маршруты
def test_routes_index_robots_health_404(con, tmp_path):
    fake = FakeX(routes={})
    app = make_app(con, tmp_path, fake)
    with serve(app) as base:
        status, body = get(base + "/")
        assert status == 200
        assert "Найти" in body and 'name="q"' in body
        assert 'name="days"' in body and 'name="product"' in body
        assert 'name="only_views"' in body and 'max="5"' in body

        status, body = get(base + "/robots.txt")
        assert status == 200
        assert body == "User-agent: *\nDisallow: /\n"

        status, body = get(base + "/healthz")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert data["version"] == hype.APP_VERSION
        assert data["session_available"] is True
        assert data["search_daily"]["cap"] == int(config.X_SESSION_SEARCH_DAILY_CAP)

        status, body = get(base + "/nope")
        assert status == 404
        assert "Ошибка 404" in body


def test_search_page_has_sorted_table_with_ratio(con, tmp_path):
    hi = tweet("1", "big", text="very viral", views=1000, followers=100)
    lo = tweet("2", "small", text="meh", views=50, followers=1000)
    no = tweet("3", "ghost", text="no views", views=None, followers=500)
    fake = FakeX(routes={"SearchTimeline": search_payload([lo, no, hi])})
    app = make_app(con, tmp_path, fake)
    with serve(app) as base:
        status, body = get(base + "/search?q=AI%20lang%3Aen&pages=1")
    assert status == 200
    assert "10.0000" in body       # 1000 / 100
    assert "0.0500" in body        # 50 / 1000
    assert "нет данных" in body    # views is None
    # Порядок: коэффициент 10 выше коэффициента 0.05, «нет данных» — в самом низу.
    assert body.index("10.0000") < body.index("0.0500") < body.index("нет данных")
    assert "https://x.com/big/status/1" in body


def test_cache_second_identical_query_hits_no_x(con, tmp_path):
    fake = FakeX(routes={"SearchTimeline": search_payload(
        [tweet("1", "nasa", views=100, followers=10)])})
    app = make_app(con, tmp_path, fake)
    first = app.search("AI lang:en", pages=1)
    second = app.search("AI lang:en", pages=1)
    assert first["cached"] is False and second["cached"] is True
    assert fake.gql_hits.get("SearchTimeline") == 1, "повтор не должен идти в X"
    assert app.cache_size() == 1


def test_cache_expires_after_ttl(con, tmp_path):
    vclock = VClock()
    fake = FakeX(routes={"SearchTimeline": search_payload(
        [tweet("1", "nasa", views=100, followers=10)])})
    app = make_app(con, tmp_path, fake, clock=vclock, cache_ttl=600.0)
    app.search("q", pages=1)
    vclock.advance(601)
    app.search("q", pages=1)
    assert fake.gql_hits.get("SearchTimeline") == 2


def test_min_interval_between_searches(con, tmp_path):
    vclock = VClock()
    fake = FakeX(routes={"SearchTimeline": search_payload(
        [tweet("1", "nasa", views=100, followers=10)])})
    app = make_app(con, tmp_path, fake, clock=vclock, min_interval=10.0)
    app.search("first", pages=1)
    with pytest.raises(hype.SearchTooSoon):
        app.search("second", pages=1)
    vclock.advance(11)
    assert app.search("second", pages=1)["cached"] is False


def test_pages_clamped_to_five(con, tmp_path):
    transport = CursorTransport()
    app = make_app(con, tmp_path, transport)
    result = app.search("q", pages=99)
    assert result["pages"] == hype.MAX_PAGES == 5
    assert transport.calls == 5, "должно быть не больше 5 страниц к X"
    assert result["stats"]["pages"] == 5


def test_posts_truncated_to_max(con, tmp_path):
    posts = [tweet(f"{i}", "nasa", views=100 + i, followers=10) for i in range(250)]
    fake = FakeX(routes={"SearchTimeline": search_payload(posts)})
    app = make_app(con, tmp_path, fake)
    result = app.search("q", pages=1)
    assert result["stats"]["posts"] == hype.MAX_POSTS == 200


def test_search_daily_cap_raises_human_error(con, tmp_path, monkeypatch):
    fake = FakeX(routes={})
    app = make_app(con, tmp_path, fake)
    monkeypatch.setattr(app, "_daily_budget", lambda: {
        "count": 100, "cap": 100, "exhausted": True, "free_at": "2026-10-08T00:00:00"})
    with pytest.raises(hype.BudgetExhausted) as err:
        app.search("q", pages=1)
    assert "лимит" in str(err.value).lower()
    assert fake.gql_hits == {}, "при исчерпанном бюджете запрос не уходит"


def test_session_unavailable_gives_503_without_traceback(con, tmp_path):
    app = hype.HypeApp(session_factory=lambda: None, db_path=config.DB_PATH)
    with serve(app) as base:
        status, body = get(base + "/search?q=AI&pages=1")
    assert status == 503
    assert "Сессия" in body and "Traceback" not in body


def test_transport_error_gives_502_without_traceback(con, tmp_path):
    def boom_transport(url, headers):
        return 500, {}, "server error"

    app = make_app(con, tmp_path, boom_transport)
    with serve(app) as base:
        status, body = get(base + "/search?q=AI&pages=1")
    assert status == 502
    assert "Traceback" not in body and "<pre>" not in body
    assert "транспорт" in body.lower()


def test_days_filter_and_only_views(con, tmp_path):
    old = tweet("1", "old", text="old", views=100, followers=10,
                created=ago(24 * 30))
    fresh = tweet("2", "fresh", text="fresh", views=200, followers=10,
                  created=ago(12))
    noviews = tweet("3", "noviews", text="x", views=None, followers=10,
                    created=ago(12))
    fake = FakeX(routes={"SearchTimeline": search_payload([old, fresh, noviews])})
    app = make_app(con, tmp_path, fake)
    result = app.search("q", pages=1, days=1, only_views=True)
    ids = {p["author"] for p in result["posts"]}
    assert ids == {"fresh"}, "old отфильтрован по дате, noviews — по просмотрам"


def test_write_path_uses_store_once(con, tmp_path):
    fake = FakeX(routes={"SearchTimeline": search_payload(
        [tweet("777", "nasa", text="hello", views=100, followers=10)])})
    app = make_app(con, tmp_path, fake, write_posts=True)
    app.search("q write", pages=1)
    rows = con.execute(
        "SELECT post_id, run_id, views, author_subs FROM x_search_post").fetchall()
    assert len(rows) == 1 and rows[0]["post_id"] == "777"
    assert rows[0]["views"] == 100 and rows[0]["author_subs"] == 10
    run = con.execute(
        "SELECT status, posts FROM x_search_run WHERE id=?",
        (rows[0]["run_id"],)).fetchone()
    assert run["status"] == "ok" and run["posts"] == 1


def test_empty_query_and_bad_params(con, tmp_path):
    app = make_app(con, tmp_path, FakeX(routes={}))
    with pytest.raises(hype.BadRequest):
        app.search("   ", pages=1)
    with pytest.raises(hype.BadRequest):
        app.search("q", pages="x")
    with pytest.raises(hype.BadRequest):
        app.search("q", pages=1, product="Nope")
    with pytest.raises(hype.BadRequest):
        app.search("q", pages=1, days="not-a-number")


def test_sort_posts_none_last():
    rows = [{"ratio": None, "views": 999}, {"ratio": 1.0, "views": 1},
            {"ratio": 5.0, "views": 2}, {"ratio": None, "views": None}]
    out = hype.sort_posts(rows)
    assert [r["ratio"] for r in out] == [5.0, 1.0, None, None]


def test_public_host_requires_explicit_flag():
    assert hype.main(["--host", "0.0.0.0", "--port", "0"]) == 2


def test_error_page_escapes_message():
    body = hype.render_error(400, "<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_session_has_no_live_guard():
    """Защита от случайной живой сети в тестах (как в остальных тестах X)."""
    assert config.X_SESSION_ENABLED is False


def test_busy_lock_blocks_second_search(con, tmp_path):
    app = make_app(con, tmp_path, FakeX(routes={}))
    app._lock.acquire()
    try:
        with pytest.raises(hype.SearchBusy) as err:
            app.search("q", pages=1)
        assert err.value.status == 429
        assert "уже выполняется" in err.value.message
    finally:
        app._lock.release()


class RecordingSession:
    """Транспорт-обёртка: запоминает аргументы ``search`` и эмулирует on_page."""

    def __init__(self, posts):
        self.posts = posts
        self.calls = []

    def search(self, query, **kw):
        self.calls.append((query, kw))
        on_page = kw.get("on_page")
        if on_page:
            on_page(1, self.posts, None)
        return self.posts


def test_passed_limits_and_timeout_to_transport(con, tmp_path):
    rec = RecordingSession([tweet("1", "nasa", views=100, followers=10)])
    app = hype.HypeApp(session_factory=lambda: rec, db_path=config.DB_PATH)
    app.search("q", pages=7)
    _query, kw = rec.calls[0]
    assert kw["max_pages"] == hype.MAX_PAGES == 5
    assert kw["max_posts"] == hype.MAX_POSTS == 200
    assert kw["max_seconds"] == hype.SEARCH_TIMEOUT_SEC == 180.0
