"""D-28: пакетный разбор авторов (`scoring.spread_authors_many`) без N+1.

Долг D-28 — `spread_authors` делал отдельный SQL-запрос на каждый пост.
Проверяем: (1) структурное совпадение с одиночным путём, (2) число запросов не
растёт линейно с числом постов.
"""
import json

from tuber.platforms.x import config, scoring


def _acc(con, handle, tier="A"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,?,'active')",
                (handle, tier))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def _post(con, acc, tid, published="2026-09-14T10:00:00", mentions=None,
          links=None, owner="a", verified=0):
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
        " text, mentions, links, is_retweet, pinned, metrics_src, metrics_at, likes,"
        " replies, owner_handle, author_verified, is_long)"
        " VALUES (?,?,?,'rss','x',?,?,0,0,'cdn',?,0,0,?,?,0)",
        (acc, str(tid), published, json.dumps(mentions or []),
         json.dumps(links or []), "2026-09-14T10:00:00", owner, verified))
    con.commit()


def _count_queries(con, fn):
    seen = []
    con.set_trace_callback(lambda sql: seen.append(sql))
    try:
        result = fn()
    finally:
        con.set_trace_callback(None)
    return len(seen), result


def test_spread_authors_many_matches_single_path(con):
    """Пакетный результат == одиночный `spread_authors` для каждого поста."""
    author = _acc(con, "author")
    for i, echo in enumerate(("echo1", "echo2", "echo3")):
        e = _acc(con, echo)
        _post(con, author, 1000 + i, owner="author")
        _post(con, e, 2000 + i, mentions=["author"], owner=echo,
              verified=1 if i == 0 else 0)
    posts = list(con.execute("SELECT * FROM posts WHERE tweet_id < 2000"
                             " ORDER BY tweet_id"))
    batch = scoring.spread_authors_many(con, posts)
    for post, res in zip(posts, batch):
        # сверка с сохранённым историческим одиночным телом
        assert res == scoring.spread_authors_one(con, post), post["tweet_id"]
    # у каждого корневого поста в окне все три эха
    assert batch[0][0] == {"echo1", "echo2", "echo3"}


def test_spread_authors_many_structure_and_weight(con):
    author = _acc(con, "author")
    echo = _acc(con, "echo")
    other = _acc(con, "other")
    _post(con, author, 3000, owner="author")
    _post(con, echo, 3001, mentions=["author"], owner="echo", verified=1)
    _post(con, other, 3002, mentions=["someone-else"], owner="other")
    posts = [con.execute("SELECT * FROM posts WHERE tweet_id='3000'").fetchone()]
    (authors, weights), = scoring.spread_authors_many(con, posts)
    assert authors == {"echo"}
    expected = round(config.SPREAD_AUTHOR_WEIGHT * (1 + config.SPREAD_VERIFIED_BONUS), 6)
    assert weights == [("echo", expected)]
    assert scoring.score_spread(weights) == expected


def test_spread_authors_many_query_count_constant(con):
    """Число SQL-запросов НЕ растёт с числом постов (граница D-28 — N+1)."""
    author = _acc(con, "author")
    echo = _acc(con, "echo")
    _post(con, echo, 9000, mentions=["author"], owner="echo")

    def make(base, n):
        posts = []
        for i in range(n):
            tid = base + i
            _post(con, author, tid, owner="author")
            posts.append(con.execute(
                "SELECT * FROM posts WHERE tweet_id=?", (str(tid),)).fetchone())
        return posts

    p2 = make(4000, 2)
    p10 = make(5000, 8)
    q2, _ = _count_queries(con, lambda: scoring.spread_authors_many(con, p2))
    q10, _ = _count_queries(con, lambda: scoring.spread_authors_many(con, p10))
    assert q2 <= 3 and q10 <= 3, (q2, q10)
    assert q10 == q2  # константа, а не рост с числом постов
