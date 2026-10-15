"""Тесты моста источников Telegram (перенос `scripts/test_bridge_sources.py`, ТЗ-4).

Сеть не используется. CLI вызывается подпроцессом на временной базе.
Канонический обмен единой базы — таблица ``candidate`` (ТЗ-4 §0), поэтому
приёмники кандидатов (импорт из фида и дискавери) проверяются по ней; реестр
(``channels`` → ``source``) остаётся нетронутым.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from tuber.platforms.telegram import bridge as bc
from tuber.platforms.telegram import store as db

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FEED = os.path.join(FIX, "tz17_feed.jsonl")
CONFIG = os.path.join(ROOT, "config", "telegram", "bridge_sources.json")
PY = sys.executable

#: Путь к временной базе; выставляется автофикстурой ниже (по одному на тест).
BRIDGE_DB = ""


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    global BRIDGE_DB
    BRIDGE_DB = str(tmp_path / "bridge.db")
    db.init_db(BRIDGE_DB).close()
    yield BRIDGE_DB


def new_db(active_handles=()):
    """Свежая временная база + соединение; реестр наполняется при необходимости."""
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(BRIDGE_DB + suf)
        except OSError:
            pass
    con = db.init_db(BRIDGE_DB)
    for h in active_handles:
        con.execute("INSERT INTO channels(handle, status, source) VALUES (?, 'active', 'test')",
                    (h,))
    con.commit()
    return BRIDGE_DB, con


def run_cli(*args):
    return subprocess.run(
        [PY, "-m", "tuber", "tg", *args],
        capture_output=True, text=True, timeout=180, cwd=ROOT,
    )


def candidates(con) -> dict:
    rows = {}
    for r in con.execute("SELECT handle, status, found_via, meta_json FROM candidate"):
        rows[r[0]] = (r[1], r[2], r[3] or "")
    return rows


# ---------------------------------------------------------------------------
# 1. Базовый импорт из фида
# ---------------------------------------------------------------------------
def test_import_basic():
    path, con = new_db(active_handles=["existing_active"])
    con.close()
    r = run_cli("bridge", "import", "--feed", FEED, "--db", path)
    assert r.returncode == 0, f"exit={r.returncode} stderr={r.stderr}"
    d = json.loads(r.stdout)
    assert d["imported"] == 3, d
    assert d["skipped_existing"] == 1, d
    assert d["skipped_filter"] == 5, d
    assert d["skipped_limit"] == 0, d
    assert d["dry"] is False, d
    assert d["feed"] == FEED, d
    con = db.connect(path)
    rows = candidates(con)
    assert rows["ai_channel_one"][0] == "new", rows["ai_channel_one"]
    assert rows["ai_channel_one"][1] == "feed:tuber-os", rows["ai_channel_one"]
    assert "mentions=5" in rows["ai_channel_one"][2], rows["ai_channel_one"]
    assert "ai_hint=1" in rows["ai_channel_one"][2], rows["ai_channel_one"]
    assert "Третий пример" not in rows["ai_channel_one"][2], "должно быть не больше 2 примеров"
    assert rows["aibot"][1] == "feed:tuber-os", rows["aibot"]
    assert rows["rare_ai"][1] == "feed:tuber-os", rows["rare_ai"]
    assert "cnn" not in rows and "joinchat" not in rows and "ab" not in rows, "фильтр не сработал"
    # существующий active не тронут и в кандидаты не попал
    st, src = con.execute(
        "SELECT status, source FROM channels WHERE handle='existing_active'").fetchone()
    assert st == "active" and src == "test", (st, src)
    con.close()


# ---------------------------------------------------------------------------
# 2. Идемпотентность: повторный импорт = 0 новых
# ---------------------------------------------------------------------------
def test_import_idempotent():
    path, con = new_db()
    con.close()
    r1 = json.loads(run_cli("bridge", "import", "--feed", FEED, "--db", path).stdout)
    r2 = json.loads(run_cli("bridge", "import", "--feed", FEED, "--db", path).stdout)
    assert r1["imported"] == 4, r1
    assert r2["imported"] == 0, r2
    assert r2["skipped_existing"] == 4, r2
    con = db.connect(path)
    assert len(candidates(con)) == 4, "повторный импорт размножил кандидатов"
    con.close()


# ---------------------------------------------------------------------------
# 3. Попытка импортировать существующий active — не понижать
# ---------------------------------------------------------------------------
def test_existing_active_not_downgraded():
    path, con = new_db(active_handles=["ai_channel_one"])
    con.close()
    r = json.loads(run_cli("bridge", "import", "--feed", FEED, "--db", path).stdout)
    assert r["imported"] == 3, r
    assert r["skipped_existing"] == 1, r
    con = db.connect(path)
    st, src = con.execute(
        "SELECT status, source FROM channels WHERE handle='ai_channel_one'").fetchone()
    assert st == "active", f"active понижен в {st}"
    assert src == "test", f"source переписан: {src}"
    assert "ai_channel_one" not in candidates(con), "реестр продублирован кандидатом"
    con.close()


# ---------------------------------------------------------------------------
# 4. Бот без ai_hint отсеян; бот с ai_hint=1 пропущен
# ---------------------------------------------------------------------------
def test_bot_filter():
    with open(FEED, encoding="utf-8") as fh:
        recs = [json.loads(x) for x in fh if x.strip()]
    bc.feed_to_candidates(recs, bc.load_config(CONFIG))
    cfg = bc.load_config(CONFIG)
    assert bc.filter_reason("spamnewsbot", 4, False, 2, cfg) == "bot"
    assert bc.filter_reason("aibot", 4, True, 2, cfg) is None


# ---------------------------------------------------------------------------
# 5. Гигант-новостник отсеян
# ---------------------------------------------------------------------------
def test_giant_filter():
    cfg = bc.load_config(CONFIG)
    for g in ("cnn", "reuters", "bbc", "nytimes", "tass"):
        assert bc.filter_reason(g, 10, False, 2, cfg) == "news_giant", g
    assert bc.filter_reason("my_author_chan", 10, False, 2, cfg) is None


# ---------------------------------------------------------------------------
# 6. Служебное/паттерн отсеяны; порог упоминаний + исключение ai_hint
# ---------------------------------------------------------------------------
def test_service_and_threshold():
    cfg = bc.load_config(CONFIG)
    assert bc.filter_reason("joinchat", 5, False, 2, cfg) == "reserved"
    assert bc.filter_reason("s", 5, False, 2, cfg) == "reserved"
    assert bc.filter_reason("ab", 5, False, 2, cfg) == "pattern"
    assert bc.filter_reason("has.dot", 5, False, 2, cfg) == "pattern"
    assert bc.filter_reason("rare_chan", 1, False, 2, cfg) == "low_mentions"
    assert bc.filter_reason("rare_chan", 1, True, 2, cfg) is None, "ai_hint должен перебивать порог"


# ---------------------------------------------------------------------------
# 7. --limit: превышение уходит в skipped_limit
# ---------------------------------------------------------------------------
def test_limit():
    path, con = new_db()
    con.close()
    r = json.loads(run_cli("bridge", "import", "--feed", FEED, "--db", path,
                           "--limit", "1").stdout)
    assert r["imported"] == 1, r
    assert r["skipped_limit"] == 3, r
    con = db.connect(path)
    n = con.execute("SELECT count(*) FROM candidate").fetchone()[0]
    con.close()
    assert n == 1, f"в БД {n} строк, ожидалось 1"


# ---------------------------------------------------------------------------
# 8. --dry: сводка без записи
# ---------------------------------------------------------------------------
def test_dry_no_write():
    path, con = new_db()
    con.close()
    r = json.loads(run_cli("bridge", "import", "--feed", FEED, "--db", path, "--dry").stdout)
    assert r["imported"] == 4 and r["dry"] is True, r
    con = db.connect(path)
    n = con.execute("SELECT count(*) FROM candidate").fetchone()[0]
    con.close()
    assert n == 0, f"dry записал {n} строк"


# ---------------------------------------------------------------------------
# 9-10. Отсутствующий и битый фид: понятная ошибка, ненулевой код, без трейсбека
# ---------------------------------------------------------------------------
def test_missing_feed():
    path, con = new_db()
    con.close()
    r = run_cli("bridge", "import", "--feed", "/tmp/tz17_does_not_exist.jsonl", "--db", path)
    assert r.returncode == 2, f"exit={r.returncode}"
    assert "не найден" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr and "Traceback" not in r.stdout, "трейсбек в лицо владельцу"
    assert r.stdout.strip() == "", "stdout должен быть пуст при ошибке"


def test_broken_feed():
    bad = tempfile.mktemp(suffix=".jsonl", dir="/tmp")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write('{"kind":"telegram","handle":"ok_handle","mentions":3}\n')
        fh.write("{это не json}\n")
    path, con = new_db()
    con.close()
    r = run_cli("bridge", "import", "--feed", bad, "--db", path)
    assert r.returncode == 2, f"exit={r.returncode}"
    assert "битый" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, "трейсбек в лицо владельцу"
    con = db.connect(path)
    n = con.execute("SELECT count(*) FROM candidate").fetchone()[0]
    con.close()
    assert n == 0, "битый фид не должен ничего записывать"
    os.remove(bad)


# ---------------------------------------------------------------------------
# 11. Дискавери из своих постов
# ---------------------------------------------------------------------------
def test_discover_from_posts():
    path, con = new_db(active_handles=["known_chan"])
    con.execute("INSERT INTO channels(handle,status,source) VALUES('self_chan','active','test')")
    con.commit()
    ch_self = con.execute("SELECT id FROM channels WHERE handle='self_chan'").fetchone()[0]
    posts = [
        # self + новый канал + короткий мусор
        ("Пишем про https://t.me/new_ai_chan и свой https://t.me/self_chan/42 , https://t.me/s", 1),
        ("Ещё упоминание @new_ai_chan и https://t.me/known_chan/7", 2),
        ("Опять https://t.me/known_chan/8 и https://t.me/self_chan/9 , бот https://t.me/spamnewsbot и гигант https://t.me/cnn", 3),
    ]
    for text, mid in posts:
        con.execute("INSERT INTO posts(channel_id,message_id,date_utc,text,links) VALUES(?,?,?,?,?)",
                    (ch_self, mid, "2026-09-15T10:00:00+00:00", text, "[]"))
    con.commit()
    con.close()
    r = json.loads(run_cli("discover", "--db", path).stdout)
    assert r["source"] == "post_mentions", r
    assert r["imported"] == 1, r        # только new_ai_chan (mentions=2)
    assert r["skipped_existing"] == 2, r  # known_chan, self_chan (по 2 упоминания)
    assert r["skipped_filter"] == 3, r    # s (паттерн), spamnewsbot (бот), cnn (гигант)
    con = db.connect(path)
    st, src = con.execute(
        "SELECT status, found_via FROM candidate WHERE handle='new_ai_chan'").fetchone()
    assert st == "new" and src == "post_mentions", (st, src)
    con.close()


# ---------------------------------------------------------------------------
# 12. Экспорт X/YouTube
# ---------------------------------------------------------------------------
def test_export_candidates():
    path, con = new_db()
    con.execute("INSERT INTO channels(handle,status,source) VALUES('src_chan','active','test')")
    ch = con.execute("SELECT id FROM channels WHERE handle='src_chan'").fetchone()[0]
    con.execute("INSERT INTO posts(channel_id,message_id,date_utc,text,links) VALUES(?,?,?,?,?)",
                (ch, 100, "2026-09-10T10:00:00+00:00",
                 "смотри https://x.com/rohanpaul_ai/status/123 и видео https://www.youtube.com/watch?v=dQw4w9WgXcQ ?t=1s",
                 '["https://x.com/rohanpaul_ai/status/123"]'))
    con.execute("INSERT INTO posts(channel_id,message_id,date_utc,text,links) VALUES(?,?,?,?,?)",
                (ch, 101, "2026-09-15T10:00:00+00:00",
                 "ещё раз https://twitter.com/rohanpaul_ai и https://youtu.be/dQw4w9WgXcQ", "[]"))
    con.execute("INSERT INTO posts(channel_id,message_id,date_utc,text,links) VALUES(?,?,?,?,?)",
                (ch, 102, "2026-09-12T10:00:00+00:00", "служебная https://x.com/i/lists/1", "[]"))
    con.commit()
    con.close()
    out = tempfile.mktemp(suffix=".jsonl", dir="/tmp")
    r = json.loads(run_cli("bridge", "export", "--db", path, "--out", out).stdout)
    assert r["kind_counts"] == {"x": 1, "youtube": 1}, r
    assert r["written"] == 2, r
    assert r["candidates"] == {"inserted": 2, "updated": 0}, r
    rows = [json.loads(x) for x in open(out, encoding="utf-8")]
    by = {(x["kind"], x["handle"]): x for x in rows}
    x = by[("x", "rohanpaul_ai")]
    assert x["mentions"] == 2, x
    # ТЗ-21/A: videos — число разных постов, video_ids — список id (для X пусто).
    assert x["videos"] == 2, x
    assert x["video_ids"] == [], x
    assert x["source"] == "tuber-telegram:posts", x
    assert x["first_seen"] == "2026-09-10T10:00:00+00:00", x
    assert x["last_seen"] == "2026-09-15T10:00:00+00:00", x
    # ТЗ-21/A: examples — список СТРОК-цитат (не объектов).
    assert len(x["examples"]) == 2, x["examples"]
    assert all(isinstance(e, str) for e in x["examples"]), x["examples"]
    assert x["examples"][0].startswith("100:"), x["examples"]
    assert all(len(e) <= 80 for e in x["examples"]), x["examples"]
    y = by[("youtube", "dQw4w9WgXcQ")]
    assert y["videos"] == 2, y
    assert y["video_ids"] == ["dQw4w9WgXcQ"], y
    assert "i" not in [h for (k, h) in by if k == "x"], "служебный x.com/i не должен попасть"
    for row in rows:
        assert "T" in row["first_seen"] and "+" in row["first_seen"], row
        assert "T" in row["last_seen"] and "+" in row["last_seen"], row
    # повторный экспорт: кандидаты не размножаются (идемпотентность обмена)
    r2 = json.loads(run_cli("bridge", "export", "--db", path, "--out", out).stdout)
    assert r2["candidates"] == {"inserted": 0, "updated": 2}, r2
    con = db.connect(path)
    assert con.execute("SELECT count(*) FROM candidate").fetchone()[0] == 2
    con.close()
    os.remove(out)


# ---------------------------------------------------------------------------
# 13. Извлечение хендлов (регрессы ссылок)
# ---------------------------------------------------------------------------
def test_extractors():
    assert bc.extract_telegram_handles("t.me/AbC_Chan/12", '["https://t.me/AbC_Chan/12?single"]') == {"abc_chan"}
    assert bc.extract_telegram_handles("", '["https://telegram.me/other_chan"]') == {"other_chan"}
    assert bc.extract_telegram_handles("голое @some_handle тут", None) == {"some_handle"}
    assert bc.extract_telegram_handles("https://t.me/+AbCdEfInvite", None) == set()
    assert bc.extract_x_handles("https://x.com/Foo_Bar/status/1", None) == {"foo_bar"}
    assert bc.extract_x_handles("https://twitter.com/home", None) == set()
    assert bc.extract_youtube_ids("https://youtu.be/dQw4w9WgXcQ", None) == {"dQw4w9WgXcQ"}
    assert bc.extract_youtube_ids("https://www.youtube.com/shorts/abcdefghijk?x=1", None) == {"abcdefghijk"}
    assert bc.norm_ts("2026-09-12 11:16:20") == "2026-09-12T11:16:20+00:00"
    assert bc.norm_ts("2026-09-12T11:16:20+00:00") == "2026-09-12T11:16:20+00:00"


# ---------------------------------------------------------------------------
# 14. Экспорт не перезаписывает чужие строки candidate (ТЗ-4, находка приёмки)
# ---------------------------------------------------------------------------
def test_export_does_not_clobber_foreign_candidate_meta():
    """Строка candidate, заведённая другой площадкой, сохраняет свой meta_json.

    Находка приёмки: экспорт Telegram UPSERT-ит X/YouTube-кандидатов в общую
    таблицу `candidate`. Если (platform, handle) уже занят другим продюсером
    (`found_via` другой), его `meta_json` принадлежит ему и перезаписи не
    подлежит — иначе один фид молча стирает поля чужого кандидата.
    """
    path, con = new_db()
    con.execute(
        "INSERT INTO candidate(platform, kind, handle, found_via, status, meta_json,"
        " first_seen_at, last_seen_at)"
        " VALUES('x','handle','foreign_handle','author:тема','new',"
        "        '{\"verified_at\": null, \"sources\": \"legacy\"}',"
        "        '2026-09-01 00:00:00', '2026-09-01 00:00:00')")
    con.commit()
    action = db.upsert_candidate(
        con, "x", "foreign_handle", kind="x", found_via="tuber-telegram:posts",
        meta={"mentions": 3, "examples": ["из фида телеги"]},
        first_seen="2026-09-15T10:00:00+00:00", last_seen="2026-09-16T10:00:00+00:00")
    con.commit()
    assert action == "updated"
    row = con.execute("SELECT meta_json, found_via, seen_count FROM candidate"
                      " WHERE platform='x' AND handle='foreign_handle'").fetchone()
    assert row[1] == "author:тема", "чужой found_via перезаписан"
    assert row[0] == '{"verified_at": null, "sources": "legacy"}', \
        f"чужой meta_json затёрт: {row[0]}"
    assert row[2] == 2, "встреча не отмечена"
    con.close()


def test_export_creates_own_candidate_row():
    """Своя строка (нашего продюсера) обновляется как раньше."""
    path, con = new_db()
    db.upsert_candidate(con, "youtube", "abcdefghijk", kind="youtube",
                        found_via="tuber-telegram:posts", external_id="abcdefghijk",
                        meta={"mentions": 1})
    con.commit()
    action = db.upsert_candidate(con, "youtube", "abcdefghijk", kind="youtube",
                                 found_via="tuber-telegram:posts",
                                 meta={"mentions": 2})
    con.commit()
    assert action == "updated"
    row = con.execute("SELECT meta_json, seen_count FROM candidate"
                      " WHERE platform='youtube' AND handle='abcdefghijk'").fetchone()
    assert '"mentions": 2' in row[0], row[0]
    assert row[1] == 2, row[1]
    con.close()


# ---------------------------------------------------------------------------
# 11. ТЗ-51 (D-55): @X / t.me/x / X — одна запись при импорте
# ---------------------------------------------------------------------------
def test_import_dedup_by_canonical_handle():
    """Канонизация хендла: alias-формы реестра ловятся при импорте очереди."""
    path, con = new_db(active_handles=["MiXeD"])
    cfg = bc.load_config(CONFIG)
    stats = bc.import_candidates(
        con, {"https://t.me/mixed": {"mentions": 5, "ai_hint": True,
                                     "examples": ["x"]}},
        source="post_mentions", dry=False, limit=10, min_mentions=2, cfg=cfg)
    assert stats["imported"] == 0, stats
    assert stats["skipped_existing"] == 1, stats
    n = con.execute("SELECT count(*) FROM candidate").fetchone()[0]
    assert n == 0, "alias продублировал существующий реестр"
    con.close()
