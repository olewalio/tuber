"""Суточная сводка ``metrics_daily`` по платформам (ТЗ-53, часть 2).

Зачем
-----
До ТЗ-53 суточную строку писал только X-коллектор
(:func:`tuber.platforms.x.collect.update_metrics_daily`). По Telegram и YouTube
сводок не было вовсе — половина требования «ничего не теряется молча» не
измерялась. Общая логика вынесена в ядро: X теперь делегирует сюда, Telegram и
YouTube пишут ту же строку тем же набором колонок ядра
(``items_ingested, dup_rate, coverage, fail_rate, latency_p95_min,
valid_date_ratio, instances_alive, likes_median, enriched_ratio, errors``);
X-специфичные счётчики транспорта (``cdn_429_count, synd_429_count, ssr_used``)
и ``stale_lag_p95_min`` живут в ``extra_json`` — как и писал X.

Источники (ТЗ-53, 2.2) — только существующие данные ядра: ``run`` (журнал
прогонов), ``content`` (снимок материалов и дат публикации), ``metric_snapshot``
(замеры), ``transport_request`` / ``transport_instance`` (транспорт X). Если для
платформы источника нет — пишется ``NULL``, а не выдуманный ``0``.

Идемпотентность — по ``(day, platform)``: повторный прогон перезаписывает строку
(``ON CONFLICT(day, platform) DO UPDATE``), дублей не плодит.

Запись идёт в ``main.metrics_daily`` (квалификация схемы) намеренно: адаптеры
платформ заводят TEMP-представление ``metrics_daily`` и ``CompatConnection``
переписывает неквалифицированное имя. При прямой записи в ядро мы минуем эту
трансляцию и не зависим от формы legacy-представления платформы.
"""

from __future__ import annotations

import statistics
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from . import storage

#: Платформы, для которых пишется суточная сводка.
PLATFORMS: tuple[str, ...] = ("x", "telegram", "youtube")

#: Колонки ядра (пишутся как есть).
CORE_COLUMNS: tuple[str, ...] = (
    "items_ingested", "dup_rate", "coverage", "fail_rate", "latency_p95_min",
    "valid_date_ratio", "instances_alive", "likes_median", "enriched_ratio", "errors",
)

#: X-специфичные поля транспорта; кладутся в ``extra_json``.
EXTRA_COLUMNS: tuple[str, ...] = (
    "cdn_429_count", "synd_429_count", "ssr_used", "stale_lag_p95_min",
)

#: Платформы, чей коллектор не пишет ``run`` (см. TECH-DEBT D-73).
#: Для них поля прогонов (coverage/fail_rate/dup_rate/errors) честно ``NULL``.
#: TODO(debt-D-73): журнал прогонов YouTube/Telegram неполон — часть полей
#: суточной сводки недоступна. См. TECH-DEBT.md, D-73.


def _parse_iso(value: Any) -> datetime | None:
    """Разобрать ISO-строку ядра (``T`` или пробел) в aware-UTC или ``None``."""
    if not value:
        return None
    v = str(value).strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _p95(values: Iterable[float]) -> float | None:
    """P95 как в X-пути: квантиль ``n=20`` при ≥20 значениях, иначе максимум."""
    vals = list(values)
    if not vals:
        return None
    if len(vals) >= 20:
        return float(statistics.quantiles(vals, n=20)[18])
    return float(max(vals))


def _utc_day(day: str | None) -> str:
    return day or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _has_metrics_at(conn: sqlite3.Connection, platform: str) -> bool:
    """Использует ли платформа X-представление ``content.meta_json.metrics_at``."""
    row = conn.execute(
        "SELECT 1 FROM content WHERE platform=?"
        " AND json_extract(meta_json, '$.metrics_at') IS NOT NULL LIMIT 1",
        (platform,),
    ).fetchone()
    return row is not None


def _has_requests(conn: sqlite3.Connection, platform: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM transport_request WHERE platform=? LIMIT 1", (platform,)
    ).fetchone()
    return row is not None


