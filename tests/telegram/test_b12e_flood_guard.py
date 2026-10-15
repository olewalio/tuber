"""ТЗ-B12e / D-34: предохранитель флуда, который реально работает.

Проверяем без сети и без боевой базы:
  * обёртка читает ВСЕ строки ``transport_account_state`` (platform='telegram',
    любой kind), берёт максимальный будущий ``flood_until`` и НЕ делает запросов;
  * разбор всех форм времени (T / Z / пробел вместо T);
  * отсутствие БД/таблицы → ``flood_gate=unknown``, без падения, без спама;
  * CLI ``forwards-mtproto`` при FloodWait ЗАПИСЫВАЕТ окно флуда в БД и кладёт
    ``flood_until`` в итог (иначе следующий прогон снова идёт в бан).

CLI НИКОГДА не зовётся в тестах обёртки: подставленный python-шим для вызова
CLI только фиксирует попытку и отказывает. Для чтения состояния флуда шим
делегирует настоящему интерпретатору.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from tuber.platforms.telegram import scoring as S
from tuber.platforms.telegram import store as db

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "telegram", "tuber_tg_forwards_resume.sh")

# Шим вместо TUBER_PYTHON: '-' (предохранитель) → настоящий python; '-m' (CLI) →
# фиксируем и отказываем, чтобы тест НИКОГДА не ушёл в сеть.
SHIM = """#!/usr/bin/env bash
if [ "$1" = "-" ]; then exec "$TUBER_TEST_REAL_PY" "$@"; fi
printf '%s\\n' "$@" >> "$TUBER_TG_RECORD"
exit 1
"""


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _past(seconds: int) -> str:
    return _future(-int(seconds))


def _make_db(path, rows):
    """Создать копию с ``transport_account_state`` (как в схеме ядра)."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE transport_account_state ("
        "platform TEXT NOT NULL, name TEXT NOT NULL, flood_until TEXT, "
        "last_error TEXT, resolves_today INTEGER DEFAULT 0, day TEXT, "
        "updated_at TEXT, PRIMARY KEY (platform, name))")
    for platform, name, until in rows:
        con.execute(
            "INSERT INTO transport_account_state(platform, name, flood_until, updated_at) "
            "VALUES (?,?,?,?)", (platform, name, until, "2026-09-23T00:00:00"))
    con.commit()
    con.close()


