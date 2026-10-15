"""P0-3: X-классификация дописывает расход в единый журнал ``llm_usage``.

Сеть не используется: клиент модели подменяется. Проверяем, что после прогона
с подставным usage в ``llm_usage`` появляется строка ``platform='x'``,
``stage='classify'``, и что отчёт расхода читает только ``llm_usage``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tuber.platforms.x import classify
from tuber.platforms.x.registry import text_hash


class FakeModel:
    """Подставной клиент: строгий JSON и usage с ценой."""

    model = "fake-model"

    def __init__(self, usage=None):
        self.calls = 0
        self.usage = usage or {"prompt_tokens": 1000, "completion_tokens": 500,
                               "cost_usd": 0.00028}

    def budget_status(self):
        return True, None, {}

    def available(self):
        return True

    def classify(self, system, user, max_tokens=None, json_mode=True,
                 temperature=None):
        import re
        self.calls += 1
        idxs = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", user, re.M)]
        items = [{"idx": i, "is_ai": 1, "topic": "релизы моделей",
                  "subtopic": "релиз", "claim_type": "release", "novelty": 0.5,
                  "lang": "en"} for i in idxs]
        return {"content": json.dumps({"items": items}), "usage": dict(self.usage)}

    def close(self):
        pass


def _acc(con, handle):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,?,?)",
                (handle, "A", "active"))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def _post(con, account_id, tid, text, handle):
    pub = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
        "%Y-%m-%d %H:%M:%S")
    con.execute(
        """INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,
             text, text_hash, owner_handle, lang, first_seen_at)
           VALUES (?,?,?,'rss',?,?,?,?,?)""",
        (account_id, str(tid), pub, text, text_hash(text), handle, "en",
         pub))
    con.commit()


def test_x_classify_writes_llm_usage(con):
    a = _acc(con, "a")
    _post(con, a, "1", "OpenAI released GPT-5 model for developers", "a")
    fake = FakeModel()
    classify.run(con, client=fake, check_budget=False)
    row = con.execute(
        "SELECT * FROM llm_usage WHERE platform='x'").fetchone()
    assert row is not None, "X обязан писать расход в llm_usage"
    assert row["stage"] == "classify"
    from tuber.platforms.x import config
    assert row["model"] == config.DEEPSEEK_MODEL
    assert row["tokens_in"] == 1000
    assert row["tokens_out"] == 500
    assert abs(row["cost_usd"] - 0.00028) < 1e-9


def test_x_classify_cost_computed_without_cost_field(con):
    a = _acc(con, "a")
    _post(con, a, "2", "Anthropic agent framework released today", "a")
    fake = FakeModel(usage={"prompt_tokens": 1_000_000, "completion_tokens": 0})
    classify.run(con, client=fake, check_budget=False)
    row = con.execute("SELECT * FROM llm_usage WHERE platform='x'").fetchone()
    # 1M входа по цене config.DEEPSEEK_PRICE_IN.
    from tuber.platforms.x import config
    expected = 1_000_000 * config.DEEPSEEK_PRICE_IN
    assert abs(row["cost_usd"] - expected) < 1e-9
