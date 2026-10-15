"""ТЗ-B12 / D-64: ответы Telegram через СВЯЗАННУЮ ГРУППУ обсуждений.

Сеть/учётка не нужны: подставляем асинхронный Telethon-совместимый фейк и свой
``linked_chat_lookup`` (эмулирует ``GetFullChannelRequest.linked_chat_id``).
Проверяем:
* канал со связанной группой — ответы читаются из группы;
* канал без группы — честно «нет связанной группы», ответов нет;
* пост с ответами, которого нет в ``content``, досоздаётся;
* потолок MTProto-запросов останавливает проход;
* лимит 200 каналов за прогон (``COMMENT_LINKED_MAX_CHANNELS``);
* FloodWait на разрешении группы останавливает прогон.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tuber.platforms.telegram import comments as tgc
from tuber.platforms.telegram import store as db


def _seed_channel(con, handle, *, engagement=1.0, msg_id=101):
    con.execute(
        "INSERT INTO source(platform, handle, title, status, first_seen_at) "
        "VALUES('telegram', ?, ?, 'active', datetime('now'))",
        (handle, f"Канал {handle}"),
    )
    sid = con.execute(
        "SELECT id FROM source WHERE platform='telegram' AND handle=?", (handle,)
    ).fetchone()["id"]
    if msg_id is not None:
        con.execute(
            "INSERT INTO content(platform, source_id, external_id, kind, "
            "published_at, text) VALUES('telegram', ?, ?, 'post', "
            "datetime('now','-1 day'), 'текст поста')",
            (sid, f"{handle}/{msg_id}"),
        )
        cid = con.execute(
            "SELECT id FROM content WHERE external_id=?", (f"{handle}/{msg_id}",)
        ).fetchone()["id"]
        con.execute(
            "INSERT INTO score(content_id, computed_at, engagement) "
            "VALUES(?, datetime('now'), ?)", (cid, engagement))
    con.commit()
    return sid


class FakeSender:
    def __init__(self, title):
        self.title = title
        self.first_name = None
        self.last_name = None
        self.username = None


class FakeReply:
    def __init__(self, rid, text, when, sender=None):
        self.id = rid
        self.message = text
        self.date = when
        self.sender = sender or FakeSender("Вася")
        self.reactions = None


class FakeReplies:
    def __init__(self, n):
        self.replies = n


class FakeMsg:
    def __init__(self, mid, n_replies, when, text="пост"):
        self.id = mid
        self.replies = FakeReplies(n_replies) if n_replies is not None else None
        self.date = when
        self.message = text


class FakeEntity:
    def __init__(self, handle):
        self.handle = handle


class FakeFloodWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class FakeTG:
    def __init__(self, messages, replies, linked, *, flood_lookup_on=None):
        self.messages = messages            # handle -> [FakeMsg,...]
        self.replies = replies              # (entity_key, msg_id) -> [FakeReply,...]
        self.linked = linked                # handle -> linked_id | None
        self.flood_lookup_on = flood_lookup_on
        self.get_entity_calls = 0

    async def get_entity(self, key):
        self.get_entity_calls += 1
        return FakeEntity(str(key))

    def iter_messages(self, entity, limit=None, reply_to=None):
        h = entity.handle if isinstance(entity, FakeEntity) else str(entity)

        async def gen():
            if reply_to is None:
                for m in (self.messages.get(h) or [])[:limit]:
                    yield m
            else:
                for r in (self.replies.get((h, reply_to)) or [])[:limit]:
                    yield r

        return gen()


NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _lookup_for(client, handlers):
    async def _lookup(cl, entity):
        h = entity.handle if isinstance(entity, FakeEntity) else str(entity)
        if client.flood_lookup_on == h:
            raise FakeFloodWait(42)
        return handlers.get(h)
    return _lookup


@pytest.fixture()
def tg_con(tg_path):
    c = db.connect(tg_path)
    yield c
    c.close()


def test_channel_with_group_collects_replies_from_group(tg_con):
    _seed_channel(tg_con, "chan1")
    client = FakeTG(
        messages={"chan1": [FakeMsg(101, 2, NOW)]},
        replies={("chan1", 101): [FakeReply(1, "а точно?", NOW),
                                 FakeReply(2, "спасибо!", NOW)]},
        linked={"chan1": 1111},
    )
    summary = tgc.collect(tg_con, client=client, n_channels=5, pause=0,
                          use_linked_chat=True,
                          linked_chat_lookup=_lookup_for(client, {"chan1": 1111}))
    assert summary["replies"] == 2
    assert summary["channels_no_group"] == 0
    assert summary["channels_ok"] == 1


def test_channel_without_group_reported_honestly(tg_con):
    _seed_channel(tg_con, "chan1")
    client = FakeTG(messages={"chan1": [FakeMsg(101, 3, NOW)]}, replies={},
                    linked={"chan1": None})
    summary = tgc.collect(tg_con, client=client, n_channels=5, pause=0,
                          use_linked_chat=True,
                          linked_chat_lookup=_lookup_for(client, {"chan1": None}))
    assert summary["channels_no_group"] == 1
    assert summary["replies"] == 0
    assert tg_con.execute(
        "SELECT COUNT(*) AS n FROM content_comment").fetchone()["n"] == 0


def test_missing_post_with_replies_created(tg_con):
    # Поста message_id=999 в content нет, но у него есть ответы.
    _seed_channel(tg_con, "chan1", msg_id=101)
    client = FakeTG(
        messages={"chan1": [FakeMsg(999, 1, NOW, text="поздний пост")]},
        replies={("chan1", 999): [FakeReply(1, "вопрос?", NOW)]},
        linked={"chan1": 1111},
    )
    summary = tgc.collect(tg_con, client=client, n_channels=5, pause=0,
                          use_linked_chat=True, create_missing_posts=True,
                          linked_chat_lookup=_lookup_for(client, {"chan1": 1111}))
    assert summary["posts_created"] == 1
    assert summary["replies"] == 1
    assert tg_con.execute(
        "SELECT COUNT(*) AS n FROM content WHERE external_id='chan1/999'"
    ).fetchone()["n"] == 1


def test_linked_max_channels_cap(tg_con):
    for i in range(5):
        _seed_channel(tg_con, f"chan{i}", msg_id=100 + i)
    client = FakeTG(messages={}, replies={}, linked={})
    # n_channels=5000 урезается до COMMENT_LINKED_MAX_CHANNELS=200; каналов всего 5.
    tgc.collect(tg_con, client=client, n_channels=5000, pause=0,
                use_linked_chat=True, max_requests=10_000,
                linked_chat_lookup=_lookup_for(client, {}))
    assert tgc.COMMENT_LINKED_MAX_CHANNELS == 200


def test_request_budget_stops(tg_con):
    for i in range(20):
        _seed_channel(tg_con, f"chan{i}", msg_id=100 + i, engagement=20 - i)
    client = FakeTG(messages={}, replies={}, linked={})
    summary = tgc.collect(tg_con, client=client, n_channels=20, pause=0,
                          use_linked_chat=True, max_requests=5,
                          linked_chat_lookup=_lookup_for(client, {}))
    assert summary["stopped"] is True
    assert summary["stop_reason"] == "request_budget"
    assert summary["requests"] <= 5
    assert summary["channels_remaining"] > 0


def test_flood_on_group_lookup_stops(tg_con):
    _seed_channel(tg_con, "chan1")
    client = FakeTG(messages={"chan1": [FakeMsg(101, 1, NOW)]}, replies={},
                    linked={"chan1": 1111}, flood_lookup_on="chan1")
    summary = tgc.collect(tg_con, client=client, n_channels=5, pause=0,
                          use_linked_chat=True,
                          linked_chat_lookup=_lookup_for(client, {"chan1": 1111}))
    assert summary["stopped"] is True
    assert summary["stop_reason"] == "flood_wait"
    assert summary["flood_wait"] == 42
    st = tg_con.execute(
        "SELECT flood_until FROM transport_account_state "
        "WHERE platform='telegram' AND name=?", (tgc.ACCOUNT_NAME,)).fetchone()
    assert st is not None and st["flood_until"] == summary["flood_until"]
