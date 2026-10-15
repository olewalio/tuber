"""ТЗ-48: обёртка суточного контура 5 (подтемы + новинки).

Проверяем главное поведение ``scripts/common/tuber_trends_daily.sh``:

* при норме МОЛЧИТ, код 0;
* при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку `ALERT:` по-русски;
* ``TUBER_LAUNCHER_DRYRUN=1`` — печатает план (обе подкоманды), CLI не зовётся;
* план И реальный прогон ОБЯЗАНЫ нести ``--allow-production`` для
  ``novelties --save`` (урок ТЗ-45F: иначе гейт записи глушит боевой прогон).

Сеть и боевая база не используются: вместо ``TUBER_PYTHON`` подставляется
скрипт-заглушка, который записывает вызовы CLI и печатает подготовленный вывод.
"""
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "common", "tuber_trends_daily.sh")

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_TRENDS_RECORD"
if [ "${TUBER_TRENDS_FAKE_RC:-0}" != "0" ]; then
  echo "boom" >&2
  exit "$TUBER_TRENDS_FAKE_RC"
fi
if [ "${TUBER_TRENDS_FAKE_EMPTY:-0}" = "1" ]; then exit 0; fi
printf '=== trends fake output ===\\n'
"""


def _run(tmp_path, *, rc="0", empty="0", dryrun=False, db=""):
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
        "TUBER_TRENDS_PROJECT": str(project),
        "TUBER_TRENDS_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_TRENDS_RECORD": str(record),
        "TUBER_TRENDS_FAKE_RC": rc,
        "TUBER_TRENDS_FAKE_EMPTY": empty,
        "TUBER_TRENDS_TIMEOUT_SEC": "5",
    })
    if db:
        env["TUBER_DB"] = db
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env,
                          check=False)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return proc, lines


def test_norm_is_silent_and_exit_zero(tmp_path):
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "при норме обёртка обязана молчать"
    joined = "\n".join(lines)
    assert "trends" in joined and "inside" in joined and "novelties" in joined, lines


def test_novelty_run_declares_allow_production(tmp_path):
    """ТЗ-45F: боевой прогон novelties --save обязан нести --allow-production."""
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "--save" in lines, lines
    assert "--allow-production" in lines, lines


def test_nonzero_rc_alerts(tmp_path):
    proc, _ = _run(tmp_path, rc="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert proc.stdout.count("\n") == 1, proc.stdout


def test_empty_output_alerts(tmp_path):
    proc, _ = _run(tmp_path, empty="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout


def test_dryrun_prints_plan_without_cli(tmp_path):
    proc, lines = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    assert "-m tuber trends inside" in text, text
    assert "-m tuber trends novelties --save --allow-production" in text, text
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_run_with_db_copy_still_works(tmp_path):
    copy_db = tmp_path / "copy.db"
    proc, lines = _run(tmp_path, db=str(copy_db))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert "--allow-production" in lines, lines
