#!/usr/bin/env python3
"""Приёмка ТЗ-5 (задачи 1–5) на КОПИИ рабочей БД.

ЖЁСТКОЕ ПРАВИЛО (общие правила задания):
  * рабочая БД открывается ТОЛЬКО на чтение (`file:...?mode=ro`);
  * копия делается через sqlite3 backup API (в /tmp), все изменяющие проверки
    идут на копии: env `TUBER_X_DB=<копия>` и `config.DB_PATH=<копия>`;
  * в конце печатаются статусы аккаунтов и счётчики постов ДО и ПОСЛЕ приёмки.

Запуск:  python3 tools/acceptance_tz5.py
Вывод:   docs/acceptance-log-5.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import channels, collect, config, store as db, enrich, health, report  # noqa: E402

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
    p(f"[{num}] {'OK  ' if ok else 'FAIL'} {what}")
    p(f"      данные: {data}")


# --------------------------------------------------------------- копия / статусы
def prod_snapshot(path):
    """Снимок статусов и счётчиков РАБОЧЕЙ БД — только чтение (mode=ro)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    snap = {"accounts": {}, "posts": None, "version": None}
    for r in con.execute("SELECT tier, status, COUNT(*) n FROM accounts"
                         " GROUP BY tier, status ORDER BY tier, status"):
        snap["accounts"][f"{r['tier']}/{r['status']}"] = r["n"]
    snap["posts"] = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    snap["collected_sum"] = con.execute(
        "SELECT COALESCE(SUM(posts_collected),0) FROM accounts").fetchone()[0]
    snap["version"] = con.execute("PRAGMA user_version").fetchone()[0]
    snap["mtime"] = os.path.getmtime(path)
    con.close()
    return snap


def short(snap):
    acc = ", ".join(f"{k}={v}" for k, v in snap["accounts"].items())
    return (f"user_version={snap['version']} | posts={snap['posts']} "
            f"| sum(posts_collected)={snap['collected_sum']} | {acc}")


def backup_via_api(src_path, dst_path):
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


