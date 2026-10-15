"""ТЗ-4 Р4: скоринг значимости, исключения ретвитов/закреплённых, веса."""
from tuber.platforms.x import config, store as db, scoring


class Row(dict):
    def keys(self):
        return list(super().keys())


def _acc(con, handle, tier="A"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,?,'active')",
                (handle, tier))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def _post(con, acc, tid, published="2026-09-14T10:00:00", likes=100, replies=5,
          mentions=None, links=None, is_retweet=0, pinned=0, metrics_src="cdn",
          owner="a", verified=0, is_long=0, text="x"):
    import json
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
        " text, mentions, links, is_retweet, pinned, metrics_src, metrics_at, likes,"
        " replies, owner_handle, author_verified, is_long)"
        " VALUES (?,?,?,'rss',?,?,?,?,?,?,?,?,?,?,?,?)",
        (acc, str(tid), published, text, json.dumps(mentions or []),
         json.dumps(links or []), is_retweet, pinned, metrics_src,
         db.utcnow_iso(), likes, replies, owner, verified, is_long))
    con.commit()


def _hist(con, tid, age, likes, replies=1):
    con.execute("INSERT INTO post_metrics_history (tweet_id, taken_at, age_hours,"
                " likes, replies, src) VALUES (?,?,?,?,?,'cdn')",
                (str(tid), db.utcnow_iso(), age, likes, replies))
    con.commit()


def test_velocity_exact_at_6h(con):
    a = _acc(con, "a")
    _post(con, a, "111", likes=600, replies=10)
    _hist(con, "111", 6.0, 600, 10)
    vel, likes6, replies6, est = scoring.velocity_6h(con, "111", "2026-09-14T10:00:00")
    assert vel == 100.0 and likes6 == 600 and est is False
    assert scoring.score_engage(vel, replies6) > 0


def test_velocity_estimated_when_young(con):
    a = _acc(con, "a")
    _post(con, a, "222", likes=30)
    _hist(con, "222", 1.0, 30, 1)
    vel, likes6, _r, est = scoring.velocity_6h(con, "222", "2026-09-14T10:00:00")
    assert est is True
    assert likes6 == 180  # 30 лайков за 1 ч -> 180 за 6 ч (линейная переоценка)


def test_rank_excludes_retweet_pinned_and_cdn_rt(con):
    a = _acc(con, "a")
    _post(con, a, "100", likes=500, owner="a")
    _post(con, a, "200", likes=900, is_retweet=1, owner="a")
    _post(con, a, "300", likes=900, pinned=1, owner="a")
    _post(con, a, "400", likes=900, metrics_src="cdn_rt", owner="a")
    ids = {r["tweet_id"] for r in scoring.rank(con)}
    assert ids == {"100"}


def test_spread_graph_and_verified_bonus(con):
    a = _acc(con, "author")
    b = _acc(con, "echo")
    _post(con, a, "500", mentions=[], owner="author")
    _post(con, b, "501", mentions=["author"], owner="echo", verified=1)
    row = con.execute("SELECT * FROM posts WHERE tweet_id='500'").fetchone()
    authors, weights = scoring.spread_authors(con, row)
    assert authors == {"echo"}
    expected = config.SPREAD_AUTHOR_WEIGHT * (1 + config.SPREAD_VERIFIED_BONUS)
    assert weights[0][1] == round(expected, 6)
    assert scoring.score_spread(weights) == round(expected, 6)


def test_spread_via_link_and_quote(con):
    a = _acc(con, "author")
    b = _acc(con, "quoter")
    _post(con, a, "600", owner="author")
    _post(con, b, "601", links=["https://x.com/author/status/600"], owner="quoter")
    row = con.execute("SELECT * FROM posts WHERE tweet_id='600'").fetchone()
    authors, _w = scoring.spread_authors(con, row)
    assert authors == {"quoter"}


def test_score_first_decreasing_and_missing(con):
    assert scoring.score_first(None) == 0.0
    assert scoring.score_first(0) == 1.0
    assert scoring.score_first(60) > scoring.score_first(600)


def test_lead_time_and_score_combine(con):
    a = _acc(con, "first")
    b = _acc(con, "second")
    _post(con, a, "700", published="2026-09-14T10:00:00", owner="first",
          links=["https://example.com/x"])
    _post(con, b, "701", published="2026-09-14T10:30:00", owner="second",
          links=["https://example.com/x"])
    row = con.execute("SELECT * FROM posts WHERE tweet_id='700'").fetchone()
    assert scoring.lead_time_min(con, row) == 30.0
    res = scoring.score_post(con, row)
    assert res["score_first"] == round(1 / (1 + 30 / 60), 6)
    assert res["score"] > 0


def test_synd_retweet_count_used_with_src_mark(con):
    a = _acc(con, "a")
    _post(con, a, "800", owner="a", metrics_src="synd")
    con.execute("UPDATE posts SET spread_src='synd', retweet_count=40 WHERE tweet_id='800'")
    con.commit()
    row = con.execute("SELECT * FROM posts WHERE tweet_id='800'").fetchone()
    res = scoring.score_post(con, row)
    assert res["spread_src"] == "synd"
    assert res["score_spread"] == round(config.SPREAD_AUTHOR_WEIGHT * 40, 6)


def test_coverage(con):
    a = _acc(con, "a")
    _post(con, a, "900", likes=10, owner="a")
    _post(con, a, "901", likes=20, owner="a", metrics_src=None)
    con.execute("UPDATE posts SET metrics_at=NULL, likes=NULL WHERE tweet_id='901'")
    con.commit()
    cov = scoring.coverage(con)
    assert cov["total"] == 2 and cov["enriched"] == 1 and cov["ratio"] == 0.5
