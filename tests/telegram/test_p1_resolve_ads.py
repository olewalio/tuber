"""ТЗ P1-1 / P1-4: резолв закрытых каналов и семантический детектор рекламы.

Проверяем без сети и без боевой базы:
  * суточный лимит резолва поднят до 200, норма за прогон остаётся 20;
  * цель резолва включает закрытые каналы (``status='private'``), а не только
    очередь кандидатов;
  * при активном окне флуда ``run_resolve`` не делает ни одного MTProto-запроса;
  * обёртки ``tuber_tg_resolve.sh`` / ``tuber_tg_ads.sh`` печатают план под
    ``TUBER_LAUNCHER_DRYRUN=1`` и пробрасывают нужные флаги в CLI;
  * генератор расписания объявляет обе обёртки и обе строки расписания.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from tuber.platforms.telegram import collect as C
from tuber.platforms.telegram import config as CFG
from tuber.platforms.telegram import store as db

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESOLVE_SH = os.path.join(ROOT, "scripts", "telegram", "tuber_tg_resolve.sh")
ADS_SH = os.path.join(ROOT, "scripts", "telegram", "tuber_tg_ads.sh")
INSTALLER = os.path.join(ROOT, "scripts", "install_hermes_cron.sh")


# ---------------------------------------------------------------------------
# Конфигурация и выбор цели
# ---------------------------------------------------------------------------
def test_resolve_limits():
    assert C.RESOLVE_MAX_PER_RUN == 20
    assert C.RESOLVE_MAX_PER_DAY >= 160, C.RESOLVE_MAX_PER_DAY
    assert CFG.RESOLVE_MAX_PER_DAY == 200


@pytest.fixture()
def tg_db(tmp_path):
    path = str(tmp_path / "p1.db")
    db.init_db(path).close()
    con = db.connect(path)
    yield con
    con.close()


def _add_channel(con, handle, status, subs, tg_id=None, checked="2026-10-01 00:00:00"):
    con.execute(
        "INSERT INTO channels(handle, title, subs, status, read_mode, checked_at) "
        "VALUES (?,?,?,?,?,?)",
        (handle, handle, subs, status,
         "unreadable" if status == "private" else "web", checked),
    )
    if tg_id is not None:
        con.execute("UPDATE channels SET tg_id=? WHERE handle=?", (tg_id, handle))
    con.commit()


def test_select_resolve_targets_includes_private(tg_db):
    con = tg_db
    _add_channel(con, "priv_big", "private", 50000)
    _add_channel(con, "cand_small", "candidate", 100)
    _add_channel(con, "already", "private", 999999, tg_id=12345)  # уже разрешён
    c = C.Collector(db_path=_path(con), mode="resolve")
    c.con = con  # работаем на той же временной базе
    handles = [r["handle"] for r in c.select_resolve_targets(20)]
    assert "priv_big" in handles, handles
    assert "cand_small" in handles, handles
    assert "already" not in handles, handles
    assert handles[0] == "priv_big", handles  # порядок: больше подписчиков выше


def _path(con):
    """Путь временной базы для конструктора Collector (нужен только для connect)."""
    row = con.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ":memory:"


# ---------------------------------------------------------------------------
# Окно флуда: подставной клиент не должен быть создан вообще
# ---------------------------------------------------------------------------
def _future(seconds):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def test_run_resolve_skips_without_requests_on_flood(tg_db, monkeypatch):
    con = tg_db
    _add_channel(con, "priv1", "private", 1000)
    until = _future(86400)
    con.execute(
        "INSERT INTO account_state(name, flood_until, resolves_today, day) "
        "VALUES ('tg_collector', ?, 0, ?)", (until, C.today_utc()))
    con.commit()

    calls = {"client": 0}

    def fake_client(api_id, api_hash):
        calls["client"] += 1
        raise AssertionError("Telethon создан во время окна флуда")

    monkeypatch.setattr(C.Collector, "_tg_client", fake_client)
    monkeypatch.setattr(C, "load_env", lambda *a, **k: {"TG_API_ID": "1", "TG_API_HASH": "h"})

    c = C.Collector(db_path=_path(con), mode="resolve", resolve_n=20)
    c.con = con
    c.run_resolve()
    assert calls["client"] == 0, "MTProto-клиент создан при активном флуде"
    assert c.summary["channels_ok"] == 0
    assert c.summary["flood_until"], "окно флуда не попало в итог"


def test_set_flood_upserts_existing_row(tg_db):
    """Повторный флуд должен обновить flood_until, а не молча потеряться."""
    con = tg_db
    # Строка tg_collector уже есть (её создаёт resolve_gate) с flood_until=NULL.
    con.execute(
        "INSERT INTO account_state(name, flood_until, resolves_today, day) "
        "VALUES ('tg_collector', NULL, 0, ?)", (C.today_utc(),))
    con.commit()
    c = C.Collector(db_path=_path(con), mode="resolve")
    c.con = con
    until = c._set_flood(None, 3600)
    row = con.execute(
        "SELECT flood_until FROM account_state WHERE name='tg_collector'").fetchone()
    # Ядро хранит время с секундной точностью — сравниваем момент, не строку.
    assert row["flood_until"][:19] == until[:19], (row["flood_until"], until)


# ---------------------------------------------------------------------------
# Обёртки: план и проброс флагов
# ---------------------------------------------------------------------------
RESOLVE_FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_TG_RECORD"
printf '%s\\n' '{"mode":"resolve","channels_ok":0,"channels_fail":0,"errors":0,"flood_until":null}'
"""

