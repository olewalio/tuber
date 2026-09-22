"""ТЗ-11, волна 3: квота YouTube (W3-1), дедуп запросов (W3-2).

Сеть не используется: YouTube-клиент — мок, база и файлы состояния ротации
живут во временной папке.
"""

from __future__ import annotations

import json

import pytest

from tuber.platforms.youtube import collect, config, store as db
from tests.youtube.test_collect import (
    FakeYouTube,
    _playlist_item,
    make_channel_item,
    make_video_meta,
)

NOW = 1_800_000_000  # фиксированное «сейчас» для детерминированных тестов
DAY = 86400


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(config, "now_ts", lambda: NOW)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "w3.db")
    db.init_db(c)
    yield c
    c.close()


class Cfg:
    """Мини-конфиг прогона с абсолютными временными путями состояния."""

    SEARCH_QUERIES = ["ai news", "AI news", "новости AI"]
    COLLECT_QUERIES_PER_RUN = 2
    # ТЗ-12 волна 3.1: дефолт обхода uploads поднят 60 -> 400 (реестр 1608
    # каналов, полный круг за 4-5 прогонов вместо ~27 суток). Мини-конфиг
    # повторяет новый дефолт.
    COLLECT_UPLOADS_CHANNELS_PER_RUN = 400
    MIN_AI_VIDEOS_PER_CHANNEL = 2
    MAX_VIDEO_AGE_DAYS = 30
    MAX_UPLOAD_PAGES_PER_CHANNEL = 2
    COLLECT_TIME_BUDGET_SECONDS = 900
    QUERY_ZERO_YIELD_LIMIT = 3
    QUERY_QUARANTINE_DAYS = 14

    def __init__(self, tmp_path, **over):
        self.QUERY_ROTATION_FILE = str(tmp_path / "query_rotation.json")
        self.UPLOADS_ROTATION_FILE = str(tmp_path / "uploads_rotation.json")
        for key, value in over.items():
            setattr(self, key, value)

    def now_ts(self):
        return NOW


def _mark_ai(conn, video_id, channel_id, is_ai=1):
    collect.store_videos(
        conn,
        [make_video_meta(video_id, channel_id=channel_id, published=NOW - 7200)],
        "seed",
    )
    db.save_classification(conn, video_id, is_ai=is_ai, topic=None, confidence=1.0)


# --- W3-1: конфиг ----------------------------------------------------------


def test_w31_config_defaults():
    assert config.COLLECT_QUERIES_PER_RUN == 2
    # ТЗ-12 (волна 3.1): дефолт обхода uploads 400, а не 60: при реестре 1608
    # подтверждённых ИИ-каналов 60/прогон давали круг ~27 суток.
    assert config.COLLECT_UPLOADS_CHANNELS_PER_RUN == 400


def test_w31_env_override_queries_per_run(monkeypatch):
    monkeypatch.delenv(config.COLLECT_QUERIES_PER_RUN_ENV, raising=False)
    assert config.collect_queries_per_run(config) == 2
    monkeypatch.setenv(config.COLLECT_QUERIES_PER_RUN_ENV, "7")
    assert config.collect_queries_per_run(config) == 7
    monkeypatch.setenv(config.COLLECT_QUERIES_PER_RUN_ENV, "мусор")
    assert config.collect_queries_per_run(config) == config.COLLECT_QUERIES_PER_RUN


def test_w31_run_cuts_search_to_two_queries(conn, tmp_path):
    """Урезанный поиск: за прогон не больше COLLECT_QUERIES_PER_RUN запросов."""
    cfg = Cfg(tmp_path, SEARCH_QUERIES=[f"q{i}" for i in range(20)],
              COLLECT_UPLOADS_CHANNELS_PER_RUN=0)
    fake = FakeYouTube(search=[])
    res = collect.run_collect(conn, cfg, client=fake, classify=False)
    assert [c["query"] for c in fake.search_calls] == ["q0", "q1"]
    assert res["queries"] == 2
    assert set(res) >= {"queries", "queries_done", "requests", "found", "new",
                        "channels_scanned", "units", "errors",
                        "stopped_by_budget", "units_uploads", "units_search",
                        # ТЗ-12: охват реестра и причина/остаток остановки.
                        "registry_total", "stop_reason", "units_remaining"}


# --- W3-1: обход реестра uploads -------------------------------------------


