"""Часть 3 «волна 2»: триггер Telegram не двигает снимок, закреплённый за стадией.

``metric_schedule.done_at`` — исторический якорь стадии. Идемпотентный триггер
``posts_upd`` при конфликте метки раньше переносил ``captured_at`` последнего
снимка, стирая этот якорь. Теперь такой снимок остаётся на месте, а новая метка
обрабатывается INSERT-веткой.
"""

from __future__ import annotations

from tuber.platforms.telegram import store as db


def _mk_channel(con, handle):
    con.execute("INSERT INTO channels(handle, title, subs, status, read_mode)"
                " VALUES (?,?,?, 'active', 'web')", (handle, handle, 100))
    con.commit()
    return con.execute("SELECT id FROM channels WHERE handle=?", (handle,)).fetchone()[0]


def _cid(con, handle, mid):
    return con.execute(
        "SELECT id FROM content WHERE platform='telegram' AND external_id=?",
        (f"{handle}/{mid}",)).fetchone()[0]


def _rows(con, cid):
    return {r["captured_at"]: r["views"] for r in con.execute(
        "SELECT captured_at, views FROM metric_snapshot WHERE content_id=?"
        " ORDER BY captured_at", (cid,))}


def test_trigger_keeps_snapshot_pinned_by_schedule(con):
    chan = _mk_channel(con, "pinchan")
    con.execute(
        "INSERT INTO posts(channel_id, message_id, date_utc, text, views, forwards,"
        " reactions, first_seen_at, views_checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (chan, 1, "2026-01-01T00:00:00+00:00", "пост", 100, 1, 1,
         "2026-01-01 00:00:00", "2026-01-02 00:00:00"))
    con.commit()
    cid = _cid(con, "pinchan", 1)
    anchored = "2026-01-02 00:00:00"
    assert anchored in _rows(con, cid)

    # Закрепляем снимок за стадией очереди.
    con.execute(
        "INSERT INTO metric_schedule(content_id, platform, stage, due_at, done_at,"
        " attempt, status) VALUES (?,?,?,?,?,?,?)",
        (cid, "telegram", "1h", anchored, anchored, 1, "measured"))
    con.commit()

    # Новая метка: без защиты триггер перенёс бы закреплённый снимок на неё.
    con.execute("UPDATE posts SET views=200, views_checked_at=? WHERE channel_id=?",
                ("2026-01-03 00:00:00", chan))
    con.commit()

    rows = _rows(con, cid)
    assert anchored in rows, f"закреплённый снимок потерян: {rows}"
    assert rows[anchored] == 100, f"исторический якорь испорчен: {rows}"
    assert "2026-01-03 00:00:00" in rows, f"новая метка не записана: {rows}"
    # Якорь стадии не сдвинулся.
    done = con.execute("SELECT done_at FROM metric_schedule WHERE content_id=?",
                       (cid,)).fetchone()[0]
    assert done == anchored
