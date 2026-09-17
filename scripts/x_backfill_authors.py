#!/usr/bin/env python3
"""Задача 1 ТЗ виральности: бэкфилл `posts.author_handle` реальным автором.

`owner_handle` — владелец ЛЕНТЫ (для фидов это handle самой ленты, не автор
поста). Реальный автор поста известен из CDN (`user.screen_name`), тем же
источником пользуется `scripts/verify_x_authors.py`. Скрипт идёт по строкам
`WHERE author_handle IS NULL`, спрашивает CDN ЧЕРЕЗ канал `CdnTweetBroker`
(единственная точка выхода в сеть, инвариант П9) и печатает числа до/после.

Пишет ТОЛЬКО в базу из конфига. Рабочая БД защищена: без явного
`--allow-production` скрипт отказывается работать (hard constraint ТЗ — все
проверки на копии). Пример:

    TUBER_X_DB=/tmp/fix_copy.db python3 scripts/backfill_authors.py --limit 50
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tuber.platforms.x import config, db  # noqa: E402
from tuber.platforms.x.channels import CdnTweetBroker  # noqa: E402


def _counts(con):
    total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    filled = con.execute("SELECT COUNT(*) FROM posts WHERE author_handle IS NOT NULL"
                         ).fetchone()[0]
    null = con.execute("SELECT COUNT(*) FROM posts WHERE author_handle IS NULL"
                       ).fetchone()[0]
    return {"total": total, "filled": filled, "null": null}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="максимум строк за прогон (по умолчанию все)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="доп. пауза между запросами, с (у канала свой лимитер)")
    ap.add_argument("--allow-production", action="store_true",
                    help="разрешить запись в рабочую БД (по умолчанию запрещено)")
    args = ap.parse_args(argv)

    # Рабочая БД — это база по УМОЛЧАНИЮ (единая data/tuber.db монорепозитория),
    # независимо от TUBER_X_DB/TUBER_DB: иначе проверка на копии «сравнивала
    # копию с копией» и всегда отказывала бы.
    from tuber import config as core_config
    working = os.path.realpath(str(core_config.DEFAULT_DB_PATH))
    if os.path.realpath(config.DB_PATH) == working and not args.allow_production:
        print(f"отказ: {config.DB_PATH} — рабочая БД. Все проверки делаются на копии;"
              " для записи в боевую добавь --allow-production")
        return 2

    con = db.init_db()
    try:
        before = _counts(con)
        # TODO(debt-D-29): автор известен только для живых постов, доступных CDN;
        # остальные остаются на owner_handle — см. docs/TECH-DEBT.md.
        q = ("SELECT tweet_id, owner_handle FROM posts"
             " WHERE author_handle IS NULL AND deleted_at IS NULL"
             " ORDER BY published_at_utc DESC")
        params = []
        if args.limit:
            q += " LIMIT ?"
            params.append(int(args.limit))
        rows = list(con.execute(q, params))
        print(f"бэкфилл авторов: база={config.DB_PATH}")
        print(f"до: всего={before['total']} заполнено={before['filled']}"
              f" пусто={before['null']}; к обработке={len(rows)}")

        broker = CdnTweetBroker(db_path=config.DB_PATH)
        changed, miss, err = [], 0, 0
        try:
            for r in rows:
                tid = str(r["tweet_id"])
                try:
                    kind, fields, _status = broker.fetch(tid)
                except Exception as e:  # канал не должен ронять бэкфилл
                    err += 1
                    print(f"  {tid}: ошибка канала {e!r}")
                    continue
                sn = (fields or {}).get("screen_name") if kind == "ok" else None
                if sn:
                    sn = str(sn).lstrip("@")
                    con.execute("UPDATE posts SET author_handle=? WHERE tweet_id=?"
                                " AND author_handle IS NULL", (sn, tid))
                    changed.append((tid, r["owner_handle"], sn))
                else:
                    miss += 1
                # Коммит на каждую строку: канал пишет журнал `requests` своим
                # соединением, и удержание транзакции даёт 30-секундные ожидания.
                con.commit()
                if args.sleep:
                    time.sleep(args.sleep)
            con.commit()
        finally:
            broker.close()

        after = _counts(con)
        print(f"после: всего={after['total']} заполнено={after['filled']}"
              f" пусто={after['null']}")
        print(f"заполнено этим прогоном: {len(changed)} (без автора: {miss},"
              f" ошибок канала: {err})")
        if changed:
            print(f"{'tweet_id':<20} {'owner_handle(лента)':<22} автор(CDN)")
            for tid, owner, sn in changed:
                print(f"{tid:<20} {str(owner):<22} {sn}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
