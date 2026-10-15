"""Тесты расширения поиска (tuber.expand). Сеть не используется: моки."""

from __future__ import annotations

import json

import pytest

from tuber.platforms.youtube import config, store as db, expand, api as yt


# --- общие фикстуры ---------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "expand_test.db")
    db.init_db(c)
    yield c
    c.close()


class FakeCfg:
    """Мини-конфиг для прогонов с предсказуемыми списками."""

    SEARCH_QUERIES = ["нейросети"]
    EXPAND_BUDGET_UNITS_PER_RUN = 2000
    EXPAND_MAX_PROBES_PER_RUN = 40
    EXPAND_MAX_NEW_CHANNELS_PER_RUN = 30
    EXPAND_PROBE_VIDEOS = 15
    EXPAND_MAX_SEARCH_FALLBACKS = 3
    EXPAND_MAX_PLAYLISTS_PER_CHANNEL = 3
    EXPAND_SOURCE_WEIGHTS = dict(config.EXPAND_SOURCE_WEIGHTS)
    CHART_REGIONS = ("RU",)
    CHART_CATEGORY_IDS = ("28",)
    CHANNEL_SEARCH_QUERIES = ["ИИ агенты"]
    EXPAND_MAX_HANDLE_RESOLVES_PER_RUN = 250
    EXPAND_MAX_QUERIES_PER_RUN = 20
    EXPAND_MAX_RESOLVE_ATTEMPTS = 3
    MIN_AI_VIDEOS_PER_CHANNEL = 2
    MIN_AI_VIDEOS_PER_CANDIDATE = 2
    MAX_VIDEO_AGE_DAYS = 30
    MAX_UPLOAD_PAGES_PER_CHANNEL = 2
    COST_VIDEOS = config.COST_VIDEOS


def _add_ai_video(conn, video_id, channel_id="donor", description="", title="AI",
                  tags=None):
    db.upsert_channel(conn, {"channel_id": channel_id, "title": "ch", "first_seen": 1})
    db.upsert_video(
        conn,
        {
            "video_id": video_id,
            "channel_id": channel_id,
            "title": title,
            "description": description,
            "tags": json.dumps(tags or []),
            "published_at": 1,
            "first_seen": 1,
        },
    )
    db.save_classification(conn, video_id, is_ai=1, topic="модели и релизы")


def _channel_item(cid, title="Канал", uploads=None, subs=1000, handle=None):
    return {
        "id": cid,
        "snippet": {
            "title": title, "description": "про ИИ",
            "customUrl": handle or ("@" + cid),
        },
        "statistics": {"subscriberCount": str(subs)},
        "contentDetails": {"relatedPlaylists": {"uploads": uploads or f"UU{cid}"}},
    }


def _video_item(vid, channel_id, title="AI news", duration="PT10M"):
    return {
        "id": vid,
        "snippet": {
            "channelId": channel_id,
            "title": title,
            "description": "описание",
            "thumbnails": {"high": {"url": f"https://x/{vid}.jpg",
                                    "width": 480, "height": 360}},
            "publishedAt": "2026-09-01T00:00:00Z",
            "tags": ["ai"],
        },
        "contentDetails": {"duration": duration},
        "statistics": {},
    }


class FakeClient:
    """Мок YouTube API с учётом квоты (пишет в quota_log, как боевой)."""

    def __init__(self, conn, channels=None, playlist_items=None, videos=None,
                 chart=None, playlists=None, search_channels_map=None,
                 handle_items=None, search_videos_items=None):
        self.conn = conn
        self.channels = channels or {}
        self.playlist_items_map = playlist_items or {}
        self.videos = videos or {}
        self.chart = chart or []
        self.playlists = playlists or {}
        self.search_channels_map = search_channels_map or {}
        self.handle_items = handle_items or []
        self.search_videos_items = search_videos_items or []
        self.calls = []

    def _log(self, endpoint, units):
        db.log_quota(self.conn, 0, "kfake", 1, units, endpoint)

    def channels_by_ids(self, ids, parts="snippet,statistics,contentDetails"):
        self.calls.append(("channels_by_ids", tuple(ids)))
        self._log("channels", config.COST_CHANNELS)
        return [self.channels[i] for i in ids if i in self.channels]

    def channels_by_handle(self, handles, parts="snippet,statistics,contentDetails"):
        self.calls.append(("channels_by_handle", tuple(handles)))
        self._log("channels", config.COST_CHANNELS)
        return list(self.handle_items)

    def playlist_items(self, playlist_id, max_results=50):
        self.calls.append(("playlist_items", playlist_id))
        self._log("playlistItems", config.COST_PLAYLIST_ITEMS)
        return list(self.playlist_items_map.get(playlist_id, []))

    def videos_by_ids(self, ids, parts="snippet,statistics,contentDetails"):
        self.calls.append(("videos_by_ids", tuple(ids)))
        self._log("videos", config.COST_VIDEOS)
        return [self.videos[i] for i in ids if i in self.videos]

    def chart_videos(self, region, category_id=None, max_results=50):
        self.calls.append(("chart_videos", region, category_id))
        self._log("videos", config.COST_VIDEOS)
        return list(self.chart)

    def playlists_by_channel(self, channel_id, max_results=50):
        self.calls.append(("playlists_by_channel", channel_id))
        self._log("playlists", config.COST_PLAYLIST_ITEMS)
        return list(self.playlists.get(channel_id, []))

    def search_channels(self, query, max_results=50, language=None, region=None):
        self.calls.append(("search_channels", query))
        self._log("search", config.COST_SEARCH)
        return list(self.search_channels_map.get(query, []))

    def search_videos(self, query, published_after=None, order="viewCount",
                      max_pages=1, extra=None):
        self.calls.append(("search_videos", query))
        self._log("search", config.COST_SEARCH)
        return list(self.search_videos_items)


# --- скоринг и приоритет ----------------------------------------------------

def test_candidate_score_source_weights():
    mention = expand.candidate_score("mention", 0, 1)
    search = expand.candidate_score("channel_search", 0, 1)
    playlist = expand.candidate_score("playlist", 0, 1)
    chart = expand.candidate_score("chart", 0, 1)
    assert mention == pytest.approx(3.0)
    assert search == pytest.approx(3.0)
    assert playlist == pytest.approx(2.0)
    assert chart == pytest.approx(1.0)
    # Повторы и подписчики повышают score.
    assert expand.candidate_score("chart", 0, 3) > chart
    assert expand.candidate_score("chart", 1_000_000, 1) > chart


def test_russian_candidate_sorts_first_at_equal_score():
    ru = {"score": 5.0, "title": "Нейросети обзор", "description": "",
          "mentions": 1, "subscriber_count": 10}
    en = {"score": 5.0, "title": "AI news", "description": "",
          "mentions": 1, "subscriber_count": 10}
    assert sorted([en, ru], key=expand.candidate_sort_key)[0] is ru


def test_score_dominates_russian_tiebreak():
    ru_low = {"score": 1.0, "title": "Нейросети", "description": "",
              "mentions": 1, "subscriber_count": 0}
    en_high = {"score": 9.0, "title": "AI", "description": "",
               "mentions": 1, "subscriber_count": 0}
    assert sorted([ru_low, en_high], key=expand.candidate_sort_key)[0] is en_high


