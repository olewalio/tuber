"""ТЗ-46/ТЗ-51: обёртка суточного разбора очереди и промоушена.

Проверяем главное поведение ``scripts/common/tuber_queue_review.sh``:

* при норме МОЛЧИТ, код 0;
* при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку ``ALERT:`` по-русски
  и завершается НЕНУЛЕВЫМ кодом (ТЗ-51);
* после разбора очереди обёртка выполняет промоушен (``tuber tg promote``);
* ``TUBER_LAUNCHER_DRYRUN=1`` — печатает план (обе команды), CLI не зовётся;
* ТЗ-45F: план И реальный прогон ОБЯЗАНЫ нести ``--allow-production`` у КАЖДОЙ
  подкоманды (иначе гейт записи глушит боевой ночной прогон), а
  ``TUBER_DB=<копия>`` работает без помех.

Сеть и боевая база не используются: вместо ``TUBER_PYTHON`` подставляется
скрипт-заглушка, который записывает вызов CLI и печатает подготовленный вывод.
"""
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "common", "tuber_queue_review.sh")

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_QUEUE_RECORD"
if [ "${TUBER_QUEUE_FAKE_RC:-0}" != "0" ]; then
  echo "boom" >&2
  exit "$TUBER_QUEUE_FAKE_RC"
fi
if [ "${TUBER_QUEUE_FAKE_EMPTY:-0}" = "1" ]; then exit 0; fi
printf '{"by_platform": {"web": {"reject": 3509}}}\\n'
"""


def _run(tmp_path, *, rc="0", empty="0", dryrun=False, db="", x_budget=""):
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
        "TUBER_QUEUE_PROJECT": str(project),
        "TUBER_QUEUE_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_QUEUE_RECORD": str(record),
        "TUBER_QUEUE_FAKE_RC": rc,
        "TUBER_QUEUE_FAKE_EMPTY": empty,
        "TUBER_QUEUE_TIMEOUT_SEC": "5",
    })
    if db:
        env["TUBER_DB"] = db
    if x_budget:
        env["TUBER_QUEUE_X_BUDGET"] = x_budget
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return proc, lines


def test_norm_is_silent_and_exit_zero(tmp_path):
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "при норме обёртка обязана молчать"
    assert "review" in lines, lines
    assert "promote" in lines, lines


def test_nonzero_rc_alerts_and_exits_nonzero(tmp_path):
    """ТЗ-51: сбой обязан быть виден крону — ненулевой код обязателен."""
    proc, _ = _run(tmp_path, rc="1")
    assert proc.returncode != 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert proc.stdout.count("\n") == 1, proc.stdout


def test_empty_output_alerts(tmp_path):
    proc, _ = _run(tmp_path, empty="1")
    assert proc.returncode != 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout


def test_promotion_step_runs_after_review(tmp_path):
    """После разбора очереди обёртка обязана выполнить промоушен (ТЗ-51)."""
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    review_idx = lines.index("review")
    promote_idx = lines.index("promote")
    assert promote_idx > review_idx, lines


def test_dryrun_prints_plan_without_cli(tmp_path):
    proc, lines = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.splitlines()
    assert out[0] == (
        f"{tmp_path}/bin/python3 -m tuber queue review --allow-production")
    assert out[1] == (
        f"{tmp_path}/bin/python3 -m tuber tg promote --allow-production")
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_dryrun_plan_has_allow_production(tmp_path):
    """ТЗ-45F: обёртка — боевой планировщик, план ОБЯЗАН объявлять флаг записи."""
    proc, _ = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 2, proc.stdout
    for line in lines:
        assert "--allow-production" in line, (
            "план обязан печатать --allow-production: " + repr(proc.stdout))


def test_run_passes_allow_production_to_every_step(tmp_path):
    """Флаг записи обязан доходить до КАЖДОЙ подкоманды, не только до review."""
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    steps = {"review": 0, "promote": 0}
    flag = 0
    for line in lines:
        if line in steps:
            steps[line] += 1
        if line == "--allow-production":
            flag += 1
    assert steps == {"review": 1, "promote": 1}, lines
    assert flag == 2, lines


def test_run_with_db_copy_still_works(tmp_path):
    copy_db = tmp_path / "copy.db"
    proc, lines = _run(tmp_path, db=str(copy_db))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert "--allow-production" in lines, lines
    assert "review" in lines, lines
    assert "promote" in lines, lines


def test_x_budget_flag_forwarded(tmp_path):
    proc, lines = _run(tmp_path, x_budget="120")
    assert proc.returncode == 0, proc.stderr
    assert "--x-budget" in lines, lines
    assert "120" in lines, lines
