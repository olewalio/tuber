"""Единый расчёт охвата замеров по платформам (часть 4 «волна 2»).

Раньше «единый знаменатель» строился бы по ``metric_schedule``, но стадии
YouTube в эту очередь намеренно не кладутся (у него своя очередь слотов
``h0..h6``/``d``). Из-за этого охват YouTube занижался. Здесь YouTube считается
ОТДЕЛЬНЫМ знаменателем по своим слотам и явно помечается как «вне единой
очереди»; X и Telegram — по ``metric_schedule``.
"""

from __future__ import annotations

from tuber.core import metrics


def coverage_by_platform(conn, *, now=None) -> dict:
    """Охват замеров: X/Telegram — по очереди, YouTube — по своим слотам."""
    result: dict[str, dict] = {}
    for platform in metrics.PLATFORMS:
        rows = {r["status"]: int(r["n"]) for r in conn.execute(
            "SELECT COALESCE(status, 'pending') AS status, COUNT(*) AS n"
            "  FROM metric_schedule WHERE platform=? GROUP BY 1", (platform,))}
        planned = sum(rows.values())
        measured = rows.get(metrics.STAGE_STATUS_MEASURED, 0)
        late = rows.get(metrics.STAGE_STATUS_MEASURED_LATE, 0)
        captured = measured + late
        result[platform] = {
            "queue": "metric_schedule",
            "planned": planned,
            "measured": measured,
            "measured_late": late,
            "closed_no_data": rows.get(metrics.STAGE_STATUS_CLOSED_NO_DATA, 0),
            "pending": rows.get(metrics.STAGE_STATUS_PENDING, 0),
            "coverage": (captured / planned) if planned else None,
        }
    # YouTube — свой знаменатель; импорт платформы только здесь, чтобы ядро не
    # зависело от адаптера на уровне модуля.
    try:
        from tuber.platforms.youtube import schedule as yt_schedule
        result["youtube"] = yt_schedule.slot_coverage(conn, now=now)
    except Exception:  # noqa: BLE001 — охват YouTube не имеет права ронять отчёт
        result["youtube"] = {
            "queue": "own_slots",
            "note": "вне единой очереди metric_schedule (слоты h0..h6/d)",
            "videos": 0, "slots_expected": 0, "slots_captured": 0,
            "slots_missed": 0, "coverage": None,
        }
    return result


def _pct(value) -> str:
    return "н/д" if value is None else f"{value * 100:.1f}%"


def format_coverage(data: dict) -> list[str]:
    """Человекочитаемые строки охвата (по одной на платформу)."""
    lines = ["охват замеров по платформам (YouTube — вне единой очереди):"]
    for platform in (*metrics.PLATFORMS, "youtube"):
        d = data.get(platform)
        if not d:
            continue
        if d.get("queue") == "metric_schedule":
            captured = d["measured"] + d["measured_late"]
            lines.append(
                f"  {platform}: {captured}/{d['planned']} = {_pct(d['coverage'])}"
                f" (в срок {d['measured']}, поздно {d['measured_late']},"
                f" закрыто без данных {d['closed_no_data']}, ждут {d['pending']})")
        else:
            lines.append(
                f"  {platform}: {d['slots_captured']}/{d['slots_expected']}"
                f" = {_pct(d['coverage'])} (вне единой очереди, свои слоты h0..h6/d,"
                f" пропущено {d['slots_missed']}, видео {d['videos']})")
    return lines