class OrderedFake(FakeYouTube):
    """Мок, который запоминает порядок сетевых обращений."""

    def __init__(self, order, **kwargs):
        super().__init__(**kwargs)
        self.order = order

    def search_videos(self, query, **kwargs):
        self.order.append(("search", query))
        return super().search_videos(query, **kwargs)

    def playlist_items(self, playlist_id, max_results=50):
        self.order.append(("playlist", playlist_id))
        return super().playlist_items(playlist_id, max_results)


def test_w31_uploads_phase_runs_before_search(conn, tmp_path):
    """Дешёвая фаза обхода uploads идёт до дорогого поиска (порядок фаз)."""
    _mark_ai(conn, "s1", "ch1")
    _mark_ai(conn, "s2", "ch1")
    cfg = Cfg(tmp_path, SEARCH_QUERIES=["q"], COLLECT_QUERIES_PER_RUN=1)
    order: list[tuple[str, str]] = []
    fake = OrderedFake(
        order,
        channels=[make_channel_item("ch1")],
        playlist=[_playlist_item("v1", NOW - 3600)],
        videos=[make_video_meta("v1", channel_id="ch1")],
        search=[],
    )
    collect.run_collect(conn, cfg, client=fake, classify=False)
    kinds = [k for k, _ in order]
    assert "playlist" in kinds and "search" in kinds
    assert kinds.index("playlist") < kinds.index("search")


def test_w31_registry_scans_channels_not_found_by_search(conn, tmp_path):
    """Обход реестра берёт подтверждённые каналы, даже если поиск их не вернул."""
    _mark_ai(conn, "a1", "ch1")
    _mark_ai(conn, "a2", "ch1")
    _mark_ai(conn, "b1", "ch2")
    _mark_ai(conn, "b2", "ch2")
    # ТЗ-12: лимит обхода 400 по умолчанию; здесь явно, чтобы тест не зависел
    # от будущей смены дефолта (проверяем сам обход реестра, не число).
    cfg = Cfg(tmp_path, COLLECT_UPLOADS_CHANNELS_PER_RUN=400)
    fake = FakeYouTube(
        search=[],
        channels=[make_channel_item("ch1"), make_channel_item("ch2")],
        playlist=[_playlist_item("v1", NOW - 3600)],
        videos=[make_video_meta("v1", channel_id="ch1")],
    )
    res = collect.run_collect(conn, cfg, queries=["q"], client=fake, classify=False)
    assert res["found"] == 0
    assert res["channels_scanned"] == 2
    assert res["registry_total"] == 2
    assert fake.playlist_calls == [("UUch1", 100), ("UUch2", 100)]
    assert res["new"] == 1


def test_w31_registry_rotation_advances_cursor(conn, tmp_path):
    """При лимите каналов ротация обходит реестр порциями по курсору."""
    for cid in ("ch1", "ch2", "ch3"):
        _mark_ai(conn, f"{cid}-a", cid)
        _mark_ai(conn, f"{cid}-b", cid)
    cfg = Cfg(tmp_path, COLLECT_UPLOADS_CHANNELS_PER_RUN=2)

    def fresh_client():
        return FakeYouTube(
            search=[],
            channels=[make_channel_item(c) for c in ("ch1", "ch2", "ch3")],
            playlist=[],
        )

    first = fresh_client()
    res1 = collect.run_collect(conn, cfg, queries=["q"], client=first, classify=False)
    assert res1["uploads_rotation"]["channels_total"] == 3
    assert [pid for pid, _ in first.playlist_calls] == ["UUch1", "UUch2"]

    second = fresh_client()
    res2 = collect.run_collect(conn, cfg, queries=["q"], client=second, classify=False)
    assert res2["uploads_rotation"]["cursor_before"] == 2
    # Продолжение порции: ch3, затем обёртка на ch1.
    assert [pid for pid, _ in second.playlist_calls] == ["UUch3", "UUch1"]


