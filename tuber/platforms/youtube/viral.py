"""Композитный индекс виральности (ТЗ виральности, часть 1).

Модуль считает по ``video_scores`` один сводный показатель «насколько видео
выбивается из нормы своего канала, с поправкой на свежесть». Оси берутся
только те, что у видео РЕАЛЬНО есть (неизвестное остаётся неизвестным):

- выброс по просмотрам — ``outlier_score`` (просмотры к медиане канала);
- выброс по лайкам — ``likes_per_1000`` к медиане ``likes_per_1000`` канала;
- выброс по комментариям — ``comment_velocity`` к медиане канала;
- упаковка — ``packaging_score`` к медиане ``packaging_score`` канала.

База сравнения — МЕДИАНА того же показателя по видео этого канала за
``config.VIRAL_CHANNEL_WINDOW_DAYS`` дней: один выброс не сдвигает медиану так,
как сдвинул бы среднее.

Честность важнее полноты:
- меньше ``config.VIRAL_MIN_AXES`` осей — ``viral_index = NULL``, а не ноль и не
  среднее по одной оси (п.1.3);
- лайки РАВНЫ нулю — это «реакции нет»: в честный топ такие видео не идут,
  отчёт показывает их отдельным блоком. Ноль лайков не может быть осью-отношением
  (логарифм нуля не определён), поэтому ось лайков у них опускается;
- лайки НЕ СОБРАНЫ (NULL) — ось ОТСУТСТВУЕТ: это не ноль и не мусор, просто по
  этой оси видео не ранжируется;
- замеры для осей скорости комментариев и просмотров уже отфильтрованы в
  ``video_scores`` правилом ``interval_quality='ok' AND is_anomaly=0``
  (образец — ``tuber/seo.py``), здесь это не дублируется.

Индекс убывает по свежести с периодом полураспада
``config.viral_half_life_days()`` дней; значение печатается в отчёте, чтобы
распад не был скрытой настройкой.

Известное ограничение (см. D-14 в docs/TECH-DEBT.md): ось просмотров берёт
готовый ``outlier_score`` (медиана канала за всю историю), тогда как остальные
оси нормируются на окно 90 дней; и среднее берётся по НАЛИЧНЫМ осям, поэтому
видео с меньшим числом собранных осей может оказаться выше видео с большим.
"""

from __future__ import annotations

import json
import logging
import math
from statistics import median
from typing import Any

from . import config, store as db

log = logging.getLogger(__name__)

# Имена осей индекса.
AXIS_VIEWS = "views"
AXIS_LIKES = "likes"
AXIS_COMMENTS = "comments"
AXIS_PACKAGING = "packaging"
AXES: tuple[str, ...] = (AXIS_VIEWS, AXIS_LIKES, AXIS_COMMENTS, AXIS_PACKAGING)

# Русские подписи осей (для отчёта).
AXIS_LABELS_RU = {
    AXIS_VIEWS: "просмотры",
    AXIS_LIKES: "лайки",
    AXIS_COMMENTS: "комментарии",
    AXIS_PACKAGING: "упаковка",
}

# Ось -> колонка video_scores, по которой она нормируется на медиану канала.
# У оси просмотров хранимое значение уже нормировано (outlier_score).
_AXIS_COLUMN = {
    AXIS_LIKES: "likes_per_1000",
    AXIS_COMMENTS: "comment_velocity",
    AXIS_PACKAGING: "packaging_score",
}

# Ключ в score_parts, под которым лежит разбивка индекса по осям.
VIRAL_PARTS_KEY = "viral"

# TODO(debt-D-14): индекс считается по наличным осям, а ось просмотров берёт
# outlier_score за всю историю — см. docs/TECH-DEBT.md.


def _ratio(value: Any, base: Any) -> float | None:
    """Значение к медиане базы; ноль/None/неположительная база — None.

    Ноль намеренно не даёт оси: отношение 0 нельзя взять в среднее логарифмов,
    а «нулевой рост» — это не виральность, а её отсутствие.
    """
    if value is None or base is None:
        return None
    try:
        value_f = float(value)
        base_f = float(base)
    except (TypeError, ValueError):
        return None
    if value_f <= 0 or base_f <= 0:
        return None
    return value_f / base_f


def _cap_axis(ratio: float, cap: float | None) -> tuple[float, bool]:
    """Обрезать отношение оси потолком. Возвращает (значение, обрезано ли).

    ``cap`` None или <= 0 — без потолка. Потолок нужен осям лайков и
    комментариев: на микровыборке (1 лайк на 3 просмотра) отношение доходит до
    ×116 и одной осью перевешивает весь индекс. Ось просмотров НЕ обрезается:
    большое отношение там означает реально большие просмотры.
    """
    if cap is None or float(cap) <= 0:
        return float(ratio), False
    cap_f = float(cap)
    if float(ratio) > cap_f:
        return cap_f, True
    return float(ratio), False


