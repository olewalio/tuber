"""Расписание замеров: кого и в какие слоты мерить прямо сейчас (этап 4, часть 2).

Слоты:
- h0..h6 — свежие видео (моложе FRESH_MAX_HOURS), шаг из FRESH_SLOT_OFFSETS;
- d — суточный слот: дважды в сутки для видео 2-10 дней, раз в сутки для более старых.

Повторно слот не планируется: замер (video_id, bucket) уже есть — пропуск.
Видео старше MAX_TRACK_DAYS из плана выпадает полностью.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Sequence

from tuber.core import storage as core_storage
from tuber.core import timeutil

from . import config, store as db, api as yt

log = logging.getLogger(__name__)

# Размер батча videos.list (1 unit за 50 id).
BATCH_SIZE = 50


def make_client(conn: Any) -> yt.YouTubeClient:
    """Клиент YouTube, пишущий квоту в это соединение."""
    return yt.YouTubeClient(conn=conn)


def _chunks(items: Sequence[Any], size: int) -> list[list[Any]]:
    """Разбить список на бачки не длиннее size."""
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def _fresh_bucket(age_seconds: int, cfg: Any) -> str | None:
    """Слот свежего видео: наибольшее смещение, не превышающее возраст."""
    chosen: str | None = None
    for bucket, offset in sorted(cfg.FRESH_SLOT_OFFSETS.items(), key=lambda kv: kv[1]):
        if age_seconds >= offset:
            chosen = bucket
        else:
            break
    return chosen


def plan_snapshots(conn: Any, now: int, cfg: Any = config) -> list[tuple[str, str]]:
    """Вернуть список (video_id, bucket), кого мерить сейчас."""
    now = int(now)
    max_age_hours = getattr(cfg, "MAX_TRACK_DAYS", config.MAX_TRACK_DAYS) * 24
    twice_interval = getattr(cfg, "DAILY_TWICE_INTERVAL_SECONDS",
                             config.DAILY_TWICE_INTERVAL_SECONDS)

    # Что уже снято: слоты по видео и время последнего суточного замера.
    existing: dict[str, set[str]] = {}
    last_daily: dict[str, int] = {}
    for row in conn.execute("SELECT video_id, bucket, captured_at FROM snapshots"):
        existing.setdefault(row["video_id"], set()).add(row["bucket"])
        if row["bucket"] == cfg.SLOT_DAILY:
            prev = last_daily.get(row["video_id"])
            if prev is None or row["captured_at"] > prev:
                last_daily[row["video_id"]] = row["captured_at"]

    plan: list[tuple[str, str]] = []
    # Меряем только видео с is_ai = 1: is_ai = 0 и NULL (разбор не удался)
    # в план не попадают до повторного разбора.
    rows = conn.execute(
        "SELECT v.video_id, v.published_at, v.is_shorts FROM videos v "
        "JOIN video_classification vc ON vc.video_id = v.video_id "
        "WHERE v.published_at IS NOT NULL AND vc.is_ai = 1 "
        "ORDER BY v.video_id"
    ).fetchall()
    speedup = float(getattr(cfg, "SHORTS_FRESH_SLOT_SPEDUP",
                           config.SHORTS_FRESH_SLOT_SPEDUP))
    for row in rows:
        video_id = row["video_id"]
        age_seconds = now - int(row["published_at"])
        if age_seconds < 0:
            continue  # будущее время публикации — мусор
        if age_seconds >= max_age_hours * 3600:
            continue  # старше MAX_TRACK_DAYS — вне плана

        if age_seconds < cfg.FRESH_MAX_HOURS * 3600:
            # Шортсы набирают просмотры и умирают быстрее: их возраст для
            # выбора слота растягивается, поэтому часовые слоты наступают
            # раньше (при 1.5 — раз в 2 часа против 3-4 часов у полных).
            # Механизм слотов и дедупликация по bucket не меняются.
            is_short = row["is_shorts"] == 1
            slot_age = int(age_seconds * speedup) if is_short else age_seconds
            bucket = _fresh_bucket(slot_age, cfg)
            if bucket is None:
                continue
            if bucket in existing.get(video_id, set()):
                continue  # слот уже снят
            plan.append((video_id, bucket))
        else:
            if age_seconds < cfg.LONG_LIVED_MAX_DAYS * 86400:
                interval = twice_interval
            else:
                interval = cfg.DAILY_INTERVAL_SECONDS
            last = last_daily.get(video_id)
            if last is not None and now - last < interval:
                continue  # свежий суточный замер уже есть
            plan.append((video_id, cfg.SLOT_DAILY))
    return plan


def _stat(item: dict, name: str) -> int | None:
    """Достать числовую статистику; отсутствие -> None (не 0)."""
    stats = item.get("statistics") or {}
    value = stats.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def count_requests(n_ids: int, batch_size: int = BATCH_SIZE) -> int:
    """Сколько вызовов ``videos.list`` нужно на ``n_ids`` видео (пачки по 50)."""
    n = int(n_ids)
    if n <= 0:
        return 0
    return (n + int(batch_size) - 1) // int(batch_size)


def fill_gap_from_budget(quota_remaining_units: int, pct: float = 20.0,
                         batch_size: int = BATCH_SIZE) -> int:
    """N видео для добора из бюджета квоты (D-47/ТЗ-B12).

    Правило: добор не расходует больше ``pct`` % ОСТАТКА суточной квоты.
    Остаток считается в units (``api.YouTubeClient.quota_remaining``);
    ``videos.list`` стоит 1 unit за пачку ≤ 50 id, поэтому
    ``N = floor(remaining_units * pct / 100) * 50``.
    """
    units = int(int(quota_remaining_units) * float(pct) / 100.0)
    return max(0, units * int(batch_size))


def _published_at_map(conn) -> dict[str, int | None]:
    return {
        row["video_id"]: row["published_at"]
        for row in conn.execute("SELECT video_id, published_at FROM videos")
    }


# TODO(debt-D-47): закрыт в ТЗ-B12b (23.09.2026): боевой добор
# `yt snapshots --fill-gap-quota-pct 20` на ЖИВОЙ базе — расход 698 units
# (≤20 % остатка), `indexed` 22 017 → 45 213; 0-осевые — отдельное решение;
# см. TECH-DEBT.md.
def fill_gap_candidates(conn, limit: int, now: int | None = None,
                        cfg: Any = config) -> list[str]:
    """Видео для добора метрик: меньше ДВУХ осей виральности (D-47).

    Оси считаются той же логикой, что и композитный индекс
    (:func:`tuber.platforms.youtube.viral.compute`): индекс не построен ровно
    там, где осей меньше ``VIRAL_MIN_AXES``. Приоритет: сначала видео с ОДНОЙ
    осью (им не хватает одной метрики), затем с нулём; внутри группы — свежие
    первыми (``published_at`` по убыванию). ``limit <= 0`` — режим выключен.
    """
    if int(limit) <= 0:
        return []
    from . import viral
    computed = viral.compute(conn, now=now, cfg=cfg)
    published = _published_at_map(conn)
    one_axis: list[tuple[int, str]] = []
    zero_axis: list[tuple[int, str]] = []
    for video_id, info in computed.items():
        axes_count = len(info["axes"])
        if axes_count >= 2:
            continue
        published_at = published.get(video_id)
        stamp = int(published_at) if published_at is not None else -1
        (one_axis if axes_count == 1 else zero_axis).append((stamp, video_id))
    # Свежие первыми; video_id как детерминированный тайбрейк.
    one_axis.sort(key=lambda item: (-item[0], item[1]))
    zero_axis.sort(key=lambda item: (-item[0], item[1]))
    ordered = [vid for _, vid in one_axis] + [vid for _, vid in zero_axis]
    return ordered[: int(limit)]


def _quota_remaining(client: Any) -> int | None:
    """Остаток суточной квоты, если клиент умеет его считать."""
    fn = getattr(client, "quota_remaining", None)
    if callable(fn):
        try:
            return int(fn())
        except Exception as exc:  # noqa: BLE001 — бюджет не должен ронять прогон
            log.warning("остаток квоты не прочитан: %s", exc)
            return None
    return None


def run_snapshots(conn: Any, cfg: Any = config, now: int | None = None,
                  client: Any | None = None, fill_gap: int = 0) -> dict[str, Any]:
    """Снять замеры по плану; ``fill_gap`` добавляет добор архивных видео (D-47).

    Сбой батча не роняет прогон. Обычный план по возрастным корзинам не
    меняется — добор идёт ПОВЕРХ: кандидаты с меньшим двух осями получают
    суточный слот. В сводку пишется расчёт числа запросов ``videos.list``
    (пачки по 50) и, если клиент умеет, остаток суточной квоты.
    """
    now = config.now_ts() if now is None else int(now)
    if client is None:
        client = make_client(conn)

    plan = plan_snapshots(conn, now, cfg)
    bucket_of = {video_id: bucket for video_id, bucket in plan}
    gap_ids = fill_gap_candidates(conn, fill_gap, now, cfg)
    for video_id in gap_ids:
        # Обычный план приоритетнее: если видео уже в нём — слот не меняем.
        bucket_of.setdefault(video_id, cfg.SLOT_DAILY)

    summary: dict[str, Any] = {
        "planned": len(plan),
        "fill_gap": len(gap_ids),
        "fill_gap_requested": int(fill_gap),
        "requests": count_requests(len(bucket_of)),
        "quota_remaining": _quota_remaining(client),
        "captured": 0,
        "batches": 0,
        "failed_batches": 0,
        "errors": [],
    }
    for batch in _chunks(list(bucket_of), BATCH_SIZE):
        summary["batches"] += 1
        try:
            items = client.videos_by_ids(batch)
        except Exception as exc:  # один батч упал — остальные идём дальше
            summary["failed_batches"] += 1
            summary["errors"].append(str(exc))
            log.warning("батч замеров не удался: %s", exc)
            continue
        for item in items:
            video_id = item.get("id")
            bucket = bucket_of.get(video_id)
            if bucket is None:
                continue
            source = cfg.SOURCE_FRESH if bucket != cfg.SLOT_DAILY else cfg.SOURCE_DAILY
            db.insert_snapshot(
                conn,
                video_id,
                now,
                bucket,
                views=_stat(item, "viewCount"),
                likes=_stat(item, "likeCount"),
                comments=_stat(item, "commentCount"),
                source=source,
            )
            summary["captured"] += 1
    # ТЗ-53, часть 1: снимки YouTube пишутся в ядровый ``metric_snapshot``, но
    # «последнее известное» (``content_latest``) этот путь не обновлял — таблица
    # замирала на дате последней миграции, а потребители (digest: охват на
    # подписчика, реакции на 1 000) читали устаревшие числа. Пересчитываем один
    # раз на прогон, а не на каждое видео; покрывает и CLI ``yt snapshots``, и
    # ``yt daily`` (оба идут через :func:`run_snapshots`).
    summary["content_latest"] = core_storage.recompute_content_latest(
        conn, platforms=("youtube",))
    # ТЗ-53, часть 2: суточная сводка metrics_daily по YouTube (раньше писалась
    # только по X). Идемпотентно по (day, platform).
    from tuber.core import daily_metrics
    daily_metrics.update_metrics_daily(conn, platforms=("youtube",))
    # Часть 4 «волна 2»: слоты, которые были обязаны сняться, но снимка нет,
    # оставляют след в журнале youtube_slot_miss (пропуск больше не растворяется).
    summary["slot_miss"] = record_missed_slots(conn, now, cfg)
    conn.commit()
    return summary


# ---------------------------------------------------------------------------
# Пропущенные слоты и охват YouTube по СВОИМ слотам (часть 4 «волна 2»)
# ---------------------------------------------------------------------------
#
# Единый знаменатель охвата (``metric_schedule``) стадии YouTube не видит:
# у него своя очередь слотов ``h0..h6``/``d`` (этот модуль). Чтобы охват
# YouTube не занижался, он считается отдельным знаменателем по собственным
# слотам и явно помечается как «вне единой очереди». Пропущенный слот (был
# обязан сняться, но снимка нет) оставляет строку в ``youtube_slot_miss``.

def _tracked_videos(conn, now: int, cfg: Any):
    """Видео под наблюдением: разобраны (``is_ai=1``) и младше ``MAX_TRACK_DAYS``.

    Сначала через представления платформы (``videos``/``video_classification``);
    если соединение ядровое и этих temp-представлений нет — прямой запрос по
    таблицам ядра (``content``/``classification``), чтобы охват работал и в
    объединённом отчёте, читающем ядро напрямую.
    """
    now = int(now)
    max_age = getattr(cfg, "MAX_TRACK_DAYS", config.MAX_TRACK_DAYS) * 86400
    try:
        return conn.execute(
            "SELECT v.video_id, v.published_at, v.is_shorts FROM videos v"
            " JOIN video_classification vc ON vc.video_id = v.video_id"
            " WHERE v.published_at IS NOT NULL AND vc.is_ai = 1"
            "   AND v.published_at <= ? AND v.published_at > ?"
            " ORDER BY v.video_id",
            (now, now - max_age)).fetchall()
    except sqlite3.OperationalError:
        return conn.execute(
            "SELECT c.external_id AS video_id,"
            " CAST(strftime('%s', c.published_at) AS INTEGER) AS published_at,"
            " c.is_short AS is_shorts"
            "  FROM content c JOIN classification cl ON cl.content_id = c.id"
            " WHERE c.platform = 'youtube' AND cl.is_ai = 1"
            "   AND c.published_at IS NOT NULL"
            "   AND CAST(strftime('%s', c.published_at) AS INTEGER) <= ?"
            "   AND CAST(strftime('%s', c.published_at) AS INTEGER) > ?"
            " ORDER BY c.external_id",
            (now, now - max_age)).fetchall()


def expected_slots(published_at, now, is_shorts, cfg: Any = config):
    """Ожидаемые к ``now`` слоты видео: список ``(bucket, due_epoch)``.

    Свежие видео (моложе ``FRESH_MAX_HOURS``) — слоты ``FRESH_SLOT_OFFSETS``,
    срок которых уже наступил; для шортсов смещения делятся на
    ``SHORTS_FRESH_SLOT_SPEDUP`` (та же логика, что в :func:`plan_snapshots`).
    Видео старше — суточные слоты ``d`` с шагом 12 ч (2-10 дней) или 24 ч.
    """
    pub = int(published_at)
    now = int(now)
    age = now - pub
    if age < 0:
        return []
    if age >= getattr(cfg, "MAX_TRACK_DAYS", config.MAX_TRACK_DAYS) * 86400:
        return []
    out: list[tuple[str, int]] = []
    if age < cfg.FRESH_MAX_HOURS * 3600:
        speedup = (float(getattr(cfg, "SHORTS_FRESH_SLOT_SPEDUP",
                            config.SHORTS_FRESH_SLOT_SPEDUP))
                   if is_shorts == 1 else 1.0)
        for bucket, offset in sorted(cfg.FRESH_SLOT_OFFSETS.items(),
                                     key=lambda kv: kv[1]):
            due = pub + int(offset / speedup)
            if now >= due:
                out.append((bucket, due))
        return out
    twice = int(getattr(cfg, "DAILY_TWICE_INTERVAL_SECONDS",
                        config.DAILY_TWICE_INTERVAL_SECONDS))
    daily = int(cfg.DAILY_INTERVAL_SECONDS)
    long_days = getattr(cfg, "LONG_LIVED_MAX_DAYS",
                        config.LONG_LIVED_MAX_DAYS) * 86400
    interval = twice if age < long_days else daily
    t = pub + cfg.FRESH_MAX_HOURS * 3600
    while t <= now:
        out.append((cfg.SLOT_DAILY, t))
        t += interval
    return out


def _daily_interval(age: int, cfg: Any) -> int:
    long_days = getattr(cfg, "LONG_LIVED_MAX_DAYS",
                        config.LONG_LIVED_MAX_DAYS) * 86400
    return (int(getattr(cfg, "DAILY_TWICE_INTERVAL_SECONDS",
                        config.DAILY_TWICE_INTERVAL_SECONDS))
            if age < long_days else int(cfg.DAILY_INTERVAL_SECONDS))


def _captured_slots(conn) -> dict[str, dict]:
    """Снятые слоты по видео: ``{video_id: {"fresh": set, "daily": [epoch]}}``."""
    captured: dict[str, dict] = {}
    for row in conn.execute(
            "SELECT c.external_id AS video_id, m.bucket AS bucket,"
            " CAST(strftime('%s', m.captured_at) AS INTEGER) AS cap"
            "  FROM metric_snapshot m JOIN content c ON c.id = m.content_id"
            " WHERE c.platform = 'youtube' AND m.bucket IS NOT NULL"):
        entry = captured.setdefault(row["video_id"],
                                    {"fresh": set(), "daily": []})
        if row["bucket"] == config.SLOT_DAILY:
            entry["daily"].append(int(row["cap"]))
        else:
            entry["fresh"].add(row["bucket"])
    return captured


def missed_slots(conn, now: int | None = None, cfg: Any = config):
    """Слоты под наблюдением, которые были обязаны сняться, но снимка нет.

    Возвращает отсортированный список ``(video_id, bucket, due_epoch)``.
    """
    now = config.now_ts() if now is None else int(now)
    captured = _captured_slots(conn)
    missing: list[tuple[str, str, int]] = []
    for v in _tracked_videos(conn, now, cfg):
        video_id = v["video_id"]
        age = now - int(v["published_at"])
        cap = captured.get(video_id, {"fresh": set(), "daily": []})
        fresh_caps = cap["fresh"]
        daily_caps = sorted(cap["daily"])
        interval = _daily_interval(age, cfg)
        for bucket, due in expected_slots(v["published_at"], now, v["is_shorts"], cfg):
            if bucket == cfg.SLOT_DAILY:
                ok = any(due <= c < due + interval for c in daily_caps)
            else:
                ok = bucket in fresh_caps
            if not ok:
                missing.append((video_id, bucket, due))
    missing.sort()
    return missing


def record_missed_slots(conn, now: int | None = None, cfg: Any = config) -> int:
    """Записать пропущенные слоты в журнал (идемпотентно). Возвращает число строк.

    ``INSERT OR IGNORE`` по PK ``(video_id, bucket, due_at)``: повторный прогон
    не плодит строк и не «оживляет» уже записанный пропуск.
    """
    now = config.now_ts() if now is None else int(now)
    ts = timeutil.iso_now()
    missing = missed_slots(conn, now, cfg)
    inserted = 0
    for video_id, bucket, due in missing:
        cur = conn.execute(
            "INSERT OR IGNORE INTO youtube_slot_miss"
            "(video_id, bucket, due_at, detected_at) VALUES (?,?,?,?)",
            (video_id, bucket, int(due), ts))
        inserted += cur.rowcount
    conn.commit()
    return inserted


def slot_coverage(conn, now: int | None = None, cfg: Any = config) -> dict:
    """Охват YouTube по ЕГО слотам (отдельный знаменатель).

    ``slots_expected`` — сколько слотов уже обязаны были сняться; ``slots_missed``
    — сколько из них пропущено (и лежит в журнале); ``coverage`` — доля снятых.
    Поле ``queue='own_slots'`` и ``note`` явно говорят, что видео вне единой
    очереди ``metric_schedule`` (в неё его стадии намеренно не кладутся).
    """
    now = config.now_ts() if now is None else int(now)
    videos = _tracked_videos(conn, now, cfg)
    expected = sum(len(expected_slots(v["published_at"], now, v["is_shorts"], cfg))
                   for v in videos)
    missing = len(missed_slots(conn, now, cfg))
    captured = max(0, expected - missing)
    return {
        "queue": "own_slots",
        "note": "вне единой очереди metric_schedule (слоты h0..h6/d)",
        "videos": len(videos),
        "slots_expected": expected,
        "slots_captured": captured,
        "slots_missed": missing,
        "coverage": (captured / expected) if expected else None,
    }
