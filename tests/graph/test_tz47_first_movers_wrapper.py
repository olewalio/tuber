"""ТЗ-47: обёртка суточного графа первопроходцев.

Проверяем ГЛАВНОЕ поведение обёртки
``scripts/common/tuber_graph_first_movers.sh``:

* при норме МОЛЧИТ, код 0;
* при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку `ALERT:` по-русски;
* `TUBER_LAUNCHER_DRYRUN=1` — печатает план, CLI не зовётся;
* `TUBER_GRAPH_FM_LIMIT` пробрасывается как `--limit`;
* ТЗ-45F: план И реальный прогон ОБЯЗАНЫ нести `--allow-production` (иначе
  гейт записи глушит боевой ночной прогон), а `TUBER_DB=<копия>` работает без помех.

Сеть и боевая база не используются: вместо `TUBER_PYTHON` подставляется
скрипт-заглушка, который записывает вызов CLI и печатает подготовленный вывод.
"""
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "common", "tuber_graph_first_movers.sh")

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_GRAPH_FM_RECORD"
if [ "${TUBER_GRAPH_FM_FAKE_RC:-0}" != "0" ]; then
  echo "boom" >&2
  exit "$TUBER_GRAPH_FM_FAKE_RC"
fi
if [ "${TUBER_GRAPH_FM_FAKE_EMPTY:-0}" = "1" ]; then exit 0; fi
printf '=== Граф первопроходцев: записей реестра 230, оценок обновлено 695 ===\\n'
"""


def _run(tmp_path, *, limit="", rc="0", empty="0", dryrun=False, db=""):
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
        "TUBER_GRAPH_FM_PROJECT": str(project),
        "TUBER_GRAPH_FM_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_GRAPH_FM_RECORD": str(record),
        "TUBER_GRAPH_FM_FAKE_RC": rc,
        "TUBER_GRAPH_FM_FAKE_EMPTY": empty,
        "TUBER_GRAPH_FM_TIMEOUT_SEC": "5",
    })
    if limit:
        env["TUBER_GRAPH_FM_LIMIT"] = limit
    if db:
        env["TUBER_DB"] = db
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return proc, lines


def test_norm_is_silent_and_exit_zero(tmp_path):
    proc, lines = _run(tmp_path, limit="5")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "при норме обёртка обязана молчать"
    assert "--limit" in lines and "5" in lines, lines


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
    assert proc.stdout.strip() == (
        f"{tmp_path}/bin/python3 -m tuber graph first-movers --allow-production")
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_dryrun_plan_has_allow_production(tmp_path):
    """ТЗ-45F: обёртка — боевой планировщик, план ОБЯЗАН объявлять флаг записи."""
    proc, _ = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "--allow-production" in proc.stdout, (
        "dry-run план обязан печатать --allow-production: " + repr(proc.stdout))


def test_dryrun_prints_limit_in_plan(tmp_path):
    proc, lines = _run(tmp_path, dryrun=True, limit="3")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == (
        f"{tmp_path}/bin/python3 -m tuber graph first-movers"
        " --allow-production --limit 3")
    assert lines == []


def test_run_passes_allow_production_to_cli(tmp_path):
    """Реальный прогон тоже передаёт --allow-production в CLI."""
    proc, lines = _run(tmp_path, limit="5")
    assert proc.returncode == 0, proc.stderr
    assert "--allow-production" in lines, lines


def test_run_with_db_copy_still_works(tmp_path):
    """TUBER_DB=<копия>: прогон не ломается, флаг копии не мешает."""
    copy_db = tmp_path / "copy.db"
    proc, lines = _run(tmp_path, limit="5", db=str(copy_db))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert "--allow-production" in lines, lines
    assert "--limit" in lines and "5" in lines, lines
