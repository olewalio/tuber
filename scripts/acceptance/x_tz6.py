#!/usr/bin/env python3
"""Приёмка ТЗ-6 (задачи 1–5) на КОПИИ рабочей БД.

ЖЁСТКИЕ ПРАВИЛА:
  * рабочая БД открывается ТОЛЬКО на чтение (`file:...?mode=ro`);
  * копия делается через sqlite3 backup API (в /tmp), все изменяющие проверки
    идут на копии: env `TUBER_X_DB=<копия>` и `config.DB_PATH=<копия>`;
  * ключ модели — только из окружения (подгружается из /root/.hermes/.env);
  * в конце печатаются статусы рабочей БД ДО и ПОСЛЕ и обязательная строка
    «рабочая БД не изменена: ДА/НЕТ».

Запуск: python3 tools/acceptance_tz6.py
Вывод:  docs/acceptance-log-6.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import config  # noqa: E402
from tuber.platforms.x import report as report_mod  # noqa: E402

_out = []
_results = []

DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


# --------------------------------------------------------------- копия / статусы
def prod_snapshot(path):
    """Снимок рабочей БД — только чтение (mode=ro)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    snap = {"accounts": {}, "posts": None, "classified": {}, "stories": None,
            "scores": None, "report_texts": None, "version": None, "mtime": None}
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
    snap["mtime"] = os.path.getmtime(path)
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


