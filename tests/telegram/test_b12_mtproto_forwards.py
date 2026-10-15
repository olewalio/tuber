"""ТЗ-B12 / D-34 и D-35: форварды через MTProto и коэффициент ``Fr``.

Сеть/учётка не нужны: подставляем асинхронный Telethon-совместимый фейк.
Проверяем:
* отбор каналов реестра по приоритету (``candidate.score_priority``, ``subs``);
* запись ``Message.forwards`` в ``posts.forwards``;
* предохранители: потолок запросов MTProto останавливает проход;
* честную остановку при FloodWait;
* окно 90 дней отсекает старое;
* ``Fr`` — монотонный коэффициент по перцентилю форвардов (D-35).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tuber.platforms.telegram import scoring as S
from tuber.platforms.telegram import store as db


def _seed(con, handle, *, msg_id=101, priority=None, subs=None,
          date="now", content=True):
    existing = con.execute(
        "SELECT id FROM source WHERE platform='telegram' AND handle=?", (handle,)
    ).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO source(platform, handle, title, status, subs, first_seen_at) "
            "VALUES('telegram', ?, ?, 'active', ?, datetime('now'))",
            (handle, f"Канал {handle}", subs),
        )
        sid = con.execute(
            "SELECT id FROM source WHERE platform='telegram' AND handle=?", (handle,)
        ).fetchone()["id"]
    else:
        sid = existing["id"]
    if priority is not None:
        con.execute(
            "INSERT INTO candidate(platform, kind, handle, score_priority, first_seen_at) "
            "VALUES('telegram', 'channel', ?, ?, datetime('now'))",
            (handle, priority),
        )
    if content:
        when = "datetime('now')" if date == "now" else "?"
        params = [sid, f"{handle}/{msg_id}"]
        if date != "now":
            params.append(date)
        con.execute(
            f"INSERT INTO content(platform, source_id, external_id, kind, published_at, text) "
            f"VALUES('telegram', ?, ?, 'post', {when}, 'текст')",
            params,
        )
    con.commit()
    return sid


class FakeMsg:
    def __init__(self, mid, forwards, age_days=0):
        self.id = mid
        self.forwards = forwards
        self.date = datetime.now(timezone.utc) - timedelta(days=age_days)


class FakeEntity:
    def __init__(self, handle):
        self.handle = handle


class FakeFloodWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class FakeTG:
    """Асинхронный Telethon-совместимый фейк.

    Разделяет два способа чтения, как в боевом сценарии (причина 1 ТЗ-D-34):
      * ``messages[handle]`` — то, что вернул бы СТАРЫЙ ``get_messages(limit=N)``
        (свежие id канала, которых в базе может не быть);
      * ``by_id[handle]`` — адресный ответ на ``get_messages(entity, ids=[...])``;
      * ``foreign[handle]`` — лишние сообщения, которые клиент отдаёт «не по заказу»
        (проверяем, что они не создают мусора).
    """

    def __init__(self, messages=None, flood_on=None, by_id=None, foreign=None):
        self.messages = messages or {}
        self.by_id = by_id or {}
        self.foreign = foreign or {}
        self.flood_on = flood_on
        self.get_entity_calls = 0
        self.get_messages_calls = 0
        self.get_messages_limits = []
        self.get_messages_ids = []

    async def get_entity(self, handle):
        self.get_entity_calls += 1
        if self.flood_on == handle:
            raise FakeFloodWait(17)
        return FakeEntity(handle)

    async def get_messages(self, entity=None, ids=None, limit=None):
        self.get_messages_calls += 1
        self.get_messages_limits.append(limit)
        self.get_messages_ids.append(list(ids) if ids is not None else None)
        h = entity.handle if isinstance(entity, FakeEntity) else entity
        pool = dict(self.by_id.get(h, {}))
        for m in self.messages.get(h, []):
            pool.setdefault(m.id, m)
        if ids is not None:
            out = [pool.get(i) for i in ids]
            out.extend(self.foreign.get(h, []))
            return out
        return list(self.messages.get(h, []))[:limit]


@pytest.fixture()
def tg_con(tg_path):
    c = db.connect(tg_path)
    yield c
    c.close()


def test_select_priority_channels_orders_by_candidate_priority(tg_con):
    _seed(tg_con, "low", priority=0.1, subs=10)
    _seed(tg_con, "high", priority=5.0, subs=1)
    _seed(tg_con, "nosubs", priority=0.0, subs=None)
    chans = S.select_priority_channels(tg_con, limit=10)
    assert [c["handle"] for c in chans][:2] == ["high", "low"]  # nosubs=0 внизу


def test_select_priority_channels_skips_checked(tg_con):
    """D-34 §2.2: канал со СВЕЖИМ маркером проверки не выбирается повторно.

    Маркер, а не «forwards IS NOT NULL»: счётчика может не быть вовсе.
    """
    _seed(tg_con, "done", priority=9.0, subs=100, msg_id=1)
    _seed(tg_con, "todo", priority=1.0, subs=1, msg_id=2)
    S.ensure_forwards_check_schema(tg_con)
    tg_con.execute(
        "INSERT INTO tg_forwards_check(channel_id, checked_at, window_days, posts_seen, posts_filled) "
        "SELECT id, datetime('now'), 90, 0, 0 FROM source WHERE handle='done'")
    tg_con.commit()
    handles = [c["handle"] for c in S.select_priority_channels(tg_con, limit=10)]
    assert "done" not in handles
    assert "todo" in handles


def test_select_priority_channels_skips_dead_channel(tg_con):
    """D-34 §2.3: канал без постов внутри окна в выборку не попадает."""
    _seed(tg_con, "alive", msg_id=1)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _seed(tg_con, "dead", msg_id=2, date=old)
    handles = [c["handle"] for c in S.select_priority_channels(tg_con, limit=10)]
    assert "alive" in handles
    assert "dead" not in handles


def test_backfill_writes_forwards(tg_con):
    """D-34 §2.1: адресное чтение пишет форварды для id, которые ЕСТЬ в content."""
    _seed(tg_con, "chan1", msg_id=101)
    # «Последние» сообщения канала — 999, которого в базе нет; в базе есть 101.
    client = FakeTG(
        messages={"chan1": [FakeMsg(999, 99)]},
        by_id={"chan1": {101: FakeMsg(101, 42)}},
        foreign={"chan1": [FakeMsg(999, 7)]},
    )
    summary = S.backfill_forwards_mtproto(tg_con, client=client, pause=0)
    assert summary["channels_processed"] == 1
    assert summary["forwards_filled"] == 1  # 101 есть в content, 999 — нет
    assert summary["messages_seen"] == 2    # 101 (в базе) + 999 (лишний от клиента)
    # Адресное чтение: спросили ровно те id, что лежат в content, а не «последние».
    assert client.get_messages_ids == [[101]]
    assert client.get_messages_limits == [None]
    row = tg_con.execute(
        "SELECT forwards FROM posts WHERE message_id=101").fetchone()
    assert row["forwards"] == 42
    # несуществующий пост не создаёт мусора
    assert tg_con.execute("SELECT COUNT(*) AS n FROM content").fetchone()["n"] == 1
    # канал помечен проверенным (маркер D-34 §2.2)
    chk = tg_con.execute(
        "SELECT posts_seen, posts_filled FROM tg_forwards_check").fetchone()
    assert chk["posts_seen"] == 2 and chk["posts_filled"] == 1


def test_backfill_respects_request_budget(tg_con):
    for i in range(10):
        _seed(tg_con, f"chan{i}", msg_id=100 + i)
    client = FakeTG(by_id={f"chan{i}": {100 + i: FakeMsg(100 + i, i)} for i in range(10)})
    summary = S.backfill_forwards_mtproto(tg_con, client=client, pause=0,
                                          max_requests=3)
    assert summary["stopped"] == "request_budget"
    assert summary["requests"] <= 3
    assert summary["channels_processed"] < 10
    assert summary["channels_left"] > 0


def test_backfill_flood_wait_stops(tg_con):
    _seed(tg_con, "chan1", msg_id=101)
    _seed(tg_con, "chan2", msg_id=102)
    client = FakeTG(by_id={"chan2": {102: FakeMsg(102, 5)}}, flood_on="chan1")
    summary = S.backfill_forwards_mtproto(tg_con, client=client, pause=0)
    assert summary["stopped"] == "flood_wait"
    assert summary["flood_wait"] == 17
    assert summary["requests"] >= 1


def test_backfill_skips_older_than_window(tg_con):
    _seed(tg_con, "chan1", msg_id=101)
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    _seed(tg_con, "chan1", msg_id=55, date=old, content=True)
    client = FakeTG(by_id={"chan1": {101: FakeMsg(101, 9), 55: FakeMsg(55, 3)}})
    S.backfill_forwards_mtproto(tg_con, client=client, days=90, pause=0)
    assert client.get_messages_ids == [[101]]  # 55 вне окна — id даже не спрашиваем
    assert tg_con.execute(
        "SELECT forwards FROM posts WHERE message_id=101").fetchone()["forwards"] == 9
    assert tg_con.execute(
        "SELECT forwards FROM posts WHERE message_id=55").fetchone()["forwards"] is None


def test_checked_channel_returns_after_recheck_window(tg_con):
    """D-34 §2.2: канал со счётчиком=None всё равно помечен и не выбирается вечно."""
    _seed(tg_con, "chan1", msg_id=101)
    # Все forwards is None: значение не пишется, но канал должен быть отмечен.
    client = FakeTG(by_id={"chan1": {101: FakeMsg(101, None)}})
    summary = S.backfill_forwards_mtproto(tg_con, client=client, pause=0)
    assert summary["forwards_filled"] == 0
    assert tg_con.execute(
        "SELECT forwards FROM posts WHERE message_id=101").fetchone()["forwards"] is None
    assert "chan1" not in [c["handle"] for c in S.select_priority_channels(tg_con)]
    # Истекло окно повторной проверки (30 суток) → канал снова в пуле.
    tg_con.execute(
        "UPDATE tg_forwards_check SET checked_at=datetime('now','-31 day')"
        " WHERE channel_id=(SELECT id FROM source WHERE handle='chan1')")
    tg_con.commit()
    assert "chan1" in [c["handle"] for c in S.select_priority_channels(tg_con)]


def test_dry_run_does_not_touch_schema(tg_con):
    """--dry-run не пишет в базу: маркер не создаётся, запросов нет."""
    _seed(tg_con, "chan1", msg_id=101)
    out = S.backfill_forwards_mtproto(tg_con, dry=True)
    assert out["dry_run"] is True
    assert out["channels_selected"] == 1
    n = tg_con.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master "
        "WHERE type='table' AND name='tg_forwards_check'").fetchone()["n"]
    assert n == 0, "dry-run создал таблицу-маркер (побочная запись)"


def test_backfill_same_value_is_idempotent(tg_con):
    """D-34 §3: то же значение форвардов не создаёт шумовых строк metric_snapshot."""
    import asyncio

    sid = _seed(tg_con, "chan1", msg_id=101)
    client = FakeTG(by_id={"chan1": {101: FakeMsg(101, 42)}})
    S.backfill_forwards_mtproto(tg_con, client=client, pause=0)
    snaps1 = tg_con.execute("SELECT COUNT(*) AS n FROM metric_snapshot").fetchone()["n"]
    # Повторный адресный проход с тем же значением (канал уже вне пула — зовём
    # внутреннюю функцию напрямую, чтобы проверить именно идемпотентность записи).
    summary = asyncio.run(S._backfill_forwards_mtproto_async(
        tg_con, [{"id": sid, "handle": "chan1"}], client=client, days=90,
        messages_per_channel=100, max_requests=10, pause=0))
    snaps2 = tg_con.execute("SELECT COUNT(*) AS n FROM metric_snapshot").fetchone()["n"]
    assert summary["forwards_filled"] == 0
    assert snaps2 == snaps1, "повторная запись того же значения создала шум"
    assert tg_con.execute(
        "SELECT forwards FROM posts WHERE message_id=101").fetchone()["forwards"] == 42


def test_entity_error_recorded_in_marker(tg_con):
    """D-34 §2.4: упавший на get_entity канал помечается и уходит из пула."""
    _seed(tg_con, "bad", msg_id=1)

    class BoomTG(FakeTG):
        async def get_entity(self, handle):
            raise ValueError("Cannot find any entity corresponding to bad")

    summary = S.backfill_forwards_mtproto(tg_con, client=BoomTG(), pause=0)
    assert summary["errors"] == 1
    row = tg_con.execute(
        "SELECT note, posts_seen FROM tg_forwards_check").fetchone()
    assert row is not None and row["posts_seen"] == 0
    assert "get_entity" in row["note"]
    assert "bad" not in [c["handle"] for c in S.select_priority_channels(tg_con)]


def test_forwards_percentile_map_monotonic(tg_con):
    _seed(tg_con, "chan1", msg_id=1)
    _seed(tg_con, "chan1", msg_id=2)
    _seed(tg_con, "chan1", msg_id=3)
    # forwards: 0, 5, 100
    ids = {}
    for mid, f in ((1, 0), (2, 5), (3, 100)):
        con_id = tg_con.execute(
            "SELECT id FROM content WHERE external_id=?", (f"chan1/{mid}",)
        ).fetchone()["id"]
        tg_con.execute(
            "INSERT INTO content_latest(content_id, captured_at, forwards) "
            "VALUES(?, datetime('now'), ?)", (con_id, f))
        ids[mid] = con_id
    tg_con.commit()
    fr, n = S.forwards_percentile_map(tg_con)
    assert n == 3
    assert fr[ids[1]] < fr[ids[2]] < fr[ids[3]]
    assert all(1.0 <= v <= 2.0 for v in fr.values())


def test_forwards_percentile_map_empty_is_neutral(tg_con):
    _seed(tg_con, "chan1", msg_id=1)
    fr, n = S.forwards_percentile_map(tg_con)
    assert n == 0 and fr == {}


def test_compute_scores_applies_fr(tg_con):
    """D-35: Fr множит significance (порядок меняется в пользу форвардного поста)."""
    cfg = S.load_config()
    _seed(tg_con, "chan1", msg_id=1, content=False)
    sid = tg_con.execute(
        "SELECT id FROM source WHERE handle='chan1'").fetchone()["id"]
    same = "2026-09-23T00:00:00+00:00"
    for mid, fwd in ((1, 0), (2, 0), (3, 0), (4, 0), (5, 1000)):
        tg_con.execute(
            "INSERT INTO content(platform, source_id, external_id, kind, published_at, text) "
            "VALUES('telegram', ?, ?, 'post', ?, ?)",
            (sid, f"chan1/{mid}", same, f"u{mid}"),
        )
        cid = tg_con.execute(
            "SELECT id FROM content WHERE external_id=?", (f"chan1/{mid}",)
        ).fetchone()["id"]
        tg_con.execute(
            "INSERT INTO content_latest(content_id, captured_at, views, reactions, forwards) "
            "VALUES(?, datetime('now'), 100, 10, ?)", (cid, fwd))
    tg_con.commit()
    S.compute_baselines(tg_con, cfg)
    S.compute_scores(tg_con, cfg)
    rows = {r["message_id"]: r for r in tg_con.execute(
        "SELECT p.message_id AS message_id, s.fr AS fr, s.significance AS sig "
        "FROM scores s JOIN posts p ON p.id=s.post_id")}
    assert rows[5]["fr"] > rows[1]["fr"]
    assert rows[5]["sig"] > rows[1]["sig"]
    assert abs(rows[5]["sig"] / rows[1]["sig"]
               - rows[5]["fr"] / rows[1]["fr"]) < 1e-9
