#!/usr/bin/env python3
"""Приёмка ТЗ-3 (П1–П11) на живых данных.

ЖЁСТКИЕ ПРАВИЛА (условия задания):
  * рабочая БД НЕ меняется: копия делается через sqlite3 backup API, рабочая БД
    открывается ТОЛЬКО на чтение (`file:...?mode=ro`);
  * вся приёмка (сбор, обогащение, классификация, сюжеты, оценки, отчёт,
    сторож) идёт на КОПИИ через env `TUBER_X_DB`;
  * в конце печатаются статусы и счётчик постов рабочей БД ДО и ПОСЛЕ — они
    обязаны совпасть.

Запуск:  python3 tools/acceptance_tz3.py
Вывод:   docs/acceptance-log-3.txt (полный stdout) + тот же текст в консоль.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import channels, classify, collect, config, store as db, enrich, health  # noqa: E402
from tuber.platforms.x import report, scores, stories  # noqa: E402
from tuber.platforms.x.broker import raw_http_get  # noqa: E402

_out = []


def p(line=""):
    print(line)
    _out.append(str(line))


def hr(title):
    p("")
    p("=" * 78)
    p(title)
    p("=" * 78)


def backup_db_via_api(src_path, dst_path):
    """Копия БД строго через sqlite3 backup API (условие 1)."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dst_path


