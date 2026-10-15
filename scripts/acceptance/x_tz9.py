#!/usr/bin/env python3
"""Приёмка ТЗ-9: запускалки Hermes — настоящие файлы-шимы вместо симлинков.

Проверки (все на настоящих данных, боевой каталог планировщика):
  0. реестр механизмов (блок LAUNCHERS установщика) разобран — источник истины
     для списка шимов; реестр и файлы `scripts/tuber_x_*.sh` согласованы;
  1. среди имён из реестра нет ни одного симлинка;
  2. все шимы реестра существуют, реальные, исполняемые (пропавший шим
     называется по имени — это отдельная ошибка, а не «расхождение»);
  3. содержимое каждого шима попарно содержит ожидаемую команду exec;
  4. `TUBER_X_LAUNCHER_DRYRUN=1 <шим>` печатает ровно одну ожидаемую строку и
     выходит 0 для КАЖДОГО шима реестра — реального запуска нет;
  5. штатный механизм планировщика
     (`/usr/local/lib/hermes-agent/tools/path_security.py::validate_within_dir`)
     принимает каждый путь реестра (возвращает None);
  6. файлы каталога, не входящие в реестр, не изменены и не удалены;
  7. рабочая БД `data/tuber_x.db` не изменена (снимки счётчиков ДО/ПОСЛЕ);
  8. каталог `reports/` не изменён (хэши файлов ДО/ПОСЛЕ);
  9. полный набор тестов зелёный;
 10. повторный запуск установщика идемпотентен (то же содержимое, тот же состав);
 11. итоговая строка установщика печатает число шимов из реестра и 0 симлинков.

ИСТОЧНИК СПИСКА (ТЗ-12 Р2). Число шимов здесь НЕ хардкодится: список берётся из
реестра механизмов — блока `LAUNCHERS=(...)` в `scripts/install_hermes_cron.sh`,
того же, по которому установщик создаёт файлы. Раньше в инструменте стояло
«десять», а после ТЗ-11 задач стало одиннадцать: пропавший из списка
`tuber_x_discover.sh` считался посторонним файлом, и инструмент печатал ложное
расхождение. Реестр теперь сверяется ещё и с каталогом `scripts/tuber_x_*.sh`:
новый рецепт без шима (или шим без рецепта) — это отдельная явная ошибка.

ЖЁСТКИЕ ПРАВИЛА:
  * рабочая БД открывается ТОЛЬКО на чтение (`file:...?mode=ro`);
  * реальный сбор/обогащение/классификация НЕ запускаются: топология тира
    проверяется dryrun-режимом шима;
  * в планировщике Hermes ничего не регистрируется, в crontab не пишется;
  * никаких массовых `rm` — установщик удаляет строго по именам из реестра.

Запуск: python3 tools/acceptance_tz9.py
Вывод:  docs/acceptance-log-9.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import config  # noqa: E402

HERMES_DIR = os.environ.get("HERMES_SCRIPTS_DIR", "/root/.hermes/scripts")
INSTALLER = os.path.join(ROOT, "scripts", "install_hermes_cron.sh")
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
REPORTS_DIR = os.path.join(ROOT, "reports")
DB_PATH = os.path.realpath(config.DB_PATH)

_LAUNCHERS_RE = re.compile(r"^[ \t]*LAUNCHERS=\([ \t]*$\n(.*?)^[ \t]*\)[ \t]*$",
                           re.M | re.S)
_ENTRY_RE = re.compile(r'^"([^":]+):([^":]+):([^"]*)"$')


def read_registry(path):
    """Реестр механизмов из установщика: [(шим, обёртка, аргумент), ...].

    Возвращает (entries, error). Единый источник истины — блок LAUNCHERS, по
    которому установщик создаёт шимы; список нигде не дублируется.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return None, f"установщик не читается: {exc}"
    m = _LAUNCHERS_RE.search(text)
    if not m:
        return None, f"в {os.path.basename(path)} не найден блок LAUNCHERS=(...)"
    entries = []
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m2 = _ENTRY_RE.match(line)
        if not m2:
            return None, f"строка реестра не разобрана: {line!r}"
        entries.append((m2.group(1), m2.group(2), m2.group(3)))
    if not entries:
        return None, "реестр LAUNCHERS пуст"
    return entries, None


