"""ТЗ-5 §1.3: единое расписание живёт в установщике — не дать разойтись с реестром.

До этой волны расписание было размазано по трём проектам и трём установщикам.
Теперь единственный источник — блоки ``JOBS`` / ``NEW_JOBS`` в
``scripts/install_hermes_cron.sh``. Фактическое расписание читается из реестра
планировщика Hermes ``/root/.hermes/cron/jobs.json``.

Тест обязан различать ДВА разных случая (граница работ ТЗ-5: реестр правит
владелец, установщик в него не пишет):

* **расхождение** — задание, объявленное в ``JOBS`` (уже зарегистрированное),
  пропало/сменило время, либо в реестре висит лишнее Tuber-задание → это
  ПАДЕНИЕ;
* **ожидает регистрации** — задание из блока ``NEW_JOBS`` ещё не зарегистрировано
  владельцем → это не ошибка: имя печатается отдельным списком и не считается
  расхождением.

Пути переопределяются ``TUBER_INSTALLER`` и ``TUBER_HERMES_JOBS_JSON``: приёмка
подставляет временную копию установщика с нарочно изменённым временем и ждёт
падения, а вместе с ``HERMES_SCRIPTS_DIR`` — проверяет идемпотентную установку.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.environ.get(
    "TUBER_INSTALLER", os.path.join(ROOT, "scripts", "install_hermes_cron.sh"))
JOBS_JSON = os.environ.get(
    "TUBER_HERMES_JOBS_JSON", "/root/.hermes/cron/jobs.json")


def _block_re(name):
    return re.compile(r"^[ \t]*" + name + r"=\([ \t]*$\n(.*?)^[ \t]*\)[ \t]*$",
                      re.M | re.S)


def _parse_block(text, name):
    """{имя шима: cron-расписание} из блока ``name=(...)`` установщика."""
    m = _block_re(name).search(text)
    if not m:
        raise AssertionError(f"в установщике не найден блок {name}=(...)")
    jobs = {}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not (line.startswith('"') and line.endswith('"')):
            raise AssertionError(f"строка блока {name} не разобрана: {line!r}")
        shim, expr, _label = line[1:-1].split(":", 2)
        jobs[shim.strip()] = expr.strip()
    return jobs


def parse_installer(path):
    """(JOBS, NEW_JOBS): зарегистрированные и новые (ожидающие) задания."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    jobs = _parse_block(text, "JOBS")
    new_jobs = _parse_block(text, "NEW_JOBS")
    if not jobs:
        raise AssertionError("блок JOBS установщика пуст")
    overlap = set(jobs) & set(new_jobs)
    assert not overlap, f"задание объявлено сразу в JOBS и NEW_JOBS: {overlap}"
    return jobs, new_jobs