ADS_FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_TG_RECORD"
printf '%s\\n' 'ADS: к разбору 200, разобрано 0'
"""


def _run_wrapper(tmp_path, wrapper, fake_body, *, dryrun=False, rc_env=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(fake_body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.txt"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    logdir = tmp_path / "logs"
    env = dict(os.environ)
    env.update({
        "TUBER_PYTHON": str(fake),
        "TUBER_TG_PROJECT": str(project),
        "TUBER_TG_LOG_DIR": str(logdir),
        "TUBER_TG_RECORD": str(record),
    })
    if rc_env:
        env.update(rc_env)
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    proc = subprocess.run(["bash", wrapper], capture_output=True, text=True, env=env)
    rec = record.read_text(encoding="utf-8") if record.exists() else ""
    return proc, rec


def test_resolve_wrapper_dryrun_plan(tmp_path):
    proc, rec = _run_wrapper(tmp_path, RESOLVE_SH, RESOLVE_FAKE, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "-m tuber tg collect --mode resolve --resolve 20" in proc.stdout, proc.stdout
    assert rec == "", "в dry-run CLI не должен вызываться"


def test_resolve_wrapper_passes_flags(tmp_path):
    proc, rec = _run_wrapper(tmp_path, RESOLVE_SH, RESOLVE_FAKE)
    assert proc.returncode == 0, proc.stderr
    assert "collect" in rec and "--mode" in rec and "resolve" in rec, rec
    assert "--resolve" in rec and "20" in rec, rec


def test_ads_wrapper_dryrun_plan(tmp_path):
    proc, rec = _run_wrapper(tmp_path, ADS_SH, ADS_FAKE, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "-m tuber tg ads --limit 200 --allow-production" in proc.stdout, proc.stdout
    assert rec == "", "в dry-run CLI не должен вызываться"


def test_ads_wrapper_passes_limit_and_gate(tmp_path):
    proc, rec = _run_wrapper(tmp_path, ADS_SH, ADS_FAKE)
    assert proc.returncode == 0, proc.stderr
    assert "--limit" in rec and "200" in rec, rec
    assert "--allow-production" in rec, rec


# ---------------------------------------------------------------------------
# Генератор расписания: объявлены и обёртки, и строки расписания
# ---------------------------------------------------------------------------
def test_installer_declares_resolve_and_ads():
    text = open(INSTALLER, encoding="utf-8").read()
    assert "tuber_tg_resolve.sh:scripts/telegram/tuber_tg_resolve.sh::" \
           "python3 -m tuber tg collect --mode resolve --resolve 20" in text
    assert "tuber_tg_ads.sh:scripts/telegram/tuber_tg_ads.sh::" \
           "python3 -m tuber tg ads --limit 200 --allow-production" in text
    assert "tuber_tg_resolve.sh:25 8-23/2 * * *:Tuber-Telegram: резолв" in text
    assert "tuber_tg_ads.sh:15 4 * * *:Tuber-Telegram: семантический детектор" in text


def test_installer_dryrun_lists_new_wrappers(tmp_path):
    env = dict(os.environ)
    env.update({
        "HERMES_SCRIPTS_DIR": str(tmp_path / "scripts"),
        "TUBER_PYTHON": shutil.which("python3") or "python3",
    })
    proc = subprocess.run(["bash", INSTALLER, "--dry-run"], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "tuber_tg_resolve.sh" in proc.stdout, proc.stdout
    assert "tuber_tg_ads.sh" in proc.stdout, proc.stdout
