"""Регресс D-27: тайбрейк «первого поста сюжета» воспроизводит порядок legacy.

Дефект (замер заказчика на замороженных копиях, отчёт 2026-09-15): в сюжете
два поста @Cointelegraph с ОДНОЙ секундой публикации (``13:25:00``). Запрос
отчёта сортировал только по ``published_at_utc``, и при равном ключе выбор
диктовал план:

* legacy читал ``story_posts`` по первичному ключу ``(story_id, tweet_id)``
  (``sqlite_autoindex_story_posts_1``) — при равном времени побеждал пост с
  лексикографически меньшим ``tweet_id`` (``2099851980843925762``);
* ядро читает ``story_member`` по ``(story_id, content_id)``, а ``content_id`` —
  это порядок ВСТАВКИ в ``content`` (порядок миграции), не семантический: был
  выбран ``2099851981557035395``.

Правило: тайбрейк по ``tweet_id`` ASC, тот же вторичный ключ, что уже
используют ``stories.cluster_posts``/``assign_roles`` (``(published_at_utc,
str(tweet_id))``). Тест проверяет, что результат НЕ зависит от порядка вставки
постов (то есть от ``content_id``), и падает, если тайбрейк убрать.
"""
from __future__ import annotations

from tuber.platforms.x import report

EARLIER_TWEET = "2099851980843925762"   # меньший tweet_id — выбор legacy
LATER_TWEET = "2099851981557035395"     # больший tweet_id — выбор ядра БЕЗ тайбрейка
SAME_SECOND = "2026-09-15T13:25:00"     # обе публикации в одну секунду


def _seed(con, insert_order):
    """Один сюжет, два кандидата с равным временем; порядок вставки задаётся.

    ``content_id`` присваивается по порядку вставки в ``content``. Без явного
    тайбрейка ``story_member(story_id, content_id)`` вернёт первым тот пост,
    который вставлен раньше, — то есть выбор зависел бы от порядка миграции.
    """
    con.execute("INSERT INTO accounts(id, handle, status) VALUES (1, 'cointelegraph', 'active')")
    for tid in insert_order:
        con.execute(
            "INSERT INTO posts(account_id, tweet_id, published_at_utc, text, text_hash,"
            " author_handle, first_seen_at) VALUES (1, ?, ?, ?, ?, 'Cointelegraph', ?)",
            (tid, SAME_SECOND, f"text of {tid}", f"hash-{tid}", SAME_SECOND))
    con.execute(
        "INSERT INTO stories(id, created_at, published_at, xconf, post_count)"
        " VALUES (1, ?, ?, 1, 2)", (SAME_SECOND, SAME_SECOND))
    for tid in insert_order:
        con.execute(
            "INSERT INTO story_posts(story_id, tweet_id, handle, role, added_at)"
            " VALUES (1, ?, 'cointelegraph', 'echo', ?)", (tid, SAME_SECOND))
    con.commit()


def test_primary_post_tiebreak_matches_legacy(db_path):
    """Два равных кандидата → выбран меньший tweet_id (порядок legacy)."""
    from tuber.platforms.x import store as db

    con = db.connect(db_path)
    try:
        # Вставляем СНАЧАЛА больший tweet_id: без тайбрейка он же первым и
        # вернётся (content_id меньше), то есть тест ловит удаление ключа.
        _seed(con, [LATER_TWEET, EARLIER_TWEET])
        first = report._primary_post(con, {"id": 1})
        assert first is not None
        assert first["tweet_id"] == EARLIER_TWEET, (
            "тайбрейк по tweet_id потерян: выбран пост по порядку content_id")
    finally:
        con.close()


def test_story_claim_tiebreak_matches_legacy(db_path):
    """Зачин сюжета (для рубрики/суммы) выбирается тем же тайбрейком."""
    from tuber.platforms.x import store as db

    con = db.connect(db_path)
    try:
        _seed(con, [LATER_TWEET, EARLIER_TWEET])
        claim = report._story_claim(con, 1)
        assert claim is not None
        assert claim["text"] == f"text of {EARLIER_TWEET}"
    finally:
        con.close()


def test_tiebreak_independent_of_insertion_order(db_path):
    """Результат не зависит от порядка вставки (content_id) — тот же, что legacy."""
    from tuber.platforms.x import store as db

    results = []
    for order in ([LATER_TWEET, EARLIER_TWEET], [EARLIER_TWEET, LATER_TWEET]):
        con = db.connect(db_path)
        try:
            con.execute("DELETE FROM story_posts")
            con.execute("DELETE FROM classified")
            con.execute("DELETE FROM posts")
            con.execute("DELETE FROM stories")
            con.execute("DELETE FROM accounts")
            con.commit()
            _seed(con, order)
            results.append(report._primary_post(con, {"id": 1})["tweet_id"])
        finally:
            con.close()
    assert results[0] == results[1] == EARLIER_TWEET
