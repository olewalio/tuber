"""ТЗ-B10: очереди и точные подписчики (D-60, D-61, D-62, D-68).

Сеть отсутствует: сборщики свидетельств и MTProto-клиент подменяются фейками.
"""
from __future__ import annotations

import asyncio
import json

from tuber.analysis import queue as q
from tuber.core import db, schema
from tuber.platforms.telegram import exact_subs as es
from tuber.platforms.telegram import promote as tg_promote


def _db(tmp_path, name="b10.db"):
    path = str(tmp_path / name)
    con = db.connect(path)
    schema.init_schema(con)
    return path, con


def _add_candidate(con, platform, handle, *, kind="channel", external_id=None,
                   status="new", meta=None, score_priority=0, distinct_sources=1):
    con.execute(
        "INSERT INTO candidate(platform, handle, external_id, kind, status, "
        "meta_json, score_priority, distinct_sources) VALUES (?,?,?,?,?,?,?,?)",
        (platform, handle, external_id or handle, kind, status,
         json.dumps(meta) if meta else None, score_priority, distinct_sources))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


class FakeFetchers:
    """Сборщики без сети; пишет, какие id видео/аккаунтов запрошены."""

    def __init__(self, *, tg=None, yt=None, x=None, x_budget=None, workers=2,
                 video_map=None, units_limit=100):
        self.tg = tg or {}
        self.yt = yt or {}
        self.xmap = x or {}
        self.x_budget = x_budget
        self.workers = workers
        self.video_map = video_map or {}
        self.units_limit = units_limit
        self.video_calls: list[list[str]] = []
        self.x_calls: list[str] = []
        self.quota = 1000

    def telegram(self, handle):
        return dict(self.tg.get(handle, {"outcome": "no_feed", "posts30": 0}))

    def youtube(self, ids):
        return {i: dict(self.yt.get(i, {"exists": False})) for i in ids}

    def youtube_resolve_videos(self, ids, *, max_units=None):
        self.video_calls.append(list(ids))
        limit = self.units_limit if max_units is None else max_units
        out = {}
        units = 0
        ids = [str(i) for i in ids if str(i).strip()]
        for i in range(0, len(ids), 50):
            if units >= limit or limit <= 0:
                break
            units += 1  # 1 unit за батч
            for vid in ids[i:i + 50]:
                cid = self.video_map.get(vid)
                if cid:
                    out[vid] = cid
        return out, units

    def quota_units_today(self):
        return self.quota

    def x(self, handle):
        self.x_calls.append(handle)
        return dict(self.xmap.get(handle, {"outcome": "gone"}))


# ---------------------------------------------------------------------------
# D-60: очередь YouTube — не-аккаунты
# ---------------------------------------------------------------------------

