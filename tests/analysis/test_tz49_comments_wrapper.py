"""ТЗ-49: обёртка суточного сбора комментариев (``tuber_comments_daily.sh``).

Проверяем главное поведение обёртки:

* при норме МОЛЧИТ, код 0;
* при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку ``ALERT:`` по-русски;
* ``TUBER_LAUNCHER_DRYRUN=1`` — печатает план по всем трём шагам (youtube,
  telegram, x), CLI не зовётся;
* ТЗ-45F: план И реальный прогон ОБЯЗАНЫ нести ``--allow-production`` (иначе
  гейт записи глушит боевой ночной прогон), а ``TUBER_DB=<копия>`` работает без
  помех.

Сеть и боевая база не используются: вместо ``TUBER_PYTHON`` подставляется
скрипт-заглушка, который записывает вызов CLI и печатает подготовленный вывод.
"""
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "common", "tuber_comments_daily.sh")

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_COMMENTS_RECORD"
if [ "${TUBER_COMMENTS_FAKE_RC:-0}" != "0" ]; then
  echo "boom" >&2
  exit "$TUBER_COMMENTS_FAKE_RC"
fi
if [ "${TUBER_COMMENTS_FAKE_EMPTY:-0}" = "1" ]; then exit 0; fi
printf 'Комментарии ок\\n'
"""


def _run(tmp_path, *, rc="0", empty="0", dryrun=False, db="", yt_limit=""):
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
        "TUBER_COMMENTS_PROJECT": str(project),
        "TUBER_COMMENTS_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_COMMENTS_RECORD": str(record),
        "TUBER_COMMENTS_FAKE_RC": rc,
        "TUBER_COMMENTS_FAKE_EMPTY": empty,
        "TUBER_COMMENTS_TIMEOUT_SEC": "5",
    })
    if db:
        env["TUBER_DB"] = db
    if yt_limit:
        env["TUBER_COMMENTS_YT_LIMIT"] = yt_limit
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return proc, lines


def test_norm_is_silent_and_exit_zero(tmp_path):
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "при норме обёртка обязана молчать"
    assert "youtube" in lines and "telegram" in lines and "x" in lines


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
    out = proc.stdout
    assert "comments youtube" in out
    assert "comments telegram" in out
    assert "comments x" in out
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_dryrun_plan_has_allow_production(tmp_path):
    """ТЗ-45F: обёртка — боевой планировщик, план ОБЯЗАН объявлять флаг записи."""
    proc, _ = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if "comments" in ln]
    assert len(lines) == 3
    assert all("--allow-production" in ln for ln in lines), proc.stdout


def test_run_passes_allow_production_to_cli(tmp_path):
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert lines.count("--allow-production") == 3, lines


def test_run_with_db_copy_still_works(tmp_path):
    copy_db = tmp_path / "copy.db"
    proc, lines = _run(tmp_path, db=str(copy_db))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert lines.count("--allow-production") == 3, lines


def test_yt_limit_flag_forwarded(tmp_path):
    proc, lines = _run(tmp_path, yt_limit="7")
    assert proc.returncode == 0, proc.stderr
    assert "--limit" in lines and "7" in lines, lines
