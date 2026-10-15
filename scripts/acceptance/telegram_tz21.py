#!/usr/bin/env python3
"""Приёмка ТЗ-21 в монорепозитории: экспорт по канону, потребитель терпит оба вида.

Перенос `scripts/acceptance_tz21.py` проекта tuber-telegram (ТЗ-4). Проверки:

  1. копия договора docs/EXCHANGE-FEED.md есть и ссылается на канон tuber-os;
  2. РЕАЛЬНЫЙ экспорт (`python3 -m tuber tg bridge export`) в tmp соответствует
     канону: `videos` — ЧИСЛО, `examples` — строки, есть `video_ids`;
  3. потребитель терпит оба вида полей (`bridge.feed_to_candidates`);
  4. импорт из живого фида tuber-os не падает (`tuber tg bridge import --dry`);
  5. единая база не изменилась (COUNT source/content/candidate);
  6. pytest -q зелёный.

Запуск: python3 scripts/acceptance/telegram_tz21.py
Вывод:  docs/tg/acceptance-log-21.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tuber.platforms.telegram import bridge as bc  # noqa: E402
from tuber.platforms.telegram import store as db  # noqa: E402

FEED_OS = "/root/tuber-os/data/exchange/external_candidates.jsonl"
WORK_DB = os.path.join(ROOT, "data", "tuber.db")
CONFIG = os.path.join(ROOT, "config", "telegram", "bridge_sources.json")
LOG = os.path.join(ROOT, "docs", "tg", "acceptance-log-21.txt")

CANON = {
    "kind": str, "handle": str, "mentions": int, "videos": int, "video_ids": list,
    "sources": list, "source": str, "examples": list, "ai_hint": int,
    "first_seen": str, "last_seen": str,
}

_out = []
_results = []


def p(line=""):
    print(line)
    _out.append(str(line))


def hr(title):
    p("")
    p("=" * 78)
    p(title)
    p("=" * 78)


def check(num, what, ok, data):
    _results.append({"num": num, "what": what, "ok": bool(ok), "data": data})
    p(f"[{num:>2}] {'OK  ' if ok else 'FAIL'} {what}")
    p(f"      данные: {data}")


def _type_ok(value, typ):
    if typ is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, typ)


def canon_violations(rows):
    bad = []
    for i, row in enumerate(rows):
        for field, typ in CANON.items():
            if field not in row or row[field] is None:
                continue
            if not _type_ok(row[field], typ):
                bad.append((i, field, typ.__name__, type(row[field]).__name__))
        for field in ("video_ids", "sources", "examples"):
            for item in row.get(field) or []:
                if not isinstance(item, str):
                    bad.append((i, field, "list[str]", type(item).__name__))
    return bad


def build_db(path):
    """Синтетическая единая база: канал + два поста (через адаптер, как прод)."""
    con = db.init_db(path)
    con.execute("INSERT INTO channels(handle,status,source)"
                " VALUES('src_chan','active','test')")
    ch = con.execute("SELECT id FROM channels WHERE handle='src_chan'").fetchone()[0]
    for mid, date, text, links in (
        (100, "2026-09-10T10:00:00+00:00",
         "https://x.com/rohanpaul_ai/status/123 и https://youtu.be/dQw4w9WgXcQ", "[]"),
        (101, "2026-09-15T10:00:00+00:00", "ещё https://twitter.com/rohanpaul_ai", "[]"),
    ):
        con.execute("INSERT INTO posts(channel_id,message_id,date_utc,text,links)"
                    " VALUES(?,?,?,?,?)", (ch, mid, date, text, links))
    con.commit()
    con.close()
    return path


def counts(path):
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for t in ("source", "content", "candidate"):
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error as exc:
                out[t] = f"нет таблицы ({exc})"
        return out
    finally:
        con.close()


def main():
    started = datetime.now(timezone.utc)
    work = tempfile.mkdtemp(prefix="tuber_tg_accept21_", dir="/tmp")
    hr("ПРИЁМКА ТЗ-21 (Telegram в монорепозитории): экспорт по канону")
    p(f"начало:     {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"репозиторий: {ROOT}")
    p(f"рабочий каталог: {work}")

    before = counts(WORK_DB)
    p(f"единая база ДО (COUNT): {before}")

    hr("П.1. Копия договора")
    doc = os.path.join(ROOT, "docs", "EXCHANGE-FEED.md")
    text = open(doc, encoding="utf-8").read() if os.path.isfile(doc) else ""
    check(1, "docs/EXCHANGE-FEED.md есть, канон указан на tuber-os",
          os.path.isfile(doc) and "канон — в" in text.lower()
          and "tuber-os" in text and "video_ids" in text,
          f"файл={os.path.isfile(doc)}; упоминание канона={'канон — в' in text.lower()}")

    hr("П.2. Реальный экспорт в tmp: `videos` числом, `examples` строками")
    out = os.path.join(work, "external_candidates.jsonl")
    db_path = build_db(os.path.join(work, "src.db"))
    proc = subprocess.run(
        [sys.executable, "-m", "tuber", "tg", "bridge", "export",
         "--db", db_path, "--out", out],
        cwd=ROOT, capture_output=True, text=True)
    if proc.returncode == 0 and os.path.isfile(out):
        rows = [json.loads(line) for line in open(out, encoding="utf-8") if line.strip()]
        bad = canon_violations(rows)
        kinds = {}
        for r in rows:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        p(f"  строк={len(rows)}; kinds={kinds}")
        for r in rows:
            p(f"  пример {r['kind']}: videos={r['videos']!r}"
              f" ({type(r['videos']).__name__}), video_ids={r['video_ids']!r},"
              f" examples={r['examples'][:1]!r}")
        check(2, "реальный экспорт соответствует канону", not bad,
              f"строк={len(rows)}; нарушений={len(bad)}; первые={bad[:3]}")
    else:
        check(2, "реальный экспорт соответствует канону", False,
              f"rc={proc.returncode}; stderr={proc.stderr.strip()[:200]}")

    hr("П.3. Потребитель терпит оба вида полей")
    cfg = bc.load_config(CONFIG)
    legacy = [
        # старый вид: videos списком, examples объектами
        {"kind": "telegram", "handle": "legacy_chan", "mentions": 3,
         "videos": ["a1", "a2"], "examples": [{"message_id": 1, "text": "цитата"}]},
        # чужой вид: videos числом, examples строками, sources строкой
        {"kind": "telegram", "handle": "modern_chan", "mentions": 4, "videos": 2,
         "sources": "x, y", "examples": ["строка-цитата"]},
    ]
    cands = bc.feed_to_candidates(legacy, cfg)
    ok = (cands.get("legacy_chan", {}).get("mentions") == 3
          and cands.get("modern_chan", {}).get("mentions") == 4
          and cands["legacy_chan"]["examples"] == ["цитата"])
    check(3, "оба вида разобраны без исключения", ok,
          f"кандидаты={ {k: v['mentions'] for k, v in cands.items()} }")

    hr("П.4. Импорт из живого фида tuber-os (--dry, база не меняется)")
    if not os.path.isfile(FEED_OS):
        check(4, "живой фид tuber-os", False, "файла нет")
    else:
        proc = subprocess.run(
            [sys.executable, "-m", "tuber", "tg", "bridge", "import",
             "--feed", FEED_OS, "--db", WORK_DB, "--dry"],
            cwd=ROOT, capture_output=True, text=True)
        out_txt = (proc.stdout or "").strip()
        payload = json.loads(out_txt) if (proc.returncode == 0 and out_txt.startswith("{")) else None
        check(4, "импорт из живого фида не падает (предъявлен JSON)",
              payload is not None,
              f"rc={proc.returncode}; {json.dumps(payload, ensure_ascii=False)}"
              if payload else f"rc={proc.returncode}; stderr={proc.stderr.strip()[:200]}")

    hr("П.5. Единая база не изменилась")
    after = counts(WORK_DB)
    p(f"  ДО={before} ПОСЛЕ={after}")
    check(5, "COUNT ключевых таблиц совпал", before == after,
          f"совпало={before == after}")

    hr("П.6. pytest -q")
    proc_t = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                            capture_output=True, text=True)
    tail = (proc_t.stdout or "").strip().splitlines()[-1] if proc_t.stdout else ""
    check(6, "pytest -q зелёный", proc_t.returncode == 0,
          f"rc={proc_t.returncode}; {tail}")

    hr("СВОДКА")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"журнал сохранён: {LOG}")
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