# --- дедупликация -----------------------------------------------------------

def test_known_channel_is_not_a_candidate(conn):
    db.upsert_channel(conn, {"channel_id": "UCknown", "title": "k", "first_seen": 1})
    state = expand._new_state(conn, FakeCfg)
    added = expand._add_candidate(conn, state, "UCknown", "chart", evidence="chart:RU:28")
    assert added is False
    n = conn.execute("SELECT COUNT(*) AS n FROM channel_candidates").fetchone()["n"]
    assert n == 0


def test_rejected_candidate_not_resurrected(conn):
    db.upsert_channel_candidate(conn, {
        "channel_id": "UCx", "source": "chart", "status": "rejected",
        "reject_reason": "нет ИИ",
    })
    state = expand._new_state(conn, FakeCfg)
    assert expand._add_candidate(conn, state, "UCx", "mention", evidence="video:v1") is False
    row = conn.execute(
        "SELECT status, mentions FROM channel_candidates WHERE channel_id='UCx'"
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["mentions"] == 1  # не увеличилось


def test_repeat_mention_bumps_mentions_and_score(conn):
    expand._add_candidate(conn, expand._new_state(conn, FakeCfg), "UCy", "mention",
                          evidence="video:v1")
    expand._add_candidate(conn, expand._new_state(conn, FakeCfg), "UCy", "mention",
                          evidence="video:v2")
    row = conn.execute(
        "SELECT mentions, score FROM channel_candidates WHERE channel_id='UCy'"
    ).fetchone()
    assert row["mentions"] == 2
    assert row["score"] == pytest.approx(3.5)


# --- mine_mentions ----------------------------------------------------------

def test_mine_mentions_finds_all_forms_and_skips_self(conn):
    uid = "UC" + "a" * 22
    donor = "UC" + "d" * 22
    desc = (
        "Смотри @SomeHandle и youtube.com/channel/" + uid +
        " а также youtube.com/c/SomeC и youtube.com/user/SomeUser. "
        "Мой канал @ownhandle и youtube.com/channel/" + donor + "."
    )
    db.upsert_channel(conn, {"channel_id": donor, "title": "d",
                             "handle": "ownhandle", "first_seen": 1})
    db.upsert_video(conn, {"video_id": "v1", "channel_id": donor,
                           "title": "t", "description": desc,
                           "published_at": 1, "first_seen": 1})
    db.save_classification(conn, "v1", is_ai=1)

    res = expand.mine_mentions(conn, FakeCfg)
    assert res["units"] == 0
    ids = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM channel_candidates")}
    assert uid in ids
    assert "@somehandle" in ids
    assert "@somec" in ids
    assert "@someuser" in ids
    assert "@ownhandle" not in ids  # самоупоминание
    assert donor not in ids  # сам канал не кандидат


def test_mine_mentions_ignores_non_ai_videos(conn):
    db.upsert_channel(conn, {"channel_id": "donor", "title": "d", "first_seen": 1})
    db.upsert_video(conn, {"video_id": "v1", "channel_id": "donor", "title": "t",
                           "description": "см. @SomeHandle", "published_at": 1,
                           "first_seen": 1})
    db.save_classification(conn, "v1", is_ai=0)
    res = expand.mine_mentions(conn, FakeCfg)
    assert res["new_handles"] == 0


# --- mine_queries -----------------------------------------------------------

def test_mine_queries_threshold_and_filters(conn):
    for i in range(3):
        _add_ai_video(conn, f"v{i}", title=f"нейросети обзор выпуск {i}")
    # Мусор: стоп-слова, эмодзи, хэштеги, цифры, одиночные буквы.
    _add_ai_video(conn, "junk", title="the and 🎉 #AI 123 a b the and")
    # Термин ниже порога (2 видео) не должен попасть.
    for i in range(2):
        _add_ai_video(conn, f"rare{i}", title="квантовый скачок сегодня")

    res = expand.mine_queries(conn, FakeCfg, min_hits=3)
    assert res["units"] == 0
    queries = {r["query"] for r in conn.execute("SELECT query FROM query_candidates")}
    assert "нейросети обзор" in queries
    assert "квантовый скачок" not in queries
    assert "the and" not in queries
    assert not any("🎉" in q for q in queries)
    # хэштег не должен становиться токеном
    assert "ai" not in queries


# --- чарты и поиск ----------------------------------------------------------

def test_scan_charts_adds_channel_candidates(conn):
    chart = [{"snippet": {"channelId": "UCchart1", "channelTitle": "AI Chart CH"}}]
    client = FakeClient(conn, chart=chart)
    state = expand._new_state(conn, FakeCfg)
    budget = expand._Budget(conn, 2000)
    res = expand.scan_charts(client, conn, FakeCfg, state, budget)
    assert res["new"] == 1
    assert res["filtered"] == 0
    assert res["units"] == 1
    row = conn.execute(
        "SELECT source, evidence FROM channel_candidates WHERE channel_id='UCchart1'"
    ).fetchone()
    assert row["source"] == "chart"
    assert row["evidence"] == "chart:RU:28"


def test_scan_charts_filters_non_ai_noise(conn):
    chart = [
        {"snippet": {"channelId": "UCons", "channelTitle": "Music Hits 2026"}},
        {"snippet": {"channelId": "UCai", "channelTitle": "Нейросети Pro"}},
    ]
    client = FakeClient(conn, chart=chart)
    state = expand._new_state(conn, FakeCfg)
    res = expand.scan_charts(client, conn, FakeCfg, state,
                             expand._Budget(conn, 2000))
    assert res["new"] == 1
    assert res["filtered"] == 1
    ids = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM channel_candidates")}
    assert ids == {"UCai"}


def test_scan_charts_does_not_store_filtered(conn):
    chart = [{"snippet": {"channelId": "UCons", "channelTitle": "Football Weekly"}}]
    client = FakeClient(conn, chart=chart)
    expand.scan_charts(client, conn, FakeCfg, expand._new_state(conn, FakeCfg),
                       expand._Budget(conn, 2000))
    n = conn.execute("SELECT COUNT(*) AS n FROM channel_candidates").fetchone()["n"]
    assert n == 0


def test_scan_charts_dedup_before_spend(conn):
    chart = [{"snippet": {"channelId": "UCchart1", "channelTitle": "c"}}]
    client = FakeClient(conn, chart=chart)
    state = expand._new_state(conn, FakeCfg)
    expand.scan_charts(client, conn, FakeCfg, state, expand._Budget(conn, 2000))
    # Второй прогон: маркер уже есть — повторных вызовов нет.
    calls_before = len(client.calls)
    expand.scan_charts(client, conn, FakeCfg, expand._new_state(conn, FakeCfg),
                       expand._Budget(conn, 2000))
    assert len(client.calls) == calls_before


def test_scan_charts_marks_kind_chart(conn):
    """D-04: скан чарта проставляет kind='chart' и не смешивается с запросами."""
    chart = [{"snippet": {"channelId": "UCchart1", "channelTitle": "AI Chart CH"}}]
    client = FakeClient(conn, chart=chart)
    expand.scan_charts(client, conn, FakeCfg, expand._new_state(conn, FakeCfg),
                       expand._Budget(conn, 2000))
    row = conn.execute(
        "SELECT kind FROM query_candidates WHERE query='chart:RU:28'"
    ).fetchone()
    assert row is not None
    assert row["kind"] == "chart"
    # Это служебный ключ, а не текстовый запрос: приём запросов его не берёт.
    assert not expand._scan_seen(conn, "chart:RU:28")  # без kind='chart' не найден
    assert expand._scan_seen(conn, "chart:RU:28", kind="chart")

    # Настоящий запрос остаётся kind='query' и с чартом не смешивается.
    for i in range(3):
        _add_ai_video(conn, f"k{i}", title="нейросети выпуск")
    expand.mine_queries(conn, FakeCfg, min_hits=3)
    kinds = dict(conn.execute("SELECT query, kind FROM query_candidates"))
    assert kinds["нейросети выпуск"] == "query"
    assert kinds["chart:RU:28"] == "chart"
    res = expand.accept_query_candidates(conn, FakeCfg)
    assert res["accepted"] == 1
    assert conn.execute(
        "SELECT status FROM query_candidates WHERE query='нейросети выпуск'"
    ).fetchone()["status"] == "accepted"
    assert conn.execute(
        "SELECT status FROM query_candidates WHERE query='chart:RU:28'"
    ).fetchone()["status"] == "accepted"  # статус маркера не сброшен


def test_search_new_channels_adds_and_marks(conn):
    items = [
        {"id": {"channelId": "UCs1"}, "snippet": {"title": "ИИ канал",
                                                   "description": "d",
                                                   "customUrl": "@iich"}},
    ]
    # Пул поиска теперь берётся из таблицы. Оставляем только проверяемую фразу.
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {
        "query": "ИИ агенты", "source": "channel_search_static",
        "hits": 1, "status": "accepted",
    })
    client = FakeClient(conn, search_channels_map={"ИИ агенты": items})
    state = expand._new_state(conn, FakeCfg)
    res = expand.search_new_channels(client, conn, FakeCfg, state,
                                     expand._Budget(conn, 2000))
    assert res["new"] == 1
    assert res["units"] == 100
    assert res["phrases_used"] == 1
    row = conn.execute(
        "SELECT runs, fail_count, last_fail_at, last_run_at "
        "FROM query_candidates WHERE query='ИИ агенты'"
    ).fetchone()
    assert row["runs"] == 1
    assert row["fail_count"] == 0
    assert row["last_fail_at"] is None
    assert row["last_run_at"] is not None

    # Утверждение, различающее версии (ТЗ-33): при сбое search_channels фраза
    # НЕ получает круг (runs=0), но получает счётчик сбоя (fail_count), а
    # кандидаты не появляются. До фикса счётчика не было вовсе — запрос
    # fail_count на дореформенном коде падает.
    conn.execute("DELETE FROM channel_candidates")
    conn.execute("UPDATE query_candidates SET runs=0 WHERE query='ИИ агенты'")
    conn.commit()
    failing = FakeClient(conn)

    def boom(query, max_results=50, language=None, region=None):
        failing.calls.append(("search_channels", query))
        raise yt.YouTubeError(503, {"error": "backend"}, "search")

    failing.search_channels = boom
    state = expand._new_state(conn, FakeCfg)
    res2 = expand.search_new_channels(failing, conn, FakeCfg, state,
                                      expand._Budget(conn, 2000))
    assert res2["new"] == 0
    row = conn.execute(
        "SELECT runs, fail_count, last_fail_at FROM query_candidates "
        "WHERE query='ИИ агенты'"
    ).fetchone()
    assert row["runs"] == 0
    assert row["fail_count"] == 1
    assert row["last_fail_at"] is not None
    n = conn.execute("SELECT COUNT(*) AS n FROM channel_candidates").fetchone()["n"]
    assert n == 0


