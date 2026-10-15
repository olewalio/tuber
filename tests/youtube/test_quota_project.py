"""Учёт суточной квоты YouTube по ПРОЕКТУ, а не по ключу (D-10).

Сеть не используется: только мок сессии и локальная база.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tuber.platforms.youtube import config, store as db, api as yt


class FakeResp:
    """Минимальный ответ requests."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


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
    c = db.connect(tmp_path / "quota_project_test.db")
    db.init_db(c)
    yield c
    c.close()


def _ok_handler(params):
    return FakeResp(200, {"items": [{"id": "v"}]})


def _day_start() -> int:
    return (config.now_ts() // 86400) * 86400


def _client(conn, keys, handler=_ok_handler):
    return yt.YouTubeClient(
        keys=keys,
        conn=conn,
        session=FakeSession(handler),
        min_interval=0,
        sleep=lambda _s: None,
    )


# --- 1. формат файла ключей -------------------------------------------------


def test_legacy_keys_file_format_reads_without_project(tmp_path):
    """Старый формат {"keys": ["...", "..."]}: каждый ключ — отдельный проект."""
    src = tmp_path / "yt_keys.json"
    src.write_text(json.dumps({"keys": ["AIzaAAA", "AIzaBBB"]}), encoding="utf-8")

    pairs = config.load_key_projects(src)
    assert pairs == [("AIzaAAA", None), ("AIzaBBB", None)]
    # Прежний контракт «список строк» тоже сохранён.
    assert config.load_keys(src) == ["AIzaAAA", "AIzaBBB"]


def test_project_keys_file_format_reads_pairs(tmp_path):
    """Формат с привязкой: ключи с одинаковым project идут в одну пару."""
    src = tmp_path / "yt_keys.json"
    src.write_text(
        json.dumps(
            {
                "keys": [
                    {"key": "k1", "project": "proj-1"},
                    {"key": "k2", "project": "proj-1"},
                    {"key": "k3"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert config.load_key_projects(src) == [
        ("k1", "proj-1"),
        ("k2", "proj-1"),
        ("k3", None),
    ]


def test_broken_key_items_skipped_and_client_builds(tmp_path, monkeypatch):
    """Битый элемент списка пропускается, клиент поднимается на годных."""
    src = tmp_path / "yt_keys.json"
    src.write_text(
        json.dumps(
            {
                "keys": [
                    None,
                    123,
                    {"nokey": "x"},
                    {"key": ""},
                    {"key": "good", "project": 42},
                ]
            }
        ),
        encoding="utf-8",
    )
    # Мусор отброшен, числовой project приведён к строке.
    assert config.load_key_projects(src) == [("good", "42")]

    monkeypatch.setattr(config, "YT_KEYS", src)
    c = db.connect(tmp_path / "broken.db")
    db.init_db(c)
    client = yt.YouTubeClient(
        conn=c, session=FakeSession(_ok_handler), min_interval=0, sleep=lambda _s: None
    )
    assert client.usable_keys_count() == 1
    c.close()


# --- 2. предохранитель по проекту ------------------------------------------


def test_two_keys_one_project_share_daily_limit(conn):
    """Два ключа одного проекта: 5000 + 5000 = 10000 суммарно — стоп.

    Старая схема «лимит на ключ» на 5000 units каждого ключа не остановилась
    бы: до 10000 на ключ ещё далеко.
    """
    db.log_quota(
        conn, _day_start(), yt.YouTubeClient.key_id("k1"), 1, 5000, "search", "proj-1"
    )
    db.log_quota(
        conn, _day_start(), yt.YouTubeClient.key_id("k2"), 1, 5000, "search", "proj-1"
    )
    client = _client(conn, [("k1", "proj-1"), ("k2", "proj-1")])

    assert client.quota_limit("k1") == config.QUOTA_LIMIT_PER_PROJECT
    assert client.spent_today("k1") == 10000
    assert client.spent_today("k2") == 10000  # суммарно по проекту
    with pytest.raises(yt.YouTubeError):
        client.videos_by_ids(["a"])


def test_project_limit_not_reached_continues(conn):
    """3999 + 3999 = 7998 < порога 8000 (10000 - запас 2000) — работа идёт."""
    db.log_quota(
        conn, _day_start(), yt.YouTubeClient.key_id("k1"), 1, 3999, "search", "proj-1"
    )
    db.log_quota(
        conn, _day_start(), yt.YouTubeClient.key_id("k2"), 1, 3999, "search", "proj-1"
    )
    client = _client(conn, [("k1", "proj-1"), ("k2", "proj-1")])
    assert client.quota_stop_threshold("k1") == 8000
    assert client.videos_by_ids(["a"]) == [{"id": "v"}]


def test_quota_reserve_threshold_allows_below(conn):
    """При расходе 7 999 unit работа ещё идёт (порог 8 000)."""
    db.log_quota(
        conn, _day_start(), yt.YouTubeClient.key_id("k1"), 1, 7999, "search", "proj-1"
    )
    client = _client(conn, [("k1", "proj-1")])
    # Запас не менялся, а порог — ровно лимит минус запас.
    assert config.QUOTA_SAFETY_RESERVE == 2000
    assert client.quota_stop_threshold("k1") == 8000
    assert client.videos_by_ids(["a"]) == [{"id": "v"}]


def test_quota_reserve_threshold_stops_at_boundary(conn):
    """При расходе 8 000 unit работа останавливается с внятной причиной.

    На прежнем коде (до этого изменения) предохранитель остановился бы только
    на 10 000, поэтому 8 000 здесь проходило бы — тест это и ловит.
    """
    db.log_quota(
        conn, _day_start(), yt.YouTubeClient.key_id("k1"), 1, 8000, "search", "proj-1"
    )
    client = _client(conn, [("k1", "proj-1")])
    with pytest.raises(yt.YouTubeError) as exc:
        client.videos_by_ids(["a"])
    text = str(exc.value)
    assert "quota" in text.lower()
    assert "threshold=8000" in text
    assert "reserve=2000" in text


def test_two_keys_without_project_are_counted_separately(conn):
    """Без проекта поведение прежнее: лимит свой у каждого ключа."""
    db.log_quota(
        conn,
        _day_start(),
        yt.YouTubeClient.key_id("k1"),
        1,
        config.QUOTA_LIMIT_PER_KEY,
        "search",
    )
    client = _client(conn, ["k1", "k2"])

    items = client.videos_by_ids(["a"])
    assert items == [{"id": "v"}]
    # k1 исчерпан — работа ушла на k2, а не встала.
    assert client.session.calls
    assert all(call["params"]["key"] == "k2" for call in client.session.calls)


def test_quota_log_records_project_of_key(conn):
    """Замеры квоты пишутся с проектом ключа."""
    client = _client(conn, [("k1", "proj-7")])
    client.videos_by_ids(["a"])
    projects = {
        row["project"] for row in conn.execute("SELECT project FROM quota_log")
    }
    assert projects == {"proj-7"}


def test_quota_stop_message_lists_all_blocked_keys(conn):
    """Текст остановки перечисляет ВСЕ заблокированные ключи, а не первый.

    Ключи разных проектов (и без проекта) имеют свои пороги; если встали оба,
    в сообщении должны быть обе строки `key_id: threshold=...`, в порядке
    исходного списка ключей.
    """
    k1 = yt.YouTubeClient.key_id("k1")
    k2 = yt.YouTubeClient.key_id("k2")
    db.log_quota(conn, _day_start(), k1, 1, 8000, "search", "proj-1")
    db.log_quota(conn, _day_start(), k2, 1, 8000, "search")
    client = _client(conn, [("k1", "proj-1"), ("k2", None)])

    with pytest.raises(yt.YouTubeError) as exc:
        client._select_key()
    message = exc.value.body["error"]["message"]
    text = str(exc.value)
    # Обе строки присутствуют, каждая со своим порогом и лимитом.
    assert f"{k1}: threshold=8000 units of limit 10000" in text
    assert f"{k2}: threshold=8000 units of limit 10000" in text
    # Порядок — как в исходном списке ключей; по строке на ключ.
    assert text.index(k1) < text.index(k2)
    lines = message.splitlines()
    assert len(lines) == 3  # заголовок + строка на каждый из двух ключей
    assert lines[0].startswith("daily quota safeguard reached")
    assert lines[1].startswith(f"{k1}: ")
    assert lines[2].startswith(f"{k2}: ")
    # Состав исключения не менялся.
    reason = exc.value.body["error"]["errors"][0]["reason"]
    assert reason == "quotaExceeded"
    assert exc.value.code == 403


# --- 3. схема и миграция quota_log ------------------------------------------


def test_log_quota_stores_project(conn):
    db.log_quota(conn, 1, "k", 1, 10, "search", "proj-9")
    assert conn.execute(
        "SELECT project FROM quota_log WHERE key_id='k'"
    ).fetchone()["project"] == "proj-9"
    db.log_quota(conn, 1, "k2", 1, 10, "search")
    assert conn.execute(
        "SELECT project FROM quota_log WHERE key_id='k2'"
    ).fetchone()["project"] is None
