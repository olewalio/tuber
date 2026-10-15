"""CLI контура 5 (ТЗ-48): ``tuber trends inside`` и ``tuber trends novelties``.

Подкоманды::

    python3 -m tuber trends inside    [--db PATH] [--window-hours 6] [--now ISO]
                                      [--limit N] [--json]
    python3 -m tuber trends novelties [--db PATH] [--window-hours 48] [--now ISO]
                                      [--limit N] [--lookback-days D] [--no-network]
                                      [--save] [--dry-run] [--allow-production] [--json]

``inside`` — тренд внутри тренда (подтемы сюжетов, только чтение).
``novelties`` — новинки за 48 ч с внешним подтверждением. По умолчанию — чтение
и печать; ``--save`` кладёт результат дня в таблицу ``novelty`` и ГЕЙТУЕТСЯ
боевой базой (``--allow-production``), как снимок «сливок» (урок ТЗ-45F).
Отказ внешнего источника печатается честно (кто именно не ответил).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from tuber import config
from tuber.core import db as _db
from tuber.core import timeutil

from . import novelties as novelties_mod
from . import subtopics as subtopics_mod

#: Маркер журнала прогонов.
RUN_PLATFORM = "trends"
RUN_MODE = "novelties"

DEFAULT_INSIDE_LIMIT = 5
DEFAULT_NOVELTIES_LIMIT = 5


def _db_path(args) -> str:
    if args.db:
        return args.db
    return os.environ.get("TUBER_DB") or config.db_path()


def _is_production(path: str) -> bool:
    """Путь ведёт на боевую единую базу (по realpath, как у «сливок»)."""
    try:
        return os.path.realpath(str(path)) == os.path.realpath(str(config.DEFAULT_DB_PATH))
    except OSError:
        return False


def _connect(path: str):
    """Соединение с ядром + штатная аддитивная миграция схемы."""
    from tuber.core import schema

    con = _db.connect(path)
    schema.migrate_schema(con)
    return con


def _now(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    iso = timeutil.parse_any(now)
    if iso is None:
        raise ValueError(f"не разобрана дата: {now!r}")
    return datetime.strptime(iso, timeutil.ISO_FMT).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Запись новинок (гейтуется)
# ---------------------------------------------------------------------------

def save_novelties(con, data: dict, *, now=None) -> int:
    """Записать/перезаписать новинки дня в таблицу ``novelty``; вернуть число строк."""
    day = _now(now).strftime("%Y-%m-%d")
    computed_at = _now(now).strftime(timeutil.ISO_FMT)
    records = list(data.get("strict", [])) + list(data.get("external", []))
    written = 0
    for rec in records:
        items = rec.get("external_items", [])
        evidence = [
            {"platform": it.get("platform"), "title": it.get("title"),
             "url": it.get("url"), "score": it.get("score"),
             "comments": it.get("comments"), "published_at": it.get("published_at")}
            for it in items
        ]
        external_url = items[0].get("url") if items else None
        con.execute(
            """INSERT INTO novelty
                 (day, entity, tier, new_internal, internal_sources, internal_platforms,
                  external_sources, external_platforms, total_sources, internal_url,
                  external_url, evidence_json, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day, entity) DO UPDATE SET
                 tier=excluded.tier, new_internal=excluded.new_internal,
                 internal_sources=excluded.internal_sources,
                 internal_platforms=excluded.internal_platforms,
                 external_sources=excluded.external_sources,
                 external_platforms=excluded.external_platforms,
                 total_sources=excluded.total_sources,
                 internal_url=excluded.internal_url, external_url=excluded.external_url,
                 evidence_json=excluded.evidence_json, computed_at=excluded.computed_at""",
            (day, rec["entity"], rec.get("tier"), 1 if rec.get("new_internal") else 0,
             rec.get("internal_sources"),
             ",".join(rec.get("internal_platforms", [])),
             rec.get("external_sources"),
             ",".join(rec.get("external_platforms", [])),
             rec.get("total_sources"), rec.get("internal_url"), external_url,
             json.dumps(evidence, ensure_ascii=False), computed_at))
        written += 1
    con.commit()
    return written


# ---------------------------------------------------------------------------
# Подкоманды
# ---------------------------------------------------------------------------

def cmd_inside(args) -> int:
    """Тренд внутри тренда: подтемы сюжетов (только чтение)."""
    con = _connect(_db_path(args))
    try:
        data = subtopics_mod.build(con, now=args.now, window_hours=args.window_hours,
                                   accel_min=args.accel_min, min_authors=args.min_authors)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        else:
            print(subtopics_mod.format_report(data, limit=args.limit or DEFAULT_INSIDE_LIMIT))
        return 0
    finally:
        con.close()


def cmd_novelties(args) -> int:
    """Новинки за 48 ч с внешним подтверждением; ``--save`` пишет (гейт)."""
    path = _db_path(args)
    writing = bool(args.save) and not args.dry_run
    if writing and _is_production(path) and not args.allow_production:
        print("отказ: запись в боевую базу без --allow-production"
              " (приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps({"dry_run": True, "window_hours": args.window_hours,
                          "save": bool(args.save), "network": not args.no_network},
                         ensure_ascii=False, sort_keys=True))
        return 0

    external_result = None
    if args.no_network:
        external_result = {"now": None, "window_hours": args.window_hours,
                           "items": [], "sources": {
                               name: {"platform": name, "title": name,
                                      "status": "skipped", "count": 0,
                                      "error": "--no-network"}
                               for name in ("hackernews", "github", "producthunt", "arxiv")}}

    con = _connect(path)
    try:
        data = novelties_mod.build(
            con, now=args.now, window_hours=args.window_hours,
            external_result=external_result, lookback_days=args.lookback_days)
        written = 0
        if writing:
            run_id = None
            if args.allow_production or not _is_production(path):
                from tuber.core import storage
                run_id = storage.add_run(con, RUN_PLATFORM, started_at=_iso_now(args.now),
                                         mode=RUN_MODE)
            written = save_novelties(con, data, now=args.now)
            if run_id is not None:
                from tuber.core import storage
                storage.log_run(con, run_id, _iso_now(args.now), "info", None,
                                f"trends novelties: {len(data['strict'])} строгих + "
                                f"{len(data['external'])} внешних, записано {written}",
                                platform=RUN_PLATFORM)
                con.execute(
                    "UPDATE run SET finished_at=?, ok_count=?, items_new=?, errors=0, note=?"
                    " WHERE id=?", (_iso_now(args.now), written, written,
                                    "trends novelties", run_id))
                con.commit()
        if args.json:
            data["written"] = written
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        else:
            print(novelties_mod.format_report(
                data, limit=args.limit or DEFAULT_NOVELTIES_LIMIT))
            if writing:
                print(f"\nзаписано строк в novelty: {written}")
        return 0
    finally:
        con.close()


def _iso_now(now=None) -> str:
    return _now(now).strftime(timeutil.ISO_FMT)


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tuber trends",
                                 description="Подтемы сюжетов и новинки (ТЗ-48)")
    sub = ap.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="путь к базе (иначе TUBER_DB)")
    common.add_argument("--now", default=None, help="якорь времени (ISO UTC)")
    common.add_argument("--limit", type=int, default=None, help="сколько примеров печатать")
    common.add_argument("--json", action="store_true", help="машинный вывод")

    sp = sub.add_parser("inside", parents=[common], help="тренд внутри тренда (подтемы)")
    sp.add_argument("--window-hours", type=int, default=subtopics_mod.DEFAULT_WINDOW_HOURS,
                    help="длина окна сравнения, ч (по умолчанию 6)")
    sp.add_argument("--accel-min", type=float, default=subtopics_mod.ACCEL_MIN,
                    help="порог accel_sub (по умолчанию 0.5)")
    sp.add_argument("--min-authors", type=int, default=subtopics_mod.MIN_AUTHORS,
                    help="минимум независимых авторов (по умолчанию 5)")
    sp.set_defaults(func=cmd_inside)

    gate = argparse.ArgumentParser(add_help=False)
    gate.add_argument("--save", action="store_true",
                      help="записать новинки дня в таблицу novelty (гейт боевой базы)")
    gate.add_argument("--dry-run", action="store_true", help="без сети и без записи в БД")
    gate.add_argument("--allow-production", action="store_true",
                      help="разрешить запись в боевую базу (иначе отказ)")

    sp = sub.add_parser("novelties", parents=[common, gate],
                        help="новинки за 48 ч с внешним подтверждением")
    sp.add_argument("--window-hours", type=int, default=novelties_mod.DEFAULT_WINDOW_HOURS,
                    help="окно первого появления, ч (по умолчанию 48)")
    sp.add_argument("--lookback-days", type=int, default=0,
                    help="ограничить скан истории снизу, дней (0 = вся история)")
    sp.add_argument("--no-network", action="store_true",
                    help="не ходить во внешний контур (честно пустой список)")
    sp.set_defaults(func=cmd_novelties)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "trends":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)