def parse_scheduler_jobs(path):
    """Tuber-задания из реестра планировщика: {имя скрипта: cron-расписание}."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("jobs") if isinstance(data, dict) else data
    jobs = {}
    for job in entries or []:
        if not isinstance(job, dict):
            continue
        script = job.get("script")
        if not isinstance(script, str):
            continue
        base = os.path.basename(script)
        if not (base.startswith("tuber_") and base.endswith(".sh")):
            continue
        sched = job.get("schedule") or {}
        expr = sched.get("expr") if isinstance(sched, dict) else None
        jobs[base] = expr
    return jobs


def compare_schedules(declared, new_jobs, actual):
    """(problems, awaiting) для объявленного и фактического расписания.

    ``problems`` — расхождения/пропажи/лишнее (падать). ``awaiting`` — имена из
    ``new_jobs``, которых ещё нет в реестре (не падать, печатать).
    """
    problems, awaiting = [], []
    declared_all = dict(declared)
    declared_all.update(new_jobs)

    for name in sorted(declared):
        if name not in actual:
            problems.append(
                f"задание {name} объявлено в JOBS установщика, но не"
                f" зарегистрировано в планировщике (ожидалось {declared[name]!r})")
        elif actual[name] != declared[name]:
            problems.append(
                f"задание {name}: в установщике расписание {declared[name]!r},"
                f" а в планировщике {actual[name]!r}")

    for name in sorted(new_jobs):
        if name not in actual:
            awaiting.append(name)
        elif actual[name] != new_jobs[name]:
            problems.append(
                f"новое задание {name}: в установщике расписание {new_jobs[name]!r},"
                f" а в планировщике {actual[name]!r}")

    for name in sorted(set(actual) - set(declared_all)):
        problems.append(
            f"задание {name} зарегистрировано в планировщике, но не объявлено"
            f" установщиком (в планировщике {actual[name]!r})")
    return problems, awaiting


def test_schedule_matches_scheduler_config(capsys):
    """Расхождение/пропажа/лишнее — падение; новое — список ожидания."""
    if not os.path.isfile(JOBS_JSON):
        pytest.skip(f"реестр планировщика не найден: {JOBS_JSON} —"
                    " сверка расписания невозможна на этой машине")
    declared, new_jobs = parse_installer(INSTALLER)
    actual = parse_scheduler_jobs(JOBS_JSON)
    assert actual, f"в {JOBS_JSON} нет ни одного задания tuber_*.sh"
    problems, awaiting = compare_schedules(declared, new_jobs, actual)
    if awaiting:
        print("ожидают регистрации владельцем:", ", ".join(awaiting))
    assert problems == [], (
        "расписание разошлось с установщиком; единый источник — блоки"
        " JOBS/NEW_JOBS в scripts/install_hermes_cron.sh:\n  - "
        + "\n  - ".join(problems))


def test_all_declared_jobs_have_shims_in_launchers():
    """Каждому объявленному заданию соответствует цель-обёртка в LAUNCHERS."""
    with open(INSTALLER, encoding="utf-8") as fh:
        text = fh.read()
    m = _block_re("LAUNCHERS").search(text)
    assert m, "в установщике нет блока LAUNCHERS"
    targets = set()
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        shim, *_ = line[1:-1].split(":")
        targets.add(shim.strip())
    declared, new_jobs = parse_installer(INSTALLER)
    missing = (set(declared) | set(new_jobs)) - targets
    assert not missing, f"нет цели-обёртки для заданий: {sorted(missing)}"


def test_comparison_catches_same_job_other_time():
    problems, awaiting = compare_schedules(
        {"tuber_x_report.sh": "0 7 * * *"}, {}, {"tuber_x_report.sh": "0 8 * * *"})
    assert not awaiting
    assert len(problems) == 1
    assert "tuber_x_report.sh" in problems[0]
    assert "'0 7 * * *'" in problems[0] and "'0 8 * * *'" in problems[0]


def test_comparison_catches_missing_and_extra():
    missing, _ = compare_schedules({"tuber_x_report.sh": "0 7 * * *"}, {}, {})
    assert missing and "не зарегистрировано" in missing[0]
    extra, _ = compare_schedules({}, {}, {"tuber_x_health.sh": "*/30 * * * *"})
    assert extra and "не объявлено" in extra[0]


def test_new_job_awaiting_is_not_a_problem():
    """Задание из NEW_JOBS без регистрации — ожидание, а не расхождение."""
    problems, awaiting = compare_schedules(
        {}, {"tuber_db_backup.sh": "0 5 * * *"}, {})
    assert problems == []
    assert awaiting == ["tuber_db_backup.sh"]


def test_new_job_registered_with_wrong_time_is_problem():
    problems, awaiting = compare_schedules(
        {}, {"tuber_db_backup.sh": "0 5 * * *"},
        {"tuber_db_backup.sh": "0 6 * * *"})
    assert awaiting == []
    assert problems and "новое задание" in problems[0]


# --------------------------------------------------------------------------- #
# Установка: реальные файлы (не симлинки), идемпотентность, --dry-run
# --------------------------------------------------------------------------- #
def _run_installer(scripts_dir, *args):
    env = dict(os.environ)
    env["HERMES_SCRIPTS_DIR"] = str(scripts_dir)
    env.setdefault("TUBER_PYTHON", shutil.which("python3") or "python3")
    return subprocess.run(["bash", INSTALLER, *args], env=env,
                          capture_output=True, text=True)


def test_installer_writes_real_files_and_is_idempotent(tmp_path):
    if shutil.which("bash") is None or not os.path.isfile(INSTALLER):
        pytest.skip("bash/установщик недоступны")
    scripts_dir = tmp_path / "scripts"
    first = _run_installer(scripts_dir)
    assert first.returncode == 0, first.stderr
    declared, new_jobs = parse_installer(INSTALLER)
    expected = set(declared) | set(new_jobs)
    made = {p.name for p in scripts_dir.glob("tuber_*.sh")}
    assert made == expected, f"надеты не те шимы: {sorted(made ^ expected)}"
    for name in expected:
        p = scripts_dir / name
        assert p.is_file() and not p.is_symlink(), f"{name} — не обычный файл"
        assert os.access(p, os.X_OK), f"{name} не исполняем"
    # Идемпотентность: повторный запуск даёт тот же набор и код 0.
    second = _run_installer(scripts_dir)
    assert second.returncode == 0, second.stderr
    made2 = {p.name for p in scripts_dir.glob("tuber_*.sh")}
    assert made2 == expected


def test_installer_removes_outdated_managed_shim(tmp_path):
    if shutil.which("bash") is None or not os.path.isfile(INSTALLER):
        pytest.skip("bash/установщик недоступны")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    stale = scripts_dir / "tuber_outdated_job.sh"
    stale.write_text("#!/usr/bin/env bash\n"
                     "# Управляется scripts/install_hermes_cron.sh (ТЗ-5)."
                     " Правка вручную будет перезаписана.\nexit 0\n",
                     encoding="utf-8")
    foreign = scripts_dir / "tuber_foreign.sh"
    foreign.write_text("#!/usr/bin/env bash\n# чужой файл\nexit 0\n",
                       encoding="utf-8")
    run = _run_installer(scripts_dir)
    assert run.returncode == 0, run.stderr
    assert not stale.exists(), "устаревший шим не удалён"
    assert foreign.exists(), "чужой файл удалён — установщик вышел за границы"


def test_installer_dry_run_writes_nothing(tmp_path):
    if shutil.which("bash") is None or not os.path.isfile(INSTALLER):
        pytest.skip("bash/установщик недоступны")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    run = _run_installer(scripts_dir, "--dry-run")
    assert run.returncode == 0, run.stderr
    assert list(scripts_dir.glob("*")) == [], "dry-run что-то записал"
    assert "ТЕСТОВЫЙ РЕЖИМ" in run.stdout
