"""Тесты сбора (tuber.collect). Сеть не используется: только мок tuber.yt."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tuber.platforms.youtube import collect, config, store as db

NOW = 1_800_000_000  # фиксированное "сейчас" для детерминированных тестов


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "collect_test.db")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    # collect/config/db берут время отсюда.
    monkeypatch.setattr(config, "now_ts", lambda: NOW)


def _iso(ts: int, frac: bool = False) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if frac:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.123Z")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_search_item(video_id, channel_id="ch1", published=NOW - 3600, title="t"):
    return {
        "id": {"kind": "youtube#video", "videoId": video_id},
        "snippet": {
            "publishedAt": _iso(published),
            "channelId": channel_id,
            "title": title,
        },
    }


def make_video_meta(
    video_id,
    channel_id="ch1",
    published=NOW - 3600,
    duration="PT10M",
    category="28",
    tags=None,
    thumbs=("maxres", "high"),
    views="1000",
    likes="10",
    comments="2",
):
    thumbnails = {}
    for i, name in enumerate(thumbs):
        thumbnails[name] = {
            "url": f"https://i.ytimg.com/vi/{video_id}/{name}.jpg",
            "width": 1280 - i * 100,
            "height": 720 - i * 50,
        }
    item = {
        "id": video_id,
        "snippet": {
            "publishedAt": _iso(published, frac=True),
            "channelId": channel_id,
            "title": f"title {video_id}",
            "description": "desc",
            "thumbnails": thumbnails,
            "categoryId": category,
        },
        "contentDetails": {"duration": duration, "caption": "true"},
        "statistics": {"viewCount": views, "likeCount": likes, "commentCount": comments},
    }
    if tags is not None:
        item["snippet"]["tags"] = tags
    return item


def make_channel_item(channel_id="ch1", language=None, country=None):
    snippet = {"title": f"channel {channel_id}"}
    if language is not None:
        snippet["defaultLanguage"] = language
    if country is not None:
        snippet["country"] = country
    return {
        "id": channel_id,
        "snippet": snippet,
        "statistics": {"subscriberCount": "500", "videoCount": "10", "viewCount": "900"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + channel_id}},
        "topicDetails": {"topicCategories": ["https://en.wikipedia.org/wiki/Technology"]},
    }


class FakeYouTube:
    """Мок tuber.yt.YouTubeClient: ничего не ходит в сеть."""

    def __init__(self, search=None, videos=None, channels=None, playlist=None):
        self.search_items = list(search or [])
        self.videos_items = list(videos or [])
        self.channel_items = list(channels or [])
        self.playlist_data = list(playlist or [])
        self.search_calls = []
        self.videos_calls = []
        self.channels_calls = []
        self.playlist_calls = []

    def search_videos(self, query, published_after=None, order="viewCount", max_pages=1):
        self.search_calls.append({"query": query, "published_after": published_after})
        return list(self.search_items)

    def videos_by_ids(self, ids, parts="snippet,statistics,contentDetails"):
        ids = list(ids)
        self.videos_calls.append(ids)
        keep = set(ids)
        return [m for m in self.videos_items if m["id"] in keep]

    def channels_by_ids(self, ids, parts="snippet,statistics,contentDetails"):
        ids = list(ids)
        self.channels_calls.append(ids)
        keep = set(ids)
        return [c for c in self.channel_items if c["id"] in keep]

    def playlist_items(self, playlist_id, max_results=50):
        self.playlist_calls.append((playlist_id, max_results))
        return list(self.playlist_data)


# --- разбор дат и длительности --------------------------------------------


def test_parse_iso_utc_z_and_fractional_seconds():
    base = int(datetime(2026, 9, 10, 0, 30, tzinfo=timezone.utc).timestamp())
    assert collect.parse_iso_utc("2026-09-10T00:30:00Z") == base
    # Дробные секунды усекаются до целого unixtime.
    assert collect.parse_iso_utc("2026-09-10T00:30:00.123Z") == base
    assert collect.parse_iso_utc("2026-09-10T03:30:00+03:00") == base
    assert collect.parse_iso_utc("2026-09-10T00:30:00") == base
    assert collect.parse_iso_utc(None) is None
    assert collect.parse_iso_utc("не дата") is None


def test_parse_duration_iso8601():
    assert collect.parse_duration("PT1H2M3S") == 3723
    assert collect.parse_duration("PT45S") == 45
    assert collect.parse_duration("P1DT1H") == 90000
    assert collect.parse_duration("PT59S") == 59
    assert collect.parse_duration(None) is None


# --- запись видео ----------------------------------------------------------


def test_store_videos_tags_are_json_array(conn):
    item = make_video_meta("v1", tags=["нейросети", "ai"], published=NOW - 7200)
    res = collect.store_videos(conn, [item], "q")
    assert res["written"] == 1 and res["new"] == 1
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    # Ровно один формат: JSON-массив.
    assert row["tags"] == json.dumps(["нейросети", "ai"], ensure_ascii=False)
    assert json.loads(row["tags"]) == ["нейросети", "ai"]


def test_store_videos_missing_tags_is_empty_json_array(conn):
    item = make_video_meta("v1", tags=None)
    collect.store_videos(conn, [item], "q")
    row = conn.execute("SELECT tags FROM videos WHERE video_id='v1'").fetchone()
    assert row["tags"] == "[]"
    assert json.loads(row["tags"]) == []


def test_store_videos_thumbnail_prefers_maxres_else_high(conn):
    collect.store_videos(conn, [make_video_meta("v1")], "q")
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert row["thumbnail_url"].endswith("/maxres.jpg")
    assert row["thumbnail_width"] == 1280
    assert row["thumbnail_height"] == 720

    collect.store_videos(conn, [make_video_meta("v2", thumbs=("high",))], "q")
    row2 = conn.execute("SELECT * FROM videos WHERE video_id='v2'").fetchone()
    assert row2["thumbnail_url"].endswith("/high.jpg")
    assert row2["thumbnail_width"] == 1280


def test_store_videos_published_at_is_unixtime_and_duration(conn):
    pub = NOW - 5000
    collect.store_videos(conn, [make_video_meta("v1", published=pub, duration="PT45S")], "q")
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert row["published_at"] == pub
    assert isinstance(row["published_at"], int)
    assert row["duration_seconds"] == 45
    assert row["is_shorts"] == 1
    assert row["caption_available"] == 1


def test_store_videos_without_thumbnail_is_skipped(conn):
    item = make_video_meta("v1", thumbs=())
    res = collect.store_videos(conn, [item], "q")
    assert res["written"] == 0 and res["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"] == 0


# --- запись канала ---------------------------------------------------------


def test_store_channel_is_russian_null_without_data(conn):
    collect.store_channel(conn, make_channel_item("c_none"))
    row = conn.execute("SELECT * FROM channels WHERE channel_id='c_none'").fetchone()
    assert row["is_russian"] is None  # "не знаем", а не 0
    assert row["default_language"] is None
    assert row["country"] is None


def test_store_channel_is_russian_by_language_and_country(conn):
    collect.store_channel(conn, make_channel_item("c_ru", language="ru"))
    collect.store_channel(conn, make_channel_item("c_country", country="RU"))
    collect.store_channel(conn, make_channel_item("c_en", language="en", country="US"))
    flags = {
        r["channel_id"]: r["is_russian"]
        for r in conn.execute("SELECT channel_id, is_russian FROM channels")
    }
    assert flags["c_ru"] == 1
    assert flags["c_country"] == 1
    assert flags["c_en"] == 0


def test_store_channel_uploads_and_topics(conn):
    collect.store_channel(conn, make_channel_item("ch1"))
    row = conn.execute("SELECT * FROM channels WHERE channel_id='ch1'").fetchone()
    assert row["uploads_playlist_id"] == "UUch1"
    assert json.loads(row["topic_categories"]) == [
        "https://en.wikipedia.org/wiki/Technology"
    ]


# --- поиск по теме ---------------------------------------------------------


def test_search_topic_prefilter_and_noise_filter(conn):
    good = make_video_meta("good", channel_id="ch1")
    noise = make_video_meta("noise", channel_id="ch1", category="20")  # Gaming
    zero = make_video_meta("zero", channel_id="ch1", duration="PT0S")
    old = make_search_item("old", published=NOW - 100 * 86400)  # вне окна
    search = [
        make_search_item("good"),
        make_search_item("noise"),
        make_search_item("zero"),
        old,
    ]
    fake = FakeYouTube(
        search=search,
        videos=[good, noise, zero],
        channels=[make_channel_item("ch1")],
    )
    res = collect.search_topic(conn, "AI", config, client=fake)

    assert res["found"] == 4
    assert res["candidates"] == 3  # old отсеян предфильтром
    assert res["written"] == 1
    assert res["new"] == 1
    stored = {r["video_id"] for r in conn.execute("SELECT video_id FROM videos")}
    assert stored == {"good"}  # noise (категория) и zero (длительность) отсеяны
    assert fake.channels_calls == [["ch1"]]
    assert conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"] == 1


def test_search_topic_skips_known_video(conn):
    collect.store_videos(conn, [make_video_meta("known")], "old")
    fake = FakeYouTube(
        search=[make_search_item("known")],
        videos=[make_video_meta("known")],
    )
    res = collect.search_topic(conn, "AI", config, client=fake)
    assert res["known"] == 1
    assert res["new"] == 0
    # Уже известное видео повторно в videos.list не отправляем.
    assert fake.videos_calls == []


# --- обход uploads ---------------------------------------------------------


def _playlist_item(video_id, published):
    return {
        "contentDetails": {"videoId": video_id},
        "snippet": {"publishedAt": _iso(published), "title": video_id},
    }


def test_collect_uploads_stops_by_age(conn):
    collect.store_channel(conn, make_channel_item("ch1"))
    fake = FakeYouTube(
        playlist=[
            _playlist_item("new1", NOW - 86400),
            _playlist_item("new2", NOW - 5 * 86400),
            _playlist_item("old1", NOW - 100 * 86400),
        ],
        videos=[
            make_video_meta("new1", channel_id="ch1"),
            make_video_meta("new2", channel_id="ch1"),
            make_video_meta("old1", channel_id="ch1"),
        ],
    )
    res = collect.collect_uploads(conn, "ch1", config, client=fake)
    # Обход остановлен на старом видео: old1 не выбран и не запрошен.
    assert res["selected"] == 2
    assert res["new"] == 2
    assert fake.videos_calls == [["new1", "new2"]]
    assert conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"] == 2


def test_collect_uploads_fetches_channel_when_missing(conn):
    fake = FakeYouTube(
        channels=[make_channel_item("ch9")],
        playlist=[_playlist_item("v1", NOW - 3600)],
        videos=[make_video_meta("v1", channel_id="ch9")],
    )
    res = collect.collect_uploads(conn, "ch9", config, client=fake)
    assert fake.channels_calls == [["ch9"]]
    assert res["new"] == 1


# --- прогон сбора ----------------------------------------------------------


def _mark_ai(conn, video_id, channel_id, is_ai=1):
    """Записать видео и его разбор (для проверки условного обхода)."""
    collect.store_videos(
        conn, [make_video_meta(video_id, channel_id=channel_id, published=NOW - 7200)], "seed"
    )
    db.save_classification(conn, video_id, is_ai=is_ai, topic=None, confidence=1.0)


def test_run_collect_summary(conn, monkeypatch):
    # Канал подтверждён: два разобранных ИИ-видео — обход разрешён.
    _mark_ai(conn, "seed1", "ch1")
    _mark_ai(conn, "seed2", "ch1")
    fake = FakeYouTube(
        search=[make_search_item("v1", channel_id="ch1")],
        videos=[make_video_meta("v1", channel_id="ch1")],
        channels=[make_channel_item("ch1")],
        playlist=[_playlist_item("v1", NOW - 3600)],
    )
    # Мок ставим на tuber.yt, как требует ТЗ.
    monkeypatch.setattr(collect.yt, "YouTubeClient", lambda conn=None: fake)
    res = collect.run_collect(conn, config, queries=["AI news"], classify=False)
    assert res["queries"] == 1
    assert res["found"] == 1
    assert res["new"] == 1
    assert res["channels_scanned"] == 1
    assert res["errors"] == []
    assert res["stopped_by_budget"] is False


def test_run_collect_survives_search_failure(conn, monkeypatch):
    from tuber.platforms.youtube import api as yt_mod

    class Broken(FakeYouTube):
        def search_videos(self, *a, **k):
            raise yt_mod.YouTubeError(500, {"error": {}}, "search")

    monkeypatch.setattr(collect.yt, "YouTubeClient", lambda conn=None: Broken())
    res = collect.run_collect(conn, config, queries=["boom"])
    assert res["found"] == 0
    assert len(res["errors"]) == 1


# --- условный обход каналов ------------------------------------------------


def test_is_ai_channel_threshold(conn):
    _mark_ai(conn, "a1", "ch1")
    assert collect._is_ai_channel(conn, "ch1") is False  # 1 < 2
    _mark_ai(conn, "a2", "ch1")
    assert collect._is_ai_channel(conn, "ch1") is True  # 2 >= 2
    # Канал без разбора не подтверждён.
    collect.store_videos(conn, [make_video_meta("u1", channel_id="ch2")], "q")
    assert collect._is_ai_channel(conn, "ch2") is False


def test_collect_uploads_respects_max_pages(conn):
    collect.store_channel(conn, make_channel_item("ch1"))
    fake = FakeYouTube(playlist=[_playlist_item("v1", NOW - 3600)],
                       videos=[make_video_meta("v1", channel_id="ch1")])
    collect.collect_uploads(conn, "ch1", config, client=fake, max_pages=3)
    assert fake.playlist_calls[-1] == ("UUch1", 150)  # 3 страницы по 50

    fake2 = FakeYouTube(playlist=[], videos=[])
    collect.collect_uploads(conn, "ch1", config, client=fake2)
    # По умолчанию MAX_UPLOAD_PAGES_PER_CHANNEL=2 страницы.
    assert fake2.playlist_calls[-1] == ("UUch1", 100)


def test_run_collect_scans_only_confirmed_ai_channels(conn, monkeypatch):
    # ch1 — лишь 1 ИИ-видео (обход запрещён), ch2 — 2 (обход разрешён).
    _mark_ai(conn, "ch1a", "ch1")
    _mark_ai(conn, "ch2a", "ch2")
    _mark_ai(conn, "ch2b", "ch2")
    fake = FakeYouTube(
        search=[make_search_item("v1", channel_id="ch1"),
                make_search_item("v2", channel_id="ch2")],
        videos=[make_video_meta("v1", channel_id="ch1"),
                make_video_meta("v2", channel_id="ch2")],
        channels=[make_channel_item("ch1"), make_channel_item("ch2")],
        playlist=[],
    )
    monkeypatch.setattr(collect.yt, "YouTubeClient", lambda conn=None: fake)
    res = collect.run_collect(conn, config, queries=["AI"], classify=False)
    assert res["channels_scanned"] == 1
    # Обходили только подтверждённый канал ch2.
    assert fake.playlist_calls == [("UUch2", 100)]


def test_run_collect_skips_channel_without_classification(conn, monkeypatch):
    # Канал найден поиском, но разбора нет — обход не запускается.
    fake = FakeYouTube(
        search=[make_search_item("v1", channel_id="ch_new")],
        videos=[make_video_meta("v1", channel_id="ch_new")],
        channels=[make_channel_item("ch_new")],
        playlist=[_playlist_item("v1", NOW - 3600)],
    )
    monkeypatch.setattr(collect.yt, "YouTubeClient", lambda conn=None: fake)
    res = collect.run_collect(conn, config, queries=["AI"], classify=False)
    assert res["channels_scanned"] == 0
    assert fake.playlist_calls == []


class _Clock:
    """Управляемые часы: время двигает мок поиска."""

    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


class AdvancingFake(FakeYouTube):
    """Каждый поиск сдвигает часы на 1000 с — бюджет 900 с истекает."""

    def __init__(self, clock, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock

    def search_videos(self, *a, **k):
        res = super().search_videos(*a, **k)
        self.clock.t += 1000
        return res


def test_run_collect_stops_by_time_budget(conn, monkeypatch):
    clock = _Clock(NOW)
    monkeypatch.setattr(config, "now_ts", clock)
    fake = AdvancingFake(
        clock,
        search=[make_search_item("v1", channel_id="ch1")],
        videos=[make_video_meta("v1", channel_id="ch1")],
    )
    monkeypatch.setattr(collect.yt, "YouTubeClient", lambda conn=None: fake)

    res = collect.run_collect(
        conn, config, queries=["q1", "q2", "q3"],
        classify=False, time_budget_seconds=900,
    )
    assert res["stopped_by_budget"] is True
    assert res["queries_done"] == 1
    assert res["requests"] == 1
    # Уже собранное сохранено.
    assert res["new"] == 1
    stored = {r["video_id"] for r in conn.execute("SELECT video_id FROM videos")}
    assert stored == {"v1"}
    # Второй и третий запросы не выполнялись.
    assert len(fake.search_calls) == 1


def test_run_collect_classify_flag_triggers_classification(conn, monkeypatch):
    from tuber.platforms.youtube import classify as classify_mod

    calls = []
    monkeypatch.setattr(
        classify_mod, "classify_videos",
        lambda c, cfg: calls.append(1) or {"classified": 0},
    )
    fake = FakeYouTube(
        search=[make_search_item("v1", channel_id="ch1")],
        videos=[make_video_meta("v1", channel_id="ch1")],
    )
    monkeypatch.setattr(collect.yt, "YouTubeClient", lambda conn=None: fake)

    collect.run_collect(conn, config, queries=["AI"], classify=True)
    assert calls == [1]

    calls.clear()
    collect.run_collect(conn, config, queries=["AI"], classify=False)
    assert calls == []  # флаг выключен — разбор не вызывается


def test_run_collect_classifies_before_channel_traversal(conn, monkeypatch):
    """Сначала разбор, потом обход: канал становится ИИ-каналом в этом же прогоне."""
    from tuber.platforms.youtube import classify as classify_mod

    def fake_classify(c, cfg):
        for row in db.get_unclassified(c):
            db.save_classification(
                c, row["video_id"], is_ai=1, topic=None, confidence=1.0
            )
        return {"classified": 2}

    monkeypatch.setattr(classify_mod, "classify_videos", fake_classify)
    fake = FakeYouTube(
        search=[make_search_item("v1", channel_id="ch1"),
                make_search_item("v2", channel_id="ch1")],
        videos=[make_video_meta("v1", channel_id="ch1"),
                make_video_meta("v2", channel_id="ch1")],
        channels=[make_channel_item("ch1")],
        playlist=[],
    )
    monkeypatch.setattr(collect.yt, "YouTubeClient", lambda conn=None: fake)

    res = collect.run_collect(conn, config, queries=["AI"], classify=True)
    # Два новых видео разобраны как ИИ -> канал подтверждён и обойдён.
    assert res["channels_scanned"] == 1
    assert fake.playlist_calls  # обход действительно был


def test_store_videos_61_180_seconds_are_shorts(conn):
    """Порог шортса — 180 с: 90 и 180 секунд шортсы, 181 — уже полное видео."""
    items = [
        make_video_meta("s90", duration="PT1M30S"),
        make_video_meta("s180", duration="PT3M"),
        make_video_meta("s181", duration="PT3M1S"),
    ]
    collect.store_videos(conn, items, "q")
    rows = {
        r["video_id"]: r["is_shorts"]
        for r in conn.execute("SELECT video_id, is_shorts FROM videos")
    }
    assert rows["s90"] == 1
    assert rows["s180"] == 1
    assert rows["s181"] == 0
