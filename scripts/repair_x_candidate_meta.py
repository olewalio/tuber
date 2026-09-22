"""Восстановление строк candidate платформы X, задетых случайным экспортом Telegram.

Один прогон `tuber tg bridge export` был выполнен против боевой единой базы
`data/tuber.db` (ошибка исполнителя при проверке CLI). Экспорт UPSERT-ит
найденные X/YouTube-кандидаты в общую таблицу `candidate`, поэтому:

* 2 476 новых строк (found_via='tuber-telegram:posts') — удалены;
* 36 существующих строк X потеряли свой meta_json (перезаписан формой экспорта)
  и получили seen_count+1.

Этот скрипт возвращает эти 36 строк к состоянию, которое даёт миграция из
legacy-базы X: meta_json = {verified_at, sources}, seen_count и last_seen_at —
из тех же legacy-строк. Legacy-база открывается ТОЛЬКО на чтение.

Запуск: python3 scripts/repair_x_candidate_meta.py [--db PATH]
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tuber.core import legacy, storage  # noqa: E402
from tuber.tools import migrate_legacy as ml

X_LEGACY = "/root/tuber-x/data/tuber_x.db"
DB = "/root/tuber/data/tuber.db"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--x", default=X_LEGACY)
    args = ap.parse_args(argv)

    from tuber.core import db as core_db

    target = core_db.connect(args.db)
    src = legacy.open_legacy(args.x)
    fixed = 0
    try:
        rows = target.execute(
            "SELECT handle, meta_json, seen_count, last_seen_at FROM candidate"
            " WHERE platform='x' AND meta_json LIKE '%\"examples\"%'"
            " AND meta_json LIKE '%\"mentions\"%'"
        ).fetchall()
        print(f"пострадавших строк X: {len(rows)}")
        for row in rows:
            handle = row[0]
            legacy_row = src.execute(
                "SELECT verified_at, sources, seen_count, last_seen_at"
                " FROM candidates WHERE handle=?", (handle,)).fetchone()
            if legacy_row is None:
                print(f"  ПРОПУСК {handle}: нет в legacy")
                continue
            extra = {
                "verified_at": ml._norm(legacy_row[0]),
                "sources": ml._norm_or_none(legacy_row[1]),
            }
            seen = legacy_row[2] or 1
            last = ml._norm(legacy_row[3])
            target.execute(
                "UPDATE candidate SET meta_json=?, seen_count=?, last_seen_at=?"
                " WHERE platform='x' AND handle=?",
                (storage.jdump(extra), seen, last, handle))
            fixed += 1
        target.commit()
        print(f"восстановлено строк: {fixed}")
    finally:
        src.close()
        target.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
