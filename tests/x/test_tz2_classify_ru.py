"""Классификатор X пишет русские поля title_ru/summary_ru в ядро (ТЗ-Tuber §1.3).

Проверяется, что запись через legacy-представление ``classified`` доезжает до
``classification`` (проекция ядра), а поля длины обрезаются по схеме.
"""
from __future__ import annotations

from tuber.platforms.x import classify, store as db


def test_classify_upsert_projects_ru_fields(con):
    con.execute(
        "INSERT INTO content(id,platform,external_id,published_at,text)"
        " VALUES (9001,'x','tweet-1','2026-09-20 10:00:00','hello')")
    con.commit()
    classify._upsert(
        con, text_hash="hash-1", tweet_id="tweet-1", status="classified",
        method="model", model="fake",
        fields={"is_ai": 1, "topic": "релизы моделей", "subtopic": "модель",
                "claim_type": "release", "novelty": 0.9, "lang": "en",
                "title_ru": "Русский заголовок", "summary_ru": "Русское резюме."})
    row = con.execute(
        "SELECT title_ru, summary_ru, topic FROM classification"
        " WHERE external_id='tweet-1'").fetchone()
    assert row is not None
    assert row["title_ru"] == "Русский заголовок"
    assert row["summary_ru"] == "Русское резюме."
    # Кэш тоже несёт поля (проекция read-through).
    cache = con.execute(
        "SELECT title_ru, summary_ru FROM classify_cache WHERE text_hash='hash-1'"
    ).fetchone()
    assert cache["title_ru"] == "Русский заголовок"


def test_classify_upsert_optional_ru_fields(con):
    con.execute(
        "INSERT INTO content(id,platform,external_id,published_at,text)"
        " VALUES (9002,'x','tweet-2','2026-09-20 10:00:00','hello')")
    con.commit()
    classify._upsert(
        con, text_hash="hash-2", tweet_id="tweet-2", status="classified",
        method="model", model="fake",
        fields={"is_ai": 1, "topic": "релизы моделей", "claim_type": "release",
                "novelty": 0.5, "lang": "en"})
    row = con.execute(
        "SELECT title_ru, summary_ru FROM classification"
        " WHERE external_id='tweet-2'").fetchone()
    assert row is not None
    assert row["title_ru"] is None and row["summary_ru"] is None