def test_search_network_error_is_not_a_phrase_round(conn):
    """ТЗ-33: сетевой сбой не увеличивает runs и не роняет фразу."""
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {
        "query": "фраза под сбоем", "source": "channel_search_static",
        "hits": 1, "status": "accepted",
    })
    client = FakeClient(conn)

    def boom(query, max_results=50, language=None, region=None):
        client.calls.append(("search_channels", query))
        raise yt.YouTubeError(503, {"error": "backend"}, "search")

    client.search_channels = boom
    state = expand._new_state(conn, FakeCfg)
    expand.search_new_channels(client, conn, FakeCfg, state,
                               expand._Budget(conn, 2000))
    # Вызов был, но фраза не отмечена как круг поиска: runs=0 и статус жив.
    assert client.calls == [("search_channels", "фраза под сбоем")]
    row = conn.execute(
        "SELECT runs, status FROM query_candidates WHERE query='фраза под сбоем'"
    ).fetchone()
    assert row["runs"] == 0
    assert row["status"] != "dropped"
    # При падении поиска новых кандидатов-каналов в базе не появляется.
    n = conn.execute("SELECT COUNT(*) AS n FROM channel_candidates").fetchone()["n"]
    assert n == 0


def _always_failing_client(conn):
    """Мок, на котором любой search_channels кидает YouTubeError."""
    client = FakeClient(conn)

    def boom(query, max_results=50, language=None, region=None):
        client.calls.append(("search_channels", query))
        raise yt.YouTubeError(503, {"error": "backend"}, "search")

    client.search_channels = boom
    return client


def test_starving_phrases_are_sidelined_so_others_get_slots(conn):
    """ТЗ-33: постоянно падающие фразы приоритета 1 не голодят остальной пул.

    До фикса обе курируемые фразы падали всегда и оставались приоритетом 1
    (runs не растёт): каждый прогон оба слота
    (``EXPAND_MAX_CHANNEL_SEARCHES_PER_RUN = 2``) уходили только на них, а
    рабочая фраза не выбиралась НИКОГДА (репро /tmp/repro/repro_starve.py).
    После фикса фраза с ``fail_count >= EXPAND_PHRASE_FAIL_LIMIT`` и свежим
    ``last_fail_at`` временно уходит из пула, и слот достаётся другим.
    """
    conn.execute("DELETE FROM query_candidates")
    for phrase in ("падающая фраза один", "падающая фраза два"):
        db.upsert_query_candidate(conn, {
            "query": phrase, "source": "channel_search_static",
            "hits": 1, "status": "accepted",
        })
    db.upsert_query_candidate(conn, {
        "query": "рабочая машинная фраза", "source": "term_mining",
        "hits": 999, "status": "accepted",
    })
    conn.commit()

    searched: list[str] = []
    for _ in range(4):
        client = _always_failing_client(conn)
        state = expand._new_state(conn, FakeCfg)
        expand.search_new_channels(client, conn, FakeCfg, state,
                                   expand._Budget(conn, 2000))
        searched += [c[1] for c in client.calls if c[0] == "search_channels"]

    # Ключевое: рабочая фраза рано или поздно получила слот. На дореформенном
    # коде её нет в searched никогда — тест падает.
    assert "рабочая машинная фраза" in searched
    rows = {r["query"]: r for r in conn.execute(
        "SELECT query, runs, fail_count, last_fail_at FROM query_candidates"
    )}
    for phrase in ("падающая фраза один", "падающая фраза два"):
        # Сбой — не круг поиска, но счётчик сбоев накоплен.
        assert rows[phrase]["runs"] == 0
        assert rows[phrase]["fail_count"] >= config.EXPAND_PHRASE_FAIL_LIMIT
        assert rows[phrase]["last_fail_at"] is not None


