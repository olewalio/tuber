"""Р7.4 — схема: все объекты, идемпотентный init, WAL.

Прежняя версия теста проверяла legacy-таблицы ``tuber_x.db`` (``accounts``,
``posts``, …) через ``sqlite_master``. В монорепозитории таких ТАБЛИЦ нет:
хранение — единое ядро, а адаптер (``tuber.platforms.x.store``) отдаёт
legacy-форму ВРЕМЕННЫМИ ПРЕДСТАВЛЕНИЯМИ той же формы (тот же приём, что в
переносе YouTube, ТЗ-2). Поэтому утверждения ниже проверяют наблюдаемое
поведение адаптера: нужные объекты видны коду, колонки на месте, ограничения
(UNIQUE, каскадное удаление) работают, повторный ``init_db`` идемпотентен.

Единственное отличие по существу: UPSERT по представлению SQLite запрещает
(«cannot UPSERT a view»), поэтому ``cursors`` вставляет триггер представления —
``UNIQUE(kind, ref)`` соблюдается как «одна строка на пару», но повторный
плейн-INSERT не падает ``IntegrityError``, а обновляет строку (как и делает
боевой код сбора). См. TECH-DEBT D-25.

Сеть не используется.
"""
import sqlite3

from tuber.core import schema
from tuber.platforms.x import store as db

EXPECTED_TABLES = {"accounts", "posts", "cursors", "instances", "requests",
                   "candidates", "runs", "run_log", "metrics_daily"}


def _tables(con):
    """Legacy-объекты, видимые коду: TEMP-представления слоя совместимости."""
    return {name for name in db.compat_tables() if db.view_exists(con, name)}


def test_all_tables_created(db_path):
    con = db.connect(db_path)
    assert EXPECTED_TABLES <= _tables(con)
    con.close()


def test_wal_enabled(db_path):
    con = db.connect(db_path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    con.close()


def test_repeat_init_is_idempotent(db_path):
    con1 = db.init_db(db_path)
    con1.execute("INSERT INTO accounts (handle, tier, status) VALUES ('keeper','A','active')")
    con1.commit()
    con1.close()
    con2 = db.init_db(db_path)          # повторный init не должен падать
    con3 = db.init_db(db_path)
    assert EXPECTED_TABLES <= _tables(con3)
    assert con3.execute("SELECT COUNT(*) FROM accounts WHERE handle='keeper'").fetchone()[0] == 1
    con2.close()
    con3.close()


def test_accounts_columns(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
    for c in ("id", "handle", "x_id", "tier", "status", "lang", "topic_guess",
              "is_author", "ai_density", "cv_interval", "posts_per_day", "link_ratio",
              "rt_ratio", "dup_ratio", "first_mover_score", "posts_collected",
              "last_success_at", "last_attempt_at", "fail_streak", "last_error",
              "cursor", "added_at", "added_by", "source_type", "notes"):
        assert c in cols, f"нет колонки accounts.{c}"
    # UNIQUE(handle): хендл в реестре уникален в пределах платформы.
    con.execute("INSERT INTO accounts (handle) VALUES ('uniq')")
    con.commit()
    try:
        con.execute("INSERT INTO accounts (handle) VALUES ('uniq')")
        con.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "UNIQUE(handle) не работает"


def test_posts_columns_and_unique(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    for c in ("id", "account_id", "tweet_id", "published_at_utc", "published_src",
              "text", "text_hash", "lang", "links", "mentions", "hashtags",
              "is_retweet", "is_quote", "is_reply", "owner_handle", "orig_handle",
              "media_kind", "first_seen_at"):
        assert c in cols, f"нет колонки posts.{c}"
    con.execute("INSERT INTO accounts (handle) VALUES ('u')")
    con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc,"
                " published_src) VALUES (1,'1','2026-09-14T10:00:00','rss')")
    con.commit()
    try:
        con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc,"
                    " published_src) VALUES (1,'1','2026-09-14T10:00:00','rss')")
        con.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "UNIQUE(tweet_id) не работает"


def test_cursors_unique(con):
    """UNIQUE(kind, ref): одна строка на пару, повтор — обновление (см. шапку)."""
    con.execute("INSERT INTO cursors (kind, ref, cursor) VALUES ('account','a','c1')")
    con.commit()
    # UPSERT по представлению SQLite запрещает, поэтому повторная вставка того
    # же (kind, ref) обновляет строку силами триггера адаптера — ровно то, что
    # делает боевой код сбора и дискавери.
    con.execute("INSERT INTO cursors (kind, ref, cursor) VALUES ('account','a','c2')")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM cursors WHERE ref='a'").fetchone()[0] == 1
    assert con.execute("SELECT cursor FROM cursors WHERE ref='a'").fetchone()[0] == "c2"
    # другой kind с тем же ref — отдельная запись
    con.execute("INSERT INTO cursors (kind, ref, cursor) VALUES ('search','a','c9')")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM cursors WHERE ref='a'").fetchone()[0] == 2


def test_candidates_and_instances_columns(con):
    cc = {r[1] for r in con.execute("PRAGMA table_info(candidates)")}
    for c in ("handle", "found_via", "found_in_account", "seen_count",
              "distinct_sources", "first_seen_at", "last_seen_at", "validated",
              "reject_reason", "llm_checked"):
        assert c in cc
    ic = {r[1] for r in con.execute("PRAGMA table_info(instances)")}
    for c in ("host", "healthy", "rss_ok", "items_last_test", "last_check_at",
              "fail_streak", "cooldown_until", "requests_today", "day", "version",
              "last_error"):
        assert c in ic
    rc = {r[1] for r in con.execute("PRAGMA table_info(requests)")}
    for c in ("id", "host", "ts", "kind", "url", "status", "items", "latency_ms", "run_id"):
        assert c in rc
    mc = {r[1] for r in con.execute("PRAGMA table_info(metrics_daily)")}
    for c in ("day", "posts_ingested", "dup_rate", "coverage", "fail_rate",
              "latency_p95_min", "valid_date_ratio", "instances_alive"):
        assert c in mc


def test_posts_cascade_delete(con):
    con.execute("INSERT INTO accounts (id, handle) VALUES (1,'x')")
    con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc,"
                " published_src) VALUES (1,'123','2026-09-14T10:00:00','rss')")
    con.commit()
    con.execute("DELETE FROM accounts WHERE id=1")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
