"""CLI графа источников (ТЗ-8): ``tuber graph <подкоманда>``.

Подкоманды:

* ``backfill`` — явный бэкфилл рёбер из уже собранного корпуса (Р1.6);
* ``consume`` — рёбра → кандидаты четырёх типов (Р2.x);
* ``report`` — отчёт графа числами (Р5.1/Р5.3);
* ``verify-feeds`` — верификация веб-фидов (Р3.1/Р3.4), сеть только здесь;
* ``prune`` — авто-отсев источников без отдачи (Р5.2).

Путь к базе — ``--db PATH`` или ``TUBER_DB``/``TUBER_X_DB`` (как у остального CLI).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from tuber.core import graph


def _db_path(args) -> str:
    if args.db:
        return args.db
    from tuber import config
    return os.environ.get("TUBER_DB") or os.environ.get("TUBER_X_DB") or config.db_path()


def _is_production(path: str) -> bool:
    """Путь ведёт на боевую единую базу (по realpath, ТЗ-47)."""
    from tuber import config
    try:
        return os.path.realpath(str(path)) == os.path.realpath(str(config.DEFAULT_DB_PATH))
    except OSError:
        return False


def _connect(args):
    """Соединение с штатной миграцией схемы на коннекте (таблица ``edge`` и
    код платформы ``web`` аддитивны — ``CREATE IF NOT EXISTS``)."""
    from tuber.core import db, schema
    con = db.connect(_db_path(args))
    schema.migrate_schema(con)
    return con


def _platforms(value):
    if not value:
        return None
    return tuple(p.strip() for p in value.split(",") if p.strip())


def cmd_backfill(args) -> int:
    from tuber.core import db
    con = _connect(args)
    try:
        stats = graph.backfill(con, platforms=_platforms(args.platforms) or ("x", "telegram", "youtube"),
                               limit=args.limit, dry=args.dry_run)
    finally:
        con.close()
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_consume(args) -> int:
    from tuber.core import db
    con = _connect(args)
    try:
        stats = graph.consume(con, platforms=_platforms(args.platforms),
                              limit=args.limit, dry=args.dry_run)
    finally:
        con.close()
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_report(args) -> int:
    from tuber.core import db
    con = _connect(args)
    try:
        if args.json:
            print(json.dumps(graph.report(con, days=args.days), ensure_ascii=False,
                             sort_keys=True))
        else:
            print(graph.format_report(con, days=args.days))
    finally:
        con.close()
    return 0


def cmd_verify_feeds(args) -> int:
    from tuber.core import db
    con = _connect(args)
    broker = graph.RawHTTPBroker()
    summary = {"checked": 0, "feed": 0, "html_only": 0, "blocked": 0,
               "redirect": 0, "dead": 0, "no_content": 0, "sources_created": 0,
               "skipped_budget": 0, "details": []}
    try:
        budget = graph.feed_verify_budget(con, limit=args.limit)
        if budget <= 0:
            summary["skipped_budget"] = 1
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0
        rows = graph.eligible_candidates(con, limit=budget, platforms=("web",))
        for row in rows:
            res = graph.verify_feed(con, row["handle"], None, broker=broker)
            summary["checked"] += 1
            v = res.get("verdict") or "?"
            if v in summary:
                summary[v] += 1
            if res.get("source_created"):
                summary["sources_created"] += 1
            summary["details"].append({"domain": row["handle"], "verdict": v,
                                       "http": res.get("http"), "feed_url": res.get("feed_url"),
                                       "entries": res.get("entries")})
    finally:
        con.close()
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_prune(args) -> int:
    from tuber.core import db
    con = _connect(args)
    try:
        stats = graph.prune_no_yield(con, days=args.days, dry=args.dry_run)
    finally:
        con.close()
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_first_movers(args) -> int:
    """Граф первопроходцев (ТЗ-47): trusted_indegree, lead_time, first_mover.

    Гейт записи: на боевой базе прогон без ``--allow-production`` отказывает,
    потому что команда пишет реестр ``first_mover`` и ось ``score.first_mover``.
    Копия базы (``TUBER_DB``) пишется без флага; ``--dry-run`` считает без записи.
    """
    from tuber.core import firstmovers
    path = _db_path(args)
    write = not args.dry_run
    if write and not args.allow_production and _is_production(path):
        print("отказ: запись в боевую базу без --allow-production"
              " (приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        return 2
    con = _connect(args)
    try:
        stats = None
        if write:
            stats = firstmovers.refresh(con, days=args.days, limit=args.limit,
                                        write_data=True)
        data = firstmovers.report(con, days=args.days, limit=args.limit)
        if args.json:
            payload = {"stats": stats, "report": data}
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            if stats is not None:
                print("прогон: записей реестра %d, оценок обновлено %d, run_id %s"
                      % (stats["ledger_rows"], stats["score_rows"], stats["run_id"]))
            print(firstmovers.format_report(data))
    finally:
        con.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tuber graph", description="Граф источников (ТЗ-8)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _common(sp):
        sp.add_argument("--db", default=None, help="путь к базе (иначе TUBER_DB)")
        sp.add_argument("--dry-run", action="store_true", help="без записи в БД")

    sp = sub.add_parser("backfill", help="бэкфилл рёбер из корпуса (Р1.6)")
    _common(sp)
    sp.add_argument("--platforms", default=None, help="x,telegram,youtube (по умолчанию все)")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser("consume", help="рёбра -> кандидаты (Р2)")
    _common(sp)
    sp.add_argument("--platforms", default=None, help="ограничить платформы-родители")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_consume)

    sp = sub.add_parser("report", help="отчёт графа (Р5)")
    sp.add_argument("--db", default=None)
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("verify-feeds", help="верификация веб-фидов (Р3.4)")
    _common(sp)
    sp.add_argument("--limit", type=int, default=graph.EDGE_FEED_VERIFY_DAILY_LIMIT)
    sp.set_defaults(func=cmd_verify_feeds)

    sp = sub.add_parser("prune", help="авто-отсев по отдаче (Р5.2)")
    _common(sp)
    sp.add_argument("--days", type=int, default=graph.EDGE_NO_YIELD_DAYS)
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("first-movers", help="граф первопроходцев (ТЗ-47)")
    _common(sp)
    sp.add_argument("--days", type=int, default=30, help="окно trusted_indegree (дн)")
    sp.add_argument("--limit", type=int, default=None,
                    help="не более N сюжетов в разборе (приёмочный прогон)")
    sp.add_argument("--allow-production", action="store_true",
                    help="разрешить запись в боевую базу (иначе отказ)")
    sp.add_argument("--json", action="store_true", help="машинный вывод")
    sp.set_defaults(func=cmd_first_movers)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