def test_phrase_returns_to_pool_after_fail_cooldown(conn):
    """ТЗ-33: фраза из кулдауна возвращается в пул, свежий сбой держит её вне."""
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {
        "query": "остывшая фраза", "source": "channel_search_static",
        "hits": 1, "status": "accepted",
    })
    limit = config.EXPAND_PHRASE_FAIL_LIMIT
    cooldown = config.EXPAND_PHRASE_FAIL_COOLDOWN_SEC
    conn.execute(
        "UPDATE query_candidates SET fail_count=?, last_fail_at=? WHERE query=?",
        (limit, int(config.now_ts()) - cooldown - 1, "остывшая фраза"),
    )
    conn.commit()
    pool = [r["query"] for r in expand.query_phrase_pool(conn)]
    assert "остывшая фраза" in pool  # кулдаун истёк — снова пробуем

    conn.execute(
        "UPDATE query_candidates SET last_fail_at=? WHERE query=?",
        (int(config.now_ts()), "остывшая фраза"),
    )
    conn.commit()
    pool = [r["query"] for r in expand.query_phrase_pool(conn)]
    assert "остывшая фраза" not in pool  # свежий сбой держит фразу вне пула


# --- resolve_mentions -------------------------------------------------------

def test_resolve_mentions_maps_handle_to_channel(conn):
    expand._add_candidate(conn, expand._new_state(conn, FakeCfg), "@iich",
                          "mention", evidence="video:v1", handle="iich")
    item = _channel_item("UCres", title="ИИ канал", handle="@iich")
    client = FakeClient(conn, handle_items=[item])
    state = expand._new_state(conn, FakeCfg)
    res = expand.resolve_mentions(client, conn, FakeCfg, state,
                                  expand._Budget(conn, 2000))
    assert res["resolved"] == 1
    ids = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM channel_candidates")}
    assert "UCres" in ids
    assert "@iich" not in ids


# --- проверка кандидатов ----------------------------------------------------

def _fake_classify_factory(ai_map):
    """Мок классификатора: пишет is_ai по карте video_id -> 0/1."""
    def fake(conn, cfg=config, limit=None, session=None, api_key=None,
             model=None, video_ids=None):
        for vid in (video_ids or []):
            db.save_classification(conn, vid, is_ai=ai_map.get(vid, 0),
                                   topic="модели и релизы")
        return {"classified": len(video_ids or [])}
    return fake


def test_probe_accepts_only_with_two_ai_videos(conn, monkeypatch):
    # Два кандидата.
    for cid in ("UCacc", "UCrej"):
        expand._add_candidate(conn, expand._new_state(conn, FakeCfg), cid, "chart",
                              evidence=f"chart:{cid}")
    channels = {
        "UCacc": _channel_item("UCacc", uploads="UUacc"),
        "UCrej": _channel_item("UCrej", uploads="UUrej"),
    }
    playlist_items = {
        "UUacc": [{"contentDetails": {"videoId": "a1"}},
                  {"contentDetails": {"videoId": "a2"}}],
        "UUrej": [{"contentDetails": {"videoId": "r1"}}],
    }
    videos = {
        "a1": _video_item("a1", "UCacc"), "a2": _video_item("a2", "UCacc"),
        "r1": _video_item("r1", "UCrej"),
    }
    client = FakeClient(conn, channels=channels, playlist_items=playlist_items,
                        videos=videos)
    monkeypatch.setattr(expand.classify_mod, "classify_videos",
                        _fake_classify_factory({"a1": 1, "a2": 1, "r1": 1}))
    state = expand._new_state(conn, FakeCfg)
    res = expand.probe_candidates(client, conn, FakeCfg, limit=10, state=state,
                                  budget=expand._Budget(conn, 2000))
    assert res["accepted"] == 1
    assert res["rejected"] == 1
    acc = conn.execute(
        "SELECT status FROM channel_candidates WHERE channel_id='UCacc'"
    ).fetchone()
    rej = conn.execute(
        "SELECT status, reject_reason FROM channel_candidates WHERE channel_id='UCrej'"
    ).fetchone()
    assert acc["status"] == "accepted"
    assert rej["status"] == "rejected"
    assert "1 < 2" in rej["reject_reason"]


def test_rejected_candidate_not_probed_again(conn, monkeypatch):
    expand._add_candidate(conn, expand._new_state(conn, FakeCfg), "UCrej", "chart",
                          evidence="chart:r")
    expand.db.set_candidate_status(conn, "UCrej", "rejected", "нет ИИ", "1")
    client = FakeClient(conn)
    called = {"n": 0}

    def fake_probe(*a, **k):
        called["n"] += 1
        return {"status": "accepted", "reason": None, "ai": 2}

    monkeypatch.setattr(expand, "_probe_channel", fake_probe)
    state = expand._new_state(conn, FakeCfg)
    res = expand.probe_candidates(client, conn, FakeCfg, limit=10, state=state,
                                  budget=expand._Budget(conn, 2000))
    assert res["probed"] == 0
    assert called["n"] == 0


# --- бюджет -----------------------------------------------------------------

def test_budget_exhaustion_stops_run_without_exceeding(conn):
    class Cfg(FakeCfg):
        CHART_REGIONS = ("RU",)
        CHART_CATEGORY_IDS = ("28",)
        CHANNEL_SEARCH_QUERIES = ["ИИ агенты", "AI agents"]

    chart = [{"snippet": {"channelId": "UCchart1", "channelTitle": "c"}}]
    client = FakeClient(conn, chart=chart,
                        search_channels_map={"ИИ агенты": [], "AI agents": []})
    summary = expand.run_expand(conn, Cfg, client=client, budget=2)
    assert summary["units"] <= 2
    assert summary["stopped_reason"]


def test_dry_run_spends_zero_units(conn):
    _add_ai_video(conn, "v1", title="нейросети", description="см @SomeHandle")
    client = FakeClient(conn)
    summary = expand.run_expand(conn, FakeCfg, client=client, dry_run=True)
    assert summary["units"] == 0
    assert client.calls == []
    # бесплатные источники всё равно отработали
    assert summary["sources"]["mention"] >= 1


# --- порядок источников -----------------------------------------------------

def test_source_order_cheap_to_expensive(conn):
    client = FakeClient(conn)
    summary = expand.run_expand(conn, FakeCfg, client=client, budget=0)
    steps = [s["step"] for s in summary["steps"]]
    assert steps == [
        "mine_mentions", "mine_queries", "accept_query_candidates",
        "resolve_mentions", "scan_charts", "scan_playlists",
        "search_new_channels", "probe_candidates",
    ]
    # Бесплатные шаги идут первыми и стоят 0 units.
    free = {s["step"]: s for s in summary["steps"]}
    assert free["mine_mentions"]["units"] == 0
    assert free["mine_queries"]["units"] == 0


def test_mine_queries_skips_duplicate_token_bigram(conn):
    for i in range(3):
        _add_ai_video(conn, f"d{i}", title="ai ai ai")
    expand.mine_queries(conn, FakeCfg, min_hits=3)
    qs = {r["query"] for r in conn.execute("SELECT query FROM query_candidates")}
    assert "ai ai" not in qs


