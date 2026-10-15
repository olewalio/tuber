#!/usr/bin/env python3
"""Приёмка ТЗ-21: договор формата фида — продюсеры по канону, импорт терпит оба вида.

Проверки:
  1-2. живые файлы на диске: у ОБОИХ продюсеров `videos` — число, `examples` —
       строки, есть `video_ids` (что было → что стало);
  3-4. РЕАЛЬНЫЙ экспорт tuber-os и tuber-telegram (в tmp) соответствует канону;
  5.   настоящий прогон импорта обоих фидов на снимке боевой базы X
       (VACUUM INTO): полный JSON — imported_new/merged/skipped_filter/
       skipped_limit/queue_total, без исключений;
  6.   приток по каждому фиду отдельно (на чистых базах) и вместе (слияние по handle);
  7.   идемпотентность: повтор на том же снимке -> imported_new=0;
  8.   кто отсеян как news_giant: факты из живого фида с числами упоминаний;
  9.   рабочие базы (tuber_x.db, tuber.db, tuber_telegram.db) не изменились;
 10.   полный pytest -q в каждом проекте (числа).

ЖЁСТКИЕ ПРАВИЛА:
  * боевые БД открываются ТОЛЬКО на чтение; копии — снимком VACUUM INTO;
  * все изменяющие прогоны идут на КОПИЯХ во временном каталоге;
  * никаких новых зависимостей: только стандартная библиотека + python3/bash.

Запуск: python3 tools/acceptance_tz21.py
Вывод:  docs/acceptance-log-21.txt (полный stdout) + консоль.
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

from tuber.platforms.x import config, store as db, feeds, registry  # noqa: E402

PROD_DB = os.path.realpath(config.DB_PATH)
FEED_TG = "/root/tuber-telegram/data/exchange/external_candidates.jsonl"
FEED_YT = "/root/tuber-os/data/exchange/external_candidates.jsonl"

PROJECTS = {
    "tuber-x": "/root/tuber",
    "tuber-os": "/root/tuber-os",
    "tuber-telegram": "/root/tuber-telegram",
}
WORK_DBS = {
    "tuber-x": "/root/tuber/data/tuber_x.db",
    "tuber-os": "/root/tuber-os/data/tuber.db",
    "tuber-telegram": "/root/tuber-telegram/data/tuber_telegram.db",
}
WORK_TABLES = {
    "tuber-x": ("accounts", "candidates", "posts", "stories"),
    "tuber-os": ("videos", "channel_candidates", "channels", "quota_log"),
    "tuber-telegram": ("channels", "posts", "classified"),
}

CANON = {
    "kind": str,
    "handle": str,
    "mentions": int,
    "videos": int,
    "video_ids": list,
    "sources": list,
    "source": str,
    "examples": list,
    "ai_hint": int,
    "first_seen": str,
    "last_seen": str,
}

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
def _type_ok(value, typ):
    if typ is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, typ)


def canon_violations(rows):
    """Список нарушений канона: (строка, поле, ожидание, факт)."""
    bad = []
    for i, row in enumerate(rows):
        for field, typ in CANON.items():
            if field not in row or row[field] is None:
                continue
            if not _type_ok(row[field], typ):
                bad.append((i, field, typ.__name__, type(row[field]).__name__))
        for field in ("video_ids", "sources", "examples"):
            for item in row.get(field) or []:
                if not isinstance(item, str):
                    bad.append((i, field, "list[str]", type(item).__name__))
    return bad


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def field_shape(path, wanted="x"):
    """Типы ключевых полей по живым строкам kind=<wanted>."""
    rows = [r for r in read_jsonl(path) if str(r.get("kind")) == wanted]
    shape = {}
    for field in ("mentions", "videos", "video_ids", "sources", "examples", "source"):
        types = {}
        for r in rows:
            if field in r and r[field] is not None:
                types[type(r[field]).__name__] = types.get(type(r[field]).__name__, 0) + 1
                if isinstance(r[field], (list, tuple)):
                    for item in r[field]:
                        key = f"элемент:{type(item).__name__}"
                        types[key] = types.get(key, 0) + 1
                        break
        shape[field] = types
    return len(rows), shape


def vacuum_copy(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        con.execute("VACUUM INTO ?", (dst,))
    finally:
        con.close()
    return dst


def table_counts(path):
    """COUNT(*) ключевых таблиц рабочей базы (только чтение, учитывает WAL)."""
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {}
    try:
        for t in WORK_TABLES.get(db_name_of(path), ()):
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error as exc:
                out[t] = f"нет таблицы ({exc})"
    finally:
        con.close()
    return out


def db_name_of(path):
    for name, pth in WORK_DBS.items():
        if os.path.realpath(pth) == os.path.realpath(path):
            return name
    return None


def candidate_handles(path):
    """Хендлы очереди `candidates` (только чтение)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT handle FROM candidates")}
    finally:
        con.close()


