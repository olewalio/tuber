"""ТЗ-21/C: договор формата фида `docs/EXCHANGE-FEED.md` (канон — в tuber-os).

tuber-x — потребитель моста, но договор обязателен для всех трёх проектов,
поэтому здесь проверяется СВОЙ реальный экспорт (`feeds.export_queue`),
сгенерированный в tmp. Падение — с точным именем поля и полученным типом.

Отдельно:
  * терпимость потребителя к обоим видам полей (ТЗ-21/B) — `videos` числом и
    списком, `sources` строкой, `examples` объектами;
  * регрессия ТЗ-21/C-2 на НАСТОЯЩИХ файлах с диска: живой фид обязан
    импортироваться без исключения (синтетика этот дефект не ловила).

Сеть не используется, рабочая база не трогается.
"""
from __future__ import annotations

import json
import os

import pytest

from tuber.platforms.x import store as db, feeds

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

REAL_FEEDS = {
    "tuber-telegram": "/root/tuber-telegram/data/exchange/external_candidates.jsonl",
    "tuber-os": "/root/tuber-os/data/exchange/external_candidates.jsonl",
}


def _type_ok(value, typ):
    if typ is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, typ)


def _assert_canon(rows):
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
            for item in row.get(field) or []:
                assert isinstance(item, str), (
                    f"строка {i}: поле `{field}` — элемент ожидался str, "
                    f"получен {type(item).__name__} ({item!r})"
                )


def _x(handle, mentions=5, *, sources=("ch1",), videos=1, video_ids=(),
       examples=()):
    return {"kind": "x", "handle": handle, "mentions": mentions,
            "videos": videos, "video_ids": list(video_ids),
            "sources": list(sources), "source": "tuber-os:video-descriptions",
            "examples": list(examples), "ai_hint": 1,
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-09-01T00:00:00Z"}


def _feed(path, rows):
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(path)


# --------------------------------------------------- свой реальный экспорт
def test_real_export_matches_feed_contract(con, tmp_path):
    p = _feed(tmp_path / "src.jsonl", [_x("alpha_ai", 7, videos=3,
                                          video_ids=("v1", "v2", "v3"))])
    feeds.import_candidates(con, [p])
    out = str(tmp_path / "exchange" / "tuber_x_candidates.jsonl")
    summary = feeds.export_queue(con, out)
    rows = [json.loads(line) for line in open(out, encoding="utf-8") if line.strip()]
    _assert_canon(rows)
    assert summary["written"] == len(rows) == 1
    row = rows[0]
    assert row["kind"] == "x" and row["handle"] == "alpha_ai"
    assert row["mentions"] == 7
    assert row["videos"] == 3 and row["video_ids"] == ["v1", "v2", "v3"]


def test_canon_doc_copy_present():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "docs", "x", "docs", "EXCHANGE-FEED.md")
    assert os.path.isfile(path), "нет docs/EXCHANGE-FEED.md"
    text = open(path, encoding="utf-8").read()
    assert "канон — в" in text.lower() and "tuber-os" in text
    assert "video_ids" in text and "**число**" in text


# ------------------------------------------- терпимость к обеим версиям поля
def test_videos_as_number_and_as_list(con, tmp_path):
    rows = [_x("num_ai", 4, videos=5), _x("list_ai", 4, videos=0,
                                          video_ids=("v1", "v2"))]
    # старый вид: videos списком
    rows[1]["videos"] = ["v1", "v2"]
    rows[1]["video_ids"] = []
    p = _feed(tmp_path / "f.jsonl", rows)
    r = feeds.import_candidates(con, [p])
    assert r["imported_new"] == 2
    assert r["bad_fields"] == 0


def test_sources_string_and_missing_take_source(con, tmp_path):
    rows = [_x("str_ai", 4), _x("miss_ai", 4)]
    rows[0]["sources"] = "chan_one, chan_two"      # строка вместо списка
    del rows[1]["sources"]                         # отсутствует -> берём source
    rows[1]["videos"] = 0
    p = _feed(tmp_path / "f.jsonl", rows)
    r = feeds.import_candidates(con, [p])
    assert r["imported_new"] == 2 and r["bad_fields"] == 0
    # Строка `sources` разобрана в два источника, а у строки без `sources`
    # взят `source` — это видно в ключах источников кандидата.
    srcs = json.loads(con.execute("SELECT sources FROM candidates WHERE handle='miss_ai'"
                                  ).fetchone()[0])
    assert any(":src:tuber-os:video-descriptions" in s for s in srcs), srcs
    srcs2 = json.loads(con.execute("SELECT sources FROM candidates WHERE handle='str_ai'"
                                   ).fetchone()[0])
    names = {s.split(":src:", 1)[1] for s in srcs2 if ":src:" in s}
    assert names == {"chan_one", "chan_two", "tuber-os:video-descriptions"}, srcs2


def test_examples_objects_strings_and_single_string(con, tmp_path):
    rows = [_x("obj_ai", 4), _x("str_ai2", 4), _x("one_ai", 4)]
    rows[0]["examples"] = [{"message_id": 1, "text": "цитата"}]
    rows[1]["examples"] = ["цитата"]
    rows[2]["examples"] = "одна строка"
    p = _feed(tmp_path / "f.jsonl", rows)
    r = feeds.import_candidates(con, [p])
    assert r["imported_new"] == 3
    assert r["bad_fields"] == 0


def test_bad_field_type_counts_but_does_not_fail(con, tmp_path):
    row = _x("bad_ai", 4)
    row["videos"] = {"n": 3}          # негодный тип
    row["sources"] = 42               # негодный тип
    row["examples"] = [{"no_text": 1}]
    p = _feed(tmp_path / "f.jsonl", [row])
    r = feeds.import_candidates(con, [p])
    assert r["imported_new"] == 1
    assert r["bad_fields"] == 3
    assert r["feeds"][0]["bad_fields"] == 3


# --------------------------------- регрессия ТЗ-21/C-2: настоящие файлы
@pytest.mark.parametrize("name", sorted(REAL_FEEDS))
def test_live_feed_import_does_not_raise(name, tmp_path):
    path = REAL_FEEDS[name]
    if not os.path.isfile(path):
        pytest.skip(f"живой фид отсутствует: {path}")
    con = db.init_db(str(tmp_path / f"{name}.db"))
    try:
        report = feeds.import_candidates(con, [path])   # не должно бросить
    finally:
        con.close()
    assert report["feeds_missing"] == []
    assert report["feeds"][0]["rows"] > 0
    assert report["feeds"][0]["bad_lines"] == 0


def test_both_live_feeds_together(tmp_path):
    """Оба живых фида вместе: смешение видов `videos` не роняет импорт."""
    paths = [p for p in REAL_FEEDS.values() if os.path.isfile(p)]
    if len(paths) < 2:
        pytest.skip("на диске нет обоих живых фидов")
    con = db.init_db(str(tmp_path / "both.db"))
    try:
        report = feeds.import_candidates(con, paths)
    finally:
        con.close()
    assert report["imported_new"] > 0
    kinds = {f["feed_tag"]: f for f in report["feeds"]}
    assert kinds["tuber-telegram"]["bad_fields"] == 0
    assert kinds["tuber-os"]["bad_fields"] == 0
