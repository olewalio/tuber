"""Р6 — тесты дискавери (сеть подменена фикстурами tests/fixtures/search_*.xml).

1. test_extract_candidates   2. test_candidates_dedup   3. test_blocklist
4. test_priority_score       5. test_verify_thresholds 6. test_daily_growth_cap
7. test_discovery_budget
"""
import os

from tuber.platforms.x import blocklist, config, store as db, discover, registry
from tuber.platforms.x.broker import parse_rss
from tests.x.mocking import make_feed

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _search_posts(host="https://one.test"):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "search_basic.xml"),
              encoding="utf-8") as fh:
        return parse_rss(fh.read(), host=host)


# ---------------------------------------------------------------- 1. извлечение
def test_extract_candidates():
    posts = _search_posts()
    by_owner = {p["owner_handle"]: p for p in posts}

    # автор + упоминания + ссылка x.com/<handle>
    a = by_owner["alpha"]
    cands = {(h, k): fv for h, k, fv in discover.extract_candidates(a, "релизы моделей")}
    assert cands[("alpha", "author")] == "author:релизы моделей"
    assert cands[("beta", "mention")] == "mention:релизы моделей"
    assert cands[("gamma", "mention")] == "mention:релизы моделей"
    assert cands[("delta", "link")] == "link:релизы моделей"

    # ретвит: владелец ленты vs автор оригинала
    rt = [p for p in posts if p["is_retweet"]][0]
    assert rt["owner_handle"] == "omega" and rt["orig_handle"] == "epsilon"
    rc = {(h, k): fv for h, k, fv in discover.extract_candidates(rt, "агенты и автоматизация")}
    assert rc[("omega", "author")] == "author:агенты и автоматизация"
    assert rc[("epsilon", "orig")] == "orig:агенты и автоматизация"

    # цитата: автор оригинала из blockquote, плюс упоминание @zeta в тексте
    q = [p for p in posts if p["is_quote"]][0]
    assert q["owner_handle"] == "quoter" and q["orig_handle"] == "janeresearcher"
    qc = {(h, k) for h, k, _ in discover.extract_candidates(q, "исследования и бенчмарки")}
    assert ("quoter", "author") in qc
    assert ("janeresearcher", "orig") in qc
    assert ("zeta", "mention") in qc

    # ссылка twitter.com/<handle>
    t = by_owner["theta"]
    tc = {(h, k) for h, k, _ in discover.extract_candidates(t, "инвестиции и раунды")}
    assert ("eta", "link") in tc


# ------------------------------------------------------------------- 2. дедуп
def test_candidates_dedup(con):
    r1 = discover.upsert_candidate(con, "foo", "author", "author:релизы моделей",
                                   "q:q1", "релизы моделей")
    assert r1["created"] is True and r1["distinct_sources"] == 1
    r2 = discover.upsert_candidate(con, "foo", "mention",
                                   "mention:инструменты разработчика",
                                   "obsX", "инструменты разработчика")
    r3 = discover.upsert_candidate(con, "foo", "mention",
                                   "mention:инструменты разработчика",
                                   "obsX", "инструменты разработчика")
    rows = con.execute("SELECT * FROM candidates WHERE handle='foo'").fetchall()
    assert len(rows) == 1, "одна запись на хендл (дедуп по PK)"
    row = rows[0]
    assert row["seen_count"] == 3
    assert row["distinct_sources"] == 2, "три источника, но два уникальных ключа"
    assert r2["created"] is False and r3["distinct_sources"] == 2


# --------------------------------------------------------------- 3. стоп-лист
def test_blocklist(con):
    # служебный хендл не попадает
    assert discover.upsert_candidate(con, "search", "author", "author:x", "q", "x") is None
    # добавленный в стоп-лист не попадает
    blocklist.add(con, "baduser", "manual")
    assert blocklist.is_blocked(con, "baduser")
    assert discover.upsert_candidate(con, "baduser", "author", "author:x", "q", "x") is None
    # уже в реестре — не кандидат (Р2.5)
    registry.add_account(con, "known", "C")
    assert discover.upsert_candidate(con, "known", "author", "author:x", "q", "x") is None
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