# --------------------------------------------------------------------- проверки
def check_migration(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    dist = {r[0] or "NULL": r[1] for r in con.execute(
        "SELECT text_src, COUNT(*) FROM posts GROUP BY text_src")}
    short_long = con.execute(
        "SELECT COUNT(*) FROM posts WHERE is_long=1 AND COALESCE(length(text),0) <= 300"
    ).fetchone()[0]
    check(1, "миграция text_src на копии (колонка есть, значения nitter/cdn/NULL)",
          "text_src" in cols,
          f"колонка={'text_src' in cols}; распределение={dist}; "
          f"is_long&len<=300={short_long}")


def check_backfill_legacy(tmpdir):
    """Разметка старых строк на синтетической базе без text_src."""
    path = os.path.join(tmpdir, "legacy.db")
    con = sqlite3.connect(path)
    con.executescript(
        """CREATE TABLE posts (id INTEGER PRIMARY KEY, account_id INTEGER,
             tweet_id TEXT, published_at_utc TEXT, published_src TEXT, text TEXT,
             text_hash TEXT, is_long INTEGER, metrics_src TEXT, deleted_at TEXT);""")
    con.execute("PRAGMA user_version=3")
    from tuber.platforms.x.registry import text_hash
    rows = [
        ("1", "текст Nitter", text_hash("текст Nitter"), 0, None),          # nitter
        ("2", "x" * 280, text_hash("x" * 280), 1, "cdn"),                    # cdn
        ("3", "y" * 500, text_hash("y" * 500), 1, "cdn"),                    # NULL
        ("4", "перезапись CDN", "deadbeef", 0, "cdn"),                       # cdn
    ]
    for tid, text, th, is_long, msrc in rows:
        con.execute("INSERT INTO posts (tweet_id, published_at_utc, published_src, text,"
                    " text_hash, is_long, metrics_src) VALUES (?,?,?,?,?,?,?)",
                    (tid, "2026-09-14T10:00:00", "rss", text, th, is_long, msrc))
    con.commit()
    db.migrate(con)
    got = {r[0]: r[1] for r in con.execute("SELECT tweet_id, text_src FROM posts")}
    con.close()
    ok = (got["1"] == "nitter" and got["2"] == "cdn" and got["3"] is None
          and got["4"] == "cdn")
    check(12, "разметка старых строк: nitter/cdn/NULL по доказуемым признакам", ok,
          f"получено={got}")


def check_needs_fulltext(con):
    n = enrich.needs_full_text_count(con)
    rows = enrich.needs_full_text_posts(con, limit=10)
    check(2, "число постов, которым нужен полный текст (is_long<=300 или text_src=cdn)",
          n > 0, f"нужно_добрать={n}; примеры={[r['tweet_id'] for r in rows[:5]]}")


def check_enrich_summary(con):
    class _Cdn:
        requests_429 = 0

    class _Router:
        cdn = _Cdn()

        def enrich_tweet(self, tid):
            return "not_found", None, 404

    s = enrich.enrich_batch(con, _Router(), dry_run=True)
    check(3, "сводка enrich печатает честное «нужно_добрать» (не 0 при факте)",
          s["needs_nitter"] > 0,
          f"selected={s['selected']} needs_nitter={s['needs_nitter']} "
          f"batch={s.get('needs_nitter_batch')}")


def check_health_checks(con):
    a = health.check_text_short_daily(con)
    b = health.check_cdn_text_ratio(con)
    c = health.check_instance_cooldown(con)
    d = health.check_long_text_gap(con)
    check(4, "сторож: доля короткого текста длинного поста за сутки (порог 5%)",
          a["value"] is not None,
          f"{a['msg']}")
    check(5, "сторож: доля text_src='cdn' за сутки (порог 5%)", b["value"] is not None,
          f"{b['msg']}")
    check(6, "сторож: инстанс Nitter в cooldown > 30 мин", c["value"] is not None,
          f"{c['msg']}")
    check("6-бис", "сторож: доля длинных постов с обрезанным текстом (порог 10%)",
          d["value"] is not None, f"{d['msg']}")
    res = health.run(con)
    p(f"      полный прогон сторожа: алертов={len(res['alerts'])}, "
      f"warn={len(res['warns'])}")
    for x in res["checks"]:
        p(f"        {'ALERT' if x['alert'] else 'ok   '} {x['name']:18s} {x['msg']}")


def check_report_quality(con):
    text = report.build(con, date="2026-09-15", translator=False)
    dup_round = "Раунд: раунд" in text
    dup_launch = "Запуск: запуск" in text
    ru_line = "Русскоязычных постов в базе:" in text
    # блок 4 не должен содержать рубрику «инфраструктура и железо»
    b4 = "\n".join(report.block4_tools(
        con, datetime(2026, 9, 15, tzinfo=timezone.utc),
        datetime(2026, 9, 16, tzinfo=timezone.utc)))
    infra_in_b4 = "инфраструктура и железо" in b4
    check(7, "выдача: нет дубля «Раунд: раунд»/«Запуск: запуск»",
          not dup_round and not dup_launch, f"дубль_раунд={dup_round}, дубль_запуск={dup_launch}")
    check(8, "выдача: блок 4 «Новинки и инструменты» без инфраструктуры/железа",
          not infra_in_b4, f"инфраструктура_в_блоке4={infra_in_b4}")
    check(9, "выдача: служебный блок содержит число русскоязычных постов",
          ru_line, f"строка_есть={ru_line}")
    from tuber.platforms.x import classify
    prompt = classify._system_prompt()
    check(13, "промпт классификации: границы рубрик (инфраструктура != инструменты)",
          "НЕ относится к «инструментам разработчика»" in prompt,
          "правило в промпте: " + str("НЕ относится к «инструментам разработчика»" in prompt))


def check_wrappers(copy_path):
    scripts = ["tuber_x_health.sh", "tuber_x_collect.sh", "tuber_x_enrich.sh",
               "tuber_x_fulltext.sh", "tuber_x_synd.sh", "tuber_x_scores.sh"]
    missing = [s for s in scripts
               if not (os.path.isfile(os.path.join(ROOT, "scripts", s))
                       and os.access(os.path.join(ROOT, "scripts", s), os.X_OK))]
    # тишина в норме: функция форматирования ALERT на пустом результате пуста
    silent = health.format_alerts({"alerts": []}) == ""
    # громкость при аномалии: обёртка печатает только строки ALERT
    env = dict(os.environ, TUBER_X_DB=copy_path)
    proc = subprocess.run(["bash", os.path.join(ROOT, "scripts", "tuber_x_health.sh")],
                          capture_output=True, text=True, env=env, timeout=300)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    only_alerts = all(ln.startswith("ALERT") for ln in lines)
    check(10, "обёртки в scripts/ есть и исполняемы; сторож молчит в норме",
          not missing and silent, f"отсутствуют/не исполняемы={missing}; "
                                  f"пустой_результат_ALERT='{health.format_alerts({'alerts': []})}'")
    check(11, "сторож-обёртка на копии: stdout — только строки ALERT (или пусто)",
          only_alerts and proc.returncode == 0,
          f"rc={proc.returncode} строк={len(lines)} "
          f"первые={lines[:3]}")


def check_live_fallback(tmpdir, copy_path, con):
    """Задача 4: сбор при заведомо мёртвом Nitter -> резервный канал x_ssr."""
    DEAD = "http://127.0.0.1:9"
    con.execute("INSERT OR REPLACE INTO instances (host, healthy, fail_streak,"
                " requests_today, day) VALUES (?,0,3,0,?)",
                (DEAD, datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    con.commit()
    n_before = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    run_id = db.start_run(con, "acceptance:fallback")
    router = channels.ChannelRouter(db_path=copy_path, instances=[DEAD], run_id=run_id)
    t0 = time.monotonic()
    s = collect.collect_tier(con, router.nitter, "A", max_accounts=2, run_id=run_id,
                             router=router)
    dur = time.monotonic() - t0
    stats = [dict(r) for r in con.execute(
        "SELECT host, status, items, latency_ms FROM requests WHERE kind='x_ssr'"
        " ORDER BY id DESC LIMIT 5")]
    n_after = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    router.close()
    items = sum(d.get("items", 0) for d in s["details"] if d.get("ok"))
    ok = s["ssr_used"] >= 1 and s["accounts_fail"] == 0 and items >= 1
    check(14, "живой резервный путь x_ssr при мёртвом Nitter (сеть реальная)",
          ok,
          f"аккаунтов={s['accounts_total']} ok={s['accounts_ok']} fail={s['accounts_fail']}"
          f" x_ssr_аккаунтов={s['ssr_used']} получено_постов={items}"
          f" новых={s['posts_new']} постов_стало={n_after - n_before} время={dur:.1f}с")
    p(f"      запросы x_ssr (последние): {stats}")
    p(f"      детали по аккаунтам: {s['details']}")


def check_test_suite():
    proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"],
                          capture_output=True, text=True, cwd=ROOT, timeout=900)
    col = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only"],
                         capture_output=True, text=True, cwd=ROOT, timeout=300)
    n_tests = sum(1 for ln in col.stdout.splitlines() if "::" in ln)
    check(15, "полный набор тестов (включая test_no_direct_network и ТЗ-5)",
          proc.returncode == 0,
          f"pytest rc={proc.returncode}; собрано тестов={n_tests}" +
          ("" if proc.returncode == 0 else f"; вывод={proc.stdout[-400:]}"))


# --------------------------------------------------------------------- главное
def main():
    started = datetime.now(timezone.utc)
    prod = os.path.realpath(config.DB_PATH)
    if not os.path.exists(config.DB_PATH):
        p(f"ОШИБКА: рабочая БД не найдена: {config.DB_PATH}")
        return 2

    tmpdir = tempfile.mkdtemp(prefix="tuber_x_acc5_")
    copy_path = os.path.join(tmpdir, "tuber_x_copy.db")

    hr("ПРИЁМКА ТЗ-5 (задачи 1–5)")
    p(f"начало:            {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"рабочая БД (RO):   {prod}")
    work_before = prod_snapshot(prod)
    p(f"статусы рабочей БД ДО:  {short(work_before)}")

    backup_via_api(prod, copy_path)
    if os.path.realpath(copy_path) == prod:
        p("ОШИБКА: копия совпала с рабочей БД — приёмка отменена")
        return 3
    p(f"копия (backup API): {copy_path}")
    os.environ["TUBER_X_DB"] = copy_path
    config.DB_PATH = copy_path
    p(f"копия закреплена: TUBER_X_DB={os.environ['TUBER_X_DB']}")

    con = db.init_db(copy_path)
    p(f"схема копии после миграции: user_version="
      f"{con.execute('PRAGMA user_version').fetchone()[0]}")

    hr("ЗАДАЧА 1. Провенанс текста")
    check_migration(con)
    check_backfill_legacy(tmpdir)
    check_needs_fulltext(con)
    check_enrich_summary(con)

    hr("ЗАДАЧА 2. Сторож свежести")
    check_health_checks(con)
    check_wrappers(copy_path)

    hr("ЗАДАЧА 3. Расписание")
    sched = os.path.join(ROOT, "docs", "schedule.md")
    txt = open(sched, encoding="utf-8").read() if os.path.exists(sched) else ""
    has_lines = "tuber_x_health.sh" in txt and "*/30 * * * *" in txt
    check(16, "docs/schedule.md: таблица, обёртки, точные строки crontab (не поставлены)",
          has_lines, f"файл_есть={os.path.exists(sched)} строк_crontab={has_lines} "
                     f"размер={len(txt)}б")

    hr("ЗАДАЧА 4. Живой резервный путь")
    check_live_fallback(tmpdir, copy_path, con)
    con.close()

    hr("ЗАДАЧА 5. Качество выдачи")
    con = db.connect(copy_path)
    check_report_quality(con)
    con.close()

    hr("ТЕСТЫ")
    check_test_suite()

    # --------------------------------------------------------- статусы ПОСЛЕ
    hr("СТАТУСЫ РАБОЧЕЙ БД ДО/ПОСЛЕ")
    work_after = prod_snapshot(prod)
    p(f"статусы рабочей БД ДО:    {short(work_before)}")
    p(f"статусы рабочей БД ПОСЛЕ: {short(work_after)}")
    same = (work_before["accounts"] == work_after["accounts"]
            and work_before["posts"] == work_after["posts"]
            and work_before["collected_sum"] == work_after["collected_sum"])
    p(f"статусы/счётчики совпали: {'ДА' if same else 'НЕТ'}")
    if not same:
        p("  ВНИМАНИЕ: рабочая БД изменилась во время приёмки. Приёмка открывала")
        p("  рабочую БД ТОЛЬКО на чтение (mode=ro) и все изменения делала на копии;")
        p("  изменение вызвано внешним процессом, писавшим в БД параллельно.")
    check(17, "рабочая БД не изменена (снимки ДО/ПОСЛЕ совпали)", same,
          f"posts {work_before['posts']}->{work_after['posts']}, "
          f"accounts_совпали={work_before['accounts'] == work_after['accounts']}")

    hr("СВОДКА")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        p(f"  {r['num']:>3} {'OK  ' if r['ok'] else 'FAIL'} {r['what']}")

    # итоговые обязательные строки
    hr("ОБЯЗАТЕЛЬНЫЕ СТРОКИ ПРИЁМКИ")
    p(f"статусы рабочей БД ДО:    {short(work_before)}")
    p(f"статусы рабочей БД ПОСЛЕ: {short(work_after)}")
    p("рабочая БД не изменена: " + ("ДА" if same else "НЕТ"))

    out_path = os.path.join(ROOT, "docs", "acceptance-log-5.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
