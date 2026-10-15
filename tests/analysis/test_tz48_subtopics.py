"""ТЗ-48: подтемы сюжетов (тренд внутри тренда) — чистые функции и окно.

Проверяем формулу плана на маленькой детерминированной базе:

* ``share_t`` нормируется на число постов окна, ``accel_sub`` — на предыдущее
  окно той же длины;
* в тренды попадают только сущности с ``accel_sub ≥ 0,5`` и ``≥ 5``
  независимыми авторами;
* ``share_{t-1} = 0`` даёт блок «новые», а не деление на ноль;
* якорь окна сдвигается к последнему посту, если свежих постов нет;
* сущность обязана быть в сюжете (``story.entities``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.analysis import names, subtopics
from tuber.core import db, schema

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _make(tmp_path, *, stories=(("gpt", "claude"),)):
    con = db.connect(str(tmp_path / "trends.db"))
    schema.init_schema(con)
    for i, (sid, platform) in enumerate([(1, "telegram"), (2, "x"), (3, "telegram"),
                                         (4, "x"), (5, "telegram"), (6, "x")], start=1):
        con.execute("INSERT INTO source(id, platform, handle, status, subs)"
                    " VALUES (?,?,?,?,?)", (sid, platform, f"h{sid}", "active", 1000))
    for i, (story_id, ents) in enumerate([(1, stories[0])], start=1):
        import json
        con.execute("INSERT INTO story(id, platform, title, entities) VALUES (?,?,?,?)",
                    (story_id, "x", "story title", json.dumps(list(ents))))
    return con


def _post(con, cid, sid, platform, published, text):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at, text)"
        " VALUES (?,?,?,?,?,?)",
        (cid, platform, sid, f"e{cid}", published.strftime("%Y-%m-%d %H:%M:%S"), text))


def _fill(con, gpt_now=6, filler_now=18, gpt_prev=1, filler_prev=9):
    """Посты: сейчас 24 (6 c gpt), предыдущее окно 10 (1 c gpt)."""
    cid = 100
    for i in range(gpt_now):
        _post(con, cid, (i % 6) + 1, "telegram" if i % 2 == 0 else "x",
              NOW - timedelta(hours=1, minutes=i), "GPT release")
        cid += 1
    for i in range(filler_now):
        _post(con, cid, ((i + 2) % 6) + 1, "telegram", NOW - timedelta(hours=2, minutes=i),
              "filler news")
        cid += 1
    for i in range(gpt_prev):
        _post(con, cid, 1, "telegram", NOW - timedelta(hours=8, minutes=i), "GPT old")
        cid += 1
    for i in range(filler_prev):
        _post(con, cid, 2, "x", NOW - timedelta(hours=9, minutes=i), "filler old")
        cid += 1
    con.commit()


def test_normalize_entity():
    assert names.normalize_entity("@Meta") == "meta"
    assert names.normalize_entity("sakana.ai") == "sakana"
    assert names.normalize_entity("ZCode") == "zcode"
    assert names.normalize_entity("…") is None
    assert names.normalize_entity("www") is None
    assert names.normalize_entity("%d1%81") is None


def test_accel_and_threshold(tmp_path):
    con = _make(tmp_path)
    try:
        _fill(con)
        data = subtopics.build(con, now=NOW, window_hours=6)
        assert data["posts_now"] == 24
        assert data["posts_prev"] == 10
        gpt = next(r for r in data["trending"] if r["entity"] == "gpt")
        # share_t = 6/24, share_prev = 1/10 -> accel = 1.5
        assert abs(gpt["accel_sub"] - 1.5) < 1e-9
        assert gpt["authors"] >= 5
        assert gpt["share_t"] == 6 / 24
    finally:
        con.close()


def test_insufficient_authors_excluded(tmp_path):
    con = _make(tmp_path)
    try:
        # 5 постов с gpt у ТРЁХ независимых источников -> порог авторов не взят
        cid = 100
        for i in range(24):
            sid = 1 if i < 5 else 2
            text = "GPT release" if i < 5 else "filler news"
            _post(con, cid, sid, "telegram", NOW - timedelta(hours=1, minutes=i), text)
            cid += 1
        for i in range(10):
            _post(con, cid, 2, "x", NOW - timedelta(hours=8, minutes=i), "GPT old" if i == 0 else "x")
            cid += 1
        con.commit()
        data = subtopics.build(con, now=NOW, window_hours=6, min_authors=5)
        assert all(r["entity"] != "gpt" for r in data["trending"])
    finally:
        con.close()


def test_new_entity_goes_to_new_block(tmp_path):
    con = _make(tmp_path, stories=(("claude",),))
    try:
        cid = 100
        for i in range(5):
            _post(con, cid, i + 1, "telegram", NOW - timedelta(hours=1, minutes=i), "claude tool")
            cid += 1
        for i in range(19):
            _post(con, cid, 2, "x", NOW - timedelta(hours=2, minutes=i), "filler news")
            cid += 1
        for i in range(10):
            _post(con, cid, 3, "telegram", NOW - timedelta(hours=8, minutes=i), "old news")
            cid += 1
        con.commit()
        data = subtopics.build(con, now=NOW, window_hours=6)
        assert all(r["entity"] != "claude" for r in data["trending"])
        assert any(r["entity"] == "claude" for r in data["new_entities"])
    finally:
        con.close()


def test_entity_outside_stories_ignored(tmp_path):
    con = _make(tmp_path, stories=(("other",),))
    try:
        _fill(con)
        data = subtopics.build(con, now=NOW, window_hours=6)
        assert all(r["entity"] != "gpt" for r in data["trending"])
    finally:
        con.close()


def test_anchor_shift_when_window_empty(tmp_path):
    con = _make(tmp_path)
    try:
        _post(con, 1, 1, "telegram", NOW - timedelta(days=3), "GPT old")
        con.commit()
        data = subtopics.build(con, now=NOW, window_hours=6)
        assert data["anchor_shifted"] is True
        assert data["anchor"] == (NOW - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    finally:
        con.close()


def test_format_report_has_numbers_and_link(tmp_path):
    con = _make(tmp_path)
    try:
        _fill(con)
        data = subtopics.build(con, now=NOW, window_hours=6, limit=5)
        text = subtopics.format_report(data, limit=5)
        assert "accel_sub" in text
        assert "gpt" in text
        assert "авторов" in text
    finally:
        con.close()
