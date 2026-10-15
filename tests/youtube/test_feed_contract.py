"""ТЗ-21/C: договор формата фида `docs/EXCHANGE-FEED.md` (канон — в tuber-os).

Тест генерирует РЕАЛЬНЫЙ экспорт tuber-os (`candidates.export_external`) во
временном каталоге и проверяет типы полей по таблице канона. Падение — с точным
именем поля и полученным типом: синтетика ТЗ-18 этот класс дефектов не ловила,
потому что тестовые строки были написаны по образцу одного продюсера.

Плюс терпимость потребителя (`_parse_feed`): чужой тип поля не роняет импорт.

Сеть не используется, рабочая база не трогается.
"""
from __future__ import annotations

import json

import pytest

from tuber.platforms.youtube import candidates, store as db

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


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "contract.db")
    db.init_db(c)
    yield c
    c.close()


def _video(conn, vid, description, published=1_700_000_000):
    conn.execute(
        "INSERT INTO videos (video_id, title, description, tags, published_at,"
        " first_seen) VALUES (?, ?, ?, NULL, ?, 1)",
        (vid, "AI news", description, published))
    conn.commit()


def _real_export(conn, tmp_path):
    _video(conn, "v1", "Канал: t.me/AlphaChannel и x.com/SomeUser")
    _video(conn, "v2", "Снова x.com/SomeUser, ещё t.me/BetaChannel", published=1_700_100_000)
    out = tmp_path / "exchange" / "external_candidates.jsonl"
    candidates.export_external(conn, out=out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    return rows


def test_real_export_matches_feed_contract(conn, tmp_path):
    rows = _real_export(conn, tmp_path)
    _assert_canon(rows)
    by = {(r["kind"], r["handle"]): r for r in rows}
    alpha = by[("telegram", "alphachannel")]
    assert alpha["videos"] == 1
    assert alpha["video_ids"] == ["v1"]
    some = by[("x", "someuser")]
    assert some["videos"] == 2
    assert some["video_ids"] == ["v1", "v2"]
    assert all(isinstance(e, str) for r in rows for e in r["examples"])


def test_canon_doc_is_the_source(conn, tmp_path):
    """Договор в tuber-os содержит таблицу канона (videos — число, video_ids)."""
    from pathlib import Path
    root = Path(candidates.__file__).resolve().parents[3]
    text = (root / "docs" / "EXCHANGE-FEED.md").read_text(encoding="utf-8")
    assert "video_ids" in text
    assert "**число**" in text
    assert "потребитель обязан" in text.lower()


# --------------------------------------------------- терпимость потребителя
def _feed(path, entries):
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                    encoding="utf-8")
    return path


def test_parse_feed_tolerates_foreign_field_types(tmp_path):
    """ТЗ-21/B: чужой тип поля не роняет импорт, а считается в bad_fields."""
    feed = _feed(tmp_path / "feed.jsonl", [
        # id списком, а не строкой — берётся первое годное значение
        {"kind": "youtube", "video_id": ["abcdefghijk"]},
        # videos числом/списком и examples объектами не мешают вытащить id
        {"kind": "youtube", "handle": "lmnopqrstuv", "videos": 5,
         "examples": [{"text": "цитата"}], "sources": "os:desc"},
        # id в объекте
        {"kind": "youtube", "url": {"url": "https://youtu.be/zyxwvutsrqp"}},
    ])
    parsed = candidates._parse_feed(feed)
    assert parsed["youtube_ids"] == ["abcdefghijk", "lmnopqrstuv", "zyxwvutsrqp"]
    assert parsed["bad_lines"] == 0
    assert parsed["bad_fields"] == 0


def test_parse_feed_counts_bad_field_type(tmp_path):
    """Явно негодный тип поля считается в bad_fields, но не роняет импорт."""
    feed = _feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "handle": "abcdefghijk", "mentions": {"n": 1},
         "videos": {"x": 1}, "examples": 42},
    ])
    parsed = candidates._parse_feed(feed)
    assert parsed["bad_fields"] == 3
    assert parsed["youtube_ids"] == ["abcdefghijk"]


# --------------------------------- регрессия ТЗ-21/C-2: настоящие файлы
REAL_FEEDS = {
    "tuber-telegram": "/root/tuber-telegram/data/exchange/external_candidates.jsonl",
    "tuber-os": "/root/tuber-os/data/exchange/external_candidates.jsonl",
}


@pytest.mark.parametrize("name", sorted(REAL_FEEDS))
def test_live_feed_parse_does_not_raise(name, tmp_path):
    """Живой фид обязан разбираться без исключения (синтетика дефект не ловила)."""
    import os
    path = REAL_FEEDS[name]
    if not os.path.isfile(path):
        pytest.skip(f"живой фид отсутствует: {path}")
    parsed = candidates._parse_feed(path)              # не должно бросить
    assert parsed["total_lines"] > 0
    assert parsed["bad_lines"] == 0


class _NoopClient:
    """Мок videos.list: ни одного элемента, ни одного сетевого вызова наружу."""

    def videos_by_ids(self, ids, parts="snippet"):
        return []


@pytest.mark.parametrize("name", sorted(REAL_FEEDS))
def test_live_feed_import_does_not_raise(name, conn, tmp_path):
    """Реальный импорт YouTube из живого фида не падает (каналы не найдены — ок)."""
    import os
    path = REAL_FEEDS[name]
    if not os.path.isfile(path):
        pytest.skip(f"живой фид отсутствует: {path}")
    summary = candidates.import_youtube_feed(conn, path, _NoopClient(), limit=50)
    assert summary["bad_lines"] == 0
    assert summary["errors"] == []
