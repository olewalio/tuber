"""Тесты смысловой классификации Telegram (ТЗ-G). Сеть — только мок.

Каждый тест отвечает на пункт ТЗ (T1–T8). Рабочая единая база не трогается:
все проверки идут на временной базе из фикстуры ``tg_path``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tuber.platforms.telegram import classify

DAY = "2026-09-20"
NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


# --- вспомогательное -------------------------------------------------------


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    """Мок requests.Session: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, contents, usage=None):
        self.contents = list(contents)
        self.usage = usage or {"prompt_tokens": 100, "completion_tokens": 50}
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        content = self.contents.pop(0) if self.contents else "[]"
        return _Resp({
            "choices": [{"message": {"content": content}}],
            "usage": dict(self.usage),
        })


def _arr(*items):
    return json.dumps(list(items), ensure_ascii=False)


def _item(cid, **kw):
    base = {
        "content_id": cid, "is_ai": True, "topic": "модели и релизы",
        "subtopic": "релиз", "confidence": 0.9,
        "title_ru": "Новый релиз модели",
        "summary_ru": "Коротко о релизе модели.", "lang": "ru",
        "reason": "предмет поста — ИИ",
    }
    base.update(kw)
    return base


def add_source(con, handle, title, source_id):
    con.execute(
        "INSERT INTO source(id, platform, handle, title, status) VALUES(?,?,?,?,?)",
        (source_id, "telegram", handle, title, "active"),
    )


def add_post(con, external_id, source_id, text, published="2026-09-20 10:00:00"):
    con.execute(
        "INSERT INTO content(platform, external_id, source_id, text,"
        " published_at, url) VALUES('telegram', ?, ?, ?, ?, ?)",
        (external_id, source_id, text, published, f"https://t.me/{external_id}"),
    )
    con.commit()


@pytest.fixture()
def ai_and_non_ai(con):
    """Один ИИ-канал и один не-ИИ; по одному посту в каждом."""
    add_source(con, "vibecoding_tg", "Vibe Coding", 1)
    add_source(con, "rian_ru", "РИА Новости", 2)
    add_post(con, "vibecoding_tg/1", 1, "Claude Code добавил поддержку AGENTS.md")
    add_post(con, "rian_ru/9", 2, "Погода в Москве на выходные")
    return con


# --- T1: только вайтлист ----------------------------------------------------


def test_t1_only_whitelist_posts_go_to_model(ai_and_non_ai):
    con = ai_and_non_ai
    handles = classify.build_whitelist(con)
    assert "vibecoding_tg" in handles
    assert "rian_ru" not in handles
    pending = classify.select_pending(con, handles)
    assert [r["handle"] for r in pending] == ["vibecoding_tg"]


def test_t1_non_ai_channel_ignored_even_with_limit(ai_and_non_ai, capsys):
    con = ai_and_non_ai
    session = FakeSession([_arr(_item(1))])
    summary = classify.classify(con, api_key="k", session=session, now=NOW)
    assert summary["selected"] == 1
    assert len(session.posts) == 1
    rows = con.execute(
        "SELECT c.external_id, cl.is_ai FROM classification cl"
        " JOIN content c ON c.id=cl.content_id").fetchall()
    assert [r["external_id"] for r in rows] == ["vibecoding_tg/1"]


# --- T2: title_ru/summary_ru пишутся ---------------------------------------


def test_t2_title_and_summary_are_stored(ai_and_non_ai):
    con = ai_and_non_ai
    session = FakeSession([_arr(_item(1, title_ru="Релиз для кодинга",
                                     summary_ru="Вышел новый инструмент."))])
    classify.classify(con, api_key="k", session=session, now=NOW)
    row = con.execute(
        "SELECT * FROM classification WHERE platform='telegram'").fetchone()
    assert row["title_ru"] == "Релиз для кодинга"
    assert row["summary_ru"] == "Вышел новый инструмент."
    assert row["method"] == "deepseek"
    assert row["status"] == "classified"
    assert row["prompt_ver"] == classify.PROMPT_VER


# --- T3: невалидный JSON → две попытки → NULL + error -----------------------


def test_t3_invalid_json_two_attempts_then_error(ai_and_non_ai):
    con = ai_and_non_ai
    session = FakeSession(["не json", "тоже не json"])
    summary = classify.classify(con, api_key="k", session=session, now=NOW)
    assert summary["model_calls"] == 2
    assert summary["failed"] == 1
    row = con.execute(
        "SELECT * FROM classification WHERE platform='telegram'").fetchone()
    assert row["is_ai"] is None
    assert row["status"] == "error"
    assert row["title_ru"] is None and row["summary_ru"] is None
    assert row["attempts"] == 2


# --- T4: суточный потолок ---------------------------------------------------


