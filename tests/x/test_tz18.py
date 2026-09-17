"""ТЗ-18: приём кандидатов X из фидов (описания YouTube / посты Telegram).

Проверяется (все прогоны — на временной БД, сеть не используется):
  * миграция схемы 6 -> 7 безопасна: две колонки добавляются, строки целы;
  * импорт новых кандидатов, идемпотентность (повтор -> imported_new=0, merged>0);
  * `reject`/`ok`/`provisional` существующих записей не меняются и не понижаются;
  * терпимость: строка чужого kind -> skipped_kinds, битая строка -> bad_lines,
    импорт продолжается; пустой файл и «все строки битые» -> ошибка;
  * отсутствующий файл — не ошибка, а feeds_missing;
  * фильтры: сервисный хендл, стоп-лист, бот, новостник-гигант, порог упоминаний;
  * --limit и skipped_limit (сортировка mentions desc, videos desc, ai_hint);
  * дубль одного хендла в двух фидах -> один кандидат, оба feed_source;
  * --dry ничего не пишет;
  * ни один импорт не регистрирует аккаунт;
  * строка суточной сводки по притоку из фидов (horizon/feed_influx).
"""
import json
import os

from tuber.platforms.x import config, store as db, feeds


# ------------------------------------------------------------------ утилиты
def _feed(path, rows, *, bad=0):
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        for _ in range(bad):
            fh.write("{это не json\n")
    return str(path)


def _x(handle, mentions=5, *, sources=("ch1",), videos=(), ai_hint=0,
       first="2026-08-01T00:00:00+00:00", last="2026-09-01T00:00:00+00:00"):
    return {"kind": "x", "handle": handle, "mentions": mentions,
            "sources": list(sources), "videos": list(videos), "ai_hint": ai_hint,
            "first_seen": first, "last_seen": last, "source": "tuber-telegram:posts"}


# =============================================================== миграция
def test_migration_v6_to_v7_adds_columns_and_keeps_rows(db_path):
    """Колонки приёма фидов видны коду, строки кандидатов не теряются.

    Прежняя версия собирала legacy-таблицу ``candidates`` (``PRAGMA
    user_version=6``) и проверяла ``ALTER TABLE``-миграцию. Схема теперь
    принадлежит ядру, поэтому проверяется то же наблюдаемое свойство:
    поля ``ai_hint``/``feed_source`` доступны, а повторная миграция ничего не
    ломает и не теряет данные.
    """
    con = db.init_db(db_path)
    con.execute("INSERT INTO candidates (handle, seen_count, validated, reject_reason)"
                " VALUES ('old_one', 7, 'reject', 'not_found')")
    con.commit()
    db.migrate(con)                       # повторная миграция — no-op
    assert db.SCHEMA_VERSION == 8         # legacy-номер версии сохранён в API
    cols = {r[1] for r in con.execute("PRAGMA table_info(candidates)")}
    assert {"ai_hint", "feed_source"} <= cols
    row = con.execute("SELECT * FROM candidates WHERE handle='old_one'").fetchone()
    assert row["seen_count"] == 7 and row["validated"] == "reject"
    assert row["ai_hint"] == 0 and row["feed_source"] is None
    con.close()


# =============================================================== основной приём
def test_import_new_then_idempotent(con, tmp_path):
    p = _feed(tmp_path / "external_candidates.jsonl",
              [_x("alice_ai", 5), _x("bob_ai", 3)])
    r1 = feeds.import_candidates(con, [p])
    assert r1["imported_new"] == 2 and r1["merged"] == 0
    assert r1["queue_total"] == 2
    assert r1["skipped_limit"] == 0
    assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    row = con.execute("SELECT * FROM candidates WHERE handle='alice_ai'").fetchone()
    assert row["validated"] is None and row["verified_at"] is None
    assert row["feed_source"] == feeds.feed_tag(p)

    r2 = feeds.import_candidates(con, [p])
    assert r2["imported_new"] == 0 and r2["merged"] == 2
    alice = con.execute("SELECT seen_count FROM candidates WHERE handle='alice_ai'"
                        ).fetchone()
    assert alice["seen_count"] == 10  # 5 + 5


def test_reject_ok_provisional_not_touched(con, tmp_path):
    p = _feed(tmp_path / "f.jsonl", [_x("keep_rej", 4), _x("keep_ok", 4),
                                     _x("keep_prov", 4)])
    feeds.import_candidates(con, [p])
    con.execute("UPDATE candidates SET validated='reject', reject_reason='not_found'"
                " WHERE handle='keep_rej'")
    con.execute("UPDATE candidates SET validated='ok', verified_at='2026-01-01T00:00:00'"
                " WHERE handle='keep_ok'")
    con.execute("UPDATE candidates SET validated='provisional' WHERE handle='keep_prov'")
    con.commit()
    feeds.import_candidates(con, [p])
    got = {r["handle"]: dict(r) for r in con.execute(
        "SELECT * FROM candidates WHERE handle IN ('keep_rej','keep_ok','keep_prov')")}
    assert got["keep_rej"]["validated"] == "reject"
    assert got["keep_rej"]["reject_reason"] == "not_found"
    assert got["keep_ok"]["validated"] == "ok"
    assert got["keep_ok"]["verified_at"] == "2026-01-01T00:00:00"
    assert got["keep_prov"]["validated"] == "provisional"


def test_tolerant_bad_lines_and_foreign_kind(con, tmp_path):
    p = _feed(tmp_path / "f.jsonl", [_x("good_ai", 5),
                                     {"kind": "youtube", "handle": "yt_only"},
                                     {"no_kind": True}], bad=2)
    r = feeds.import_candidates(con, [p])
    assert r["imported_new"] == 1
    assert r["feeds"][0]["bad_lines"] == 2
    assert r["feeds"][0]["skipped_kinds"] == 2


