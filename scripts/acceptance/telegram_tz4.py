#!/usr/bin/env python3
"""Приёмка ТЗ-4: перенос Telegram в монорепозиторий `/root/tuber`.

Проверяет ровно то, что требует §2 ТЗ-4, на ЗАМОРОЖЕННЫХ копиях (боевые базы
только читаются):

  1. `pytest -q` — точное число тестов; перенесённых тестов Telegram не меньше,
     чем в `/root/tuber-telegram` (сравнение `--collect-only -q`);
  2. один живой офлайн-прогон `tuber tg scoring all` на КОПИИ единой базы:
     оценки пересчитываются, число строк `score` для telegram не меньше, чем
     `scores` в legacy;
  3. экспорт/импорт кандидатов: JSONL round-trip на копии, таблица `candidate`
     не размножается (идемпотентность), контрактные тесты зелёные;
  4. `parity` — ноль расхождений (на пересобранной из замороженных копий базе,
     как в ТЗ-3b: боевые legacy растут от живых коллекторов, поэтому сверка
     идёт против снимка, а не против «сейчас»);
  5. боевая база `/root/tuber-telegram/data/tuber_telegram.db` не тронута —
     sha256 и mtime до/после.

Запуск: python3 scripts/acceptance/telegram_tz4.py [--fast]
        (--fast пропускает пересборку единой базы и parity, беря существующую
         копию data/acceptance/unified_copy.db)
Вывод:  docs/tg/acceptance-log-tz4.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG = os.path.join(ROOT, "docs", "tg", "acceptance-log-tz4.txt")

LEGACY = {
    "os": "/root/tuber-os/data/tuber.db",
    "x": "/root/tuber-x/data/tuber_x.db",
    "tg": "/root/tuber-telegram/data/tuber_telegram.db",
}
LEGACY_TG = LEGACY["tg"]
TUBER_TG_LEGACY_REPO = "/root/tuber-telegram"
UNIFIED_COPY = os.path.join(ROOT, "data", "acceptance", "unified_copy.db")

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


def run(args, *, cwd=ROOT, timeout=3600):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path):
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    return {"sha256": sha256(path), "mtime": st.st_mtime, "size": st.st_size}


def sqlite_copy(src, dst):
    """Согласованная копия через `.backup`, источник — только для чтения."""
    if os.path.exists(dst):
        os.remove(dst)
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dst)
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()
    return dst


def q1(path, sql, params=()):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def pytest_count(path, target=None, env=None):
    args = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    if target:
        args.append(target)
    proc = subprocess.run(args, cwd=path, capture_output=True, text=True, env=env)
    tail = [ln for ln in (proc.stdout or "").splitlines() if "tests collected" in ln]
    if not tail:
        return None, proc.stdout
    return int(tail[-1].split()[0]), tail[-1]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Приёмка ТЗ-4 (Telegram)")
    ap.add_argument("--fast", action="store_true",
                    help="не пересобирать единую базу, взять готовую копию приёмки")
    args = ap.parse_args(argv)

    started = datetime.now(timezone.utc)
    work = tempfile.mkdtemp(prefix="tuber_tg_accept4_", dir="/tmp")
    hr("ПРИЁМКА ТЗ-4: перенос Telegram в монорепозиторий tuber")
    p(f"начало:          {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"репозиторий:     {ROOT}")
    p(f"рабочий каталог: {work}")

    hr("П.5 (до). Боевые базы: sha256 + mtime ДО")
    before = {}
    for name, path in LEGACY.items():
        fp = fingerprint(path)
        before[name] = fp
        p(f"  {name:3s} {path}\n      mtime={fp['mtime']} size={fp['size']} "
          f"sha256={fp['sha256'][:16]}…")

    hr("Заморозка копий legacy (read-only `.backup`)")
    copies = {}
    for name, path in LEGACY.items():
        dst = os.path.join(work, f"copy-{name}.db")
        sqlite_copy(path, dst)
        copies[name] = dst
        p(f"  {name:3s} -> {dst}")

    # ------------------------------------------------------------------ 1. тесты
    hr("П.1. Тесты: монорепозиторий против legacy-проекта Telegram")
    mon_count, mon_tail = pytest_count(ROOT)
    mon_tg, mon_tg_tail = pytest_count(ROOT, "tests/telegram")
    legacy_count, legacy_tail = pytest_count(TUBER_TG_LEGACY_REPO)
    p(f"  весь монорепозиторий: {mon_tail}")
    p(f"  tests/telegram:       {mon_tg_tail}")
    p(f"  /root/tuber-telegram: {legacy_tail}")
    check(1, "перенесённых тестов Telegram не меньше, чем в /root/tuber-telegram",
          mon_tg is not None and legacy_count is not None and mon_tg >= legacy_count,
          f"было {legacy_count} (legacy-проект), стало {mon_tg} (tests/telegram); "
          f"весь монорепозиторий — {mon_count} (собрано)")

    # ------------------------------------------------- 4. parity на снимке копий
    target = os.path.join(work, "unified.db")
    if args.fast:
        shutil.copy(UNIFIED_COPY, target)
        p(f"\n[--fast] единая база взята готовой: {UNIFIED_COPY}")
    else:
        hr("Пересборка единой базы из замороженных копий (migrate)")
        proc = run([sys.executable, "-m", "tuber", "migrate", "--target", target,
                    "--os", copies["os"], "--x", copies["x"], "--tg", copies["tg"]])
        p(proc.stdout[-3000:] if proc.stdout else "")
        if proc.returncode != 0:
            p(proc.stderr[-2000:])
        check(0, "migrate из копий завершился кодом 0", proc.returncode == 0,
              f"rc={proc.returncode}")
    # схема: домиграция на текущую версию ядра (как делает адаптер при connect)
    run([sys.executable, "-m", "tuber", "tools", "migrate", "--target", target,
         "--schema-only"])

    if not args.fast:
        hr("П.4. parity на пересобранной из копий базе")
        proc = run([sys.executable, "-m", "tuber", "parity", "--target", target,
                    "--os", copies["os"], "--x", copies["x"], "--tg", copies["tg"]])
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        for line in (proc.stdout or "").strip().splitlines()[-6:]:
            p(f"  {line}")
        check(4, "parity: ноль необъяснённых расхождений", proc.returncode == 0,
              f"rc={proc.returncode}; {tail}")

    # ------------------------------------------------- 2. офлайн-прогон скоринга
    hr("П.2. Офлайн-прогон `tuber tg scoring all` на КОПИИ единой базы")
    legacy_scores = q1(copies["tg"], "SELECT COUNT(*) FROM scores")
    score_before = q1(target, "SELECT COUNT(*) FROM score WHERE platform='telegram'")
    p(f"  legacy scores:                {legacy_scores}")
    p(f"  score(telegram) ДО прогона:   {score_before}")
    # `--deadline 0` режет ТОЛЬКО сетевую часть `forwards` (t.me/s не отдаёт
    # счётчик форвардов — долг D-34; legacy-прогон там ходил в сеть и висел на
    # таймаутах), пересчёт баз и оценок идёт полностью офлайн.
    env = dict(os.environ, TUBER_TG_REPORTS_DIR=os.path.join(work, "reports"))
    proc = subprocess.run(
        [sys.executable, "-m", "tuber", "tg", "scoring", "all",
         "--db", target, "--deadline", "0", "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800, env=env)
    tail = (proc.stdout or "").strip().splitlines()[-4:]
    for line in tail:
        p(f"  {line}")
    if proc.returncode != 0:
        p(proc.stderr[-2000:])
    score_after = q1(target, "SELECT COUNT(*) FROM score WHERE platform='telegram'")
    p(f"  score(telegram) ПОСЛЕ:        {score_after}")
    check(2, "scoring пересчитал оценки, строк score(telegram) >= legacy scores",
          proc.returncode == 0 and score_after is not None
          and legacy_scores is not None and score_after >= legacy_scores,
          f"legacy={legacy_scores}; до={score_before}; после={score_after}")

    # ------------------------------------------------------- 3. round-trip фида
    hr("П.3. Экспорт/импорт кандидатов: JSONL round-trip и идемпотентность")
    feed_dir = os.path.join(work, "exchange")
    os.makedirs(feed_dir, exist_ok=True)
    export_out = os.path.join(feed_dir, "external_candidates.jsonl")
    r1 = run([sys.executable, "-m", "tuber", "tg", "bridge", "export",
              "--db", target, "--out", export_out])
    d1 = json.loads((r1.stdout or "{}").strip().splitlines()[-1]) if r1.stdout.strip() else {}
    p(f"  экспорт: {json.dumps(d1, ensure_ascii=False)[:300]}")
    cand_after_export = q1(target, "SELECT COUNT(*) FROM candidate")

    # Приёмная фида telegram (как приходит из tuber-os): берём фикстуру проекта.
    feed_in = os.path.join(ROOT, "tests", "telegram", "fixtures", "tz17_feed.jsonl")
    imp1 = run([sys.executable, "-m", "tuber", "tg", "bridge", "import",
                "--feed", feed_in, "--db", target])
    imp2 = run([sys.executable, "-m", "tuber", "tg", "bridge", "import",
                "--feed", feed_in, "--db", target])
    i1 = json.loads((imp1.stdout or "{}").strip() or "{}")
    i2 = json.loads((imp2.stdout or "{}").strip() or "{}")
    cand_final = q1(target, "SELECT COUNT(*) FROM candidate")
    tg_cand = q1(target, "SELECT COUNT(*) FROM candidate WHERE platform='telegram'")
    p(f"  импорт 1: {json.dumps(i1, ensure_ascii=False)}")
    p(f"  импорт 2: {json.dumps(i2, ensure_ascii=False)}")
    p(f"  candidate всего: {cand_final}; из них telegram: {tg_cand}")
    check(3, "импорт фида идемпотентен: повтор даёт 0 новых",
          r1.returncode == 0 and imp1.returncode == 0 and imp2.returncode == 0
          and i1.get("imported", 0) > 0 and i2.get("imported") == 0
          and i2.get("skipped_existing", 0) == i1.get("imported", 0),
          f"импорт1={i1.get('imported')}, импорт2 imported={i2.get('imported')}, "
          f"skipped_existing={i2.get('skipped_existing')}, candidate={cand_final}")

    # Второй экспорт не должен размножить candidate (UPSERT по (platform,handle)).
    r2 = run([sys.executable, "-m", "tuber", "tg", "bridge", "export",
              "--db", target, "--out", export_out])
    d2 = json.loads((r2.stdout or "{}").strip().splitlines()[-1]) if r2.stdout.strip() else {}
    cand_after_export2 = q1(target, "SELECT COUNT(*) FROM candidate")
    p(f"  экспорт 2: candidates={d2.get('candidates')}")
    check(3.1, "повторный экспорт не размножает candidate",
          cand_after_export2 == cand_final,
          f"до={cand_final}; после={cand_after_export2}; {d2.get('candidates')}")

    hr("Контрактные тесты Telegram (в монорепозитории)")
    proc = run([sys.executable, "-m", "pytest", "-q",
                "tests/telegram/test_feed_contract.py"])
    tail = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    check(3.2, "tests/telegram/test_feed_contract.py зелёный", proc.returncode == 0,
          f"rc={proc.returncode}; {tail}")

    hr("Полный pytest -q монорепозитория")
    proc = run([sys.executable, "-m", "pytest", "-q"])
    tail = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    check(6, "полный набор монорепозитория зелёный", proc.returncode == 0,
          f"rc={proc.returncode}; {tail}")

    hr("П.5 (после). Боевые базы: sha256 + mtime ПОСЛЕ")
    after = {}
    for name, path in LEGACY.items():
        fp = fingerprint(path)
        after[name] = fp
        p(f"  {name:3s} mtime={fp['mtime']} sha256={fp['sha256'][:16]}…")
    tg_same = (before["tg"] == after["tg"])
    check(5, "боевая tuber_telegram.db не тронута (sha256 и mtime совпали)", tg_same,
          f"sha256 {before['tg']['sha256'][:16]}… == {after['tg']['sha256'][:16]}…; "
          f"mtime {before['tg']['mtime']} == {after['tg']['mtime']}")
    p("  (изменение mtime у os/x-баз означает, что в них писал живой коллектор, "
      "а не этот инструмент: обращения сюда — read-only)")

    hr("СВОДКА")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        if not r["ok"]:
            p(f"  FAIL [{r['num']}] {r['what']}: {r['data']}")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"журнал сохранён: {LOG}")
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
