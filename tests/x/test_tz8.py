"""ТЗ-8: приговор модели is_ai отсекает не-ИИ посты, каталог отчёта
переопределяется, пункт про кэш переводов честный.

Сеть не используется. БД — временная (фикстуры conftest).
"""
import os
import subprocess
import sys
from datetime import datetime, timezone

from tuber.platforms.x import ai_filter, config, report, scores, stories
from tuber.platforms.x.registry import text_hash

from tests.x.test_tz3 import acc, post

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


# ------------------------------------------------------------------ helpers
def classify(con, text, is_ai, *, topic="релизы моделей", claim="news"):
    """Приговор модели для конкретного текста (по text_hash)."""
    con.execute(
        "INSERT OR REPLACE INTO classified (text_hash, is_ai, topic, claim_type,"
        " status, method) VALUES (?,?,?,?, 'classified', 'model')",
        (text_hash(text), is_ai, topic, claim))
    con.commit()


# ====================================== ТЗ-8 задача 1: общий предикат is_ai
def test_ai_filter_join_and_tweet_helper(con):
    a = acc(con, "a")
    post(con, a, "1", "OpenAI releases a new agent toolkit", handle="a")
    post(con, a, "2", "Sugar Cosmetics restructures its business", handle="a")
    classify(con, "OpenAI releases a new agent toolkit", 1)
    classify(con, "Sugar Cosmetics restructures its business", 0)
    assert ai_filter.is_ai_post(con, "1") is True
    assert ai_filter.is_ai_post(con, "2") is False
    assert ai_filter.is_ai_post(con, "999") is False  # без приговора


def test_stories_skip_non_ai_and_unclassified(con):
    # Даты постов фиксированы относительно NOW: иначе `published_at_utc` берётся
    # от реального «сейчас» и при прогоне позже 12:00 UTC пост попадает «в
    # будущее» относительно NOW и выпадает из окна сюжетов.
    a = acc(con, "a")
    b = acc(con, "b")
    post(con, a, "1", "OpenAI releases a new agent toolkit today", handle="a",
         published="2026-09-15T11:00:00")
    post(con, b, "2", "Sugar Cosmetics restructures its business now", handle="b",
         published="2026-09-15T11:00:00")
    post(con, a, "3", "совсем не классифицированный текст про погоду", handle="c",
         published="2026-09-15T11:00:00")
    classify(con, "OpenAI releases a new agent toolkit today", 1)
    classify(con, "Sugar Cosmetics restructures its business now", 0)
    st = stories.run(con, now=NOW)
    assert st["posts"] == 1, "в окне должен остаться только пост с is_ai=1"
    ids = {r["tweet_id"] for r in con.execute("SELECT tweet_id FROM story_posts")}
    assert ids == {"1"}, f"в сюжеты попали лишние посты: {ids}"


def test_scores_skip_non_ai(con):
    a = acc(con, "a")
    post(con, a, "1", "OpenAI releases a new agent toolkit today", handle="a",
         likes=50, replies=5)
    post(con, a, "2", "Sugar Cosmetics restructures its business now", handle="a",
         likes=50, replies=5)
    classify(con, "OpenAI releases a new agent toolkit today", 1)
    classify(con, "Sugar Cosmetics restructures its business now", 0)
    ids = {r["tweet_id"] for r in scores.compute(con)}
    assert "1" in ids and "2" not in ids, "is_ai=0 не должен получать значимость"


def test_dropped_non_ai_counter(con):
    a = acc(con, "a")
    post(con, a, "1", "OpenAI releases a new agent toolkit today", handle="a",
         published="2026-09-15T11:00:00")
    post(con, a, "2", "Sugar Cosmetics restructures its business now", handle="a",
         published="2026-09-15T11:00:00")
    post(con, a, "3", "ещё один пост не про ИИ про спорт", handle="a",
         published="2026-09-15T11:00:00")
    classify(con, "OpenAI releases a new agent toolkit today", 1)
    classify(con, "Sugar Cosmetics restructures its business now", 0)
    classify(con, "ещё один пост не про ИИ про спорт", 0)
    start, end = report.day_bounds("2026-09-15", now=NOW)
    assert ai_filter.dropped_non_ai(con, report._iso(start), report._iso(end)) == 2