# ------------------------------------------------------------------ 4. скоринг
def test_priority_score(con):
    # базовый: 1 источник, рубрика 2, упоминание
    discover.upsert_candidate(con, "gooddev", "mention",
                              "mention:инструменты разработчика", "q:a",
                              "инструменты разработчика")
    assert registry.candidate_priority(con, "gooddev") == 1.0

    # + 3 за источник TIER-A
    con.execute("INSERT INTO accounts (handle, tier, status, added_by)"
                " VALUES ('tiera','A','active','test')")
    con.commit()
    discover.upsert_candidate(con, "tierAseen", "mention",
                              "mention:инструменты разработчика", "tiera",
                              "инструменты разработчика")
    assert registry.candidate_priority(con, "tierAseen") == 1.0 + 3.0

    # + 2 за рубрику приоритета 1 и + 1 за находку среди авторов
    discover.upsert_candidate(con, "prio1", "author", "author:релизы моделей",
                              "q:b", "релизы моделей")
    assert registry.candidate_priority(con, "prio1") == 1.0 + 2.0 + 1.0

    # - 2 за спам-шаблон (длина >= 13, длинная цифровая часть)
    assert registry.is_spam_handle("spam1234567890") is True
    discover.upsert_candidate(con, "spam1234567890", "mention",
                              "mention:инструменты разработчика", "q:c",
                              "инструменты разработчика")
    assert registry.candidate_priority(con, "spam1234567890") == 1.0 - 2.0


# --------------------------------------------------------------- 5. пороги
def _f(**kw):
    base = {"n": 20, "posts_per_day": 5.0, "cv_interval": 0.5,
            "link_ratio": 0.1, "rt_ratio": 0.1, "dup_ratio": 0.0}
    base.update(kw)
    return base


def test_verify_thresholds():
    # provisional: порог cooccurrence НЕ применяется, остальные пять — да
    ok, reason, _ = registry.evaluate_provisional(_f(), 20, 0)
    assert ok and reason is None
    ok, reason, _ = registry.evaluate_provisional(_f(), 14, 0)
    assert not ok and reason == "items_lt_15"
    ok, reason, _ = registry.evaluate_provisional(_f(posts_per_day=0.1), 20, 0)
    assert not ok and reason == "posts_per_day_out_of_range"
    ok, reason, _ = registry.evaluate_provisional(_f(cv_interval=0.14), 20, 0)
    assert not ok and reason == "cv_too_low"
    ok, reason, _ = registry.evaluate_provisional(_f(link_ratio=0.71), 20, 0)
    assert not ok and reason == "link_ratio_too_high"
    ok, reason, _ = registry.evaluate_provisional(_f(dup_ratio=0.31), 20, 0)
    assert not ok and reason == "dup_ratio_too_high"
    ok, reason, _ = registry.evaluate_provisional(_f(rt_ratio=0.81), 20, 0)
    assert not ok and reason == "rt_ratio_too_high"
    # границы: ровно на пороге — проходит
    ok, _, _ = registry.evaluate_provisional(_f(cv_interval=0.15, link_ratio=0.7,
                                                dup_ratio=0.3, rt_ratio=0.8), 15, 0)
    assert ok
    # full evaluate: cooccurrence всё ещё порог для прямой Р4.3-проверки
    ok, reason, checks = registry.evaluate_candidate(_f(), 20, 1)
    assert not ok and reason == "cooccurrence_lt_2"
    assert checks["cooccurrence_ge_2"] is False


