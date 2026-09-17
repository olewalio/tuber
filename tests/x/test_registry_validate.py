"""Р7.6 — все шесть порогов Р4.3: по одному тесту на отказ."""
from tuber.platforms.x import config, store as db, registry
from tests.x.mocking import make_feed


def _features(items=20, mentioners=3, ppd=5.0, cv=0.5, link=0.1, dup=0.0, rt=0.1):
    return ({"n": items, "posts_per_day": ppd, "cv_interval": cv,
             "link_ratio": link, "rt_ratio": rt, "dup_ratio": dup}, items, mentioners)


def _evaluate(**kw):
    f, items, mentioners = _features(**kw)
    return registry.evaluate_candidate(f, items, mentioners)


# --- Р4.1: хендл
def test_bad_handle_rejected_not_raised():
    for bad in ["", "@", "bad handle", "toolonghandle1234567", "layerzero_core\\",
                "привет", "a" * 16, None]:
        h, err = registry.validate_handle(bad)
        assert err == "bad_handle" and h is None
    assert registry.validate_handle("@NASA") == ("nasa", None)


# --- Р4.3.1
def test_reject_items_lt_15():
    ok, reason, checks = _evaluate(items=14)
    assert not ok and reason == "items_lt_15"
    assert checks["items_ge_15"] is False
    ok2, _, _ = _evaluate(items=15)
    assert ok2 is True


# --- Р4.3.2
def test_reject_cooccurrence_lt_2():
    ok, reason, checks = _evaluate(mentioners=1)
    assert not ok and reason == "cooccurrence_lt_2"
    assert checks["cooccurrence_ge_2"] is False
    ok2, _, _ = _evaluate(mentioners=2)
    assert ok2 is True


# --- Р4.3.3
def test_reject_posts_per_day_too_low():
    ok, reason, _ = _evaluate(ppd=0.1)
    assert not ok and reason == "posts_per_day_out_of_range"


def test_reject_posts_per_day_too_high():
    ok, reason, _ = _evaluate(ppd=45.0)
    assert not ok and reason == "posts_per_day_out_of_range"


# --- Р4.3.4
def test_reject_cv_too_low():
    ok, reason, checks = _evaluate(cv=0.14)
    assert not ok and reason == "cv_too_low"
    assert checks["cv_ok"] is False
    ok2, _, _ = _evaluate(cv=0.15)
    assert ok2 is True


def test_cv_not_checked_below_10_posts():
    """При < 10 постах CV не считается (ведь cv_interval=None) и не блокирует приём (Р4.3.4)."""
    ok, reason, checks = _evaluate(items=20, cv=None)
    assert ok and reason is None
    assert checks["cv_ok"] is True


# --- Р4.3.5
def test_reject_link_ratio_too_high():
    ok, reason, _ = _evaluate(link=0.71)
    assert not ok and reason == "link_ratio_too_high"
    ok2, _, _ = _evaluate(link=0.7)
    assert ok2 is True


def test_reject_dup_ratio_too_high():
    ok, reason, _ = _evaluate(dup=0.31)
    assert not ok and reason == "dup_ratio_too_high"
    ok2, _, _ = _evaluate(dup=0.3)
    assert ok2 is True


def test_reject_rt_ratio_too_high():
    ok, reason, _ = _evaluate(rt=0.81)
    assert not ok and reason == "rt_ratio_too_high"
    ok2, _, _ = _evaluate(rt=0.8)
    assert ok2 is True


def test_all_thresholds_pass():
    ok, reason, checks = _evaluate()
    assert ok and reason is None
    assert all(checks.values())


# --- признаки считаются по реальным постам
def test_compute_features_from_posts():
    posts = [
        {"tweet_id": "1", "published_at_utc": "2026-09-14T10:00:00", "links": [],
         "is_retweet": 0, "text": "a"},
        {"tweet_id": "2", "published_at_utc": "2026-09-14T12:00:00",
         "links": ["https://x.test"], "is_retweet": 1, "text": "b"},
        {"tweet_id": "3", "published_at_utc": "2026-09-14T20:00:00", "links": [],
         "is_retweet": 0, "text": "a"},
    ]
    f = registry.compute_features(posts, min_posts_for_cv=10)
    assert f["n"] == 3
    assert f["link_ratio"] == 1 / 3
    assert f["rt_ratio"] == 1 / 3
    assert abs(f["dup_ratio"] - 1 / 3) < 1e-9     # "a" дважды
    assert f["cv_interval"] is None               # < 10 постов -> не считаем
    assert f["posts_per_day"] is not None


