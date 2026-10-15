"""ТЗ-B12c: единый гейт боевой базы для `yt snapshots` (аналог D-43).

Сеть и боевая база не используются:

* гейт проверяется на подставном «боевом» пути (monkeypatch
  ``tuber.config.DEFAULT_DB_PATH``) — отказ обязан случиться ДО открытия базы;
* «пропуск с флагом» тоже идёт на ВРЕМЕННУЮ базу (она лишь объявлена боевой),
  поэтому реальный сбор/квота не трогаются;
* обёртка крона ``scripts/youtube/tuber_snapshots.sh`` проверяется на
  построении аргументов через скрипт-заглушку вместо интерпретатора контура.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from tuber import config as core_config
from tuber.platforms.youtube import cli
from tuber.platforms.youtube import store as db

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "youtube" / "tuber_snapshots.sh"
INSTALLER = ROOT / "scripts" / "install_hermes_cron.sh"


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    """Тесты не читают реальный /root/.hermes/.env."""
    monkeypatch.setattr(cli.config, "load_env", lambda *a, **k: 0)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Изолированные пути CLI: база, замок и лог во временной папке."""
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "tuber.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "tuber.log")
    return tmp_path


# --- гейт ------------------------------------------------------------------


def test_gate_helper(tmp_path, monkeypatch):
    prod = tmp_path / "prod.db"
    monkeypatch.setattr(core_config, "DEFAULT_DB_PATH", prod)
    # Боевой путь без флага — отказ с подсказкой.
    msg = db.production_gate_error(str(prod), False)
    assert msg and "--allow-production" in msg
    # С флагом — можно; копия — можно и без флага.
    assert db.production_gate_error(str(prod), True) is None
    assert db.production_gate_error(str(tmp_path / "copy.db"), False) is None
    assert db.production_gate_error("", False) is None


def test_snapshots_refuses_without_flag(sandbox, monkeypatch, capsys):
    """Без флага на боевой базе — понятный отказ, код 2, база не открыта."""
    monkeypatch.setattr(core_config, "DEFAULT_DB_PATH", sandbox / "tuber.db")
    rc = cli.main(["snapshots"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "боевая единая база" in captured.err
    assert "--allow-production" in captured.err
    # База не создана: гейт сработал до _connect().
    assert not (sandbox / "tuber.db").exists()


def test_snapshots_passes_with_flag(sandbox, monkeypatch, capsys):
    """С флагом команда идёт дальше (пустая временная база: 0 замеров, без сети)."""
    monkeypatch.setattr(core_config, "DEFAULT_DB_PATH", sandbox / "tuber.db")
    rc = cli.main(["snapshots", "--allow-production", "--json"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert '"command": "snapshots"' in out


def test_snapshots_on_copy_needs_no_flag(sandbox, monkeypatch, capsys):
    """Копия (путь не совпадает с боевым) работает без флага."""
    monkeypatch.setattr(core_config, "DEFAULT_DB_PATH", sandbox / "other.db")
    rc = cli.main(["snapshots"])
    assert rc == 0


# --- обёртка крона ---------------------------------------------------------

FAKE_PY = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_YT_RECORD"
printf '%s\\n' '{"command": "snapshots", "captured": 0}'
"""


def _run_wrapper(tmp_path, db_path=""):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(FAKE_PY)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.txt"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({
        "TUBER_PYTHON": str(fake),
        "TUBER_YT_PROJECT": str(project),
        "TUBER_YT_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_YT_RECORD": str(record),
    })
    if db_path:
        env["TUBER_DB"] = db_path
    proc = subprocess.run(["bash", str(WRAPPER)], capture_output=True,
                          text=True, env=env, check=False)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return proc, lines


def test_wrapper_declares_allow_production(tmp_path):
    """Крон-обёртка обязана передавать --allow-production (иначе крон встанет)."""
    proc, lines = _run_wrapper(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "snapshots" in lines
    assert "--allow-production" in lines, lines


def test_wrapper_passes_db_for_copy(tmp_path):
    """Если задан TUBER_DB (копия), обёртка всё равно корректно строит команду."""
    proc, lines = _run_wrapper(tmp_path, db_path=str(tmp_path / "copy.db"))
    assert proc.returncode == 0, proc.stderr
    assert "--db" in lines and str(tmp_path / "copy.db") in lines


def test_installer_declares_allow_production():
    """Источник расписания (LAUNCHERS) объявляет флаг для шима snapshots."""
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r"^.*tuber_snapshots\.sh:.*$", text, re.MULTILINE)
    assert match, "в install_hermes_cron.sh нет записи LAUNCHERS для snapshots"
    assert "yt snapshots --allow-production" in match.group(0), match.group(0)
