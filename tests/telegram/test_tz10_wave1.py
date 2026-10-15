"""ТЗ-10, волна 1: B1 (IntegrityError metric_snapshot), B2 (change-detection),
B4 (служебные сообщения Telegram).

Все проверки — без сети, на временных базах (``tmp_path``).
"""
from __future__ import annotations

import os

import pytest

from tuber.platforms.telegram import collect as C
from tuber.platforms.telegram import store as db

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

#: Путь к временной базе для интеграционных прогонов коллектора.
TEST_DB = ""


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    global TEST_DB
    TEST_DB = str(tmp_path / "tz10_tg_test.db")
    db.init_db(TEST_DB).close()
    yield TEST_DB


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def reset_db(channels):
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(TEST_DB + suf)
        except OSError:
            pass
    con = db.init_db(TEST_DB)
    for ch in channels:
        con.execute(
            "INSERT INTO channels(handle, title, subs, status, read_mode, checked_at) "
            "VALUES (?,?,?,?,?,?)",
            (ch["handle"], ch.get("title", ch["handle"]), ch.get("subs", 1000),
             ch.get("status", "active"), ch.get("read_mode", "web"), ch.get("checked_at")),
        )
    con.commit()
    return con


def install_fake(routes):
    calls = []

    def fake(client, url):
        calls.append(url)
        if url in routes:
            v = routes[url]
            return v if isinstance(v, tuple) else (200, v)
        return 404, "<html><body>not found</body></html>"

    C.http_get = fake
    return calls


def make_collector(**kw):
    kw.setdefault("sleep_scale", 0.0)
    kw.setdefault("client", object())
    return C.Collector(db_path=TEST_DB, **kw)


# ---------------------------------------------------------------------------
# B1: идемпотентная запись metric_snapshot при любом views_checked_at
# ---------------------------------------------------------------------------
def _mk_channel(con, handle):
    con.execute("INSERT INTO channels(handle, title, subs, status, read_mode)"
                " VALUES (?,?,?, 'active', 'web')", (handle, handle, 100))
    con.commit()
    return con.execute("SELECT id FROM channels WHERE handle=?", (handle,)).fetchone()[0]


def _mk_post(con, chan, mid, *, views, views_checked_at, text="пост"):
    con.execute(
        "INSERT INTO posts(channel_id, message_id, date_utc, text, views, forwards,"
        " reactions, first_seen_at, views_checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (chan, mid, "2026-01-01T00:00:00+00:00", text, views, 1, 1,
         "2026-01-01 00:00:00", views_checked_at),
    )
    con.commit()


def _content_id(con, handle, mid):
    return con.execute(
        "SELECT id FROM content WHERE platform='telegram' AND external_id=?",
        (f"{handle}/{mid}",)).fetchone()[0]


def _snapshot_rows(con, cid):
    return con.execute(
        "SELECT captured_at, views FROM metric_snapshot WHERE content_id=?"
        " ORDER BY captured_at", (cid,)).fetchall()


def test_b1_older_views_checked_at_does_not_collide(con):
    """Два снапшота, приходит метка, равная старому -> нет IntegrityError,
    история (обе метки) сохраняется (ТЗ-10 B1)."""
    chan = _mk_channel(con, "b1chan")
    _mk_post(con, chan, 1, views=100, views_checked_at="2026-01-02 00:00:00")
    cid = _content_id(con, "b1chan", 1)
    con.execute(
        "INSERT INTO metric_snapshot(content_id, captured_at, views, forwards,"
        " reactions, source) VALUES(?, '2026-01-03 00:00:00', 120, 2, 2, 'telegram')",
        (cid,))
    con.commit()
    assert len(_snapshot_rows(con, cid)) == 2

    # раньше здесь падало UNIQUE constraint failed
    con.execute(
        "UPDATE posts SET views=150, views_checked_at=? WHERE channel_id=? AND message_id=?",
        ("2026-01-02 00:00:00", chan, 1))
    con.commit()

    rows = {r["captured_at"]: r["views"] for r in _snapshot_rows(con, cid)}
    assert len(rows) == 2, f"история снапшотов потеряна: {rows}"
    assert rows["2026-01-02 00:00:00"] == 150, f"метрика не записана: {rows}"
    assert rows["2026-01-03 00:00:00"] == 120, f"чужой снапшот испорчен: {rows}"