# --- интеграция: verify_account пишет решения и признаки в БД
def _seed_core(con, handles=("core1", "core2")):
    for h in handles:
        con.execute("INSERT OR IGNORE INTO accounts (handle, tier, status, added_by)"
                    " VALUES (?, 'A', 'active', 'test')", (h,))
    con.commit()
    for h in handles:
        aid = con.execute("SELECT id FROM accounts WHERE handle=?", (h,)).fetchone()[0]
        con.execute(
            "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
            " mentions, text) VALUES (?,?,?,'rss',?,?)",
            (aid, f"core-{h}", "2026-09-01T00:00:00", '["cand"]',
             f"talking about @cand from {h}"))
    con.commit()


def test_verify_account_accepts_good_candidate(con, make_broker):
    _seed_core(con)
    b = make_broker()
    b._fake_transport.set_feed("cand", body=make_feed(20, handle="cand", jitter=1.0))
    registry.add_account(con, "cand", "C")
    res = registry.verify_account(con, b, "cand")
    assert res["ok"] is True, res
    assert res["mentioners"] >= 2
    row = con.execute("SELECT * FROM accounts WHERE handle='cand'").fetchone()
    assert row["status"] == "active"
    assert row["verified_at"] is not None
    assert row["cv_interval"] is not None and row["posts_per_day"] is not None
    cand = con.execute("SELECT * FROM candidates WHERE handle='cand'").fetchone()
    assert cand["validated"] == "ok" and cand["reject_reason"] is None


def test_verify_account_rejects_unreadable_candidate(con, make_broker):
    _seed_core(con)
    b = make_broker()
    b._fake_transport.set_feed("cand", body=make_feed(10, handle="cand", jitter=1.0))
    registry.add_account(con, "cand", "C")
    res = registry.verify_account(con, b, "cand")
    assert res["ok"] is False and res["reason"] == "items_lt_15"
    assert con.execute("SELECT status FROM accounts WHERE handle='cand'").fetchone()[0] \
        == "rejected"
    cand = con.execute("SELECT * FROM candidates WHERE handle='cand'").fetchone()
    assert cand["validated"] == "reject" and cand["reject_reason"] == "items_lt_15"


def test_verify_account_provisional_without_core_backing(con, make_broker):
    """Р2-БИС: при пустом ядре cooccurrence_ge_2 НЕ причина отказа.

    Кандидат, прошедший пять порогов без ядра, становится provisional (разрыв
    замкнутого круга). Раньше он отклонялся с cooccurrence_lt_2 — это и был дефект.
    """
    b = make_broker()
    b._fake_transport.set_feed("cand", body=make_feed(20, handle="cand", jitter=1.0))
    registry.add_account(con, "cand", "C")
    res = registry.verify_account(con, b, "cand")
    assert res["ok"] is True and res["status"] == "provisional"
    assert res["reason"] is None and res["mentioners"] == 0
    row = con.execute("SELECT * FROM accounts WHERE handle='cand'").fetchone()
    assert row["status"] == "provisional"
    assert row["ai_density"] is not None and row["ai_density_src"] == "heuristic"
    assert row["provisional_since"] is not None
    cand = con.execute("SELECT * FROM candidates WHERE handle='cand'").fetchone()
    assert cand["validated"] == "provisional" and cand["reject_reason"] is None


def test_daily_cap_keeps_out_of_active(con, make_broker, monkeypatch):
    """Р4.4: не более 50 новых active в сутки.

    Кандидат прошёл бы в active, но потолок исчерпан — он остаётся provisional
    (ТЗ-2: активный рост ограничен, но прошедший пороги не откатывается в rejected).
    """
    monkeypatch.setattr(config, "DAILY_NEW_ACTIVE_CAP", 0)
    _seed_core(con)
    b = make_broker()
    b._fake_transport.set_feed("cand", body=make_feed(20, handle="cand", jitter=1.0))
    registry.add_account(con, "cand", "C")
    res = registry.verify_account(con, b, "cand")
    assert res["daily_cap_hit"] is True and res["status"] == "provisional"
    assert con.execute("SELECT status FROM accounts WHERE handle='cand'").fetchone()[0] \
        == "provisional"


def test_tier_and_status_defaults(con):
    r = registry.add_account(con, "someone", None, "manual")
    assert r["ok"] and r["tier"] == config.DEFAULT_TIER
    row = con.execute("SELECT * FROM accounts WHERE handle='someone'").fetchone()
    assert row["status"] == "candidate" and row["tier"] == "C"
    r2 = registry.add_account(con, "SOMEONE", "A", "again")
    assert r2["created"] is False
    assert con.execute("SELECT COUNT(*) FROM accounts WHERE handle='someone'").fetchone()[0] == 1