def compute_daily(conn: sqlite3.Connection, day: str, platform: str,
                  *, now: datetime | None = None) -> dict[str, Any]:
    """Собрать поля суточной строки для ``(day, platform)`` из данных ядра.

    Значения, для которых у платформы нет источника, — ``None`` (не ``0``).
    Для ``platform='x'`` результат совпадает с прежним X-коллектором
    (перенос той же логики на таблицы ядра).
    """
    now_dt = now or datetime.now(timezone.utc)
    prefix = day + "%"
    fields: dict[str, Any] = {k: None for k in CORE_COLUMNS + EXTRA_COLUMNS}

    # --- прогоны (run): coverage/fail_rate/dup_rate/errors -------------------
    r = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(ok_count),0) ok, COALESCE(SUM(fail_count),0) fail,"
        " COALESCE(SUM(items_new),0) new, COALESCE(SUM(items_upd),0) upd,"
        " COALESCE(SUM(errors),0) err"
        " FROM run WHERE platform=? AND started_at LIKE ?",
        (platform, prefix),
    ).fetchone()
    if r is not None and r["n"]:
        ok, fail, new, upd = int(r["ok"]), int(r["fail"]), int(r["new"]), int(r["upd"])
        total = ok + fail
        fields["coverage"] = round(ok / total, 6) if total else None
        fields["fail_rate"] = round(fail / total, 6) if total else None
        fields["dup_rate"] = round(upd / (new + upd), 6) if (new + upd) else None
        # Долг D-73: X-коллектор колонку errors не писал — сохраняем поведение.
        fields["errors"] = int(r["err"]) if platform != "x" else None

    # --- материалы (content): объём, валидность даты -------------------------
    ing = conn.execute(
        "SELECT COUNT(*) n,"
        " SUM(CASE WHEN published_at IS NULL OR published_at=''"
        "          OR published_at LIKE '1970%' THEN 1 ELSE 0 END) bad"
        " FROM content WHERE platform=? AND first_seen_at LIKE ?"
        "   AND COALESCE(kind,'post') <> 'story_stub'",
        (platform, prefix),
    ).fetchone()
    n_ing = int(ing["n"] or 0)
    fields["items_ingested"] = n_ing
    fields["valid_date_ratio"] = (
        1.0 if n_ing == 0 else round(1.0 - int(ing["bad"] or 0) / n_ing, 6)
    )

    # --- задержка транспорта (transport_request) -----------------------------
    if _has_requests(conn, platform):
        lat = [row[0] for row in conn.execute(
            "SELECT latency_ms FROM transport_request"
            " WHERE platform=? AND ts LIKE ? AND latency_ms IS NOT NULL",
            (platform, prefix)).fetchall()]
        p95 = _p95(lat)
        fields["latency_p95_min"] = None if p95 is None else round(p95 / 60000.0, 4)
        # X-специфичные счётчики дублёров (kinds канала X).
        def _cnt(kind: str, status: int | None) -> int:
            if status is None:
                sql = ("SELECT COUNT(*) FROM transport_request"
                       " WHERE platform=? AND kind=? AND ts LIKE ?")
                params = (platform, kind, prefix)
            else:
                sql = ("SELECT COUNT(*) FROM transport_request"
                       " WHERE platform=? AND kind=? AND status=? AND ts LIKE ?")
                params = (platform, kind, status, prefix)
            return int(conn.execute(sql, params).fetchone()[0])
        fields["cdn_429_count"] = _cnt("cdn_tweet", 429)
        fields["synd_429_count"] = _cnt("synd_timeline", 429)
        fields["ssr_used"] = _cnt("x_ssr", None)

    # --- живые инстансы транспорта -------------------------------------------
    inst = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN healthy=1 THEN 1 ELSE 0 END) alive"
        " FROM transport_instance WHERE platform=?",
        (platform,),
    ).fetchone()
    if inst is not None and int(inst["n"] or 0) > 0:
        fields["instances_alive"] = int(inst["alive"] or 0)

    # --- медиана лайков и доля обогащённых -----------------------------------
    if _has_metrics_at(conn, platform):
        # X-путь: лайки обогащения в content.meta_json (как писал X-коллектор).
        likes = [row[0] for row in conn.execute(
            "SELECT json_extract(meta_json, '$.likes') FROM content"
            " WHERE platform=? AND json_extract(meta_json, '$.metrics_at') LIKE ?"
            "   AND json_extract(meta_json, '$.likes') IS NOT NULL",
            (platform, prefix)).fetchall()]
        fields["likes_median"] = (
            round(float(statistics.median(likes)), 4) if likes else None)
        tot = int(conn.execute(
            "SELECT COUNT(*) FROM content WHERE platform=?"
            "   AND COALESCE(kind,'post') <> 'story_stub'", (platform,)).fetchone()[0])
        enriched = int(conn.execute(
            "SELECT COUNT(*) FROM content WHERE platform=?"
            "   AND json_extract(meta_json, '$.metrics_at') IS NOT NULL"
            "   AND COALESCE(kind,'post') <> 'story_stub'", (platform,)).fetchone()[0])
        fields["enriched_ratio"] = round(enriched / tot, 6) if tot else None
    else:
        # Прочие платформы: лайки и обогащение — из замеров metric_snapshot.
        likes = [row[0] for row in conn.execute(
            "SELECT likes FROM metric_snapshot"
            " WHERE platform=? AND captured_at LIKE ? AND likes IS NOT NULL",
            (platform, prefix)).fetchall()]
        fields["likes_median"] = (
            round(float(statistics.median(likes)), 4) if likes else None)
        tot = int(conn.execute(
            "SELECT COUNT(*) FROM content WHERE platform=?"
            "   AND COALESCE(kind,'post') <> 'story_stub'", (platform,)).fetchone()[0])
        enriched = int(conn.execute(
            "SELECT COUNT(DISTINCT ms.content_id) FROM metric_snapshot ms"
            " JOIN content c ON c.id = ms.content_id"
            " WHERE ms.platform=? AND c.platform=?"
            "   AND COALESCE(c.kind,'post') <> 'story_stub'",
            (platform, platform)).fetchone()[0])
        fields["enriched_ratio"] = round(enriched / tot, 6) if tot else None

    # --- устаревание источников (X-специфичное поле extra_json) ---------------
    lags: list[float] = []
    for row in conn.execute(
            "SELECT MAX(published_at) m FROM content WHERE platform=?"
            " AND COALESCE(kind,'post') <> 'story_stub' GROUP BY source_id",
            (platform,)).fetchall():
        dt = _parse_iso(row["m"])
        if dt is not None:
            lags.append((now_dt - dt).total_seconds() / 60.0)
    fields["stale_lag_p95_min"] = None if not lags else round(_p95(lags), 4)

    return fields