def registry_commands(entries, project):
    """Ожидаемая команда шима — та же, что печатает dryrun установщика."""
    exp = {}
    for shim, wrapper, arg in entries:
        cmd = f"exec {project}/scripts/{wrapper}"
        if arg:
            cmd += f" {arg}"
        exp[shim] = cmd
    return exp


def project_wrappers():
    """Рецепты проекта: файлы scripts/tuber_x_*.sh."""
    if not os.path.isdir(SCRIPTS_DIR):
        return []
    return sorted(n for n in os.listdir(SCRIPTS_DIR)
                  if n.startswith("tuber_x_") and n.endswith(".sh"))


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


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_snapshot(d):
    snap = {}
    for name in os.listdir(d):
        full = os.path.join(d, name)
        if os.path.islink(full) or not os.path.isfile(full):
            snap[name] = ("nonregular", None)
        else:
            snap[name] = ("file", sha256_file(full))
    return snap


def prod_snapshot(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        snap = {
            "user_version": con.execute("PRAGMA user_version").fetchone()[0],
            "posts": con.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "stories": con.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
            "scores": con.execute("SELECT COUNT(*) FROM scores").fetchone()[0],
            "report_texts": con.execute("SELECT COUNT(*) FROM report_texts").fetchone()[0],
            "classified": tuple(con.execute(
                "SELECT status, COUNT(*) FROM classified GROUP BY status"
                " ORDER BY status").fetchall()),
            "accounts": tuple(con.execute(
                "SELECT tier, status, COUNT(*) FROM accounts"
                " GROUP BY tier, status ORDER BY tier, status").fetchall()),
        }
    finally:
        con.close()
    return snap


def short(snap):
    return (f"user_version={snap['user_version']} posts={snap['posts']} "
            f"stories={snap['stories']} scores={snap['scores']} "
            f"report_texts={snap['report_texts']} classified={list(snap['classified'])}")


def run_installer():
    return subprocess.run([INSTALLER], cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ))


