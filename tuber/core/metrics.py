"""Общий путь расчёта дельт и стадий замеров (ТЗ-43, контур 1 «Скорость»).

Зачем
-----
Расчёт ``delta_views``/``delta_likes``/``interval_seconds``/``bucket`` до этой
волны жил ТОЛЬКО в YouTube-пути
(:func:`tuber.platforms.youtube.store.insert_snapshot`). У X и Telegram
эквивалента не было: снимки писались, но ``delta_views`` у 0 из 48 128 (TG) и
0 из 17 142 (X), ``bucket`` = NULL у всех. Здесь общий расчёт вынесен в ядро,
чтобы X и Telegram заполняли те же поля при записи снимка.

Граница с YouTube
-----------------
YouTube-путь НЕ переведён на этот модуль: у него своя семантика слотов
(``h0..h6/d``) и свои тесты, и ломать их нельзя. Правила дельт ниже повторяют
YouTube один-в-один:

* ``interval_quality`` = ``first`` (нет предыдущего снимка), ``ok`` (интервал
  не короче :data:`MIN_INTERVAL_FOR_SPEED_SECONDS`), ``short`` (короче);
* дельты считаются при интервале не короче :data:`MIN_PAIR_INTERVAL_SECONDS`;
* скорость (``views_per_day``/``views_per_hour``) — только при ``ok``;
* **отрицательная дельта не пишется**: снимок помечается ``is_anomaly=1``
  («неполный»), а значение остаётся NULL.

Стадии очереди
--------------
``metric_schedule`` (ТЗ-43) планирует замеры ``1h/6h/24h/72h`` от
``published_at``. ``bucket`` снимка X/Telegram — имя стадии; возраст
(``age_hours``) хранится отдельно как фактический, поэтому «добор
просроченного» замера честно виден по возрасту.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import storage
from . import timeutil

#: Стадии очереди замеров (часы от публикации).
STAGES: tuple[str, ...] = ("1h", "6h", "24h", "72h")

#: Стадия → номинал в часах.
STAGE_HOURS: dict[str, int] = {"1h": 1, "6h": 6, "24h": 24, "72h": 72}

#: Платформы, для которых работает очередь (YouTube планирует свои слоты сам).
PLATFORMS: tuple[str, ...] = ("x", "telegram")

#: Метрика внимания платформы (согласовано с ТЗ-45 / долг D-59):
#: лайки для X, просмотры для Telegram.
PLATFORM_METRIC: dict[str, str] = {"x": "likes", "telegram": "views"}

#: Минимальный интервал между замерами, чтобы дельта считалась честной, сек.
MIN_PAIR_INTERVAL_SECONDS = 600
#: Минимальный интервал, при котором считается скорость, сек.
MIN_INTERVAL_FOR_SPEED_SECONDS = 3600

#: Сколько часов истории планировать (после 72 ч все стадии уже наступили).
PLAN_LOOKBACK_HOURS = 96

#: Насколько замер может опоздать, чтобы его ещё имело смысл делать вживую, ч.
SWEEP_GRACE_HOURS = 6
#: После стольких часов просрочки строка закрывается без данных, ч.
SWEEP_STALE_HOURS = 96
#: Потолок попыток для одной строки очереди.
MAX_ATTEMPTS = 5

#: Различимые состояния строки очереди ``metric_schedule.status`` (ТЗ-53, D-67).
#:
#: * ``pending``        — замер ещё не сделан (``done_at IS NULL``);
#: * ``measured``       — замер сделан, снимок со стадией в ``bucket`` есть;
#: * ``closed_no_data`` — стадия закрыта БЕЗ данных (просрочена безнадёжно или
#:   исчерпаны попытки): ``done_at`` стоит, снимка с этой стадией нет.
STAGE_STATUS_PENDING = "pending"
STAGE_STATUS_MEASURED = "measured"
STAGE_STATUS_CLOSED_NO_DATA = "closed_no_data"
STAGE_STATUSES: tuple[str, ...] = (
    STAGE_STATUS_PENDING, STAGE_STATUS_MEASURED, STAGE_STATUS_CLOSED_NO_DATA)


# ---------------------------------------------------------------------------
# Время
# ---------------------------------------------------------------------------

def _to_dt(value) -> datetime | None:
    iso = timeutil.parse_any(value)
    if iso is None:
        return None
    return datetime.strptime(iso, timeutil.ISO_FMT).replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(timeutil.ISO_FMT)


def _now_dt(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    dt = _to_dt(now)
    if dt is None:
        raise ValueError(f"не разобрана дата: {now!r}")
    return dt


def stage_for_age(age_hours: float | None) -> str | None:
    """Ближайшая стадия к возрасту снимка (``None``, если возраст неизвестен).

    Используется, когда снимок писал не планировщик (сбор X/Telegram). Границы —
    середина между номиналами: 3,5 ч / 15 ч / 48 ч.
    """
    if age_hours is None:
        return None
    try:
        age = float(age_hours)
    except (TypeError, ValueError):
        return None
    # Границы — середина между номиналами: 3,5 ч / 15 ч / 48 ч.
    if age < 3.5:
        return "1h"
    if age < 15.0:
        return "6h"
    if age < 48.0:
        return "24h"
    return "72h"


def due_at(published_at, stage: str) -> str | None:
    """Момент, когда замер стадии полагается: ``published_at + stage``."""
    dt = _to_dt(published_at)
    if dt is None or stage not in STAGE_HOURS:
        return None
    return _iso(dt + timedelta(hours=STAGE_HOURS[stage]))


def _age_hours(conn: sqlite3.Connection, content_id: int, captured_at: str) -> float | None:
    row = conn.execute(
        "SELECT published_at FROM content WHERE id=?", (content_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    pub, cap = _to_dt(row[0]), _to_dt(captured_at)
    if pub is None or cap is None:
        return None
    return (cap - pub).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Дельта и корзина одного снимка
# ---------------------------------------------------------------------------

#: Колонки-счётчики, по которым считается дельта.
_COUNTERS: tuple[tuple[str, str], ...] = (
    ("delta_views", "views"),
    ("delta_likes", "likes"),
    ("delta_comments", "comments"),
    ("delta_replies", "replies"),
    ("delta_reposts", "reposts"),
)


def apply_deltas(
    conn: sqlite3.Connection,
    content_id: int,
    captured_at: str,
    *,
    bucket: str | None = None,
    force_bucket: bool = False,
) -> dict | None:
    """Посчитать и записать дельты/корзину снимка ``(content_id, captured_at)``.

    Возвращает словарь записанных значений или ``None``, если снимка нет.
    Идемпотентно: повторный вызов на неизменных данных даёт тот же результат.
    """
    cap_iso = timeutil.parse_any(captured_at)
    row = conn.execute(
        "SELECT * FROM metric_snapshot WHERE content_id=? AND captured_at=?",
        (content_id, cap_iso)).fetchone()
    if row is None:
        return None

    prev = conn.execute(
        """
        SELECT captured_at, views, likes, comments, replies, reposts
          FROM metric_snapshot
         WHERE content_id=? AND captured_at < ?
         ORDER BY captured_at DESC LIMIT 1
        """,
        (content_id, cap_iso)).fetchone()

    interval_seconds = None
    interval_quality = "first"
    deltas: dict[str, int | None] = {name: None for name, _ in _COUNTERS}
    views_per_day = views_per_hour = None
    is_anomaly = 0

    if prev is not None:
        prev_cap = timeutil.iso_to_epoch(prev["captured_at"])
        cap = timeutil.iso_to_epoch(cap_iso)
        if prev_cap is not None and cap is not None:
            interval_seconds = int(cap) - int(prev_cap)
        if interval_seconds is not None and interval_seconds >= MIN_PAIR_INTERVAL_SECONDS:
            for name, col in _COUNTERS:
                cur, old = row[col], prev[col]
                if cur is None or old is None:
                    continue
                value = int(cur) - int(old)
                if value < 0:
                    # Отрицательная дельта — не пишем, помечаем снимок неполным.
                    is_anomaly = 1
                    deltas[name] = None
                else:
                    deltas[name] = value
        if interval_seconds is not None and interval_seconds >= MIN_INTERVAL_FOR_SPEED_SECONDS:
            interval_quality = "ok"
            if deltas["delta_views"] is not None:
                views_per_day = deltas["delta_views"] / (interval_seconds / 86400)
                views_per_hour = deltas["delta_views"] / (interval_seconds / 3600)
        elif interval_seconds is not None:
            interval_quality = "short"

    age = _age_hours(conn, content_id, cap_iso)
    if bucket is None or (force_bucket and row["bucket"] is None):
        bucket = stage_for_age(age)
    elif bucket is None:
        bucket = row["bucket"]

    conn.execute(
        """
        UPDATE metric_snapshot SET
          bucket=?, age_hours=?, interval_seconds=?, interval_quality=?,
          delta_views=?, delta_likes=?, delta_comments=?,
          delta_replies=?, delta_reposts=?,
          views_per_day=?, views_per_hour=?, is_anomaly=?
        WHERE content_id=? AND captured_at=?
        """,
        (bucket, age, interval_seconds, interval_quality,
         deltas["delta_views"], deltas["delta_likes"], deltas["delta_comments"],
         deltas["delta_replies"], deltas["delta_reposts"],
         views_per_day, views_per_hour, is_anomaly,
         content_id, cap_iso))
    return {
        "bucket": bucket, "age_hours": age, "interval_seconds": interval_seconds,
        "interval_quality": interval_quality, "is_anomaly": is_anomaly,
        **deltas, "views_per_day": views_per_day, "views_per_hour": views_per_hour,
    }


def apply_deltas_latest(
    conn: sqlite3.Connection,
    content_id: int,
    *,
    bucket: str | None = None,
) -> dict | None:
    """Посчитать дельты/корзину ПОСЛЕДНЕГО снимка материала.

    Зовётся из путей записи X и Telegram сразу после вставки снимка (ТЗ-43,
    п. 2): так ``bucket``/``delta_*`` заполняются «при записи», а не задним
    числом.
    """
    row = conn.execute(
        "SELECT MAX(captured_at) FROM metric_snapshot WHERE content_id=?",
        (content_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    return apply_deltas(conn, content_id, row[0], bucket=bucket)


def backfill_snapshots(
    conn: sqlite3.Connection,
    *,
    platforms: tuple[str, ...] = PLATFORMS,
    only_unbucketed: bool = True,
    limit: int = 0,
) -> dict:
    """Пересчитать дельты/корзину у уже имеющихся снимков X и Telegram.

    Идемпотентно. ``only_unbucketed=True`` (по умолчанию) берёт только снимки с
    ``bucket IS NULL`` — то есть те, что писали сборщики X/Telegram до этой
    волны (и которые пишутся ими сейчас, пока сбор не заполняет корзину сам).
    Возвращает ``{"snapshots": N, "contents": M}``.
    """
    marks = ",".join("?" for _ in platforms)
    where = [f"platform IN ({marks})"]
    params: list = list(platforms)
    if only_unbucketed:
        where.append("bucket IS NULL")
    sql = (
        "SELECT content_id, captured_at FROM metric_snapshot "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY content_id, captured_at"
    )
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()

    count = 0
    seen: set[int] = set()
    for content_id, captured_at in rows:
        if apply_deltas(conn, int(content_id), captured_at) is not None:
            count += 1
            seen.add(int(content_id))
    # D-66: снимки X уже несли `views`, но `content_latest` их терял — оси
    # «охват на подписчика» и «реакции на 1 000» не видели X. Бэкфилл доводит
    # «последнее известное» до снимка (idempotent, только для этих платформ).
    latest = storage.recompute_content_latest(conn, platforms=tuple(platforms))
    conn.commit()
    return {"snapshots": count, "contents": len(seen), "content_latest": latest}


def snapshot_metric(platform: str, row) -> float | None:
    """Метрика внимания снимка по платформе (лайки X, просмотры Telegram/YT)."""
    col = PLATFORM_METRIC.get(platform, "views")
    value = row[col] if col in row.keys() else None
    if value is None and col == "likes":
        # У X метрика — лайки; если их нет, второй приоритет — просмотры.
        value = row["views"] if "views" in row.keys() else None
    return None if value is None else float(value)


# ---------------------------------------------------------------------------
# Планирование и добор очереди
# ---------------------------------------------------------------------------

def plan_stages(
    conn: sqlite3.Connection,
    *,
    now=None,
    lookback_hours: int = PLAN_LOOKBACK_HOURS,
    platforms: tuple[str, ...] = PLATFORMS,
    limit: int = 0,
) -> dict:
    """Поставить стадии ``1h/6h/24h/72h`` для постов X/Telegram.

    Стадии планируются от ``published_at``; повторный прогон не плодит строк
    (``ON CONFLICT(content_id, stage) DO NOTHING``). Берутся посты за последние
    ``lookback_hours``. Возвращает ``{"planned_posts", "inserted", "existing",
    "due_now"}``.
    """
    now_dt = _now_dt(now)
    now_iso = _iso(now_dt)
    cutoff = _iso(now_dt - timedelta(hours=int(lookback_hours)))
    marks = ",".join("?" for _ in platforms)
    sql = (
        "SELECT id, platform, published_at FROM content "
        f"WHERE platform IN ({marks}) AND published_at >= ? AND published_at <= ? "
        "ORDER BY published_at DESC"
    )
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, [*platforms, cutoff, now_iso]).fetchall()

    inserted = existing = 0
    seen_content: set[int] = set()
    for r in rows:
        cid, platform, published_at = r[0], r[1], r[2]
        seen_content.add(int(cid))
        for stage in STAGES:
            due = due_at(published_at, stage)
            if due is None:
                continue
            cur = conn.execute(
                "INSERT INTO metric_schedule"
                "(content_id, platform, stage, due_at, attempt, status) "
                "VALUES (?,?,?,?,0,?) ON CONFLICT(content_id, stage) DO NOTHING",
                (cid, platform, stage, due, STAGE_STATUS_PENDING))
            if cur.rowcount:
                inserted += 1
            else:
                existing += 1
    conn.commit()
    due_now = conn.execute(
        "SELECT COUNT(*) FROM metric_schedule WHERE done_at IS NULL AND due_at <= ?",
        (now_iso,)).fetchone()[0]
    return {
        "planned_posts": len(seen_content), "inserted": inserted,
        "existing": existing, "due_now": int(due_now), "cutoff": cutoff,
    }


def _nearest_snapshot(conn, content_id: int, due: str, tolerance_hours: float):
    due_dt = _to_dt(due)
    if due_dt is None:
        return None
    lo = _iso(due_dt - timedelta(hours=tolerance_hours))
    hi = _iso(due_dt + timedelta(hours=tolerance_hours))
    return conn.execute(
        "SELECT captured_at FROM metric_snapshot "
        " WHERE content_id=? AND captured_at BETWEEN ? AND ? "
        " ORDER BY ABS(julianday(captured_at) - julianday(?)) LIMIT 1",
        (content_id, lo, hi, due)).fetchone()


#: Допуск привязки существующего снимка к стадии, часы.
LINK_TOLERANCE_HOURS = 3.0


def sweep_overdue(
    conn: sqlite3.Connection,
    *,
    now=None,
    limit: int = 0,
    fetch=None,
    grace_hours: int = SWEEP_GRACE_HOURS,
    stale_hours: int = SWEEP_STALE_HOURS,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict:
    """Добрать просроченные стадии очереди (``due_at < now``, ``done_at IS NULL``).

    Для каждой строки:

    1. если рядом с ``due_at`` уже есть снимок (±:data:`LINK_TOLERANCE_HOURS`) —
       он и есть замер этой стадии: проставляем ``bucket`` и дельты, ``done_at``;
    2. иначе, если замер опоздал не сильнее ``grace_hours`` и есть ``fetch`` —
       берём свежие метрики и пишем снимок сейчас (``recorded``);
    3. иначе строка либо ждёт следующей попытки (``attempt += 1``), либо, если
       просрочена безнадёжно (``stale_hours`` / ``max_attempts``), закрывается
       без данных (``missed``).

    ``fetch`` — callable ``(platform, external_id, handle) -> dict|None`` с полями
    ``views/likes/comments/replies/reposts``. Возвращает счётчики.
    """
    now_dt = _now_dt(now)
    now_iso = _iso(now_dt)
    sql = (
        "SELECT s.content_id AS content_id, s.platform AS platform, s.stage AS stage,"
        "       s.due_at AS due_at, s.attempt AS attempt,"
        "       c.external_id AS external_id, c.source_id AS source_id,"
        "       c.published_at AS published_at, src.handle AS handle"
        "  FROM metric_schedule s"
        "  JOIN content c ON c.id = s.content_id"
        "  LEFT JOIN source src ON src.id = c.source_id"
        " WHERE s.done_at IS NULL AND s.due_at <= ?"
        " ORDER BY s.due_at DESC"
    )
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (now_iso,)).fetchall()

    result = {"linked": 0, "recorded": 0, "missed": 0, "deferred": 0, "processed": 0}
    recorded_contents: set[int] = set()
    for r in rows:
        result["processed"] += 1
        content_id = int(r["content_id"])
        due = r["due_at"]
        due_dt = _to_dt(due)
        overdue_h = (now_dt - due_dt).total_seconds() / 3600.0 if due_dt else 1e9

        snap = _nearest_snapshot(conn, content_id, due, LINK_TOLERANCE_HOURS)
        if snap is not None:
            apply_deltas(conn, content_id, snap["captured_at"], bucket=r["stage"])
            conn.execute(
                "UPDATE metric_schedule SET done_at=?, attempt=attempt+1, status=?"
                " WHERE content_id=? AND stage=?",
                (snap["captured_at"], STAGE_STATUS_MEASURED, content_id, r["stage"]))
            result["linked"] += 1
            continue

        if fetch is not None and overdue_h <= grace_hours and content_id not in recorded_contents:
            metrics = None
            try:
                metrics = fetch(r["platform"], r["external_id"], r["handle"])
            except Exception:  # noqa: BLE001 — сеть не имеет права ронять добор
                metrics = None
            if metrics:
                _write_snapshot(conn, content_id, r["platform"], now_iso,
                                metrics, bucket=r["stage"])
                apply_deltas(conn, content_id, now_iso, bucket=r["stage"])
                conn.execute(
                    "UPDATE metric_schedule SET done_at=?, attempt=attempt+1, status=?"
                    " WHERE content_id=? AND stage=?",
                    (now_iso, STAGE_STATUS_MEASURED, content_id, r["stage"]))
                recorded_contents.add(content_id)
                result["recorded"] += 1
                continue
            conn.execute(
                "UPDATE metric_schedule SET attempt=attempt+1"
                " WHERE content_id=? AND stage=?",
                (content_id, r["stage"]))
            result["deferred"] += 1
            continue

        attempt = int(r["attempt"] or 0) + 1
        if overdue_h >= stale_hours or attempt >= max_attempts:
            # TODO(debt-D-67): закрыт в ТЗ-53 — в metric_schedule добавлена
            # колонка status (pending/measured/closed_no_data), миграция
            # идемпотентна. Стадия, закрытая без данных, теперь отличима от
            # измеренной. См. TECH-DEBT.md D-67.
            conn.execute(
                "UPDATE metric_schedule SET done_at=?, attempt=?, status=?"
                " WHERE content_id=? AND stage=?",
                (now_iso, attempt, STAGE_STATUS_CLOSED_NO_DATA, content_id, r["stage"]))
            result["missed"] += 1
        else:
            conn.execute(
                "UPDATE metric_schedule SET attempt=?"
                " WHERE content_id=? AND stage=?",
                (attempt, content_id, r["stage"]))
            result["deferred"] += 1
    conn.commit()
    return result


def monotonic_views(conn, content_id: int, views) -> int | None:
    """Не позволить просмотрам убывать для материала (ТЗ-53, инвариант).

    Счётчик просмотров платформы монотонен: новый снимок не может показать
    меньше, чем уже сохранённый максимум для того же ``content_id``. Возвращает
    ``max(views, MAX(metric_snapshot.views))``; ``None`` пропускает как есть
    (нечего сравнивать). Отрицательный/устаревший сетевой ответ не понижает ряд.
    """
    if views is None:
        return None
    row = conn.execute(
        "SELECT MAX(views) FROM metric_snapshot WHERE content_id=?",
        (content_id,)).fetchone()
    prev = row[0] if row is not None else None
    if prev is None:
        return views
    return max(int(prev), int(views))


def _write_snapshot(conn, content_id, platform, captured_at, metrics, *, bucket=None):
    """Записать снимок метрик (без дельт — их считает :func:`apply_deltas`).

    Просмотры проходят через :func:`monotonic_views`: очередь не понижает уже
    сохранённое значение, даже если сетевой ответ оказался меньше (ТЗ-53).
    """
    views = monotonic_views(conn, content_id, metrics.get("views"))
    conn.execute(
        """
        INSERT INTO metric_snapshot
          (content_id, platform, captured_at, bucket, source,
           views, likes, comments, replies, reposts, forwards, reactions)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(content_id, captured_at) DO UPDATE SET
          views=excluded.views, likes=excluded.likes, comments=excluded.comments,
          replies=excluded.replies, reposts=excluded.reposts,
          forwards=excluded.forwards, reactions=excluded.reactions
        """,
        (content_id, platform, captured_at, bucket, "metric_queue",
         views, metrics.get("likes"), metrics.get("comments"),
         metrics.get("replies"), metrics.get("reposts"),
         metrics.get("forwards"), metrics.get("reactions")))


# ---------------------------------------------------------------------------
# Блок «Раннее»: скорость, доля 6 ч, ускорение
# ---------------------------------------------------------------------------

def schedule_status_counts(conn: sqlite3.Connection, *, now=None) -> dict:
    """Счётчики строк очереди по статусу (ТЗ-53, D-67).

    Возвращает ``{"pending", "measured", "closed_no_data", "overdue_pending"}``.
    ``overdue_pending`` — строки, у которых срок уже наступил (``due_at <= now``),
    но замер ещё не сделан: их смешение с «ещё не наступило» и давало ложное
    «закрыто 0 из N». Строки со статусом ``NULL`` (старая база до миграции)
    считаются ``pending`` — миграция их дозаполняет при запуске.
    """
    now_iso = _iso(_now_dt(now))
    counts = {st: 0 for st in STAGE_STATUSES}
    for r in conn.execute(
            "SELECT COALESCE(status, ?) AS status, COUNT(*) AS n"
            "  FROM metric_schedule GROUP BY COALESCE(status, ?)",
            (STAGE_STATUS_PENDING, STAGE_STATUS_PENDING)).fetchall():
        key = r["status"] if r["status"] in counts else STAGE_STATUS_PENDING
        counts[key] += int(r["n"])
    counts["overdue_pending"] = int(conn.execute(
        "SELECT COUNT(*) FROM metric_schedule WHERE done_at IS NULL AND due_at <= ?",
        (now_iso,)).fetchone()[0])
    return counts


def format_schedule(schedule: dict) -> str:
    """Человекочитаемая строка статусов стадий (Д-67): не путать «нет данных» с «ещё не наступило»."""
    return (
        f"стадии очереди: измерено {schedule.get('measured', 0)},"
        f" закрыто без данных {schedule.get('closed_no_data', 0)},"
        f" ждут замера {schedule.get('pending', 0)}"
        f" (из них просрочено {schedule.get('overdue_pending', 0)})")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def build_early(
    conn: sqlite3.Connection,
    *,
    now=None,
    window_days: int = 7,
    limit: int = 5,
    min_author_posts: int = 2,
    platforms: tuple[str, ...] = PLATFORMS,
) -> dict:
    """Блок «Раннее»: скорость ``v``, ``share_6h``, ``accel`` и вердикт.

    ``v = (m24 − m1) / 23`` (единиц в час), ``share_6h = (m6 − m1)/(m24 − m1)``
    (резкий разгон при ``≥ 0,5``), ``accel = v(6→24) / max(v(1→6), ε)``.

    Порог «разгоняется» — ``m6 ≥ 3 × медианы автора НА ТОМ ЖЕ ВОЗРАСТЕ`` (стадия
    6h). Медиана считается по всем постам автора за окно; автору нужно минимум
    ``min_author_posts`` таких постов, иначе вердикт честно «медианы нет».
    Возвращает ``{"items": [...], "medias": {...}, "scanned": N}``; ``items``
    отсортированы по ``accel`` и обрезаны до ``limit``.
    """
    now_dt = _now_dt(now)
    cutoff = _iso(now_dt - timedelta(days=int(window_days)))
    marks = ",".join("?" for _ in platforms)
    rows = conn.execute(
        f"""
        SELECT c.id AS cid, c.source_id AS sid, c.platform AS platform,
               c.external_id AS external_id, c.url AS url, c.published_at AS published_at,
               m.captured_at AS captured_at, m.bucket AS bucket, m.age_hours AS age_hours,
               m.views AS views, m.likes AS likes
          FROM content c
          JOIN metric_snapshot m ON m.content_id = c.id
         WHERE c.platform IN ({marks}) AND c.published_at >= ?
        """,
        (*platforms, cutoff)).fetchall()

    # {cid: {"1h": metric, "6h": ..., ...}}
    by_content: dict[int, dict] = {}
    meta: dict[int, dict] = {}
    for r in rows:
        metric = snapshot_metric(r["platform"], r)
        if metric is None:
            continue
        stage = r["bucket"] if r["bucket"] in STAGE_HOURS else stage_for_age(r["age_hours"])
        if stage is None:
            continue
        item = by_content.setdefault(int(r["cid"]), {})
        # При нескольких снимках одной стадии берём последний по времени.
        prev = item.get(stage)
        if prev is None or (r["captured_at"] or "") >= (prev[0] or ""):
            item[stage] = (r["captured_at"], metric)
        meta[int(r["cid"])] = {
            "content_id": int(r["cid"]), "source_id": r["sid"], "platform": r["platform"],
            "external_id": r["external_id"], "url": r["url"],
            "published_at": r["published_at"],
        }

    stages = {cid: {st: v[1] for st, v in d.items()} for cid, d in by_content.items()}

    # Медиана автора на стадии 6h (тот же возраст поста). Считается
    # leave-one-out: собственный пост автора в медиану не входит, иначе
    # всплеск завышал бы собственную норму. Нужен минимум ``min_author_posts``
    # ДРУГИХ постов автора на этой стадии.
    groups: dict[int, list[tuple[int, float]]] = {}
    for cid, st in stages.items():
        if "6h" in st:
            sid = meta[cid]["source_id"]
            if sid is not None:
                groups.setdefault(int(sid), []).append((cid, st["6h"]))
    medians: dict[int, float] = {}
    for pairs in groups.values():
        for cid, _val in pairs:
            others = [v for other, v in pairs if other != cid]
            if len(others) >= int(min_author_posts):
                medians[cid] = _median(others)

    items: list[dict] = []
    for cid, st in stages.items():
        m1, m6, m24 = st.get("1h"), st.get("6h"), st.get("24h")
        if m1 is None or m6 is None or m24 is None:
            continue
        base = m24 - m1
        v = base / 23.0
        share_6h = (m6 - m1) / base if base > 0 else None
        v16 = (m6 - m1) / 5.0
        v624 = (m24 - m6) / 18.0
        # Ранний темп (1ч→6ч) нулевой или отрицательный — делить не на что:
        # accel не определён. Раньше знаменатель подпирался EPSILON=1e-9, и
        # выдача печатала вырожденные 1e11/5.5e7 при m6 == m1 (ТЗ-53, D-67).
        accel = None if v16 <= 0 else v624 / v16
        author_median = medians.get(cid)
        grows = bool(author_median is not None and m6 >= 3.0 * author_median)
        items.append({
            **meta[cid], "m1": m1, "m6": m6, "m24": m24,
            "v": v, "share_6h": share_6h, "accel": accel,
            "author_median_6h": author_median, "grows": grows,
        })

    items.sort(key=lambda it: (it["accel"] if it["accel"] is not None else float("-inf")),
               reverse=True)
    return {"items": items[:limit] if limit and limit > 0 else items,
            "all_count": len(items), "medias": medians, "scanned": len(stages),
            "window_days": window_days,
            # Статусы стадий очереди: «закрыто без данных» отдельно от «ещё не
            # наступило» (ТЗ-53, D-67) — блок «Раннее» больше не выдаёт
            # «закрыто 0 из 400» на просроченной стадии без данных.
            "schedule": schedule_status_counts(conn, now=now)}


def format_early(data: dict) -> str:
    """Человекочитаемый блок «Раннее» (пусто → пустая строка: норма молчит)."""
    items = data.get("items") or []
    if not items:
        return ""
    lines = ["-- Раннее (скорость по стадиям 1ч/6ч/24ч) --"]
    for it in items:
        share = "н/д" if it["share_6h"] is None else f"{it['share_6h']:.2f}"
        accel = "нет раннего темпа" if it["accel"] is None else f"{it['accel']:.2f}"
        med = "нет" if it["author_median_6h"] is None else f"{it['author_median_6h']:.0f}"
        verdict = "разгоняется" if it["grows"] else "нет"
        link = it.get("url") or it.get("external_id") or "?"
        lines.append(
            f"  {it['platform']} {link}: m1={it['m1']:.0f} m6={it['m6']:.0f} "
            f"m24={it['m24']:.0f}, v={it['v']:.2f}, share_6h={share}, "
            f"accel={accel}, медиана6ч={med} -> {verdict}")
    return "\n".join(lines)
