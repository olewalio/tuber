"""Кросс-платформенный сторож (часть 2 «волна 2»).

Сторож X смотрит только X. Здесь — три проверки по ВСЕЙ единой базе, которые
запускаются из того же крона (обёртка ``scripts/x/tuber_x_health.sh``):

* свежесть сбора по платформам (Telegram, YouTube): последний увиденный
  материал старше порога → ALERT;
* висящие прогоны: ``run.finished_at IS NULL`` и старт старее 3 ч → ALERT;
* просроченные замеры: ``metric_schedule.due_at`` старее 6 ч и ``done_at IS NULL``
  → ALERT с числом.

Формат вывода тот же, что у сторожа X: в норме молчит, печатает строки
``ALERT: ...``, всегда выходит с кодом 0. Алерты дедуплицируются общим модулем
:mod:`tuber.core.health_dedup` (файл ``data/health_all_seen.json``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from tuber import config as _config
from tuber.core import db as _db
from tuber.core import health_dedup
from tuber.core import storage, timeutil

#: Платформы и порог свежести сбора, часы. Telegram собирается каждые 30 мин —
#: 6 ч молчания это уже реальный сбой; YouTube наполняется суточным циклом и
#: импортом кандидатов — 30 ч допускает один пропущенный проход.
FRESHNESS_HOURS: dict[str, float] = {"telegram": 6.0, "youtube": 30.0}
#: Прогон, не закрывшийся за это время, считается зависшим, часы.
OPEN_RUN_HOURS = 3.0
#: Замер, просроченный сильнее этого, — ALERT, часы.
OVERDUE_MEASURE_HOURS = 6.0
#: Дедуп алертов: не чаще раза в окно; путь перекрывается переменной окружения.
HEALTH_SEEN_PATH = os.environ.get("TUBER_ALL_HEALTH_SEEN",
                                  "/root/tuber/data/health_all_seen.json")
DEDUP_WINDOW_HOURS = 6.0


def _res(name, alert, msg, *, severity="ALERT", **extra):
    out = {"name": name, "alert": bool(alert), "severity": severity, "msg": msg}
    out.update(extra)
    return out


def _to_dt(value) -> datetime | None:
    iso = timeutil.parse_any(value)
    if iso is None:
        return None
    return datetime.strptime(iso, timeutil.ISO_FMT).replace(tzinfo=timezone.utc)


def check_platform_freshness(con, platform: str, hours: float) -> dict:
    """Свежесть сбора платформы: последний материал старше ``hours`` → ALERT."""
    row = con.execute(
        "SELECT first_seen_at FROM content WHERE platform=?"
        " ORDER BY julianday(first_seen_at) DESC LIMIT 1", (platform,)).fetchone()
    last = _to_dt(row["first_seen_at"]) if row and row["first_seen_at"] else None
    name = f"freshness_{platform}"
    if last is None:
        return _res(name, True, f"{platform}: материалов в базе нет вообще",
                    value=None, platform=platform)
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    alert = age_h > float(hours)
    msg = (f"{platform}: последний материал {last:%Y-%m-%d %H:%M} UTC"
           f" ({age_h:.1f} ч назад, порог {hours:g} ч)")
    return _res(name, alert, msg, value=round(age_h, 2), platform=platform)


def check_open_runs(con, hours: float = OPEN_RUN_HOURS) -> dict:
    """Висящие прогоны: незакрытые и старше ``hours`` → ALERT с числом."""
    row = con.execute(
        "SELECT COUNT(*) AS n FROM run"
        " WHERE finished_at IS NULL AND started_at IS NOT NULL"
        "   AND julianday(started_at) < julianday('now', ?)",
        (f"-{float(hours)} hours",)).fetchone()
    n = int(row["n"] if row else 0)
    return _res("open_runs", n > 0,
                f"висящие прогоны: {n} строк run.finished_at IS NULL"
                f" старше {hours:g} ч — журнал прогонов не закрывается",
                value=n)


def check_overdue_measures(con, hours: float = OVERDUE_MEASURE_HOURS) -> dict:
    """Просроченные замеры очереди → ALERT с числом."""
    row = con.execute(
        "SELECT COUNT(*) AS n FROM metric_schedule"
        " WHERE done_at IS NULL AND julianday(due_at) < julianday('now', ?)",
        (f"-{float(hours)} hours",)).fetchone()
    n = int(row["n"] if row else 0)
    return _res("overdue_measures", n > 0,
                f"просроченные замеры: {n} строк metric_schedule с due_at"
                f" старше {hours:g} ч и done_at IS NULL",
                value=n)


def run_all(con, *, freshness_hours: dict | None = None,
            open_run_hours: float = OPEN_RUN_HOURS,
            overdue_hours: float = OVERDUE_MEASURE_HOURS) -> dict:
    """Прогнать все кросс-платформенные проверки; писать историю в ``run_log``."""
    thresholds = dict(FRESHNESS_HOURS if freshness_hours is None else freshness_hours)
    checks = [check_platform_freshness(con, platform, hours)
              for platform, hours in thresholds.items()]
    checks.append(check_open_runs(con, open_run_hours))
    checks.append(check_overdue_measures(con, overdue_hours))
    alerts = [c for c in checks if c["alert"] and c["severity"] == "ALERT"]
    warns = [c for c in checks if c["alert"] and c["severity"] != "ALERT"]
    ts = timeutil.iso_now()
    for c in checks:
        level = "ALERT" if c["alert"] and c["severity"] == "ALERT" else (
            "WARN" if c["alert"] else "INFO")
        try:
            storage.log_run(con, None, ts, level, None, f"health_all: {c['msg']}",
                            platform="all")
        except Exception:  # noqa: BLE001 — журнал не имеет права ронять сторож
            pass
    try:
        con.commit()
    except Exception:  # noqa: BLE001
        pass
    return {"checks": checks, "alerts": alerts, "warns": warns, "ok": not alerts}


def format_alerts(result: dict) -> str:
    """Строки только ALERT (пусто в норме)."""
    return "\n".join(f"ALERT: {c['msg']}" for c in result["alerts"])


def _db_path() -> str:
    return os.environ.get("TUBER_DB") or _config.db_path()


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(prog="tuber.core.health_all",
                                     description="кросс-платформенный сторож")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--db", dest="db_path", default=None)
    args, _ = parser.parse_known_args(argv)
    path = args.db_path or _db_path()
    con = _db.connect(path)
    try:
        res = run_all(con)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        else:
            emitted = health_dedup.filter_alerts(
                [c["msg"] for c in res["alerts"]],
                HEALTH_SEEN_PATH, window_hours=DEDUP_WINDOW_HOURS)
            out = "\n".join(f"ALERT: {msg}" for msg in emitted)
            if out:
                print(out)
        return 0
    finally:
        con.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
