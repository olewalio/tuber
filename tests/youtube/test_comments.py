"""Тесты сбора верхних комментариев (tuber.comments, tuber.yt.comment_threads).

Сеть не используется: клиент YouTube подменяется фейком. Боевая БД не
затрагивается: соединение открывается на временной базе.
"""

from __future__ import annotations

import time

import pytest

from tuber.platforms.youtube import comments, config, store as db, report, api as yt

NOW = int(time.time())


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "comments_test.db")
    db.init_db(c)
    yield c
    c.close()


def add_channel(conn, cid="c1", title="Канал"):
    db.upsert_channel(conn, {"channel_id": cid, "title": title, "first_seen": 1})


def add_video(conn, vid, cid="c1", is_ai=1, title=None):
    db.upsert_video(
        conn,
        {
            "video_id": vid,
            "channel_id": cid,
            "title": title or f"Видео {vid}",
            "published_at": NOW - 86400,
            "first_seen": 1,
        },
    )
    db.save_classification(
        conn,
        vid,
        is_ai=is_ai,
        topic="ai",
        title_ru=title or f"Видео {vid}",
        lang="en",
        confidence=0.9,
    )


def add_snap(conn, vid, comments_count, captured_at, views=1000):
    db.insert_snapshot(
        conn, vid, captured_at, "d",
        views=views, likes=0, comments=comments_count,
    )


def add_comment(conn, comment_id, video_id, text="текст", likes=0, captured_at=NOW):
    conn.execute(
        "INSERT INTO video_comments (comment_id, video_id, author, text, likes, "
        "published_at, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (comment_id, video_id, "Автор", text, likes, captured_at, captured_at),
    )
    conn.commit()


class FakeClient:
    """Фейковый клиент: отдаёт заранее заданные комментарии по video_id."""

    def __init__(self, by_video=None, error_for=None, disabled_for=None):
        self.by_video = by_video or {}
        self.error_for = error_for or set()
        self.disabled_for = disabled_for or set()
        self.calls: list[str] = []
        self.max_results: list[int] = []

    def comment_threads(self, video_id, max_results=20):
        self.calls.append(video_id)
        self.max_results.append(max_results)
        if video_id in self.disabled_for:
            raise yt.YouTubeError(
                403,
                {"error": {"errors": [{"reason": "commentsDisabled"}]}},
                "commentThreads",
            )
        if video_id in self.error_for:
            raise yt.YouTubeError(0, "network error", "commentThreads")
        return list(self.by_video.get(video_id, []))


# --- отбор видео -----------------------------------------------------------


def test_select_uses_leaders_first(conn):
    """Порядок лидеров отличается от порядка фолбэка: лидер идёт первым.

    У «leader» высокая скорость роста комментариев, но мало комментариев
    всего; у «big» рост почти нулевой, зато комментариев много. Тест
    не тавтологичен: без ветки лидеров порядок был бы [big, leader].
    """
    add_channel(conn)
    start = NOW - 8 * 3600
    add_video(conn, "leader", is_ai=1)
    add_snap(conn, "leader", 0, start)
    add_snap(conn, "leader", 10, start + 3600)  # +10 за час
    add_video(conn, "big", is_ai=1)
    add_snap(conn, "big", 100, start)
    add_snap(conn, "big", 100, start + 3600)  # рост 0, но всего 100

    ids = comments.select_videos(conn, limit=2)
    assert ids == ["leader", "big"]


def test_select_fallback_by_absolute_comments_only_ai(conn):
    add_channel(conn)
    # Без валидной пары замеров лидеров нет — работает добор по абсолюту.
    add_video(conn, "ai_big", is_ai=1)
    add_video(conn, "ai_small", is_ai=1)
    add_video(conn, "not_ai", is_ai=0)
    add_snap(conn, "ai_big", 900, NOW - 3600)
    add_snap(conn, "ai_small", 50, NOW - 3600)
    add_snap(conn, "not_ai", 5000, NOW - 3600)

    ids = comments.select_videos(conn, limit=5)
    assert ids[0] == "ai_big"
    assert "not_ai" not in ids
    assert set(ids) == {"ai_big", "ai_small"}


def test_select_respects_limit(conn):
    add_channel(conn)
    for i in range(5):
        add_video(conn, f"v{i}", is_ai=1)
        add_snap(conn, f"v{i}", 10 * i, NOW - 3600)
    assert len(comments.select_videos(conn, limit=2)) == 2


