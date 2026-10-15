#!/usr/bin/env python3
"""QC сверки разметки `tuber.db` против независимого прогона DeepSeek.

Задача kanban t_5ca77dc6: взять случайные записи из корпуса, по каждой
сравнить (1) тему из БД и (2) свежую классификацию ТЕМ ЖЕ промптом, что
использует боевой конвейер, и выписать расхождения.

Ключевой принцип: промпт, разбор ответа и валидация берутся ИЗ БОЕВЫХ МОДУЛЕЙ
(`tuber/platforms/<platform>/classify.py`), а не переписываются. Меняется только
транспорт доставки (свой HTTP-вызов) и хранение результата (JSON-файл вместо БД).

База открывается ТОЛЬКО на чтение (`mode=ro`), никаких записей не делается.

Запуск:
    python3 tools/audit-scripts/qc_classify_sample.py --per-platform 100 \
        --out /root/tuber/reports/qc-2026-10-02.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path("/root/tuber")
sys.path.insert(0, str(REPO))

DB = os.environ.get("TUBER_DB", "/root/tuber/data/tuber.db")
ENV = "/root/.hermes/.env"
URL = "https://api.deepseek.com/chat/completions"


# --------------------------------------------------------------- окружение ---
def load_env(path: str = ENV) -> None:
    src = Path(path)
    if not src.exists():
        return
    for raw in src.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def connect_ro():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ------------------------------------------------------------------- вызов ---
import requests  # noqa: E402


def call_model(api_key: str, model: str, system: str, user: str, *,
               extra: dict | None = None, timeout: float = 120.0):
    body = {"model": model, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user}],
        "temperature": 0, "stream": False}
    if extra:
        body.update(extra)
    r = requests.post(URL, headers={"Authorization": f"Bearer {api_key}",
                                    "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    text = ""
    if data.get("choices"):
        text = (data["choices"][0].get("message") or {}).get("content") or ""
    return text, (data.get("usage") or {})


# ------------------------------------------------------------------ выборка ---
SQL = {
    "telegram": """
        SELECT cl.content_id, cl.topic AS db_topic, cl.is_ai AS db_is_ai,
               cl.confidence AS db_conf, cl.subtopic AS db_subtopic,
               cl.title_ru AS db_title, cl.classified_at,
               c.text, c.published_at, c.url, c.lang AS content_lang,
               s.handle AS handle, s.title AS channel_title
        FROM classification cl JOIN content c ON c.id = cl.content_id
        LEFT JOIN source s ON s.id = c.source_id
        WHERE cl.platform='telegram' AND cl.topic IS NOT NULL
          AND c.text IS NOT NULL AND length(trim(c.text)) > 0
        ORDER BY RANDOM() LIMIT ?""",
    "youtube": """
        SELECT cl.external_id AS video_id, cl.topic AS db_topic, cl.is_ai AS db_is_ai,
               cl.confidence AS db_conf, cl.subtopic AS db_subtopic,
               cl.title_ru AS db_title, cl.classified_at,
               c.title, c.text AS description, c.duration_seconds, c.url,
               c.lang AS content_lang, s.title AS channel_title
        FROM classification cl JOIN content c ON c.id = cl.content_id
        LEFT JOIN source s ON s.id = c.source_id
        WHERE cl.platform='youtube' AND cl.topic IS NOT NULL
          AND (c.title IS NOT NULL OR c.text IS NOT NULL)
        ORDER BY RANDOM() LIMIT ?""",
    "x": """
        SELECT cl.content_id, cl.external_id AS tweet_id, cl.topic AS db_topic,
               cl.is_ai AS db_is_ai, cl.confidence AS db_conf,
               cl.claim_type AS db_claim, cl.subtopic AS db_subtopic,
               cl.title_ru AS db_title, cl.classified_at,
               c.text, c.text_hash, c.lang, c.links, c.url, c.published_at,
               s.handle AS handle
        FROM classification cl JOIN content c ON c.id = cl.content_id
        LEFT JOIN source s ON s.id = c.source_id
        WHERE cl.platform='x' AND cl.topic IS NOT NULL
          AND c.text IS NOT NULL AND length(trim(c.text)) > 0
        ORDER BY RANDOM() LIMIT ?""",
}


def sample(con, platform: str, n: int):
    return [dict(r) for r in con.execute(SQL[platform], (n,))]


# ------------------------------------------------------------ реклассификация ---
def run_platform(platform: str, rows: list[dict], api_key: str, con, *, verbose=True):
    from tuber.platforms.telegram import classify as tgc
    from tuber.platforms.youtube import classify as ytc
    from tuber.platforms.x import classify as xc

    if platform == "telegram":
        batch, sysp, parse = tgc.BATCH_SIZE, tgc.SYSTEM_PROMPT, None
        model, extra = "deepseek-chat", None
    elif platform == "youtube":
        batch, sysp, parse = ytc.BATCH_SIZE, ytc.SYSTEM_PROMPT, None
        model, extra = "deepseek-chat", None
    else:
        batch, sysp, parse = xc.config.CLASSIFY_BATCH, xc._system_prompt(), None
        model = xc.config.DEEPSEEK_MODEL
        extra = {"max_tokens": xc.config.DEEPSEEK_MAX_TOKENS,
                 "thinking": xc.config.DEEPSEEK_THINKING,
                 "response_format": {"type": "json_object"}}

    recent = []
    if platform == "x":
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        recent = [{"topic": r["topic"], "subtopic": r["subtopic"],
                   "claim_type": r["claim_type"]}
                  for r in con.execute(
                      "SELECT topic, subtopic, claim_type FROM classification"
                      " WHERE platform='x' AND status='classified'"
                      " AND classified_at >= ? ORDER BY classified_at DESC LIMIT ?",
                      (since, xc.config.CLASSIFY_RECENT_CLAIMS))]

    rowkey = {"telegram": lambda r: str(r["content_id"]),
              "youtube": lambda r: str(r["video_id"]),
              "x": lambda r: str(r["tweet_id"])}[platform]

    results, total_cost, calls = [], 0.0, 0
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        if platform == "telegram":
            user = tgc.build_user_prompt(chunk)
        elif platform == "youtube":
            user = ytc.build_user_prompt(chunk)
        else:
            items = [{"tweet_id": r["tweet_id"], "text": r["text"],
                      "text_hash": r["text_hash"], "lang": r["lang"],
                      "links": r["links"], "handle": r["handle"]} for r in chunk]
            user = xc.build_user_prompt(items, recent=recent)

        parsed, err, raw = None, None, ""
        for attempt in range(2):
            try:
                raw, usage = call_model(api_key, model, sysp, user, extra=extra)
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                break
            calls += 1
            if platform == "telegram":
                parsed = tgc.parse_response(raw, [str(r["content_id"]) for r in chunk])
            elif platform == "youtube":
                parsed = ytc.parse_response(raw, [r["video_id"] for r in chunk])
            else:
                pmap, verr = xc.validate_response(raw, items)
                if pmap is not None:
                    parsed = {str(items[i - 1]["tweet_id"]): f
                              for i, f in pmap.items()}
                else:
                    err = verr
            if parsed:
                break
            if attempt == 0:
                user = user + ("\n\nПредыдущий ответ не прошёл проверку схемы"
                               f" ({err}). Верни строго JSON с объектом на каждый"
                               " элемент.")
        if not parsed:
            for r in chunk:
                results.append({"key": rowkey(r),
                                "db_topic": r["db_topic"], "fresh_topic": None,
                                "agree": None, "note": f"не разобрано: {err}"})
            if verbose:
                print(f"  [{platform}] батч не разобран: {err}")
            continue

        for r in chunk:
            key = rowkey(r)
            item = parsed.get(key)
            fresh_topic = item.get("topic") if item else None
            fresh_ai = item.get("is_ai") if item else None
            db_ai = r["db_is_ai"]
            db_topic = r["db_topic"]
            agree = (fresh_topic == db_topic)
            results.append({
                "key": key, "platform": platform,
                "db_topic": db_topic, "db_is_ai": db_ai,
                "db_conf": r.get("db_conf"), "db_claim": r.get("db_claim"),
                "db_title": r.get("db_title"), "db_subtopic": r.get("db_subtopic"),
                "db_classified_at": r.get("classified_at"),
                "fresh_topic": fresh_topic, "fresh_is_ai": fresh_ai,
                "fresh_conf": item.get("confidence") if item else None,
                "fresh_title": item.get("title_ru") if item else None,
                "agree": agree,
                "ai_agree": (int(fresh_ai) == int(db_ai)) if fresh_ai is not None
                            and db_ai is not None else None,
                "snippet": _snippet(r),
                "url": r.get("url"),
            })
    return results, calls, total_cost


def _snippet(r: dict) -> str:
    for f in ("text", "description", "title"):
        v = r.get(f)
        if v:
            return " ".join(str(v).split())[:400]
    return ""


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-platform", type=int, default=100)
    ap.add_argument("--platforms", default="telegram,x,youtube")
    ap.add_argument("--seed", type=int, default=20261002)
    ap.add_argument("--out", default="/root/tuber/reports/qc-sample-raw.json")
    args = ap.parse_args()

    load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("НЕТ DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    random.seed(args.seed)
    con = connect_ro()
    all_results, meta = [], {}
    for pf in [p.strip() for p in args.platforms.split(",") if p.strip()]:
        rows = sample(con, pf, args.per_platform)
        print(f"[{pf}] выбрано {len(rows)} записей")
        t0 = time.time()
        res, calls, _ = run_platform(pf, rows, api_key, con)
        agree = [r for r in res if r["agree"] is True]
        disagree = [r for r in res if r["agree"] is False]
        undef = [r for r in res if r["agree"] is None]
        meta[pf] = {"n": len(res), "agree": len(agree), "disagree": len(disagree),
                    "undef": len(undef), "calls": calls,
                    "seconds": round(time.time() - t0, 1)}
        print(f"[{pf}] совпало {len(agree)}, расхождений {len(disagree)}, "
              f"не разобрано {len(undef)}, вызовов {calls}, "
              f"{meta[pf]['seconds']}с")
        all_results.extend(res)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "db": DB, "per_platform": args.per_platform, "seed": args.seed,
         "meta": meta, "results": all_results},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print("записано:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
