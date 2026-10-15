#!/usr/bin/env python3
"""Приёмка ТЗ-7 на КОПИИ рабочей БД.

ЖЁСТКИЕ ПРАВИЛА:
  * рабочая БД открывается ТОЛЬКО на чтение (`file:...?mode=ro`);
  * копия делается через sqlite3 backup API (во временном каталоге); все
    прогоны идут на копии: env `TUBER_X_DB=<копия>`;
  * установщик запускается с подменённым `HERMES_SCRIPTS_DIR` (тестовый режим),
    поэтому боевой каталог планировщика и системный crontab не меняются;
  * секреты не печатаются;
  * в конце печатаются статусы рабочей БД ДО и ПОСЛЕ и обязательная строка
    «рабочая БД не изменена: ДА/НЕТ».

Запуск: python3 tools/acceptance_tz7.py
Вывод:  docs/acceptance-log-7.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import config  # noqa: E402

_out = []
_results = []

LAUNCHERS = {
    "tuber_x_collect_a.sh": "tuber_x_collect.sh",
    "tuber_x_collect_b.sh": "tuber_x_collect.sh",
    "tuber_x_collect_c.sh": "tuber_x_collect.sh",
    "tuber_x_enrich.sh": "tuber_x_enrich.sh",
    "tuber_x_fulltext.sh": "tuber_x_fulltext.sh",
    "tuber_x_classify.sh": "tuber_x_classify.sh",
    "tuber_x_scores.sh": "tuber_x_scores.sh",
    "tuber_x_report.sh": "tuber_x_report.sh",
    "tuber_x_synd.sh": "tuber_x_synd.sh",
    "tuber_x_health.sh": "tuber_x_health.sh",
}


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
    p(f"[{num}] {'OK  ' if ok else 'FAIL'} {what}")
    p(f"      данные: {data}")


# --------------------------------------------------------------- копия/статусы
def prod_snapshot(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    snap = {"accounts": {}, "posts": None, "classified": {}, "stories": None,
            "scores": None, "report_texts": None, "version": None}
    for r in con.execute("SELECT tier, status, COUNT(*) n FROM accounts"
                         " GROUP BY tier, status ORDER BY tier, status"):
        snap["accounts"][f"{r['tier']}/{r['status']}"] = r["n"]
    snap["posts"] = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    for r in con.execute("SELECT status, COUNT(*) n FROM classified"
                         " GROUP BY status ORDER BY status"):
        snap["classified"][r["status"]] = r["n"]
    snap["stories"] = con.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    snap["scores"] = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    snap["report_texts"] = con.execute("SELECT COUNT(*) FROM report_texts").fetchone()[0]
    snap["version"] = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    return snap


def short(snap):
    acc = ", ".join(f"{k}={v}" for k, v in snap["accounts"].items())
    cls = ", ".join(f"{k}={v}" for k, v in snap["classified"].items())
    return (f"user_version={snap['version']} posts={snap['posts']} "
            f"classified[{cls}] stories={snap['stories']} scores={snap['scores']} "
            f"report_texts={snap['report_texts']} | {acc}")


def backup_via_api(src_path, dst_path):
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def prod_fingerprint(path):
    """Дешёвый отпечаток содержимого рабочей БД (без её открытия на запись)."""
    snap = prod_snapshot(path)
    return (snap["posts"], tuple(sorted(snap["classified"].items())),
            tuple(sorted(snap["accounts"].items())), snap["stories"],
            snap["scores"], snap["report_texts"])


def wait_quiet(path, stable_checks=2, interval=2.0, timeout=180.0):
    """Дождаться окна, когда посторонние процессы не пишут в рабочую БД.

    Приёмка сама рабочую БД не пишет (только читает), но на сервере может идти
    независимая цепочка расписания. Чтобы снимки ДО/ПОСЛЕ были сопоставимы,
    ждём, пока содержимое перестанет меняться. Возвращает (тихо_ли, минут_ожидания).
    """
    t0 = time.time()
    prev = prod_fingerprint(path)
    stable = 0
    while time.time() - t0 < timeout:
        time.sleep(interval)
        cur = prod_fingerprint(path)
        if cur == prev:
            stable += 1
            if stable >= stable_checks:
                return True, round(time.time() - t0, 1)
        else:
            stable = 0
            prev = cur
    return False, round(time.time() - t0, 1)


def crontab_text():
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return (proc.returncode, proc.stdout or "")


# --------------------------------------------------------------------- главное
def main():
    started = datetime.now(timezone.utc)
    prod = os.path.realpath(config.DB_PATH)
    if not os.path.exists(config.DB_PATH):
        p(f"ОШИБКА: рабочая БД не найдена: {config.DB_PATH}")
        return 2

    tmpdir = tempfile.mkdtemp(prefix="tuber_x_acc7_")
    copy_path = os.path.join(tmpdir, "tuber_x_copy.db")
    install_dir = os.path.join(tmpdir, "hermes_scripts")

    hr("ПРИЁМКА ТЗ-7 (задачи 1–4)")
    p(f"начало:            {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"рабочая БД (RO):   {prod}")
    quiet_before, waited = wait_quiet(prod)
    p(f"окно тишины до старта: {'есть' if quiet_before else 'НЕ найдено'} "
      f"(ожидание {waited}с)")
    work_before = prod_snapshot(prod)
    p(f"статусы рабочей БД ДО:  {short(work_before)}")

    rc_cron_before, cron_before = crontab_text()

    backup_via_api(prod, copy_path)
    if os.path.realpath(copy_path) == prod:
        p("ОШИБКА: копия совпала с рабочей БД — приёмка отменена")
        return 3
    p(f"копия (backup API): {copy_path}")
    check(1, "копия создана через sqlite3 backup API и не совпадает с рабочей БД",
          os.path.exists(copy_path) and os.path.realpath(copy_path) != prod,
          f"копия={os.path.basename(copy_path)}")

    env = dict(os.environ)
    env["TUBER_X_DB"] = copy_path
    env.setdefault("PYTHONPATH", ROOT)

    # ------------------------------------------- ЗАДАЧА 4: установка запускалок
    hr("ЗАДАЧА 4. install_hermes_cron.sh в тестовом режиме (боевой каталог не трогаем)")
    inst_env = dict(env)
    inst_env["HERMES_SCRIPTS_DIR"] = install_dir
    inst_env["TUBER_X_PROJECT"] = ROOT
    proc = subprocess.run([os.path.join(ROOT, "scripts", "install_hermes_cron.sh")],
                          cwd=ROOT, env=inst_env, capture_output=True, text=True)
    p("--- вывод установщика ---")
    for line in proc.stdout.splitlines():
        p("   " + line)
    if proc.stderr.strip():
        p("--- stderr установщика ---")
        for line in proc.stderr.splitlines():
            p("   " + line)

    got = sorted(f for f in os.listdir(install_dir)
                 if os.path.islink(os.path.join(install_dir, f)))
    check(2, "установщик создал ровно десять ссылок-запускалок",
          proc.returncode == 0 and got == sorted(LAUNCHERS),
          f"rc={proc.returncode}; ссылок={len(got)}; имена={got}")

    # фактические цели ссылок (readlink и разрешение)
    hr("ЗАДАЧА 4. Список созданных ссылок и их фактическое разрешение")
    all_good = True
    for name in sorted(LAUNCHERS):
        path = os.path.join(install_dir, name)
        raw = os.readlink(path) if os.path.islink(path) else "(не ссылка)"
        real = os.path.realpath(path)
        wrapper = os.path.join(ROOT, "scripts", LAUNCHERS[name])
        ok = (os.path.islink(path) and real == os.path.realpath(wrapper)
              and os.path.exists(real) and os.access(real, os.X_OK))
        all_good = all_good and ok
        p(f"   {'OK ' if ok else 'BAD'} {name:24s} readlink={raw}")
        p(f"       разрешение -> {real}")
    check(3, "каждая ссылка указывает на исполняемую обёртку проекта",
          all_good, f"проверено={len(LAUNCHERS)}")

    # идемпотентность
    before_links = {n: os.readlink(os.path.join(install_dir, n)) for n in LAUNCHERS}
    proc2 = subprocess.run([os.path.join(ROOT, "scripts", "install_hermes_cron.sh")],
                           cwd=ROOT, env=inst_env, capture_output=True, text=True)
    after_links = {n: os.readlink(os.path.join(install_dir, n)) for n in LAUNCHERS}
    check(4, "повторный запуск установщика идемпотентен (ссылки не изменились)",
          proc2.returncode == 0 and before_links == after_links,
          f"rc={proc2.returncode}; ссылки совпали={before_links == after_links}")

    # ------------------------------------------- тир из имени файла (задача 1)
    hr("ЗАДАЧА 1. Запускалки collect определяют тир из имени файла (без сети)")
    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    record = os.path.join(tmpdir, "collect_args.txt")
    fake_py = os.path.join(fake_bin, "python3")
    with open(fake_py, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >> \"$TUBER_X_RECORD\"\n")
    os.chmod(fake_py, 0o755)
    tier_env = dict(env)
    tier_env["PATH"] = fake_bin + os.pathsep + tier_env.get("PATH", "")
    tier_env["TUBER_X_RECORD"] = record
    tier_env["TUBER_X_LOG_DIR"] = os.path.join(tmpdir, "logs")
    tiers = {}
    for name in ("tuber_x_collect_a.sh", "tuber_x_collect_b.sh", "tuber_x_collect_c.sh"):
        if os.path.exists(record):
            os.remove(record)
        r = subprocess.run([os.path.join(install_dir, name)], env=tier_env,
                           capture_output=True, text=True)
        args = open(record, encoding="utf-8").read().split() if os.path.exists(record) else []
        tiers[name] = (r.returncode, args[-1] if args else None)
    ok_tiers = all(tiers[n] == (0, n[-4].upper()) for n in tiers)
    check(5, "запуск без аргументов берёт тир из имени файла (a->A, b->B, c->C)",
          ok_tiers, f"результаты={{ {', '.join(f'{k}:{v}' for k, v in tiers.items())} }}")

    # ------------------------------------------- ЗАДАЧА 4: сторож на копии
    hr("ЗАДАЧА 4. tuber_x_health.sh на копии: пустой stdout при норме, rc=0")
    hp = subprocess.run([os.path.join(install_dir, "tuber_x_health.sh")],
                        env=env, capture_output=True, text=True)
    p(f"   stdout сторожа (длина {len(hp.stdout)}): {hp.stdout!r}")
    if hp.stderr.strip():
        p(f"   stderr (первые строки): {hp.stderr.strip().splitlines()[:3]}")
    check(6, "сторож печатает пустой вывод при отсутствии аномалий и возвращает 0",
          hp.returncode == 0 and hp.stdout == "",
          f"rc={hp.returncode}; строк ALERT в stdout={len(hp.stdout.strip().splitlines()) if hp.stdout.strip() else 0}")

    # ------------------------------------------- crontab не тронут
    hr("ЗАДАЧА 4. Системный crontab не изменён установщиком")
    rc_cron_after, cron_after = crontab_text()
    check(7, "установщик ничего не записал в системный crontab",
          rc_cron_after == rc_cron_before and cron_after == cron_before,
          f"rc до/после={rc_cron_before}/{rc_cron_after}; текст совпал="
          f"{cron_after == cron_before}")

    # ------------------------------------------- тесты
    hr("ТЕСТЫ")
    proc_t = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                            env=env, capture_output=True, text=True)
    tail = (proc_t.stdout or "").strip().splitlines()[-1] if proc_t.stdout else ""
    check(8, "полный набор тестов зелёный", proc_t.returncode == 0,
          f"pytest rc={proc_t.returncode}; {tail}")

    # --------------------------------------------------------- статусы ПОСЛЕ
    hr("СТАТУСЫ РАБОЧЕЙ БД ДО/ПОСЛЕ")
    quiet_after, waited_after = wait_quiet(prod)
    work_after = prod_snapshot(prod)
    p(f"окно тишины после работы: {'есть' if quiet_after else 'НЕ найдено'} "
      f"(ожидание {waited_after}с)")
    p(f"статусы рабочей БД ДО:    {short(work_before)}")
    p(f"статусы рабочей БД ПОСЛЕ: {short(work_after)}")
    same = (work_before["accounts"] == work_after["accounts"]
            and work_before["posts"] == work_after["posts"]
            and work_before["classified"] == work_after["classified"]
            and work_before["stories"] == work_after["stories"]
            and work_before["scores"] == work_after["scores"]
            and work_before["report_texts"] == work_after["report_texts"])
    external = " (обнаружена запись посторонним процессом расписания, не приёмкой)" \
        if not same else ""
    check(9, "рабочая БД не изменена приёмкой (снимки ДО/ПОСЛЕ совпали)", same,
          f"posts {work_before['posts']}->{work_after['posts']}; "
          f"classified {work_before['classified']}->{work_after['classified']}{external}")

    hr("СВОДКА")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        p(f"  {r['num']:>3} {'OK  ' if r['ok'] else 'FAIL'} {r['what']}")

    hr("ОБЯЗАТЕЛЬНЫЕ СТРОКИ ПРИЁМКИ")
    p(f"рабочая БД: {prod}")
    p(f"статусы рабочей БД ДО:    {short(work_before)}")
    p(f"статусы рабочей БД ПОСЛЕ: {short(work_after)}")
    p("рабочая БД не изменена: " + ("ДА" if same else "НЕТ"))

    out_path = os.path.join(ROOT, "docs", "acceptance-log-7.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
