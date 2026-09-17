"""Тесты сборщика Telegram (перенос `scripts/test_collect.py`, ТЗ-4).

Сеть не используется: HTTP-страницы подменяются локальными фикстурами.
Работает на временной единой базе (адаптер `store`) — боевая не трогается.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest

from tuber.platforms.telegram import collect as C
from tuber.platforms.telegram import store as db

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

#: Путь к временной базе; выставляется автофикстурой ниже (по одному на тест).
TEST_DB = ""


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    global TEST_DB
    TEST_DB = str(tmp_path / "tt_test.db")
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
            (
                ch["handle"], ch.get("title", ch["handle"]), ch.get("subs", 1000),
                ch.get("status", "active"), ch.get("read_mode", "web"),
                ch.get("checked_at"),
            ),
        )
    con.commit()
    return con


def install_fake(routes):
    calls = []

    def fake(client, url):
        calls.append(url)
        if url in routes:
            v = routes[url]
            if isinstance(v, tuple):
                return v
            return 200, v
        return 404, "<html><body>not found</body></html>"

    C.http_get = fake
    return calls


def make_collector(**kw):
    kw.setdefault("sleep_scale", 0.0)
    kw.setdefault("client", object())
    return C.Collector(db_path=TEST_DB, **kw)


# ---------------------------------------------------------------------------
# 1. Разбор message_id / даты (UTC) / просмотров / текста
# ---------------------------------------------------------------------------
def test_parse_fields():
    posts = C.parse_page(fixture("chatgptv.html"), "chatgptv")
    assert posts, "нет разобранных постов"
    p = next((x for x in posts if x["message_id"] == 11998), None)
    assert p is not None, "пост 11998 не найден"
    assert p["date_utc"] == "2026-09-13T09:10:18+00:00", f"date_utc={p['date_utc']}"
    dt = datetime.fromisoformat(p["date_utc"])
    assert dt.utcoffset() == timedelta(0), "дата не в UTC"
    assert p["views"] == 30400, f"views={p['views']}"
    assert "Вайс-Сити" in p["text"], "текст не разобран"
    assert p["media_kind"] == "photo", f"media={p['media_kind']}"
    assert p["reactions"] and p["reactions"] > 0, "реакции не разобраны"


# ---------------------------------------------------------------------------
# 2. Пагинация: при min_id в БД запрашивается ?before=
# ---------------------------------------------------------------------------
def test_pagination_before():
    con = reset_db([{"handle": "chatgptv", "subs": 100}])
    cid = con.execute("SELECT id FROM channels WHERE handle='chatgptv'").fetchone()[0]
    con.execute(
        "INSERT INTO posts(channel_id, message_id, date_utc, text) VALUES (?,?,?,?)",
        (cid, 11900, "2026-09-12T00:00:00+00:00", "старый"),
    )
    con.commit()
    con.close()
    calls = install_fake({
        "https://t.me/s/chatgptv": fixture("chatgptv.html"),
        "https://t.me/s/chatgptv?before=11988": fixture("chatgptv_before.html"),
    })
    c = make_collector(mode="web", deadline=60)
    c.execute()
    before = [u for u in calls if "?before=" in u]
    assert before, f"?before= не запрошен, calls={calls}"
    assert before[0] == "https://t.me/s/chatgptv?before=11988", f"before url={before[0]}"


# ---------------------------------------------------------------------------
# 3. Повторный прогон: upsert без дублей, но обновляет просмотры
# ---------------------------------------------------------------------------
def test_upsert_updates_views():
    reset_db([{"handle": "deeptechnet", "subs": 10}]).close()
    install_fake({"https://t.me/s/deeptechnet": fixture("deeptechnet.html")})
    make_collector(mode="web", deadline=60).execute()
    con = db.connect(TEST_DB)
    n1 = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    assert n1 > 0, "первый прогон не записал посты"
    # обнуляем просмотры и снимаем throttle, как будто прошли сутки
    con.execute("UPDATE posts SET views=1, views_checked_at='2000-01-01 00:00:00'")
    con.commit()
    con.close()

    make_collector(mode="web", deadline=60).execute()
    con = db.connect(TEST_DB)
    n2 = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    assert n1 == n2, f"дубли: было {n1}, стало {n2}"
    updated = con.execute("SELECT COUNT(*) FROM posts WHERE views>1").fetchone()[0]
    assert updated > 0, "просмотры не обновились"
    # throttle: без снятия отметки views не должны перезаписаться
    con.execute("UPDATE posts SET views=222 WHERE message_id=(SELECT MIN(message_id) FROM posts)")
    con.commit()
    con.close()
    make_collector(mode="web", deadline=60).execute()
    con = db.connect(TEST_DB)
    still = con.execute(
        "SELECT views FROM posts WHERE message_id=(SELECT MIN(message_id) FROM posts)"
    ).fetchone()[0]
    assert still == 222, f"views перезаписаны раньше суток (={still})"
    con.close()


# ---------------------------------------------------------------------------
# 4. Пост без текста и без медиа не пишется
# ---------------------------------------------------------------------------
def test_junk_not_written():
    reset_db([{"handle": "edgechan", "subs": 5}]).close()
    install_fake({"https://t.me/s/edgechan": fixture("edgechan_synthetic.html")})
    make_collector(mode="web", deadline=60).execute()
    con = db.connect(TEST_DB)
    ids = [r[0] for r in con.execute("SELECT message_id FROM posts ORDER BY message_id")]
    con.close()
    assert 100 not in ids, f"пустой пост 100 записан: {ids}"
    assert 101 in ids, f"нормальный пост 101 потерян: {ids}"
    assert 103 in ids, f"медиа-пост 103 (без текста) потерян: {ids}"


# ---------------------------------------------------------------------------
# 5. Реклама по ERID/промокоду помечается is_ad=1
# ---------------------------------------------------------------------------
def test_ad_marking():
    reset_db([{"handle": "edgechan", "subs": 5}]).close()
    install_fake({"https://t.me/s/edgechan": fixture("edgechan_synthetic.html")})
    make_collector(mode="web", deadline=60).execute()
    con = db.connect(TEST_DB)
    is_ad = con.execute("SELECT is_ad FROM posts WHERE message_id=102").fetchone()
    not_ad = con.execute("SELECT is_ad FROM posts WHERE message_id=101").fetchone()
    con.close()
    assert is_ad and is_ad[0] == 1, f"пост 102 (ERID/промокод) не помечен: {is_ad}"
    assert not_ad and not_ad[0] == 0, f"обычный пост 101 ошибочно помечен рекламой: {not_ad}"


# ---------------------------------------------------------------------------
# 6. resolve уважает дневной лимит 20 и flood_until
# ---------------------------------------------------------------------------
def test_resolve_guards():
    con = reset_db([
        {"handle": "cand1", "subs": 10, "status": "candidate"},
        {"handle": "cand2", "subs": 9, "status": "candidate"},
    ])
    today = C.today_utc()
    con.execute(
        "INSERT INTO account_state(name, flood_until, resolves_today, day) VALUES ('tg_collector', NULL, 20, ?)",
        (today,),
    )
    con.commit()
    con.close()

    c = make_collector(mode="resolve", resolve_n=20)
    c.connect()
    allowed, eff, reason = c.resolve_gate(20)
    assert not allowed and eff == 0, f"дневной лимит не сработал: {allowed},{eff}"
    assert "limit" in reason, f"reason={reason}"

    # лимит 19 использован -> остаётся ровно 1
    c.con.execute(
        "UPDATE account_state SET resolves_today=19, day=? WHERE name='tg_collector'", (today,)
    )
    c.con.commit()
    allowed, eff, _ = c.resolve_gate(20)
    assert allowed and eff == 1, f"остаток лимита неверен: {allowed},{eff}"

    # flood_until в будущем -> режим не запускается
    until = C.iso_utc(C.utcnow() + timedelta(hours=3))
    c.con.execute("UPDATE account_state SET flood_until=? WHERE name='tg_collector'", (until,))
    c.con.commit()
    allowed, eff, reason = c.resolve_gate(5)
    assert not allowed and "flood until" in reason, f"flood_until не сработал: {reason}"
    # Ядро хранит даты с секундной точностью (``YYYY-MM-DD HH:MM:SS``), а
    # legacy ``iso_utc`` писал микросекунды. Сравниваем момент, а не строку:
    # сам предохранитель флуда проверен выше поведением, а не форматом.
    assert c.summary["flood_until"][:19] == until[:19], "flood_until не попал в summary"

    # run_resolve при флуде не должен трогать Telethon
    def boom(*a, **k):
        raise AssertionError("Telethon вызван во время флуда")

    orig = C.Collector._tg_client
    C.Collector._tg_client = boom
    try:
        c.run_resolve()
    finally:
        C.Collector._tg_client = orig
    assert c.summary["channels_ok"] == 0, "резолв выполнился при активном флуде"
    c.con.close()


# ---------------------------------------------------------------------------
# 7. --deadline завершает прогон и пишет строку в runs
# ---------------------------------------------------------------------------
def test_deadline_writes_run():
    reset_db([{"handle": "edgechan", "subs": 5}]).close()
    install_fake({"https://t.me/s/edgechan": fixture("edgechan_synthetic.html")})
    c = make_collector(mode="web", deadline=0)
    summary = c.execute()
    assert summary["duration_sec"] >= 0
    con = db.connect(TEST_DB)
    row = con.execute(
        "SELECT finished_at, mode FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    con.close()
    assert row is not None, "строка в runs не записана"
    assert row[0], "finished_at не заполнен"
    assert row[1] == "web", f"mode={row[1]}"


# ---------------------------------------------------------------------------
# 8. Пустая/приватная страница -> read_mode='unreadable', status='private'
# ---------------------------------------------------------------------------
def test_unreadable_page():
    reset_db([{"handle": "privchan", "subs": 1}]).close()
    install_fake({"https://t.me/s/privchan": fixture("empty_private.html")})
    make_collector(mode="web", deadline=60).execute()
    con = db.connect(TEST_DB)
    row = con.execute(
        "SELECT read_mode, status FROM channels WHERE handle='privchan'"
    ).fetchone()
    con.close()
    assert tuple(row) == ("unreadable", "private"), f"канал не помечен: {row}"


# ---------------------------------------------------------------------------
# 9. Формат счётчиков (31.8K / 1.2M / 1,5 тыс.)
# ---------------------------------------------------------------------------
def test_count_format():
    assert C.parse_count("31.8K") == 31800, C.parse_count("31.8K")
    assert C.parse_count("1.2M") == 1200000
    assert C.parse_count("2,5 тыс") == 2500
    assert C.parse_count("204") == 204
    assert C.parse_count("") is None


# ---------------------------------------------------------------------------
# 10. Ссылки в тексте сохраняются как «текст (url)», медia/forward
# ---------------------------------------------------------------------------
def test_text_links_and_forward():
    posts = C.parse_page(fixture("chatgptv.html"), "chatgptv")
    p = next(x for x in posts if x["message_id"] == 11998)
    assert "Мы в МАХ (https://max.ru/" in p["text"], f"ссылка не сохранена: {p['text'][-120:]!r}"
    links = json.loads(p["links"])
    assert any("max.ru" in u for u in links), f"links={links}"

    fw = [x for x in C.parse_page(fixture("ai_machinelearning_big_data.html"), "ai_machinelearning_big_data")
          if x["is_forward"]]
    assert fw, "пересылка не распознана"
    assert "data_analysis_ml" in (fw[0]["fwd_from"] or ""), f"fwd_from={fw[0]['fwd_from']}"
    assert fw[0]["has_own_media"] == 0, "медиа пересланного поста ошибочно считано своим"
    assert fw[0]["media_kind"] == "photo", f"media_kind={fw[0]['media_kind']}"