def snapshot_prod(path):
    """Статусы и счётчик постов. Прод-база открывается ТОЛЬКО на чтение."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {"accounts": {}, "posts": None, "posts_collected": None}
    try:
        for r in con.execute("SELECT tier, status, COUNT(*) n FROM accounts"
                             " GROUP BY tier, status ORDER BY tier, status"):
            out["accounts"][f"{r['tier']}/{r['status']}"] = r["n"]
        out["posts"] = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        out["posts_collected"] = con.execute(
            "SELECT COALESCE(SUM(posts_collected),0) FROM accounts").fetchone()[0]
    finally:
        con.close()
    return out


def short(snap):
    return (", ".join(f"{k}={v}" for k, v in snap["accounts"].items())
            + f" | posts={snap['posts']} sum(posts_collected)={snap['posts_collected']}")


_PROD = None
_PROD_FP = None


def fingerprint(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            "posts": con.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "accounts": con.execute(
                "SELECT tier||'/'||status||'='||COUNT(*) FROM accounts"
                " GROUP BY tier,status ORDER BY tier,status").fetchall(),
            "sum_pc": con.execute("SELECT COALESCE(SUM(posts_collected),0) FROM accounts"
                                  ).fetchone()[0],
        }
    finally:
        con.close()


def guard(label):
    """Проверка, что рабочая БД не изменилась после фазы приёмки (условие 1)."""
    fp = fingerprint(_PROD)
    if _PROD_FP is not None and fp != _PROD_FP:
        p(f"!!! НАРУШЕНИЕ: рабочая БД изменилась после фазы «{label}»")
        p(f"    было:  {_PROD_FP}")
        p(f"    стало: {fp}")
        return False
    p(f"[guard] после «{label}» рабочая БД не изменилась")
    return True


def run_cli(args, env, timeout=1800):
    cmd = [sys.executable, "-m", "tuber x"] + args
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    started = datetime.now(timezone.utc)
    prod = os.path.realpath(config.DB_PATH)
    if not os.path.exists(config.DB_PATH):
        p(f"ОШИБКА: рабочая БД не найдена: {config.DB_PATH}")
        return 2

    tmpdir = tempfile.mkdtemp(prefix="tuber_x_acc3_")
    copy_path = os.path.join(tmpdir, "tuber_x_copy.db")
    backup_db_via_api(config.DB_PATH, copy_path)
    if os.path.realpath(copy_path) == prod:
        p("ОШИБКА: копия совпала с рабочей БД — приёмка отменена")
        return 3

    p(f"Рабочая БД (ТОЛЬКО ЧТЕНИЕ): {config.DB_PATH}")
    p(f"Копия приёмки (sqlite3 backup API): {copy_path}")
    before = snapshot_prod(config.DB_PATH)
    p(f"статусы рабочей БД ДО:  {short(before)}")

    # Жёсткая изоляция: и родитель, и все подпроцессы работают с копией.
    global _PROD, _PROD_FP
    _PROD = os.path.realpath(config.DB_PATH)
    _PROD_FP = fingerprint(_PROD)
    env = dict(os.environ)
    env["TUBER_X_DB"] = copy_path
    os.environ["TUBER_X_DB"] = copy_path
    config.DB_PATH = copy_path
    p(f"копия закреплена: TUBER_X_DB={os.environ['TUBER_X_DB']} (и в config.DB_PATH)")
    # Ключ модели — только из окружения; при отсутствии приёмка честно сообщит.
    p(f"ключ DeepSeek в окружении: {'да' if env.get(config.DEEPSEEK_API_KEY_ENV) else 'НЕТ'}")

    con = db.init_db(copy_path)
    results = {}

    # --------------------------------------------------- НАПОЛНЕНИЕ КОПИИ
    hr("НАПОЛНЕНИЕ КОПИИ: живой сбор Nitter + обогащение CDN (рабочая БД не трогается)")
    for tier in ("A", "B", "C"):
        rc, out = run_cli(["collect", "--tier", tier], env)
        p("\n".join(out.strip().splitlines()[:6]))
    n_posts = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    p(f"постов в копии после сбора: {n_posts}")
    t0 = datetime.now(timezone.utc)
    batches = 0
    while batches < 40:
        rc, out = run_cli(["enrich", "--batch", "400"], env)
        batches += 1
        if "выбрано=0" in out:
            break
    enriched_all = con.execute("SELECT COUNT(*) FROM posts WHERE deleted_at IS NULL"
                               " AND metrics_at IS NOT NULL").fetchone()[0]
    total_all = con.execute("SELECT COUNT(*) FROM posts WHERE deleted_at IS NULL"
                            ).fetchone()[0]
    p(f"обогащение: батчей {batches}, метрик у {enriched_all}/{total_all}; "
      f"время {(datetime.now(timezone.utc) - t0).total_seconds():.0f} с")
    guard("наполнение копии")

    # --------------------------------------------------------------- П1 классификация
    hr("П1. Классификация: cli classify --limit 40 (на копии)")
    rc, out = run_cli(["classify", "--limit", "40"], env)
    p(out.rstrip())
    cov = classify.coverage(con)
    results["P1"] = {"rc": rc, "ratio": cov["ratio"], "classified": cov["classified"],
                     "with_topic": cov["with_topic"],
                     "ok": bool(cov["ratio"] is not None and cov["ratio"] >= 0.80)}
    p(f"П1: постов с topic {cov['with_topic']}/{cov['classified']} = "
      f"{(cov['ratio'] or 0):.1%} (порог >= 80%) -> "
      f"{'ПРОЙДЕНО' if results['P1']['ok'] else 'НЕ ПРОЙДЕНО'}")
    cost = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM classify_daily").fetchone()[0]
    p(f"П1 стоимость в classify_daily: ${cost:.6f} (лог стоимости ведётся)")

    # добор классификации до дневного потолка — для содержательных сюжетов
    hr("П1-добор: classify без лимита (дневной потолок 400)")
    rc, out = run_cli(["classify"], env)
    p(out.rstrip())
    cov = classify.coverage(con)
    p(f"после добора: с topic {cov['with_topic']}/{cov['classified']} "
      f"({(cov['ratio'] or 0):.1%}), heuristic={cov['heuristic']}, failed={cov['failed']}")
    guard("П1 classify")

    # -------------------------------------------------------------- П2 сюжеты
    hr("П2. Сюжеты: cli stories --window 72 (на копии)")
    rc, out = run_cli(["stories", "--window", "72"], env)
    p(out.rstrip())
    n_stories = con.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    n_multi = con.execute("SELECT COUNT(*) FROM stories WHERE xconf >= 2").fetchone()[0]
    results["P2"] = {"stories": n_stories, "xconf_ge2": n_multi,
                     "ok": n_stories >= 20 and n_multi >= 5}
    p(f"П2: сюжетов {n_stories} (порог >= 20), с xconf>=2: {n_multi} (порог >= 5) -> "
      f"{'ПРОЙДЕНО' if results['P2']['ok'] else 'НЕ ПРОЙДЕНО'}")
    guard("П2 stories")

    # ---------------------------------------------------------------- П3 оси
    hr("П3. Оси: cli scores (на копии)")
    rc, out = run_cli(["scores"], env)
    p(out.rstrip())
    fm_n = con.execute("SELECT COUNT(*) FROM accounts WHERE COALESCE(first_mover_score,0) > 0"
                       ).fetchone()[0]
    dk_n = con.execute("SELECT COUNT(*) FROM darks").fetchone()[0]
    results["P3"] = {"first_movers": fm_n, "darks": dk_n,
                     "ok": fm_n >= 1 and dk_n >= 3}
    p(f"П3: аккаунтов с непустым first_mover_score: {fm_n}; тёмных лошадок: {dk_n} "
      f"(порог >= 3) -> {'ПРОЙДЕНО' if results['P3']['ok'] else 'НЕ ПРОЙДЕНО'}")
    guard("П3 scores")

    # -------------------------------------------------------------- П9 enrich
    hr("П9. Обогащение метриками: cli enrich --limit 100 (на копии)")
    rc, out = run_cli(["enrich", "--limit", "100"], env)
    p(out.rstrip())
    total = con.execute("SELECT COUNT(*) FROM posts WHERE deleted_at IS NULL").fetchone()[0]
    enriched = con.execute("SELECT COUNT(*) FROM posts WHERE deleted_at IS NULL"
                           " AND metrics_at IS NOT NULL AND likes IS NOT NULL"
                           " AND replies IS NOT NULL").fetchone()[0]
    nreq = con.execute("SELECT COUNT(*) FROM requests WHERE kind='cdn_tweet'").fetchone()[0]
    nbad = con.execute("SELECT COUNT(*) FROM requests WHERE kind='cdn_tweet'"
                       " AND status NOT IN (200,400,404)").fetchone()[0]
    ratio = (enriched / total) if total else 0.0
    fail_ratio = (nbad / nreq) if nreq else 0.0
    results["P9"] = {"total": total, "enriched": enriched, "ratio": ratio,
                     "cdn_requests": nreq, "cdn_fail_ratio": fail_ratio,
                     "ok": ratio >= 0.95 and fail_ratio <= 0.05}
    p(f"П9: непустые метрики у {enriched}/{total} = {ratio:.1%} (порог >= 95%); "
      f"отказов канала {nbad}/{nreq} = {fail_ratio:.1%} (порог <= 5%) -> "
      f"{'ПРОЙДЕНО' if results['P9']['ok'] else 'НЕ ПРОЙДЕНО'}")
    guard("П9 enrich")

    # пересборка сюжетов/оценок после обогащения и классификации
    run_cli(["stories", "--window", "72"], env)
    run_cli(["scores"], env)

    # -------------------------------------------------------------- П10/P11 SQL
    hr("П10. Честность оценки: SQL по scores")
    bad = con.execute(
        "SELECT COUNT(*) FROM scores WHERE COALESCE(metrics_missing,0)=0 AND"
        " (metrics_at IS NULL OR metrics_at < ?)",
        (db.iso(datetime.now(timezone.utc) - timedelta(hours=24)),)).fetchone()[0]
    total_scores = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    missing = con.execute("SELECT COUNT(*) FROM scores WHERE metrics_missing=1"
                          ).fetchone()[0]
    results["P10"] = {"scores": total_scores, "bad": bad, "metrics_missing": missing,
                      "ok": bad == 0}
    p(f"П10: оценок {total_scores}, из них metrics_missing=1: {missing}; "
      f"постов без свежих метрик и без пометки: {bad} (порог 0) -> "
      f"{'ПРОЙДЕНО' if results['P10']['ok'] else 'НЕ ПРОЙДЕНО'}")

    hr("П11. Ретвит не портит рейтинг")
    rt_handles = [r["handle"] for r in con.execute(
        "SELECT DISTINCT COALESCE(owner_handle, '') handle FROM posts"
        " WHERE COALESCE(is_retweet,0)=1 LIMIT 5")]
    rt_total = con.execute("SELECT COUNT(*) FROM posts WHERE is_retweet=1").fetchone()[0]
    top = scores.rank(con, limit=20)
    zero_likes = [t["tweet_id"] for t in top if (t["likes"] or 0) == 0]
    rt_in_top = [t["tweet_id"] for t in top if t["is_retweet"]]
    resolved = unresolved = 0
    for r in con.execute("SELECT * FROM posts WHERE is_retweet=1"):
        if scores.resolve_original_tweet_id(r):
            resolved += 1
        else:
            unresolved += 1
    results["P11"] = {"retweets": rt_total, "resolved": resolved,
                      "unresolved": unresolved, "zero_likes_in_top": len(zero_likes),
                      "retweets_in_top": len(rt_in_top),
                      "ok": len(zero_likes) == 0 and len(rt_in_top) == 0}
    p(f"П11: ретвитов в базе {rt_total} (оригинал разрешён у {resolved}, "
      f"не разрешён у {unresolved}); в топ-20 нулевых лайков: {len(zero_likes)}, "
      f"ретвитов: {len(rt_in_top)} -> "
      f"{'ПРОЙДЕНО' if results['P11']['ok'] else 'НЕ ПРОЙДЕНО'}")
    if rt_handles:
        for h in rt_handles[:3]:
            rc, out = run_cli(["scores", "--account", h], env)
            p(out.rstrip())

    # ---------------------------------------------------------------- П4 отчёт
    hr("П4. Отчёт: cli report --date сегодня (на копии)")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rc, out = run_cli(["report", "--date", today], env)
    p(out.rstrip())
    blocks = [f"{i}. " for i in range(1, 8)]
    names = ["Главное за сутки", "По темам", "Деньги и запуски",
             "Новинки и инструменты", "Русскоязычный срез",
             "Тёмные лошадки и первые авторы", "Служебный блок"]
    have_blocks = all(nm in out for nm in names)
    import re as _re
    links = _re.findall(r"https://x\.com/[A-Za-z0-9_]+/status/\d+", out)
    results["P4"] = {"blocks": have_blocks, "links": len(links), "rc": rc,
                     "ok": have_blocks and rc == 0}
    p(f"П4: 7 блоков {'есть' if have_blocks else 'НЕТ'}; ссылок вида "
      f"x.com/<handle>/status/<id>: {len(links)} -> "
      f"{'ПРОЙДЕНО' if results['P4']['ok'] else 'НЕ ПРОЙДЕНО'}")
    guard("П4 report")
    # сохранить отчёт приёмки отдельным файлом, чтобы не перезаписать боевой
    if links:
        pass

    # ------------------------------------------------------------ П5 живые ссылки
    hr("П5. Живые ссылки из отчёта (3 случайные)")
    import random
    import time as _time
    sample = random.sample(links, min(3, len(links))) if links else []
    live = []
    for u in sample:
        status = 0
        for attempt in range(3):   # транзиентные сетевые сбои не считаем 404
            try:
                status, _h, _b = raw_http_get(u, timeout=25)
            except Exception as e:  # noqa: BLE001
                status = 0
                p(f"  {u} -> ошибка транспорта {e}")
            if status in (200, 403, 404):
                break
            _time.sleep(1.5)
        alive = status in (200, 403)
        live.append(alive)
        tail = "ок" if alive else ("404 — пост недоступен" if status == 404
                                   else f"транспорт/код {status}")
        p(f"  {u} -> HTTP {status} ({tail})")
    results["P5"] = {"checked": len(sample), "alive": sum(live),
                     "ok": bool(sample) and all(live)}
    p(f"П5: открылись (200/403-заглушка): {sum(live)}/{len(sample)} -> "
      f"{'ПРОЙДЕНО' if results['P5']['ok'] else 'НЕ ПРОЙДЕНО (нет ссылок или 404)'}")

    # ------------------------------------------------------------- П6 сторож
    hr("П6. Сторож в норме: python3 tuber_x/health.py")
    proc = subprocess.run([sys.executable, "tuber_x/health.py"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)
    p(f"stdout: {proc.stdout.strip()!r}")
    p(f"stderr: {proc.stderr.strip()[:300]!r}")
    p(f"exit code: {proc.returncode}")
    results["P6"] = {"rc": proc.returncode, "stdout": proc.stdout.strip(),
                     "ok": proc.returncode == 0 and proc.stdout.strip() == ""}
    p(f"П6: {'ПРОЙДЕНО' if results['P6']['ok'] else 'НЕ ПРОЙДЕНО'}")
    if not results["P6"]["ok"]:
        res = health.run(con)
        p(health.format_report(res))

    # ---------------------------------------------------------- П7 сторож при сбое
    hr("П7. Сторож при сбое: сдвиг first_seen_at на копии (>= 6 ч)")
    shifted = db.iso(datetime.now(timezone.utc) - timedelta(hours=10))
    con.execute("UPDATE posts SET first_seen_at=?", (shifted,))
    con.commit()
    proc = subprocess.run([sys.executable, "tuber_x/health.py"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)
    p(f"stdout: {proc.stdout.strip()!r}")
    p(f"exit code: {proc.returncode}")
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    results["P7"] = {"rc": proc.returncode, "lines": len(lines),
                     "ok": proc.returncode == 0 and len(lines) >= 1
                           and all(l.startswith("ALERT:") for l in lines)}
    p(f"П7: строк ALERT {len(lines)} (каждая начинается с ALERT:) -> "
      f"{'ПРОЙДЕНО' if results['P7']['ok'] else 'НЕ ПРОЙДЕНО'}")

    # -------------------------------------------------------------- П8 pytest
    hr("П8. Тесты: pytest tests -q")
    proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=ROOT,
                          capture_output=True, text=True, timeout=1800)
    tail = "\n".join(proc.stdout.strip().splitlines()[-5:])
    p(tail)
    results["P8"] = {"rc": proc.returncode, "ok": proc.returncode == 0}

    con.close()

    # ------------------------------------------------------ статусы ДО/ПОСЛЕ
    hr("ИТОГ: рабочая БД ДО и ПОСЛЕ (должны совпасть)")
    after = snapshot_prod(_PROD)
    p(f"ДО:    {short(before)}")
    p(f"ПОСЛЕ: {short(after)}")
    same = (before == after) and (fingerprint(_PROD) == _PROD_FP)
    p(f"рабочая БД не изменена: {'ДА' if same else 'НЕТ — НАРУШЕНИЕ'}")
    results["prod_unchanged"] = same

    hr("СВОДКА ПРИЁМКИ")
    passed = 0
    for k in sorted(results):
        v = results[k]
        if isinstance(v, dict) and "ok" in v:
            p(f"  {k}: {'OK' if v['ok'] else 'FAIL'}  {json.dumps(v, ensure_ascii=False)}")
            passed += 1 if v["ok"] else 0
    p(f"итог: рабочая БД не изменена = {same}")

    out_path = os.path.join(ROOT, "docs", "acceptance-log-3.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"журнал приёмки сохранён: {out_path}")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