def test_resolve_mentions_respects_cap(conn):
    for i in range(5):
        expand._add_candidate(conn, expand._new_state(conn, FakeCfg), f"@h{i}",
                              "mention", handle=f"h{i}", evidence="video:v")

    class Cfg(FakeCfg):
        EXPAND_MAX_HANDLE_RESOLVES_PER_RUN = 2

    client = FakeClient(conn, handle_items=[])
    state = expand._new_state(conn, Cfg)
    res = expand.resolve_mentions(client, conn, Cfg, state,
                                  expand._Budget(conn, 2000))
    assert res["calls"] == 2


# --- этап 11: фильтр качества запросов -------------------------------------

def test_is_ai_query_roots_pass():
    for q in ["ai", "ии", "chatgpt", "нейросети обзор", "claude code",
              "AI agents", "fine-tuning", "stable diffusion art", "gpt-5",
              "graphic design", "prompt engineering"]:
        assert expand.is_ai_query(q), q


def test_is_ai_query_drops_single_generic_and_common():
    for q in ["tutorial", "news", "code", "tools", "review", "top",
              "новини", "новости", "урок", "обзор", "топ"]:
        assert not expand.is_ai_query(q), q


def test_is_ai_query_bigram_rules():
    # Биграмма без общих слов и стоп-слов проходит.
    assert expand.is_ai_query("квантовый скачок")
    # Биграмма с общим словом — нет.
    assert not expand.is_ai_query("news tutorial")
    assert not expand.is_ai_query("best tools")
    # Односложное общее слово не проходит, но с корнем — да.
    assert not expand.is_ai_query("seo")
    assert expand.is_ai_query("seo chatgpt")


def test_has_ai_marker_word_boundaries():
    assert not expand.has_ai_marker("email marketing")
    assert not expand.has_ai_marker("html tutorial")
    assert not expand.has_ai_marker("music hits")
    assert expand.has_ai_marker("AI Weekly")
    assert expand.has_ai_marker("Нейросети Pro")


def test_mine_queries_never_accepts_and_filters(conn):
    for i in range(3):
        _add_ai_video(conn, f"v{i}", title="нейросети выпуск")
    for i in range(3):
        _add_ai_video(conn, f"j{i}", title="tutorial news code")
    res = expand.mine_queries(conn, FakeCfg, min_hits=3)
    rows = conn.execute(
        "SELECT query, status FROM query_candidates WHERE source='term_mining'"
    ).fetchall()
    assert rows, "должны быть пригодные термины"
    assert all(r["status"] == "new" for r in rows)
    queries = {r["query"] for r in rows}
    assert "нейросети выпуск" in queries
    assert "tutorial news" not in queries
    assert "tutorial" not in queries
    assert res["terms_filtered"] >= 1


def test_accept_query_candidates_respects_limit(conn):
    for i in range(30):
        db.upsert_query_candidate(conn, {
            "query": f"нейросети выпуск {i}", "source": "term_mining",
            "hits": 100 - i, "status": "new",
        })
    db.upsert_query_candidate(conn, {
        "query": "tutorial news", "source": "term_mining",
        "hits": 999, "status": "new",
    })

    class Cfg(FakeCfg):
        EXPAND_MAX_QUERIES_PER_RUN = 12

    res = expand.accept_query_candidates(conn, Cfg)
    assert res["accepted"] == 12
    n_acc = conn.execute(
        "SELECT COUNT(*) AS n FROM query_candidates "
        "WHERE source='term_mining' AND status='accepted'"
    ).fetchone()["n"]
    assert n_acc == 12
    bad = conn.execute(
        "SELECT status, reject_reason FROM query_candidates WHERE query='tutorial news'"
    ).fetchone()
    assert bad["status"] == "rejected"
    # Лимит на повторный приём тоже соблюдается: каждый вызов добавляет не больше 12.
    res2 = expand.accept_query_candidates(conn, Cfg)
    assert res2["accepted"] == 12


def test_accept_query_candidates_skips_chart_kind(conn):
    """D-04: очередная партия не отдаёт чарт-ключи как текстовые запросы."""
    db.upsert_query_candidate(conn, {
        "query": "нейросети выпуск", "source": "term_mining",
        "hits": 5, "status": "new", "kind": "query",
    })
    # Такого быть не должно, но проверяем прямо: служебный ключ с kind='chart'
    # не попадает в приём, даже если бы лежал с source='term_mining'.
    db.upsert_query_candidate(conn, {
        "query": "chart:RU:28", "source": "term_mining",
        "hits": 999, "status": "new", "kind": "chart",
    })
    res = expand.accept_query_candidates(conn, FakeCfg)
    assert res["accepted"] == 1
    assert conn.execute(
        "SELECT status FROM query_candidates WHERE query='нейросети выпуск'"
    ).fetchone()["status"] == "accepted"
    assert conn.execute(
        "SELECT status FROM query_candidates WHERE query='chart:RU:28'"
    ).fetchone()["status"] == "new"


# --- этап 11: unresolved вместо потери кандидатов --------------------------

def test_resolve_limit_marks_unresolved_not_rejected(conn):
    for i in range(5):
        expand._add_candidate(conn, expand._new_state(conn, FakeCfg), f"@h{i}",
                              "mention", handle=f"h{i}", evidence="video:v")

    class Cfg(FakeCfg):
        EXPAND_MAX_HANDLE_RESOLVES_PER_RUN = 2

    client = FakeClient(conn, handle_items=[])
    state = expand._new_state(conn, Cfg)
    expand.resolve_mentions(client, conn, Cfg, state, expand._Budget(conn, 2000))
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM channel_candidates "
        "WHERE channel_id LIKE '@%' GROUP BY status"
    ).fetchall()
    assert {r["status"]: r["n"] for r in rows} == {"unresolved": 5}


def test_resolve_three_failures_then_rejected(conn):
    expand._add_candidate(conn, expand._new_state(conn, FakeCfg), "@dead",
                          "mention", handle="dead", evidence="video:v")
    client = FakeClient(conn, handle_items=[])
    for attempt in (1, 2):
        state = expand._new_state(conn, FakeCfg)
        expand.resolve_mentions(client, conn, FakeCfg, state,
                                expand._Budget(conn, 2000))
        row = conn.execute(
            "SELECT status, resolve_attempts FROM channel_candidates "
            "WHERE channel_id='@dead'"
        ).fetchone()
        assert row["status"] == "unresolved", attempt
        assert row["resolve_attempts"] == attempt
    state = expand._new_state(conn, FakeCfg)
    expand.resolve_mentions(client, conn, FakeCfg, state,
                            expand._Budget(conn, 2000))
    row = conn.execute(
        "SELECT status, reject_reason, resolve_attempts FROM channel_candidates "
        "WHERE channel_id='@dead'"
    ).fetchone()
    assert row["status"] == "rejected"
    assert "3" in row["reject_reason"]
    assert row["resolve_attempts"] == 3


# --- этап 11: миграции ------------------------------------------------------