# -------------------------------------------------------- 6. дневной потолок
def test_daily_growth_cap(con, make_broker):
    b = make_broker()
    handles = [f"cand{i:02d}" for i in range(60)]
    # два active-аккаунта TIER-A упоминают каждого кандидата (путь а)
    for core in ("core1", "core2"):
        con.execute("INSERT INTO accounts (handle, tier, status, added_by)"
                    " VALUES (?, 'A', 'active', 'test')", (core,))
    con.commit()
    core_ids = {h: con.execute("SELECT id FROM accounts WHERE handle=?", (h,)).fetchone()[0]
                for h in ("core1", "core2")}
    for i, h in enumerate(handles):
        registry.add_account(con, h, "C")
        b._fake_transport.set_feed(h, body=make_feed(20, handle=h, jitter=1.0))
        for core in ("core1", "core2"):
            con.execute(
                "INSERT INTO posts (account_id, tweet_id, published_at_utc,"
                " published_src, mentions, text) VALUES (?,?,?,'rss',?,?)",
                (core_ids[core], f"{core}-{h}", "2026-09-01T00:00:00",
                 f'["{h}"]', f"about @{h}"))
        con.commit()
    for h in handles:
        registry.verify_account(con, b, h)
    n_active = con.execute("SELECT COUNT(*) FROM accounts WHERE status='active'"
                           " AND handle LIKE 'cand%'").fetchone()[0]
    n_prov = con.execute("SELECT COUNT(*) FROM accounts WHERE status='provisional'"
                         ).fetchone()[0]
    assert n_active == config.DAILY_NEW_ACTIVE_CAP, (n_active, n_prov)
    assert n_active <= 50
    assert n_prov == 60 - config.DAILY_NEW_ACTIVE_CAP