def latest_score_rows(conn) -> list[Any]:
    """Последняя строка видеоскор на каждое видео + метаданные видео.

    Один срез на видео: PK ``(video_id, computed_at)`` гарантирует
    единственность, поэтому JOIN по MAX(computed_at) не размножает строки.
    """
    return conn.execute(
        """
        SELECT vs.video_id       AS video_id,
               vs.computed_at    AS computed_at,
               vs.outlier_score  AS outlier_score,
               vs.likes_per_1000 AS likes_per_1000,
               vs.comment_velocity AS comment_velocity,
               vs.packaging_score  AS packaging_score,
               vs.score_parts      AS score_parts,
               v.channel_id      AS channel_id,
               v.published_at    AS published_at,
               v.is_shorts       AS is_shorts
        FROM video_scores vs
        JOIN videos v ON v.video_id = vs.video_id
        JOIN (
            SELECT video_id, MAX(computed_at) AS mc
            FROM video_scores GROUP BY video_id
        ) t ON t.video_id = vs.video_id AND t.mc = vs.computed_at
        """
    ).fetchall()


def channel_medians(
    rows,
    now: int,
    window_days: int = config.VIRAL_CHANNEL_WINDOW_DAYS,
    min_base: int = config.VIRAL_MIN_CHANNEL_BASE,
) -> dict[tuple[str, str], float]:
    """Медианы показателей по каналам за окно свежести.

    Ключ — ``(channel_id, ось)``. Канал берёт только видео, опубликованные не
    раньше ``now - window_days`` суток. Медиана ненадёжна на выборке меньше
    ``min_base`` видео и при неположительном значении — такие ключи опускаются.
    """
    per_key: dict[tuple[str, str], list[float]] = {}
    cutoff = int(now) - int(window_days) * 86400
    for row in rows:
        channel_id = row["channel_id"]
        published_at = row["published_at"]
        if not channel_id or published_at is None or int(published_at) < cutoff:
            continue
        for axis, column in _AXIS_COLUMN.items():
            value = row[column]
            if value is None:
                continue
            per_key.setdefault((channel_id, axis), []).append(float(value))
    medians: dict[tuple[str, str], float] = {}
    for key, values in per_key.items():
        if len(values) < int(min_base):
            continue
        value = float(median(values))
        if value > 0:
            medians[key] = value
    return medians


def axes_for(
    row,
    medians: dict[tuple[str, str], float],
    cap: float | None = None,
) -> dict[str, Any]:
    """Оси видео, которые реально известны, и статус лайков.

    Возвращает ``{"axes": {ось: отношение}, "likes_zero": bool,
    "likes_null": bool, "capped": [ось, ...]}``. Ось попадает в ``axes`` только
    если её значение положительно и есть медиана канала.

    ``cap`` — потолок отношения для осей лайков и комментариев (None — взять
    ``config.viral_axis_cap()``; <= 0 — без потолка). Ось просмотров и упаковки
    потолком не режется. ``capped`` перечисляет оси, у которых отношение было
    больше потолка, — чтобы отчёт мог показать счётчик.
    """
    if cap is None:
        cap = config.viral_axis_cap()
    channel_id = row["channel_id"]
    axes: dict[str, float] = {}
    capped: list[str] = []

    outlier = row["outlier_score"]
    if outlier is not None and float(outlier) > 0:
        axes[AXIS_VIEWS] = float(outlier)

    likes_per_1000 = row["likes_per_1000"]
    likes_zero = likes_per_1000 is not None and float(likes_per_1000) == 0.0
    likes_null = likes_per_1000 is None
    if not likes_zero:
        ratio = _ratio(likes_per_1000, medians.get((channel_id, AXIS_LIKES)))
        if ratio is not None:
            value, was_capped = _cap_axis(ratio, cap)
            axes[AXIS_LIKES] = value
            if was_capped:
                capped.append(AXIS_LIKES)

    for axis in (AXIS_COMMENTS, AXIS_PACKAGING):
        ratio = _ratio(row[_AXIS_COLUMN[axis]], medians.get((channel_id, axis)))
        if ratio is None:
            continue
        if axis == AXIS_COMMENTS:
            value, was_capped = _cap_axis(ratio, cap)
            axes[axis] = value
            if was_capped:
                capped.append(axis)
        else:
            axes[axis] = ratio

    return {
        "axes": axes,
        "likes_zero": likes_zero,
        "likes_null": likes_null,
        "capped": capped,
    }


def freshness_decay(age_days: float, half_life_days: float) -> float:
    """Множитель распада: 1.0 в момент публикации, 0.5 через период полураспада."""
    if half_life_days is None or float(half_life_days) <= 0:
        return 1.0
    age = max(0.0, float(age_days))
    return float(0.5 ** (age / float(half_life_days)))