# --- свежесть --------------------------------------------------------------


def test_run_skips_fresh_video(conn):
    add_channel(conn)
    add_video(conn, "v1", is_ai=1)
    add_snap(conn, "v1", 100, NOW - 3600)
    add_comment(conn, "c1", "v1", captured_at=NOW - 3600)  # младше 7 суток

    client = FakeClient({"v1": [{"comment_id": "c9", "author": "a",
                                 "text": "t", "likes": 1, "published_at": NOW}]})
    summary = comments.run(conn, config, client=client, limit=3)

    assert summary["skipped"] == 1
    assert summary["videos"] == 0
    assert summary["units"] == 0
    assert client.calls == []


def test_run_refreshes_stale_video(conn):
    add_channel(conn)
    add_video(conn, "v1", is_ai=1)
    add_snap(conn, "v1", 100, NOW - 3600)
    old = NOW - config.COMMENT_REFRESH_DAYS * 86400 - 10
    add_comment(conn, "old", "v1", captured_at=old)

    client = FakeClient({"v1": [{"comment_id": "c_new", "author": "a",
                                 "text": "t", "likes": 1, "published_at": NOW}]})
    summary = comments.run(conn, config, client=client, limit=3)
    assert summary["skipped"] == 0
    assert summary["videos"] == 1
    assert summary["comments"] == 1


def test_run_disabled_then_skipped_next_run(conn):
    """Дефект 2(а): 403 disabled помечаем как проверку — повтор не платит."""
    add_channel(conn)
    add_video(conn, "v1", is_ai=1)
    add_snap(conn, "v1", 100, NOW - 3600)

    first = FakeClient(disabled_for={"v1"})
    s1 = comments.run(conn, config, client=first, limit=3)
    assert s1["disabled"] == 1
    assert s1["videos"] == 0
    assert s1["units"] == config.COST_COMMENT_THREADS
    assert first.calls == ["v1"]
    row = conn.execute(
        "SELECT status FROM comment_checks WHERE video_id='v1'"
    ).fetchone()
    assert row["status"] == "disabled"

    second = FakeClient({"v1": [{"comment_id": "c1", "author": "a",
                                  "text": "t", "likes": 1,
                                  "published_at": NOW}]})
    s2 = comments.run(conn, config, client=second, limit=3)
    assert s2["skipped"] == 1
    assert s2["units"] == 0
    assert second.calls == []


def test_run_empty_then_skipped_next_run(conn):
    """Пустой ответ тоже фиксируется и не переплачивается на следующем прогоне."""
    add_channel(conn)
    add_video(conn, "v1", is_ai=1)
    add_snap(conn, "v1", 100, NOW - 3600)

    first = FakeClient({"v1": []})
    assert comments.run(conn, config, client=first, limit=3)["videos"] == 1
    assert first.calls == ["v1"]
    assert conn.execute(
        "SELECT status FROM comment_checks WHERE video_id='v1'"
    ).fetchone()["status"] == "empty"

    second = FakeClient({"v1": []})
    s2 = comments.run(conn, config, client=second, limit=3)
    assert s2["skipped"] == 1
    assert second.calls == []


def test_run_rechecks_after_window(conn):
    """Дефект 2(б): истекло окно свежести — вызов снова есть."""
    add_channel(conn)
    add_video(conn, "v1", is_ai=1)
    add_snap(conn, "v1", 100, NOW - 3600)
    old = NOW - config.COMMENT_REFRESH_DAYS * 86400 - 10
    comments.save_check(conn, "v1", "empty", None, old)

    client = FakeClient({"v1": [{"comment_id": "c1", "author": "a",
                                  "text": "t", "likes": 1,
                                  "published_at": NOW}]})
    summary = comments.run(conn, config, client=client, limit=3)
    assert summary["skipped"] == 0
    assert client.calls == ["v1"]