def test_t4_daily_cap_stops_run(ai_and_non_ai, monkeypatch, capsys):
    con = ai_and_non_ai
    monkeypatch.setenv("TG_CLASSIFY_DAILY_CAP", "1")
    con.execute(
        "INSERT INTO classify_daily(day, platform, posts, cost_usd)"
        " VALUES(?,?,?,?)", (DAY, "telegram", 1, 0.0))
    con.commit()
    session = FakeSession([_arr(_item(1))])
    summary = classify.classify(con, api_key="k", session=session, now=NOW)
    out = capsys.readouterr().out
    assert summary["capped"] is True
    assert session.posts == []
    assert "TG-CAP" in out
    assert con.execute(
        "SELECT COUNT(*) FROM classification").fetchone()[0] == 0


# --- T5: нет ключа ----------------------------------------------------------


def test_t5_no_key_alerts_and_skips(ai_and_non_ai, monkeypatch, capsys):
    con = ai_and_non_ai
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(classify, "load_env", lambda path=None: 0)
    session = FakeSession([_arr(_item(1))])
    summary = classify.classify(con, session=session, now=NOW)
    out = capsys.readouterr().out
    assert summary["no_key"] is True
    assert session.posts == []
    assert "ALERT: нет ключа DeepSeek" in out
    assert con.execute(
        "SELECT COUNT(*) FROM classification").fetchone()[0] == 0


def test_t5_main_returns_zero_without_key(ai_and_non_ai, monkeypatch, capsys):
    con = ai_and_non_ai
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(classify, "load_env", lambda path=None: 0)
    from tuber.platforms.telegram import store as db
    monkeypatch.setattr(db, "connect", lambda *a, **k: con)
    rc = classify.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ALERT" in out


# --- T6: dry-run ничего не пишет -------------------------------------------


def test_t6_dry_run_writes_nothing(ai_and_non_ai, capsys):
    con = ai_and_non_ai
    session = FakeSession([_arr(_item(1))])
    summary = classify.classify(con, dry_run=True, session=session, now=NOW)
    capsys.readouterr()
    assert summary["dry_run"] is True
    assert session.posts == []
    assert con.execute(
        "SELECT COUNT(*) FROM classification").fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM classify_daily").fetchone()[0] == 0


# --- T7: кэш повторного текста ---------------------------------------------


def test_t7_same_text_uses_cache_no_second_call(con):
    add_source(con, "aichannel", "AI Channel", 1)
    add_source(con, "gpt_news", "GPT News", 2)
    add_post(con, "aichannel/1", 1, "Один и тот же текст про модель")
    session = FakeSession([_arr({"content_id": 1, "is_ai": True,
                                 "topic": "модели и релизы", "subtopic": None,
                                 "confidence": 0.8, "title_ru": "Заголовок",
                                 "summary_ru": "Описание.", "lang": "ru",
                                 "reason": "ИИ"})])
    first = classify.classify(con, api_key="k", session=session, now=NOW)
    assert first["classified"] == 1
    assert len(session.posts) == 1

    # Тот же текст в другом канале — берём из кэша, модель не вызывается.
    add_post(con, "gpt_news/2", 2, "Один и тот же текст про модель")
    second = classify.classify(con, api_key="k", session=session, now=NOW)
    assert second["cached"] == 1
    assert second["classified"] == 1
    assert len(session.posts) == 1  # второго вызова модели нет
    assert con.execute(
        "SELECT COUNT(*) FROM classification WHERE platform='telegram'"
    ).fetchone()[0] == 2


# --- T8: сборка вайтлиста ---------------------------------------------------


def test_t8_build_whitelist_file(con, tmp_path):
    add_source(con, "vibecoding_tg", "Vibe Coding", 1)
    add_source(con, "prog_ai", "Программирование и ИИ", 2)
    add_source(con, "dailyprompts", "Промпты дня", 3)
    add_source(con, "rian_ru", "РИА Новости", 4)
    add_source(con, "mash", "Mash", 5)
    path = tmp_path / "telegram_ai_channels.txt"
    handles = classify.build_whitelist_file(con, path=str(path))
    lowered = {h.lower() for h in handles}
    assert {"vibecoding_tg", "prog_ai", "dailyprompts"} <= lowered
    assert "rian_ru" not in lowered
    assert "mash" not in lowered
    text = path.read_text(encoding="utf-8")
    assert "vibecoding_tg" in text and "mash" not in text


# --- разбор ответов модели --------------------------------------------------


def test_parse_accepts_fence_and_drops_unknown_topic():
    text = "```json\n" + _arr(_item(1, topic="крипта и мемы")) + "\n```"
    parsed = classify.parse_response(text, ["1"])
    assert parsed is not None
    assert parsed["1"]["topic"] is None


def test_parse_ignores_foreign_ids():
    parsed = classify.parse_response(_arr(_item(1), _item(999)), ["1"])
    assert set(parsed) == {"1"}
