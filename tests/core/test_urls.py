"""Тесты синтеза ссылки и бэкфилла ``content.url`` (долг D-45, ТЗ-6 §3).

Проверяется:
* синтез по каждой платформе (правила :mod:`tuber.core.urls`);
* NULL, когда любого компонента нет (ссылка не выдумывается);
* заполнение ``url`` при записи через :func:`tuber.core.storage.upsert_content`;
* идемпотентность бэкфилла (повторный прогон меняет 0 строк);
* выдача ``tuber report`` не собирает ссылку на месте при пустом ``content.url``,
  а печатает честную оговорку.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tuber.analysis import report
from tuber.core import db, schema, storage, urls


def _iso(days_ago: float = 1.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# Синтез
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("platform,external_id,handle,expected", [
    ("youtube", "--8Rr6ahsGI", None,
     "https://www.youtube.com/watch?v=--8Rr6ahsGI"),
    ("youtube", "--8Rr6ahsGI", "@ignored",
     "https://www.youtube.com/watch?v=--8Rr6ahsGI"),
    ("x", "1565707185229017090", "chandrarsrikant",
     "https://x.com/chandrarsrikant/status/1565707185229017090"),
    ("x", "1565707185229017090", "@chandrarsrikant",
     "https://x.com/chandrarsrikant/status/1565707185229017090"),
    ("telegram", "AGI_and_RL/1346", None, "https://t.me/AGI_and_RL/1346"),
    ("telegram", "AGI_and_RL/1346", "AGI_and_RL", "https://t.me/AGI_and_RL/1346"),
])
def test_content_url_by_platform(platform, external_id, handle, expected):
    assert urls.content_url(platform, external_id, handle) == expected


@pytest.mark.parametrize("platform,external_id,handle", [
    ("youtube", None, "ch"),          # нет external_id
    ("youtube", "   ", "ch"),         # пустой external_id
    ("x", "123", None),               # нет handle
    ("x", "123", "  "),               # пустой handle
    ("x", None, "h"),                 # нет external_id
    ("telegram", "ch", "ch"),         # нет номера сообщения (нет '/')
    ("telegram", "ch/", "ch"),        # пустой номер
    ("telegram", "/42", None),        # нет канала
    ("unknown", "1", "h"),            # чужая платформа
])
def test_content_url_none_when_component_missing(platform, external_id, handle):
    assert urls.content_url(platform, external_id, handle) is None


# --------------------------------------------------------------------------- #
# Приоритет значения из базы (D-45): база главнее синтеза
# --------------------------------------------------------------------------- #
def test_material_url_prefers_db_value():
    """Непустой content.url печатается как есть, синтез его не перекрывает."""
    assert urls.material_url("https://t.me/real/10", "telegram", "other/1",
                             "other") == "https://t.me/real/10"


def test_material_url_synthesizes_when_db_empty():
    """Пустая колонка → единый синтез (handle есть)."""
    assert urls.material_url(None, "x", "999", "alice") == \
        "https://x.com/alice/status/999"
    assert urls.material_url("", "youtube", "vid", None) == \
        "https://www.youtube.com/watch?v=vid"


def test_material_url_none_when_nothing_to_print():
    """Пустая колонка и не хватает компонентов → None (не выдумываем)."""
    assert urls.material_url(None, "x", "999", None) is None
    assert urls.material_url(None, "telegram", "noslash", None) is None


# --------------------------------------------------------------------------- #
# Заполнение при записи и бэкфилл
# --------------------------------------------------------------------------- #
def _fresh(tmp_path):
    conn = db.connect(str(tmp_path / "core.db"))
    schema.init_schema(conn)
    return conn


def test_upsert_content_fills_url(tmp_path):
    conn = _fresh(tmp_path)
    try:
        with db.write_tx(conn):
            sid = storage.upsert_source(conn, "x", "handle_a")
            storage.upsert_content(conn, "x", "777", source_id=sid,
                                   published_at=_iso(1))
            cid = storage.upsert_content(conn, "youtube", "vid", published_at=_iso(1))
        assert conn.execute("SELECT url FROM content WHERE platform='x'").fetchone()[0] \
            == "https://x.com/handle_a/status/777"
        assert conn.execute("SELECT url FROM content WHERE id=?", (cid,)).fetchone()[0] \
            == "https://www.youtube.com/watch?v=vid"
    finally:
        conn.close()


def test_backfill_is_idempotent(tmp_path):
    conn = _fresh(tmp_path)
    try:
        with db.write_tx(conn):
            conn.execute("INSERT INTO source(id, platform, external_id, handle)"
                         " VALUES (1,'x','x1','hx'), (2,'telegram','t1','tgc')")
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,published_at,url)"
                " VALUES (10,'x',1,'999',?,NULL),"
                " (11,'telegram',2,'tgc/5',?,NULL),"
                " (12,'youtube',NULL,'vid',?,NULL),"
                # ссылка уже стоит — не трогаем
                " (13,'x',1,'1000',?,'https://x.com/hx/status/1000')",
                (_iso(1), _iso(1), _iso(1), _iso(1)))
            first = schema.ensure_content_url(conn)
        assert first["filled"] == 3
        assert first["remaining"] == 0
        before = conn.execute(
            "SELECT id, url FROM content ORDER BY id").fetchall()
        # Второй прогон: гейт по маркеру → 0 изменений, тот же результат.
        with db.write_tx(conn):
            second = schema.ensure_content_url(conn)
        assert second == {}
        after = conn.execute("SELECT id, url FROM content ORDER BY id").fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]
    finally:
        conn.close()


def test_backfill_leaves_unknown_null(tmp_path):
    conn = _fresh(tmp_path)
    try:
        with db.write_tx(conn):
            # X без handle вообще (ни content, ни source) — ссылка не выдумывается.
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,published_at,url)"
                " VALUES (20,'x',NULL,'555',?,NULL),"
                " (21,'telegram',NULL,'nochannel',?,NULL)",
                (_iso(1), _iso(1)))
            counts = schema.backfill_content_url(conn)
        assert counts["filled"] == 0
        assert counts["remaining"] == 2
        assert conn.execute("SELECT url FROM content WHERE id=20").fetchone()[0] is None
        assert conn.execute("SELECT url FROM content WHERE id=21").fetchone()[0] is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Выдача: без синтеза на месте
# --------------------------------------------------------------------------- #
def test_report_does_not_invent_link(tmp_path):
    conn = _fresh(tmp_path)
    try:
        with db.write_tx(conn):
            conn.execute("INSERT INTO source(id, platform, external_id, handle)"
                         " VALUES (1,'x','x1','hx')")
            # handle есть, но content.url пуст: выдача НЕ должна собрать ссылку.
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,published_at,"
                "text,author_handle,url) VALUES"
                " (30,'x',1,'888',?,'пост без url','hx',NULL)",
                (_iso(1),))
            conn.execute("INSERT INTO content_latest(content_id,likes)"
                         " VALUES (30, 5)")
        text = report.build(conn, days=10, db_path=":memory:")
        assert "https://x.com/hx/status/888" not in text
        assert report.NO_URL in text
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Контур сбора: ссылка заполняется при записи каждой платформой (D-45, §3.3)
# --------------------------------------------------------------------------- #
def test_x_collection_insert_fills_url(tmp_path):
    from tuber.platforms.x import store as xstore

    con = xstore.connect(str(tmp_path / "x.db"))
    try:
        con.execute("INSERT INTO accounts(handle, tier, status)"
                    " VALUES ('hx','A','active')")
        aid = con.execute("SELECT id FROM accounts WHERE handle='hx'").fetchone()[0]
        # owner_handle — реальный автор поста при сборе по аккаунту.
        con.execute(
            "INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
            " text, text_hash, owner_handle, lang, first_seen_at)"
            " VALUES (?,?,?,'rss',?,?,?,?,?)",
            (aid, "11111", _iso(1), "hello", "h1", "hx", "en", _iso(0)))
        con.commit()
        assert con.execute("SELECT url FROM content WHERE platform='x'").fetchone()[0] \
            == "https://x.com/hx/status/11111"
    finally:
        con.close()


def test_youtube_collection_insert_fills_url(tmp_path):
    from tuber.platforms.youtube import store as ystore

    con = ystore.connect(str(tmp_path / "yt.db"))
    try:
        con.execute("INSERT INTO channels(channel_id, handle, title)"
                    " VALUES ('UC1','ytc','T')")
        ystore.upsert_video(con, {"video_id": "vid1", "channel_id": "UC1",
                                  "title": "t", "published_at": "2026-01-01 00:00:00"})
        assert con.execute("SELECT url FROM content WHERE platform='youtube'").fetchone()[0] \
            == "https://www.youtube.com/watch?v=vid1"
    finally:
        con.close()


def test_telegram_collection_insert_fills_url(tmp_path):
    from tuber.platforms.telegram import store as tgstore

    con = tgstore.connect(str(tmp_path / "tg.db"))
    try:
        con.execute("INSERT INTO channels(handle, tg_id, title) VALUES ('tgc', 1, 'T')")
        con.commit()
        cid = con.execute(
            "SELECT id FROM source WHERE platform='telegram'").fetchone()[0]
        con.execute("INSERT INTO posts(channel_id, message_id, date_utc, text)"
                    " VALUES (?,?,?,?)", (cid, 10, _iso(1), "hi"))
        con.commit()
        assert con.execute(
            "SELECT url FROM content WHERE platform='telegram'").fetchone()[0] \
            == "https://t.me/tgc/10"
    finally:
        con.close()


def test_migrate_schema_backfills_only_on_request(tmp_path):
    """Живые соединения (store.connect) не должны переписывать content.url."""
    path = str(tmp_path / "mig.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    try:
        with db.write_tx(conn):
            conn.execute("INSERT INTO source(id, platform, external_id, handle)"
                         " VALUES (1,'x','x1','hx')")
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,published_at,url)"
                " VALUES (40,'x',1,'42',?,NULL)", (_iso(1),))
        schema.migrate_schema(conn)                      # обычный путь
        assert conn.execute("SELECT url FROM content WHERE id=40").fetchone()[0] is None
        schema.migrate_schema(conn, backfill_url=True)   # явный запрос
        assert conn.execute("SELECT url FROM content WHERE id=40").fetchone()[0] \
            == "https://x.com/hx/status/42"
    finally:
        conn.close()


def test_db_backfill_urls_cli(tmp_path, capsys):
    from tuber.tools import db_backup

    path = str(tmp_path / "cli.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    with db.write_tx(conn):
        conn.execute("INSERT INTO source(id, platform, external_id, handle)"
                     " VALUES (1,'telegram','t1','tgc')")
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,url)"
            " VALUES (50,'telegram',1,'tgc/7',?,NULL)", (_iso(1),))
    conn.close()

    rc = db_backup.main(["backfill-urls", "--db", path])
    out = capsys.readouterr().out
    assert rc == 0 and "заполнено 1" in out
    conn = db.connect(path, readonly=True)
    try:
        assert conn.execute("SELECT url FROM content WHERE id=50").fetchone()[0] \
            == "https://t.me/tgc/7"
    finally:
        conn.close()
