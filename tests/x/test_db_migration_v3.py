"""ТЗ-4 Р3: состав legacy-колонок и идемпотентность миграции.

Прежняя версия теста проверяла ``ALTER TABLE``-миграции legacy-схемы
``tuber_x`` (``PRAGMA user_version`` 2 → 8 и появление колонок). В
монорепозитории схема принадлежит ядру, поэтому проверяются те же
НАБЛЮДАЕМЫЕ свойства, но по своему источнику:

* все legacy-колонки (ТЗ-4: метрики вовлечённости; ТЗ-10: здоровье инстансов)
  видны коду через представления адаптера;
* ``UNIQUE(tweet_id, taken_at)`` истории метрик соблюдается (в ядре —
  ``UNIQUE(content_id, captured_at)`` у ``metric_snapshot``);
* ``db.migrate`` идемпотентен: повторный прогон ничего не меняет и не теряет
  данные, а схема ядра после него полна (``schema.verify_schema`` пуст).

Сеть не используется.
"""
import sqlite3

from tuber.core import schema
from tuber.platforms.x import store as db


def test_posts_tz4_columns(db_path):
    con = db.connect(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    for c in ("likes", "replies", "has_quote", "is_long", "metrics_at",
              "metrics_src", "pinned", "retweet_count", "author_verified",
              "spread_src", "deleted_at"):
        assert c in cols, f"нет колонки posts.{c}"
    con.close()


def test_metrics_daily_tz4_columns(db_path):
    con = db.connect(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(metrics_daily)")}
    for c in ("likes_median", "enriched_ratio", "cdn_429_count", "synd_429_count",
              "ssr_used", "stale_lag_p95_min"):
        assert c in cols, f"нет колонки metrics_daily.{c}"
    con.close()


def test_post_metrics_history_table_and_index(db_path):
    con = db.connect(db_path)
    assert db.view_exists(con, "post_metrics_history")
    # Ядро хранит историю в metric_snapshot с UNIQUE(content_id, captured_at);
    # повторный замер того же поста в ту же секунду — ошибка целостности.
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('a','A','active')")
    con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src)"
                " VALUES (1,'1','2026-09-15T09:00:00','rss')")
    con.commit()
    con.execute("INSERT INTO post_metrics_history (tweet_id, taken_at, likes)"
                " VALUES ('1','2026-09-15T10:00:00',5)")
    con.commit()
    try:
        con.execute("INSERT INTO post_metrics_history (tweet_id, taken_at, likes)"
                    " VALUES ('1','2026-09-15T10:00:00',6)")
        con.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised
    con.close()


def test_migrate_from_v2_adds_columns(tmp_path):
    """``db.migrate`` приводит базу ядра к текущей схеме и идемпотентен.

    Прежний смысл теста — «старая база домигрируется и получает новые колонки».
    Теперь источник схемы один (ядро), поэтому проверяем эквивалент: пустая база
    после ``migrate`` полна, а повторный прогон ничего не ломает.
    """
    p = str(tmp_path / "old.db")
    con = db.init_db(p)
    assert schema.verify_schema(con) == []
    posts_cols = {r[1] for r in con.execute("PRAGMA table_info(content)")}
    assert {"external_id", "author_handle", "deleted_at", "is_repost"} <= posts_cols
    mcols = {r[1] for r in con.execute("PRAGMA main.table_info(metrics_daily)")}
    assert "likes_median" in mcols and "items_ingested" in mcols
    db.migrate(con)                      # повторный прогон — no-op
    assert schema.verify_schema(con) == []
    assert db.SCHEMA_VERSION >= 3        # legacy-номер версии сохранён в API
    con.close()


def test_repeat_init_keeps_data_with_tz4(db_path):
    con = db.init_db(db_path)
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('k','A','active')")
    con.commit()
    con.close()
    con2 = db.init_db(db_path)
    assert con2.execute("SELECT COUNT(*) FROM accounts WHERE handle='k'").fetchone()[0] == 1
    con2.close()