def test_b1_same_views_checked_at_idempotent(con):
    """Повторный UPDATE с той же меткой не плодит строк (ТЗ-10 B1)."""
    chan = _mk_channel(con, "b1idem")
    _mk_post(con, chan, 7, views=10, views_checked_at="2026-02-01 00:00:00")
    cid = _content_id(con, "b1idem", 7)
    for _ in range(3):
        con.execute(
            "UPDATE posts SET views=11, views_checked_at=? WHERE channel_id=? AND message_id=?",
            ("2026-02-01 00:00:00", chan, 7))
        con.commit()
    rows = _snapshot_rows(con, cid)
    assert len(rows) == 1, f"дубли снапшота: {rows}"
    assert rows[0]["views"] == 11


def test_b1_repeated_run_same_snapshot_count(con):
    """Повторный прогон того же набора постов -> то же число строк (ТЗ-10 B1)."""
    chan = _mk_channel(con, "b1run")
    _mk_post(con, chan, 1, views=5, views_checked_at="2026-03-01 00:00:00")
    _mk_post(con, chan, 2, views=6, views_checked_at="2026-03-01 00:00:00")
    cids = (_content_id(con, "b1run", 1), _content_id(con, "b1run", 2))

    def total():
        return con.execute(
            "SELECT COUNT(*) FROM metric_snapshot WHERE content_id IN (?,?)", cids).fetchone()[0]

    con.execute("UPDATE posts SET views=7, views_checked_at=? WHERE channel_id=?",
                ("2026-03-02 00:00:00", chan))
    con.commit()
    n1 = total()
    con.execute("UPDATE posts SET views=7, views_checked_at=? WHERE channel_id=?",
                ("2026-03-02 00:00:00", chan))
    con.commit()
    n2 = total()
    assert n1 == n2 == 2, f"нестабильное число строк: {n1} -> {n2}"


# ---------------------------------------------------------------------------
# B2: change-detection — повторный прогон не делает пустых UPDATE
# ---------------------------------------------------------------------------
def test_b2_second_run_has_no_spurious_updates():
    reset_db([{"handle": "deeptechnet", "subs": 10}]).close()
    install_fake({"https://t.me/s/deeptechnet": fixture("deeptechnet.html")})
    r1 = make_collector(mode="web", deadline=60).execute()
    assert r1["posts_new"] > 0, f"первый прогон ничего не записал: {r1}"
    r2 = make_collector(mode="web", deadline=60).execute()
    assert r2["posts_new"] == 0, f"второй прогон пишет новое: {r2}"
    assert r2["posts_upd"] == 0, f"второй прогон делает пустые UPDATE: {r2}"


def test_b2_real_change_is_still_written():
    reset_db([{"handle": "deeptechnet", "subs": 10}]).close()
    install_fake({"https://t.me/s/deeptechnet": fixture("deeptechnet.html")})
    make_collector(mode="web", deadline=60).execute()
    con = db.connect(TEST_DB)
    # имитируем «прошли сутки» + изменившееся значение просмотров
    con.execute("UPDATE posts SET views=1, views_checked_at='2000-01-01 00:00:00'")
    con.commit()
    con.close()
    r = make_collector(mode="web", deadline=60).execute()
    assert r["posts_upd"] > 0, f"реальное изменение не записано: {r}"
    con = db.connect(TEST_DB)
    changed = con.execute("SELECT COUNT(*) FROM posts WHERE views>1").fetchone()[0]
    con.close()
    assert changed > 0, "просмотры не обновились"


# ---------------------------------------------------------------------------
# B4: служебные сообщения не сохраняются и считаются
# ---------------------------------------------------------------------------
_SERVICE_PAGE = """<html><body>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap service_message js-widget_message"
      data-post="svchan/2">
  <div class="tgme_widget_message_text js-message_text">Live stream started</div>
  <time datetime="2026-09-17T10:00:00+00:00"></time>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="svchan/1">
  <div class="tgme_widget_message_text js-message_text">настоящий пост</div>
  <time datetime="2026-09-17T09:00:00+00:00"></time>
 </div>
</div>
</body></html>"""


def test_b4_parse_page_skips_service_and_counts():
    stats = {}
    posts = C.parse_page(_SERVICE_PAGE, "svchan", stats=stats)
    assert [p["message_id"] for p in posts] == [1], "служебный пост разобран"
    assert stats["service_skipped"] == 1


def test_b4_collector_does_not_store_service():
    reset_db([{"handle": "svchan", "subs": 5}]).close()
    install_fake({"https://t.me/s/svchan": _SERVICE_PAGE})
    res = make_collector(mode="web", deadline=60).execute()
    assert res["service_skipped"] == 1, f"счётчик отсечённых не сработал: {res}"
    con = db.connect(TEST_DB)
    ids = [r[0] for r in con.execute("SELECT message_id FROM posts")]
    con.close()
    assert 2 not in ids, f"служебное сообщение сохранено: {ids}"
    assert 1 in ids, f"настоящий пост потерян: {ids}"