def test_w31_second_run_is_idempotent(conn, tmp_path):
    """Повторный прогон не плодит дубли и не перезапрашивает известное."""
    _mark_ai(conn, "s1", "ch1")
    _mark_ai(conn, "s2", "ch1")
    cfg = Cfg(tmp_path)
    first = FakeYouTube(
        channels=[make_channel_item("ch1")],
        playlist=[_playlist_item("v1", NOW - 3600)],
        videos=[make_video_meta("v1", channel_id="ch1")],
    )
    res1 = collect.run_collect(conn, cfg, queries=["q"], client=first, classify=False)
    assert res1["new"] == 1
    assert first.videos_calls == [["v1"]]

    second = FakeYouTube(
        channels=[make_channel_item("ch1")],
        playlist=[_playlist_item("v1", NOW - 3600)],
        videos=[make_video_meta("v1", channel_id="ch1")],
    )
    res2 = collect.run_collect(conn, cfg, queries=["q"], client=second, classify=False)
    assert res2["new"] == 0
    # Уже известное видео: videos.list не вызывается повторно.
    assert second.videos_calls == []
    assert conn.execute("SELECT COUNT(*) AS n FROM videos WHERE video_id='v1'"
                        ).fetchone()["n"] == 1


# --- W3-2: дедуп и учёт отдачи --------------------------------------------


def test_w32_normalize_query_key():
    assert config.normalize_query_key("AI News!") == ""
    assert config.normalize_query_key("OpenAI GPT") == "openai gpt"
    assert config.normalize_query_key("Новости ИИ, сегодня") == "сегодня"
    assert config.normalize_query_key("нейросети") == "нейросети"


def test_w32_three_duplicates_collapse_to_one_slot():
    pool = ["AI news", "news AI", "И новости"]
    kept, dropped = config.dedupe_search_queries(pool)
    assert kept == ["AI news"]
    assert dropped == ["news AI", "И новости"]


def test_w32_rotation_sees_cleaned_pool(conn, tmp_path):
    cfg = Cfg(tmp_path, SEARCH_QUERIES=["AI news", "news AI", "OpenAI GPT"],
              COLLECT_QUERIES_PER_RUN=100)
    fake = FakeYouTube(search=[])
    res = collect.run_collect(conn, cfg, client=fake, classify=False)
    assert [c["query"] for c in fake.search_calls] == ["AI news", "OpenAI GPT"]
    assert res["queries_deduped"] == 1
    assert res["rotation"]["full_len"] == 2
    state = json.loads((tmp_path / "query_rotation.json").read_text(encoding="utf-8"))
    assert state["full_len"] == 2


def test_w32_query_quarantine_after_three_zero_runs():
    stats: dict = {}
    limit, cooldown = 3, 14 * DAY
    for i in range(3):
        collect.record_query_result(stats, "q", 0, NOW + i, limit, cooldown)
    assert collect.query_is_quarantined(stats["q"], NOW + 10, limit, cooldown) is True
    pool = ["q", "other"]
    assert collect.active_search_queries(pool, stats, NOW + 10, limit, cooldown) == ["other"]
    # Через 14 дней запрос возвращается в ротацию.
    later = NOW + 14 * DAY + 10
    assert collect.query_is_quarantined(stats["q"], later, limit, cooldown) is False
    assert collect.active_search_queries(pool, stats, later, limit, cooldown) == ["q", "other"]


def test_w32_nonzero_yield_resets_series():
    stats: dict = {}
    limit, cooldown = 3, 14 * DAY
    collect.record_query_result(stats, "q", 0, NOW, limit, cooldown)
    collect.record_query_result(stats, "q", 0, NOW + 1, limit, cooldown)
    collect.record_query_result(stats, "q", 5, NOW + 2, limit, cooldown)
    assert stats["q"]["zero_runs"] == 0
    assert "quarantine_until" not in stats["q"]


def test_w32_run_collect_excludes_quarantined_query(conn, tmp_path):
    cfg = Cfg(tmp_path, SEARCH_QUERIES=["only"], COLLECT_QUERIES_PER_RUN=1,
              COLLECT_UPLOADS_CHANNELS_PER_RUN=0)
    for _ in range(3):
        collect.run_collect(conn, cfg, client=FakeYouTube(search=[]), classify=False)
    fourth = FakeYouTube(search=[])
    res = collect.run_collect(conn, cfg, client=fourth, classify=False)
    assert fourth.search_calls == []
    assert res["queries"] == 0
    assert res["queries_quarantined"] == 1


def test_w32_save_cursor_preserves_query_stats(tmp_path):
    path = tmp_path / "rot.json"
    collect.save_query_stats(path, {"q": {"zero_runs": 1}})
    collect.save_cursor(path, 4, 10)
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["cursor"] == 4
    assert state["queries"] == {"q": {"zero_runs": 1}}
    assert collect.load_query_stats(path) == {"q": {"zero_runs": 1}}
