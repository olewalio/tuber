"""ТЗ-43: обёртка очереди замеров (``scripts/common/tuber_metric_queue.sh``).

Проверяем главное поведение:

* при норме МОЛЧИТ, код 0;
* при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку `ALERT:` по-русски;
* ``TUBER_LAUNCHER_DRYRUN=1`` — печатает команду, CLI не зовётся, rc=0;
* план И реальный прогон ОБЯЗАНЫ нести ``--allow-production`` (урок ТЗ-45F:
  иначе гейт записи глушит боевой прогон);
* ``--limit`` и ``--no-network`` пробрасываются.

Сеть и боевая база не используются: вместо ``TUBER_PYTHON`` — скрипт-заглушка.
"""
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "common", "tuber_metric_queue.sh")

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_MQ_RECORD"
if [ "${TUBER_MQ_FAKE_RC:-0}" != "0" ]; then
  echo "boom" >&2
  exit "$TUBER_MQ_FAKE_RC"
fi
if [ "${TUBER_MQ_FAKE_EMPTY:-0}" = "1" ]; then exit 0; fi
printf '=== metrics fake output ===\\n'
"""


def _run(tmp_path, *, rc="0", empty="0", dryrun=False, db="", limit="",
         no_network="0"):
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
        "TUBER_METRIC_QUEUE_PROJECT": str(project),
        "TUBER_METRIC_QUEUE_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_MQ_RECORD": str(record),
        "TUBER_MQ_FAKE_RC": rc,
        "TUBER_MQ_FAKE_EMPTY": empty,
        "TUBER_METRIC_QUEUE_TIMEOUT_SEC": "5",
        "TUBER_METRIC_QUEUE_LIMIT": limit,
        "TUBER_METRIC_QUEUE_NO_NETWORK": no_network,
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
    assert "metrics" in lines and "run" in lines, lines


def test_run_declares_allow_production(tmp_path):
    """ТЗ-45F: боевой прогон обязан нести --allow-production."""
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
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
    assert "-m tuber metrics run --allow-production" in proc.stdout, proc.stdout
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_limit_and_no_network_passed(tmp_path):
    proc, lines = _run(tmp_path, limit="50", no_network="1")
    assert proc.returncode == 0, proc.stderr
    joined = "\n".join(lines)
    assert "--limit" in joined and "50" in joined, lines
    assert "--no-network" in joined, lines
    assert "--allow-production" in joined, lines


def test_run_with_db_copy_still_works(tmp_path):
    copy_db = tmp_path / "copy.db"
    proc, lines = _run(tmp_path, db=str(copy_db))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert "--allow-production" in lines, lines
