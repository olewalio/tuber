"""Тесты транспорта YouTube (tuber.yt). Сеть не используется: только мок."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from tuber.platforms.youtube import config, store as db
from tuber.platforms.youtube import api as yt


class FakeResp:
    """Минимальный ответ requests."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def text(self):
        return str(self._payload)


class FakeSession:
    """Подменяет requests.Session.get, записывает все запросы."""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self._handler(dict(params or {}))


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "yt_test.db")
    db.init_db(c)
    yield c
    c.close()


def _client(handler, keys=("k1", "k2"), conn=None, sleep=None):
    session = FakeSession(handler)
    client = yt.YouTubeClient(
        keys=list(keys),
        conn=conn,
        session=session,
        min_interval=0,
        sleep=sleep or (lambda _s: None),
    )
    return client, session


def test_videos_by_ids_parses_items(conn):
    def handler(params):
        return FakeResp(200, {"items": [{"id": "a"}, {"id": "b"}]})

    client, session = _client(handler, conn=conn)
    items = client.videos_by_ids(["a", "b"])
    assert items == [{"id": "a"}, {"id": "b"}]
    # Ключ передавался, но в БД попал только несекретный key_id.
    assert all("key" in c["params"] for c in session.calls)
    logged = conn.execute("SELECT DISTINCT key_id FROM quota_log").fetchall()
    key_ids = {r["key_id"] for r in logged}
    assert key_ids and all("k1" not in kid and "k2" not in kid for kid in key_ids)


def test_quota_log_units_recorded(conn):
    def handler(params):
        return FakeResp(200, {"items": []})

    client, _ = _client(handler, conn=conn)
    client.search_videos("AI news", max_pages=1)
    total_units = conn.execute("SELECT SUM(units) AS u FROM quota_log").fetchone()["u"]
    # probe (1) + search (100).
    assert total_units == 1 + config.COST_SEARCH


def test_http_500_raises_not_none(conn):
    def handler(params):
        return FakeResp(500, {"error": {"code": 500, "message": "backend error"}})

    client, _ = _client(handler, conn=conn)
    with pytest.raises(yt.YouTubeError) as exc:
        client.videos_by_ids(["a"])
    assert exc.value.code == 500
    assert exc.value.endpoint.startswith("videos")


def test_broken_key_rotated_on_403(conn):
    def handler(params):
        if params.get("key") == "k1":
            return FakeResp(
                403,
                {"error": {"errors": [{"reason": "keyInvalid"}]}},
            )
        return FakeResp(200, {"items": [{"id": "ok"}]})

    client, _ = _client(handler, conn=conn)
    items = client.videos_by_ids(["a"])
    assert items == [{"id": "ok"}]
    assert client.bad_keys_count == 1
    assert client.usable_keys_count() == 1


def test_quota_exceeded_moves_to_next_key(conn):
    def handler(params):
        if params.get("key") == "k1":
            return FakeResp(
                403,
                {"error": {"errors": [{"reason": "quotaExceeded"}]}},
            )
        return FakeResp(200, {"items": [{"id": "ok"}]})

    client, _ = _client(handler, conn=conn)
    assert client.videos_by_ids(["a"]) == [{"id": "ok"}]
    assert client.bad_keys_count == 1


def test_429_pauses_and_retries(conn):
    state = {"n": 0}

    def handler(params):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResp(200, {"items": []})  # probe
        if state["n"] == 2:
            return FakeResp(
                429, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
            )
        return FakeResp(200, {"items": [{"id": "v"}]})

    sleeps = []
    client, _ = _client(handler, conn=conn, sleep=sleeps.append)
    items = client.videos_by_ids(["v"])
    assert items == [{"id": "v"}]
    assert client.retry_delay in sleeps


def test_no_usable_keys_raises(conn):
    def handler(params):
        return FakeResp(
            400, {"error": {"errors": [{"reason": "keyInvalid"}]}}
        )

    client, _ = _client(handler, keys=("k1",), conn=conn)
    with pytest.raises(yt.YouTubeError):
        client.videos_by_ids(["a"])


def test_search_videos_paging_and_published_after(conn):
    pages = []

    def handler(params):
        if params.get("part") == "statistics":  # probe
            return FakeResp(200, {"items": []})
        pages.append(dict(params))
        if len(pages) == 1:
            return FakeResp(200, {"items": [{"id": "p1"}], "nextPageToken": "tok"})
        return FakeResp(200, {"items": [{"id": "p2"}]})

    client, _ = _client(handler, conn=conn)
    items = client.search_videos("AI", published_after=1_700_000_000, max_pages=2)
    assert [i["id"] for i in items] == ["p1", "p2"]
    assert pages[0]["publishedAfter"] == "2023-11-14T22:13:20Z"
    assert pages[1].get("pageToken") == "tok"


def test_playlist_items(conn):
    def handler(params):
        if params.get("part") == "statistics":
            return FakeResp(200, {"items": []})
        return FakeResp(200, {"items": [{"id": "pi1"}]})

    client, _ = _client(handler, conn=conn)
    items = client.playlist_items("PL123", max_results=10)
    assert items == [{"id": "pi1"}]


def test_load_keys_reads_hermes_file():
    keys = config.load_keys()
    assert isinstance(keys, list)
    assert all(isinstance(k, str) and k for k in keys)


# --- новые вызовы расширения (этап 9) --------------------------------------


