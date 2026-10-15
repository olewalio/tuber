#!/usr/bin/env python3
"""Сборка отчёта QC по разметке tuber.db из сырого JSON-файла.

Вход: файл, который пишет `qc_classify_sample.py` (список записей с полями
db_topic / fresh_topic / agree / db_conf / snippet / url).

Выход: markdown-отчёт `/root/tuber/reports/qc-<дата>.md`.

Запуск:
    python3 tools/audit-scripts/qc_report.py \
        --raw /root/tuber/reports/qc-2026-10-02-raw.json \
        --out /root/tuber/reports/qc-2026-10-02.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PLATFORM_RU = {"telegram": "Telegram", "x": "X (Twitter)", "youtube": "YouTube"}


def pct(a: int, b: int) -> str:
    return f"{(100.0 * a / b):.1f}%" if b else "n/a"


def _db_context(db: str):
    """Версия промпта и период разметки по платформам (только чтение)."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        return list(con.execute(
            "SELECT platform, prompt_ver, COUNT(*), "
            "MIN(substr(classified_at,1,10)), MAX(substr(classified_at,1,10)) "
            "FROM classification WHERE topic IS NOT NULL "
            "GROUP BY platform, prompt_ver ORDER BY platform"))
    except Exception:  # noqa: BLE001
        return []


def worst_key(r: dict):
    """Чем меньше ключ — тем «хуже» пример. Сначала расхождения is_ai,
    затем низкая уверенность боевой разметки, затем более короткий текст."""
    is_ai_bad = 0 if r.get("ai_agree") is False else 1
    conf = r.get("db_conf")
    conf = conf if isinstance(conf, (int, float)) else 1.0
    return (is_ai_bad, conf, len(r.get("snippet") or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    data = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    res = data["results"]
    meta = data.get("meta", {})

    total = len(res)
    agree = [r for r in res if r.get("agree") is True]
    disc = [r for r in res if r.get("agree") is False]
    undef = [r for r in res if r.get("agree") is None]
    n_ai = [r for r in res if r.get("ai_agree") is False]

    L: list[str] = []
    L.append(f"# QC-сверка разметки tuber.db — {datetime.now().strftime('%d.%m.%Y')}")
    L.append("")
    L.append("Проверка: случайные записи боевого корпуса сравниваются с независимым "
             "прогоном DeepSeek ТЕМ ЖЕ промптом, что использует конвейер.")
    L.append("")
    L.append(f"- База: `{data.get('db')}` (открыта только на чтение)")
    L.append(f"- Объём выборки: {total} записей, по {data.get('per_platform')} на платформу")
    L.append(f"- Дата прогона: {data.get('generated_at')}")
    L.append(f"- Промпт и разбор ответа: импортируются из боевых модулей "
             f"`tuber/platforms/<platform>/classify.py` (не переписаны)")
    L.append("")

    L.append("## Итог")
    L.append("")
    L.append(f"- Совпало тема-в-тема: {len(agree)} из {total} ({pct(len(agree), total)})")
    L.append(f"- Расхождений темы: {len(disc)} из {total} ({pct(len(disc), total)})")
    L.append(f"- Не разобрано моделью: {len(undef)}")
    L.append(f"- Дополнительно расхождений по признаку is_ai: {len(n_ai)}")
    L.append("")
    L.append("**Доля ошибок разметки: "
             f"{pct(len(disc), total)}** (расхождение темы в независимом прогоне).")
    L.append("")

    L.append("## По платформам")
    L.append("")
    L.append("| Платформа | Выборка | Совпало | Расхождений | Доля ошибок |")
    L.append("|---|---|---|---|---|")
    for pf in ("telegram", "x", "youtube"):
        m = meta.get(pf)
        if not m:
            continue
        n = m["n"]
        L.append(f"| {PLATFORM_RU.get(pf, pf)} | {n} | {m['agree']} | "
                 f"{m['disagree']} | {pct(m['disagree'], n)} |")
    L.append("")

    pairs = Counter((r.get("db_topic"), r.get("fresh_topic")) for r in disc)
    if pairs:
        L.append("## Частые пары расхождений (боевая тема -> независимая тема)")
        L.append("")
        L.append("| Повторов | Боевая тема | Независимая тема |")
        L.append("|---|---|---|")
        for (a, b), c in pairs.most_common(15):
            L.append(f"| {c} | {a} | {b} |")
        L.append("")

    worst = sorted(disc, key=worst_key)[:args.top]
    L.append(f"## {len(worst)} худших примеров")
    L.append("")
    for i, r in enumerate(worst, 1):
        L.append(f"**{i}. [{r.get('platform')}] id={r.get('key')}** "
                 f"(уверенность боевой разметки: {r.get('db_conf')})")
        L.append(f"- Боевая тема: {r.get('db_topic')}"
                 + (f" / подтема: {r.get('db_subtopic')}" if r.get('db_subtopic') else ""))
        L.append(f"- Независимо: {r.get('fresh_topic')}")
        if r.get("ai_agree") is False:
            L.append(f"- Расхождение is_ai: боевое={r.get('db_is_ai')}, "
                     f"независимо={r.get('fresh_is_ai')}")
        if r.get("db_title"):
            L.append(f"- Боевой заголовок: {r.get('db_title')}")
        if r.get("fresh_title"):
            L.append(f"- Независимый заголовок: {r.get('fresh_title')}")
        L.append(f"- Текст: {r.get('snippet')}")
        if r.get("url"):
            L.append(f"- Ссылка: {r.get('url')}")
        L.append("")

    L.append("## Как читать результат")
    L.append("")
    L.append("Расхождение не всегда означает ошибку конвейера: у части тем таксономия "
             "пересекается (например «кейсы внедрения» и «скандалы и риски» в одном "
             "посте, посвящённом провалу внедрения). Но стабильная доля расхождений "
             "порядка четверти выборки означает низкую воспроизводимость разметки: один "
             "и тот же текст классифицируется по-разному в двух прогонах.")
    L.append("")

    ctx = _db_context(data.get("db"))
    if ctx:
        L.append("## Контекст боевой разметки (версия промпта и даты)")
        L.append("")
        L.append("| Платформа | Версия промпта | Записей | Период разметки |")
        L.append("|---|---|---|---|")
        for pf, ver, n, lo, hi in ctx:
            L.append(f"| {PLATFORM_RU.get(pf, pf)} | {ver or 'не записана'} | {n} | "
                     f"{lo} — {hi} |")
        L.append("")
        L.append("Если версия промпта в базе не записана, нельзя утверждать, что "
                 "сверка шла тем же промптом, — часть расхождений может быть дрейфом "
                 "промпта между прогонами. Там, где версия совпадает (Telegram, "
                 "`tg-v1`), расхождения означают именно невоспроизводимость одного и "
                 "того же промпта.")
        L.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print("записано:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
