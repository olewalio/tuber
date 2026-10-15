"""Дефект 2026-10-03: окно суток применялось ПОСЛЕ топ-N главного блока.

``main_stories`` сортировал ВСЮ таблицу по ``xconf`` и только потом обрезал до
``LIMIT``; ``report._main_rows`` фильтровал уже обрезанный срез. ``xconf``
(число независимых авторов) копится со временем, поэтому глобальный топ ВСЕГДА
занят старыми сюжетами, и блок 1 печатал NO_DATA при непустых данных за сутки.

Тесты фиксируют новое правило: окно уходит в SQL ДО ``LIMIT``; внутри суток
порядок детерминирован (``xconf DESC, post_count DESC, published_at DESC``).
"""
from __future__ import annotations

from tuber.platforms.x import report, store as db, stories


def _seed_account(con):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('acc','A','active')")
    return con.execute("SELECT id FROM accounts WHERE handle='acc'").fetchone()["id"]


def _seed_story(con, acc_id, tid, published_iso, xconf, post_count, topic,
                suspect=0, is_single=0):
    """Вставить пост+сюжет+участника. Возвращает НАСТОЯЩИЙ id сюжета.

    ``INSERT INTO stories`` идёт через представление; его триггер сам назначает
    ``story.id`` (``NEW.id`` не используется) — настоящий id достаём из
    ``x_last_insert`` через ``store.last_insert_id``.
    """
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
        " text, text_hash, mentions, links, is_retweet, is_quote, owner_handle,"
        " lang, first_seen_at)"
        " VALUES (?,?,?,'rss',?,?, '[]','[]',0,0, 'acc', 'ru', ?)",
        (acc_id, tid, published_iso, f"text {tid}", f"h_{tid}", published_iso))
    con.execute(
        """INSERT INTO stories (created_at, window_hours, threshold,
             first_tweet_id, first_mover, published_at, xconf, post_count,
             lead_time_min, topics, entities, is_new_entity, is_single, suspect,
             claimed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
        (published_iso.replace("T", " "), 24, 1, tid, "acc", published_iso,
         xconf, post_count, 0, f'["{topic}"]', "[]", is_single, suspect,
         published_iso.replace("T", " ")))
    sid = db.last_insert_id(con)
    con.execute(
        "INSERT INTO classified (text_hash, tweet_id, topic, status)"
        " VALUES (?,?,?,'classified')", (f"h_{tid}", tid, topic))
    con.execute(
        "INSERT INTO story_posts (story_id, tweet_id, handle, role, added_at)"
        " VALUES (?,?,?,?,?)", (sid, tid, "acc", "primary", published_iso))
    con.commit()
    return sid


def _text(con, start, end):
    return "\n".join(report.block1_main(con, start, end, translator=None))


def test_main_rows_ignores_global_top_outside_window(con):
    """Топ-N (20 сюжетов) лежит ВНЕ окна; один подходящий внутри — он и виден."""
    acc = _seed_account(con)
    # Глобальный топ: старые сюжеты с большим xconf, все вне окна 02.10.
    for i in range(20):
        _seed_story(con, acc, f"old{i}", f"2026-09-30T{i:02d}:00:00",
                    xconf=100 - i, post_count=200 - i, topic=f"старое-{i}")
    # Единственный подходящий сюжет ВНУТРИ окна, xconf заведомо меньше.
    inside = _seed_story(con, acc, "in1", "2026-10-02T12:00:00",
                         xconf=2, post_count=2, topic="тема-внутри")

    start, end = report.day_bounds("2026-10-02")
    rows = report._main_rows(con, start, end)
    assert [r["id"] for r in rows] == [inside], [r["id"] for r in rows]
    text = _text(con, start, end)
    assert "тема-внутри" in text, text
    assert report.NO_DATA not in text, text


def test_window_boundaries_inclusive_start_exclusive_end(con):
    """Ровно 00:00:00 суток попадает; ровно 00:00:00 следующих — нет."""
    acc = _seed_account(con)
    start, end = report.day_bounds("2026-10-02")
    start_id = _seed_story(con, acc, "b-start", "2026-10-02T00:00:00",
                           xconf=2, post_count=1, topic="тема-начало")
    _seed_story(con, acc, "b-end", "2026-10-03T00:00:00",
                xconf=2, post_count=1, topic="тема-конец")

    rows = report._main_rows(con, start, end)
    assert [r["id"] for r in rows] == [start_id], [r["id"] for r in rows]
    text = _text(con, start, end)
    assert "тема-начало" in text, text
    assert "тема-конец" not in text, text


def test_empty_day_still_no_data(con):
    """Если внутри окна подходящих сюжетов нет — NO_DATA остаётся."""
    acc = _seed_account(con)
    # В окне только suspect/single и один вне окна.
    _seed_story(con, acc, "s1", "2026-10-02T10:00:00",
                xconf=5, post_count=5, topic="подозр", suspect=1)
    _seed_story(con, acc, "s2", "2026-10-02T11:00:00",
                xconf=1, post_count=1, topic="одиноч", is_single=1)
    _seed_story(con, acc, "s3", "2026-09-29T11:00:00",
                xconf=99, post_count=99, topic="старое")

    start, end = report.day_bounds("2026-10-02")
    assert report._main_rows(con, start, end) == []
    text = _text(con, start, end)
    assert report.NO_DATA in text, text


def test_order_deterministic_on_equal_xconf(con):
    """При равных xconf выше сюжет с большим post_count, затем более свежий."""
    acc = _seed_account(con)
    sid1 = _seed_story(con, acc, "p1", "2026-10-02T08:00:00",
                       xconf=2, post_count=2, topic="posts2")
    sid2 = _seed_story(con, acc, "p3", "2026-10-02T07:00:00",
                       xconf=2, post_count=3, topic="posts3")
    sid3 = _seed_story(con, acc, "p2new", "2026-10-02T09:00:00",
                       xconf=2, post_count=2, topic="posts2new")
    sid4 = _seed_story(con, acc, "p3old", "2026-10-02T06:00:00",
                       xconf=2, post_count=3, topic="posts3old")

    start, end = report.day_bounds("2026-10-02")
    ids = [s["id"] for s in report._main_rows(con, start, end)]
    # post_count 3 выше post_count 2; внутри равного post_count — свежее выше.
    assert ids == [sid2, sid4, sid3, sid1], ids


def test_main_stories_window_applied_in_sql(con):
    """main_stories с окном возвращает только строки окна, даже если топ вне его."""
    acc = _seed_account(con)
    for i in range(20):
        _seed_story(con, acc, f"o{i}", f"2026-09-30T{i % 24:02d}:10:00",
                    xconf=100 - i, post_count=200 - i, topic=f"old{i}")
    inside = _seed_story(con, acc, "win", "2026-10-02T12:00:00",
                         xconf=1, post_count=1, topic="inside")
    start, end = report.day_bounds("2026-10-02")
    rows = stories.main_stories(con, limit=15, start=start, end=end)
    assert [r["id"] for r in rows] == [inside], [r["id"] for r in rows]
