"""ТЗ-46: разбор очереди кандидатов и миля повышения.

Проверяем ядро контура на синтетической базе с инъекцией сборщиков
свидетельств (сети нет):

* у каждого кандидата появляется вердикт ``promote``/``hold``/``reject`` с
  причиной и датой разбора;
* дубль известного источника и мёртвый источник уходят в ``reject``;
* Telegram/YouTube повышаются по числу постов/видео за 30 дней, X — по
  постам и медиане лайков, иначе ``hold``;
* ``queue promote`` активирует источник, идемпотентен и по ``--dry-run`` не
  пишет;
* ``active`` против ``provisional`` — по подписчикам;
* сухой прогон разбора ничего не пишет;
* гейт боевой базы: без ``--allow-production`` запись запрещена.
"""
from __future__ import annotations

import json

from tuber import cli, config
from tuber.analysis import queue as q
from tuber.core import db, schema


def _db(tmp_path, name="q.db"):
    path = str(tmp_path / name)
    con = db.connect(path)
    schema.init_schema(con)
    return path, con


def _add_candidate(con, platform, handle, *, external_id=None, status="new",
                   meta=None):
    con.execute(
        "INSERT INTO candidate(platform, handle, external_id, status, meta_json)"
        " VALUES (?,?,?,?,?)",
        (platform, handle, external_id or handle, status,
         json.dumps(meta) if meta else None))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


class FakeFetchers:
    """Сборщики свидетельств без сети: детерминированные ответы."""

    def __init__(self, *, tg=None, yt=None, x=None, x_budget=None, workers=2):
        self.tg = tg or {}
        self.yt = yt or {}
        self.xmap = x or {}
        self.x_budget = x_budget
        self.workers = workers

    def telegram(self, handle):
        return dict(self.tg.get(handle, {"outcome": "no_feed", "posts30": 0}))

    def youtube(self, ids):
        return {i: dict(self.yt.get(i, {"exists": False})) for i in ids}

    def x(self, handle):
        return dict(self.xmap.get(handle, {"outcome": "gone"}))


def _review(con, **kw):
    rev = q.QueueReviewer(con, fetchers=kw.pop("fetchers"), **kw)
    return rev, rev.run()


def test_review_web_rejects_all(tmp_path):
    path, con = _db(tmp_path)
    _add_candidate(con, "web", "%d0%9f%d1%80%d0%b8%d0%bc%d0%b5%d1%80")
    con.commit()
    _, summary = _review(con, fetchers=FakeFetchers())
    assert summary["by_platform"]["web"] == {q.REJECT: 1}
    row = con.execute("SELECT status, reject_reason, validated FROM candidate").fetchone()
    assert row["status"] == "rejected"
    assert "сборщика" in row["reject_reason"]
    assert row["validated"] == "reject"
    con.close()


def test_review_every_candidate_gets_verdict(tmp_path):
    path, con = _db(tmp_path)
    _add_candidate(con, "web", "d1")
    _add_candidate(con, "telegram", "tg1")
    _add_candidate(con, "youtube", "UC1")
    _add_candidate(con, "x", "x1")
    con.commit()
    fetchers = FakeFetchers(
        tg={"tg1": {"outcome": "ok", "subs": 5000, "posts30": 4}},
        yt={"UC1": {"exists": True, "subs": 3000, "videos30": 5, "video_ids": []}},
        x={"x1": {"outcome": "ok", "subs": 2000, "posts30": 4, "median_likes": 50}},
        x_budget=10)
    _, summary = _review(con, fetchers=fetchers)
    left = con.execute("SELECT COUNT(*) FROM candidate WHERE status='new'").fetchone()[0]
    assert left == 0
    total = sum(sum(b.values()) for b in summary["by_platform"].values())
    assert total == 4
    for row in con.execute("SELECT meta_json FROM candidate"):
        rev = json.loads(row["meta_json"])["review"]
        assert rev["verdict"] in (q.PROMOTE, q.HOLD, q.REJECT)
        assert rev["reason"]
        assert rev["reviewed_at"]
    con.close()


