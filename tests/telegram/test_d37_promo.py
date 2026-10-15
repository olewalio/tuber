"""Задача A ТЗ-19 (D-37): вердикт модели доходит до ``content.is_promo``.

Сеть — только мок (``FakeSession``); боевая база не трогается: всё на временных
базах фикстуры ``con`` (см. ``tests/telegram/conftest.py``).

Покрывается: чтение вердикта из кэша (1 / 0 / нет записи), fallback на ключевое
правило, запись ``content.is_promo`` при разметке (в обе стороны) и идемпотентный
backfill (повторный прогон = 0 изменений).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tuber.platforms.telegram import ads
from tuber.platforms.telegram.classify import text_hash_of

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def con(tmp_path):
    """Временная единая база со слоем совместимости (без сети).

    Фикстура объявлена ЛОКАЛЬНО, а не взята из ``tests/telegram/conftest.py``:
    так файл самодостаточен и не зависит от порядка сбора pytest (см. приёмку
    ТЗ-19: тест-файлы задачи A добавляются в конец команды).
    """
    from tuber.platforms.telegram import store as db

    path = str(tmp_path / "d37_promo.db")
    db.init_db(path).close()
    conn = db.connect(path)
    yield conn
    conn.close()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, contents, usage=None):
        self.contents = list(contents)
        self.usage = usage or {"prompt_tokens": 100, "completion_tokens": 50}
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        content = self.contents.pop(0) if self.contents else "[]"
        return _Resp({"choices": [{"message": {"content": content}}],
                      "usage": dict(self.usage)})


def add_source(con, handle, source_id):
    con.execute(
        "INSERT INTO source(id, platform, handle, title, status) VALUES(?,?,?,?,?)",
        (source_id, "telegram", handle, handle, "active"))


def add_post(con, external_id, source_id, text, *, is_promo=0):
    cur = con.execute(
        "INSERT INTO content(platform, external_id, source_id, text, text_hash,"
        " is_promo, published_at, url)"
        " VALUES('telegram', ?, ?, ?, ?, ?, ?, ?)",
        (external_id, source_id, text, text_hash_of(text), is_promo,
         NOW.strftime("%Y-%m-%d %H:%M:%S"), f"https://t.me/{external_id}"))
    con.commit()
    return cur.lastrowid


def add_cache(con, text, is_ad, *, status="classified"):
    con.execute(
        "INSERT OR REPLACE INTO ad_cache(text_hash, is_ad, status) VALUES(?,?,?)",
        (text_hash_of(text), is_ad, status))
    con.commit()


def _promo_by_external(con):
    return {r["external_id"]: r["is_promo"]
            for r in con.execute(
                "SELECT external_id, is_promo FROM content WHERE platform='telegram'")}


# --- чтение вердикта из кэша -----------------------------------------------


def test_verdict_reads_cache_one_zero_and_missing(con):
    add_cache(con, "текст реклама", 1)
    add_cache(con, "текст не реклама", 0)
    assert ads.verdict_for_hash(con, text_hash_of("текст реклама")) == 1
    assert ads.verdict_for_hash(con, text_hash_of("текст не реклама")) == 0
    assert ads.verdict_for_hash(con, text_hash_of("нет такой записи")) is None
    assert ads.verdict_for_hash(con, None) is None
    # Строка ошибки (is_ad IS NULL) вердиктом не считается — не выдумываем.
    add_cache(con, "сбой", None, status="error")
    assert ads.verdict_for_hash(con, text_hash_of("сбой")) is None


# --- fallback на ключевое правило ------------------------------------------


def test_resolve_is_ad_cache_wins_else_keyword(con):
    # Кэша нет -> работает прежнее ключевое правило (erid/реклам/промокод).
    assert ads.resolve_is_ad(con, "обычный новостной пост") == 0
    assert ads.resolve_is_ad(con, "Промокод на скидку") == 1
    # Вердикт модели авторитетнее ключевого правила — в обе стороны.
    add_cache(con, "Промокод на скидку", 0)
    assert ads.resolve_is_ad(con, "Промокод на скидку") == 0
    add_cache(con, "обычный новостной пост", 1)
    assert ads.resolve_is_ad(con, "обычный новостной пост") == 1
    # error-строка не перебивает ключевое правило.
    add_cache(con, "ещё промокод", None, status="error")
    assert ads.resolve_is_ad(con, "ещё промокод") == 1


# --- A1: разметка пишет content.is_promo в обе стороны ----------------------


def test_run_writes_is_promo_both_directions(con):
    add_source(con, "ch", 1)
    cid1 = add_post(con, "ch/1", 1, "обычный пост про модель", is_promo=0)
    cid2 = add_post(con, "ch/2", 1, "Промокод на скидку", is_promo=0)
    payload = json.dumps([
        {"content_id": cid1, "is_ad": True, "confidence": 0.9, "reason": "r"},
        {"content_id": cid2, "is_ad": False, "confidence": 0.9, "reason": "r"},
    ], ensure_ascii=False)
    summary = ads.run(con, only="ch", session=FakeSession([payload]),
                      api_key="k", now=NOW)
    assert summary["classified"] == 2
    assert _promo_by_external(con) == {"ch/1": 1, "ch/2": 0}


# --- A2: backfill в обе стороны + идемпотентность ---------------------------


def test_backfill_both_directions_and_idempotent(con):
    add_source(con, "ch", 1)
    add_post(con, "ch/1", 1, "текст раз", is_promo=0)   # вердикт 1 -> 0->1
    add_post(con, "ch/2", 1, "текст два", is_promo=1)   # вердикт 0 -> 1->0
    add_post(con, "ch/3", 1, "текст три", is_promo=0)   # вердикта нет
    add_cache(con, "текст раз", 1)
    add_cache(con, "текст два", 0)

    first = ads.backfill_content_promo(con)
    assert first == {"to_promo": 1, "to_normal": 1, "matched": 2}
    assert _promo_by_external(con) == {"ch/1": 1, "ch/2": 0, "ch/3": 0}

    # Повторный прогон — ноль изменений (идемпотентность).
    second = ads.backfill_content_promo(con)
    assert second == {"to_promo": 0, "to_normal": 0, "matched": 2}
