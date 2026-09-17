"""Расписание замеров: кого и в какие слоты мерить прямо сейчас (этап 4, часть 2).

Слоты:
- h0..h6 — свежие видео (моложе FRESH_MAX_HOURS), шаг из FRESH_SLOT_OFFSETS;
- d — суточный слот: дважды в сутки для видео 2-10 дней, раз в сутки для более старых.

Повторно слот не планируется: замер (video_id, bucket) уже есть — пропуск.
Видео старше MAX_TRACK_DAYS из плана выпадает полностью.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

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


def _published_at_map(conn) -> dict[str, int | None]:
    return {
        row["video_id"]: row["published_at"]
        for row in conn.execute("SELECT video_id, published_at FROM videos")
    }


# TODO(debt-D-47): архивные видео не получают метрик (план по возрастным
# корзинам) — добор --fill-gap постепенно закрывает 1-осевые; см. TECH-DEBT.md.
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
    return summary
