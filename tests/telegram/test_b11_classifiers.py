"""Тесты волны B11 (D-36/D-37/D-52): кэш, маркировка темой, детектор рекламы,
пост-уровневый фильтр отчёта.

Сеть — только мок (``FakeSession``). Боевая база не трогается: тесты работают
на временных базах из фикстуры ``tg_path``/``con`` (см. ``conftest.py``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tuber.analysis import report
from tuber.core import db, schema
from tuber.platforms.telegram import ads, classify, config

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


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


def _arr(*items):
    return json.dumps(list(items), ensure_ascii=False)


def _topic_item(cid, topic="модели и релизы"):
    return {"content_id": cid, "is_ai": True, "topic": topic, "subtopic": "s",
            "confidence": 0.9, "title_ru": "t", "summary_ru": "s", "lang": "ru",
            "reason": "r"}


def _ad_item(cid, is_ad):
    return {"content_id": cid, "is_ad": is_ad, "confidence": 0.9, "reason": "r"}


def add_source(con, handle, source_id, status="active"):
    con.execute(
        "INSERT INTO source(id, platform, handle, title, status) VALUES(?,?,?,?,?)",
        (source_id, "telegram", handle, handle, status))


def add_post(con, external_id, source_id, text, published=None):
    published = published or NOW.strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "INSERT INTO content(platform, external_id, source_id, text,"
        " published_at, url) VALUES('telegram', ?, ?, ?, ?, ?)",
        (external_id, source_id, text, published, f"https://t.me/{external_id}"))
    con.commit()


# --- D-36/D-52: тематический классификатор ---------------------------------


def test_topic_classifier_fills_classification(con):
    add_source(con, "vibecoding_tg", 1)
    add_post(con, "vibecoding_tg/1", 1, "Claude Code добавил AGENTS.md")
    session = FakeSession([_arr(_topic_item(1))])
    summary = classify.classify(con, api_key="k", session=session, now=NOW)
    assert summary["classified"] == 1 and summary["ai"] == 1
    row = con.execute(
        "SELECT is_ai, topic, status FROM classification WHERE platform='telegram'"
    ).fetchone()
    assert (row["is_ai"], row["topic"], row["status"]) == (1, "модели и релизы",
                                                           "classified")


def test_topic_classifier_cache_second_run_zero_calls(con):
    """Требование ТЗ: повторный прогон на том же входе не делает вызовов."""
    add_source(con, "vibecoding_tg", 1)
    add_post(con, "vibecoding_tg/1", 1, "Claude Code добавил AGENTS.md")
    first = FakeSession([_arr(_topic_item(1))])
    classify.classify(con, api_key="k", session=first, now=NOW)
    assert len(first.posts) == 1

    second = FakeSession([_arr(_topic_item(1))])
    summary = classify.classify(con, api_key="k", session=second, now=NOW)
    assert summary["model_calls"] == 0
    assert len(second.posts) == 0


def test_topic_weight_map_and_neutral_default():
    assert config.topic_weight_of("модели и релизы") == 1.40
    assert config.topic_weight_of("прочее") == 0.60
    assert config.topic_weight_of(None) == 1.0
    assert config.topic_weight_of("неизвестная тема") == 1.0


# --- D-37: семантический детектор рекламы ----------------------------------


def test_ad_detector_keyword_misses_semantic_catches(con):
    """Ключевое правило не видит нативную рекламу — семантика видит."""
    add_source(con, "vibecoding_tg", 1)
    add_post(con, "vibecoding_tg/1", 1, "Заходите на вебинар партнёра, лучший курс")
    session = FakeSession([_arr(_ad_item(1, True))])
    summary = ads.run(con, only="vibecoding_tg", session=session, api_key="k", now=NOW)
    assert summary["keyword_ad"] == 0   # ключи (erid/реклам/промокод) не сработали
    assert summary["ad"] == 1           # семантика нашла
    row = con.execute("SELECT is_ad, status FROM ad_cache").fetchone()
    assert row["is_ad"] == 1 and row["status"] == "classified"


def test_ad_detector_cache_second_run_zero_calls(con):
    add_source(con, "vibecoding_tg", 1)
    add_post(con, "vibecoding_tg/1", 1, "Обычный новостной пост про модель")
    first = FakeSession([_arr(_ad_item(1, False))])
    ads.run(con, only="vibecoding_tg", session=first, api_key="k", now=NOW)
    assert len(first.posts) == 1

    second = FakeSession([_arr(_ad_item(1, False))])
    summary = ads.run(con, only="vibecoding_tg", session=second, api_key="k", now=NOW)
    assert summary["model_calls"] == 0
    assert len(second.posts) == 0


def test_ad_detector_invalid_json_writes_error_not_guess(con):
    add_source(con, "vibecoding_tg", 1)
    add_post(con, "vibecoding_tg/1", 1, "Текст поста")
    session = FakeSession(["не json", "тоже не json"])
    summary = ads.run(con, only="vibecoding_tg", session=session, api_key="k", now=NOW)
    assert summary["failed"] == 1
    row = con.execute("SELECT is_ad, status FROM ad_cache").fetchone()
    assert row["is_ad"] is None and row["status"] == "error"


def test_ad_detector_dry_run_counts_keyword_without_calls(con):
    add_source(con, "vibecoding_tg", 1)
    add_post(con, "vibecoding_tg/1", 1, "Промокод на скидку")
    summary = ads.run(con, only="vibecoding_tg", dry_run=True, now=NOW)
    assert summary["keyword_ad"] == 1
    assert summary["model_calls"] == 0


# --- D-52: пост-уровневый фильтр отчёта ------------------------------------


def _report_db(tmp_path):
    conn = db.connect(str(tmp_path / "b11.db"))
    schema.init_schema(conn)
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO source(id,platform,handle,title,status)"
            " VALUES(1,'telegram','ai_chan','AI chan','active')")
        conn.execute(
            "INSERT INTO source(id,platform,handle,title,status)"
            " VALUES(2,'telegram','meme_chan','Meme chan','active')")
        pub = NOW.strftime("%Y-%m-%d %H:%M:%S")
        for cid, sid, text, is_ai, topic in [
                (10, 1, "Релиз модели GPT-7", 1, "модели и релизы"),
                (11, 1, "Мем про котиков", 0, None),
                (12, 2, "Пост про WOW", None, None)]:  # без строки classification
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,text,"
                "published_at,url) VALUES(?, 'telegram', ?, ?, ?, ?, ?)",
                (cid, sid, f"ch/{cid}", text, pub, f"https://t.me/ch/{cid}"))
            if is_ai is not None:
                conn.execute(
                    "INSERT INTO classification(content_id,platform,is_ai,topic,"
                    "status) VALUES(?, 'telegram', ?, ?, 'classified')",
                    (cid, is_ai, topic))
    return conn


def test_report_filter_by_is_ai_returns_expected_rows(tmp_path):
    conn = _report_db(tmp_path)
    cutoff = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        # OFF: Слой A только по статусу реестра — проходят все три поста.
        off_items, off_stats = report.telegram_items(conn, cutoff, require_ai=False)
        assert off_stats["considered"] == 3
        assert len(off_items) == 3

        # ON: пост-уровневый фильтр оставляет ТОЛЬКО is_ai=1.
        on_items, on_stats = report.telegram_items(conn, cutoff, require_ai=True)
        assert on_stats["considered"] == 1
        assert len(on_items) == 1
        assert "Релиз модели GPT-7" in on_items[0].desc
    finally:
        conn.close()


def test_report_topic_filter_flag_default_off(monkeypatch):
    monkeypatch.delenv("TUBER_TG_TOPIC_FILTER", raising=False)
    assert report.telegram_topic_filter_enabled() is False
    monkeypatch.setenv("TUBER_TG_TOPIC_FILTER", "1")
    assert report.telegram_topic_filter_enabled() is True