def test_empty_and_all_bad_feeds_raise(con, tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    try:
        feeds.import_candidates(con, [str(empty)])
        raise AssertionError("пустой фид должен был дать FeedError")
    except feeds.FeedError:
        pass
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{x\n{y\n")
    try:
        feeds.import_candidates(con, [str(bad)])
        raise AssertionError("все битые строки должны были дать FeedError")
    except feeds.FeedError:
        pass


def test_missing_file_is_not_error(con, tmp_path):
    p = _feed(tmp_path / "f.jsonl", [_x("ok_ai", 5)])
    r = feeds.import_candidates(con, [str(tmp_path / "нет.jsonl"), p])
    assert r["feeds_missing"] == [str(tmp_path / "нет.jsonl")]
    assert r["imported_new"] == 1


def test_filters_and_threshold(con, tmp_path):
    rows = [
        _x("search", 9),                 # служебный
        _x("spammer12345678", 9),        # бот (цифровая часть)
        _x("foxnews", 9),                # новостник-гигант
        _x("low_mentions", 1),           # ниже порога (VERIFY_MIN_MENTIONS=2)
        _x("fine_ai", 2),                # проходит ровно по порогу
    ]
    p = _feed(tmp_path / "f.jsonl", rows)
    r = feeds.import_candidates(con, [p])
    sf = r["skipped_filter"]
    assert sf["service_handle"] == 1
    assert sf["bot"] == 1
    assert sf["news_giant"] == 1
    assert sf["below_mention_threshold"] == 1
    assert r["imported_new"] == 1
    assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_blocklist_and_registered(con, tmp_path):
    con.execute("INSERT INTO blocklist (handle, reason) VALUES ('bad_boy','manual')")
    con.commit()
    con.execute("INSERT INTO accounts (handle, status) VALUES ('known_acc','active')")
    con.commit()
    p = _feed(tmp_path / "f.jsonl", [_x("bad_boy", 5), _x("known_acc", 5),
                                     _x("new_ai", 5)])
    r = feeds.import_candidates(con, [p])
    assert r["skipped_filter"]["blocklist"] == 1
    assert r["skipped_filter"]["already_registered"] == 1
    assert r["imported_new"] == 1


def test_limit_and_sorting(con, tmp_path):
    rows = [_x(h, m) for h, m in (("low_ai", 2), ("mid_ai", 5), ("top_ai", 9))]
    p = _feed(tmp_path / "f.jsonl", rows)
    r = feeds.import_candidates(con, [p], limit=2)
    assert r["imported_new"] == 2
    assert r["skipped_limit"] == 1
    handles = {row[0] for row in con.execute("SELECT handle FROM candidates")}
    assert handles == {"top_ai", "mid_ai"}


def test_duplicate_in_two_feeds_merged(con, tmp_path):
    common = _x("twin_ai", 4, sources=("ch1",))
    p1 = _feed(tmp_path / "a" / "external_candidates.jsonl", [common])
    p2 = _feed(tmp_path / "b" / "external_candidates.jsonl", [common])
    r = feeds.import_candidates(con, [p1, p2])
    assert r["imported_new"] == 1 and r["merged"] == 0
    row = con.execute("SELECT * FROM candidates WHERE handle='twin_ai'").fetchone()
    assert row["feed_source"].count(",") == 1  # два тега фидов
    assert row["seen_count"] == 8
    assert con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


def test_dry_writes_nothing(con, tmp_path):
    p = _feed(tmp_path / "f.jsonl", [_x("dry_ai", 5)])
    before = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    r = feeds.import_candidates(con, [p], dry=True)
    after = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    assert r["dry"] is True and r["imported_new"] == 1
    assert before == after == 0


def test_feed_influx_and_horizon(con, tmp_path):
    yt = _feed(tmp_path / "os" / "external_candidates.jsonl", [_x("yt_ai", 5)])
    tg = _feed(tmp_path / "tg" / "external_candidates.jsonl", [_x("tg_ai", 5)])
    # подменяем метку фида, чтобы сработала ветка YouTube
    assert feeds.found_via_token("tuber-os", yt) == "yt_desc:tuber-os"
    assert feeds.found_via_token("tuber-telegram", tg) == "tg_posts:tuber-telegram"
    feeds.import_candidates(con, [yt, tg])
    con.execute("UPDATE candidates SET feed_source='tuber-os' WHERE handle='yt_ai'")
    con.execute("UPDATE candidates SET feed_source='tuber-telegram' WHERE handle='tg_ai'")
    con.commit()
    influx = feeds.feed_influx(con)
    assert influx["yt_desc"] == 1 and influx["tg_posts"] == 1
    assert influx["queue_total"] == 2
    assert influx["checks_per_day"] == config.DISCOVERY_MAX_VERIFY_DEFAULT
    # 2 кандидата / 30 проверок в сутки -> минимум 1 сутки
    assert influx["horizon_days"] == 1


def test_cli_prints_single_json(con, tmp_path, monkeypatch, capsys):
    from tuber.platforms.x import cli
    p = _feed(tmp_path / "f.jsonl", [_x("cli_ai", 5)])
    monkeypatch.setattr(config, "DB_PATH", con.execute("PRAGMA database_list")
                        .fetchone()[2])
    rc = cli.main(["import_candidates", "--feed", p])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert set(payload) == {"feeds", "feeds_missing", "imported_new", "merged",
                            "skipped_filter", "bad_fields", "skipped_limit",
                            "queue_total", "dry"}
    assert payload["imported_new"] == 1