def test_run_fills_limit_from_other_videos_when_leaders_fresh(conn):
    """Дефект 2: свежие лидеры не съедают лимит — добор по другим видео.

    Раньше все отобранные лидеры могли оказаться свежими, и прогон сдавался
    нулём обращений. Теперь кандидатов берём с запасом и набираем лимит.
    """
    add_channel(conn)
    start = NOW - 8 * 3600
    for vid in ("lead1", "lead2"):
        add_video(conn, vid, is_ai=1)
        add_snap(conn, vid, 0, start)
        add_snap(conn, vid, 60, start + 3600)
        comments.save_check(conn, vid, "empty", None, NOW - 3600)
    for vid in ("other1", "other2"):
        add_video(conn, vid, is_ai=1)
        add_snap(conn, vid, 0, start)
        add_snap(conn, vid, 5, start + 3600)

    client = FakeClient({
        "other1": [{"comment_id": "o1", "author": "a", "text": "t",
                    "likes": 1, "published_at": NOW}],
        "other2": [],
    })
    summary = comments.run(conn, config, client=client, limit=2)
    assert summary["skipped"] == 2
    assert summary["videos"] == 2
    assert set(client.calls) == {"other1", "other2"}
    assert summary["units"] == 2 * config.COST_COMMENT_THREADS


def test_run_fetches_max_and_keeps_top_likes(conn):
    """Дефект 3: 1 unit, но тянем 100 и оставляем лучших по лайкам, не первых.

    ТЗ-49 поднял потолок на видео (COMMENT_MAX_PER_VIDEO = 500): хранится
    min(пришло, потолок) — здесь все 60.
    """
    add_channel(conn)
    add_video(conn, "v1", is_ai=1)
    add_snap(conn, "v1", 100, NOW - 3600)

    # 60 комментариев: порядок следования перепутан, лайки уникальны (0..59).
    items = [
        {"comment_id": f"c{i}", "author": "a", "text": f"t{i}",
         "likes": (i * 13) % 60, "published_at": NOW}
        for i in range(60)
    ]
    client = FakeClient({"v1": items})
    summary = comments.run(conn, config, client=client, limit=3)

    assert client.max_results == [config.COMMENT_FETCH_MAX]
    kept = min(len(items), config.COMMENT_MAX_PER_VIDEO)
    assert summary["comments"] == kept
    rows = conn.execute(
        "SELECT comment_id FROM video_comments WHERE video_id='v1' "
        "ORDER BY likes DESC, comment_id ASC"
    ).fetchall()
    got = [r["comment_id"] for r in rows]
    expected = [c["comment_id"] for c in
                sorted(items, key=lambda c: -c["likes"])[:kept]]
    assert got == expected
    # Сохранены не первые по порядку следования, а именно лучшие по лайкам.
    assert got != [c["comment_id"] for c in items[:kept]]


# --- запись и отбор по лайкам ----------------------------------------------


def test_save_ranks_by_likes_and_caps(conn):
    add_channel(conn)
    add_video(conn, "v1")
    items = [
        {"comment_id": "a", "author": "a", "text": "a", "likes": 1, "published_at": NOW},
        {"comment_id": "b", "author": "b", "text": "b", "likes": 50, "published_at": NOW},
        {"comment_id": "c", "author": "c", "text": "c", "likes": 10, "published_at": NOW},
    ]
    written = comments.save_comments(conn, "v1", items, max_per_video=2, captured_at=NOW)
    assert written == 2
    rows = conn.execute(
        "SELECT comment_id FROM video_comments WHERE video_id='v1' ORDER BY likes DESC"
    ).fetchall()
    assert [r["comment_id"] for r in rows] == ["b", "c"]


def test_save_ignores_duplicates(conn):
    add_channel(conn)
    add_video(conn, "v1")
    items = [{"comment_id": "a", "author": "a", "text": "a", "likes": 1,
              "published_at": NOW}]
    assert comments.save_comments(conn, "v1", items, 20, NOW) == 1
    assert comments.save_comments(conn, "v1", items, 20, NOW) == 0


# --- сбои не валят прогон --------------------------------------------------


def test_run_counts_errors_and_continues(conn):
    add_channel(conn)
    add_video(conn, "bad", is_ai=1)
    add_video(conn, "good", is_ai=1)
    add_snap(conn, "bad", 900, NOW - 3600)
    add_snap(conn, "good", 800, NOW - 3600)

    client = FakeClient(
        {"good": [{"comment_id": "g1", "author": "a", "text": "t", "likes": 2,
                   "published_at": NOW}]},
        error_for={"bad"},
    )
    summary = comments.run(conn, config, client=client, limit=3)
    assert summary["errors"] == 1
    assert summary["videos"] == 1
    assert summary["comments"] == 1