def test_expand_migration_idempotent(conn):
    # Запросы: хороший accepted, мусор accepted и new.
    db.upsert_query_candidate(conn, {"query": "нейросети обзор",
                                     "source": "term_mining", "hits": 10,
                                     "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "code", "source": "term_mining",
                                     "hits": 9, "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "news tutorial",
                                     "source": "term_mining", "hits": 8,
                                     "status": "new"})
    # Скан-маркеры не трогаем.
    db.upsert_query_candidate(conn, {"query": "chart:RU:28", "source": "chart",
                                     "hits": 1, "status": "accepted"})
    # handle-кандидат, потерянный из-за лимита.
    db.upsert_channel_candidate(conn, {
        "channel_id": "@lost", "source": "mention", "status": "rejected",
        "reject_reason": "handle не разрешён",
    })
    # Чарт: шум и ИИ.
    db.upsert_channel_candidate(conn, {
        "channel_id": "UCons", "source": "chart", "status": "new",
        "title": "Music Hits 2026",
    })
    db.upsert_channel_candidate(conn, {
        "channel_id": "UCai", "source": "chart", "status": "new",
        "title": "AI Weekly",
    })

    first = expand.migrate_expand_filters(conn, FakeCfg)
    dump1 = _dump(conn)
    second = expand.migrate_expand_filters(conn, FakeCfg)
    dump2 = _dump(conn)
    assert dump1 == dump2
    assert first["queries_rejected"] == 2
    assert first["charts_rejected"] == 1
    assert first["handles_requeued"] == 1

    # Мусорные запросы отклонены, хороший — в работе.
    assert conn.execute(
        "SELECT status FROM query_candidates WHERE query='нейросети обзор'"
    ).fetchone()["status"] == "accepted"
    for q in ("code", "news tutorial"):
        assert conn.execute(
            "SELECT status FROM query_candidates WHERE query=?", (q,)
        ).fetchone()["status"] == "rejected"
    assert conn.execute(
        "SELECT status FROM query_candidates WHERE query='chart:RU:28'"
    ).fetchone()["status"] == "accepted"
    assert conn.execute(
        "SELECT status FROM channel_candidates WHERE channel_id='@lost'"
    ).fetchone()["status"] == "unresolved"
    assert conn.execute(
        "SELECT status FROM channel_candidates WHERE channel_id='UCons'"
    ).fetchone()["status"] == "rejected"
    assert conn.execute(
        "SELECT status FROM channel_candidates WHERE channel_id='UCai'"
    ).fetchone()["status"] == "new"
    assert second["charts_rejected"] == 0


def _dump(conn):
    q = [tuple(r) for r in conn.execute(
        "SELECT query, status, reject_reason FROM query_candidates ORDER BY query")]
    c = [tuple(r) for r in conn.execute(
        "SELECT channel_id, status, reject_reason, resolve_attempts "
        "FROM channel_candidates ORDER BY channel_id")]
    return q, c


