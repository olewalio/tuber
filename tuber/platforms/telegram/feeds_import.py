#!/usr/bin/env python3
"""ТЗ-17, часть A: импорт кандидатов-каналов из фида tuber-os (kind="telegram").

Читает JSONL-фид (формат ТЗ-16), берёт только строки kind="telegram", пишет новых
кандидатов в channels (status='candidate', source='feed:tuber-os'). Существующие
записи во ВСЕХ статусах не трогаются. Фильтр (служебное/боты/гиганты/порог) — по
конфигу config/bridge_sources.json.

stdout — ровно один JSON: imported, skipped_existing, skipped_filter,
skipped_limit, dry, feed. Ошибки файла — в stderr понятной строкой, код 2.

Пример:
  python3 -m tuber tg bridge import --feed /root/tuber-os/data/exchange/external_candidates.jsonl --dry
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import bridge as bc  # noqa: E402
from . import store as db  # noqa: E402


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Импорт кандидатов-каналов из фида tuber-os (ТЗ-17/A)")
    ap.add_argument("--feed", required=True, help="путь к JSONL-фиду tuber-os")
    ap.add_argument("--limit", type=int, default=200, help="максимум импортов за прогон (по умолчанию 200)")
    ap.add_argument("--dry", action="store_true", help="сводка без записи в БД")
    ap.add_argument("--min-mentions", type=int, default=None, help="порог упоминаний (по умолчанию из конфига)")
    ap.add_argument("--db", default=bc.DEFAULT_DB, help="путь к БД (по умолчанию рабочая)")
    ap.add_argument("--config", default=bc.DEFAULT_CONFIG, help="путь к конфигу фильтра")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    min_mentions = args.min_mentions
    if min_mentions is None:
        try:
            min_mentions = int(bc.load_config(args.config).get("min_mentions_default", 2))
        except bc.ConfigError as exc:
            sys.stderr.write(f"ошибка: {exc}\n")
            return 2

    result = {
        "imported": 0,
        "skipped_existing": 0,
        "skipped_filter": 0,
        "skipped_limit": 0,
        "dry": bool(args.dry),
        "feed": args.feed,
    }
    try:
        cfg = bc.load_config(args.config)
        records = bc.read_feed(args.feed, kind="telegram")
        candidates = bc.feed_to_candidates(records, cfg)
        con = db.connect(args.db)
        try:
            stats = bc.import_candidates(
                con, candidates,
                source="feed:tuber-os", dry=args.dry,
                limit=args.limit, min_mentions=min_mentions, cfg=cfg,
            )
        finally:
            con.close()
    except bc.ConfigError as exc:
        sys.stderr.write(f"ошибка: {exc}\n")
        return 2
    except sqlite3.Error as exc:
        sys.stderr.write(f"ошибка БД: {exc}\n")
        return 2

    result.update({k: stats[k] for k in ("imported", "skipped_existing", "skipped_filter", "skipped_limit")})
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
