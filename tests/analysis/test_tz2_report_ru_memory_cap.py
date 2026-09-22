"""Тесты ТЗ-Tuber: русские описания X, память выдачи, кэп на автора,
окно по публикации, честная маркировка автора и счётчики (ТЗ §1–§7).

Сеть и LLM не используются: pytest-окружение отключает живой перевод, а кэш
``report_text`` подкладывается в тестовую базу.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tuber.analysis import digest_memory, report
from tuber.core import db, schema


def _iso(days_ago: float = 1.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def build_core(tmp_path, *, yt=(), x=()):
    """Собрать ядро с YouTube и X.

    ``yt`` — список ``(source_id, {handle,title,views,vpd,age,lang})``.
    ``x``  — список ``{author,source,text,likes,age,text_hash,title_ru,summary_ru}``.
    """
    conn = db.connect(str(tmp_path / "tz.db"))
    schema.init_schema(conn)
    cid = 1000
    with db.write_tx(conn):
        seen_yt = set()
        for src, v in yt:
            if src in seen_yt:
                continue
            seen_yt.add(src)
            conn.execute(
                "INSERT INTO source(id,platform,external_id,handle,title,lang)"
                " VALUES (?,?,?,?,?,?)",
                (src, "youtube", f"UC{src}", v["handle"], v["title"],
                 v.get("lang", "en")))
        yid = 2000
        for src, v in yt:
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,lang,title,url,kind) VALUES (?,?,?,?,?,?,?,?,'video')",
                (yid, "youtube", src, f"v{yid}", _iso(v["age"]),
                 v.get("lang", "en"), v["title"],
                 f"https://www.youtube.com/watch?v=v{yid}"))
            conn.execute(
                "INSERT INTO metric_snapshot(content_id,captured_at,"
                "interval_quality,views,views_per_day,platform,external_id)"
                " VALUES (?,?,'ok',?,?,?,?)",
                (yid, _iso(0.5), v["views"], v["vpd"], "youtube", f"v{yid}"))
            yid += 1
        x_sources: dict = {}
        for i, p in enumerate(x):
            src_name = p["source"]
            if src_name not in x_sources:
                sid = 500 + len(x_sources)
                x_sources[src_name] = sid
                conn.execute(
                    "INSERT INTO source(id,platform,external_id,handle,title)"
                    " VALUES (?,?,?,?,?)",
                    (sid, "x", f"xd{sid}", src_name, src_name))
            sid = x_sources[src_name]
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,text_hash,author_handle,url,is_repost,lang)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                (cid, "x", sid, f"t{i}", _iso(p["age"]), p["text"],
                 p.get("text_hash"), p["author"],
                 f"https://x.com/{p['source']}/status/{cid}", p.get("lang", "en")))
            conn.execute(
                "INSERT INTO content_latest(content_id,likes,platform,external_id)"
                " VALUES (?,?,?,?)", (cid, p["likes"], "x", f"t{i}"))
            if p.get("title_ru") or p.get("summary_ru"):
                conn.execute(
                    "INSERT INTO classification(content_id,platform,title_ru,"
                    "summary_ru) VALUES (?,?,?,?)",
                    (cid, "x", p.get("title_ru"), p.get("summary_ru")))
            cid += 1
    return conn


# --------------------------------------------------------------------------- #
# Память выдачи (ТЗ §2)
# --------------------------------------------------------------------------- #
def test_digest_memory_schema_and_idempotency(tmp_path):
    conn = db.connect(str(tmp_path / "m.db"))
    try:
        schema.init_schema(conn)
        digest_memory.ensure_schema(conn)
        digest_memory.ensure_schema(conn)  # идемпотентно
        day1 = [(1, "x", "X"), (2, "youtube", "YouTube")]
        assert digest_memory.mark_sent(conn, day1, "2026-09-19 10:00:00") == 2
        # Повтор за ту же дату — без дублей (ТЗ §2.3).
        assert digest_memory.mark_sent(conn, day1, "2026-09-19 10:00:00") == 0
        day2 = [(3, "x", "X"), (4, "youtube", "YouTube")]
        assert digest_memory.mark_sent(conn, day2, "2026-09-20 10:00:00") == 2
        now = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
        assert digest_memory.recent_ids(conn, days=1, now=now) == {3, 4}
        assert digest_memory.recent_ids(conn, days=3, now=now) == {1, 2, 3, 4}
        rows = conn.execute("SELECT COUNT(*) FROM digest_sent").fetchone()[0]
        assert rows == 4
    finally:
        conn.close()


def test_digest_memory_env_off(monkeypatch):
    monkeypatch.setenv("TUBER_DIGEST_MEMORY", "0")
    assert digest_memory.enabled() is False
    monkeypatch.delenv("TUBER_DIGEST_MEMORY")
    assert digest_memory.enabled() is True
    monkeypatch.setenv("TUBER_DIGEST_MEMORY_DAYS", "5")
    assert digest_memory.memory_days() == 5
    monkeypatch.setenv("TUBER_DIGEST_MEMORY_DAYS", "не число")
    assert digest_memory.memory_days() == digest_memory.DIGEST_MEMORY_DAYS


def test_recent_ids_absent_table_is_empty(tmp_path):
    conn = db.connect(str(tmp_path / "empty.db"))
    try:
        schema.init_schema(conn)
        assert digest_memory.recent_ids(conn, days=3) == set()
    finally:
        conn.close()


def test_compact_excludes_memory_and_prints_honest_empty(tmp_path):
    yt = [(1, {"handle": "yt1", "title": "Видео один", "views": 100_000,
               "vpd": 900, "age": 5.0})]
    conn = build_core(tmp_path, yt=yt)
    try:
        digest_memory.ensure_schema(conn)
        digest_memory.mark_sent(conn, [(2000, "youtube", "YouTube")])
        text = report.build_compact(conn, days=10, db_path=":memory:")
        assert "Видео один" not in text
        assert "все позиции показывались" in text
    finally:
        conn.close()


def test_memory_flag_off_keeps_position(tmp_path, monkeypatch):
    yt = [(1, {"handle": "yt1", "title": "Видео один", "views": 100_000,
               "vpd": 900, "age": 5.0})]
    conn = build_core(tmp_path, yt=yt)
    try:
        digest_memory.ensure_schema(conn)
        digest_memory.mark_sent(conn, [(2000, "youtube", "YouTube")])
        monkeypatch.setenv("TUBER_DIGEST_MEMORY", "0")
        text = report.build_compact(conn, days=10, db_path=":memory:")
        assert "Видео один" in text
    finally:
        monkeypatch.delenv("TUBER_DIGEST_MEMORY")
        conn.close()


# --------------------------------------------------------------------------- #
# Русские описания X (ТЗ §1)
# --------------------------------------------------------------------------- #
def test_x_ru_description_priority(tmp_path):
    x = [
        {"author": "a", "source": "a", "text": "raw english text",
         "likes": 100, "age": 0.5, "text_hash": "h1",
         "summary_ru": "Русское резюме", "title_ru": "Русский заголовок"},
        {"author": "b", "source": "b", "text": "raw english 2",
         "likes": 90, "age": 0.5, "text_hash": "h2", "title_ru": "Только заголовок"},
    ]
    conn = build_core(tmp_path, x=x)
    try:
        # Кэш переводчика: h3 → русский (приоритет ниже title_ru/summary_ru).
        with db.write_tx(conn):
            conn.execute(
                "INSERT INTO report_text(text_hash, ru, created_at, src)"
                " VALUES ('h2','Кэш перевода', ?, 'model')", (_iso(0.1),))
        lines, _ = report.x_section(conn, _iso(10))
        body = "\n".join(lines)
        assert "Русское резюме" in body          # summary_ru — высший приоритет
        assert "Только заголовок" in body        # title_ru — второй приоритет
        assert "raw english text" not in body    # сырой текст не печатается
    finally:
        conn.close()


def test_x_ru_cache_used_and_flag_off(tmp_path, monkeypatch):
    x = [{"author": "a", "source": "a", "text": "raw english text",
          "likes": 100, "age": 0.5, "text_hash": "h9"}]
    conn = build_core(tmp_path, x=x)
    try:
        with db.write_tx(conn):
            conn.execute(
                "INSERT INTO report_text(text_hash, ru, created_at, src)"
                " VALUES ('h9','Перевод из кэша', ?, 'model')", (_iso(0.1),))
        lines, _ = report.x_section(conn, _iso(10))
        assert "Перевод из кэша" in "\n".join(lines)
        monkeypatch.setenv("TUBER_RU_DESC", "0")
        lines_off, _ = report.x_section(conn, _iso(10))
        assert "raw english text" in "\n".join(lines_off)
    finally:
        monkeypatch.delenv("TUBER_RU_DESC")
        conn.close()


def test_x_ru_no_cache_falls_back_to_raw(tmp_path):
    x = [{"author": "a", "source": "a", "text": "raw fallback text",
          "likes": 100, "age": 0.5, "text_hash": "nope"}]
    conn = build_core(tmp_path, x=x)
    try:
        lines, _ = report.x_section(conn, _iso(10))
        # В pytest живой перевод выключен: кэша нет → последний фолбэк.
        assert "raw fallback text" in "\n".join(lines)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Маркировка автора (ТЗ §5)
# --------------------------------------------------------------------------- #
def test_x_author_mismatch_labeled(tmp_path):
    x = [{"author": "real", "source": "feed", "text": "пост", "likes": 100,
          "age": 0.5, "text_hash": "m1"}]
    conn = build_core(tmp_path, x=x)
    try:
        lines, stats = report.x_section(conn, _iso(10))
        body = "\n".join(lines)
        assert "@real (в ленте @feed)" in body
        assert stats["author_mismatch"] == 1
        full = report.build(conn, days=10, db_path=":memory:")
        assert "автор не совпадает с лентой ссылки у 1 из 1" in full
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Окно YouTube по публикации (ТЗ §4)
# --------------------------------------------------------------------------- #
def test_youtube_published_window(tmp_path):
    yt = [
        (1, {"handle": "yt1", "title": "Свежее видео", "views": 100_000,
             "vpd": 100, "age": 1.0}),
        (2, {"handle": "yt2", "title": "Старый мега-ролик", "views": 9_000_000,
             "vpd": 9_000_000, "age": 30.0}),
    ]
    conn = build_core(tmp_path, yt=yt)
    try:
        lines, stats = report.youtube_section(conn, _iso(10))
        body = "\n".join(lines)
        assert "Свежее видео" in body
        assert "Старый мега-ролик" not in body
        assert stats["considered"] == 1
        assert stats["above"] == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Кэп на автора (ТЗ §3)
# --------------------------------------------------------------------------- #
def test_x_cap_per_author_with_topup(tmp_path):
    x = [{"author": "prolific", "source": "prolific", "text": f"пост {i}",
          "likes": 1000 - i, "age": 0.5, "text_hash": f"c{i}"} for i in range(5)]
    x.append({"author": "other", "source": "other", "text": "другой",
              "likes": 10, "age": 0.5, "text_hash": "co"})
    conn = build_core(tmp_path, x=x)
    try:
        items, _ = report.x_items(conn, _iso(10))
        prolific = [it for it in items if "prolific" in it.author_key]
        assert len(prolific) <= report.MAX_PER_AUTHOR == 2
        assert any(it.author_key == "other" for it in items)
    finally:
        conn.close()


def test_youtube_cap_per_channel_with_topup(tmp_path):
    yt = [(1, {"handle": "same", "title": f"видео {i}", "views": 100_000,
               "vpd": 1000 - i, "age": 1.0}) for i in range(5)]
    yt.append((2, {"handle": "other", "title": "другое видео", "views": 90_000,
                   "vpd": 1, "age": 1.0}))
    conn = build_core(tmp_path, yt=yt)
    try:
        items, _ = report.youtube_items(conn, _iso(10))
        same = [it for it in items if it.author_key == "1"]
        assert len(same) <= report.MAX_PER_AUTHOR == 2
        assert any(it.author_key == "2" for it in items)
    finally:
        conn.close()


def test_max_per_author_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TUBER_MAX_PER_AUTHOR", "1")
    assert report.max_per_author() == 1
    monkeypatch.setenv("TUBER_MAX_PER_AUTHOR", "опечатка")
    assert report.max_per_author() == report.MAX_PER_AUTHOR


# --------------------------------------------------------------------------- #
# Честные счётчики сводки (ТЗ §6)
# --------------------------------------------------------------------------- #
def test_compact_honest_counters(tmp_path):
    yt = [(1, {"handle": "yt1", "title": f"видео {i}", "views": 100_000,
               "vpd": 1000 - i, "age": 2.0}) for i in range(5)]
    conn = build_core(tmp_path, yt=yt)
    try:
        text = report.build_compact(conn, days=10, db_path=":memory:")
        assert "рассмотрено" in text and "выше порога" in text
        assert "показано" in text and "скрыто" in text
        # Знаменатель честный: «выше порога» — 5, «скрыто» — не 0.
        yt_line = next(l for l in text.splitlines() if l.strip().startswith("YouTube: "))
        assert "выше порога 5" in yt_line
        assert "скрыто 3" in yt_line  # 5 выше порога − 2 показано (кэп)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Классификатор X: поля title_ru/summary_ru (ТЗ §1.3)
# --------------------------------------------------------------------------- #
def test_x_classifier_prompt_and_validation():
    from tuber.platforms.x import classify
    prompt = classify._system_prompt()
    assert "title_ru" in prompt and "summary_ru" in prompt
    raw = ('{"items":[{"idx":1,"is_ai":1,"topic":"релизы моделей",'
           '"subtopic":"новая модель","claim_type":"release","novelty":0.9,'
           '"lang":"en","title_ru":"Релиз модели","summary_ru":"Вышла модель."}]}')
    items = [{"text": "New model released", "lang": "en"}]
    parsed, err = classify.validate_response(raw, items)
    assert err is None
    assert parsed[1]["title_ru"] == "Релиз модели"
    assert parsed[1]["summary_ru"] == "Вышла модель."


def test_x_classifier_fields_optional():
    from tuber.platforms.x import classify
    raw = ('{"items":[{"idx":1,"is_ai":1,"topic":"релизы моделей",'
           '"subtopic":"x","claim_type":"release","novelty":0.5,"lang":"en"}]}')
    parsed, err = classify.validate_response(raw, [{"text": "t", "lang": "en"}])
    assert err is None
    assert parsed[1]["title_ru"] is None and parsed[1]["summary_ru"] is None
