"""Оценка качества сквозных сюжетов (ТЗ «сквозной сюжет»).

Скрипт НЕ ходит в сеть: читает готовую базу (по умолчанию — копию боевой),
перечисляет КРОСС-ПЛАТФОРМЕННЫЕ пары участников сквозных сюжетов и (если задан
файл разметки) считает честные precision/recall по паре постов.

Разметка — JSONL, по строке на пару::

    {"a_id": 123, "b_id": 456, "verdict": "same", "note": "почему"}

``verdict``: ``same`` — это одно событие, ``different`` — разные. Пары, которых
в предсказании нет (посты не в одном сюжете), тоже можно размечать — тогда
recall считается по ним.

Запуск::

    # выгрузить предсказанные пары (>=20, включая кросс-платформенные)
    python3 scripts/x_cross_story_eval.py --db /root/tz5/cross_test.db --dump pairs.jsonl

    # посчитать метрики по разметке
    python3 scripts/x_cross_story_eval.py --db /root/tz5/cross_test.db \
        --labels docs/cross-story-labels.jsonl
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _connect(path):
    from tuber.core import db
    return db.connect(path, readonly=True)


def predicted_pairs(con):
    """Все кросс-платформенные пары внутри сквозных сюжетов.

    Возвращает ``{(a_id, b_id): story_id}`` (a_id < b_id).
    """
    rows = con.execute(
        "SELECT sm.story_id, sm.content_id, c.platform"
        " FROM story_member sm JOIN content c ON c.id = sm.content_id"
        " JOIN story st ON st.id = sm.story_id"
        " WHERE st.platform = 'cross'").fetchall()
    by_story: dict[int, list] = {}
    for r in rows:
        by_story.setdefault(r["story_id"], []).append((r["content_id"], r["platform"]))
    out = {}
    for sid, members in by_story.items():
        for (ai, ap), (bi, bp) in itertools.combinations(members, 2):
            if ap == bp:
                continue
            out[(min(ai, bi), max(ai, bi))] = sid
    return out


def dump_pairs(con, path):
    pairs = predicted_pairs(con)
    lines = []
    for (a, b), sid in sorted(pairs.items()):
        ra = con.execute(
            "SELECT platform, external_id, text, title, published_at FROM content"
            " WHERE id=?", (a,)).fetchone()
        rb = con.execute(
            "SELECT platform, external_id, text, title, published_at FROM content"
            " WHERE id=?", (b,)).fetchone()
        lines.append(json.dumps({
            "story_id": sid, "a_id": a, "b_id": b,
            "a_platform": ra["platform"], "b_platform": rb["platform"],
            "a_ext": ra["external_id"], "b_ext": rb["external_id"],
            "a_text": " ".join(((ra["title"] or "") + " " + (ra["text"] or "")).split())[:300],
            "b_text": " ".join(((rb["title"] or "") + " " + (rb["text"] or "")).split())[:300],
        }, ensure_ascii=False))
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"предсказанных кросс-платформенных пар: {len(pairs)} -> {path}")


def evaluate(con, labels_path: str) -> dict:
    pred = predicted_pairs(con)
    labels = []
    for line in Path(labels_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            labels.append(json.loads(line))
    tp = fp = fn = tn = 0
    rows = []
    for lab in labels:
        a, b = int(lab["a_id"]), int(lab["b_id"])
        pair = (min(a, b), max(a, b))
        is_pred = pair in pred
        is_true = lab["verdict"] == "same"
        if is_pred and is_true:
            tp += 1
        elif is_pred and not is_true:
            fp += 1
        elif not is_pred and is_true:
            fn += 1
        else:
            tn += 1
        rows.append((pair, lab["verdict"], is_pred))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    result = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "n": len(labels),
              "precision": round(prec, 4), "recall": round(rec, 4),
              "f1": round(f1, 4)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="оценка сквозных сюжетов")
    ap.add_argument("--db", required=True)
    ap.add_argument("--dump", default=None, help="выгрузить пары в JSONL")
    ap.add_argument("--labels", default=None, help="разметка пар (JSONL)")
    args = ap.parse_args(argv)
    con = _connect(args.db)
    try:
        if args.dump:
            dump_pairs(con, args.dump)
        if args.labels:
            evaluate(con, args.labels)
        if not args.dump and not args.labels:
            pairs = predicted_pairs(con)
            by_story = {}
            for pair, sid in pairs.items():
                by_story.setdefault(sid, 0)
                by_story[sid] += 1
            print(f"сквозных сюжетов с кросс-платформенными парами: {len(by_story)}")
            print(f"кросс-платформенных пар: {len(pairs)}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