def test_duplicate_source_rejected(tmp_path):
    path, con = _db(tmp_path)
    con.execute("INSERT INTO source(platform, handle, status) VALUES ('telegram','dup','active')")
    _add_candidate(con, "telegram", "dup")
    con.commit()
    _, summary = _review(con, fetchers=FakeFetchers())
    assert summary["by_platform"]["telegram"] == {q.REJECT: 1}
    assert "дубль" in con.execute("SELECT reject_reason FROM candidate").fetchone()[0]
    con.close()


def test_telegram_verdicts(tmp_path):
    path, con = _db(tmp_path)
    _add_candidate(con, "telegram", "alive")
    _add_candidate(con, "telegram", "few")
    _add_candidate(con, "telegram", "dead")
    _add_candidate(con, "telegram", "gone")
    con.commit()
    fetchers = FakeFetchers(tg={
        "alive": {"outcome": "ok", "subs": 12000, "posts30": 8},
        "few": {"outcome": "ok", "subs": 900, "posts30": 2},
        "dead": {"outcome": "ok", "subs": 500, "posts30": 0},
        "gone": {"outcome": "no_feed"},
    })
    _, summary = _review(con, fetchers=fetchers)
    got = {r["handle"]: r["status"] for r in
           con.execute("SELECT handle, status FROM candidate")}
    assert got == {"alive": "promoted", "few": "hold", "dead": "rejected",
                   "gone": "rejected"}
    con.close()


def test_youtube_verdicts(tmp_path):
    path, con = _db(tmp_path)
    _add_candidate(con, "youtube", "UC_alive")
    _add_candidate(con, "youtube", "UC_missing")
    _add_candidate(con, "youtube", "UC_hidden")
    con.commit()
    fetchers = FakeFetchers(yt={
        "UC_alive": {"exists": True, "subs": 8000, "videos30": 6, "video_ids": ["v1"]},
        "UC_missing": {"exists": False},
        "UC_hidden": {"exists": True, "subs": None, "videos30": 4, "video_ids": []},
    })
    _, summary = _review(con, fetchers=fetchers)
    got = {r["handle"]: r["status"] for r in
           con.execute("SELECT handle, status FROM candidate")}
    assert got == {"UC_alive": "promoted", "UC_missing": "rejected",
                   "UC_hidden": "hold"}
    con.close()


def test_x_verdicts_median_likes(tmp_path):
    path, con = _db(tmp_path)
    _add_candidate(con, "x", "hot")
    _add_candidate(con, "x", "cold")
    _add_candidate(con, "x", "notfound")
    con.commit()
    fetchers = FakeFetchers(
        x={"hot": {"outcome": "ok", "subs": 3000, "posts30": 5, "median_likes": 100},
           "cold": {"outcome": "ok", "subs": 3000, "posts30": 5, "median_likes": 3},
           "notfound": {"outcome": "gone"}},
        x_budget=10)
    _, summary = _review(con, fetchers=fetchers)
    got = {r["handle"]: r["status"] for r in
           con.execute("SELECT handle, status FROM candidate")}
    assert got == {"hot": "promoted", "cold": "hold", "notfound": "rejected"}
    con.close()


def test_x_budget_exhausted_holds(tmp_path):
    path, con = _db(tmp_path)
    for i in range(5):
        _add_candidate(con, "x", f"x{i}")
    con.commit()
    fetchers = FakeFetchers(x_budget=0)
    _, summary = _review(con, fetchers=fetchers)
    assert summary["by_platform"]["x"] == {q.HOLD: 5}
    con.close()


def test_dry_run_review_writes_nothing(tmp_path):
    path, con = _db(tmp_path)
    _add_candidate(con, "web", "d1")
    _add_candidate(con, "telegram", "tg1")
    con.commit()
    rev = q.QueueReviewer(con, fetchers=FakeFetchers(), platforms=("web", "telegram"))
    summary = q._dry_review(rev)
    assert summary["dry_run"] is True
    assert summary["by_platform"]["web"] == {q.REJECT: 1}
    # У telegram-кандидата по умолчанию нет публичной ленты → reject.
    assert summary["by_platform"]["telegram"] == {q.REJECT: 1}
    assert con.execute("SELECT COUNT(*) FROM candidate WHERE status='new'").fetchone()[0] == 2
    con.close()


