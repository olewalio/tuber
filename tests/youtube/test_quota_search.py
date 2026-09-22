"""Предохранитель поисковой квоты: 100 вызовов search.list в сутки на проект.

Сеть не используется: только мок сессии и локальная база. Число поисков
подставляется строками ``quota_log`` (живую поисковую квоту не тратим).

Проверки ТЗ-16 (часть A):
(a) два ключа одного проекта делят счётчик: 60 + 25 = 85 → поиск блокируется;
(b) счётчики разных проектов независимы: 85 у проекта A не мешает поиску
    ключа проекта B;
(c) ключ без привязки к проекту считается по ключу (прежнее поведение);
(d) ниже порога поиск проходит (мок сети).
"""

from __future__ import annotations

import sqlite3

import pytest

from tuber.platforms.youtube import config, store as db, expand, collect, api as yt


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

    def search_calls(self):
        return [c for c in self.calls if c["url"].rstrip("/").endswith("/search")]


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "quota_search.db")
    db.init_db(c)
    yield c
    c.close()


def _ok_handler(params):
    return FakeResp(200, {"items": [{"id": {"videoId": "v1"}}]})


def _client(conn, keys, handler=_ok_handler):
    return yt.YouTubeClient(
        keys=keys,
        conn=conn,
        session=FakeSession(handler),
        min_interval=0,
        sleep=lambda _s: None,
    )