def write_daily(conn: sqlite3.Connection, day: str, platform: str,
                fields: Mapping[str, Any]) -> None:
    """Записать/перезаписать суточную строку ядра идемпотентно по (day, platform)."""
    core = {k: fields.get(k) for k in CORE_COLUMNS}
    extra = {k: fields.get(k) for k in EXTRA_COLUMNS}
    cols = ["day", "platform"] + list(CORE_COLUMNS)
    values: list[Any] = [day, platform] + [core[k] for k in CORE_COLUMNS]
    has_extra = any(v is not None for v in extra.values())
    if has_extra:
        cols.append("extra_json")
        values.append(storage.jdump(extra))
    placeholders = ", ".join("?" for _ in cols)
    assigns = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("day", "platform"))
    # ``main.`` — минуем TEMP-трансляцию имён адаптеров платформ (см. модуль).
    conn.execute(
        f"INSERT INTO main.metrics_daily ({', '.join(cols)}) VALUES ({placeholders})"
        f" ON CONFLICT(day, platform) DO UPDATE SET {assigns}",
        values,
    )


def update_metrics_daily(conn: sqlite3.Connection, day: str | None = None, *,
                         platforms: tuple[str, ...] = PLATFORMS,
                         now: datetime | None = None,
                         days: int = 1) -> dict[str, Any]:
    """Посчитать и записать сводку за ``days`` суток (по умолчанию — за один день).

    ``day`` — последний день окна (по умолчанию сегодня UTC); ``days>1`` —
    разовый бэкфилл назад. Идемпотентно: повторный прогон перезаписывает строки.
    """
    last = _utc_day(day)
    last_dt = datetime.fromisoformat(last + "T00:00:00").replace(tzinfo=timezone.utc)
    result: dict[str, Any] = {"last_day": last, "days": int(days), "platforms": {}}
    for offset in range(int(days) - 1, -1, -1):
        d = (last_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        for platform in platforms:
            fields = compute_daily(conn, d, platform, now=now)
            write_daily(conn, d, platform, fields)
            result["platforms"].setdefault(platform, []).append(d)
    conn.commit()
    return result
