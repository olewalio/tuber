"""Общий предикат «пост про ИИ» (ТЗ-8 задача 1).

Приговор модели (`classified.is_ai`) — это ДАННЫЕ: их не переписываем и не
удаляем. Но участвовать в сюжетах, оценках значимости, тёмных лошадках, первых
авторах и в отчёте могут только посты, для которых есть успешный приговор
`is_ai=1`. Посты с `is_ai=0` и посты БЕЗ приговора (ещё не классифицированные)
исключаются.

Предикат живёт здесь в одном месте и переиспользуется всеми выборками, чтобы не
было копипасты по файлам. Связь с приговором — по `text_hash` (кэш
классификации, Р1.4).
"""
from __future__ import annotations


def ai_join(post_alias="p", classified_alias="c_ai"):
    """JOIN таблицы posts к её приговору. В выборку попадают только is_ai=1."""
    return (f"JOIN classified {classified_alias}"
            f" ON {classified_alias}.text_hash={post_alias}.text_hash"
            f" AND {classified_alias}.is_ai=1")


# Готовый предикат для часто используемого алиаса posts AS p.
AI_JOIN = ai_join("p", "c_ai")


def dropped_non_ai(con, start_iso, end_iso):
    """Сколько постов за окно отброшено именно как НЕ про ИИ (`is_ai=0`).

    Посты без приговора сюда не входят: их отсев — «ещё не классифицирован»,
    а не приговор модели.
    """
    return con.execute(
        "SELECT COUNT(*) FROM posts p JOIN classified c ON c.text_hash=p.text_hash"
        " WHERE c.is_ai=0"
        " AND p.published_at_utc >= ? AND p.published_at_utc < ?",
        (start_iso, end_iso)).fetchone()[0]


def is_ai_post(con, tweet_id):
    """True, если у поста есть приговор модели `is_ai=1`."""
    row = con.execute(
        "SELECT c.is_ai FROM posts p JOIN classified c ON c.text_hash=p.text_hash"
        " WHERE p.tweet_id=? AND c.is_ai=1 LIMIT 1", (str(tweet_id),)).fetchone()
    return row is not None
