#!/usr/bin/env python3
"""Сверка отчётов X: legacy-код на копии legacy-базы ↔ монорепо на копии ядра.

Приёмка ТЗ-3 §3.2/§3.5. Что делает:

1. берёт КОПИИ обеих баз (рабочие базы не трогает: правка запрещена);
2. если базы ядра нет — переносит её из legacy копии (`tuber migrate`);
3. гоняет отчёт legacy-кодом (`python3 -m tuber_x.cli report`) и новым кодом
   (`python3 -m tuber x report`) за одну дату;
4. сравнивает выводы построчно и печатает таблицу расхождений с причинами;
5. замеряет время обоих прогонов.

Использование::

    python3 scripts/acceptance/x_compare_reports.py --date 2026-09-15 \
        --legacy-db /root/tuber-x/data/tuber_x.db \
        --core-db /root/tuber/data/tuber.db [--workdir DIR] [--legacy-repo PATH]

Обе базы копируются ДО запуска (``sqlite3 .backup``), поэтому даже при ошибке
в коде боевые файлы не меняются.
"""
from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _backup(src: str, dst: str) -> None:
    """Консистентная копия живой базы (WAL-safe) через ``.backup``."""
    import sqlite3

    if os.path.exists(dst):
        os.unlink(dst)
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(dst)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()


def _run(cmd: list[str], *, cwd: str, env: dict[str, str]) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    out = proc.stdout
    if proc.returncode not in (0, 4):          # 4 = «нет данных за сутки» (норма)
        out += "\n--- stderr ---\n" + proc.stderr
    return proc.returncode, out, dt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="дата отчёта YYYY-MM-DD")
    ap.add_argument("--legacy-db", required=True, help="боевая база tuber-x (только чтение)")
    ap.add_argument("--core-db", required=True, help="единая база ядра (копируется)")
    ap.add_argument("--legacy-repo", default="/root/tuber-x")
    ap.add_argument("--workdir", default=None, help="каталог для копий и отчётов")
    ap.add_argument("--runs", type=int, default=3, help="повторов замера времени")
    args = ap.parse_args(argv)

    work = args.workdir or os.path.join(ROOT, "data", "acceptance")
    os.makedirs(work, exist_ok=True)
    legacy_copy = os.path.join(work, "x_legacy_copy.db")
    core_copy = os.path.join(work, "unified_copy.db")
    legacy_out = os.path.join(work, f"report_legacy_{args.date}.txt")
    core_out = os.path.join(work, f"report_core_{args.date}.txt")

    print(f"1. копии баз → {legacy_copy}, {core_copy}")
    _backup(args.legacy_db, legacy_copy)
    _backup(args.core_db, core_copy)

    # Единая копия может быть снята до переноса X: домигрируем её из legacy копии
    # (тот же инструмент ТЗ-1, идемпотентный).
    from tuber.tools import migrate_legacy
    from tuber.tools.migrate_legacy import Stats, migrate_x
    from tuber.core import db as core_db
    from tuber.core.schema import migrate_schema

    target = core_db.connect(core_copy)
    migrate_schema(target)
    stats = Stats()
    # ``migrate_x`` сам открывает транзакцию записи (BEGIN IMMEDIATE): оборачивать
    # её второй нельзья — SQLite ответит «cannot start a transaction within a
    # transaction».
    migrate_x(target, legacy_copy, stats)
    print(f"   домиграция X: {sum(stats.migrated.values())} строк,"
          f" пропусков {sum(stats.skipped.values())}")
    target.close()

    print("2a. parity на копиях (правила X)")
    from tuber.tools import parity_report
    parity_rc = parity_report.main(["--target", core_copy, "--x", legacy_copy])
    print(f"   код parity: {parity_rc} (0 — расхождений нет, 2 — есть)")

    env_legacy = dict(os.environ)
    env_legacy["TUBER_X_DB"] = legacy_copy
    env_legacy["TUBER_X_REPORT_DIR"] = work
    env_core = dict(os.environ)
    env_core["TUBER_DB"] = core_copy
    env_core["TUBER_X_REPORT_DIR"] = work

    print(f"2. отчёт legacy ({args.date})")
    rc_l, out_l, dt_l = _run([sys.executable, "-m", "tuber_x.cli", "report",
                              "--date", args.date, "--stdout-only"],
                             cwd=args.legacy_repo, env=env_legacy)
    open(legacy_out, "w", encoding="utf-8").write(out_l)
    print(f"   код {rc_l}, {len(out_l.splitlines())} строк, {dt_l:.2f} с")

    print(f"3. отчёт монорепозитория ({args.date})")
    rc_c, out_c, dt_c = _run([sys.executable, "-m", "tuber", "x", "report",
                              "--date", args.date, "--stdout-only"],
                             cwd=ROOT, env=env_core)
    open(core_out, "w", encoding="utf-8").write(out_c)
    print(f"   код {rc_c}, {len(out_c.splitlines())} строк, {dt_c:.2f} с")

    print("4. построчная сверка")
    diff = list(difflib.unified_diff(out_l.splitlines(), out_c.splitlines(),
                                     fromfile="legacy", tofile="core", lineterm=""))
    if not diff:
        print("   расхождений нет")
    else:
        print(f"   строк расхождения: {len([d for d in diff if d[:1] in '+-' and d[:3] not in ('+++', '---')])}")
        for line in diff:
            print("   " + line)

    print("5. время (лучший из %d повторов)" % args.runs)
    best_l = min(dt_l, *(_run([sys.executable, "-m", "tuber_x.cli", "report",
                               "--date", args.date, "--stdout-only"],
                              cwd=args.legacy_repo, env=env_legacy)[2]
                          for _ in range(max(0, args.runs - 1))))
    best_c = min(dt_c, *(_run([sys.executable, "-m", "tuber", "x", "report",
                               "--date", args.date, "--stdout-only"],
                              cwd=ROOT, env=env_core)[2]
                          for _ in range(max(0, args.runs - 1))))
    ratio = best_c / best_l if best_l else float("inf")
    print(f"   legacy {best_l:.2f} с, монорепо {best_c:.2f} с, отношение {ratio:.2f}×")
    print(f"   вердикт: {'OK' if ratio <= 2.0 else 'ДЕФЕКТ (медленнее более чем в 2 раза)'}")
    return 0 if not diff else 1


if __name__ == "__main__":
    sys.exit(main())
