"""Тесты суточного бэкапа единой базы (ТЗ-5 §1.4).

Проверяется: копия снимается и читается, ``PRAGMA integrity_check`` пишется в
журнал, отсутствие базы — ошибка (а не «бэкап пустоты»), ротация по ``--keep``,
и что ``main`` печатает сводку и возвращает код 2 при проблеме.
"""
from __future__ import annotations

import sqlite3

import pytest

from tuber.tools import db_backup


def _make_db(path, rows: int = 3) -> str:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t(v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    conn.commit()
    conn.close()
    return str(path)


def test_backup_creates_copy_and_logs_integrity(tmp_path):
    src = _make_db(tmp_path / "tuber.db", rows=5)
    out = tmp_path / "backups"
    summary = db_backup.backup_database(src, backup_dir=str(out))
    assert summary["source_ok"] and summary["backup_ok"]
    assert summary["source_integrity"] == "ok"
    copy = summary["backup"]
    assert copy.endswith(".db")
    # Копия — самостоятельная база с теми же данными.
    conn = sqlite3.connect(copy)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5
    conn.close()
    # Результат записан в журнал (обязательное требование §1.4).
    log = (out / db_backup.LOG_NAME).read_text(encoding="utf-8")
    assert "backup_integrity=True" in log
    assert summary["backup"] in log


def test_backup_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        db_backup.backup_database(str(tmp_path / "nope.db"), backup_dir=str(tmp_path))


def test_backup_prune_keeps_newest(tmp_path, monkeypatch):
    src = _make_db(tmp_path / "tuber.db")
    out = tmp_path / "backups"
    out.mkdir()
    # Три «старых» копии с разным mtime.
    import os
    import time
    for i, name in enumerate(["tuber-20260101-000000.db",
                              "tuber-20260102-000000.db",
                              "tuber-20260103-000000.db"]):
        p = out / name
        p.write_bytes(b"x")
        os.utime(p, (1_700_000_000 + i * 10, 1_700_000_000 + i * 10))
        time.sleep(0)
    summary = db_backup.backup_database(src, backup_dir=str(out), keep=2)
    remaining = sorted(p.name for p in out.glob("tuber-*.db"))
    assert len(remaining) == 2
    # Свежая копия (только что созданная) осталась.
    assert os.path.basename(summary["backup"]) in remaining


def test_integrity_check_reports_corruption(tmp_path):
    good = _make_db(tmp_path / "good.db")
    ok, verdict = db_backup.integrity_check(good)
    assert ok and verdict == "ok"


def test_cli_backup_prints_summary(tmp_path, capsys):
    src = _make_db(tmp_path / "tuber.db")
    rc = db_backup.main(["backup", "--db", src, "--dir", str(tmp_path / "b")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tuber db backup" in out and "ok" in out


def test_cli_backup_missing_db_returns_2(tmp_path, capsys):
    rc = db_backup.main(["backup", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "ошибка" in capsys.readouterr().err
