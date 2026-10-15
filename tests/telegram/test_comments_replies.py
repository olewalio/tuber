"""D-64: чтение ответов Telegram у САМОГО КАНАЛА и экономия запросов.

Живой замер 24.09.2026 (канал ``gremtelegram``, пост 85131):
* способ A — peer КАНАЛА, ``reply_to=<id поста>``: 2 ответа с текстами;
* способ B — peer СВЯЗАННОЙ ГРУППЫ: ``MsgIdInvalidError`` (у группы своя
  нумерация сообщений, id поста там невалиден).

Сеть/учётка не нужны: асинхронный Telethon-совместимый фейк. Проверяем:
* регрессию способа — при сборе ответов peer именно КАНАЛ (тест падает, если
  кто-то вернёт peer группы);
* канал без постов с ``n_replies > 0`` не шлёт ``GetFullChannelRequest`` и
  помечается как «ответов нет»;
* канал с таким постом шлёт запрос, «каналов без группы» растёт;
* ``MsgIdInvalidError`` не роняет прогон: ошибка канала, остальные идут дальше.
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
    def __init__(self, mid, n_replies, when):
        self.id = mid
        self.replies = FakeReplies(n_replies) if n_replies is not None else None
        self.date = when
        self.message = "пост"


class FakeEntity:
    def __init__(self, handle):
        self.handle = handle


class FakeMsgIdInvalid(Exception):
    """Аналог telethon ``MsgIdInvalidError`` (имя без ``FloodWait``)."""


class RecordingTG:
    """Фейк, запоминающий peer, с которым читались ответы."""

    def __init__(self, messages, replies, *, invalid_replies_on=None):
        self.messages = messages            # handle -> [FakeMsg,...]
        self.replies = replies              # (handle, msg_id) -> [FakeReply,...]
        self.invalid_replies_on = set(invalid_replies_on or ())
        self.reply_peer_handles: list[str] = []
        self.entity_calls: list[str] = []

    async def get_entity(self, handle):
        self.entity_calls.append(str(handle))
        return FakeEntity(str(handle))

    def iter_messages(self, entity, limit=None, reply_to=None):
        h = entity.handle if isinstance(entity, FakeEntity) else str(entity)
        if reply_to is None:
            async def gen_msgs():
                for m in (self.messages.get(h) or [])[:limit]:
                    yield m
            return gen_msgs()

        # Чтение ответов: фиксируем peer, которым его запросили.
        self.reply_peer_handles.append(h)

        async def gen_replies():
            if h in self.invalid_replies_on:
                raise FakeMsgIdInvalid(
                    "The message ID used in the peer was invalid "
                    "(caused by GetRepliesRequest)")
            for r in (self.replies.get((h, reply_to)) or [])[:limit]:
                yield r
        return gen_replies()


NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _lookup_for(handlers, *, calls=None):
    async def _lookup(client, entity):
        h = entity.handle if isinstance(entity, FakeEntity) else str(entity)
        if calls is not None:
            calls.append(h)
        return handlers.get(h)
    return _lookup


@pytest.fixture()
def tg_con(tg_path):
    c = db.connect(tg_path)
    yield c
    c.close()


def test_replies_read_with_channel_peer(tg_con):
    """Регрессия: peer при чтении ответов — КАНАЛ, а не связанная группа."""
    _seed_channel(tg_con, "chan1")
    client = RecordingTG(
        messages={"chan1": [FakeMsg(101, 2, NOW)]},
        replies={("chan1", 101): [FakeReply(1, "Он же не лавров 😉", NOW),
                                  FakeReply(2, "а жаль", NOW)]},
    )
    summary = tgc.collect(
        tg_con, client=client, n_channels=5, pause=0, use_linked_chat=True,
        linked_chat_lookup=_lookup_for({"chan1": 1111}))
    assert summary["replies"] == 2
    # Тексты прочитаны с peer канала, а не с id/хендла группы.
    assert client.reply_peer_handles == ["chan1"]
    assert "1111" not in client.reply_peer_handles
    assert summary["top"][0]["linked_group"] is True


def test_no_replies_channel_skips_full_request(tg_con):
    """Нет постов с n_replies>0 — GetFullChannelRequest не слать, «ответов нет»."""
    _seed_channel(tg_con, "quiet")
    client = RecordingTG(
        messages={"quiet": [FakeMsg(101, 0, NOW), FakeMsg(102, None, NOW)]},
        replies={},
    )
    calls: list[str] = []
    summary = tgc.collect(
        tg_con, client=client, n_channels=5, pause=0, use_linked_chat=True,
        linked_chat_lookup=_lookup_for({}, calls=calls))
    assert calls == []                       # запрос «ответы возможны» не слался
    assert summary["channels_no_replies"] == 1
    assert summary["channels_no_group"] == 0
    assert summary["replies"] == 0
    assert summary["top"][0]["no_replies"] is True


def test_channel_with_replies_requests_full_and_counts_no_group(tg_con):
    """Есть пост с ответами — запрос слать, «каналов без группы» растёт."""
    _seed_channel(tg_con, "busy")
    client = RecordingTG(messages={"busy": [FakeMsg(101, 3, NOW)]}, replies={})
    calls: list[str] = []
    summary = tgc.collect(
        tg_con, client=client, n_channels=5, pause=0, use_linked_chat=True,
        linked_chat_lookup=_lookup_for({"busy": None}, calls=calls))
    assert calls == ["busy"]                 # запрос на канал с ответами был
    assert summary["channels_no_group"] == 1
    assert summary["channels_no_replies"] == 0
    assert summary["top"][0]["linked_group"] is False


def test_msgid_invalid_does_not_abort_run(tg_con):
    """MsgIdInvalidError — ошибка канала, остальные каналы продолжают."""
    _seed_channel(tg_con, "bad", engagement=50.0, msg_id=101)
    _seed_channel(tg_con, "good", engagement=10.0, msg_id=201)
    client = RecordingTG(
        messages={"bad": [FakeMsg(101, 1, NOW)],
                  "good": [FakeMsg(201, 1, NOW)]},
        replies={("good", 201): [FakeReply(1, "работает?", NOW)]},
        invalid_replies_on={"bad"},
    )
    summary = tgc.collect(tg_con, client=client, n_channels=5, pause=0)
    assert summary["errors"] == 1
    assert summary["replies"] == 1
    # Ответ «хорошего» канала записан, несмотря на сбой соседа.
    assert tg_con.execute(
        "SELECT COUNT(*) AS n FROM content_comment").fetchone()["n"] == 1
    assert summary["channels"] == 2
