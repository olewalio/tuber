#!/usr/bin/env python3
"""ТЗ-17, часть B: дискавери t.me-кандидатов из собственных постов.

Сканирует posts.text и posts.links (только чтение), извлекает telegram-хендлы и
импортирует их тем же путём, что часть A, но с source='post_mentions'. Фильтр и
дедупликация — общие (:mod:`tuber.platforms.telegram.bridge`, не копипаста).

stdout — один JSON: imported, skipped_existing, skipped_filter, skipped_limit,
dry, source, scanned_handles.

Пример:
  python3 -m tuber tg discover --limit 200 --dry
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import bridge as bc  # noqa: E402
from . import store as db  # noqa: E402

SOURCE = "post_mentions"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Дискавери telegram-кандидатов из своих постов (ТЗ-17/B)")
    ap.add_argument("--limit", type=int, default=200, help="максимум импортов за прогон (по умолчанию 200)")
    ap.add_argument("--dry", action="store_true", help="сводка без записи в БД")
    ap.add_argument("--min-mentions", type=int, default=None, help="порог упоминаний (по умолчанию из конфига)")
    ap.add_argument("--db", default=bc.DEFAULT_DB, help="путь к БД (по умолчанию рабочая)")
    ap.add_argument("--config", default=bc.DEFAULT_CONFIG, help="путь к конфигу фильтра")
    return ap.parse_args(argv)


def scan_posts(con) -> dict:
    """{handle: {"mentions": n(постов), "ai_hint": bool, "examples": [text, ...]}}."""
    cands: dict[str, dict] = {}
    cur = con.execute(
        "SELECT p.message_id, p.text, p.links, c.is_ai "
        "FROM posts p LEFT JOIN classified c ON c.post_id = p.id"
    )
    scanned = 0
    for message_id, text, links, is_ai in cur:
        handles = bc.extract_telegram_handles(text, links)
        if not handles:
            continue
        scanned += len(handles)
        example = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
        for h in handles:
            if not h:
                continue
            cur_c = cands.setdefault(h, {"mentions": 0, "ai_hint": False, "examples": []})
            cur_c["mentions"] += 1  # один пост = одно упоминание
            if is_ai == 1:
                cur_c["ai_hint"] = True
            if example and len(cur_c["examples"]) < 2:
                cur_c["examples"].append(example)
    return cands, scanned


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
        "imported": 0, "skipped_existing": 0, "skipped_filter": 0,
        "skipped_limit": 0, "dry": bool(args.dry), "source": SOURCE,
        "scanned_handles": 0,
    }
    try:
        cfg = bc.load_config(args.config)
        con = db.connect(args.db)
        try:
            candidates, scanned = scan_posts(con)
            stats = bc.import_candidates(
                con, candidates,
                source=SOURCE, dry=args.dry,
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
    result["scanned_handles"] = scanned
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
