"""ТЗ-13 Р2 (долг D-01): расписание живёт в двух местах — не дать им разойтись.

В ТЗ-11 установщик `scripts/install_hermes_cron.sh` печатал одно расписание, а в
планировщике Hermes стояло другое; расхождение по 4 заданиям из 10 нашлось только
ручной сверкой. Тест закрывает этот разрыв.

Как устроен:
  * единый источник — блок объявления заданий `JOBS=(...)` в установщике;
  * фактические задания проекта (`скрипты tuber_x_*.sh`) читаются из конфигурации
    планировщика Hermes (`/root/.hermes/cron/jobs.json`);
  * расписание и наличие каждого задания сверяются попарно; расхождение роняет
    тест с сообщением, где именно и что ожидалось (Р2.1);
  * отсутствие конфигурации планировщика (другая машина, чистая копия) — не
    падение, а явный `skip` с причиной (Р2.2);
  * сравнение проверяется и на синтетике, чтобы гарантированно ловить именно
    случай «то же задание, другое время» (Р2.3).

Пути переопределяются переменными окружения `TUBER_X_INSTALLER` и
`TUBER_X_HERMES_JOBS_JSON` — приёмка подставляет временную копию установщика с
нарочно изменённым временем и ждёт падения теста.
"""
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALLER = os.environ.get("TUBER_X_INSTALLER",
                           os.path.join(ROOT, "scripts", "x", "install_hermes_cron.sh"))
JOBS_JSON = os.environ.get("TUBER_X_HERMES_JOBS_JSON",
                           "/root/.hermes/cron/jobs.json")

def _block_re(name):
    return re.compile(r"^[ \t]*" + name + r"=\([ \t]*$\n(.*?)^[ \t]*\)[ \t]*$",
                      re.M | re.S)


def parse_installer_jobs(path):
    """Блок JOBS установщика: {имя скрипта-шима: cron-расписание} (единый источник).

    Строка вида `"tuber_x_report.sh:0 7 * * *:Tuber-x: ежедневный отчёт (report)"`.
    В подписи есть двоеточие, поэтому делим максимум на три части.
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = _block_re("JOBS").search(text)
    if not m:
        raise AssertionError(f"в {path} не найден блок JOBS=(...)")
    jobs = {}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not (line.startswith('"') and line.endswith('"')):
            raise AssertionError(f"строка блока JOBS не разобрана: {line!r}")
        name, expr, _label = line[1:-1].split(":", 2)
        jobs[name.strip()] = expr.strip()
    if not jobs:
        raise AssertionError("блок JOBS установщика пуст")
    return jobs


def parse_scheduler_jobs(path):
    """Задания проекта из конфигурации планировщика: {имя скрипта: cron-расписание}."""
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
        if not (base.startswith("tuber_x_") and base.endswith(".sh")):
            continue
        sched = job.get("schedule") or {}
        expr = sched.get("expr") if isinstance(sched, dict) else None
        jobs[base] = expr
    return jobs


def compare_schedules(declared, actual):
    """Список расхождений между объявленным и фактическим расписанием.

    Пусто — всё сошлось. Сообщение называет задание и обе стороны, чтобы правка
    была очевидной.
    """
    problems = []
    for name in sorted(declared):
        if name not in actual:
            problems.append(
                f"задание {name} объявлено в установщике, но не зарегистрировано"
                f" в планировщике (ожидалось {declared[name]!r})")
        elif actual[name] != declared[name]:
            problems.append(
                f"задание {name}: в установщике расписание {declared[name]!r},"
                f" а в планировщике {actual[name]!r}")
    for name in sorted(set(actual) - set(declared)):
        problems.append(
            f"задание {name} зарегистрировано в планировщике, но не объявлено"
            f" в установщике (в планировщике {actual[name]!r})")
    return problems


def test_schedule_matches_scheduler_config():
    """Р2.1/Р2.2: расписание и наличие заданий совпадают с установщиком."""
    if not os.path.isfile(JOBS_JSON):
        pytest.skip(f"конфигурация планировщика не найдена: {JOBS_JSON} —"
                    " сверка расписания невозможна на этой машине")
    declared = parse_installer_jobs(INSTALLER)
    actual = parse_scheduler_jobs(JOBS_JSON)
    assert actual, f"в {JOBS_JSON} нет ни одного задания tuber_x_*.sh"
    problems = compare_schedules(declared, actual)
    assert problems == [], (
        "расписание разошлось с установщиком (D-01); единый источник — блок"
        " JOBS=(...) в scripts/install_hermes_cron.sh:\n  - "
        + "\n  - ".join(problems))


def test_comparison_catches_same_job_other_time():
    """Р2.3: случай ТЗ-11 — то же задание, другое время — тест обязан поймать."""
    problems = compare_schedules({"tuber_x_report.sh": "0 7 * * *"},
                                 {"tuber_x_report.sh": "0 8 * * *"})
    assert len(problems) == 1
    assert "tuber_x_report.sh" in problems[0]
    assert "'0 7 * * *'" in problems[0] and "'0 8 * * *'" in problems[0]


def test_comparison_catches_missing_and_extra():
    """Наличие задания тоже сверяется: пропажа и лишнее — обе ошибки."""
    missing = compare_schedules({"tuber_x_report.sh": "0 7 * * *"}, {})
    assert missing and "не зарегистрировано" in missing[0]
    extra = compare_schedules({}, {"tuber_x_health.sh": "*/30 * * * *"})
    assert extra and "не объявлено" in extra[0]