def test_search_channels_type_channel_and_paging(conn):
    pages = []

    def handler(params):
        if params.get("part") == "statistics":  # probe
            return FakeResp(200, {"items": []})
        pages.append(dict(params))
        if len(pages) == 1:
            return FakeResp(200, {"items": [{"id": "c1"}], "nextPageToken": "tok"})
        return FakeResp(200, {"items": [{"id": "c2"}]})

    client, _ = _client(handler, conn=conn)
    items = client.search_channels("AI agents", max_results=2, language="ru", region="RU")
    assert [i["id"] for i in items] == ["c1", "c2"]
    assert pages[0]["type"] == "channel"
    assert pages[0]["relevanceLanguage"] == "ru"
    assert pages[0]["regionCode"] == "RU"
    assert pages[1].get("pageToken") == "tok"
    # 2 страницы поиска по 100 units (пробы ключей не считаем).
    total = conn.execute(
        "SELECT COALESCE(SUM(units),0) AS u FROM quota_log WHERE endpoint='search'"
    ).fetchone()["u"]
    assert total == 2 * config.COST_SEARCH


def test_chart_videos_params_and_cost(conn):
    seen = {}

    def handler(params):
        seen.update(params)
        return FakeResp(200, {"items": [{"id": "v1"}]})

    client, _ = _client(handler, conn=conn)
    items = client.chart_videos("RU", "28", max_results=50)
    assert items == [{"id": "v1"}]
    assert seen["chart"] == "mostPopular"
    assert seen["regionCode"] == "RU"
    assert seen["videoCategoryId"] == "28"
    total = conn.execute(
        "SELECT COALESCE(SUM(units),0) AS u FROM quota_log WHERE endpoint='videos'"
    ).fetchone()["u"]
    assert total == config.COST_VIDEOS


def test_playlists_by_channel(conn):
    def handler(params):
        return FakeResp(200, {"items": [{"id": "PL1"}]})

    client, _ = _client(handler, conn=conn)
    items = client.playlists_by_channel("UC1", max_results=10)
    assert items == [{"id": "PL1"}]
    total = conn.execute(
        "SELECT COALESCE(SUM(units),0) AS u FROM quota_log WHERE endpoint='playlists'"
    ).fetchone()["u"]
    assert total == config.COST_PLAYLIST_ITEMS


def test_channels_by_handle_one_call_per_handle(conn):
    calls = []

    def handler(params):
        if params.get("part") == "statistics":  # probe
            return FakeResp(200, {"items": []})
        calls.append(params.get("forHandle"))
        return FakeResp(200, {"items": [{"id": "UC" + str(len(calls))}]})

    client, _ = _client(handler, conn=conn)
    handles = [f"h{i}" for i in range(51)]
    items = client.channels_by_handle(handles)
    # API принимает одно forHandle за вызов — 51 вызов, 51 канал.
    assert len(items) == 51
    assert len(calls) == 51
    assert calls[0] == "h0"
    total = conn.execute(
        "SELECT COALESCE(SUM(units),0) AS u FROM quota_log WHERE endpoint='channels'"
    ).fetchone()["u"]
    assert total == 51 * config.COST_CHANNELS


# --- предохранитель суточной квоты: смена суток и сбой чтения ---------------


def test_quota_blocked_key_returns_next_day(monkeypatch, tmp_path):
    """Ключ, исчерпавший сутки, снова доступен после смены суток.

    Исключение по квоте живёт только текущие сутки: вместе с ключом хранятся
    сутки исключения, при смене суток счётчик quota_log начинается заново и
    ключ возвращается в работу. На прежнем коде ключ выпадал навсегда.
    """
    c = db.connect(tmp_path / "yt_day.db")
    db.init_db(c)
    day1 = 1_700_000_000
    day2 = day1 + 86_400
    db.log_quota(
        c,
        yt._day_start(day1),
        yt.YouTubeClient.key_id("k1"),
        1,
        config.QUOTA_LIMIT_PER_KEY,
        "search",
        ts=day1,
    )

    def handler(params):
        return FakeResp(200, {"items": []})

    client = yt.YouTubeClient(
        keys=["k1"],
        conn=c,
        session=FakeSession(handler),
        min_interval=0,
        sleep=lambda _s: None,
    )
    monkeypatch.setattr(config, "now_ts", lambda: day1)
    with pytest.raises(yt.YouTubeError) as exc:
        client._select_key()
    assert "quota" in str(exc.value).lower()
    # В те же сутки ключ исключён.
    assert client._is_quota_blocked("k1")

    monkeypatch.setattr(config, "now_ts", lambda: day2)
    assert client._select_key() == "k1"
    assert not client._is_quota_blocked("k1")
    c.close()


def test_spent_today_reading_failure_warns_and_returns_zero(caplog):
    """Сбой чтения quota_log не глушится: предупреждение в лог и возврат 0."""

    class BrokenConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("no such table: quota_log")

    client = yt.YouTubeClient(
        keys=["k1"],
        conn=BrokenConn(),
        session=FakeSession(lambda params: FakeResp(200, {"items": []})),
        min_interval=0,
        sleep=lambda _s: None,
    )
    with caplog.at_level(logging.WARNING, logger="tuber.yt"):
        assert client.spent_today("k1") == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("quota_log" in m for m in messages)
    assert any("no such table" in m for m in messages)
