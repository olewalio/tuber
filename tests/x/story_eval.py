"""Замер качества кластеризации сюжетов на размеченном наборе пар.

Набор и снимок корпуса лежат в `tests/data/` и пересобираются командой
`python3 scripts/build_story_pairs.py`. Модуль не ходит в сеть и в БД: работает
по снимку, поэтому тест и разовые замеры дают одинаковые числа.

Положительный класс — пара «одно событие». Считаем precision/recall/F1 по паре
постов: предсказание — попали ли оба поста в один кластер.
"""
from __future__ import annotations

import json
import os

from tuber.platforms.x import config, stories

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WINDOW_PATH = os.path.join(DATA_DIR, "story_window.jsonl")
PAIRS_PATH = os.path.join(DATA_DIR, "story_pairs.jsonl")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_window(path=None):
    return _load(path or WINDOW_PATH)


def load_pairs(path=None):
    return _load(path or PAIRS_PATH)


def cluster_index(posts, **kwargs):
    """tweet_id -> номер кластера (по новому правилу)."""
    clusters = stories.cluster_posts(posts, **kwargs)
    idx = {}
    for i, members in enumerate(clusters):
        for p in members:
            idx[str(p["tweet_id"])] = i
    return idx


def _metrics(rows):
    tp = sum(1 for r in rows if r["pred"] and r["truth"])
    fp = sum(1 for r in rows if r["pred"] and not r["truth"])
    fn = sum(1 for r in rows if not r["pred"] and r["truth"])
    tn = sum(1 for r in rows if not r["pred"] and not r["truth"])
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4), "n": len(rows)}


def evaluate(posts, pairs, **kwargs):
    """Метрики нового правила. Возвращает (сводка, строки по парам)."""
    idx = cluster_index(posts, **kwargs)
    rows = []
    for p in pairs:
        a, b = p["a"]["tweet_id"], p["b"]["tweet_id"]
        pred = idx.get(a) is not None and idx.get(a) == idx.get(b)
        rows.append({"a": a, "b": b, "label": p["label"], "note": p["note"],
                     "pred": pred, "truth": p["label"] == "same"})
    return _metrics(rows), rows


def evaluate_old(posts, pairs, simhash_threshold=None):
    """Метрики СТАРОГО правила (D-08): равенство наборов сущностей / simhash.

    Воспроизводит снятый код `cluster_posts`/`_same_story`: сравнение идёт
    ТОЛЬКО с постом-зачином кластера, окна времени нет. Оставлено для замера
    «до» и для регрессионного теста пояснения.
    """
    thr = (config.SIMHASH_THRESHOLD if simhash_threshold is None
           else int(simhash_threshold))
    items = sorted(posts, key=lambda p: (p["published_at_utc"] or "",
                                         str(p["tweet_id"])))
    clusters = []
    for p in items:
        ents = stories.extract_entities(p["text"], p["mentions"], p["links"])
        core = stories._core_entities(ents)
        sh = stories.simhash64(p["text"])
        placed = False
        for c in clusters:
            if (stories.hamming(sh, c["simhash"]) <= thr
                    or (ents and ents == c["entities"])
                    or (core and core == c["core"])):
                c["posts"].append(str(p["tweet_id"]))
                placed = True
                break
        if not placed:
            clusters.append({"posts": [str(p["tweet_id"])], "entities": ents,
                             "core": core, "simhash": sh})
    idx = {}
    for i, c in enumerate(clusters):
        for t in c["posts"]:
            idx[t] = i
    rows = []
    for p in pairs:
        a, b = str(p["a"]["tweet_id"]), str(p["b"]["tweet_id"])
        pred = idx.get(a) is not None and idx.get(a) == idx.get(b)
        rows.append({"a": a, "b": b, "label": p["label"], "note": p["note"],
                     "pred": pred, "truth": p["label"] == "same"})
    return _metrics(rows), rows