def test_run_units_from_quota_log(conn):
    add_channel(conn)
    add_video(conn, "v1", is_ai=1)
    add_snap(conn, "v1", 10, NOW - 3600)

    class QuotaClient(FakeClient):
        def comment_threads(self, video_id, max_results=20):
            db.log_quota(conn, 0, "k1", 1, config.COST_COMMENT_THREADS,
                         "commentThreads")
            return super().comment_threads(video_id, max_results)

    client = QuotaClient({"v1": [{"comment_id": "c1", "author": "a",
                                  "text": "t", "likes": 1, "published_at": NOW}]})
    summary = comments.run(conn, config, client=client, limit=3)
    assert summary["videos"] == 1
    assert summary["units"] == 1


def test_run_units_fallback_without_quota_rows(conn):
    add_channel(conn)
    add_video(conn, "v1", is_ai=1)
    add_snap(conn, "v1", 10, NOW - 3600)
    client = FakeClient({"v1": []})
    summary = comments.run(conn, config, client=client, limit=3)
    assert summary["units"] == config.COST_COMMENT_THREADS


# --- yt.comment_threads ----------------------------------------------------


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params))
        return FakeResp(*self.resp)


def _client_with(resp):
    session = FakeSession(resp)
    client = yt.YouTubeClient(keys=["k1"], session=session, min_interval=0,
                              sleep=lambda *_: None)
    # Ключ уже проверен: без этого первый запрос уйдёт на probe (videos.list).
    client._valid.add("k1")
    return client, session


def test_comment_threads_parses_flat_list(tmp_path):
    payload = {
        "items": [
            {
                "id": "thread1",
                "snippet": {
                    "topLevelComment": {
                        "id": "c1",
                        "snippet": {
                            "authorDisplayName": "Вася",
                            "textDisplay": "Отличное видео",
                            "likeCount": 12,
                            "publishedAt": "2024-01-02T03:04:05Z",
                        },
                    }
                },
            }
        ]
    }
    client, session = _client_with((200, payload))
    out = client.comment_threads("vid1", max_results=20)
    assert out == [{
        "comment_id": "c1",
        "author": "Вася",
        "text": "Отличное видео",
        "likes": 12,
        "published_at": 1704164645,
    }]
    _, params = session.requests[0]
    assert params["part"] == "snippet"
    assert params["videoId"] == "vid1"
    assert params["order"] == "relevance"
    assert params["textFormat"] == "plainText"
    assert params["maxResults"] == 20


def test_comment_threads_disabled_raises_video_level(tmp_path):
    """403 commentsDisabled — уровневая ошибка видео, ключ остаётся рабочим."""
    payload = {"error": {"errors": [{"reason": "commentsDisabled"}]}}
    client, session = _client_with((403, payload))
    with pytest.raises(yt.YouTubeError) as err:
        client.comment_threads("vid1")
    assert err.value.code == 403
    assert len(session.requests) == 1  # без ротации ключа
    assert client._bad == set()


def test_comment_threads_forbidden_is_video_level(tmp_path):
    """Дефект 1: 403 forbidden — 1 запрос, ключ рабочий, повтор тем же ключом."""
    payload = {"error": {"errors": [{"reason": "forbidden"}]}}
    client, session = _client_with((403, payload))

    with pytest.raises(yt.YouTubeError) as err:
        client.comment_threads("vid1")
    assert err.value.code == 403
    assert len(session.requests) == 1
    assert client._bad == set()
    assert client.usable_keys_count() == 1

    with pytest.raises(yt.YouTubeError):
        client.comment_threads("vid2")
    assert len(session.requests) == 2
    assert client._bad == set()


# --- report.top_comment ----------------------------------------------------


def test_top_comment_picks_most_liked(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_comment(conn, "a", "v1", text="слабый", likes=1)
    add_comment(conn, "b", "v1", text="сильный", likes=99)
    best = report.top_comment(conn, "v1")
    assert best["text"] == "сильный"
    assert best["likes"] == 99


def test_top_comment_none_when_empty(conn):
    add_channel(conn)
    add_video(conn, "v1")
    assert report.top_comment(conn, "v1") is None
