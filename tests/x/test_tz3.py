"""ТЗ-3: классификация, сюжеты, оси, отчёт и сторож — только подставные данные.

Сеть не используется: клиент модели подменяется, транспорт не вызывается.
"""
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from tuber.platforms.x import classify, config, store as db, health, report, scores, stories
from tests.x.mocking import mark_ai


# ------------------------------------------------------------------ helpers
def acc(con, handle, *, tier="A", status="active", dup=None, ai=None):
    con.execute("INSERT INTO accounts (handle, tier, status, dup_ratio, ai_density)"
                " VALUES (?,?,?,?,?)", (handle, tier, status, dup, ai))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def post(con, account_id, tid, text, *, minutes_ago=60, handle=None, mentions=None,
         links=None, is_retweet=0, is_quote=0, lang="en", likes=None, replies=None,
         metrics=True, published=None, first_seen=None):
    from tuber.platforms.x.registry import text_hash
    pub = published or db.iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    con.execute(
        """INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,
             text, text_hash, mentions, links, is_retweet, is_quote, owner_handle, lang,
             likes, replies, metrics_at, metrics_src, first_seen_at)
           VALUES (?,?,?,'rss',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, str(tid), pub, text, text_hash(text),
         json.dumps(mentions or []), json.dumps(links or []), is_retweet, is_quote,
         handle, lang, likes, replies,
         db.iso(datetime.now(timezone.utc)) if metrics else None,
         "cdn" if metrics else None, first_seen or db.utcnow_iso()))
    con.commit()
    return str(tid)


class FakeModel:
    """Подставной клиент модели. Считает вызовы, отдаёт строгий JSON."""

    model = "fake-model"

    def __init__(self, *, topic="релизы моделей", claim="release", bad_first=False):
        self.calls = 0
        self.topic = topic
        self.claim = claim
        self.bad_first = bad_first
        self.bad_left = 1 if bad_first else 0

    def budget_status(self):
        return True, None, {}

    def available(self):
        return True

    def classify(self, system, user, max_tokens=None, json_mode=True,
                 temperature=None):
        import re
        self.calls += 1
        if self.bad_left > 0:
            self.bad_left -= 1
            return {"content": "не json", "usage": {}}
        idxs = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", user, re.M)]
        items = [{"idx": i, "is_ai": 1, "topic": self.topic, "subtopic": "новая тема",
                  "claim_type": self.claim, "novelty": 0.5, "lang": "en"} for i in idxs]
        return {"content": json.dumps({"items": items}),
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "cost_usd": 0.00001}}

    def close(self):
        pass


# ----------------------------------------------- Р6.1 кластеризация (D-08)
def test_text_overlap_cluster(con):
    """Почти дословные перепечатки — один сюжет; чужая тема — отдельно.

    После пересборки (D-08) склейку решает IDF-взвешенное пересечение токенов,
    а не порог simhash; simhash оставлен диагностикой (см. test_simhash_deterministic).
    """
    a = acc(con, "a")
    same1 = "OpenAI released GPT-5, a new large language model for developers today"
    same2 = "OpenAI released GPT-5 — a new large language model for developers today!"
    other = "NASA launched a new space telescope to study distant galaxies"
    p1 = post(con, a, "1", same1, handle="a")
    p2 = post(con, a, "2", same2, handle="b")
    p3 = post(con, a, "3", other, handle="c")
    rows = list(con.execute("SELECT * FROM posts ORDER BY tweet_id"))
    clusters = stories.cluster_posts(rows)
    by_id = {frozenset(str(p["tweet_id"]) for p in c) for c in clusters}
    assert frozenset({"1", "2"}) in by_id
    assert all("3" not in c for c in by_id if len(c) > 1)


def test_giant_entity_alone_does_not_glue(con):
    """Одно общее слово-гигант (openai) не склеивает разные события (D-08)."""
    a = acc(con, "a")
    post(con, a, "1", "OpenAI released GPT-5 for developers", handle="a")
    post(con, a, "2", "OpenAI hires a new chief financial officer in New York",
         handle="b")
    rows = list(con.execute("SELECT * FROM posts ORDER BY tweet_id"))
    clusters = stories.cluster_posts(rows)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 1]


# ---------------------------------------------------------------- Р6.2 роли
def test_roles(con):
    a = acc(con, "primary")
    b = acc(con, "echoer")
    c = acc(con, "amplifier")
    d = acc(con, "extender")
    base = "OpenAI released GPT-5 model for developers"
    post(con, a, "10", base, minutes_ago=100, handle="primary")
    post(con, b, "11", "OpenAI released GPT-5 model for developers", minutes_ago=90,
         handle="echoer")
    post(con, c, "12", base, minutes_ago=80, handle="amplifier", is_retweet=1,
         mentions=["primary"])
    post(con, d, "13", base + " and it now supports video via Sora",
         minutes_ago=70, handle="extender")
    rows = list(con.execute("SELECT * FROM posts ORDER BY published_at_utc"))
    roles = dict(stories.assign_roles(rows))
    assert roles["10"] == "primary"
    assert roles["11"] == "echo"
    assert roles["12"] == "amplifier"
    assert roles["13"] == "extender"


# ---------------------------------------------------------------- Р6.3 xconf
def test_xconf(con):
    a = acc(con, "one")
    b = acc(con, "two")
    posts = []
    for i in range(5):
        post(con, a, f"a{i}", f"post about GPT model number {i}", handle="one")
        post(con, b, f"b{i}", f"another post about GPT model number {i}", handle="two")
    rows = list(con.execute("SELECT * FROM posts"))
    assert len(rows) == 10
    assert stories.xconf(rows) == 2


# --------------------------------------------------------- Р6.4 significance
def test_significance(con):
    # (1) графово-временная ветка
    sig1, eng1, vel1, spread1 = scores.significance_values(
        likes6=None, replies6=None, xconf=3, hours_since=2, metrics_missing=True)
    assert sig1 == pytest.approx((2 ** 0.8) / ((2 + 2) ** 1.8), abs=1e-6)
    assert eng1 is None and vel1 is None and spread1 == 2
    # (2) популярность, xconf=1 (spread=0)
    sig2, eng2, vel2, spread2 = scores.significance_values(
        likes6=600, replies6=10, xconf=1, hours_since=6, metrics_missing=False)
    expected2 = (math.log1p(620 / 6.0)) / ((1 + 6 / 24.0) ** 1.8)
    assert sig2 == pytest.approx(expected2, abs=1e-6)
    assert eng2 == 620 and vel2 == pytest.approx(620 / 6.0) and spread2 == 0
    # (3) популярность, лайков нет, есть распространение
    sig3, _e, _v, spread3 = scores.significance_values(
        likes6=0, replies6=0, xconf=5, hours_since=0, metrics_missing=False)
    assert sig3 == pytest.approx(0.8 * math.log1p(4), abs=1e-6)
    assert spread3 == 4


def test_metrics_missing_branch(con):
    """Р3.1-бис: без метрик формула подменяется и ставится metrics_missing=1."""
    a = acc(con, "nometrics")
    post(con, a, "50", "no metrics here", handle="nometrics", metrics=False)
    post(con, a, "51", "with metrics here", handle="nometrics2", likes=10, replies=1)
    row = con.execute("SELECT * FROM posts WHERE tweet_id='50'").fetchone()
    rec = scores.score_post(con, row)
    assert rec["metrics_missing"] == 1
    assert rec["branch"] == "graph"
    assert rec["likes_at_6h"] is None
    row2 = con.execute("SELECT * FROM posts WHERE tweet_id='51'").fetchone()
    rec2 = scores.score_post(con, row2)
    assert rec2["metrics_missing"] == 0 and rec2["branch"] == "popularity"


# ------------------------------------------------------ Р6.5 suspect excluded
def test_suspect_excluded(con):
    handles = ["spam1", "spam2", "spam3", "spam4", "spam5"]
    ids = []
    for h in handles:
        aid = acc(con, h, dup=0.9)
        ids.append(post(con, aid, f"s{h}", "identical circulating text about funding",
                        handle=h, likes=5, replies=1))
    mark_ai(con)
    st = stories.run(con, now=datetime.now(timezone.utc))
    assert st["suspect"] == 1
    sus = scores.suspect_stories(con)
    assert len(sus) == 1 and sus[0]["xconf"] >= config.SUSPECT_MIN_XCONF
    scores.compute(con)
    scored = {r["tweet_id"] for r in con.execute("SELECT tweet_id FROM scores")}
    assert not (scored & set(ids)), "suspect-сюжет не должен попадать в рейтинги"


def test_retweet_not_zero_in_ranking(con):
    """Р3.1-тер: ретвит не оценивается как самостоятельный и не даёт нуля."""
    a = acc(con, "author")
    b = acc(con, "rt")
    post(con, a, "100", "OpenAI GPT-5 released, big news", handle="author",
         likes=500, replies=20)
    post(con, b, "101", "RT @author: OpenAI GPT-5 released, big news", handle="rt",
         is_retweet=1, likes=0, replies=0,
         links=["https://x.com/author/status/100"])
    mark_ai(con)
    stories.run(con, now=datetime.now(timezone.utc))
    recs = scores.compute(con)
    ids = {r["tweet_id"] for r in recs}
    assert "101" not in ids, "ретвит не должен оцениваться как самостоятельный пост"
    assert "100" in ids
    assert scores.resolve_original_tweet_id(
        con.execute("SELECT * FROM posts WHERE tweet_id='101'").fetchone()) == "100"


# ------------------------------------------------------------------ classify
def test_classify_cache(con):
    a = acc(con, "a", tier="A")
    post(con, a, "1", "OpenAI model release news for agents", handle="a")
    post(con, a, "2", "Anthropic agent framework released today", handle="a")
    fake = FakeModel()
    s1 = classify.run(con, client=fake, check_budget=False)
    assert s1["classified"] == 2 and fake.calls == 1
    s2 = classify.run(con, client=fake, check_budget=False)
    assert s2["selected"] == 0 and s2["classified"] == 0
    assert fake.calls == 1, "повторный прогон не должен звать модель (кэш text_hash)"


def test_classify_cache_by_same_text_hash(con):
    a = acc(con, "a")
    post(con, a, "1", "identical text about models", handle="a")
    post(con, a, "2", "identical text about models", handle="b")
    fake = FakeModel()
    s = classify.run(con, client=fake, check_budget=False)
    assert s["classified"] == 1
    assert fake.calls == 1


def test_classify_retry_then_failed(con):
    a = acc(con, "a")
    post(con, a, "1", "OpenAI model release with agents", handle="a")
    fake = FakeModel(bad_first=True)
    s = classify.run(con, client=fake, check_budget=False)
    assert fake.calls == 2, "при невалидном JSON — ровно один повтор"
    assert s["classified"] == 1
    # всегда плохой ответ -> failed и повтор в следующем прогоне
    class AlwaysBad(FakeModel):
        def classify(self, *a, **k):
            self.calls += 1
            return {"content": "{bad", "usage": {}}
    bad = AlwaysBad()
    b = acc(con, "b")
    post(con, b, "9", "Anthropic agent platform released", handle="b")
    s2 = classify.run(con, client=bad, check_budget=False)
    assert s2["failed"] == 1
    assert con.execute("SELECT status FROM classified WHERE tweet_id='9'"
                       ).fetchone()["status"] == "failed"


def test_classify_prefilter_marks_heuristic_not_classified(con):
    """ТЗ-6 задача 1: предфильтр помечает heuristic ТОЛЬКО при явном флаге."""
    a = acc(con, "a")
    post(con, a, "1", "Totally unrelated note about weather and lunch", handle="a")
    fake = FakeModel()
    s = classify.run(con, client=fake, check_budget=False, use_prefilter=True)
    assert s["heuristic"] == 1 and s["classified"] == 0 and fake.calls == 0
    row = con.execute("SELECT * FROM classified WHERE tweet_id='1'").fetchone()
    assert row["status"] == "heuristic" and row["method"] == "heuristic"
    assert row["topic"] is None and row["is_ai"] == 0


def test_classify_budget_blocks(con):
    a = acc(con, "a")
    post(con, a, "1", "OpenAI model release with agents", handle="a")

    class NoBudget(FakeModel):
        def budget_status(self):
            return False, "дневной бюджет исчерпан", {}
    fake = NoBudget()
    s = classify.run(con, client=fake, check_budget=True)
    assert s["budget_ok"] is False and s["classified"] == 0 and fake.calls == 0
    assert "исчерпан" in s["budget_reason"]


def test_classify_daily_cap(con, monkeypatch):
    a = acc(con, "a")
    for i in range(5):
        post(con, a, f"{i}", f"OpenAI model number {i} release for agents", handle="a")
    monkeypatch.setattr(config, "CLASSIFY_DAILY_CAP", 3)
    fake = FakeModel()
    s = classify.run(con, client=fake, check_budget=False)
    assert s["selected"] == 3 and s["classified"] == 3


# ------------------------------------------------------------------- stories
def test_stories_run_single_and_xconf(con):
    a = acc(con, "a")
    post(con, a, "1", "OpenAI released GPT-5 model", handle="a")
    mark_ai(con)
    st = stories.run(con, now=datetime.now(timezone.utc))
    assert st["stories"] == 1 and st["single"] == 1
    row = con.execute("SELECT * FROM stories").fetchone()
    assert row["xconf"] == 1 and row["is_single"] == 1
    # single не попадает в главный блок
    assert stories.main_stories(con) == []


def test_first_mover_score(con):
    a = acc(con, "fm")
    b = acc(con, "other")
    now = datetime.now(timezone.utc)
    ts = db.iso(now)
    for i in range(2):
        # ТЗ п.3.1: в ось входят только сюжеты, где у автора был конкурент,
        # то есть xconf >= 2.
        con.execute("INSERT INTO stories (created_at, published_at, xconf)"
                    " VALUES (?,?,2)", (ts, ts))
        sid = db.last_insert_id(con)
        role = "primary" if i == 0 else "echo"
        con.execute("INSERT INTO story_posts (story_id, tweet_id, handle, role)"
                    " VALUES (?,?,?,?)", (sid, f"t{i}", "fm", role))
        con.execute("INSERT INTO story_posts (story_id, tweet_id, handle, role)"
                    " VALUES (?,?,?,?)", (sid, f"u{i}", "other", "echo"))
    con.commit()
    fm = scores.first_mover_score(con, persist=False)
    assert fm["fm"]["score"] == pytest.approx(0.5, abs=1e-6)


def test_first_mover_single_author_is_null(con):
    """Аккаунт с одними одиночками не получает 1.000 — у него NULL (ТЗ п.3.2)."""
    acc(con, "loner")
    now = datetime.now(timezone.utc)
    ts = db.iso(now)
    for i in range(2):
        con.execute("INSERT INTO stories (created_at, published_at, xconf)"
                    " VALUES (?,?,1)", (ts, ts))
        sid = db.last_insert_id(con)
        con.execute("INSERT INTO story_posts (story_id, tweet_id, handle, role)"
                    " VALUES (?,?,?,?)", (sid, f"l{i}", "loner", "primary"))
    con.commit()
    fm = scores.first_mover_score(con, persist=False)
    assert "loner" not in fm
    scores.first_mover_score(con, persist=True)
    row = con.execute("SELECT first_mover_score FROM accounts WHERE handle='loner'"
                      ).fetchone()
    assert row["first_mover_score"] is None


def test_darks(con):
    now = datetime.now(timezone.utc)
    ts = db.iso(now - timedelta(hours=1))
    for i in range(5):
        con.execute("INSERT INTO stories (created_at, published_at) VALUES (?,?)",
                    (ts, ts))
        sid = db.last_insert_id(con)
        con.execute("INSERT INTO story_posts (story_id, tweet_id, handle, role)"
                    " VALUES (?,?,?,?)", (sid, f"d{i}", "dark", "echo"))
    con.commit()
    dk = scores.darks(con, persist=False, min_stories=5)
    assert any(d["handle"] == "dark" and d["stories_cur"] == 5
               and d["stories_prev"] == 0 for d in dk)


def test_darks_excludes_top20(con):
    now = datetime.now(timezone.utc)
    ts = db.iso(now - timedelta(hours=1))
    a = acc(con, "star")
    for i in range(5):
        con.execute("INSERT INTO stories (created_at, published_at) VALUES (?,?)",
                    (ts, ts))
        sid = db.last_insert_id(con)
        con.execute("INSERT INTO story_posts (story_id, tweet_id, handle, role)"
                    " VALUES (?,?,?,?)", (sid, f"s{i}", "star", "primary"))
    con.commit()
    p = post(con, a, "900", "star post", handle="star", likes=1000)
    mark_ai(con)
    con.execute("INSERT INTO scores (tweet_id, computed_at, significance)"
                " VALUES (?,?,?)", (p, ts, 10.0))
    con.commit()
    dk = scores.darks(con, persist=False, min_stories=5, top_n=20)
    assert all(d["handle"] != "star" for d in dk)


# -------------------------------------------------------------------- report
def test_report_blocks(con):
    text = report.build(con, date="2026-09-15", translator=False,
                        now=datetime(2026, 9, 15, 12, tzinfo=timezone.utc))
    for header in ("1. Главное за сутки", "2. По темам", "3. Деньги и запуски",
                   "4. Новинки и инструменты", "5. Русскоязычный срез",
                   "6. Тёмные лошадки и первые авторы", "7. Служебный блок"):
        assert header in text, f"нет блока: {header}"
    assert report.NO_DATA in text


def test_report_no_missing_date(con):
    a = acc(con, "a")
    good = post(con, a, "700", "OpenAI выпустила модель", handle="a", lang="ru",
                published="2026-09-15T10:00:00")
    bad = post(con, a, "701", "сломанная дата но русский текст", handle="a", lang="ru",
               published="2026-09-15T99:99:99")
    mark_ai(con)
    text = report.build(con, date="2026-09-15", translator=False,
                        now=datetime(2026, 9, 15, 12, tzinfo=timezone.utc))
    assert good in text
    assert bad not in text, "пост без проверенной даты не должен попадать в блоки"


def test_report_has_links_and_authors(con):
    a = acc(con, "first")
    b = acc(con, "second")
    post(con, a, "1", "OpenAI released GPT-5 model for developers", handle="first",
         minutes_ago=120)
    post(con, b, "2", "OpenAI released GPT-5 model for developers now", handle="second",
         minutes_ago=60)
    mark_ai(con)
    stories.run(con, now=datetime.now(timezone.utc))
    text = report.build(con, date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        translator=False)
    assert "https://x.com/first/status/1" in text
    assert "@first" in text


# -------------------------------------------------------------------- health
def test_health_silent(con):
    a = acc(con, "a", status="active")
    post(con, a, "1", "fresh post", handle="a")
    # Задача 6 ТЗ виральности: норма включает строку значимости за окно, иначе
    # сторож честно кричит, что отчёт будет пустым.
    con.execute("INSERT INTO scores (tweet_id, computed_at, significance)"
                " VALUES ('1', ?, 1.0)", (db.utcnow_iso(),))
    con.execute("INSERT INTO instances (host, healthy, last_check_at) VALUES (?,1,?)",
                ("https://nitter.example", db.utcnow_iso()))
    # ТЗ-11: норма теперь включает свежий успешный прогон дискавери — иначе
    # сторож честно сообщает, что расширение реестра не работает.
    con.execute("INSERT INTO runs (started_at, finished_at, mode, errors)"
                " VALUES (?,?, 'discover:all:120', 0)",
                (db.utcnow_iso(), db.utcnow_iso()))
    con.commit()
    res = health.run(con)
    assert res["alerts"] == []
    assert health.format_alerts(res) == ""
    assert res["ok"] is True


def test_health_alert_one_line(con):
    res = health.run(con)   # пустая база -> stale ALERT
    out = health.format_alerts(res)
    lines = [l for l in out.splitlines() if l.strip()]
    assert res["alerts"] and len(lines) == len(res["alerts"])
    assert all(l.startswith("ALERT:") for l in lines)


def test_health_thresholds(con):
    # доля отказов > 20%
    for _ in range(12):
        con.execute("INSERT INTO requests (host, ts, kind, status) VALUES ('h',?,?,?)",
                    (db.utcnow_iso(), "feed", 500))
    for _ in range(4):
        con.execute("INSERT INTO requests (host, ts, kind, status) VALUES ('h',?,?,?)",
                    (db.utcnow_iso(), "feed", 200))
    con.commit()
    assert health.check_fail_rate(con)["alert"] is True
    # живых инстансов ноль при известных
    con.execute("INSERT INTO instances (host, healthy) VALUES ('x', 0)")
    con.commit()
    assert health.check_instances_alive(con)["alert"] is True
    # даты за сутки < 99%
    a = acc(con, "a")
    con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc,"
                " published_src, first_seen_at) VALUES"
                " (?, '1', '1970-01-01T00:00:00', 'dup', ?)",
                (a, db.utcnow_iso()))
    con.commit()
    assert health.check_date_validity(con)["alert"] is True


# ------------------------------------------------------------------- misc
def test_simhash_deterministic():
    t = "OpenAI GPT-5 released for developers"
    assert stories.simhash64(t) == stories.simhash64(t)
    assert stories.hamming(stories.simhash64(t), stories.simhash64(t)) == 0
    assert stories.extract_entities("OpenAI ships GPT-5, see @sama") >= {"openai", "gpt-5"}
