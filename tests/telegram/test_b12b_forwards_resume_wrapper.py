"""ТЗ-B12b: обёртка ВОЗОБНОВЛЯЕМОГО добора форвардов Telegram.

Проверяем главное поведение `scripts/telegram/tuber_tg_forwards_resume.sh`:
  * при норме МОЛЧИТ, код 0;
  * `TUBER_LAUNCHER_DRYRUN=1` — печатает план, CLI не зовётся; план несёт
    `--allow-production` и умеренный потолок `--max-requests 200`;
  * реальный прогон пробрасывает `--allow-production`, `--max-requests` и `--db`;
  * ненулевой код / пусто / неразобранный итог — ОДНА строка `ALERT:`, код 0.

Сеть и боевая база не используются: вместо `TUBER_PYTHON` подставляется
скрипт-заглушка, который записывает вызов и печатает подготовленную сводку.
"""
import json
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "telegram", "tuber_tg_forwards_resume.sh")

FAKE = """#!/usr/bin/env bash
# Записываем только вызов CLI (начинается с -m). Вызов предохранителя флуда
# (первый аргумент '-') не записываем — иначе тесты примут его за вызов CLI.
if [ "$1" = "-m" ]; then printf '%s\\n' "$@" >> "$TUBER_TG_RECORD"; fi
case "$*" in
  *forwards-mtproto*)
    if [ "${TUBER_TG_FAKE_RC:-0}" != "0" ]; then echo "boom" >&2; exit "$TUBER_TG_FAKE_RC"; fi
    if [ "${TUBER_TG_FAKE_EMPTY:-0}" = "1" ]; then exit 0; fi
    printf '%s\\n' "$TUBER_TG_FAKE_JSON"
    ;;
esac
"""

JSON_OK = json.dumps({"forwards_mtproto": {
    "channels_selected": 100, "channels_processed": 3, "forwards_filled": 50,
    "requests": 12, "stopped": None}})


def _run(tmp_path, *, rc="0", empty="0", dryrun=False, db="", requests=""):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.txt"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({
        "TUBER_PYTHON": str(fake),
        "TUBER_TG_PROJECT": str(project),
        "TUBER_TG_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_TG_RECORD": str(record),
        "TUBER_TG_FAKE_RC": rc,
        "TUBER_TG_FAKE_EMPTY": empty,
        "TUBER_TG_FAKE_JSON": JSON_OK,
        "TUBER_TG_FORWARDS_CRON_LOG": str(tmp_path / "logs" / "resume.log"),
    })
    if db:
        env["TUBER_DB"] = db
    if requests:
        env["TUBER_TG_FORWARDS_RESUME_REQUESTS"] = requests
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return proc, lines


def _cli_line(lines):
    """Строки argv, относящиеся к вызову CLI (до `--json`)."""
    return [ln for ln in lines if "forwards-mtproto" in ln or ln.startswith("--")]


def test_norm_is_silent_and_exit_zero(tmp_path):
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "при норме обёртка обязана молчать"
    assert "--max-requests" in lines and "200" in lines, lines


def test_dryrun_prints_plan_without_network(tmp_path):
    proc, lines = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    plan = proc.stdout.strip()
    assert plan.endswith(
        "-m tuber tg scoring forwards-mtproto --allow-production "
        "--max-requests 200"), plan
    assert "--allow-production" in plan
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_dryrun_limit_env_override(tmp_path):
    proc, _ = _run(tmp_path, dryrun=True, requests="50")
    assert proc.returncode == 0, proc.stderr
    assert "--max-requests 50" in proc.stdout, proc.stdout


def test_run_passes_flags_to_cli(tmp_path):
    proc, lines = _run(tmp_path, db=str(tmp_path / "copy.db"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "--allow-production" in lines, lines
    assert "--max-requests" in lines and "200" in lines, lines
    assert "--db" in lines, lines


def test_nonzero_rc_alerts(tmp_path):
    proc, _ = _run(tmp_path, rc="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert proc.stdout.count("\n") == 1, proc.stdout


def test_empty_output_alerts(tmp_path):
    proc, _ = _run(tmp_path, empty="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
