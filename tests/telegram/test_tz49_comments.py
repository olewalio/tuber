"""ТЗ-49: сбор текстов ответов Telegram через Telethon (фейковый клиент).

Сеть/учётка не нужны: подставляем асинхронный Telethon-совместимый фейк.
Проверяем:
* отбор топ-каналов по вовлечённости;
* запись ответов в ``content_comment`` с привязкой к посту ``content``;
* предохранители: потолок ответов за прогон;
* честную остановку при flood-wait со записью состояния в БД;
* курсор канала (состояние в БД, как у снимков метрик);
* ``dry_run`` не трогает ни сеть, ни базу.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tuber.platforms.telegram import comments as tgc
from tuber.platforms.telegram import store as db


def _seed_channel(con, handle, *, engagement=1.0, views=1000, forwards=10,
                  reactions=3, msg_id=101, published="now"):
    con.execute(
        "INSERT INTO source(platform, handle, title, status, first_seen_at) "
        "VALUES('telegram', ?, ?, 'active', datetime('now'))",
        (handle, f"Канал {handle}"),
    )
    sid = con.execute(
        "SELECT id FROM source WHERE platform='telegram' AND handle=?",
        (handle,)).fetchone()["id"]
    when = f"datetime('now','-1 day')" if published == "now" else "?"
    con.execute(
        f"INSERT INTO content(platform, source_id, external_id, kind, url, "
        f"published_at, text) VALUES('telegram', ?, ?, 'post', NULL, {when}, ?)",
        (sid, f"{handle}/{msg_id}", "текст поста")
        if published == "now" else (sid, f"{handle}/{msg_id}", published, "текст поста"),
    )
    cid = con.execute(
        "SELECT id FROM content WHERE platform='telegram' AND external_id=?",
        (f"{handle}/{msg_id}",)).fetchone()["id"]
    con.execute(
        "INSERT INTO score(content_id, computed_at, engagement) VALUES(?, datetime('now'), ?)",
        (cid, engagement))
    con.execute(
        "INSERT INTO content_latest(content_id, captured_at, views, forwards, reactions) "
        "VALUES(?, datetime('now'), ?, ?, ?)", (cid, views, forwards, reactions))
    con.commit()
    return sid, cid


class FakeSender:
    def __init__(self, title):
        self.title = title
        self.first_name = None
        self.last_name = None
        self.username = None


class FakeReply:
    def __init__(self, rid, text, when, sender):
        self.id = rid
        self.message = text
        self.date = when
        self.sender = sender
        self.reactions = None


class FakeReplies:
    def __init__(self, n):
        self.replies = n


class FakeMsg:
    def __init__(self, mid, n_replies, when):
        self.id = mid
        self.replies = FakeReplies(n_replies) if n_replies is not None else None
        self.date = when
        self.message = "пост"


class FakeEntity:
    def __init__(self, handle):
        self.handle = handle


class FakeTG:
    """Асинхронный Telethon-совместимый фейк."""

    def __init__(self, messages, replies, flood_on=None):
        self.messages = messages            # handle -> [FakeMsg,...]
        self.replies = replies              # (handle, msg_id) -> [FakeReply,...]
        self.flood_on = flood_on            # handle, на котором бросаем flood
        self.resolved = []

    async def get_entity(self, handle):
        self.resolved.append(handle)
        if self.flood_on == handle:
            raise FakeFloodWait(42)
        if handle not in self.messages:
            raise ValueError(f"нет entity {handle}")
        return FakeEntity(handle)

    def iter_messages(self, entity, limit=None, reply_to=None):
        handle = entity.handle if isinstance(entity, FakeEntity) else entity

        async def gen():
            if reply_to is None:
                for m in (self.messages.get(handle) or [])[:limit]:
                    yield m
            else:
                for r in (self.replies.get((handle, reply_to)) or [])[:limit]:
                    yield r

        return gen()


class FakeFloodWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def tg_con(tg_path):
    c = db.connect(tg_path)
    yield c
    c.close()


def _reply(cid, text, author="Вася", likes=None):
    return FakeReply(cid, text, NOW, FakeSender(author))


def test_select_channels_ranks_by_engagement(tg_con):
    _seed_channel(tg_con, "low", engagement=1.0)
    _seed_channel(tg_con, "high", engagement=50.0)
    chans = tgc.select_channels(tg_con, n=5)
    assert [c["handle"] for c in chans][:2] == ["high", "low"]
    assert chans[0]["engagement"] == 50.0


def test_collect_writes_replies_bound_to_post(tg_con):
    sid, cid = _seed_channel(tg_con, "chan1")
    client = FakeTG(
        messages={"chan1": [FakeMsg(101, 2, NOW)]},
        replies={("chan1", 101): [_reply(1, "А это точно работает?"),
                                  _reply(2, "Спасибо!")]},
    )
    summary = tgc.collect(tg_con, client=client, n_channels=5, pause=0)
    assert summary["replies"] == 2
    rows = tg_con.execute(
        "SELECT comment_id, content_id, platform, author, text FROM content_comment "
        "WHERE platform='telegram'").fetchall()
    assert len(rows) == 2
    assert rows[0]["content_id"] == cid
    assert rows[0]["comment_id"] == "tg:chan1/101/1"
    assert "работает?" in rows[0]["text"]


def test_collect_skips_reply_without_post(tg_con):
    # Канал есть, но поста message_id=999 в content нет.
    _seed_channel(tg_con, "chan1", msg_id=101)
    client = FakeTG(messages={"chan1": [FakeMsg(999, 1, NOW)]},
                    replies={("chan1", 999): [_reply(1, "вопрос?")]})
    summary = tgc.collect(tg_con, client=client, n_channels=5, pause=0)
    assert summary["replies"] == 0
    assert tg_con.execute("SELECT COUNT(*) AS n FROM content_comment").fetchone()["n"] == 0


def test_collect_respects_max_replies(tg_con):
    _seed_channel(tg_con, "chan1")
    client = FakeTG(
        messages={"chan1": [FakeMsg(101, 5, NOW)]},
        replies={("chan1", 101): [_reply(i, f"вопрос {i}?") for i in range(1, 6)]},
    )
    summary = tgc.collect(tg_con, client=client, n_channels=5, max_replies=2, pause=0)
    assert summary["replies"] == 2


def test_flood_wait_stops_honestly_and_stores_state(tg_con):
    _seed_channel(tg_con, "chan1")
    client = FakeTG(messages={"chan1": []}, replies={}, flood_on="chan1")
    summary = tgc.collect(tg_con, client=client, n_channels=5, pause=0)
    assert summary["stopped"] is True
    assert summary["flood_wait"] == 42
    assert summary["flood_until"]
    st = tg_con.execute(
        "SELECT flood_until FROM transport_account_state "
        "WHERE platform='telegram' AND name=?", (tgc.ACCOUNT_NAME,)).fetchone()
    assert st is not None and st["flood_until"] == summary["flood_until"]


def test_cursor_written_per_channel(tg_con):
    _seed_channel(tg_con, "chan1")
    client = FakeTG(messages={"chan1": [FakeMsg(101, 1, NOW)]},
                    replies={("chan1", 101): [_reply(1, "ок?")]})
    tgc.collect(tg_con, client=client, n_channels=5, pause=0)
    row = tg_con.execute(
        "SELECT cursor, items_total, meta_json FROM cursor "
        "WHERE platform='telegram' AND kind=? AND ref='chan1'",
        (tgc.CURSOR_KIND,)).fetchone()
    assert row is not None
    assert row["cursor"] == "101"
    assert row["items_total"] == 1


def test_dry_run_selects_only_and_writes_nothing(tg_con):
    _seed_channel(tg_con, "chan1")
    summary = tgc.collect(tg_con, dry_run=True, n_channels=5)
    assert summary["dry_run"] is True
    assert summary["channels"] == 1
    assert summary["replies"] == 0
    assert tg_con.execute("SELECT COUNT(*) AS n FROM content_comment").fetchone()["n"] == 0
    assert tg_con.execute("SELECT COUNT(*) AS n FROM cursor").fetchone()["n"] == 0