def test_d60_video_id_resolves_to_channel(tmp_path):
    path, con = _db(tmp_path)
    cid = _add_candidate(con, "youtube", "vid00000001", kind="youtube",
                         external_id="vid00000001")
    con.commit()
    fetchers = FakeFetchers(
        video_map={"vid00000001": "UCreal"},
        yt={"UCreal": {"exists": True, "subs": 5000, "videos30": 6,
                       "video_ids": ["a"]}},
        units_limit=100)
    rev = q.QueueReviewer(con, fetchers=fetchers, platforms=("youtube",))
    summary = rev.run()
    row = con.execute("SELECT status, external_id, meta_json FROM candidate "
                      "WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "promoted"
    assert row["external_id"] == "UCreal"  # id видео превращён в канал
    ev = json.loads(row["meta_json"])["review"]["evidence"]
    assert ev["channel_id"] == "UCreal" and ev["video_id"] == "vid00000001"
    assert summary["youtube_queue"]["videos_resolved"] == 1
    assert summary["youtube_queue"]["units_spent"] == 1
    con.close()


def test_d60_video_resolve_respects_units_limit(tmp_path, monkeypatch):
    from tuber.platforms.youtube import config as yt_config

    monkeypatch.setattr(yt_config, "YOUTUBE_VIDEO_RESOLVE_UNITS_LIMIT", 1)
    path, con = _db(tmp_path)
    # 60 видео: при лимите 1 unit разрешается только первый батч (50).
    for i in range(60):
        _add_candidate(con, "youtube", f"vid{i:08d}", kind="youtube",
                       external_id=f"vid{i:08d}")
    con.commit()
    video_map = {f"vid{i:08d}": f"UC{i}" for i in range(60)}
    fetchers = FakeFetchers(video_map=video_map, units_limit=1,
                            yt={f"UC{i}": {"exists": True, "subs": 100,
                                           "videos30": 5} for i in range(60)})
    rev = q.QueueReviewer(con, fetchers=fetchers, platforms=("youtube",))
    summary = rev.run()
    yq = summary["youtube_queue"]
    assert yq["units_spent"] == 1
    assert yq["videos_resolved"] == 50
    assert yq["videos_remaining"] == 10
    # Непроверенные честно уходят в hold, а не в reject.
    assert con.execute("SELECT COUNT(*) FROM candidate WHERE status='hold'").fetchone()[0] == 10
    con.close()


def test_d60_query_candidates_routed_not_rejected(tmp_path):
    path, con = _db(tmp_path)
    _add_candidate(con, "youtube", "AI agents", kind="query",
                   external_id="AI agents")
    con.commit()
    rev = q.QueueReviewer(con, fetchers=FakeFetchers(), platforms=("youtube",))
    summary = rev.run()
    row = con.execute("SELECT status, reject_reason, meta_json FROM candidate").fetchone()
    assert row["status"] == "hold"          # НЕ reject
    assert "expand" in row["reject_reason"]
    ev = json.loads(row["meta_json"])["review"]["evidence"]
    assert ev["kind"] == "query"
    assert summary["youtube_queue"]["queries_routed"] == 1
    con.close()


def test_d60_networkfetcher_resolver_batches_and_caps(tmp_path):
    path, con = _db(tmp_path)
    nf = q.NetworkFetchers(con=con, db_path=path)

    class FakeYT:
        def __init__(self):
            self.calls = 0

        def videos_by_ids(self, ids, parts="snippet"):
            self.calls += 1
            return [{"id": i, "snippet": {"channelId": "UC" + i}}
                    for i in ids]

    fake = FakeYT()
    nf._yt = fake
    ids = [f"v{i}" for i in range(120)]
    mp, units = nf.youtube_resolve_videos(ids, max_units=2)
    assert units == 2 and fake.calls == 2
    assert len(mp) == 100
    con.close()


# ---------------------------------------------------------------------------
# D-61: курсор обхода X-очереди
# ---------------------------------------------------------------------------

def test_d61_cursor_advances_and_no_recheck(tmp_path):
    path, con = _db(tmp_path)
    handles = [f"acc{i}" for i in range(5)]
    for i, h in enumerate(handles):
        _add_candidate(con, "x", h, score_priority=100 - i)
    con.commit()

    good = {h: {"outcome": "ok", "subs": 2000, "posts30": 4, "median_likes": 50}
            for h in handles}
    f1 = FakeFetchers(x=good, x_budget=2)
    rev1 = q.QueueReviewer(con, fetchers=f1, platforms=("x",))
    s1 = rev1.run()
    assert s1["x_queue"]["checked"] == 2
    assert len(f1.x_calls) == 2

    # Курсор в базе указывает на следующего.
    cur = con.execute("SELECT cursor, items_total FROM cursor WHERE platform='x' "
                      "AND kind=?", (q.X_CURSOR_KIND,)).fetchone()
    assert cur is not None
    payload = json.loads(cur["cursor"])
    assert payload["offset"] == 2
    assert cur["items_total"] == 5

    f2 = FakeFetchers(x=good, x_budget=2)
    rev2 = q.QueueReviewer(con, fetchers=f2, platforms=("x",))
    s2 = rev2.run()
    assert len(f2.x_calls) == 2
    assert set(f2.x_calls).isdisjoint(set(f1.x_calls))  # тех же не переспрашивает
    assert s2["x_queue"]["offset"] == 4
    con.close()


def test_d61_reports_projection_numbers(tmp_path):
    path, con = _db(tmp_path)
    for i in range(10):
        _add_candidate(con, "x", f"a{i}")
    con.commit()
    rev = q.QueueReviewer(con, fetchers=FakeFetchers(x_budget=2), platforms=("x",))
    summary = rev.run()
    xq = summary["x_queue"]
    assert xq["checked"] == 2
    assert xq["per_day"] == 800          # 800 запросов / 1 на аккаунт
    assert xq["remaining"] == 8
    assert xq["days"] == 1               # 8 <= 800
    con.close()


def test_d61_budget_zero_keeps_position(tmp_path):
    path, con = _db(tmp_path)
    for i in range(3):
        _add_candidate(con, "x", f"a{i}")
    con.commit()
    rev = q.QueueReviewer(con, fetchers=FakeFetchers(x_budget=0), platforms=("x",))
    summary = rev.run()
    assert summary["by_platform"]["x"] == {q.HOLD: 3}
    assert summary["x_queue"]["offset"] == 0
    con.close()


# ---------------------------------------------------------------------------
# D-62: точные подписчики при JS-оболочке (разбор ответа)
# ---------------------------------------------------------------------------

class _Entity:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_d62_parse_full_channel_exact_subs():
    entity = _Entity(id=42, title="Канал")
    full_chat = _Entity(participants_count=10734, about="описание")
    res = es.parse_full_channel("chan", entity, full_chat)
    assert res == {"handle": "chan", "tg_id": 42, "subs": 10734,
                   "title": "Канал", "about": "описание"}
    # Нет счётчика — None, не выдумываем число.
    res2 = es.parse_full_channel("chan", entity, _Entity(participants_count=None))
    assert res2["subs"] is None


def test_d62_apply_exact_subs_writes_candidate_and_divergence(tmp_path):
    path, con = _db(tmp_path)
    _add_candidate(con, "telegram", "chan", status="hold", meta={
        "review": {"verdict": "hold",
                   "evidence": {"subs": 10700, "subs_source": "rounded"}}})
    con.commit()
    handles, targets = es.collect_targets(con)
    assert handles == ["chan"] and targets["chan"]["rounded"] == 10700
    res = {"chan": {"handle": "chan", "tg_id": 42, "subs": 10734, "title": "t"}}
    out = es.apply_exact_subs(con, res, targets)
    assert out["filled_candidates"] == 1
    assert out["divergences"] == [(10734, 10700)]
    ev = json.loads(con.execute("SELECT meta_json FROM candidate").fetchone()[0])
    ev = ev["review"]["evidence"]
    assert ev["subs"] == 10734 and ev["subs_source"] == "exact"
    assert ev["subs_rounded"] == 10700
    con.close()


def test_d62_resolve_flood_wait_stops(tmp_path):
    class Flood(Exception):
        seconds = 42

    Flood.__name__ = "FloodWaitError"

    class Client:
        def __init__(self):
            self.calls = 0

        async def get_entity(self, h):
            self.calls += 1
            raise Flood()

    results, stats = asyncio.run(es._resolve_async(
        ["a", "b", "c"], client=Client(), limit=1000, pause=0, sleep=lambda s: None))
    assert stats["stopped"] == "flood_wait"
    assert stats["flood_wait"] == 42
    assert stats["resolved"] == 0


# ---------------------------------------------------------------------------
# D-68: заполнение subs и пересчёт vr (тот же проход)
# ---------------------------------------------------------------------------

def _add_post(con, source_id, ext, views):
    con.execute(
        "INSERT INTO content(platform, source_id, external_id, published_at, "
        "meta_json) VALUES('telegram', ?, ?, '2026-09-20T00:00:00', ?)",
        (source_id, ext, json.dumps({"views": views})))


def test_d68_fills_subs_and_recomputes_vr(tmp_path):
    path, con = _db(tmp_path)
    con.execute("INSERT INTO source(platform, handle, status, subs) "
                "VALUES('telegram','chan_x','candidate',NULL)")
    sid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    for i, v in enumerate((10000, 20000, 30000)):  # медиана 20000
        _add_post(con, sid, f"chan_x/{i}", v)
    con.commit()

    handles, targets = es.collect_targets(con)
    assert handles == ["chan_x"]
    res = {"chan_x": {"handle": "chan_x", "tg_id": 7, "subs": 100000, "title": "T"}}
    out = es.apply_exact_subs(con, res, targets)
    assert out["filled_sources"] == 1
    assert out["vr_ok"] == 1
    assert out["now_pass_vr"] == 1      # vr = 20 % >= 15 — проходит
    # vr = 20000/100000*100 = 20 %
    row = con.execute("SELECT subs, vr, avg_views FROM source WHERE id=?", (sid,)).fetchone()
    assert row["subs"] == 100000
    assert row["vr"] == 20.0
    assert row["avg_views"] == 20000
    con.close()


def test_d68_vr_below_threshold_not_counted(tmp_path):
    path, con = _db(tmp_path)
    con.execute("INSERT INTO source(platform, handle, status, subs) "
                "VALUES('telegram','weak','candidate',NULL)")
    sid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    for i, v in enumerate((100, 200, 300)):  # медиана 200; subs 100000 -> vr 0.2 %
        _add_post(con, sid, f"weak/{i}", v)
    con.commit()
    handles, targets = es.collect_targets(con)
    out = es.apply_exact_subs(con, {"weak": {"subs": 100000}}, targets)
    assert out["vr_ok"] == 1
    assert out["now_pass_vr"] == 0
    # порог повышения — из конфига promote
    assert tg_promote.config.PROMOTE_MIN_VR == 15.0
    con.close()


def test_d68_run_pass_end_to_end_no_network(tmp_path):
    path, con = _db(tmp_path)
    con.execute("INSERT INTO source(platform, handle, status, subs) "
                "VALUES('telegram','e2e','candidate',NULL)")
    sid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    for i, v in enumerate((1000, 2000, 3000)):
        _add_post(con, sid, f"e2e/{i}", v)
    con.commit()

    def fake_resolver(handles, *, client=None, limit=None, pause=None, sleep=None,
                      log=None):
        assert handles == ["e2e"]
        return ({"e2e": {"handle": "e2e", "tg_id": 1, "subs": 10000,
                         "title": "E2E"}},
                {"requested": 1, "resolved": 1, "not_found": 0, "errors": 0,
                 "done": 1, "flood_wait": None, "stopped": None})

    summary = es.run_pass(con, resolver=fake_resolver)
    assert summary["without_subs_before"] == 1
    assert summary["resolved"] == 1
    assert summary["filled_sources"] == 1
    assert summary["still_without_subs"] == 0
    assert summary["remaining"] == 0
    assert con.execute("SELECT subs FROM source WHERE id=?", (sid,)).fetchone()[0] == 10000
    con.close()