def main():
    started = datetime.now(timezone.utc)
    hr("ПРИЁМКА ТЗ-9 (задачи 1–4): шимы вместо симлинков")
    p(f"начало:              {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"каталог планировщика: {HERMES_DIR}")
    p(f"рабочая БД (RO):      {DB_PATH}")
    p(f"установщик:           {INSTALLER}")

    if not os.path.exists(DB_PATH):
        p(f"ОШИБКА: рабочая БД не найдена: {DB_PATH}")
        return 2

    # --- реестр механизмов: источник истины для списка шимов (ТЗ-12 Р2) -------
    hr("РЕЕСТР МЕХАНИЗМОВ. Блок LAUNCHERS установщика")
    entries, reg_err = read_registry(INSTALLER)
    if reg_err:
        check(0, "реестр механизмов (LAUNCHERS) разобран", False, reg_err)
        p("")
        p("ОСТАНОВ: без реестра список ожидаемых шимов неизвестен — молча")
        p("подставлять число нельзя (это и был дефект D-02).")
        _save_log()
        return 2
    EXPECTED = registry_commands(entries, ROOT)
    wrappers_registry = sorted({w for _, w, _ in entries})
    wrappers_on_disk = project_wrappers()
    check(0, "реестр механизмов (LAUNCHERS) разобран", True,
          f"шимов={len(EXPECTED)}; обёрток в реестре={len(wrappers_registry)}")
    for shim, wrapper, arg in entries:
        p(f"   {shim:24s} -> scripts/{wrapper}{(' ' + arg) if arg else ''}")

    # реестр ↔ каталог рецептов: ни рецепта без шима, ни шима без рецепта
    no_shim = sorted(set(wrappers_on_disk) - set(wrappers_registry))
    no_recipe = sorted(set(wrappers_registry) - set(wrappers_on_disk))
    check(12, "реестр и рецепты scripts/tuber_x_*.sh согласованы попарно",
          not no_shim and not no_recipe,
          f"рецептов={len(wrappers_on_disk)}; без шима={no_shim}; "
          f"без рецепта={no_recipe}")

    # --- снимки ДО (посторонние файлы каталога, reports, БД) -------------------
    hermes_before = dir_snapshot(HERMES_DIR)
    foreign_before = {k: v for k, v in hermes_before.items() if k not in EXPECTED}
    reports_before = dir_snapshot(REPORTS_DIR)
    db_before = prod_snapshot(DB_PATH)
    p("")
    p(f"файлов в каталоге ДО: {len(hermes_before)} (посторонних {len(foreign_before)})")
    p(f"статусы БД ДО:        {short(db_before)}")

    # Пропавший ожидаемый шим — это отдельная ошибка с именем, а не «расхождение».
    missing_before = [n for n in EXPECTED if not os.path.exists(os.path.join(HERMES_DIR, n))]
    if missing_before:
        p("")
        p(f"ВНИМАНИЕ: до прогона установщика в каталоге отсутствуют шимы реестра:"
          f" {len(missing_before)}")
        for n in missing_before:
            p(f"   отсутствует: {n}")
        p("Это FAIL (ТЗ-12 Р2): инструмент не имеет права молчать о пропаже шима.")

    # --- запуск установщика на боевом каталоге --------------------------------
    hr("ЗАДАЧА 1/2. Запуск установщика (боевой каталог планировщика)")
    proc = run_installer()
    p("--- вывод установщика ---")
    for line in proc.stdout.splitlines():
        p("   " + line)
    if proc.stderr.strip():
        p("--- stderr ---")
        for line in proc.stderr.strip().splitlines():
            p("   " + line)

    n_shims = len(EXPECTED)

    # [1] нет симлинков среди имён реестра
    symlinks = [n for n in EXPECTED
                if os.path.islink(os.path.join(HERMES_DIR, n))]
    check(1, f"среди {n_shims} имён реестра нет ни одного симлинка (os.path.islink==False)",
          proc.returncode == 0 and symlinks == [],
          f"rc={proc.returncode}; симлинков={len(symlinks)}; {symlinks}")

    # [2] все шимы реестра существуют, реальные, исполняемые
    bad = []
    for n in EXPECTED:
        f = os.path.join(HERMES_DIR, n)
        if not (os.path.exists(f) and os.path.isfile(f) and not os.path.islink(f)
                and os.access(f, os.X_OK)):
            bad.append(n)
    check(2, f"все {n_shims} шимов реестра существуют, обычные (не ссылки), исполняемые",
          bad == [] and not missing_before,
          f"проверено={n_shims}; проблемных={len(bad)}; {bad};"
          f" отсутствовало до прогона={len(missing_before)}")

    # [3] содержимое попарно совпадает с реестром
    mism = []
    for n, cmd in EXPECTED.items():
        path = os.path.join(HERMES_DIR, n)
        if not os.path.exists(path):
            mism.append((n, "файл отсутствует"))
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if cmd not in text:
            mism.append((n, cmd))
    check(3, f"содержимое каждого шима содержит команду exec из реестра ({n_shims}/{n_shims})",
          mism == [], f"совпало={n_shims - len(mism)}/{n_shims}; {mism}")

    # [4] dryrun всех шимов реестра
    hr("ЗАДАЧА 1. dryrun: топология «запускалка → обёртка» без реального запуска")
    dry_env = dict(os.environ)
    dry_env["TUBER_X_LAUNCHER_DRYRUN"] = "1"
    dry_ok = True
    for n, cmd in EXPECTED.items():
        path = os.path.join(HERMES_DIR, n)
        if not os.path.exists(path):
            dry_ok = False
            p(f"   BAD {n}: шима нет в каталоге")
            continue
        r = subprocess.run([path], capture_output=True, text=True, env=dry_env)
        ok = (r.returncode == 0 and r.stdout == cmd + "\n")
        dry_ok = dry_ok and ok
        p(f"   {'OK ' if ok else 'BAD'} {n}: stdout={r.stdout!r} rc={r.returncode}")
    check(4, f"dryrun каждого из {n_shims} шимов печатает ровно команду реестра и выходит 0",
          dry_ok, f"проверено {n_shims}/{n_shims}")

    # [5] validate_within_dir принимает все пути реестра
    hr(f"ЗАДАЧА 4.5. Штатный механизм планировщика принимает все {n_shims} путей")
    hermes_root = "/usr/local/lib/hermes-agent"
    sys.path.insert(0, hermes_root)
    try:
        from tools.path_security import validate_within_dir  # noqa: E402
        imported = True
    except Exception as exc:  # pragma: no cover
        imported = False
        p(f"   ОШИБКА импорта path_security: {exc!r}")
    from pathlib import Path  # noqa: E402

    verdicts = {}
    if imported:
        root = Path(HERMES_DIR)
        for n in EXPECTED:
            verdicts[n] = validate_within_dir(root / n, root)
    rejected = {n: v for n, v in verdicts.items() if v is not None}
    for n in sorted(EXPECTED):
        p(f"   {n:24s} -> {verdicts.get(n)}")
    check(5, f"validate_within_dir принимает каждый из {n_shims} шимов (вердикт None)",
          imported and rejected == {},
          f"импорт={'да' if imported else 'нет'}; принято={len(verdicts) - len(rejected)}/{n_shims}")

    # [6] посторонние файлы каталога не изменены/не удалены --------------------
    hr("ЗАДАЧА 2. Посторонние файлы каталога планировщика")
    hermes_after = dir_snapshot(HERMES_DIR)
    foreign_after = {k: v for k, v in hermes_after.items() if k not in EXPECTED}
    check(6, f"файлы каталога вне реестра ({len(EXPECTED)} имён) не изменены и не удалены",
          foreign_before == foreign_after,
          f"было={len(foreign_before)} стало={len(foreign_after)}; "
          f"совпало={foreign_before == foreign_after}")

    # [8] reports не изменён (снимем сейчас; сбор не запускался) ---------------
    reports_after = dir_snapshot(REPORTS_DIR)
    check(8, "каталог reports/ не изменён (хэши файлов совпали)",
          reports_before == reports_after,
          f"файлов={len(reports_after)}; совпало={reports_before == reports_after}")

    # [10] повторный запуск идемпотентен ---------------------------------------
    hr("ЗАДАЧА 2.1. Повторный запуск установщика (идемпотентность)")
    content_first = {}
    for n in EXPECTED:
        path = os.path.join(HERMES_DIR, n)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                content_first[n] = fh.read()
    proc2 = run_installer()
    content_second = {}
    for n in EXPECTED:
        path = os.path.join(HERMES_DIR, n)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                content_second[n] = fh.read()
    hermes_second = dir_snapshot(HERMES_DIR)
    same_content = content_first == content_second
    same_count = len(hermes_second) == len(hermes_after)
    check(10, "повторный запуск идемпотентен: содержимое шимов и состав каталога те же",
          proc2.returncode == 0 and same_content and same_count,
          f"rc={proc2.returncode}; содержимое совпало={same_content}; "
          f"файлов {len(hermes_after)}->{len(hermes_second)}")

    # строка установщика про шимы и 0 симлинков
    want_line = f"шимы установлены: {n_shims} (симлинков: 0)"
    line_ok = want_line in proc.stdout
    check(11, f"итоговая строка установщика: '{want_line}'",
          line_ok, "найдена" if line_ok else "не найдена")

    # [9] тесты ---------------------------------------------------------------
    hr("ТЕСТЫ")
    proc_t = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                            capture_output=True, text=True, env=dict(os.environ))
    tail = (proc_t.stdout or "").strip().splitlines()[-1] if proc_t.stdout else ""
    check(9, "полный набор тестов зелёный", proc_t.returncode == 0,
          f"pytest rc={proc_t.returncode}; {tail}")

    # [7] рабочая БД не изменена -----------------------------------------------
    hr("ЗАДАЧА 4.7. Рабочая БД не изменена приёмкой")
    db_after = prod_snapshot(DB_PATH)
    p(f"статусы БД ДО:    {short(db_before)}")
    p(f"статусы БД ПОСЛЕ: {short(db_after)}")
    check(7, "рабочая БД не изменена приёмкой (снимки счётчиков ДО/ПОСЛЕ совпали)",
          db_before == db_after,
          f"posts {db_before['posts']}->{db_after['posts']}; совпало={db_before == db_after}")

    # --------------------------------------------------------- сводка
    hr("СВОДКА")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        p(f"  {r['num']:>3} {'OK  ' if r['ok'] else 'FAIL'} {r['what']}")

    _save_log()
    return 0 if all(r["ok"] for r in _results) else 1


def _save_log():
    out_path = os.path.join(ROOT, "docs", "acceptance-log-9.txt")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")


if __name__ == "__main__":
    sys.exit(main())