def index_from_axes(
    axes: dict[str, float],
    age_days: float,
    half_life_days: float,
    min_axes: int = config.VIRAL_MIN_AXES,
) -> float | None:
    """Композитный индекс: среднее геометрическое осей, убывающее по свежести.

    Меньше ``min_axes`` осей — None. Среднее геометрическое (а не
    арифметическое) выбрано потому, что оси — отношения «во сколько раз выше
    нормы»: оно устойчиво к выбросам и не даёт одной оси перевесить остальные.
    """
    if len(axes) < int(min_axes):
        return None
    values = [float(v) for v in axes.values()]
    if any(v <= 0 for v in values):
        return None
    geometric = math.exp(sum(math.log(v) for v in values) / len(values))
    return float(geometric) * freshness_decay(age_days, half_life_days)


def compute(conn, now: int | None = None, cfg: Any = config) -> dict[str, dict[str, Any]]:
    """Индекс и разбивка по осям для всех видео одним проходом.

    Не пишет в базу. Ключ — ``video_id``; значение: ``index``, ``axes``,
    ``likes_zero``, ``likes_null``, ``age_days``, ``computed_at``, ``half_life_days``.
    """
    moment = int(config.now_ts() if now is None else now)
    half_life = config.viral_half_life_days(cfg)
    cap = config.viral_axis_cap(cfg)
    rows = latest_score_rows(conn)
    medians = channel_medians(rows, moment)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        info = axes_for(row, medians, cap=cap)
        published_at = row["published_at"]
        if published_at is None:
            age_days = 0.0
        else:
            age_days = max(0.0, (moment - int(published_at)) / 86400.0)
        index = index_from_axes(info["axes"], age_days, half_life)
        out[row["video_id"]] = {
            "index": index,
            "axes": info["axes"],
            "likes_zero": info["likes_zero"],
            "likes_null": info["likes_null"],
            "capped": info["capped"],
            "age_days": age_days,
            "computed_at": int(row["computed_at"]),
            "score_parts": row["score_parts"],
            "half_life_days": half_life,
        }
    return out


def _merge_parts(raw: Any, viral_block: dict[str, Any]) -> str:
    """Вложить разбивку индекса в score_parts, не потеряв скор упаковки."""
    data: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (ValueError, TypeError):
            data = {}
    data[VIRAL_PARTS_KEY] = viral_block
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def refresh(conn, now: int | None = None, cfg: Any = config) -> dict[str, Any]:
    """Посчитать индекс и записать его в последнюю строку video_scores.

    Пишется ровно одна строка на видео (последний срез), история не множится.
    ``viral_index`` пишется как NULL, если осей меньше двух. Разбивка кладётся в
    ``score_parts`` под ключом ``viral``. Возвращает сводку с числами, которые
    отчёт обязан показать.
    """
    result = compute(conn, now=now, cfg=cfg)
    updates: list[tuple[str, int, float | None, str]] = []
    stats: dict[str, Any] = {
        "total": len(result),
        "indexed": 0,
        "null_index": 0,
        "likes_zero": 0,
        "likes_null": 0,
        "axis_capped": 0,
        "axes_histogram": {},
        "axis_cap": config.viral_axis_cap(cfg),
        "min_views": config.viral_min_views(cfg=cfg),
        "half_life_days": config.viral_half_life_days(cfg),
    }
    histogram: dict[int, int] = {}
    for video_id, info in result.items():
        axes = info["axes"]
        histogram[len(axes)] = histogram.get(len(axes), 0) + 1
        if info["index"] is not None:
            stats["indexed"] += 1
        else:
            stats["null_index"] += 1
        if info["likes_zero"]:
            stats["likes_zero"] += 1
        if info["likes_null"]:
            stats["likes_null"] += 1
        if info["capped"]:
            stats["axis_capped"] += 1
        block = {
            "index": round(info["index"], 6) if info["index"] is not None else None,
            "axes": {k: round(float(v), 6) for k, v in axes.items()},
            "likes_zero": bool(info["likes_zero"]),
            "capped": bool(info["capped"]),
            "capped_axes": list(info["capped"]),
            "half_life_days": info["half_life_days"],
        }
        updates.append(
            (
                video_id,
                info["computed_at"],
                info["index"],
                _merge_parts(info["score_parts"], block),
            )
        )
    stats["axes_histogram"] = {str(k): histogram[k] for k in sorted(histogram)}
    stats["rows_written"] = db.set_viral_indices(conn, updates)
    log.info(
        "виральность: индекс посчитан у %d, NULL у %d (осей<2), лайки=0 %d, "
        "лайки не собраны %d, ось обрезана потолком ×%g у %d",
        stats["indexed"], stats["null_index"], stats["likes_zero"], stats["likes_null"],
        stats["axis_cap"], stats["axis_capped"],
    )
    return stats
