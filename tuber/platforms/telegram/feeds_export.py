#!/usr/bin/env python3
"""ТЗ-17, часть C + ТЗ-21/A: экспорт фида кандидатов для tuber-x и tuber-os.

Из своих постов извлекает аккаунты X (kind="x") и ссылки на видео YouTube
(kind="youtube", handle = video_id). Формат строки — по договору
`docs/EXCHANGE-FEED.md` (канон — в tuber-os); source фиксирован
"tuber-telegram:posts". Пишет JSONL, сортировка по mentions desc.

ТЗ-21/A: поля приведены к канону —
  * `videos`  — ЧИСЛО (из скольких разных постов пришёл кандидат), не список;
  * `video_ids` — список строк: для своих YouTube-строк реальный id видео,
    для остальных `[]`;
  * `examples` — список СТРОК (цитаты), а не объектов;
  * `mentions` — число.

stdout — один JSON: kind_counts, written, out, top.

Пример:
  python3 -m tuber tg bridge export --out data/exchange/external_candidates.jsonl --dry
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import bridge as bc  # noqa: E402
from . import store as db  # noqa: E402

SOURCE = "tuber-telegram:posts"
MAX_EXAMPLES_EXPORT = 3


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Экспорт фида кандидатов X/YouTube (ТЗ-17/C)")
    ap.add_argument("--out", default=os.path.join(bc._config.EXCHANGE_DIR,
                                                  "external_candidates.jsonl"),
                    help="путь к выходному JSONL ('-' — не писать файл)")
    ap.add_argument("--limit", type=int, default=2000, help="максимум строк (по умолчанию 2000)")
    ap.add_argument("--dry", action="store_true", help="сводка без записи файла")
    ap.add_argument("--db", default=bc.DEFAULT_DB, help="путь к БД (по умолчанию рабочая)")
    return ap.parse_args(argv)


def _get(store: dict, handle: str) -> dict:
    return store.setdefault(handle, {
        "mentions": 0, "sources": set(), "posts": set(), "examples": [],
        "first_seen": None, "last_seen": None, "ai_hint": False,
    })


def _touch(rec: dict, date_utc, message_id, example, chan, is_ai):
    rec["mentions"] += 1
    # ТЗ-21/A: `videos` — число РАЗНЫХ постов, из которых пришёл кандидат.
    if message_id is not None:
        rec["posts"].add(message_id)
    if chan:
        rec["sources"].add(chan)
    if is_ai == 1:
        rec["ai_hint"] = True
    date_utc = bc.norm_ts(date_utc)
    if date_utc:
        if rec["first_seen"] is None or date_utc < rec["first_seen"]:
            rec["first_seen"] = date_utc
        if rec["last_seen"] is None or date_utc > rec["last_seen"]:
            rec["last_seen"] = date_utc
    if example and len(rec["examples"]) < MAX_EXAMPLES_EXPORT:
        # ТЗ-21/A: пример — строка-цитата (с префиксом message_id для трассировки).
        quote = f"{message_id}: {example}" if message_id is not None else example
        rec["examples"].append(bc.truncate(quote, bc.MAX_EXAMPLE_LEN))


def collect(con) -> tuple[dict, dict]:
    x_store: dict[str, dict] = {}
    yt_store: dict[str, dict] = {}
    cur = con.execute(
        "SELECT p.message_id, p.text, p.links, p.date_utc, c.handle, cls.is_ai "
        "FROM posts p JOIN channels c ON c.id = p.channel_id "
        "LEFT JOIN classified cls ON cls.post_id = p.id"
    )
    for message_id, text, links, date_utc, chan, is_ai in cur:
        xs = bc.extract_x_handles(text, links)
        ys = bc.extract_youtube_ids(text, links)
        if not xs and not ys:
            continue
        example = bc.truncate(text or "", 80)
        for h in xs:
            _touch(_get(x_store, h), date_utc, message_id, example, chan, is_ai)
        for vid in ys:
            _touch(_get(yt_store, vid), date_utc, message_id, example, chan, is_ai)
    return x_store, yt_store


def build_rows(kind: str, store: dict, exported_at: str) -> list[dict]:
    rows = []
    for handle, rec in sorted(store.items(), key=lambda kv: (-kv[1]["mentions"], kv[0])):
        rows.append({
            "kind": kind,
            "handle": handle,
            "mentions": rec["mentions"],
            # ТЗ-21/A: `videos` — число разных постов; для YouTube-строк — реальный
            # id видео в `video_ids`, для остальных — пустой список.
            "videos": len(rec["posts"]) or rec["mentions"],
            "video_ids": [handle] if kind == "youtube" else [],
            "sources": sorted(rec["sources"]),
            "ai_hint": 1 if rec["ai_hint"] else 0,
            "first_seen": rec["first_seen"],
            "last_seen": rec["last_seen"],
            "source": SOURCE,
            "examples": rec["examples"],
            "exported_at": exported_at,
        })
    return rows


def _store_candidates(con, x_store: dict, yt_store: dict) -> dict:
    """Записать найденные X/YouTube-кандидаты в каноническую таблицу ``candidate``.

    JSONL остаётся рабочим представлением обмена (ТЗ-4 §0), но канонический
    обмен единой базы идёт через ``candidate``; UPSERT по ``(platform, handle)``
    идемпотентен, поэтому повторный экспорт не размножает строки.
    """
    counts = {"inserted": 0, "updated": 0}
    for kind, store in (("x", x_store), ("youtube", yt_store)):
        for handle, rec in store.items():
            action = db.upsert_candidate(
                con, kind, handle, kind=kind, found_via=SOURCE,
                external_id=handle if kind == "youtube" else None,
                meta={"mentions": rec["mentions"],
                      "sources": sorted(rec["sources"]),
                      "ai_hint": bool(rec["ai_hint"]),
                      "examples": rec["examples"]},
                first_seen=bc.norm_ts(rec["first_seen"]),
                last_seen=bc.norm_ts(rec["last_seen"]))
            counts[action] += 1
    con.commit()
    return counts


def main(argv=None) -> int:
    args = parse_args(argv)
    exported_at = bc.utcnow_iso()
    candidates: dict[str, int] = {"inserted": 0, "updated": 0}
    try:
        con = db.connect(args.db)
        try:
            x_store, yt_store = collect(con)
            candidates = _store_candidates(con, x_store, yt_store)
        finally:
            con.close()
    except sqlite3.Error as exc:
        sys.stderr.write(f"ошибка БД: {exc}\n")
        return 2

    rows = build_rows("x", x_store, exported_at) + build_rows("youtube", yt_store, exported_at)
    rows.sort(key=lambda r: (-r["mentions"], r["kind"], r["handle"]))
    rows = rows[: max(0, args.limit)] if args.limit and args.limit > 0 else rows

    kind_counts: dict[str, int] = {}
    for r in rows:
        kind_counts[r["kind"]] = kind_counts.get(r["kind"], 0) + 1

    if not args.dry and args.out != "-":
        out_dir = os.path.dirname(args.out)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, args.out)

    result = {
        "kind_counts": kind_counts,
        "written": len(rows),
        "out": args.out,
        "dry": bool(args.dry),
        "candidates": candidates,
        "top": [{"kind": r["kind"], "handle": r["handle"], "mentions": r["mentions"]} for r in rows[:5]],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