def test_query_upsert_preserves_status(conn):
    db.upsert_query_candidate(conn, {"query": "ai agents", "source": "term_mining",
                                     "hits": 5, "status": "rejected"})
    db.set_query_candidate_status(conn, "ai agents", "rejected", "не ИИ-запрос")
    # Повторная находка не возвращает отклонённый запрос в new.
    db.upsert_query_candidate(conn, {"query": "ai agents", "source": "term_mining",
                                     "hits": 7, "status": "new"})
    row = conn.execute(
        "SELECT status, hits FROM query_candidates WHERE query='ai agents'"
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["hits"] == 6


def test_accept_does_not_touch_existing_accepted(conn):
    db.upsert_query_candidate(conn, {"query": "claude", "source": "term_mining",
                                     "hits": 100, "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "нейросети", "source": "term_mining",
                                     "hits": 90, "status": "new"})
    res = expand.accept_query_candidates(conn, FakeCfg)
    assert res["accepted"] == 1
    assert conn.execute(
        "SELECT status FROM query_candidates WHERE query='claude'"
    ).fetchone()["status"] == "accepted"
    assert conn.execute(
        "SELECT status FROM query_candidates WHERE query='нейросети'"
    ).fetchone()["status"] == "accepted"


# --- D-03: доля ИИ-видео при приёме кандидата -------------------------------

def _probe_share_case(conn, monkeypatch, checked, ai_count, cid="UCp"):
    """Проверить канал с заданным числом проверенных и ИИ-видео."""
    expand._add_candidate(conn, expand._new_state(conn, FakeCfg), cid, "chart",
                          evidence=f"chart:{cid}")
    ids = [f"{cid}v{i}" for i in range(checked)]
    uploads = f"UU{cid}"
    channels = {cid: _channel_item(cid, uploads=uploads)}
    playlist_items = {uploads: [{"contentDetails": {"videoId": v}} for v in ids]}
    videos = {v: _video_item(v, cid) for v in ids}
    ai_map = {v: (1 if i < ai_count else 0) for i, v in enumerate(ids)}
    client = FakeClient(conn, channels=channels, playlist_items=playlist_items,
                        videos=videos)
    monkeypatch.setattr(expand.classify_mod, "classify_videos",
                        _fake_classify_factory(ai_map))
    state = expand._new_state(conn, FakeCfg)
    return expand.probe_candidates(client, conn, FakeCfg, limit=10, state=state,
                                   budget=expand._Budget(conn, 2000))


def _reason(conn, cid):
    return conn.execute(
        "SELECT reject_reason FROM channel_candidates WHERE channel_id=?", (cid,)
    ).fetchone()["reject_reason"]


def test_probe_rejects_corporate_channel_by_share(conn, monkeypatch):
    """(а) 15 проверенных, 3 ИИ-видео (20%) — отказ с долей в причине."""
    res = _probe_share_case(conn, monkeypatch, checked=15, ai_count=3, cid="UCcorp")
    assert res["accepted"] == 0
    assert res["rejected"] == 1
    reason = _reason(conn, "UCcorp")
    assert "3 из 15" in reason
    assert "20%" in reason
    assert "25%" in reason


def test_probe_share_boundary_reject_at_24_and_accept_at_27(conn, monkeypatch):
    """(б) 24% — отказ, 27% — приём (оба края порога 25%)."""
    res = _probe_share_case(conn, monkeypatch, checked=25, ai_count=6, cid="UC24")
    assert res["rejected"] == 1
    assert "24%" in _reason(conn, "UC24")

    res = _probe_share_case(conn, monkeypatch, checked=15, ai_count=4, cid="UC27")
    assert res["accepted"] == 1
    assert _reason(conn, "UC27") is None


def test_probe_small_channel_uses_absolute_rule(conn, monkeypatch):
    """(в) 5 проверенных, 2 ИИ-видео — приём: доля не применяется."""
    res = _probe_share_case(conn, monkeypatch, checked=5, ai_count=2, cid="UCsmall")
    assert res["accepted"] == 1
    assert _reason(conn, "UCsmall") is None


def test_probe_rejects_russian_tutor_by_share(conn, monkeypatch):
    """(г) 15 проверенных, 2 ИИ-видео — русскоязычный обучающий отказ."""
    res = _probe_share_case(conn, monkeypatch, checked=15, ai_count=2, cid="UCru")
    assert res["rejected"] == 1
    reason = _reason(conn, "UCru")
    assert "2 из 15" in reason
    assert "13%" in reason


def _fake_classify_some_factory(ai_map):
    """Мок классификатора, который разбирает только перечисленные video_id.

    Остальные проверенные видео (короткие/непригодные) строки в
    video_classification не получают — именно этот случай и проверяем.
    """
    def fake(conn, cfg=config, limit=None, session=None, api_key=None,
             model=None, video_ids=None):
        done = 0
        for vid in (video_ids or []):
            if vid not in ai_map:
                continue
            db.save_classification(conn, vid, is_ai=ai_map[vid],
                                   topic="модели и релизы")
            done += 1
        return {"classified": done}
    return fake


def _probe_classified_case(conn, monkeypatch, probed, classified, ai_count,
                           cid="UCp"):
    """Канал: probed проверенных видео, из них классификатор разобрал classified."""
    expand._add_candidate(conn, expand._new_state(conn, FakeCfg), cid, "chart",
                          evidence=f"chart:{cid}")
    ids = [f"{cid}v{i}" for i in range(probed)]
    uploads = f"UU{cid}"
    channels = {cid: _channel_item(cid, uploads=uploads)}
    playlist_items = {uploads: [{"contentDetails": {"videoId": v}} for v in ids]}
    videos = {v: _video_item(v, cid) for v in ids}
    ai_map = {v: (1 if i < ai_count else 0)
              for i, v in enumerate(ids[:classified])}
    client = FakeClient(conn, channels=channels, playlist_items=playlist_items,
                        videos=videos)
    monkeypatch.setattr(expand.classify_mod, "classify_videos",
                        _fake_classify_some_factory(ai_map))
    state = expand._new_state(conn, FakeCfg)
    return expand.probe_candidates(client, conn, FakeCfg, limit=10, state=state,
                                   budget=expand._Budget(conn, 2000))


def test_probe_share_uses_classified_denominator(conn, monkeypatch):
    """(а) проверено 15, разобрано 10, 3 ИИ → приём (30% среди разобранных)."""
    res = _probe_classified_case(conn, monkeypatch, probed=15, classified=10,
                                 ai_count=3, cid="UCden1")
    assert res["accepted"] == 1
    assert _reason(conn, "UCden1") is None


def test_probe_share_reject_reports_both_numbers(conn, monkeypatch):
    """(б) проверено 15, разобрано 10, 2 ИИ → отказ с обоими числами."""
    res = _probe_classified_case(conn, monkeypatch, probed=15, classified=10,
                                 ai_count=2, cid="UCden2")
    assert res["rejected"] == 1
    reason = _reason(conn, "UCden2")
    assert "2 из 10 разобранных (20%) < 25% (проверено 15)" in reason


def test_probe_share_needs_enough_classified(conn, monkeypatch):
    """(в) проверено 15, разобрано 4 — доля не применяется, 2 ИИ → приём."""
    res = _probe_classified_case(conn, monkeypatch, probed=15, classified=4,
                                 ai_count=2, cid="UCden3")
    assert res["accepted"] == 1
    assert _reason(conn, "UCden3") is None


def test_probe_share_all_classified_absolute_rule(conn, monkeypatch):
    """(г) проверено 8, разобрано 8, 2 ИИ → абсолютное правило, приём."""
    res = _probe_classified_case(conn, monkeypatch, probed=8, classified=8,
                                 ai_count=2, cid="UCden4")
    assert res["accepted"] == 1
    assert _reason(conn, "UCden4") is None


def test_probe_share_no_probed_suffix_when_equal(conn, monkeypatch):
    """Если разобраны все проверенные, «(проверено N)» не печатается."""
    res = _probe_classified_case(conn, monkeypatch, probed=15, classified=15,
                                 ai_count=2, cid="UCden5")
    assert res["rejected"] == 1
    reason = _reason(conn, "UCden5")
    assert "2 из 15 разобранных (13%) < 25%" in reason
    assert "проверено" not in reason


# --- D-02: пул фраз из таблицы, порядок и отсев -----------------------------

def test_query_phrase_pool_priority_order(conn):
    """ТЗ-33: урожайная → курируемая → повторная → машинная многословная →
    машинная односложная."""
    conn.execute("DELETE FROM query_candidates")
    # 1) проверенная с урожаем.
    db.upsert_query_candidate(conn, {"query": "лучшая фраза",
                                     "source": "channel_search", "hits": 0,
                                     "status": "accepted"})
    conn.execute("UPDATE query_candidates SET runs=1, accepted=5 "
                 "WHERE query='лучшая фраза'")
    # 2) курируемая конфига, ни разу не ходившая.
    db.upsert_query_candidate(conn, {"query": "курируемая фраза",
                                     "source": "channel_search_static",
                                     "hits": 1, "status": "accepted"})
    # 3) прочая проверенная без урожая.
    db.upsert_query_candidate(conn, {"query": "повторная машинная",
                                     "source": "term_mining", "hits": 5,
                                     "status": "accepted"})
    conn.execute("UPDATE query_candidates SET runs=1, accepted=0 "
                 "WHERE query='повторная машинная'")
    # 4) машинная многословная, не ходившая, с большим hits.
    db.upsert_query_candidate(conn, {"query": "машинная многословная",
                                     "source": "term_mining", "hits": 100,
                                     "status": "accepted"})
    # 5) машинная односложная, не ходившая.
    db.upsert_query_candidate(conn, {"query": "claude",
                                     "source": "term_mining", "hits": 100,
                                     "status": "accepted"})
    conn.commit()

    order = [r["query"] for r in expand.query_phrase_pool(conn)]
    assert order == [
        "лучшая фраза", "курируемая фраза", "повторная машинная",
        "машинная многословная", "claude",
    ]


def test_query_phrase_pool_skips_service_playlist_keys(conn):
    """ТЗ-34: служебные ключи и один знак — не в пуле, ``ии``/``ai`` — в пуле.

    И ``ии``, и ``ai`` — рабочие аббревиатуры темы канала; под правило «короче
    трёх знаков» из ТЗ-33 они попадали ошибочно. Строки в ``query_candidates``
    при этом не тронуты: обход плейлистов на них опирается.
    """
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {"query": "playlist:PLx",
                                     "source": "playlist", "hits": 1,
                                     "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "playlists:UCGpsgNbzdF7BECCVbB1COHw",
                                     "source": "playlist", "hits": 1,
                                     "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "!!!", "source": "term_mining",
                                     "hits": 1, "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "\u0438", "source": "term_mining",
                                     "hits": 999, "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "ai", "source": "term_mining",
                                     "hits": 1510, "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "\u0438\u0438",
                                     "source": "term_mining", "hits": 258,
                                     "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "нейросети обзор",
                                     "source": "term_mining", "hits": 1,
                                     "status": "accepted"})
    conn.commit()

    pool = [r["query"] for r in expand.query_phrase_pool(conn, limit=2000)]
    assert "playlist:PLx" not in pool
    assert "playlists:UCGpsgNbzdF7BECCVbB1COHw" not in pool
    assert "!!!" not in pool          # нет ни одной буквы
    assert "\u0438" not in pool       # один знак
    assert "ai" in pool               # ТЗ-34: рабочая двухбуквенная аббревиатура
    assert "\u0438\u0438" in pool     # русская аббревиатура ИИ
    assert "нейросети обзор" in pool  # человеческая фраза на месте
    # Строки в query_candidates не тронуты: обход плейлистов на них опирается.
    stored = {r[0] for r in conn.execute(
        "SELECT query FROM query_candidates WHERE query LIKE 'playlist%'"
    )}
    assert stored == {"playlist:PLx", "playlists:UCGpsgNbzdF7BECCVbB1COHw"}


