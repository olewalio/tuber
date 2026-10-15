#!/usr/bin/env python3
"""Бэкфилл языка YouTube-источников (ТЗ-3 §4.1).

У 12 524 из 13 236 YouTube-источников ``source.lang`` пуст; у 8 801 из них
язык восстанавливается по самому частому ``content.lang`` их видео. Миграция
тяжёлая (полный скан ``content`` по пустым YouTube-источникам) и не должна
срабатывать на каждом соединении через ``migrate_schema``, поэтому запускается
явно этим скриптом.

Идемпотентно: повторный прогон ничего не меняет (предикат требует пустой
``lang``). Режим ``--dry-run`` только считает, сколько строк изменилось бы,
и НИЧЕГО не пишет.

Запуск::

    python3 scripts/backfill_source_lang.py --dry-run
    python3 scripts/backfill_source_lang.py
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tuber import config
from tuber.core import db as core_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="backfill_source_lang",
        description="Заполнить source.lang YouTube по языкам видео (ТЗ-3 §4.1)")
    ap.add_argument("--db", dest="db_path", default=None,
                    help="путь к единой базе (по умолчанию боевая)")
    ap.add_argument("--dry-run", action="store_true",
                    help="только посчитать изменяемые строки, ничего не писать")
    args = ap.parse_args(argv)

    db_path = config.db_path(args.db_path)
    if not os.path.exists(db_path):
        print(f"ошибка: базы нет: {db_path}", file=sys.stderr)
        return 2

    conn = core_db.connect(db_path)
    try:
        changed = core_db.backfill_source_lang(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    verb = "изменилось бы" if args.dry_run else "заполнено"
    print(f"source.lang: {verb} строк — {changed}")
    if args.dry_run:
        print("(dry-run: база не изменена)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
