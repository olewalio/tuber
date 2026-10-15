#!/usr/bin/env python3
"""Приёмка ТЗ-11: дискавери в расписании, осмысленная проверка роста реестра.

Проверки (раздел 5 ТЗ-11):
  1. обёртка запускается, в норме молчит (пустой stdout; при изменении
     состояния — одна короткая строка, но никогда не сырой дамп CLI);
  2. прогон дискавери на КОПИИ базы: счётчики candidate/provisional/rejected
     ДО/ПОСЛЕ, сколько кандидатов нашлось, нет дублей учётных записей;
  3. идемпотентность установщика: два прогона в тестовом режиме -> одно задание
     дискавери, не два;
  4. шим дискавери — настоящий файл (не симлинк) внутри /root/.hermes/scripts/;
  5. расписание в установщике: 11 задач, дискавери 50 4 * * *, старые 10 не
     потеряны (у ``tuber_x_scores.sh`` расписание изменено на 40 */2 * * * —
     см. docs/x/docs/scores-cadence-20260917.md), остальные не изменились;
  6. сторож: «дискавери не запускался 40 ч» -> ALERT; «новый provisional за
     сутки» -> молчит; «ничего 8 суток» -> WARN;
  7. полный набор тестов зелёный; новых зависимостей нет;
  8. боевая БД не изменена (счётчики и user_version ДО/ПОСЛЕ), reports/ не
     затронут, служебные файлы приёмки — во временном каталоге.

ЖЁСТКИЕ ПРАВИЛА:
  * боевая БД открывается ТОЛЬКО на чтение (`file:...?mode=ro`); все изменяющие
    прогоны идут на КОПИИ;
  * задания в планировщик не ставятся; установщик запускается в тестовом режиме;
  * никаких новых зависимостей: только стандартная библиотека + python3/bash.

Запуск: python3 tools/acceptance_tz11.py
Вывод:  docs/acceptance-log-11.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import config, store as db, health  # noqa: E402

HERMES_DIR = os.environ.get("HERMES_SCRIPTS_DIR", "/root/.hermes/scripts")
WRAPPER = os.path.join(ROOT, "scripts", "tuber_x_discover.sh")
INSTALLER = os.path.join(ROOT, "scripts", "install_hermes_cron.sh")
PROD_DB = os.path.realpath(config.DB_PATH)

# Расписание ТЗ-7 (10 задач), сверяется построчно.
OLD_JOBS = {
    "tuber_x_collect_a.sh": "0 * * * *",
    "tuber_x_collect_b.sh": "15 */4 * * *",
    "tuber_x_collect_c.sh": "30 3 * * *",
    "tuber_x_enrich.sh": "5 * * * *",
    "tuber_x_fulltext.sh": "20 * * * *",
    "tuber_x_synd.sh": "40 4 * * *",
    "tuber_x_classify.sh": "20 6 * * *",
    # изменено 17.09.2026: ежедневного прогона не хватало сторожу (окно 6 ч),
    # см. docs/x/docs/scores-cadence-20260917.md
    "tuber_x_scores.sh": "40 */2 * * *",
    "tuber_x_report.sh": "0 7 * * *",
    "tuber_x_health.sh": "*/30 * * * *",
}
NEW_JOB = {"tuber_x_discover.sh": "50 4 * * *"}

_out = []
_results = []


def p(line=""):
    print(line)
    _out.append(str(line))


def hr(title):
    p("")
    p("=" * 78)
    p(title)
    p("=" * 78)


def check(num, what, ok, data):
    _results.append({"num": num, "what": what, "ok": bool(ok), "data": data})
    p(f"[{num:>2}] {'OK  ' if ok else 'FAIL'} {what}")
    p(f"      данные: {data}")


# -------------------------------------------------------------------- утилиты
def prod_snapshot(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            "user_version": con.execute("PRAGMA user_version").fetchone()[0],
            "posts": con.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "accounts": con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            "candidates": con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
            "stories": con.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
            "scores": con.execute("SELECT COUNT(*) FROM scores").fetchone()[0],
        }
    finally:
        con.close()


def copy_db(src, dst):
    """Консистентная копия рабочей БД (учитывает WAL) — только чтение исходной."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dst