def load_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env_file = "/root/.hermes/.env"
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def run_cli(args, env, timeout=1800):
    cmd = [sys.executable, "-m", "tuber x"] + args
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------------------- главное
def main():
    started = datetime.now(timezone.utc)
    prod = os.path.realpath(config.DB_PATH)
    if not os.path.exists(config.DB_PATH):
        p(f"ОШИБКА: рабочая БД не найдена: {config.DB_PATH}")
        return 2

    tmpdir = tempfile.mkdtemp(prefix="tuber_x_acc6_")
    copy_path = os.path.join(tmpdir, "tuber_x_copy.db")
    # ТЗ-8 задача 2: приёмка пишет отчёт ТОЛЬКО во временный каталог и не
    # трогает боевой `reports/`.
    report_dir = os.path.join(tmpdir, "reports")

    hr("ПРИЁМКА ТЗ-6 (задачи 1–5)")
    p(f"начало:            {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"рабочая БД (RO):   {prod}")
    work_before = prod_snapshot(prod)
    p(f"статусы рабочей БД ДО:  {short(work_before)}")

    backup_via_api(prod, copy_path)
    if os.path.realpath(copy_path) == prod:
        p("ОШИБКА: копия совпала с рабочей БД — приёмка отменена")
        return 3
    p(f"копия (backup API): {copy_path}")

    env = dict(os.environ)
    env["TUBER_X_DB"] = copy_path
    env["TUBER_X_REPORT_DIR"] = report_dir
    env.setdefault("PYTHONPATH", ROOT)
    key = load_key()
    if key:
        env["DEEPSEEK_API_KEY"] = key
    config.DB_PATH = copy_path
    check(1, "копия создана через sqlite3 backup API и не совпадает с рабочей БД",
          os.path.exists(copy_path) and os.path.realpath(copy_path) != prod,
          f"копия={os.path.basename(copy_path)}; ключ DeepSeek "
          f"{'есть' if key else 'НЕТ'}")
    if not key:
        p("ОШИБКА: нет ключа модели — прогон classify невозможен")
        return 4

    # ------------------------------------------------ ЗАДАЧА 1/4: классификация
    hr("ЗАДАЧА 4. classify --limit 40 на копии (предфильтр по умолчанию ВЫКЛ)")
    before = prod_snapshot(copy_path)
    rc, out = run_cli(["classify", "--limit", "40"], env)
    p("--- вывод classify (первые строки) ---")
    for line in out.strip().splitlines()[:12]:
        p("   " + line)
    after = prod_snapshot(copy_path)
    new_classified = (after["classified"].get("classified", 0)
                      - before["classified"].get("classified", 0))
    new_heuristic = (after["classified"].get("heuristic", 0)
                     - before["classified"].get("heuristic", 0))
    check(2, "classify отработал на копии без ошибки", rc == 0,
          f"rc={rc}; если rc!=0, хвост вывода: {out.strip()[-300:] if rc else '-'}")
    check(3, "моделью классифицировано постов", new_classified > 0,
          f"новых classified={new_classified} (было {before['classified']})")
    check(4, "в heuristic ушло 0 постов (предфильтр выключен)", new_heuristic == 0,
          f"новых heuristic={new_heuristic}")

    con = sqlite3.connect(copy_path)
    con.row_factory = sqlite3.Row
    topics = [dict(r) for r in con.execute(
        "SELECT COALESCE(topic,'(нет)') topic, COUNT(*) n FROM classified"
        " WHERE status='classified' GROUP BY topic ORDER BY n DESC")]
    p("распределение тем по всей таблице classified (после прогона):")
    for t in topics:
        p(f"   {t['topic']}: {t['n']}")
    check(5, "распределение тем получено (всего классифицировано моделью)",
          after["classified"].get("classified", 0) > 0,
          f"классифицировано={after['classified'].get('classified', 0)}; "
          f"тем={len(topics)}")
    con.close()

    # ------------------------------------------------ ЗАДАЧА 4: scores и report
    hr("ЗАДАЧА 4. scores (включая stories) на копии")
    rc_st, out_st = run_cli(["stories", "--limit", "50"], env)
    rc_sc, out_sc = run_cli(["scores", "--limit", "50"], env)
    for line in (out_st.strip().splitlines()[:3] + out_sc.strip().splitlines()[:4]):
        p("   " + line)
    con = sqlite3.connect(copy_path)
    n_stories = con.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    n_story_posts = con.execute("SELECT COUNT(*) FROM story_posts").fetchone()[0]
    n_scores = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    con.close()
    check(6, "scores/stories отработали на копии", rc_st == 0 and rc_sc == 0,
          f"rc stories={rc_st}, rc scores={rc_sc}; сюжетов={n_stories}, "
          f"story_posts={n_story_posts}, оценок={n_scores}")

    hr("ЗАДАЧА 4. report на копии (файл + 7 блоков)")
    # ТЗ-8 задача 3: кандидатов на перевод считаем ДО прогона, результат
    # сравниваем с приростом строк report_texts. Пустая таблица при нуле
    # кандидатов — это НЕ «кэш наполнен».
    con = sqlite3.connect(copy_path)
    con.row_factory = sqlite3.Row
    n_rt_before = con.execute("SELECT COUNT(*) FROM report_texts").fetchone()[0]
    cand = report_mod.translation_candidates(con, date=DATE)
    con.close()
    rc_rep, out_rep = run_cli(["report", "--date", DATE], env)
    report_path = os.path.join(report_dir, DATE + ".md")
    has_file = os.path.exists(report_path)
    text = open(report_path, encoding="utf-8").read() if has_file else ""
    headers = ["1. Главное за сутки", "2. По темам", "3. Деньги и запуски",
               "4. Новинки и инструменты", "5. Русскоязычный срез",
               "6. Тёмные лошадки и первые авторы", "7. Служебный блок"]
    missing = [h for h in headers if h not in text]
    check(7, "файл отчёта создан во временном каталоге, рабочее reports/ не тронуто",
          has_file and os.path.realpath(report_path).startswith(tmpdir),
          f"{report_path}; размер={len(text)}б; rc={rc_rep}")
    check(8, "в отчёте присутствуют все 7 блоков", not missing,
          f"нет блоков: {missing or 'нет'}")
    dup_bad = [pat for pat in ("Раунд: раунд", "Запуск: запуск") if pat in text]
    check(9, "нет дублей формулировок «Раунд: раунд» / «Запуск: запуск»", not dup_bad,
          f"найдено: {dup_bad or 'нет'}")

    con = sqlite3.connect(copy_path)
    con.row_factory = sqlite3.Row
    n_rt = con.execute("SELECT COUNT(*) FROM report_texts").fetchone()[0]
    daily = con.execute("SELECT * FROM classify_daily ORDER BY day DESC LIMIT 1").fetchone()
    daily_d = dict(daily) if daily else {}
    con.close()
    tr_status, tr_msg = report_mod.translation_verdict(cand, n_rt - n_rt_before)
    check(10, "кэш переводов: итог честный (кандидаты против прироста строк)",
          tr_status in ("ok", "nothing"),
          f"кандидатов={cand}; строк report_texts {n_rt_before}->{n_rt}"
          f" (+{n_rt - n_rt_before}); вердикт={tr_status}: {tr_msg}; "
          f"classify_daily={daily_d.get('day')} постов={daily_d.get('posts')} "
          f"вызовов={daily_d.get('model_calls')} стоимость=${daily_d.get('cost_usd')}")

    # ------------------------------------------------ ЗАДАЧА 3: расписание
    hr("ЗАДАЧА 3. Расписание и обёртки")
    sched = os.path.join(ROOT, "docs", "schedule.md")
    stext = open(sched, encoding="utf-8").read() if os.path.exists(sched) else ""
    chain = "collect → enrich → fulltext → classify → scores → report → health"
    check(11, "docs/schedule.md: полная цепочка, МСК, Hermes, потолок, пустой отчёт",
          all(s in stext for s in (chain, "МСК", "Hermes", "CLASSIFY_DAILY_CAP",
                                   "нет данных за сутки")),
          f"размер={len(stext)}б")
    for idx, name in enumerate(("tuber_x_classify.sh", "tuber_x_report.sh"), start=12):
        path = os.path.join(ROOT, "scripts", name)
        ok = os.path.exists(path) and os.access(path, os.X_OK)
        txt = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        redirects = '>>"$LOG"' in txt
        check(idx,
              f"обёртка {name}: есть, исполняемая, грузит ключ из .env без печати",
              ok and "DEEPSEEK_API_KEY" in txt and "/root/.hermes/.env" in txt
              and 'echo "$DEEPSEEK_API_KEY"' not in txt,
              f"exists={os.path.exists(path)} exec={os.access(path, os.X_OK)} "
              f"stdout_redirect={redirects}")

    # ------------------------------------------------ ЗАДАЧА 5: аудит отката
    hr("ЗАДАЧА 5. Аудит путей отката/записи в рабочую БД")
    from tools import audit_writeback
    audit = audit_writeback.scan()
    check(14, "в репозитории нет путей записи в рабочую БД / отката из снимка",
          audit["hazards"] == [],
          f"опасных={len(audit['hazards'])}; читающих копий={len(audit['ok_usages'])}; "
          f"файлов просканировано={audit['files_scanned']}")

    # ------------------------------------------------ тесты
    hr("ТЕСТЫ")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                          capture_output=True, text=True)
    tail = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    check(15, "полный набор тестов зелёный", proc.returncode == 0,
          f"pytest rc={proc.returncode}; {tail}")

    # --------------------------------------------------------- статусы ПОСЛЕ
    hr("СТАТУСЫ РАБОЧЕЙ БД ДО/ПОСЛЕ")
    work_after = prod_snapshot(prod)
    p(f"статусы рабочей БД ДО:    {short(work_before)}")
    p(f"статусы рабочей БД ПОСЛЕ: {short(work_after)}")
    same = (work_before["accounts"] == work_after["accounts"]
            and work_before["posts"] == work_after["posts"]
            and work_before["classified"] == work_after["classified"]
            and work_before["stories"] == work_after["stories"]
            and work_before["scores"] == work_after["scores"]
            and work_before["report_texts"] == work_after["report_texts"])
    check(16, "рабочая БД не изменена приёмкой (снимки ДО/ПОСЛЕ совпали)", same,
          f"posts {work_before['posts']}->{work_after['posts']}; "
          f"classified {work_before['classified']}->{work_after['classified']}")

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

    out_path = os.path.join(ROOT, "docs", "acceptance-log-6.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
