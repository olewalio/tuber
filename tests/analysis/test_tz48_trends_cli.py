"""ТЗ-48: CLI ``tuber trends inside`` / ``tuber trends novelties``.

Проверяем: выдача печатается, ``--json`` отдаёт машинный вид, ``--no-network``
не ходит наружу, ``--dry-run`` ничего не пишет, а ``--save`` на боевой базе
запрещён без ``--allow-production`` (урок ТЗ-45F).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tuber import cli, config
from tuber.analysis import trends
from tuber.core import db, schema

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _make_db(tmp_path):
    path = str(tmp_path / "cli.db")
    con = db.connect(path)
    schema.init_schema(con)
    con.execute("INSERT INTO story(id, platform, title, entities)"
                " VALUES (1,'x','story','[\"gpt\"]')")
    platforms = {1: "telegram", 2: "telegram", 3: "telegram",
                 4: "x", 5: "x", 6: "x"}
    for sid, platform in platforms.items():
        con.execute("INSERT INTO source(id, platform, handle, status, subs)"
                    " VALUES (?,?,?,?,?)", (sid, platform, f"ch{sid}", "active", 1000))
    cid = 1
    for i in range(24):
        if i < 6:
            sid = i + 1
            cid_platform = platforms[sid]
            text = "gpt release"
        else:
            sid = 1
            cid_platform = "telegram"
            text = "filler"
        con.execute("INSERT INTO content(id, platform, source_id, external_id,"
                    " published_at, text) VALUES (?,?,?,?,?,?)",
                    (cid, cid_platform, sid, f"e{cid}",
                     (NOW - timedelta(hours=1, minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), text))
        cid += 1
    for i in range(10):
        con.execute("INSERT INTO content(id, platform, source_id, external_id,"
                    " published_at, text) VALUES (?,?,?,?,?,?)",
                    (cid, "x", 4, f"e{cid}",
                     (NOW - timedelta(hours=8, minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
                     "gpt old" if i == 0 else "filler"))
        cid += 1
    con.commit()
    con.close()
    return path


def test_inside_prints_report(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = cli.main(["trends", "inside", "--db", path, "--now", "2026-09-21 12:00:00"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Тренд внутри тренда" in out
    assert "accel_sub" in out


def test_inside_json(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = cli.main(["trends", "inside", "--db", path, "--now", "2026-09-21 12:00:00", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["window_hours"] == 6
    assert any(r["entity"] == "gpt" for r in data["trending"])


def test_novelties_no_network(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = cli.main(["trends", "novelties", "--db", path, "--no-network",
                   "--now", "2026-09-21 12:00:00"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "внешний контур" in out
    assert "НЕ ОТВЕТИЛ" in out


def test_novelties_dry_run_writes_nothing(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = cli.main(["trends", "novelties", "--db", path, "--dry-run", "--save"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["dry_run"] is True
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM novelty").fetchone()[0] == 0
    finally:
        con.close()


def test_save_gate_blocks_production(tmp_path, capsys, monkeypatch):
    path = _make_db(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", Path(path))
    rc = trends.main(["novelties", "--db", path, "--save", "--no-network",
                      "--now", "2026-09-21 12:00:00"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--allow-production" in err
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM novelty").fetchone()[0] == 0
    finally:
        con.close()


def test_save_writes_rows_on_copy(tmp_path, capsys):
    path = _make_db(tmp_path)
    con = db.connect(path)
    data = {
        "strict": [],
        "external": [{
            "entity": "zcode", "tier": "external", "new_internal": False,
            "internal_sources": 2, "internal_platforms": ["telegram", "x"],
            "external_sources": 1, "external_platforms": ["github"],
            "total_sources": 3, "internal_url": "https://t.me/x/1",
            "external_items": [{"platform": "github", "title": "zcode", "url": "https://github.com/z",
                                "score": 10, "comments": None, "published_at": "2026-09-21 09:00:00"}],
            "external_score": 10.0,
        }],
    }
    written = trends.save_novelties(con, data, now=NOW)
    con.close()
    assert written == 1
    con = db.connect(path)
    try:
        row = con.execute("SELECT entity, tier, external_url FROM novelty").fetchone()
        assert row[0] == "zcode" and row[1] == "external"
        assert row[2] == "https://github.com/z"
    finally:
        con.close()