def _run(tmp_path, db_path, *, dryrun=False, log_name="resume.log"):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "python3"
    shim.write_text(SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.txt"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    logdir = tmp_path / "logs"
    log_path = logdir / log_name
    env = dict(os.environ)
    env.update({
        "TUBER_PYTHON": str(shim),
        "TUBER_TEST_REAL_PY": sys.executable,
        "TUBER_TG_PROJECT": str(project),
        "TUBER_TG_LOG_DIR": str(logdir),
        "TUBER_TG_RECORD": str(record),
        "TUBER_TG_FORWARDS_CRON_LOG": str(log_path),
        "TUBER_DB": str(db_path),
    })
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    rec = record.read_text(encoding="utf-8") if record.exists() else ""
    return proc, log, rec


# ---------------------------------------------------------------------------
# Обёртка: предохранитель
# ---------------------------------------------------------------------------
def test_active_flood_skips_without_requests(tmp_path):
    db_path = tmp_path / "copy.db"
    until = _future(86400)
    _make_db(db_path, [("telegram", "forwards_mtproto", until)])
    proc, log, rec = _run(tmp_path, db_path)  # без dryrun: если gate не сработает, дойдёт до шима
    assert proc.returncode == 0, proc.stderr
    assert "skip: окно флуда активно" in log, log
    assert "flood_gate=active" in log, log
    assert f"flood={until}" in log, log
    assert "kind=forwards_mtproto" in log, log
    assert f"источник={db_path}" in log, log
    assert rec == "", "при активном окне флуда CLI звать нельзя"


def test_expired_flood_goes_further_dryrun(tmp_path):
    db_path = tmp_path / "copy.db"
    _make_db(db_path, [("telegram", "tg_comments", _past(3600))])
    proc, log, rec = _run(tmp_path, db_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "flood_gate=clear" in log, log
    assert "skip: окно флуда активно" not in log, log
    assert "forwards-mtproto" in proc.stdout, proc.stdout  # пошёл дальше
    assert rec == ""


def test_missing_db_logs_unknown(tmp_path):
    proc, log, rec = _run(tmp_path, tmp_path / "nope.db", dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "flood_gate=unknown" in log, log


def test_missing_table_logs_unknown(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()  # файл есть, таблицы нет
    proc, log, rec = _run(tmp_path, db_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "flood_gate=unknown" in log, log


def test_unknown_not_repeated_consecutively(tmp_path):
    db_path = tmp_path / "nope.db"
    _run(tmp_path, db_path, dryrun=True)
    proc, log, rec = _run(tmp_path, db_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert log.count("flood_gate=unknown") == 1, log


@pytest.mark.parametrize("shape", ["iso", "z", "space"])
def test_flood_time_formats(tmp_path, shape):
    base = _future(86400)
    value = {"iso": base, "z": base + "Z", "space": base.replace("T", " ")}[shape]
    db_path = tmp_path / "copy.db"
    _make_db(db_path, [("telegram", "forwards_mtproto", value)])
    proc, log, rec = _run(tmp_path, db_path)  # без dryrun: активное окно должно остановить
    assert proc.returncode == 0, proc.stderr
    assert "skip: окно флуда активно" in log, (shape, log)
    assert rec == "", (shape, "CLI не должен зваться")


def test_picks_max_future_across_kinds(tmp_path):
    near, far = _future(3600), _future(7200)
    db_path = tmp_path / "copy.db"
    _make_db(db_path, [
        ("telegram", "forwards_mtproto", near),
        ("telegram", "tg_comments", far),
        ("telegram", "old", _past(60)),
    ])
    proc, log, rec = _run(tmp_path, db_path)
    assert proc.returncode == 0, proc.stderr
    assert f"flood={far}" in log, log
    assert "kind=tg_comments" in log, log


def test_other_platform_ignored(tmp_path):
    db_path = tmp_path / "copy.db"
    _make_db(db_path, [("other", "job", _future(7200))])
    proc, log, rec = _run(tmp_path, db_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "flood_gate=clear" in log, log


# ---------------------------------------------------------------------------
# CLI: сохранение состояния флуда (D-34)
# ---------------------------------------------------------------------------
class FakeFloodWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class FakeEntity:
    def __init__(self, handle):
        self.handle = handle


class FakeTG:
    def __init__(self, flood_on=None):
        self.flood_on = flood_on

    async def get_entity(self, handle):
        if self.flood_on == handle:
            raise FakeFloodWait(47708)
        return FakeEntity(handle)

    async def get_messages(self, entity, limit=None):
        return []


def _seed(con, handle, msg_id=101):
    con.execute(
        "INSERT INTO source(platform, handle, title, status, first_seen_at) "
        "VALUES('telegram', ?, ?, 'active', datetime('now'))",
        (handle, f"Канал {handle}"))
    sid = con.execute(
        "SELECT id FROM source WHERE handle=?", (handle,)).fetchone()["id"]
    con.execute(
        "INSERT INTO content(platform, source_id, external_id, kind, published_at, text) "
        "VALUES('telegram', ?, ?, 'post', datetime('now'), 'текст')",
        (sid, f"{handle}/{msg_id}"))
    con.commit()
    return sid


def test_flood_wait_saves_state_and_json_field(tg_path):
    con = db.connect(tg_path)
    try:
        _seed(con, "chan1", 101)
        summary = S.backfill_forwards_mtproto(con, client=FakeTG("chan1"), pause=0)
        assert summary["stopped"] == "flood_wait"
        assert summary["flood_wait"] == 47708
        assert summary["flood_until"], summary
        row = con.execute(
            "SELECT name, flood_until, last_error FROM transport_account_state "
            "WHERE platform='telegram'").fetchone()
        assert row is not None, "флуд-состояние не записано"
        assert row["name"] == "forwards_mtproto"
        assert row["flood_until"] == summary["flood_until"]
        assert row["last_error"] == "FLOOD_WAIT 47708s"
        # flood_until — в будущем (наивная строка трактуется как UTC).
        dt = datetime.fromisoformat(row["flood_until"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        assert dt > datetime.now(timezone.utc)
        # flood_until присутствует в JSON-итоге (его же печатает --json).
        payload = json.dumps({"forwards_mtproto": summary}, ensure_ascii=False)
        assert '"flood_until"' in payload
        # Повторный прогон не плодит дубли (upsert по (platform, name)).
        S.backfill_forwards_mtproto(con, client=FakeTG("chan1"), pause=0)
        n = con.execute(
            "SELECT COUNT(*) AS c FROM transport_account_state "
            "WHERE platform='telegram' AND name='forwards_mtproto'").fetchone()["c"]
        assert n == 1
    finally:
        con.close()


def test_save_flood_state_failure_does_not_raise(tg_path, monkeypatch):
    con = db.connect(tg_path)
    try:
        import tuber.core.storage as storage

        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(storage, "upsert_transport_account_state", boom)
        until = S.save_flood_state(con, 30)  # не должно бросить
        assert until
        assert con.execute(
            "SELECT COUNT(*) AS c FROM transport_account_state").fetchone()["c"] == 0
    finally:
        con.close()
