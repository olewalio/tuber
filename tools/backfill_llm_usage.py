#!/usr/bin/env python3
"""Перенос исторического расхода DeepSeek из jsonl в единый журнал ``llm_usage``.

Контекст (ТЗ P0-3). До этой правки расход X/Telegram лежал только во внешнем
jsonl ``/root/.hermes/logs/deepseek_usage.jsonl`` (плюс ``classify_daily`` для
суточных предохранителей), а ``llm_usage`` наполнялся только YouTube. Чтобы
журнал расхода стал непрерывным, этот инструмент дописывает исторические
записи X/Telegram в ``llm_usage``.

Правила:

* читаются ТОЛЬКО записи со скриптом ``tuber_*`` (``tuber_x_classify``,
  ``tuber_tg_classify``, ``tuber_tg_ads``). YouTube в jsonl отсутствует, поэтому
  пересечения с уже перенесёнными строками нет и двойного счёта не будет;
* сопоставление скрипт → (platform, stage)::

      tuber_x_classify  -> (x,        classify)
      tuber_tg_classify -> (telegram, classify)
      tuber_tg_ads      -> (telegram, ads)

* идемпотентность: ключ переноса (script + метка времени + токены + стоимость +
  порядковый номер одинаковой записи) хранится в ``legacy_map``; повторный
  ``--apply`` не создаёт дублей;
* по умолчанию ``--dry-run`` — только печатает, сколько строк и на какую сумму
  добавит; запись выполняется ТОЛЬКО с ``--apply``;
* перед записью проверяется, что сборщики стоят: нет процессов
  ``tuber ... collect`` и ``data/.lock`` не удерживается (``fcntl.flock``);
  иначе инструмент отказывает и ничего не пишет;
* после ``--apply`` печатается контроль: сколько строк стало и сумма по
  ``llm_usage``.

Запуск::

    python3 tools/backfill_llm_usage.py --dry-run
    python3 tools/backfill_llm_usage.py --apply

Опции: ``--jsonl PATH`` (по умолчанию ``/root/.hermes/logs/deepseek_usage.jsonl``),
``--db PATH``, ``--lock PATH``, ``--since ISO`` / ``--until ISO`` (окно; ``--until``
отсекает вызовы, которые новый писатель уже положил в ``llm_usage``).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tuber import config  # noqa: E402
from tuber.core import db as core_db  # noqa: E402
from tuber.core import schema as core_schema  # noqa: E402
from tuber.core import storage, timeutil  # noqa: E402

DEFAULT_JSONL = "/root/.hermes/logs/deepseek_usage.jsonl"

SCRIPT_MAP = {
    "tuber_x_classify": ("x", "classify"),
    "tuber_tg_classify": ("telegram", "classify"),
    "tuber_tg_ads": ("telegram", "ads"),
}

# Пространство ключей переноса в legacy_map.
LEDGER_DB = "deepseek_usage.jsonl"
LEDGER_TABLE = "llm_usage_backfill"


def _db_path(args) -> str:
    if getattr(args, "db", None):
        return args.db
    return os.environ.get("TUBER_DB") or config.db_path()


def _lock_path(args, db_path: str) -> str:
    if getattr(args, "lock", None):
        return args.lock
    return str(Path(db_path).resolve().parent / ".lock")


def read_candidates(jsonl_path: str, *, since=None, until=None) -> list[dict]:
    """Прочитать jsonl и вернуть пригодные к переносу записи.

    Каждая запись — dict с ключом переноса ``key`` и полями для ``llm_usage``.
    Дубликаты (одинаковые script/ts/токены/стоимость) различаются порядковым
    номером, чтобы ни одна реальная строка не потерялась.

    ``since``/``until`` (ISO-время) ограничивают окно. Это важно, потому что
    после выката писателя в ``llm_usage`` новые вызовы X/Telegram попадают и в
    jsonl, и в ядро; полный перенос истории без окна задвоил бы эту часть.
    """
    since_iso = timeutil.parse_any(since) if since else None
    until_iso = timeutil.parse_any(until) if until else None
    result: list[dict] = []
    seen: dict[tuple, int] = {}
    if not os.path.exists(jsonl_path):
        return result
    with open(jsonl_path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            script = rec.get("script")
            if script not in SCRIPT_MAP:
                continue
            ts = rec.get("ts") or ""
            created_at = timeutil.parse_any(ts) or ts
            if since_iso and created_at and created_at < since_iso:
                continue
            if until_iso and created_at and created_at > until_iso:
                continue
            platform, stage = SCRIPT_MAP[script]
            pt = int(rec.get("prompt_tokens") or 0)
            ct = int(rec.get("completion_tokens") or 0)
            cr = int(rec.get("cache_read_tokens") or 0)
            cost = float(rec.get("cost_usd") or 0.0)
            base = (script, ts, pt, ct, cr, round(cost, 6))
            n = seen.get(base, 0) + 1
            seen[base] = n
            key = "|".join(str(x) for x in (*base, n))
            result.append({
                "key": key,
                "platform": platform,
                "stage": stage,
                "model": rec.get("model"),
                "tokens_in": pt,
                "tokens_out": ct,
                "cost_usd": cost,
                "created_at": created_at,
                "script": script,
            })
    return result


def plan(conn, candidates: list[dict]) -> list[dict]:
    """Отфильтровать уже перенесённые записи (по ключу в legacy_map)."""
    pending = []
    for rec in candidates:
        if storage.get_legacy_map(conn, LEDGER_DB, LEDGER_TABLE, rec["key"]) is None:
            pending.append(rec)
    return pending


def _collector_reason(line: str) -> str | None:
    """Вернуть причину, если строка процесса — реальный сборщик.

    Признак ТОЧНЫЙ: смотрим на argv процесса, а не ищем подстроку по всей
    командной строке. Иначе процесс агента, в аргументах которого лежит текст
    задания со словами ``tuber``/``collect`` (например, ТЗ в argv jCode),
    ложно блокировал перенос (ТЗ: предохранитель ловил чужие процессы).

    Сборщик — это либо ``python… -m tuber <platform> collect …``
    (argv[0] — интерпретатор, argv[1] — ``-m``, argv[2] — ``tuber``,
    ``collect`` — отдельный аргумент), либо запущенный файл-обёртка
    ``tuber_*_collect*.sh`` как argv[0].
    """
    import re
    import shlex
    try:
        argv = shlex.split(line)
    except ValueError:
        argv = line.split()
    if not argv:
        return None
    exe = os.path.basename(argv[0])
    # 1) CLI контура: python[3[.x]] -m tuber <platform> collect ...
    if re.fullmatch(r"python[0-9.]*", exe):
        if len(argv) >= 4 and argv[1] == "-m" and argv[2] == "tuber":
            if "collect" in argv[3:]:
                return f"найден процесс сборщика: {line.strip()[:160]}"
    # 2) файл-обёртка сборщика как argv[0].
    if re.fullmatch(r"tuber[\w/.-]*_collect[\w.-]*\.sh", exe):
        return f"найден процесс-обёртка сборщика: {line.strip()[:160]}"
    return None


def collectors_running(lock_path: str) -> tuple[bool, str]:
    """Проверить, стоят ли сборщики. Возвращает (running, причина)."""
    # 1. Процессы `tuber ... collect`.
    try:
        out = subprocess.run(
            ["ps", "-eo", "args="], capture_output=True, text=True, timeout=10)
        for line in out.stdout.splitlines():
            reason = _collector_reason(line)
            if reason:
                return True, reason
    except Exception:  # noqa: BLE001 — ps недоступен: не блокируем по этой причине
        pass

    # 2. Файловый замок data/.lock (его берут сборщики YouTube, fcntl.flock).
    import fcntl
    lock_path = str(lock_path)
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True, f"замок {lock_path} удерживается сборщиком"
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fh.close()
    return False, ""


def _summarize(rows: list[dict]) -> dict:
    by_script: dict[str, dict] = {}
    for r in rows:
        agg = by_script.setdefault(r["script"], {"rows": 0, "cost_usd": 0.0})
        agg["rows"] += 1
        agg["cost_usd"] += float(r["cost_usd"])
    return {
        "rows": len(rows),
        "cost_usd": sum(float(r["cost_usd"]) for r in rows),
        "by_script": by_script,
    }


def apply_pending(conn, pending: list[dict]) -> int:
    """Записать строки в llm_usage и отметить ключи в legacy_map."""
    inserted = 0
    for rec in pending:
        new_id = storage.add_llm_usage(
            conn, rec["platform"], stage=rec["stage"], model=rec["model"],
            tokens_in=rec["tokens_in"], tokens_out=rec["tokens_out"],
            cost_usd=rec["cost_usd"], created_at=rec["created_at"])
        storage.set_legacy_map(
            conn, LEDGER_DB, LEDGER_TABLE, rec["key"],
            "llm_usage", new_id)
        inserted += 1
    conn.commit()
    return inserted


def _connect(path: str, *, readonly: bool = False):
    if readonly:
        # dry-run ничего не пишет: схему ядра не мигрируем.
        return core_db.connect(path, readonly=True)
    conn = core_db.connect(path)
    core_schema.migrate_schema(conn)
    return conn


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill_llm_usage",
        description="Перенос расхода X/Telegram из deepseek jsonl в llm_usage")
    p.add_argument("--jsonl", default=DEFAULT_JSONL, help="путь к jsonl расхода")
    p.add_argument("--db", help="путь к единой базе")
    p.add_argument("--lock", help="путь к файлу-замку сборщиков")
    p.add_argument("--since", help="переносить записи не раньше этого ISO-времени")
    p.add_argument("--until", help="переносить записи не позже этого ISO-времени "
                                  "(отсекает уже записанные новым кодом вызовы)")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="только показать план (по умолчанию)")
    p.add_argument("--apply", dest="apply", action="store_true",
                   help="записать строки в llm_usage")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = _db_path(args)
    lock_path = _lock_path(args, db_path)

    candidates = read_candidates(args.jsonl, since=args.since, until=args.until)
    if not candidates:
        print(f"в {args.jsonl} нет записей tuber_* — нечего переносить")
        return 0

    # Предупреждение о возможном пересечении: если ядро уже содержит строки
    # X/Telegram, значит писатель выкачен и часть jsonl уже лежит в llm_usage.
    if not args.until:
        ro = core_db.connect(db_path, readonly=True)
        try:
            overlap = ro.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE platform IN ('x','telegram')"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001 — базы/таблицы может ещё не быть
            overlap = 0
        finally:
            ro.close()
        if overlap:
            print(f"ВНИМАНИЕ: в llm_usage уже {overlap} строк X/Telegram — новый "
                  "писатель активен; задайте --until до времени выката, иначе "
                  "эти вызовы будут задвоены.", file=sys.stderr)

    # Проверка сборщиков ДО любого открытия базы: при --apply схема может
    # мигрироваться, а это уже запись. Отказываем раньше, ничего не трогая.
    if args.apply:
        running, reason = collectors_running(lock_path)
        if running:
            print(f"ОТКАЗ: сборщики не стоят — {reason}", file=sys.stderr)
            print("остановите сборщики и повторите --apply", file=sys.stderr)
            return 1

    conn = _connect(db_path, readonly=not args.apply)
    try:
        pending = plan(conn, candidates)
        stats = _summarize(pending)
        total_before = conn.execute(
            "SELECT COUNT(*) FROM llm_usage").fetchone()[0]

        print(f"jsonl: {args.jsonl}")
        print(f"база:  {db_path}")
        print(f"записей tuber_* в jsonl: {len(candidates)}")
        print(f"уже перенесено (ключей в legacy_map): "
              f"{len(candidates) - len(pending)}")
        print(f"к переносу: {stats['rows']} строк, "
              f"сумма ${stats['cost_usd']:.4f}")
        for script, agg in sorted(stats["by_script"].items()):
            print(f"    {script:<20} {agg['rows']:>6} строк, "
                  f"${agg['cost_usd']:.4f}")

        if not args.apply:
            print("\ndry-run: запись не выполнялась (для записи добавьте --apply)")
            return 0

        if not pending:
            print("\nвсе записи уже перенесены, дублей не создаём")
        else:
            inserted = apply_pending(conn, pending)
            print(f"\nперенесено строк: {inserted}")

        total_after = conn.execute(
            "SELECT COUNT(*) FROM llm_usage").fetchone()[0]
        total_sum = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage").fetchone()[0]
        print(f"контроль llm_usage: строк {total_before} -> {total_after}, "
              f"сумма ${float(total_sum):.4f}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
