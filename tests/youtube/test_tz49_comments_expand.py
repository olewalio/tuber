"""ТЗ-49: расширенный сбор комментариев YouTube и харвест комментаторов.

Проверяем новые свойства контура 6:
* пагинация ``comment_threads_page`` — несколько страниц с одного видео;
* харвест авторов с likes ≥ порога в очередь кандидатов с пометкой
  происхождения «комментатор вирального видео» и дедупликацией;
* ``dry_run`` ничего не пишет и не тратит units;
* расход квоты показывается числом (units за прогон и за сутки).

Сеть не трогаем: клиент подменяется фейком; база — временная.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tuber.platforms.youtube import comments, config, store as db, api as yt

NOW = int(time.time())


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "tz49.db")
    db.init_db(c)
    yield c
    c.close()


def _cfg(**over):
    base = {
        "COMMENT_MAX_VIDEOS_PER_RUN": 50,
        "COMMENT_MAX_PER_VIDEO": 500,
        "COMMENT_FETCH_MAX": 100,
        "COMMENT_PAGES_PER_VIDEO": 5,
        "COMMENT_VIRAL_TOP_N": 50,
        "COMMENT_CHANNEL_TOP_N": 3,
        "COMMENT_CHANNEL_VIDEOS": 5,
        "COMMENT_HARVEST_MIN_LIKES": 50,
        "COMMENT_SWEEP_FRESH_HOURS": 24,
        "COMMENT_REFRESH_DAYS": 7,
        "COMMENT_VIRAL_WINDOW_DAYS": 14,
        "COST_COMMENT_THREADS": 1,
    }
    base.update(over)
    return SimpleNamespace(**base)


def add_channel(conn, cid="c1", title="Канал"):
    db.upsert_channel(conn, {"channel_id": cid, "title": title, "first_seen": 1})


def add_video(conn, vid, cid="c1", is_ai=1):
    db.upsert_video(conn, {
        "video_id": vid, "channel_id": cid, "title": f"Видео {vid}",
        "published_at": NOW - 86400, "first_seen": 1,
    })
    db.save_classification(conn, vid, is_ai=is_ai, topic="ai",
                           title_ru=f"Видео {vid}", lang="ru", confidence=0.9)


def add_snap(conn, vid, comments_count, captured_at, views=1000):
    db.insert_snapshot(conn, vid, captured_at, "d",
                       views=views, likes=0, comments=comments_count)


class PageClient:
    """Клиент с пагинацией: страницы задаются списком на видео."""

    def __init__(self, pages_by_video):
        self.pages_by_video = pages_by_video
        self.calls = []

    def comment_threads_page(self, video_id, max_results=100, page_token=None):
        self.calls.append((video_id, page_token))
        pages = self.pages_by_video.get(video_id, [])
        idx = 0 if page_token is None else int(page_token)
        items = pages[idx] if idx < len(pages) else []
        nxt = str(idx + 1) if idx + 1 < len(pages) else None
        return list(items), nxt


def _c(cid, likes=0, author="Вася", author_channel_id=None, text=None):
    return {"comment_id": cid, "author": author, "text": text or f"текст {cid}",
            "likes": likes, "published_at": NOW,
            "author_channel_id": author_channel_id}


def test_pagination_collects_all_pages(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_snap(conn, "v1", 10, NOW - 3600)
    client = PageClient({"v1": [
        [_c("c1"), _c("c2")],
        [_c("c3")],
    ]})
    summary = comments.run(conn, _cfg(COMMENT_MAX_VIDEOS_PER_RUN=1), client=client, limit=1)
    assert summary["videos"] == 1
    assert summary["pages"] == 2
    assert summary["comments"] == 3
    assert {r["comment_id"] for r in conn.execute(
        "SELECT comment_id FROM video_comments")} == {"c1", "c2", "c3"}


def test_dry_run_writes_nothing_and_spends_zero(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_snap(conn, "v1", 100, NOW - 3600)
    before = conn.execute("SELECT COUNT(*) AS n FROM content_comment").fetchone()["n"]
    summary = comments.run(conn, _cfg(), limit=5, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["units"] == 0
    after = conn.execute("SELECT COUNT(*) AS n FROM content_comment").fetchone()["n"]
    assert after == before
    harvest = conn.execute(
        "SELECT COUNT(*) AS n FROM candidate WHERE found_via=?",
        (comments.COMMENT_FOUND_VIA,)).fetchone()["n"]
    assert harvest == 0


def test_harvest_commenters_into_candidate_queue(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_snap(conn, "v1", 100, NOW - 3600)
    client = PageClient({"v1": [[
        _c("c1", likes=80, author="Аня", author_channel_id="UC_anya"),
        _c("c2", likes=10, author="Пётр", author_channel_id="UC_petr"),
        _c("c3", likes=120, author="Аня", author_channel_id="UC_anya"),
    ]]})
    summary = comments.run(conn, _cfg(COMMENT_MAX_VIDEOS_PER_RUN=1), client=client, limit=1)
    assert summary["harvested"]["added"] == 1
    row = conn.execute(
        "SELECT handle, found_via, status, validated, meta_json FROM candidate "
        "WHERE platform='youtube' AND found_via=?",
        (comments.COMMENT_FOUND_VIA,)).fetchone()
    assert row["handle"] == "UC_anya"          # likes 10 ниже порога
    assert row["found_via"] == comments.COMMENT_FOUND_VIA
    assert row["status"] == "new" and row["validated"] == "pending"
    assert comments.COMMENT_ORIGIN in row["meta_json"]


def test_harvest_dedup_bumps_seen_count(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_snap(conn, "v1", 100, NOW - 3600)
    items = [_c("c1", likes=99, author="Аня", author_channel_id="UC_anya")]
    comments.harvest_commenters(conn, items, cfg=_cfg(), now=NOW, video_id="v1")
    comments.harvest_commenters(conn, items, cfg=_cfg(), now=NOW + 10, video_id="v1")
    rows = conn.execute(
        "SELECT seen_count FROM candidate WHERE platform='youtube' AND handle='UC_anya'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["seen_count"] == 2


def test_harvest_skips_known_source(conn):
    add_channel(conn, cid="UC_anya", title="Уже известный канал")
    comments.harvest_commenters(
        conn, [_c("c1", likes=99, author_channel_id="UC_anya")],
        cfg=_cfg(), now=NOW)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM candidate WHERE platform='youtube' "
        "AND handle='UC_anya'").fetchone()["n"] == 0


def test_units_today_counts_commentthreads(conn):
    db.log_quota(conn, NOW, "k1", 3, 3, "commentThreads")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert comments.units_today(conn, today) == 3
    assert comments.units_today(conn, "1970-01-01") == 0


def test_select_targets_prefers_viral_top(conn):
    """Виральные видео идут первыми, лидеры роста — после них."""
    import json

    add_channel(conn)
    start = NOW - 8 * 3600
    add_video(conn, "viral")
    db.insert_snapshot(conn, "viral", start, "d", views=1000, likes=0, comments=1)
    db.insert_snapshot(conn, "viral", start + 10800, "d", views=5000, likes=10,
                       comments=1)
    db.save_score(conn, "viral", NOW, likes_per_1000=2.0, outlier_score=2.0)
    db.set_viral_indices(conn, [("viral", NOW, 9.5, json.dumps(
        {"viral": {"axes": {"views": 5.0, "likes": 2.0}, "index": 9.5}}))])
    add_video(conn, "leader")
    add_snap(conn, "leader", 0, start)
    add_snap(conn, "leader", 60, start + 3600)
    ids = comments.select_targets(conn, _cfg(), limit=10)
    assert ids[0] == "viral"
    assert "leader" in ids