def test_block5_and_block7_respect_is_ai(con):
    a = acc(con, "a")
    post(con, a, "700", "русский пост про ИИ агентов", handle="a", lang="ru",
         published="2026-09-15T10:00:00")
    post(con, a, "701", "русский пост про косметику", handle="a", lang="ru",
         published="2026-09-15T09:00:00")
    post(con, a, "702", "русский пост вообще без приговора", handle="a", lang="ru",
         published="2026-09-15T08:00:00")
    classify(con, "русский пост про ИИ агентов", 1)
    classify(con, "русский пост про косметику", 0)
    start, end = report.day_bounds("2026-09-15", now=NOW)
    text = "\n".join(report.block5_russian(con, start, end))
    assert "ИИ агентов" in text
    assert "косметику" not in text and "без приговора" not in text
    service = "\n".join(report.block7_service(con, start, end, day="2026-09-15"))
    assert "Отброшено как не про ИИ: 1" in service


def test_report_blocks_1_3_4_have_no_non_ai(con):
    a = acc(con, "ai")
    b = acc(con, "notai")
    text_ai = "OpenAI releases a new agent toolkit for developers"
    text_no = "Sugar Cosmetics restructures its business now"
    post(con, a, "1", text_ai, handle="ai", published="2026-09-15T05:00:00")
    post(con, b, "2", text_ai + " today", handle="notai",
         published="2026-09-15T05:30:00")
    post(con, b, "9", text_no, handle="notai", published="2026-09-15T06:00:00")
    classify(con, text_ai, 1, topic="агенты и автоматизация")
    classify(con, text_ai + " today", 1, topic="агенты и автоматизация")
    classify(con, text_no, 0, topic="кейсы внедрения", claim="funding")
    stories.run(con, now=NOW)
    text = report.build(con, date="2026-09-15", translator=False, now=NOW)
    assert "Sugar Cosmetics" not in text
    assert "https://x.com/notai/status/9" not in text
    assert "Отброшено как не про ИИ: 1" in text


# ====================================== ТЗ-8 задача 2: каталог отчёта
def test_report_dir_env_override():
    env = dict(os.environ)
    env["TUBER_X_REPORT_DIR"] = "/tmp/tuber_x_report_env_check"
    env["PYTHONPATH"] = ROOT
    proc = subprocess.run(
        [sys.executable, "-c",
         "from tuber.platforms.x import config; print(config.REPORT_DIR)"],
        cwd=ROOT, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/tmp/tuber_x_report_env_check"


def test_report_write_uses_report_dir(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_DIR", str(tmp_path))
    path = report.write("содержимое\n", date="2026-09-15")
    assert path == str(tmp_path / "2026-09-15.md")
    assert os.path.exists(path)


# ====================================== ТЗ-8 задача 3: честный кэш переводов
def test_translation_verdict_three_cases():
    assert report.translation_verdict(0, 0)[0] == "nothing"
    assert "кандидатов 0" in report.translation_verdict(0, 0)[1]
    assert report.translation_verdict(5, 0)[0] == "fail"
    assert report.translation_verdict(5, 3)[0] == "ok"


def test_translation_candidates_counts_uncached(con):
    a = acc(con, "first")
    b = acc(con, "second")
    post(con, a, "t1", "OpenAI ships a new agent toolkit", handle="first",
         published="2026-09-15T05:00:00")
    post(con, b, "t2", "OpenAI ships a new agent toolkit now", handle="second",
         published="2026-09-15T06:00:00")
    classify(con, "OpenAI ships a new agent toolkit", 1)
    classify(con, "OpenAI ships a new agent toolkit now", 1)
    stories.run(con, now=NOW)
    assert report.translation_candidates(con, date="2026-09-15", now=NOW) == 1
    con.execute("INSERT INTO report_texts (text_hash, ru, src) VALUES (?,?,?)",
                (text_hash("OpenAI ships a new agent toolkit"), "перевод", "model"))
    con.commit()
    assert report.translation_candidates(con, date="2026-09-15", now=NOW) == 0
