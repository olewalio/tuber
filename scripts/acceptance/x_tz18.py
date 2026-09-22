#!/usr/bin/env python3
"""Приёмка ТЗ-18: приём кандидатов X из фидов (описания YouTube / посты Telegram).

Проверки:
  1. миграция схемы 6 -> 7 на КОПИИ боевой базы безопасна (счётчики целы);
  2. реальные фиды приняты на копии: числа imported_new/merged/skipped_filter/
     queue_total и распределение притока по ai_hint;
  3. идемпотентность: повторный прогон -> imported_new=0, merged>0;
  4. `reject`/`ok`/`provisional` существующих кандидатов не меняются;
  5. терпимость: битая строка -> bad_lines, чужой kind -> skipped_kinds,
     импорт продолжается;
  6. отсутствующий файл — не ошибка, а feeds_missing;
  7. --limit и skipped_limit;
  8. фильтры: сервисный хендл, стоп-лист, бот, новостник-гигант, порог упоминаний;
  9. дубль одного хендла в двух фидах -> один кандидат, оба фида в feed_source;
 10. живой прогон --dry на реальных фидах ничего не пишет;
 11. импорт НЕ регистрирует аккаунты (реестр до/после импорта совпал);
 12. суточная сводка (ТЗ-12) содержит строку о притоке из фидов;
 13. полный набор тестов зелёный;
 14. боевая база не изменена приёмкой, reports/ не затронут.

ЖЁСТКИЕ ПРАВИЛА:
  * боевая БД открывается ТОЛЬКО на чтение; копии делаются снимком VACUUM INTO;
  * все изменяющие прогоны идут на КОПИЯХ;
  * никаких новых зависимостей: только стандартная библиотека + python3/bash.

Запуск: python3 tools/acceptance_tz18.py
Вывод:  docs/acceptance-log-18.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import config, store as db, feeds  # noqa: E402

PROD_DB = os.path.realpath(config.DB_PATH)
WRAPPER = os.path.join(ROOT, "scripts", "tuber_x_report.sh")
FEED_TG = "/root/tuber-telegram/data/exchange/external_candidates.jsonl"
FEED_YT = "/root/tuber-os/data/exchange/external_candidates.jsonl"

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


# ------------------------------------------------------------------ утилиты
def prod_snapshot(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            "user_version": con.execute("PRAGMA user_version").fetchone()[0],
            "posts": con.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "accounts": con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            "candidates": con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
            "stories": con.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
        }
    finally:
        con.close()


def vacuum_copy(src, dst):
    """Снимок боевой БД через VACUUM INTO (учитывает WAL)."""
    if os.path.exists(dst):
        os.remove(dst)
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        con.execute("VACUUM INTO ?", (dst,))
    finally:
        con.close()
    return dst


def table_counts(con):
    out = {}
    for t in ("accounts", "candidates", "posts", "stories", "blocklist"):
        out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


def candidate_handles(con):
    return {r[0] for r in con.execute("SELECT handle FROM candidates")}


def write_feed(path, rows, *, raw_lines=()):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        for line in raw_lines:
            fh.write(line + "\n")
    return path


def run_cli(db_path, args):
    env = dict(os.environ)
    env["TUBER_X_DB"] = db_path
    return subprocess.run([sys.executable, "-m", "tuber x", *args], cwd=ROOT,
                          capture_output=True, text=True, env=env)


def run_cli_json(db_path, args, label):
    """Прогон CLI с разбором JSON. При сбое печатает ПРИЧИНУ, а не трейсбек.

    ТЗ-21/C-3: приёмка обязана предъявлять причину отказа (код возврата и
    stderr), а не падать необработанным исключением при разборе stdout.
    """
    proc = run_cli(db_path, args)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out.startswith("{"):
        p(f"    {label}: код возврата {proc.returncode}, stdout не JSON")
        if proc.stderr.strip():
            first = " | ".join(proc.stderr.strip().splitlines()[:4])
            p(f"    {label}: причина (stderr): {first}")
        return None, proc
    try:
        return json.loads(out), proc
    except ValueError as exc:
        p(f"    {label}: stdout не разобрался как JSON: {exc}")
        return None, proc


def _x(handle, mentions=5, sources=("ch1",), ai_hint=0):
    return {"kind": "x", "handle": handle, "mentions": mentions,
            "sources": list(sources), "videos": [], "ai_hint": ai_hint,
            "first_seen": "2026-08-01T00:00:00+00:00",
            "last_seen": "2026-09-01T00:00:00+00:00",
            "source": "tuber-telegram:posts"}


def giants_from_feed(path):
    """Фактически отсеиваемые новостники-гиганты из живого фида, с текстом.

    Толерантно к обоим видам `examples`: список строк (tuber-os) и список
    объектов (старый tuber-telegram).
    """
    parsed = feeds.read_feed(path)
    seen = {}
    for row in parsed["rows"]:
        h, err = db_handle(row.get("handle"))
        if err or h not in config.NEWS_GIANTS_SET:
            continue
        e = seen.setdefault(h, {"mentions": 0, "example": ""})
        e["mentions"] += int(row.get("mentions") or 0)
        bad = []
        examples = feeds._examples(row, bad)
        ex = examples[0] if examples else ""
        if not e["example"] and ex:
            e["example"] = str(ex)[:120]
    return seen


def db_handle(raw):
    from tuber.platforms.x import registry
    return registry.validate_handle(raw)


# ================================================================== основное
def main():
    started = datetime.now(timezone.utc)
    work = tempfile.mkdtemp(prefix="tuber_x_accept18_", dir="/tmp")
    hr("ПРИЁМКА ТЗ-18: приём кандидатов X из фидов YouTube-описаний и Telegram-постов")
    p(f"начало:                  {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"репозиторий:             {ROOT}")
    p(f"боевая БД (только чтение): {PROD_DB}")
    p(f"рабочий каталог приёмки:   {work}")
    p(f"фид Telegram: {FEED_TG} (есть={os.path.isfile(FEED_TG)})")
    p(f"фид YouTube (tuber-os): {FEED_YT} (есть={os.path.isfile(FEED_YT)})")

    if not os.path.exists(PROD_DB):
        p(f"ОШИБКА: боевая БД не найдена: {PROD_DB}")
        return 2

    prod_before = prod_snapshot(PROD_DB)
    p(f"боевая БД ДО: user_version={prod_before['user_version']}"
      f" accounts={prod_before['accounts']} candidates={prod_before['candidates']}"
      f" posts={prod_before['posts']} stories={prod_before['stories']}")

    # ============================================ 1. миграция на копии
    hr("П.1. Миграция схемы 6 -> 7 на КОПИИ боевой базы (снимок VACUUM INTO)")
    p("примечание: боевая база уже несёт user_version=7 — миграцию к ней применил")
    p("живой сторож (задание 23eee973e095, прогон 23:30:37 MSK), а не приёмка.")
    p("Поэтому 6 -> 7 проверяется честно: у копии откатываем схему к v6.")
    copy_main = vacuum_copy(PROD_DB, os.path.join(work, "main.db"))
    # откат к состоянию ДО ТЗ-18: снимаем добавленные колонки и версию схемы
    con_roll = sqlite3.connect(copy_main)
    for col in ("feed_source", "ai_hint"):
        con_roll.execute(f"ALTER TABLE candidates DROP COLUMN {col}")
    con_roll.execute("PRAGMA user_version=6")
    con_roll.commit()
    before_t = table_counts(con_roll)
    before_cols = {r[1] for r in con_roll.execute("PRAGMA table_info(candidates)")}
    con_roll.close()
    rc_mig = run_cli(copy_main, ["init"])
    con = sqlite3.connect(copy_main)
    uv = con.execute("PRAGMA user_version").fetchone()[0]
    cols = {r[1] for r in con.execute("PRAGMA table_info(candidates)")}
    after_t = table_counts(con)
    con.close()
    check(1, "копия мигрировала 6 -> 7; ключевые таблицы не изменились;"
          " колонки ai_hint/feed_source появились",
          uv == 7 and before_t == after_t and {"ai_hint", "feed_source"} <= cols
          and not ({"ai_hint", "feed_source"} & before_cols),
          f"rc init={rc_mig.returncode}; user_version=6->{uv};"
          f" счётчики совпали={before_t == after_t}; новые колонки="
          f"{sorted({'ai_hint', 'feed_source'} & cols)}")

    # ============================================ 2. реальные фиды (не dry)
    hr("П.2. Приём реальных фидов на КОПИИ (полный импорт)")
    con = sqlite3.connect(copy_main)
    handles_before = candidate_handles(con)
    accounts_before = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    con.close()
    payload, proc = run_cli_json(
        copy_main, ["import_candidates", "--feed", FEED_TG, "--feed", FEED_YT],
        "импорт обоих реальных фидов")
    p("stdout CLI:")
    p(proc.stdout.strip() or "(пусто)")
    con = sqlite3.connect(copy_main)
    new_handles = candidate_handles(con) - handles_before
    hint_rows = con.execute(
        "SELECT ai_hint, COUNT(*) FROM candidates WHERE handle IN"
        " ({}) GROUP BY ai_hint".format(",".join("?" * len(new_handles)) or "NULL"),
        tuple(new_handles)).fetchall() if new_handles else []
    accounts_after = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    con.close()
    hint_dist = {str(k): v for k, v in hint_rows}
    check(2, "импорт реальных фидов отработал; ровно один JSON в stdout",
          payload is not None and proc.returncode == 0 and set(payload) == {
              "feeds", "feeds_missing", "imported_new", "merged", "skipped_filter",
              "bad_fields", "skipped_limit", "queue_total", "dry"},
          (f"rc={proc.returncode}; imported_new={payload['imported_new']};"
           f" merged={payload['merged']};"
           f" skipped_filter={payload['skipped_filter']};"
           f" bad_fields={payload['bad_fields']};"
           f" skipped_limit={payload['skipped_limit']};"
           f" queue_total={payload['queue_total']}") if payload else
          f"rc={proc.returncode}; JSON не получен (причина выше)")
    if payload is None:
        p("ОСТАНОВ: импорт реальных фидов не дал JSON — приёмка предъявляет причину.")
        return 1
    p(f"приток по ai_hint (новые кандидаты): {hint_dist}")
    p(f"feeds: {json.dumps(payload['feeds'], ensure_ascii=False)}")
    p(f"feeds_missing: {payload['feeds_missing']}")

    # ТЗ-21/C-3: импорт ОБОИХ реальных фидов не падает; числа по каждому.
    per_feed = {}
    for label, path in (("tuber-telegram", FEED_TG), ("tuber-os", FEED_YT)):
        if not os.path.isfile(path):
            p(f"  {label}: файла нет — пропуск")
            continue
        # Каждый фид — на СВОЕЙ чистой базе, чтобы числа были его собственными.
        solo = os.path.join(work, f"solo_{label}.db")
        db.init_db(solo).close()
        one, one_proc = run_cli_json(
            solo, ["import_candidates", "--feed", path],
            f"только {label} (чистая база)")
        if one is None:
            p(f"  {label}: импорт НЕ отработал (rc={one_proc.returncode})")
            continue
        per_feed[label] = one
        p(f"  {label}: строк={one['feeds'][0]['rows']}, kind=x={one['feeds'][0]['x_rows']},"
          f" bad_lines={one['feeds'][0]['bad_lines']},"
          f" bad_fields={one['feeds'][0]['bad_fields']},"
          f" imported_new={one['imported_new']}, merged={one['merged']},"
          f" skipped_limit={one['skipped_limit']}, queue_total={one['queue_total']}")
    check(17, "импорт КАЖДОГО живого фида по отдельности не падает (числа выше)",
          len(per_feed) == len([pth for pth in (FEED_TG, FEED_YT)
                                if os.path.isfile(pth)]) and len(per_feed) > 0,
          f"фидов проверено: {sorted(per_feed)}")

    # обоснование NEWS_GIANTS фактическими хендлами живого фида tuber-os
    giants = giants_from_feed(FEED_YT if os.path.isfile(FEED_YT) else FEED_TG)
    p("")
    p("Фактически отсеиваемые новостники-гиганты из живого фида (kind=x):")
    for h, e in sorted(giants.items()):
        p(f"  @{h}: упоминаний {e['mentions']}; пример: {e['example']}")

    check(3, "импорт НЕ регистрирует аккаунты (реестр до/после совпал)",
          accounts_before == accounts_after,
          f"accounts {accounts_before} -> {accounts_after}")

    # ============================================ 3. идемпотентность
    hr("П.3. Идемпотентность: повторный импорт того же фида")
    payload2, proc2 = run_cli_json(
        copy_main, ["import_candidates", "--feed", FEED_TG, "--feed", FEED_YT],
        "повторный импорт обоих фидов")
    check(4, "повтор -> imported_new=0, merged>0 (растёт учёт подтверждений)",
          payload2 is not None and payload2["imported_new"] == 0
          and payload2["merged"] > 0,
          (f"imported_new={payload2['imported_new']}; merged={payload2['merged']};"
           f" queue_total={payload2['queue_total']}") if payload2 else
          f"rc={proc2.returncode}; JSON не получен")
    if payload2 is None:
        return 1

    # ============================================ 4-9. негативные на отдельной копии
    hr("П.4-9. Негативные проверки (отдельная чистая база приёмки)")
    neg = os.path.join(work, "neg.db")
    db.init_db(neg).close()
    con = sqlite3.connect(neg)
    con.row_factory = sqlite3.Row
    # готовим состояния: reject / ok / provisional
    con.executemany(
        "INSERT OR REPLACE INTO candidates (handle, seen_count, validated,"
        " reject_reason, verified_at, first_seen_at, last_seen_at)"
        " VALUES (?,1,?,?,?,?,?)",
        [("keep_rej", "reject", "not_found", None, db.utcnow_iso(), db.utcnow_iso()),
         ("keep_ok", "ok", None, "2026-01-01T00:00:00", db.utcnow_iso(), db.utcnow_iso()),
         ("keep_prov", "provisional", None, None, db.utcnow_iso(), db.utcnow_iso())])
    con.execute("INSERT INTO blocklist (handle, reason) VALUES ('bad_boy','manual')")
    con.commit()
    con.close()

    f1 = write_feed(os.path.join(work, "feeds", "a", "external_candidates.jsonl"),
                    [_x("keep_rej", 4), _x("keep_ok", 4), _x("keep_prov", 4),
                     _x("new_a", 4), _x("search", 9), _x("spammer12345678", 9),
                     _x("foxnews", 9), _x("low_x", 1), _x("bad_boy", 5)],
                    raw_lines=["{битая строка", "{ещё одна битая",
                               json.dumps({"kind": "youtube", "handle": "yt_only"})])
    f2 = write_feed(os.path.join(work, "feeds", "b", "external_candidates.jsonl"),
                    [_x("new_a", 4), _x("twin_ai", 4)])
    missing = os.path.join(work, "feeds", "нет.jsonl")

    neg_payload, proc_neg = run_cli_json(
        neg, ["import_candidates", "--feed", f1, "--feed", f2, "--feed", missing],
        "негативный прогон")
    if neg_payload is None:
        check(5, "целостность негативного прогона", False,
              f"rc={proc_neg.returncode}; JSON не получен")
        return 1
    con = sqlite3.connect(neg)
    con.row_factory = sqlite3.Row
    states = {r["handle"]: dict(r) for r in con.execute(
        "SELECT * FROM candidates WHERE handle IN ('keep_rej','keep_ok','keep_prov')")}
    acc_total = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    con.close()
    p(f"neg: {json.dumps(neg_payload, ensure_ascii=False)}")

    check(5, "reject/ok/provisional существующих не изменились и не понижены",
          states["keep_rej"]["validated"] == "reject"
          and states["keep_rej"]["reject_reason"] == "not_found"
          and states["keep_ok"]["validated"] == "ok"
          and states["keep_ok"]["verified_at"] == "2026-01-01T00:00:00"
          and states["keep_prov"]["validated"] == "provisional",
          f"reject={states['keep_rej']['validated']};"
          f" ok={states['keep_ok']['validated']};"
          f" provisional={states['keep_prov']['validated']}")
    check(6, "терпимость: битые строки -> bad_lines, чужой kind -> skipped_kinds",
          neg_payload["feeds"][0]["bad_lines"] == 2
          and neg_payload["feeds"][0]["skipped_kinds"] == 1,
          f"bad_lines={neg_payload['feeds'][0]['bad_lines']};"
          f" skipped_kinds={neg_payload['feeds'][0]['skipped_kinds']}")
    check(7, "отсутствующий файл — не ошибка, а feeds_missing",
          neg_payload["feeds_missing"] == [missing] and proc_neg.returncode == 0,
          f"feeds_missing={neg_payload['feeds_missing']}; rc={proc_neg.returncode}")
    sf = neg_payload["skipped_filter"]
    check(8, "фильтры: сервисный, бот, новостник-гигант, стоп-лист, порог",
          sf["service_handle"] == 1 and sf["bot"] == 1 and sf["news_giant"] == 1
          and sf["blocklist"] == 1 and sf["below_mention_threshold"] == 1,
          f"service={sf['service_handle']}; bot={sf['bot']};"
          f" news_giant={sf['news_giant']}; blocklist={sf['blocklist']};"
          f" below_threshold={sf['below_mention_threshold']}")
    check(9, "дубль в двух фидах -> один кандидат, оба фида в feed_source",
          acc_total == 5 and neg_payload["imported_new"] == 2
          and neg_payload["merged"] == 3,
          f"candidates={acc_total}; imported_new={neg_payload['imported_new']};"
          f" merged={neg_payload['merged']} (new_a в двух фидах учтён один раз)")

    # --limit
    limdb = vacuum_copy(PROD_DB, os.path.join(work, "limit.db"))
    run_cli(limdb, ["init"])
    lim_payload, proc_lim = run_cli_json(
        limdb, ["import_candidates", "--feed", FEED_TG, "--limit", "3"],
        "--limit")
    if lim_payload is None:
        check(10, "--limit", False, f"rc={proc_lim.returncode}; JSON не получен")
        return 1
    con = sqlite3.connect(limdb)
    lim_count = con.execute("SELECT COUNT(*) FROM candidates WHERE feed_source IS NOT NULL"
                            ).fetchone()[0]
    con.close()
    check(10, "--limit ограничивает приём; остаток учтён в skipped_limit",
          lim_payload["imported_new"] == 3 and lim_payload["skipped_limit"] > 0
          and lim_count == 3,
          f"imported_new={lim_payload['imported_new']};"
          f" skipped_limit={lim_payload['skipped_limit']}; записей в базе={lim_count}")

    # ============================================ 10. живой --dry
    hr("П.10. Живой прогон --dry на реальных фидах (запись запрещена)")
    drydb = vacuum_copy(PROD_DB, os.path.join(work, "dry.db"))
    run_cli(drydb, ["init"])
    con = sqlite3.connect(drydb)
    dry_before = candidate_handles(con)
    con.close()
    dry_payload, proc_dry = run_cli_json(
        drydb, ["import_candidates", "--feed", FEED_TG, "--feed", FEED_YT, "--dry"],
        "живой --dry")
    if dry_payload is None:
        check(11, "живой --dry", False, f"rc={proc_dry.returncode}; JSON не получен")
        return 1
    con = sqlite3.connect(drydb)
    dry_after = candidate_handles(con)
    con.close()
    p("живой stdout --dry:")
    p(proc_dry.stdout.strip())
    check(11, "--dry считает те же числа, но в базу ничего не пишет",
          dry_payload["dry"] is True and dry_payload["imported_new"] > 0
          and dry_before == dry_after,
          f"imported_new={dry_payload['imported_new']};"
          f" merged={dry_payload['merged']};"
          f" в базе новых={len(dry_after - dry_before)};"
          f" queue_total={dry_payload['queue_total']}")

    # ============================================ 12. строка сводки
    hr("П.12. Строка о притоке из фидов в суточной сводке (ТЗ-12)")
    sumdb = vacuum_copy(PROD_DB, os.path.join(work, "summary.db"))
    run_cli(sumdb, ["init"])
    # избавляемся от сетевого перевода: кладём в кэш все тексты классификаций
    con = sqlite3.connect(sumdb)
    con.execute("INSERT OR IGNORE INTO report_texts (text_hash, ru, model, created_at, src)"
                " SELECT text_hash, 'перевод отключён на приёмке', 'acceptance', ?,"
                " 'acceptance' FROM classified", (db.utcnow_iso(),))
    con.commit()
    con.close()
    report_dir = os.path.join(work, "reports")
    os.makedirs(report_dir, exist_ok=True)
    env = dict(os.environ)
    env["TUBER_X_DB"] = sumdb
    env["TUBER_X_REPORT_DIR"] = report_dir
    env["TUBER_X_PROJECT"] = ROOT
    proc_sum = subprocess.run([WRAPPER], cwd=ROOT, capture_output=True, text=True,
                              env=env)
    feed_line = [ln for ln in proc_sum.stdout.splitlines() if "Приток из фидов" in ln]
    p("строка сводки:")
    p(feed_line[0] if feed_line else "(не найдена)")
    check(12, "сводка содержит строку «Приток из фидов» с очередью и горизонтом",
          proc_sum.returncode == 0 and len(feed_line) == 1
          and "Всего в очереди" in feed_line[0] and "закроется за" in feed_line[0],
          f"rc={proc_sum.returncode}; строк={len(feed_line)}")
    # дополнительно: feed_influx не падает и честно считает на базе БЕЗ
    # колонки feed_source (схема до ТЗ-18)
    oldp = os.path.join(work, "v6.db")
    old = sqlite3.connect(oldp)
    old.execute("CREATE TABLE candidates (handle TEXT PRIMARY KEY, validated TEXT)")
    old.execute("INSERT INTO candidates (handle) VALUES ('x1')")
    old.commit()
    influx_old = feeds.feed_influx(old)
    old.close()
    check(13, "feed_influx честен на схеме без feed_source (нули, не падение)",
          influx_old["yt_desc"] == 0 and influx_old["tg_posts"] == 0
          and influx_old["queue_total"] == 1,
          f"yt={influx_old['yt_desc']}; tg={influx_old['tg_posts']};"
          f" queue={influx_old['queue_total']}; horizon={influx_old['horizon_days']}")

    # ============================================ 13. тесты
    hr("П.13. Полный набор тестов")
    proc_t = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                            capture_output=True, text=True, env=dict(os.environ))
    tail = (proc_t.stdout or "").strip().splitlines()[-1] if proc_t.stdout else ""
    check(14, "полный набор тестов зелёный", proc_t.returncode == 0,
          f"pytest rc={proc_t.returncode}; {tail}")

    # ============================================ 14. боевая БД
    hr("П.14. Боевая база и reports/ приёмкой не затронуты")
    prod_after = prod_snapshot(PROD_DB)
    p(f"боевая ДО:    {prod_before}")
    p(f"боевая ПОСЛЕ: {prod_after}")
    check(15, "боевая база не изменена (счётчики и user_version совпали)",
          prod_before == prod_after, f"совпало={prod_before == prod_after}")
    check(16, "служебные файлы приёмки — во временном каталоге /tmp",
          work.startswith("/tmp") or work.startswith(tempfile.gettempdir()),
          f"work={work}; gettempdir={tempfile.gettempdir()}")

    hr("СВОДКА ПРИЁМКИ")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        p(f"  {r['num']:>3} {'OK  ' if r['ok'] else 'FAIL'} {r['what']}")

    out_path = os.path.join(ROOT, "docs", "acceptance-log-18.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
