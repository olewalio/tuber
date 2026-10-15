"""ТЗ-21/C: договор формата фида `docs/EXCHANGE-FEED.md` (канон — в tuber-os).

Перенос `tests/test_feed_contract.py` проекта tuber-telegram (ТЗ-4). Тест
генерирует РЕАЛЬНЫЙ экспорт Telegram (`feeds_export.main`) во временном каталоге
и проверяет типы полей по таблице канона. Падение — с точным именем поля и
полученным типом: именно этот класс дефектов (videos списком вместо числа,
examples объектами вместо строк) ломал мосты 15.09.2026.

Сеть не используется, рабочая база не трогается.
"""
from __future__ import annotations

import json
import os

from tuber.platforms.telegram import feeds_export, store as db

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Таблица канона: поле → ожидаемый тип (обязательные пишет продюсер).
CANON = {
    "kind": str,
    "handle": str,
    "mentions": int,
    "videos": int,
    "video_ids": list,
    "sources": list,
    "source": str,
    "examples": list,
    "ai_hint": int,
    "first_seen": str,
    "last_seen": str,
}


def _type_ok(value, typ):
    if typ is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, typ)


def _assert_canon(rows):
    """Проверить типы полей каждой строки; упасть с именем поля и типом."""
    assert rows, "экспорт пуст — проверять нечего"
    for i, row in enumerate(rows):
        for field, typ in CANON.items():
            if field not in row or row[field] is None:
                continue
            value = row[field]
            assert _type_ok(value, typ), (
                f"строка {i}: поле `{field}` — ожидался {typ.__name__}, "
                f"получен {type(value).__name__} ({value!r})"
            )
        for field in ("video_ids", "sources", "examples"):
            if not row.get(field):
                continue
            for item in row[field]:
                assert isinstance(item, str), (
                    f"строка {i}: поле `{field}` — элемент ожидался str, "
                    f"получен {type(item).__name__} ({item!r})"
                )


def _build_db(path):
    con = db.init_db(path)
    con.execute("INSERT INTO channels(handle,status,source) VALUES('src_chan','active','test')")
    ch = con.execute("SELECT id FROM channels WHERE handle='src_chan'").fetchone()[0]
    posts = [
        (ch, 100, "2026-09-10T10:00:00+00:00",
         "смотри https://x.com/rohanpaul_ai/status/123 и видео "
         "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
         '["https://x.com/rohanpaul_ai/status/123"]', 1),
        (ch, 101, "2026-09-15T10:00:00+00:00",
         "ещё раз https://twitter.com/rohanpaul_ai и https://youtu.be/dQw4w9WgXcQ",
         "[]", 1),
    ]
    for channel_id, mid, date, text, links, is_ai in posts:
        con.execute("INSERT INTO posts(channel_id,message_id,date_utc,text,links)"
                    " VALUES(?,?,?,?,?)", (channel_id, mid, date, text, links))
        pid = con.execute("SELECT id FROM posts WHERE message_id=?", (mid,)).fetchone()[0]
        con.execute("INSERT INTO classified(post_id,is_ai,topic) VALUES(?,?,?)",
                    (pid, is_ai, "ai"))
    con.commit()
    con.close()
    return path


def _real_export(tmp_path):
    database = _build_db(str(tmp_path / "src.db"))
    out = str(tmp_path / "exchange" / "external_candidates.jsonl")
    rc = feeds_export.main(["--db", database, "--out", out])
    assert rc == 0, f"экспорт упал: код {rc}"
    with open(out, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_real_export_matches_feed_contract(tmp_path):
    rows = _real_export(tmp_path)
    _assert_canon(rows)
    kinds = {r["kind"] for r in rows}
    assert kinds == {"x", "youtube"}
    youtube = [r for r in rows if r["kind"] == "youtube"][0]
    assert youtube["video_ids"] == [youtube["handle"]]
    xrow = [r for r in rows if r["kind"] == "x"][0]
    assert xrow["video_ids"] == []
    assert isinstance(xrow["videos"], int) and xrow["videos"] == 2


def test_examples_are_strings(tmp_path):
    rows = _real_export(tmp_path)
    for row in rows:
        assert all(isinstance(e, str) for e in row["examples"]), row["examples"]


def test_canon_doc_copy_present():
    """В проекте есть копия договора с шапкой «канон — в tuber-os»."""
    path = os.path.join(ROOT, "docs", "EXCHANGE-FEED.md")
    assert os.path.isfile(path), "нет docs/EXCHANGE-FEED.md"
    text = open(path, encoding="utf-8").read()
    assert "канон — в" in text.lower()
    assert "tuber-os" in text
    assert "video_ids" in text and "**число**" in text
