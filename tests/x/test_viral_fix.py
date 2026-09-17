"""ТЗ «починка виральности»: автор поста, ось «распространение», чистка оценок.

Проверяются задачи 1, 2, 3, 5 и 6. Сеть не используется; БД временная (conftest).
"""
import sqlite3
from datetime import datetime, timezone

from tuber.platforms.x import config, store as db, enrich, report, scores, stories
from tuber.platforms.x.registry import text_hash

from tests.x.test_tz3 import acc, post
from tests.x.test_tz8 import classify

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


# --------------------------------------------------- задача 1: миграция/колонка
def test_migration_adds_author_handle_column(db_path):
    """Отдельная колонка реального автора есть и переживает повторный migrate.

    Прежняя версия проверяла legacy-миграцию ``ALTER TABLE posts ADD COLUMN
    author_handle``. В монорепозитории колонка живёт в ядре
    (``content.author_handle``), а адаптер отдаёт её как ``posts.author_handle``
    (без правки плоского SQL X-кода) — проверяется то же наблюдаемое свойство.
    """
    con = db.init_db(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    assert "author_handle" in cols
    assert db.SCHEMA_VERSION == 8   # legacy-номер версии сохранён в API адаптера
    db.migrate(con)                 # идемпотентно: повтор не падает
    assert "author_handle" in {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    con.close()


def test_apply_metrics_fills_author_and_keeps_owner(con):
    a = acc(con, "feed_owner")
    post(con, a, "1", "a post about GPT", handle="feed_owner", metrics=False)
    res = enrich.apply_metrics(con, "1", {"screen_name": "RealAuthor", "likes": 5,
                                          "replies": 1, "author_verified": 1})
    assert res["updated"] == 1
    row = con.execute("SELECT owner_handle, author_handle FROM posts"
                      " WHERE tweet_id='1'").fetchone()
    assert row["owner_handle"] == "feed_owner"   # владелец ленты не тронут
    assert row["author_handle"] == "RealAuthor"  # реальный автор из CDN


def test_post_author_prefers_author_handle(con):
    assert db.post_author({"author_handle": "Real", "owner_handle": "feed"}) == "Real"
    assert db.post_author({"owner_handle": "feed"}) == "feed"
    assert db.post_author({"acc_handle": "acc"}) == "acc"
    assert db.post_author({}) is None
    assert db.post_author(None) is None


# --------------------------------------------------------- задача 2: автор везде
def test_xconf_counts_real_authors_not_feed_owner(con):
    a = acc(con, "megafeed")
    post(con, a, "1", "text one about GPT", handle="megafeed")
    post(con, a, "2", "text two about GPT", handle="megafeed")
    con.execute("UPDATE posts SET author_handle='alice' WHERE tweet_id='1'")
    con.execute("UPDATE posts SET author_handle='bob' WHERE tweet_id='2'")
    con.commit()
    rows = list(con.execute("SELECT * FROM posts"))
    assert stories.xconf(rows) == 2  # два РЕАЛЬНЫХ автора, а не один владелец ленты


# ---------------------------------------------------- задача 3: ось распространения
def test_spread_takes_max_of_xconf_and_graph():
    sig, _e, _v, spread = scores.significance_values(
        likes6=0, replies6=0, xconf=1, hours_since=0, metrics_missing=False,
        spread_graph=3)
    assert spread == 3
    # xconf больше графа — берётся xconf
    _s, _e, _v, spread2 = scores.significance_values(
        likes6=0, replies6=0, xconf=5, hours_since=0, metrics_missing=False,
        spread_graph=1)
    assert spread2 == 4


def test_spread_axis_not_degenerate_for_single_story(con):
    """Пост-одиночка сюжета (xconf=1), но с графом упоминаний, получает spread>0."""
    a = acc(con, "author_x")
    b = acc(con, "spreader")
    post(con, a, "1", "big GPT release about a new model", handle="author_x",
         likes=10, replies=1, published="2026-09-15T11:00:00")
    post(con, b, "2", "echoing @author_x about the release", handle="spreader",
         mentions=["author_x"], published="2026-09-15T11:30:00")
    classify(con, "big GPT release about a new model", 1)
    classify(con, "echoing @author_x about the release", 1)
    recs = {r["tweet_id"]: r for r in scores.compute(con)}
    assert recs["1"]["spread"] >= 1
    assert recs["1"]["spread_graph"] >= 1


# --------------------------------------------------------- задача 5: чистка оценок
def test_purge_stale_scores_when_post_drops_out(con):
    a = acc(con, "a")
    post(con, a, "1", "OpenAI agent toolkit release", handle="a",
         likes=10, replies=1, published="2026-09-15T10:00:00")
    post(con, a, "2", "Sugar Cosmetics restructures business", handle="a",
         likes=10, replies=1, published="2026-09-15T10:00:00")
    classify(con, "OpenAI agent toolkit release", 1)
    classify(con, "Sugar Cosmetics restructures business", 1)
    scores.compute(con, now=NOW)
    assert con.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 2
    # пост 2 получил приговор is_ai=0 -> должен выпасть из выборки и из scores
    classify(con, "Sugar Cosmetics restructures business", 0)
    stats = {}
    scores.compute(con, now=NOW, stats=stats)
    assert stats["removed_stale"] == 1
    left = {r[0] for r in con.execute("SELECT tweet_id FROM scores")}
    assert left == {"1"}


def test_purge_stale_keeps_rows_outside_window(con):
    a = acc(con, "a")
    post(con, a, "old", "old GPT story", handle="a", likes=1, replies=0,
         published="2025-01-01T00:00:00")
    classify(con, "old GPT story", 0)
    con.execute("INSERT INTO scores (tweet_id, computed_at, significance)"
                " VALUES ('old', ?, 1.0)", (db.utcnow_iso(),))
    con.commit()
    scores.compute(con, now=NOW, since_days=30)
    assert con.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


# ------------------------------------------------- задача 6: пустые сутки не молчат
def test_data_status_empty_and_not_empty(con):
    start, end = report.day_bounds("2026-09-15", now=NOW)
    assert report.data_status(con, start, end)["empty"] is True
    a = acc(con, "a")
    post(con, a, "1", "GPT release", handle="a", likes=5,
         published="2026-09-15T10:00:00")
    classify(con, "GPT release", 1)
    scores.compute(con, now=NOW)
    assert report.data_status(con, start, end)["empty"] is False