def test_query_phrase_pool_keeps_short_ai_terms_after_migration(conn):
    """ТЗ-34: миграция ТЗ-33 закрыла ``ии``/``ai``/``ml`` — они возвращаются в пул.

    Сначала воспроизводим состояние после миграции ТЗ-33 (строки ``dropped`` с
    прежней причиной), затем прогоняем те же миграции, что и ``init_db``, и
    проверяем, что рабочие двухбуквенные фразы снова доступны пулу.
    """
    conn.execute("DELETE FROM query_candidates")
    for query, hits in (("ai", 1510), ("ии", 258), ("ml", 21)):
        db.upsert_query_candidate(conn, {"query": query, "source": "term_mining",
                                         "hits": hits, "status": "accepted"})
    conn.execute("UPDATE query_candidates SET status='dropped', reject_reason=?",
                 ("непригодна для поиска (служебный ключ, нет букв или короче "
                  "3 знаков)",))
    conn.commit()

    # init_db прогоняет миграции в боевом порядке; на 14bb133 он закрывает
    # короткие фразы и не умеет их возвращать, поэтому тест различит версии.
    db.init_db(conn)

    pool = [r["query"] for r in expand.query_phrase_pool(conn, limit=2000)]
    assert "ai" in pool
    assert "ии" in pool
    assert "ml" in pool


def test_query_phrase_pool_prefers_curated_over_machine(conn):
    """ТЗ-33: лимит 1 — выбирается курируемая фраза, а не term_mining с hits=100."""
    conn.execute("DELETE FROM query_candidates")
    for i in range(5):
        db.upsert_query_candidate(conn, {
            "query": f"машинная фраза {i}", "source": "term_mining",
            "hits": 100, "status": "accepted",
        })
    db.upsert_query_candidate(conn, {"query": "курируемая фраза",
                                     "source": "channel_search_static",
                                     "hits": 1, "status": "accepted"})
    conn.commit()

    pool = expand.query_phrase_pool(conn, limit=1)
    assert [r["query"] for r in pool] == ["курируемая фраза"]


def test_query_phrase_pool_excludes_dropped_and_exhausted(conn):
    """(б) отсеянная фраза не попадает в пул и не тратит units."""
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {"query": "мусор после круга",
                                     "source": "channel_search_static",
                                     "hits": 1, "status": "accepted"})
    db.upsert_query_candidate(conn, {"query": "мусор после 2 кругов",
                                     "source": "channel_search_static",
                                     "hits": 1, "status": "accepted"})
    conn.execute(
        "UPDATE query_candidates SET runs=1, accepted=0, status='dropped' "
        "WHERE query='мусор после круга'"
    )
    conn.execute(
        "UPDATE query_candidates SET runs=2, accepted=0 "
        "WHERE query='мусор после 2 кругов'"
    )
    conn.commit()
    pool = expand.query_phrase_pool(conn)
    assert [r["query"] for r in pool] == []

    client = FakeClient(conn, search_channels_map={})
    state = expand._new_state(conn, FakeCfg)
    res = expand.search_new_channels(client, conn, FakeCfg, state,
                                     expand._Budget(conn, 2000))
    assert res["phrases_used"] == 0
    assert res["units"] == 0
    assert client.calls == []


def test_search_drops_phrase_after_two_zero_rounds(conn):
    """(г) второй нулевой круг даёт status='dropped' с верной причиной."""
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {"query": "фраза без урожая",
                                     "source": "channel_search_static",
                                     "hits": 1, "status": "accepted"})
    conn.execute(
        "UPDATE query_candidates SET runs=1, accepted=0, last_run_at='t0' "
        "WHERE query='фраза без урожая'"
    )
    conn.commit()
    client = FakeClient(conn, search_channels_map={"фраза без урожая": []})
    state = expand._new_state(conn, FakeCfg)
    res = expand.search_new_channels(client, conn, FakeCfg, state,
                                     expand._Budget(conn, 2000))
    assert res["phrases_used"] == 1
    assert res["phrases_dropped"] == 1
    row = conn.execute(
        "SELECT runs, status, reject_reason FROM query_candidates "
        "WHERE query='фраза без урожая'"
    ).fetchone()
    assert row["runs"] == 2
    assert row["status"] == "dropped"
    assert row["reject_reason"] == "нулевой урожай после 2 кругов"


def test_search_recounts_accepted_from_facts(conn):
    """Пересчёт accepted идёт по принятым каналам с evidence='search:'||query."""
    conn.execute("DELETE FROM query_candidates")
    db.upsert_query_candidate(conn, {"query": "урожайная фраза",
                                     "source": "channel_search_static",
                                     "hits": 1, "status": "accepted"})
    for cid in ("UCy1", "UCy2"):
        db.upsert_channel_candidate(conn, {
            "channel_id": cid, "source": "channel_search", "status": "accepted",
            "evidence": "search:урожайная фраза",
        })
    expand.recount_phrase_yields(conn)
    row = conn.execute(
        "SELECT accepted FROM query_candidates WHERE query='урожайная фраза'"
    ).fetchone()
    assert row["accepted"] == 2


def test_static_phrases_registered_available_to_pool(conn):
    """(д) статические фразы конфига после регистрации доступны пулу."""
    rows = conn.execute(
        "SELECT query, status, source FROM query_candidates "
        "WHERE source='channel_search_static'"
    ).fetchall()
    registered = {r["query"] for r in rows}
    assert set(config.CHANNEL_SEARCH_QUERIES) <= registered
    assert all(r["status"] == "accepted" for r in rows)
    pool = {r["query"] for r in expand.query_phrase_pool(conn)}
    assert "ИИ агенты" in pool


def test_resolve_mentions_stops_at_limit_when_queue_exceeds_it(conn):
    """Очередь больше лимита → разбор упирается в лимит и откладывает остаток.

    Синтетическая очередь: ёмкость берём из боевого конфига (она обязана быть
    400 — замеренная очередь ≈ 363), сверху кладём ещё 5 кандидатов. Резолвер
    обязан остановиться ровно на 400 вызовах, назвать причину «лимит резолва
    handle» и отложить остаток в unresolved, а не потерять его.
    """
    limit = config.EXPAND_MAX_HANDLE_RESOLVES_PER_RUN
    assert limit == 400, limit
    queue = limit + 5
    seed = expand._new_state(conn, config)
    for i in range(queue):
        expand._add_candidate(conn, seed, f"@h{i}", "mention",
                              handle=f"h{i}", evidence="video:v")
    client = FakeClient(conn, handle_items=[])  # ни один handle не разрешается
    state = expand._new_state(conn, config)
    res = expand.resolve_mentions(
        client, conn, config, state, expand._Budget(conn, 10000)
    )
    assert res["calls"] == limit
    assert "лимит резолва handle" in res["stopped_reason"]
    assert res["unresolved"] == queue