# ---------------------------------------------------------------------------
# Миля повышения
# ---------------------------------------------------------------------------

def _promoted(con, platform, handle, evidence, external_id=None):
    cid = _add_candidate(con, platform, handle, status="promoted",
                         external_id=external_id,
                         meta={"review": {"verdict": q.PROMOTE,
                                          "reason": "ok", "evidence": evidence}})
    con.commit()
    return cid


def test_promote_activates_and_is_idempotent(tmp_path):
    path, con = _db(tmp_path)
    _promoted(con, "telegram", "chan", {"subs": 5000, "posts30": 10})
    _promoted(con, "youtube", "UC1", {"subs": 300, "videos30": 4}, external_id="UC1")
    con.commit()
    s1 = q.promote(con, dry_run=False)
    assert s1["promoted"] == 2
    assert s1["active"] == 1 and s1["provisional"] == 1
    assert con.execute("SELECT status FROM source WHERE platform='telegram' AND handle='chan'").fetchone()[0] == "active"
    assert con.execute("SELECT status FROM source WHERE platform='youtube' AND external_id='UC1'").fetchone()[0] == "provisional"
    assert con.execute("SELECT COUNT(*) FROM candidate WHERE promoted_at IS NOT NULL").fetchone()[0] == 2
    # Повторный прогон — ноль изменений.
    s2 = q.promote(con, dry_run=False)
    assert s2["checked"] == 0 and s2["promoted"] == 0
    assert con.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 2
    con.close()


def test_promote_dry_run_writes_nothing(tmp_path):
    path, con = _db(tmp_path)
    _promoted(con, "telegram", "chan", {"subs": 5000, "posts30": 10})
    con.commit()
    s = q.promote(con, dry_run=True)
    assert s["promoted"] == 1
    assert con.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM candidate WHERE promoted_at IS NOT NULL").fetchone()[0] == 0
    con.close()


def test_promote_skips_web_and_without_evidence(tmp_path):
    path, con = _db(tmp_path)
    _promoted(con, "web", "dom", {"subs": 5000})
    _add_candidate(con, "telegram", "noev", status="promoted")
    con.commit()
    s = q.promote(con, dry_run=False)
    assert s["promoted"] == 0
    con.close()


def test_promote_collect_first_dispatches_to_seeds(tmp_path, monkeypatch):
    path, con = _db(tmp_path)
    _promoted(con, "telegram", "chan", {"subs": 5000, "posts30": 10})
    _promoted(con, "youtube", "UC1", {"subs": 900, "videos30": 4}, external_id="UC1")
    con.commit()
    calls = {"tg": [], "yt": []}
    monkeypatch.setattr(q, "_seed_telegram",
                        lambda con, path, handles: calls["tg"].extend(handles) or len(handles))
    monkeypatch.setattr(q, "_seed_youtube",
                        lambda con, handles: calls["yt"].extend(handles) or len(handles))
    s = q.promote(con, dry_run=False, collect_first=True, db_path=path)
    assert s["seeded"] == 2
    assert calls["tg"] == ["chan"]
    assert calls["yt"] == ["UC1"]
    con.close()


def test_gate_blocks_production(tmp_path, monkeypatch, capsys):
    path, con = _db(tmp_path)
    _add_candidate(con, "web", "d1")
    con.commit()
    con.close()
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", path)
    rc = cli.main(["queue", "review", "--db", path, "--offline"])
    assert rc == 2
    assert "allow-production" in capsys.readouterr().err
    # С флагом — запись идёт.
    rc = cli.main(["queue", "review", "--db", path, "--offline", "--allow-production"])
    assert rc == 0
    con = db.connect(path)
    assert con.execute("SELECT COUNT(*) FROM candidate WHERE status='new'").fetchone()[0] == 0
    con.close()


def test_cli_report(tmp_path, capsys):
    path, con = _db(tmp_path)
    _add_candidate(con, "web", "d1")
    con.commit()
    con.close()
    rc = cli.main(["queue", "report", "--db", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Разбор очереди" in out
