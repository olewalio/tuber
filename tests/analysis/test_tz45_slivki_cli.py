"""ТЗ-45: CLI ``tuber rating slivki`` — выдача, снимок, гейт боевой базы.

Проверяет, что команда ``python3 -m tuber rating slivki`` (без подкоманды) даёт
выдачу, ``capture`` пишет снимок, ``--dry-run`` не пишет ничего, а на боевой базе
запись запрещена без ``--allow-production`` (урок ТЗ-45F).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tuber import cli, config
from tuber.analysis import slivki
from tuber.core import db, schema

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _make_db(tmp_path):
    path = str(tmp_path / "cli.db")
    con = db.connect(path)
    schema.init_schema(con)
    con.execute(
        "INSERT INTO source(id, platform, handle, status, subs)"
        " VALUES (1,'telegram','chan','active',2000)")
    for cid in range(1, 5):
        published = NOW - timedelta(hours=20, minutes=cid)
        con.execute(
            "INSERT INTO content(id, platform, source_id, external_id, published_at)"
            " VALUES (?, 'telegram', 1, ?, ?)",
            (cid, f"e{cid}", published.strftime("%Y-%m-%d %H:%M:%S")))
        con.execute(
            "INSERT INTO metric_snapshot(content_id, platform, captured_at, views)"
            " VALUES (?, 'telegram', ?, ?)",
            (cid, (published + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
             1000 if cid == 4 else 100))
    con.commit()
    con.close()
    return path


def test_default_command_prints_report(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = cli.main(["rating", "slivki", "--db", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Сливки" in out
    assert "относительный выброс" in out
    assert "до самой сути" in out


def test_report_is_readonly(tmp_path, capsys):
    path = _make_db(tmp_path)
    cli.main(["rating", "slivki", "--db", path])
    capsys.readouterr()
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM source_metric_history").fetchone()[0] == 0
    finally:
        con.close()


def test_capture_writes_snapshot(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = slivki.main(["slivki", "capture", "--db", path])
    capsys.readouterr()
    assert rc == 0
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM source_metric_history").fetchone()[0] == 1
    finally:
        con.close()


def test_capture_dry_run_writes_nothing(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = slivki.main(["slivki", "capture", "--db", path, "--dry-run"])
    capsys.readouterr()
    assert rc == 0
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM source_metric_history").fetchone()[0] == 0
    finally:
        con.close()


def test_production_gate_blocks_without_flag(tmp_path, capsys, monkeypatch):
    path = _make_db(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", Path(path))
    rc = slivki.main(["slivki", "capture", "--db", path])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--allow-production" in err
    con = db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM source_metric_history").fetchone()[0] == 0
    finally:
        con.close()


def test_json_output(tmp_path, capsys):
    path = _make_db(tmp_path)
    rc = cli.main(["rating", "slivki", "--db", path, "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json
    data = json.loads(out)
    assert data["history_required_days"] == 7
    assert "outlier_by_platform" in data
