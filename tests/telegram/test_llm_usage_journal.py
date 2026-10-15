"""P0-3: Telegram-классификация и реклама пишут расход в ``llm_usage``.

Сеть не используется. Для классификации подменяется ``requests.Session``, чтобы
прогон шёл «как настоящий» (``session is None`` -> запись в журнал включена).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tuber.platforms.telegram import ads, classify

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_real_jsonl(monkeypatch):
    """Не трогать боевой jsonl: подменяем внешний след детерминированным."""
    from tuber.platforms.x import channels

    def _fake_log(script, model, pt=0, ct=0, cr=0, cost_usd=None, path=None):
        cost = 0.000126 if cost_usd is None else cost_usd
        return {"script": script, "model": model, "prompt_tokens": pt,
                "completion_tokens": ct, "cache_read_tokens": cr,
                "cost_usd": cost}

    monkeypatch.setattr(channels, "log_deepseek_usage", _fake_log)


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

    def close(self):
        pass


def _arr(*items):
    return json.dumps(list(items), ensure_ascii=False)


def _topic_item(cid):
    return {"content_id": cid, "is_ai": True, "topic": "модели и релизы",
            "subtopic": "релиз", "confidence": 0.9, "title_ru": "t",
            "summary_ru": "s", "lang": "ru", "reason": "r"}


def _ad_item(cid):
    return {"content_id": cid, "is_ad": True, "confidence": 0.9, "reason": "r"}


def _setup(con, external_id="vibecoding_tg/1", text="Claude Code AGENTS.md"):
    con.execute("INSERT INTO source(id, platform, handle, title, status)"
                " VALUES(1,'telegram','vibecoding_tg','Vibe','active')")
    con.execute(
        "INSERT INTO content(platform, external_id, source_id, text,"
        " published_at, url) VALUES('telegram',?,1,?,?,?)",
        (external_id, text, "2026-09-20 10:00:00", "https://t.me/" + external_id))
    con.commit()


def test_tg_classify_writes_llm_usage(con, monkeypatch):
    _setup(con)
    fake = FakeSession([_arr(_topic_item(1))])
    monkeypatch.setattr("requests.Session", lambda: fake)
    classify.classify(con, api_key="k", now=NOW)
    row = con.execute(
        "SELECT * FROM llm_usage WHERE platform='telegram'"
        " AND stage='classify'").fetchone()
    assert row is not None, "Telegram classify обязан писать в llm_usage"
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
    assert abs(row["cost_usd"] - 0.000126) < 1e-9


def test_tg_ads_writes_llm_usage(con, monkeypatch):
    _setup(con, external_id="vibecoding_tg/2", text="Реклама промокод скидка")
    fake = FakeSession([_arr(_ad_item(1))])
    monkeypatch.setattr("requests.Session", lambda: fake)
    ads.run(con, api_key="k", now=NOW)
    row = con.execute(
        "SELECT * FROM llm_usage WHERE platform='telegram' AND stage='ads'"
    ).fetchone()
    assert row is not None, "Telegram ads обязан писать в llm_usage"
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
