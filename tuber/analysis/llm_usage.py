"""Отчёт расхода LLM по единому журналу ``llm_usage`` (P0-3).

Единственный источник истины — таблица ``llm_usage`` единого ядра. Отчёт НЕ
склеивает её ни с ``classify_daily`` (суточные предохранители), ни с внешним
jsonl ``/root/.hermes/logs/deepseek_usage.jsonl`` (внешний след для аудита):
склейка давала бы двойной счёт (см. ТЗ P0-3).

Разбивка — по ``platform`` и ``stage`` за окно (по умолчанию всё время). В
выводе отдельно печатается дата начала учёта (``MIN(created_at)``), чтобы
неполная история не выдавалась за полную.

CLI::

    python3 -m tuber usage [--db PATH] [--days N] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from tuber import config
from tuber.core import db as _db
from tuber.core import timeutil


def _db_path(args) -> str:
    if getattr(args, "db", None):
        return args.db
    return os.environ.get("TUBER_DB") or config.db_path()


def _connect(path: str):
    # Отчёт только читает; схему ядра не мигрируем.
    return _db.connect(path, readonly=True)


def summary(conn, *, days: int | None = None, now=None) -> dict:
    """Свод расхода строго по ``llm_usage``.

    ``days`` — окно в сутках (None = всё время). Возвращает dict с полями
    ``rows``, ``by_platform``, ``total``, ``first_created_at`` (начало учёта),
    ``window_start`` и ``days``.
    """
    now_iso = timeutil.iso_now() if now is None else timeutil.parse_any(now)
    window_start = None
    if days:
        from datetime import datetime, timedelta, timezone
        dt = datetime.strptime(now_iso, timeutil.ISO_FMT).replace(
            tzinfo=timezone.utc) - timedelta(days=int(days))
        window_start = dt.strftime(timeutil.ISO_FMT)

    where = ""
    params: list = []
    if window_start:
        where = "WHERE created_at >= ?"
        params.append(window_start)

    rows = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT platform, stage, COUNT(*) AS calls,
                   COALESCE(SUM(tokens_in), 0) AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd
            FROM llm_usage
            {where}
            GROUP BY platform, stage
            ORDER BY cost_usd DESC, platform, stage
            """,
            params,
        ).fetchall()
    ]

    by_platform: dict[str, dict] = {}
    for r in rows:
        agg = by_platform.setdefault(
            r["platform"] or "(пусто)",
            {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
        )
        agg["calls"] += int(r["calls"])
        agg["tokens_in"] += int(r["tokens_in"])
        agg["tokens_out"] += int(r["tokens_out"])
        agg["cost_usd"] += float(r["cost_usd"])

    total = {
        "calls": sum(int(r["calls"]) for r in rows),
        "tokens_in": sum(int(r["tokens_in"]) for r in rows),
        "tokens_out": sum(int(r["tokens_out"]) for r in rows),
        "cost_usd": sum(float(r["cost_usd"]) for r in rows),
    }
    first = conn.execute("SELECT MIN(created_at) FROM llm_usage").fetchone()
    return {
        "rows": rows,
        "by_platform": by_platform,
        "total": total,
        "first_created_at": first[0] if first else None,
        "window_start": window_start,
        "days": days,
    }


def _fmt_usd(value: float) -> str:
    return f"${value:.4f}"


def _print(report: dict) -> None:
    total = report["total"]
    print("Расход LLM (источник: llm_usage, только ядро)")
    print(f"  начало учёта: {report['first_created_at'] or '(нет данных)'}")
    if report["window_start"]:
        print(f"  окно: последние {report['days']} сут (с {report['window_start']})")
    else:
        print("  окно: всё время")
    print()
    print(f"  {'platform':<12} {'stage':<16} {'вызовов':>8} "
          f"{'токенов вход':>13} {'токенов выход':>14} {'сумма':>11}")
    for r in report["rows"]:
        print(f"  {r['platform'] or '(пусто)':<12} {r['stage'] or '(пусто)':<16} "
              f"{int(r['calls']):>8} {int(r['tokens_in']):>13} "
              f"{int(r['tokens_out']):>14} {_fmt_usd(float(r['cost_usd'])):>11}")
    print()
    print("  по платформам:")
    for platform, agg in sorted(report["by_platform"].items(),
                                key=lambda kv: -kv[1]["cost_usd"]):
        print(f"    {platform:<12} {int(agg['calls']):>7} вызовов, "
              f"{_fmt_usd(float(agg['cost_usd']))}")
    print()
    print(f"  ИТОГО: {int(total['calls'])} вызовов, "
          f"{int(total['tokens_in'])} токенов вход, "
          f"{int(total['tokens_out'])} токенов выход, "
          f"{_fmt_usd(float(total['cost_usd']))}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tuber usage",
                                description="Расход LLM по журналу llm_usage")
    p.add_argument("--db", help="путь к единой базе")
    p.add_argument("--days", type=int, default=None, help="окно в сутках")
    p.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "usage":
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    path = _db_path(args)
    if not os.path.exists(path):
        print(f"нет базы: {path}", file=sys.stderr)
        return 1
    conn = _connect(path)
    try:
        report = summary(conn, days=args.days)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