def _add_searches(conn, key: str, n: int, units: int = 1, project: str | None = None):
    """Подставить n строк поискового расхода для ключа (в его key_id)."""
    key_id = yt.YouTubeClient.key_id(key)
    for _ in range(n):
        db.log_quota(
            conn,
            (config.now_ts() // 86400) * 86400,
            key_id,
            1,
            units,
            "search",
            project,
            ts=config.now_ts(),
        )


def _quota_rows(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM quota_log").fetchone()["n"])


def test_search_guard_threshold_constants():
    """Порог считается в одном месте: лимит минус резерв."""
    assert config.QUOTA_SEARCH_LIMIT_PER_PROJECT == 100
    assert config.QUOTA_SEARCH_RESERVE == 15
    client = yt.YouTubeClient(keys=["AIza-no-network"], conn=None)
    assert client.search_stop_threshold() == 85


# --- (a) два ключа одного проекта делят счётчик -----------------------------


def test_two_keys_one_project_share_search_counter(conn):
    project = "100200300"
    keys = [("AIzaAAA", project), ("AIzaBBB", project)]
    _add_searches(conn, "AIzaAAA", 60, project=project)
    _add_searches(conn, "AIzaBBB", 25, project=project)

    client = _client(conn, keys)
    assert client.search_calls_today("AIzaAAA") == 85
    assert client.search_calls_today("AIzaBBB") == 85

    rows_before = _quota_rows(conn)
    session = client.session
    with pytest.raises(yt.SearchQuotaGuard) as exc:
        client.search_videos("нейросети")

    msg = str(exc.value)
    assert "search quota guard: проект 100200300: 85/100 (лимит 100, резерв 15)" in msg
    # Вызов НЕ отправлен и квота НЕ потрачена.
    assert session.calls == []
    assert _quota_rows(conn) == rows_before


def test_guard_not_triggered_at_84_for_project(conn):
    """84 из 85 — ещё можно (проверяем, что порог не срабатывает раньше)."""
    project = "P-A"
    keys = [("AIzaAAA", project)]
    _add_searches(conn, "AIzaAAA", 84, project=project)
    client = _client(conn, keys)
    assert client.search_guard_exceeded("AIzaAAA") is False


# --- (b) разные проекты независимы ------------------------------------------


def test_different_projects_are_independent(conn):
    keys = [("AIzaAAA", "proj-A"), ("AIzaBBB", "proj-B")]
    _add_searches(conn, "AIzaAAA", 85, project="proj-A")

    client = _client(conn, keys)
    assert client.search_guard_exceeded("AIzaAAA") is True
    assert client.search_guard_exceeded("AIzaBBB") is False

    # Ключ проекта B работает: поиск проходит (мок сети).
    items = client.search_videos("нейросети")
    assert items
    assert client.session.search_calls(), "поиск должен был уйти в сеть"


# --- (c) ключ без привязки к проекту считается по ключу ---------------------


def test_key_without_project_is_counted_per_key(conn):
    keys = [("AIzaAAA", None), ("AIzaBBB", None)]
    _add_searches(conn, "AIzaAAA", 85)

    client = _client(conn, keys)
    # Счётчики раздельные: 85 у одного ключа не трогают второй.
    assert client.search_calls_today("AIzaAAA") == 85
    assert client.search_calls_today("AIzaBBB") == 0
    assert client.search_guard_exceeded("AIzaAAA") is True
    assert client.search_guard_exceeded("AIzaBBB") is False


def test_unbound_full_project_style_key_is_blocked(conn):
    """Один ключ без проекта с 85 поисками — блокируется с понятным текстом."""
    keys = [("AIzaAAA", None)]
    _add_searches(conn, "AIzaAAA", 85)
    client = _client(conn, keys)
    with pytest.raises(yt.SearchQuotaGuard) as exc:
        client.search_videos("нейросети")
    msg = str(exc.value)
    assert msg.startswith("search quota guard: ключ k")
    assert "85/100 (лимит 100, резерв 15)" in msg


# --- (d) ниже порога поиск проходит -----------------------------------------


def test_below_threshold_search_passes(conn):
    keys = [("AIzaAAA", "proj-A")]
    _add_searches(conn, "AIzaAAA", 84, project="proj-A")

    client = _client(conn, keys)
    items = client.search_videos("нейросети")
    assert items == [{"id": {"videoId": "v1"}}]
    assert client.session.search_calls(), "ниже порога поиск должен уйти в сеть"


def test_threshold_is_exact_at_85(conn):
    """Ровно 85 — уже блок (порог включительный), 84 — нет."""
    keys = [("AIzaAAA", "proj-A")]
    _add_searches(conn, "AIzaAAA", 84, project="proj-A")
    client = _client(conn, keys)
    assert client.search_guard_exceeded("AIzaAAA") is False

    _add_searches(conn, "AIzaAAA", 1, project="proj-A")
    assert client.search_guard_exceeded("AIzaAAA") is True


def test_non_search_endpoint_ignores_search_guard(conn):
    """Предохранитель касается только search.list, не videos.list."""
    keys = [("AIzaAAA", "proj-A")]
    _add_searches(conn, "AIzaAAA", 85, project="proj-A")
    client = _client(conn, keys)
    items = client.videos_by_ids(["v1"])
    assert items
    assert client.session.calls, "videos.list не должен блокироваться поисковым лимитом"


# --- прогон не падает, а сообщает причину пропуска поисков ------------------


class _GuardClient:
    """Мок: любой поиск поднимает SearchQuotaGuard, остальное не нужно."""

    def __init__(self):
        self.calls = []

    def search_channels(self, *a, **k):
        self.calls.append("search_channels")
        raise yt.SearchQuotaGuard(
            "search quota guard: проект P: 85/100 (лимит 100, резерв 15)",
            project="P", calls=85, threshold=85,
        )

    def search_videos(self, *a, **k):
        self.calls.append("search_videos")
        raise yt.SearchQuotaGuard(
            "search quota guard: проект P: 85/100 (лимит 100, резерв 15)",
            project="P", calls=85, threshold=85,
        )


def test_expand_search_step_reports_guard_without_crash(conn):
    db.upsert_query_candidate(conn, {
        "query": "нейросети", "source": "channel_search_static",
        "hits": 1, "status": "accepted",
    })
    conn.commit()
    client = _GuardClient()
    state = expand._new_state(conn, config)
    res = expand.search_new_channels(client, conn, config, state,
                                     expand._Budget(conn, 2000))
    assert res["guard_skipped"] == 1
    assert res["guard"] and "search quota guard" in res["guard"]
    assert res["units"] == 0  # вызов не отправлен — units не потрачены


def test_collect_reports_guard_without_crash(conn, tmp_path, monkeypatch):
    # Ротацию не трогаем: явный список запросов.
    client = _GuardClient()
    summary = collect.run_collect(
        conn, config, queries=["нейросети"], client=client, classify=False,
    )
    assert summary["search_guard"] and "search quota guard" in summary["search_guard"]
    assert summary["queries_done"] == 0
    assert summary["search_guard"]  # причина пропуска отражена в итоге


# --- ТЗ-19 / Дефект 1: счётчик на подключении без row_factory ----------------


def test_counts_read_on_connection_without_row_factory(tmp_path):
    """spent_today/search_calls_today читаются и без row_factory, сбор не падает.

    Обычный sqlite3.connect() даёт строки-tuple; раньше row["n"]/row["u"]
    поднимал TypeError и ронял сбор. Теперь доступ позиционный, а предохранитель
    либо считает, либо честно возвращает 0 — но не падает.
    """
    path = tmp_path / "norow.db"
    c = db.connect(path)
    db.init_db(c)
    c.close()
    plain = sqlite3.connect(str(path))
    db.install_compat(plain)  # слой совместимости ставится адаптером и на «сырое» соединение
    try:
        _add_searches(plain, "k1", 3, units=1, project=None)
        client = _client(plain, ["k1"])
        assert client.search_calls_today("k1") == 3
        assert client.spent_today("k1") == 3
        assert client.search_guard_exceeded("k1") in (True, False)
        assert client.quota_exceeded("k1") in (True, False)
    finally:
        plain.close()