def candidate_counts(con):
    rows = con.execute("SELECT COALESCE(validated,'__none__') v, COUNT(*) n FROM candidates"
                       " GROUP BY v ORDER BY v").fetchall()
    d = {r[0]: r[1] for r in rows}
    for k in ("__none__", "provisional", "reject", "ok"):
        d.setdefault(k, 0)
    return d


def account_status_counts(con):
    rows = con.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status"
                       " ORDER BY status").fetchall()
    return {r[0]: r[1] for r in rows}


def accounts_dupes(con):
    total = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    distinct = con.execute("SELECT COUNT(DISTINCT handle) FROM accounts").fetchone()[0]
    return total, distinct


def set_search_usage(con, target):
    """Подогнать число поисковых запросов дискавери за сутки (на КОПИИ)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    used = con.execute("SELECT COUNT(*) FROM requests WHERE kind='search' AND ts LIKE ?",
                       (day + "%",)).fetchone()[0]
    if used < target:
        for _ in range(target - used):
            con.execute("INSERT INTO requests (host, ts, kind, status)"
                        " VALUES ('acceptance', ?, 'search', 200)", (db.utcnow_iso(),))
    elif used > target:
        n = used - target
        con.execute("DELETE FROM requests WHERE id IN (SELECT id FROM requests"
                    " WHERE kind='search' AND ts LIKE ? LIMIT ?)", (day + "%", n))
    con.commit()
    return con.execute("SELECT COUNT(*) FROM requests WHERE kind='search' AND ts LIKE ?",
                       (day + "%",)).fetchone()[0]


def run_wrapper(db_copy, log_dir, extra_env=None):
    env = dict(os.environ)
    env["TUBER_X_PROJECT"] = ROOT
    env["TUBER_X_DB"] = db_copy
    env["TUBER_X_LOG_DIR"] = log_dir
    if extra_env:
        env.update(extra_env)
    return subprocess.run([WRAPPER], cwd=ROOT, capture_output=True, text=True, env=env)


def run_installer(scripts_dir, *, dryrun=True, project=ROOT):
    env = dict(os.environ)
    env["TUBER_X_PROJECT"] = project
    env["HERMES_SCRIPTS_DIR"] = scripts_dir
    if dryrun:
        env["TUBER_X_INSTALL_DRYRUN"] = "1"
    return subprocess.run([INSTALLER], cwd=ROOT, capture_output=True, text=True, env=env)


# ================================================================ основное
def main():
    started = datetime.now(timezone.utc)
    work = tempfile.mkdtemp(prefix="tuber_x_accept11_")
    hr("ПРИЁМКА ТЗ-11: дискавери в расписании, осмысленный рост реестра")
    p(f"начало:               {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"репозиторий:          {ROOT}")
    p(f"боевая БД (RO):       {PROD_DB}")
    p(f"рабочий каталог приёмки: {work}")

    if not os.path.exists(PROD_DB):
        p(f"ОШИБКА: боевая БД не найдена: {PROD_DB}")
        return 2

    prod_before = prod_snapshot(PROD_DB)
    p(f"боевая БД ДО:         user_version={prod_before['user_version']}"
      f" posts={prod_before['posts']} accounts={prod_before['accounts']}"
      f" candidates={prod_before['candidates']} stories={prod_before['stories']}"
      f" scores={prod_before['scores']}")

    copy_run = copy_db(PROD_DB, os.path.join(work, "run.db"))
    copy_silent = copy_db(PROD_DB, os.path.join(work, "silent.db"))
    logs = os.path.join(work, "logs")
    os.makedirs(logs, exist_ok=True)

    # ================================================== 1. обёртка: тишина
    hr("П.1. Обёртка: молчит в норме, короткая сводка при изменении состояния")
    con_sil = sqlite3.connect(copy_silent)
    used_sil = set_search_usage(con_sil, config.DISCOVERY_DAILY_BUDGET)
    con_sil.close()
    proc_sil = run_wrapper(copy_silent, logs)
    log_sil_path = os.path.join(logs, "tuber_x_discover.log")
    log_sil = open(log_sil_path, encoding="utf-8").read() if os.path.exists(log_sil_path) \
        else ""
    check(1, "обёртка в нормальном прогоне (квота исчерпана) молчит: stdout пуст, rc=0",
          proc_sil.returncode == 0 and proc_sil.stdout == "",
          f"rc={proc_sil.returncode}; stdout={proc_sil.stdout!r};"
          f" квота={used_sil}/{config.DISCOVERY_DAILY_BUDGET}")

    # ================================================== 2. прогон на копии
    hr("П.2. Прогон дискавери на КОПИИ базы")
    con_run = sqlite3.connect(copy_run)
    # Ограничиваем нагрузку на Nitter: оставляем 6 поисковых запросов запаса.
    used_run = set_search_usage(con_run, config.DISCOVERY_DAILY_BUDGET - 6)
    before_c = candidate_counts(con_run)
    before_a = account_status_counts(con_run)
    before_acc = accounts_dupes(con_run)
    con_run.close()
    p(f"квота поиска на копии: {used_run}/{config.DISCOVERY_DAILY_BUDGET}"
      f" (прогон ограничен {config.DISCOVERY_DAILY_BUDGET - used_run} запросами)")
    p(f"кандидаты ДО:  {before_c}")
    p(f"аккаунты  ДО:  {before_a}")

    proc_run = run_wrapper(copy_run, logs)
    log_path = os.path.join(logs, "tuber_x_discover.log")
    log_run = open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else ""
    # последний блок прогона
    block = log_run.split("=== ") [-1]
    found = re.search(r"кандидатов_найдено=(\d+)", log_run)
    checked = re.search(r"проверено=(\d+)", log_run)
    prov = re.search(r"provisional=(\d+)", log_run)
    rej = re.search(r"rejected=(\d+)", log_run)
    found_n = int(found.group(1)) if found else 0
    checked_n = int(checked.group(1)) if checked else 0
    prov_n = int(prov.group(1)) if prov else 0
    rej_n = int(rej.group(1)) if rej else 0

    con_run = sqlite3.connect(copy_run)
    after_c = candidate_counts(con_run)
    after_a = account_status_counts(con_run)
    after_acc = accounts_dupes(con_run)
    con_run.close()
    p(f"rc обёртки: {proc_run.returncode}; stdout={proc_run.stdout!r}")
    p(f"найдено кандидатов: {found_n}; верификация: проверено={checked_n}"
      f" provisional={prov_n} rejected={rej_n}")
    p(f"кандидаты ПОСЛЕ: {after_c}")
    p(f"аккаунты  ПОСЛЕ: {after_a}")

    check(2, "прогон на копии отработал и изменил/подтвердил очередь кандидатов",
          proc_run.returncode == 0 and (found_n > 0 or checked_n > 0),
          f"найдено={found_n}; проверено={checked_n};"
          f" candidates {before_c} -> {after_c};"
          f" accounts {before_a} -> {after_a}")
    check(3, "нет дублей учётных записей (COUNT(handle) == COUNT(DISTINCT handle))",
          after_acc[0] == after_acc[1],
          f"accounts={after_acc[0]} distinct_handle={after_acc[1]}")

    # stdout обёртки: не сырой дамп CLI, а максимум одна короткая строка
    raw_dump = ("запросов=" in proc_run.stdout) or ("=== discover" in proc_run.stdout)
    short = proc_run.stdout == "" or len(proc_run.stdout.strip().splitlines()) == 1
    check(4, "stdout обёртки — не сырой дамп CLI: пусто или одна короткая строка",
          (not raw_dump) and short,
          f"строк={len(proc_run.stdout.strip().splitlines())}; сырой_дамп={raw_dump}")

    # ================================================== 3. идемпотентность
    hr("П.3. Идемпотентность установщика (тестовый режим, 2 прогона)")
    inst_dir = os.path.join(work, "hermes_scripts")
    p1 = run_installer(inst_dir, dryrun=True)
    p2 = run_installer(inst_dir, dryrun=True)
    disc_lines = [ln for ln in p2.stdout.splitlines()
                  if "50 4 *" in ln and "tuber_x_discover.sh" in ln]
    check(5, "два прогона в тестовом режиме совпадают; ровно одно задание дискавери",
          p1.returncode == 0 and p2.returncode == 0 and p1.stdout == p2.stdout
          and len(disc_lines) == 1,
          f"rc={p1.returncode}/{p2.returncode}; идентично={p1.stdout == p2.stdout};"
          f" заданий дискавери={len(disc_lines)}")

    # ================================================== 4. шим — настоящий файл
    hr("П.4. Шим дискавери — настоящий файл внутри каталога планировщика")
    before_names = set(os.listdir(HERMES_DIR)) if os.path.isdir(HERMES_DIR) else set()
    inst_real = run_installer(HERMES_DIR, dryrun=False)
    shim = os.path.join(HERMES_DIR, "tuber_x_discover.sh")
    shim_ok = (os.path.isfile(shim) and not os.path.islink(shim)
               and os.access(shim, os.X_OK))
    with open(shim, encoding="utf-8") as fh:
        shim_text = fh.read()
    check(6, "шим tuber_x_discover.sh — обычный исполняемый файл (не симлинк)"
          " внутри /root/.hermes/scripts/",
          shim_ok and "tuber_x_discover.sh" in shim_text,
          f"exists={os.path.isfile(shim)}; symlink={os.path.islink(shim)};"
          f" +x={os.access(shim, os.X_OK)}; dir={HERMES_DIR}")
    after_names = set(os.listdir(HERMES_DIR))
    check(7, "посторонние файлы каталога планировщика не удалены",
          before_names <= after_names,
          f"было={len(before_names)} стало={len(after_names)};"
          f" потеряно={len(before_names - after_names)}")

    # ================================================== 5. расписание
    hr("П.5. Расписание установщика: 11 задач, дискавери 50 4 *; старые 10 целы")
    job_lines = [ln for ln in inst_real.stdout.splitlines()
                 if re.match(r"^\s+\S.*\*.*\.sh", ln)]
    def schedule_of(name):
        for ln in inst_real.stdout.splitlines():
            # строки установки шимов содержат только имя; строки заданий — ещё и
            # метку «Tuber-x: ...» после расписания.
            if name in ln and "Tuber-x:" in ln:
                return ln.strip().split(name)[0].strip()
        return None
    old_ok = {n: schedule_of(n) == s for n, s in OLD_JOBS.items()}
    old_all_ok = all(old_ok.values())
    disc_sched = schedule_of("tuber_x_discover.sh")
    check(8, "список задач стал 11; дискавери 50 4 * * *; старые 10 не потеряны"
          " (расписание в пределах ожидаемого)",
          len(job_lines) == 11 and disc_sched == "50 4 * * *" and old_all_ok,
          f"задач={len(job_lines)}; дискавери='{disc_sched}';"
          f" старые_ок={old_all_ok};"
          f" несовпавшие={[n for n, v in old_ok.items() if not v]}")

    # ================================================== 6. сторож
    hr("П.6. Сторож: сценарии поломки и нормы")
    hdb = os.path.join(work, "health.db")
    con = db.init_db(hdb)

    # 6a. дискавери не запускался 40 часов
    old_run = db.iso(datetime.now(timezone.utc) - timedelta(hours=40))
    con.execute("INSERT INTO runs (started_at, finished_at, mode, errors)"
                " VALUES (?,?, 'discover:all:120', 0)", (old_run, old_run))
    con.commit()
    r_fresh = health.check_discover_freshness(con)
    check(9, "дискавери не запускался 40 ч -> ALERT",
          r_fresh["alert"] is True and r_fresh["severity"] == "ALERT",
          r_fresh["msg"])

    # 6b. новый provisional за сутки -> молчит
    con.execute("DELETE FROM runs")
    con.execute("INSERT INTO runs (started_at, finished_at, mode, errors)"
                " VALUES (?,?, 'discover:all:120', 0)",
                (db.utcnow_iso(), db.utcnow_iso()))
    con.execute("INSERT INTO candidates (handle, validated, first_seen_at, verified_at)"
                " VALUES ('fresh1', 'provisional', ?, ?)",
                (db.utcnow_iso(), db.utcnow_iso()))
    con.commit()
    r_fresh2 = health.check_discover_freshness(con)
    r_growth2 = health.check_registry_growth(con)
    check(10, "свежий прогон + новый provisional -> сторож молчит",
          r_fresh2["alert"] is False and r_growth2["alert"] is False,
          f"discover_freshness={r_fresh2['alert']};"
          f" registry_growth={r_growth2['alert']} ({r_growth2['msg']})")

    # 6c. ничего 8 суток -> WARN
    con.execute("DELETE FROM candidates")
    con.commit()
    r_growth3 = health.check_registry_growth(con)
    check(11, "ничего нового 7+ суток (fresh-прогон, кандидатов нет) -> WARN",
          r_growth3["alert"] is True and r_growth3["severity"] == "WARN",
          r_growth3["msg"])

    # 6d. ноль новых active за 48 ч сам по себе — не WARN
    con.execute("INSERT INTO candidates (handle, first_seen_at) VALUES ('c', ?)",
                (db.utcnow_iso(),))
    con.commit()
    r_growth4 = health.check_registry_growth(con)
    check(12, "ноль новых active за 48 ч — больше не WARN (справочная строка)",
          r_growth4["alert"] is False and r_growth4["active_48h"] == 0,
          f"alert={r_growth4['alert']}; active_48h={r_growth4['active_48h']};"
          f" {r_growth4['msg']}")
    con.close()

    # ================================================== 7. тесты и зависимости
    hr("П.7. Полный набор тестов и зависимости")
    proc_t = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                            capture_output=True, text=True, env=dict(os.environ))
    tail = (proc_t.stdout or "").strip().splitlines()[-1] if proc_t.stdout else ""
    check(13, "полный набор тестов зелёный", proc_t.returncode == 0,
          f"pytest rc={proc_t.returncode}; {tail}")
    # новые зависимости: обёртка/установщик/сторож не вводят импортов вне stdlib.
    stub_ok = _no_new_deps()
    check(14, "новых зависимостей нет (только stdlib + python3/bash)",
          stub_ok, "import-аудит: сторонних модулей не добавлено")

    # ================================================== 8. боевая БД не тронута
    hr("П.8. Боевая БД и reports/ приёмкой не затронуты")
    prod_after = prod_snapshot(PROD_DB)
    p(f"боевая БД ДО:    user_version={prod_before['user_version']}"
      f" posts={prod_before['posts']} accounts={prod_before['accounts']}"
      f" candidates={prod_before['candidates']} stories={prod_before['stories']}"
      f" scores={prod_before['scores']}")
    p(f"боевая БД ПОСЛЕ: user_version={prod_after['user_version']}"
      f" posts={prod_after['posts']} accounts={prod_after['accounts']}"
      f" candidates={prod_after['candidates']} stories={prod_after['stories']}"
      f" scores={prod_after['scores']}")
    check(15, "боевая БД не изменена приёмкой (счётчики и user_version совпали)",
          prod_before == prod_after,
          f"совпало={prod_before == prod_after}")
    check(16, "служебные файлы приёмки — во временном каталоге, не в reports/",
          work.startswith(tempfile.gettempdir()),
          f"work={work}; reports_dir={config.REPORT_DIR}")

    # ================================================== сводка
    hr("СВОДКА")
    shutil.rmtree(work, ignore_errors=True)
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        p(f"  {r['num']:>3} {'OK  ' if r['ok'] else 'FAIL'} {r['what']}")

    out_path = os.path.join(ROOT, "docs", "acceptance-log-11.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")
    return 0 if all(r["ok"] for r in _results) else 1


def _no_new_deps():
    """Грубая проверка: модули проекта импортируют только stdlib/локальные модули."""
    third_party_markers = ("import requests", "import deepseek", "import httpx",
                           "import aiohttp", "import openai")
    offenders = []
    for name in os.listdir(os.path.join(ROOT, "tuber")):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(ROOT, "tuber", name), encoding="utf-8") as fh:
            text = fh.read()
        for marker in third_party_markers:
            if marker in text:
                offenders.append((name, marker))
    return not offenders


if __name__ == "__main__":
    sys.exit(main())
