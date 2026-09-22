"""ТЗ-47: CLI ``tuber graph first-movers`` — запись, dry-run, гейт боевой базы."""
from __future__ import annotations

import json
from pathlib import Path

from tuber import config
from tuber.core import db, schema
from tuber.tools import graph_cmd

from tests.graph.test_firstmovers import _scenario


def _make_db(tmp_path):
    path = str(tmp_path / "cli.db")
    con = db.connect(path)
    schema.init_schema(con)
    _scenario(con)
    con.close()
    return path


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = graph_cmd.main(["first-movers", "--db", path, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Граф первопроходцев" in out
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM first_mover").fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM score WHERE first_mover IS NOT NULL").fetchone()[0] == 0
    finally:
        con.close()


def test_write_fills_axes(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = graph_cmd.main(["first-movers", "--db", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "записей реестра" in out
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM first_mover").fetchone()[0] > 0
        assert con.execute(
            "SELECT COUNT(*) FROM score WHERE first_mover > 0").fetchone()[0] > 0
        assert con.execute(
            "SELECT COUNT(*) FROM run_log WHERE platform='graph'").fetchone()[0] == 1
    finally:
        con.close()


def test_json_output(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = graph_cmd.main(["first-movers", "--db", path, "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["report"]["window_days"] == 30
    assert "stats" in payload


def test_production_gate_blocks_without_flag(tmp_path, capsys, monkeypatch):
    path = _make_db(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", Path(path))
    rc = graph_cmd.main(["first-movers", "--db", path])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--allow-production" in err
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM first_mover").fetchone()[0] == 0
    finally:
        con.close()


def test_production_gate_allows_with_flag(tmp_path):
    path = _make_db(tmp_path)
    import tuber.config as cfg
    cfg.DEFAULT_DB_PATH = Path(path)
    try:
        rc = graph_cmd.main(["first-movers", "--db", path, "--allow-production"])
    finally:
        cfg.DEFAULT_DB_PATH = Path(config.ROOT) / "data" / "tuber.db"
    assert rc == 0