# ------------------------------------------------------------------ 7. бюджет
def test_discovery_budget(con, make_broker):
    day = db.utcnow_iso()
    # исчерпываем СУТОЧНЫЙ бюджет дискавери (Р1.3)
    for i in range(config.DISCOVERY_DAILY_BUDGET):
        con.execute("INSERT INTO requests (host, ts, kind, url, status, items)"
                    " VALUES ('one.test', ?, 'search', 'u', 200, 1)", (day,))
    con.commit()
    b = make_broker()
    summary = discover.run_cycle(con, b, budget=20)
    assert summary["skipped"] is True and summary["reason"] == "budget_exhausted"
    assert summary["requests"] == 0
    assert b._fake_transport.calls == [], "при исчерпанном бюджете сеть не трогаем"
    last = con.execute("SELECT msg FROM run_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "бюджет" in last and "пропущен" in last


# ------------------------------------------------ Р2-БИС: разрыв замкнутого круга
def _seed_provisional(con, handle, *, mentions=(), ai_density=0.6, posts=12):
    con.execute(
        "INSERT INTO accounts (handle, tier, status, added_by, posts_collected,"
        " ai_density, ai_density_src, provisional_since)"
        " VALUES (?, 'C', 'provisional', 'test', ?, ?, 'heuristic', ?)",
        (handle, posts, ai_density, db.utcnow_iso()))
    con.commit()
    aid = con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()[0]
    for i, m in enumerate(mentions):
        con.execute(
            "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
            " mentions, text) VALUES (?,?,?,'rss',?,?)",
            (aid, f"{handle}-{i}", "2026-09-01T00:00:00", f'["{m}"]', f"cc @{m}"))
    con.commit()
    return aid


def test_promotion_by_mentions_breaks_deadlock(con, make_broker):
    """П8: два provisional с >=10 постов упоминают третьего -> third становится active.

    Ядро (active) пусто, но provisional-упоминатели разрешают разрыв круга (Р2-БИС.1).
    """
    b = make_broker()
    _seed_provisional(con, "prov1", mentions=("third",))
    _seed_provisional(con, "prov2", mentions=("third",))
    registry.add_account(con, "third", "C")
    b._fake_transport.set_feed("third", body=make_feed(20, handle="third", jitter=1.0))
    res = registry.verify_account(con, b, "third")
    assert res["mentioners"] == 2 and res["status"] == "active", res
    assert con.execute("SELECT status FROM accounts WHERE handle='third'"
                       ).fetchone()[0] == "active"
    msgs = [r[0] for r in con.execute(
        "SELECT msg FROM run_log WHERE handle='third'").fetchall()]
    assert any("core_mentions" in m for m in msgs), msgs


def test_provisional_mentioner_needs_10_posts_and_density(con, make_broker):
    """Р2-БИС.1/Р2-БИС.3: упоминание слабого provisional не засчитывается."""
    b = make_broker()
    _seed_provisional(con, "weak1", mentions=("target",), ai_density=0.3)   # плотность низкая
    _seed_provisional(con, "weak2", mentions=("target",), posts=5)          # постов мало
    registry.add_account(con, "target", "C")
    b._fake_transport.set_feed("target", body=make_feed(20, handle="target", jitter=1.0))
    res = registry.verify_account(con, b, "target")
    assert res["mentioners"] == 0 and res["status"] == "provisional", res


def test_bootstrap_blocks_provisional_mention_promotion(con):
    """Р2-БИС.2: пока active < 20, provisional -> active только путём (б).

    Provisional с двумя упоминаниями ядра НЕ промоутится по (а) в фазе bootstrap,
    но промоутится, когда ядро перешло порог 20.
    """
    for core in ("core1", "core2"):
        con.execute("INSERT INTO accounts (handle, tier, status, added_by)"
                    " VALUES (?, 'A', 'active', 'test')", (core,))
    con.commit()
    cid = {h: con.execute("SELECT id FROM accounts WHERE handle=?", (h,)).fetchone()[0]
           for h in ("core1", "core2")}
    for core in ("core1", "core2"):
        con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc,"
                    " published_src, mentions, text) VALUES (?,?,?,'rss',?,?)",
                    (cid[core], f"{core}-p1", "2026-09-01T00:00:00", '["p1"]', "cc @p1"))
    con.commit()
    _seed_provisional(con, "p1", ai_density=0.6)
    assert registry.bootstrap_phase(con) is True
    promoted = registry.promote_provisional(con)
    assert promoted == [], "в bootstrap путь (а) для provisional отключён"
    assert con.execute("SELECT status FROM accounts WHERE handle='p1'"
                       ).fetchone()[0] == "provisional"
    # набираем 20 active -> фаза роста, путь (а) открывается
    for i in range(18):
        con.execute("INSERT INTO accounts (handle, tier, status, added_by)"
                    " VALUES (?, 'C', 'active', 'test')", (f"filler{i:02d}",))
    con.commit()
    assert registry.bootstrap_phase(con) is False
    promoted = registry.promote_provisional(con)
    assert any(p["handle"] == "p1" and p["by"] == "core_mentions" for p in promoted)
    msgs = [r[0] for r in con.execute(
        "SELECT msg FROM run_log WHERE handle='p1'").fetchall()]
    assert any("by=core_mentions" in m for m in msgs), msgs


def test_promotion_by_tenure(con):
    """П9: provisional с 14 днями непрерывного сбора и ai_density>=0.5 -> active by=tenure."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S")
    con.execute("INSERT INTO accounts (handle, tier, status, added_by, posts_collected,"
                " ai_density, ai_density_src, provisional_since, last_success_at)"
                " VALUES ('tenured','C','provisional','test',15,0.7,'heuristic',?,?)",
                (old, db.utcnow_iso()))
    con.commit()
    n = registry.simulate_tenure(con, days=14)
    assert n == 1
    promoted = registry.promote_provisional(con, simulate_days=14)
    assert any(p["handle"] == "tenured" and p["by"] == "tenure" for p in promoted)
    row = con.execute("SELECT status, promo_path FROM accounts WHERE handle='tenured'"
                      ).fetchone()
    assert row["status"] == "active" and row["promo_path"] == "tenure"
    msgs = [r[0] for r in con.execute(
        "SELECT msg FROM run_log WHERE handle='tenured'").fetchall()]
    assert any("by=tenure" in m for m in msgs), msgs