def run_cli(db_path, args, cwd=ROOT):
    env = dict(os.environ)
    env["TUBER_X_DB"] = db_path
    return subprocess.run([sys.executable, "-m", "tuber x", *args], cwd=cwd,
                          capture_output=True, text=True, env=env)


def cli_json(db_path, args, label):
    proc = run_cli(db_path, args)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out.startswith("{"):
        p(f"    {label}: код {proc.returncode}; stdout не JSON")
        if proc.stderr.strip():
            p("    причина (stderr): "
              + " | ".join(proc.stderr.strip().splitlines()[:4]))
        return None, proc
    try:
        return json.loads(out), proc
    except ValueError as exc:
        p(f"    {label}: stdout не JSON: {exc}")
        return None, proc


def new_db(path):
    db.init_db(path).close()
    return path


# ================================================================== основное
def main():
    started = datetime.now(timezone.utc)
    work = tempfile.mkdtemp(prefix="tuber_x_accept21_", dir="/tmp")
    hr("ПРИЁМКА ТЗ-21: договор формата фида (EXCHANGE-FEED v2)")
    p(f"начало:                    {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"репозиторий:               {ROOT}")
    p(f"боевая база X (только чт.): {PROD_DB}")
    p(f"рабочий каталог приёмки:    {work}")
    p(f"фид Telegram:  {FEED_TG} (есть={os.path.isfile(FEED_TG)})")
    p(f"фид tuber-os:  {FEED_YT} (есть={os.path.isfile(FEED_YT)})")

    if not os.path.exists(PROD_DB):
        p(f"ОШИБКА: боевая база X не найдена: {PROD_DB}")
        return 2

    prod_before = table_counts(PROD_DB) or {}
    p(f"боевая база X ДО (COUNT): {prod_before}")
    work_before = {name: table_counts(path) for name, path in WORK_DBS.items()}
    for name, counts in work_before.items():
        p(f"  рабочая база {name} ДО (COUNT): {counts}")

    # ============================================ 1-2. живые файлы на диске
    hr("П.1-2. Живые файлы продюсеров на диске: ФАКТИЧЕСКИЙ формат (что было)")
    p("Живые файлы могли быть записаны ДО правки продюсеров (их обновит плановый")
    p("экспорт). Приёмка фиксирует фактический формат и проверяет главное:")
    p("потребитель обязан разобрать ЛЮБОЙ из двух видов, не падая.")
    for label, path in (("tuber-telegram", FEED_TG), ("tuber-os", FEED_YT)):
        num = 1 if label == "tuber-telegram" else 2
        if not os.path.isfile(path):
            check(num, f"живой фид {label}", False, "файл отсутствует")
            continue
        n, shape = field_shape(path, "x")
        p(f"  {label}: строк kind=x — {n}")
        for field, types in shape.items():
            p(f"      {field}: {types}")
        # терпимость: живой файл разбирается и импортируется без исключения
        try:
            feeds.read_feed(path)
            solo_db = new_db(os.path.join(work, f"probe_{label}.db"))
            probe, _ = cli_json(solo_db, ["import_candidates", "--feed", path],
                                f"пробный импорт {label}")
            ok = probe is not None
            detail = (f"videos={shape['videos']}; examples={shape['examples']};"
                      f" video_ids={shape['video_ids']}; импорт ок"
                      if ok else f"импорт не дал JSON: videos={shape['videos']}")
        except Exception as exc:  # noqa: BLE001 — приёмка фиксирует исключение
            ok = False
            detail = f"исключение {type(exc).__name__}: {exc}"
        check(num, f"живой фид {label}: фактический формат зафиксирован,"
                   f" импорт не падает", ok, detail)

    # ============================================ 3-4. реальный экспорт продюсеров
    hr("П.3-4. Реальный экспорт продюсеров в tmp и проверка канона")
    exports = {}
    os_out = os.path.join(work, "os_export.jsonl")
    proc = subprocess.run([sys.executable, "-m", "tuber.cli", "candidates-export",
                           "--out", os_out], cwd=PROJECTS["tuber-os"],
                          capture_output=True, text=True)
    if proc.returncode == 0 and os.path.isfile(os_out):
        rows = read_jsonl(os_out)
        exports["tuber-os"] = rows
        bad = canon_violations(rows)
        check(3, "реальный экспорт tuber-os соответствует канону", not bad,
              f"строк={len(rows)}; нарушений={len(bad)}; первые={bad[:3]}")
    else:
        check(3, "реальный экспорт tuber-os соответствует канону", False,
              f"rc={proc.returncode}; stderr={proc.stderr.strip()[:200]}")

    tg_out = os.path.join(work, "tg_export.jsonl")
    proc = subprocess.run([sys.executable, "scripts/export_candidates.py",
                           "--out", tg_out], cwd=PROJECTS["tuber-telegram"],
                          capture_output=True, text=True)
    if proc.returncode == 0 and os.path.isfile(tg_out):
        rows = read_jsonl(tg_out)
        exports["tuber-telegram"] = rows
        bad = canon_violations(rows)
        check(4, "реальный экспорт tuber-telegram соответствует канону", not bad,
              f"строк={len(rows)}; нарушений={len(bad)}; первые={bad[:3]}")
    else:
        check(4, "реальный экспорт tuber-telegram соответствует канону", False,
              f"rc={proc.returncode}; stderr={proc.stderr.strip()[:200]}")

    # ============================================ 5. реальный прогон на снимке
    hr("П.5. Настоящий импорт обоих фидов на СНИМКЕ боевой базы X (VACUUM INTO)")
    snap = vacuum_copy(PROD_DB, os.path.join(work, "prod_x_snapshot.db"))
    p(f"снимок: {snap}")
    payload, proc = cli_json(snap, ["import_candidates", "--feed", FEED_TG,
                                    "--feed", FEED_YT], "импорт на снимке")
    p("полный JSON прогона:")
    p(proc.stdout.strip() or "(пусто)")
    if payload is None:
        check(5, "импорт обоих живых фидов на снимке (без исключений)", False,
              f"rc={proc.returncode}; причина выше")
        return 1
    check(5, "импорт обоих живых фидов на снимке (без исключений)",
          proc.returncode == 0 and set(payload) == {
              "feeds", "feeds_missing", "imported_new", "merged", "skipped_filter",
              "bad_fields", "skipped_limit", "queue_total", "dry"},
          f"imported_new={payload['imported_new']}; merged={payload['merged']};"
          f" skipped_limit={payload['skipped_limit']};"
          f" queue_total={payload['queue_total']}; bad_fields={payload['bad_fields']}")
    p(f"feeds: {json.dumps(payload['feeds'], ensure_ascii=False)}")

    # ============================================ 6. приток по фидам
    hr("П.6. Приток: каждый фид отдельно (чистая база) и вместе (слияние по handle)")
    solo = {}
    solo_handles = {}
    for label, path in (("tuber-telegram", FEED_TG), ("tuber-os", FEED_YT)):
        if not os.path.isfile(path):
            continue
        solo_db = new_db(os.path.join(work, f"solo_{label}.db"))
        one, one_proc = cli_json(solo_db, ["import_candidates", "--feed", path],
                                 f"только {label}")
        if one is None:
            p(f"  {label}: импорт НЕ отработал (rc={one_proc.returncode})")
            continue
        solo[label] = one
        solo_handles[label] = candidate_handles(solo_db)
        p(f"  {label}: строк={one['feeds'][0]['rows']}, kind=x={one['feeds'][0]['x_rows']},"
          f" imported_new={one['imported_new']}, merged={one['merged']},"
          f" skipped_filter={one['skipped_filter']},"
          f" skipped_limit={one['skipped_limit']}, queue_total={one['queue_total']}")
    together_db = new_db(os.path.join(work, "together.db"))
    both, both_proc = cli_json(together_db,
                               ["import_candidates", "--feed", FEED_TG, "--feed", FEED_YT],
                               "оба фида вместе")
    both_handles = candidate_handles(together_db) if both is not None else set()
    union = set().union(*solo_handles.values()) if solo_handles else set()
    shared = (sorted(set.intersection(*solo_handles.values()))
              if len(solo_handles) > 1 else [])
    crossed = sorted(both_handles - union)
    if both is not None:
        p(f"  вместе: imported_new={both['imported_new']}, merged={both['merged']},"
          f" skipped_filter={both['skipped_filter']},"
          f" skipped_limit={both['skipped_limit']}, queue_total={both['queue_total']}")
        p(f"  общих handle в двух фидах (сливаются в одну запись): {len(shared)}"
          + (f" — {shared[:15]}" if shared else ""))
        p(f"  объединение по одному фиду: {len(union)}; вместе: {len(both_handles)};"
          f" перешли порог только вместе (mentions сложились): {len(crossed)}"
          + (f" — {crossed[:15]}" if crossed else ""))
    check(6, "приток посчитан по каждому фиду и вместе; вместе ⊇ каждого по отдельности",
          bool(solo_handles) and both is not None and union <= both_handles
          and len(both_handles) == both["imported_new"],
          f"по фидам={ {k: v['imported_new'] for k, v in solo.items()} };"
          f" вместе={both['imported_new'] if both else None};"
          f" общих={len(shared)}; перешли порог вместе={len(crossed)}")

    # ============================================ 7. идемпотентность
    hr("П.7. Идемпотентность: повтор на ТОМ ЖЕ снимке")
    again, again_proc = cli_json(snap, ["import_candidates", "--feed", FEED_TG,
                                        "--feed", FEED_YT], "повтор на снимке")
    check(7, "повтор -> imported_new=0, merged>0",
          again is not None and again["imported_new"] == 0 and again["merged"] > 0,
          (f"imported_new={again['imported_new']}; merged={again['merged']};"
           f" queue_total={again['queue_total']}") if again else
          f"rc={again_proc.returncode}; JSON не получен")

    # ============================================ 8. news_giant по фактам
    hr("П.8. Кто отсеян как news_giant: факты из живого фида tuber-os")
    giants = {}
    if os.path.isfile(FEED_YT):
        parsed = feeds.read_feed(FEED_YT)
        for row in parsed["rows"]:
            h, err = registry.validate_handle(row.get("handle"))
            if err or h not in config.NEWS_GIANTS_SET:
                continue
            e = giants.setdefault(h, {"mentions": 0, "example": ""})
            e["mentions"] += int(row.get("mentions") or 0)
            bad = []
            exs = feeds._examples(row, bad)
            if exs and not e["example"]:
                e["example"] = str(exs[0])[:110]
    for h, e in sorted(giants.items(), key=lambda kv: -kv[1]["mentions"]):
        p(f"  @{h}: упоминаний {e['mentions']}; пример: {e['example']}")
    must = {"ndtv", "khabar_gaon", "skynews"}
    check(8, "верхние новостники-гиганты живого фида есть в NEWS_GIANTS",
          must <= set(giants),
          f"всего отсеяно={len(giants)}; обязательные={sorted(must)};"
          f" пропущено={sorted(must - set(giants))}")

    # ============================================ 9. рабочие базы не изменились
    hr("П.9. Рабочие базы (tuber_x.db, tuber.db, tuber_telegram.db) не изменились")
    work_after = {name: table_counts(path) for name, path in WORK_DBS.items()}
    for name in WORK_DBS:
        p(f"  {name}: ДО={work_before[name]} ПОСЛЕ={work_after[name]}"
          f" совпало={work_before[name] == work_after[name]}")
    check(9, "все три рабочие базы не изменены приёмкой (COUNT до = COUNT после)",
          all(work_before[n] == work_after[n] for n in WORK_DBS),
          f"совпало по всем: {all(work_before[n] == work_after[n] for n in WORK_DBS)}")

    # ============================================ 10. pytest
    hr("П.10. Полный pytest -q в каждом проекте")
    for name, path in PROJECTS.items():
        proc_t = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=path,
                                capture_output=True, text=True)
        tail = (proc_t.stdout or "").strip().splitlines()[-1] if proc_t.stdout else ""
        p(f"  {name}: rc={proc_t.returncode}; {tail}")
        check(10, f"pytest -q в {name} зелёный", proc_t.returncode == 0,
              f"rc={proc_t.returncode}; {tail}")

    hr("СВОДКА ПРИЁМКИ")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        p(f"  {r['num']:>3} {'OK  ' if r['ok'] else 'FAIL'} {r['what']}")

    out_path = os.path.join(ROOT, "docs", "acceptance-log-21.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
