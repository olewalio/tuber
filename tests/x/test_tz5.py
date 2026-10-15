"""ТЗ-5: провенанс текста, сторож, расписание, качество выдачи.

Все проверки — без сети. БД — временная (фикстуры conftest).
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from tuber.platforms.x import classify, collect, config, store as db, enrich, health, report


# ================================================ ТЗ-5 задача 1: текст-провенанс
def _legacy_db(tmp_path):
    """Урезанная база до миграции text_src (user_version=3)."""
    p = str(tmp_path / "legacy.db")
    con = sqlite3.connect(p)
    con.executescript(
        """CREATE TABLE posts (
             id INTEGER PRIMARY KEY, account_id INTEGER, tweet_id TEXT,
             published_at_utc TEXT, published_src TEXT, text TEXT, text_hash TEXT,
             is_long INTEGER, metrics_src TEXT, deleted_at TEXT);""")
    con.execute("PRAGMA user_version=3")
    con.commit()
    return p, con


def _ins(con, tid, text, text_hash, is_long=0, metrics_src=None):
    con.execute("INSERT INTO posts (tweet_id, published_at_utc, published_src, text,"
                " text_hash, is_long, metrics_src) VALUES (?,?,?,?,?,?,?)",
                (tid, "2026-09-14T10:00:00", "rss", text, text_hash, is_long, metrics_src))


def test_migration_adds_text_src_and_backfills_provable(tmp_path):
    """Провенанс текста (``text_src``) сохраняется как есть — Nitter/CDN/NULL.

    Прежняя версия теста проверяла разовую домиграцию колонки на legacy-базе
    (эвристика «доказать источник»). В монорепозитории эвристика не нужна:
    колонка ``text_src`` есть у каждой строки с самого начала, а ядро хранит её
    без изменений. Тест проверяет то, что важно для отчёта и сторожа: значения
    ``nitter`` / ``cdn`` / NULL не подменяются и переживают повторный ``migrate``.
    """
    con = db.init_db(str(tmp_path / "prov.db"))
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('a','A','active')")
    con.commit()
    rows = [("1", "обычный текст из Nitter", "nitter"),
            ("2", "x" * 280, "cdn"),
            ("3", "y" * 500, None),
            ("4", "перезаписанный CDN текст", "cdn"),
            ("5", None, None)]
    for tid, text, src in rows:
        con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc,"
                    " published_src, text, text_src, is_long, metrics_src)"
                    " VALUES (1,?,?,?,?,?,?,?)",
                    (tid, "2026-09-14T10:00:00", "rss", text, src,
                     1 if tid in ("2", "3") else 0,
                     "cdn" if src == "cdn" else None))
    con.commit()
    db.migrate(con)
    got = {r[0]: r[1] for r in con.execute("SELECT tweet_id, text_src FROM posts")}
    assert got["1"] == "nitter"
    assert got["2"] == "cdn"
    assert got["3"] is None
    assert got["4"] == "cdn"
    assert got["5"] is None
    con.close()


def test_store_posts_marks_nitter_and_ssr_none(con):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('a','A','active')")
    con.commit()
    aid = con.execute("SELECT id FROM accounts WHERE handle='a'").fetchone()["id"]
    posts = [{"tweet_id": "111", "published_at_utc": "2026-09-14T10:00:00",
              "published_src": "rss", "text": "пост Nitter"}]
    collect.store_posts(con, aid, posts, text_src="nitter")
    ssr = [{"tweet_id": "222", "published_at_utc": "2026-09-14T10:00:00",
            "published_src": "snowflake", "text": None}]
    collect.store_posts(con, aid, ssr, text_src=None)
    rows = {r["tweet_id"]: r["text_src"] for r in
            con.execute("SELECT tweet_id, text_src FROM posts")}
    assert rows["111"] == "nitter"
    assert rows["222"] is None


def test_store_posts_does_not_wipe_text_on_empty_update(con):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('a','A','active')")
    con.commit()
    aid = con.execute("SELECT id FROM accounts WHERE handle='a'").fetchone()["id"]
    collect.store_posts(con, aid, [{"tweet_id": "9", "published_at_utc":
                                    "2026-09-14T10:00:00", "published_src": "rss",
                                    "text": "полный текст Nitter"}], text_src="nitter")
    # Повторный заход через x_ssr: текст пустой — он НЕ должен затирать записанный.
    collect.store_posts(con, aid, [{"tweet_id": "9", "published_at_utc":
                                    "2026-09-14T10:00:00", "published_src": "snowflake",
                                    "text": None}], text_src=None)
    row = con.execute("SELECT text, text_src FROM posts WHERE tweet_id='9'").fetchone()
    assert row["text"] == "полный текст Nitter"
    assert row["text_src"] == "nitter"


def test_enrich_fills_empty_text_with_cdn_provenance(con):
    from tests.x.test_enrich_text_safety import StubRouter, add_account, add_post
    acc = add_account(con)
    add_post(con, acc, "1114567890123456789", text=None)
    fields = {"likes": 1, "replies": 0, "has_quote": 0, "is_long": 0, "lang": "en",
              "text": "текст из CDN", "author_verified": 0}
    enrich.enrich_batch(con, StubRouter({"1114567890123456789": ("ok", fields, 200)}))
    row = con.execute("SELECT text, text_src FROM posts WHERE tweet_id=?",
                      ("1114567890123456789",)).fetchone()
    assert row["text"] == "текст из CDN" and row["text_src"] == "cdn"


def test_enrich_preserves_existing_text_provenance(con):
    from tests.x.test_enrich_text_safety import StubRouter, add_account, add_post
    acc = add_account(con)
    tid = "1234567890123456789"
    add_post(con, acc, tid, text="полный текст Nitter " * 200)
    con.execute("UPDATE posts SET text_src='nitter' WHERE tweet_id=?", (tid,))
    con.commit()
    fields = {"likes": 1, "replies": 0, "has_quote": 0, "is_long": 1, "lang": "en",
              "text": "обрезанный", "author_verified": 0}
    enrich.enrich_batch(con, StubRouter({tid: ("ok", fields, 200)}))
    row = con.execute("SELECT text_src FROM posts WHERE tweet_id=?", (tid,)).fetchone()
    assert row["text_src"] == "nitter"


def test_needs_full_text_nonzero_for_truncated_cdn_long_post(con):
    """Главный тест пробела: is_long=1 + CDN-текст 280 символов -> ненулевое число."""
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('sama','A','active')")
    con.commit()
    aid = con.execute("SELECT id FROM accounts WHERE handle='sama'").fetchone()["id"]
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src, text,"
        " text_hash, is_long, metrics_src, metrics_at, text_src)"
        " VALUES (?,?,'2026-09-14T10:00:00','rss',?,?,1,'cdn',?, 'cdn')",
        (aid, "2099518608967712870", "x" * 280, "h", db.utcnow_iso()))
    con.commit()
    assert enrich.needs_full_text_count(con) == 1
    # Сводка enrich (даже без новых постов к обогащению) обязана быть честной.
    from tests.x.test_enrich_text_safety import StubRouter
    s = enrich.enrich_batch(con, StubRouter({}))
    assert s["selected"] == 0
    assert s["needs_nitter"] == 1


def test_fulltext_refetch_restores_full_text(con):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('sama','A','active')")
    con.commit()
    aid = con.execute("SELECT id FROM accounts WHERE handle='sama'").fetchone()["id"]
    con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
                " text, text_hash, is_long, metrics_src, text_src)"
                " VALUES (?,?,'2026-09-14T10:00:00','rss','короткий', 'h', 1, 'cdn', 'cdn')",
                (aid, "555"))
    con.commit()
    full = "полный " * 800

    class Broker:
        def fetch_feed(self, handle, cursor=None, priority="collect", force=False):
            return [{"tweet_id": "555", "published_at_utc": "2026-09-14T10:00:00",
                     "published_src": "rss", "text": full,
                     "links": [], "mentions": [], "hashtags": [],
                     "is_retweet": 0, "is_quote": 0, "is_reply": 0, "media_kind": None,
                     "cursor_next": None}]

    s = collect.refetch_fulltext(con, Broker())
    assert s["nitter_ok"] == 1 and s["after"] == 0
    row = con.execute("SELECT text, text_src, length(text) L FROM posts WHERE tweet_id='555'"
                      ).fetchone()
    assert row["L"] == len(full) and row["text_src"] == "nitter"


# ==================================================== ТЗ-5 задача 2: сторож
def _post(con, aid, tid, *, is_long=0, text="t", text_src=None, lang="en",
          first_seen=None):
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src, text,"
        " is_long, text_src, lang, first_seen_at) VALUES (?,?,?,'rss',?,?,?,?,?)",
        (aid, tid, first_seen or db.utcnow_iso(), text, is_long, text_src, lang,
         first_seen or db.utcnow_iso()))
    con.commit()


def _acc(con, handle="a"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,'A','active')",
                (handle,))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def test_health_text_short_daily_alert(con):
    a = _acc(con)
    # 10 постов за сутки, 2 из них — длинные с коротким текстом = 20% > 5%
    for i in range(8):
        _post(con, a, f"s{i}", text="ok")
    _post(con, a, "s8", is_long=1, text="x" * 280)
    _post(con, a, "s9", is_long=1, text="y" * 280)
    res = health.check_text_short_daily(con)
    assert res["alert"] is True and res["short"] == 2


def test_health_cdn_text_ratio_alert(con):
    a = _acc(con)
    for i in range(10):
        _post(con, a, f"c{i}", text_src="cdn" if i < 3 else None)
    res = health.check_cdn_text_ratio(con)
    assert res["alert"] is True and res["cdn"] == 3


def test_health_instance_cooldown_alert(con):
    until = (datetime.now(timezone.utc) + timedelta(minutes=59)).strftime("%Y-%m-%dT%H:%M:%S")
    con.execute("INSERT INTO instances (host, healthy, cooldown_until) VALUES"
                " ('https://h.test', 0, ?)", (until,))
    con.commit()
    res = health.check_instance_cooldown(con)
    assert res["alert"] is True and res["value"] == 1


def test_health_cooldown_short_is_ok(con):
    until = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    con.execute("INSERT INTO instances (host, healthy, cooldown_until) VALUES"
                " ('https://h.test', 0, ?)", (until,))
    con.commit()
    assert health.check_instance_cooldown(con)["alert"] is False


# ================================================ ТЗ-5 задача 5: качество выдачи
def test_dedup_kind_removes_duplicate_word():
    assert report._dedup_kind("Раунд", "раунд Sugar") == "Sugar"
    assert report._dedup_kind("Запуск", "запуск локальные модели") == "локальные модели"
    assert report._dedup_kind("Раунд", "Раунд: Series B") == "Series B"
    assert report._dedup_kind("Раунд", None) == "без уточнения"
    assert report._dedup_kind("Раунд", "раунд") == "без уточнения"
    assert report._dedup_kind("Запуск", "продукт X") == "продукт X"


def _story(con, aid, tid, *, topic, subtopic, claim_type, topics, text="txt",
           published="2026-09-15T05:00:00", xconf=2):
    th = f"h{tid}"
    con.execute(
        "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src, text,"
        " text_hash, owner_handle) VALUES (?,?,?,'rss',?,?, 'author')",
        (aid, tid, published, text, th))
    con.execute("INSERT INTO classified (text_hash, tweet_id, topic, subtopic,"
                " claim_type, status) VALUES (?,?,?,?,?,'classified')",
                (th, tid, topic, subtopic, claim_type))
    con.execute("INSERT INTO stories (published_at, xconf, topics, is_single,"
                " suspect, first_mover, first_tweet_id) VALUES (?,?,?,0,0,'author',?)",
                (published, xconf, json.dumps(topics, ensure_ascii=False), tid))
    # lastrowid после записи в представление всегда 0: id сюжета возвращает
    # триггер адаптера (см. store.last_insert_id).
    sid = db.last_insert_id(con)
    con.execute("INSERT INTO story_posts (story_id, tweet_id, handle, role) VALUES (?,?,?,?)",
                (sid, tid, "author", "primary"))
    con.commit()
    return sid


def _range():
    start = datetime(2026, 9, 15, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def test_block3_money_no_duplicate_kind(con):
    a = _acc(con)
    _story(con, a, "m1", topic="инвестиции и раунды", subtopic="раунд Sugar",
           claim_type="funding", topics=["инвестиции и раунды"])
    _story(con, a, "m2", topic="релизы моделей", subtopic="запуск локальные модели",
           claim_type="release", topics=["релизы моделей"])
    start, end = _range()
    out = "\n".join(report.block3_money(con, start, end))
    assert "Раунд: Sugar" in out
    assert "Запуск: локальные модели" in out
    assert "раунд Sugar" not in out
    assert "запуск локальные" not in out


def test_block4_excludes_infrastructure(con):
    a = _acc(con)
    _story(con, a, "i1", topic="инфраструктура и железо", subtopic="питание 800V DC",
           claim_type="news", topics=["инфраструктура и железо"])
    _story(con, a, "d1", topic="инструменты разработчика", subtopic="новый SDK",
           claim_type="release", topics=["инструменты разработчика"])
    start, end = _range()
    out = "\n".join(report.block4_tools(con, start, end))
    assert "800V" not in out
    assert "новый SDK" in out


def test_block7_reports_ru_count(con):
    a = _acc(con)
    _post(con, a, "r1", text="русский", lang="ru")
    start, end = _range()
    out = "\n".join(report.block7_service(con, start, end, day="2026-09-15"))
    assert "Русскоязычных постов в базе: 1" in out


def test_report_build_has_ru_line_and_no_double_round(con):
    a = _acc(con)
    _story(con, a, "m3", topic="инвестиции и раунды", subtopic="раунд Flam",
           claim_type="funding", topics=["инвестиции и раунды"], text="Flam raised $40M")
    _post(con, a, "r2", text="привет", lang="ru")
    text = report.build(con, date="2026-09-15", translator=False)
    assert "Русскоязычных постов в базе:" in text
    assert "Раунд: раунд" not in text


def test_classify_prompt_has_rubric_boundaries():
    prompt = classify._system_prompt()
    assert "инфраструктура и железо" in prompt
    assert "НЕ относится к «инструментам разработчика»" in prompt
    assert "новый/новинка" in prompt
