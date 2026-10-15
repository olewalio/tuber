"""ТЗ-B9: единый гейт записи (D-43), канал владельца (D-69), инкремент (D-42).

Сеть и боевая база не нужны:

* гейт проверяется на подставном «боевом» пути (monkeypatch ``config.PRODUCTION_DB``)
  — отказ обязан случиться ДО открытия базы, поэтому сбойные подкоманды до сети
  не доходят;
* владелец заводится в пустую единую базу и ищется перцентиль-блоком из базы
  (без переменных окружения);
* инкремент сравнивается с полным пересчётом на одних и тех же данных при
  фиксированном ``now`` — результаты обязаны совпасть.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from tuber.analysis import digest as D
from tuber.platforms.telegram import (
    classify,
    config,
    discover,
    feeds_export,
    feeds_import,
    followers,
    owner,
    prelim,
    promote,
    scoring,
    store as db,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# D-43 — единый гейт боевой базы
# ---------------------------------------------------------------------------

def test_production_gate_helper(tmp_path, monkeypatch):
    prod = tmp_path / "prod.db"
    monkeypatch.setattr(config, "PRODUCTION_DB", str(prod))
    # Боевой путь без флага — отказ с подсказкой.
    msg = db.production_gate_error(str(prod), False)
    assert msg and "--allow-production" in msg
    # С флагом — можно; копия — можно и без флага.
    assert db.production_gate_error(str(prod), True) is None
    assert db.production_gate_error(str(tmp_path / "copy.db"), False) is None


def test_discover_refuses_then_writes_with_flag(tmp_path, monkeypatch, capsys):
    prod = tmp_path / "prod.db"
    con = db.connect(str(prod))
    con.close()
    monkeypatch.setattr(config, "PRODUCTION_DB", str(prod))
    # Без флага — понятный отказ, код 2.
    assert discover.main(["--db", str(prod)]) == 2
    err = capsys.readouterr().err
    assert "боевая единая база" in err and "--allow-production" in err
    # С флагом — подкоманда отрабатывает (пустая база: пишет ноль кандидатов).
    assert discover.main(["--db", str(prod), "--allow-production"]) == 0


@pytest.mark.parametrize("argv", [
    ["scoring", "scores", "--db", "{db}"],
    ["promote", "--db", "{db}"],
    ["followers", "--db", "{db}"],
    ["prelim", "--db", "{db}"],
    ["feeds_export", "--db", "{db}"],
    ["feeds_import", "--feed", "{feed}", "--db", "{db}"],
    ["add-owner", "ownerchan", "--db", "{db}"],
])
def test_all_mutating_subcommands_gate(tmp_path, monkeypatch, capsys, argv):
    """Все изменяющие подкоманды `tuber tg` (кроме collect/init-db) гейтятся."""
    prod = tmp_path / "prod.db"
    feed = tmp_path / "feed.jsonl"
    feed.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "PRODUCTION_DB", str(prod))
    mains = {
        "scoring": scoring.main, "promote": promote.main, "followers": followers.main,
        "prelim": prelim.main, "feeds_export": feeds_export.main,
        "feeds_import": feeds_import.main, "add-owner": owner.main,
    }
    cmd = argv[0]
    args = [a.format(db=str(prod), feed=str(feed)) for a in argv[1:]]
    # scoring/followers гейтят через scoring.assert_can_write → SystemExit(2).
    try:
        rc = mains[cmd](args)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    assert rc == 2, (cmd, rc)
    err = capsys.readouterr().err
    assert "--allow-production" in err


def test_classify_gate(tmp_path, monkeypatch, capsys):
    prod = tmp_path / "prod.db"
    monkeypatch.setattr(config, "PRODUCTION_DB", str(prod))
    monkeypatch.setattr(config, "DB_PATH", str(prod))
    assert classify.main([]) == 2
    err = capsys.readouterr().err
    assert "--allow-production" in err


# ---------------------------------------------------------------------------
# D-69 — автопостановка канала владельца
# ---------------------------------------------------------------------------

def test_resolve_handle_from_env_and_error(monkeypatch):
    monkeypatch.delenv("TUBER_OWNER_CHANNEL", raising=False)
    with pytest.raises(SystemExit):
        owner.resolve_handle(None)
    monkeypatch.setenv("TUBER_OWNER_CHANNEL", "t.me/MyChan")
    assert owner.resolve_handle(None) == "MyChan"
    assert owner.normalize_handle("@other") == "other"


def test_add_owner_idempotent(tmp_path):
    con = db.connect(str(tmp_path / "owner.db"))
    try:
        r1 = owner.add_owner(con, "mychan")
        assert r1["action"] == "inserted"
        r2 = owner.add_owner(con, "@mychan")
        assert r2["action"] == "updated"
        n = con.execute(
            "SELECT COUNT(*) FROM source WHERE platform='telegram' "
            "AND lower(handle)='mychan'").fetchone()[0]
        assert n == 1, "повторный add-owner создал дубль"
        row = con.execute(
            "SELECT status, source_kind, read_mode FROM source "
            "WHERE lower(handle)='mychan'").fetchone()
        assert tuple(row) == ("active", "owner", "web")
    finally:
        con.close()


def _seed_owner_posts(con, sid, subs, values):
    con.execute("UPDATE source SET subs=? WHERE id=?", (subs, sid))
    for i, (views, reactions) in enumerate(values):
        cid = 1000 + i
        con.execute(
            "INSERT INTO content(id, platform, source_id, external_id, published_at) "
            "VALUES (?,?,?,?,?)",
            (cid, "telegram", sid, f"e{cid}", "2026-09-22 12:00:00"))
        con.execute(
            "INSERT INTO content_latest(content_id, platform, views, reactions) "
            "VALUES (?,?,?,?)", (cid, "telegram", views, reactions))


def test_percentile_block_finds_owner_from_db(tmp_path, monkeypatch):
    con = db.connect(str(tmp_path / "digest.db"))
    try:
        res = owner.add_owner(con, "myowner")
        # Субъект по умолчанию обязан находиться из базы без переменных окружения.
        monkeypatch.delenv("TUBER_SLIVKI_ME", raising=False)
        _seed_owner_posts(con, res["source_id"], 1000,
                          [(100, 5), (100, 5), (100, 5)])
        block = D.percentile_block(con, now=NOW)
        assert block, "перцентиль-блок не нашёл владельца из базы"
        assert block["subject"]["found"] is True
        assert block["subject"]["handle"] == "myowner"
        assert block["subject"]["platform"] == "telegram"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# D-42 — инкрементальный пересчёт
# ---------------------------------------------------------------------------

def _mk_scoring_db(path, now):
    con = scoring.connect(str(path))
    con.execute("INSERT INTO channels(handle,title,status,is_author) "
                "VALUES ('ch','ch','active',1)")
    cid = con.execute("SELECT id FROM channels WHERE handle='ch'").fetchone()[0]
    for i in range(8):
        text = f"t{i}"
        con.execute(
            "INSERT INTO posts(channel_id,message_id,date_utc,text,text_hash,"
            "views,reactions,is_ad) VALUES (?,?,?,?,?,?,?,0)",
            (cid, 100 + i, now.isoformat(), text,
             hashlib.sha1(text.encode()).hexdigest(), 100 + i * 10, 5 + i))
    con.commit()
    return con, cid


def _snapshot(con):
    base = sorted(tuple(r) for r in con.execute(
        "SELECT channel_id, posts_in_window, median_views, median_reactions, "
        "median_er, is_author_data FROM channel_baselines").fetchall())
    scores = sorted(tuple(r) for r in con.execute(
        "SELECT post_id, er, coalesce(eng,0), coalesce(significance,-1), anomaly "
        "FROM scores").fetchall())
    return base, scores


def test_incremental_matches_full_on_same_data(tmp_path):
    cfg = scoring.load_config()
    # Полный пересчёт на свежей базе.
    con, cid = _mk_scoring_db(tmp_path / "full.db", NOW)
    scoring.compute_baselines(con, cfg, now=NOW)
    scoring.compute_scores(con, cfg, now=NOW)
    full = _snapshot(con)
    # Тот же корпус, но инкрементом (таблицы пусты → шлюз None берёт весь корпус).
    con.execute("DELETE FROM scores")
    con.execute("DELETE FROM channel_baselines")
    con.commit()
    scoring.compute_baselines(con, cfg, now=NOW, incremental=True)
    scoring.compute_scores(con, cfg, now=NOW, incremental=True)
    inc = _snapshot(con)
    assert full == inc, "инкремент не совпал с полным пересчётом на одних данных"
    # Повторный инкремент на неизменных данных — тихий no-op.
    con.execute("DELETE FROM scores")
    con.execute("DELETE FROM channel_baselines")
    con.commit()
    con.close()


def test_incremental_second_run_is_noop(tmp_path):
    cfg = scoring.load_config()
    con, cid = _mk_scoring_db(tmp_path / "inc.db", NOW)
    scoring.compute_baselines(con, cfg, now=NOW)
    scoring.compute_scores(con, cfg, now=NOW)
    before = _snapshot(con)
    b = scoring.compute_baselines(con, cfg, now=NOW, incremental=True)
    s = scoring.compute_scores(con, cfg, now=NOW, incremental=True)
    assert s["posts_selected"] == 0, "инкремент заново пересчитал уже оценённые посты"
    assert b["baselines_written"] == 0
    assert before == _snapshot(con)
    con.close()
