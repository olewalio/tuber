"""Витрина отчёта Tuber_OS (этап 6).

Модуль читает ТОЛЬКО базу данных: сеть здесь не используется ни разу.
Все функции отдают честные None/пустые списки, если данных ещё нет.
Выдумывать цифры запрещено (METHODOLOGY.md, RESEARCH-MECHANICS.md):
неизвестное остаётся неизвестным, и отчёт прямо пишет, сколько замеров
накоплено и сколько нужно.

Главный принцип ранжирования — скорость (views_per_day), а не абсолютные
просмотры: 1 млн просмотров при 2 в час это мёртвый груз, 50 тыс. при
150 в час — живой тренд.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from statistics import median
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from . import config, viral
from tuber.core import urls

# --- пороги (RESEARCH-MECHANICS.md, раздел 1) ------------------------------

# Ниже этого outlier видео в отчёт не берём.
DARK_HORSE_MIN_OUTLIER = 3.0
# 10x+ — аномалия, ручная проверка на накрутку.
SUSPICIOUS_OUTLIER = 10.0
# 5x-10x — приоритет разбора.
PRIORITY_OUTLIER = 5.0
# Меньше этого числа видео в выборке — медиану не считаем и outlier не выдумываем.
# Выборка всегда одного формата: смешивать шортсы и полные видео нельзя.
MIN_CHANNEL_VIDEOS = 5
# Минимальный интервал между замерами для скорости роста комментариев.
MIN_COMMENT_INTERVAL_SECONDS = 600
# Для скорости нужны как минимум два замера на видео.
REQUIRED_SNAPSHOTS_PER_VIDEO = 2
# Порог «миллионник» для аудита видео с несобранными лайками (п.1.5 ТЗ).
# Это отдельный порог отчёта, не стоимость: совпадение цифры с ценами за
# миллион токенов в classify/thumbs случайно.
MILLION_VIEWS = 1_000_000

MSK = ZoneInfo("Europe/Moscow")

WEEKDAYS_RU = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


# --- общие помощники -------------------------------------------------------


def _median(values: Sequence[float]) -> float | None:
    """Медиана списка; пустой список — None."""
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return float(median(clean))


def _per_1000(part: Any, total: Any) -> float | None:
    """Значение на 1000 просмотров; нет данных или деление на ноль — None."""
    if part is None or total is None:
        return None
    total = float(total)
    if total <= 0:
        return None
    return float(part) * 1000.0 / total


def _video_url(video_id: str) -> str:
    # Единый хелпер синтеза ссылок (D-45): не дублируем правило YouTube.
    return urls.content_url("youtube", video_id)


def _is_russian_expr() -> str:
    """SQL-условие «русское видео»: язык ru или канал помечен русским."""
    return "(vc.lang = 'ru' OR COALESCE(c.is_russian, 0) = 1)"


def _latest_views(conn, video_id: str) -> float | None:
    """Просмотры видео из последнего замера с непустым views."""
    row = conn.execute(
        """
        SELECT views FROM snapshots
        WHERE video_id = ? AND views IS NOT NULL
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()
    if row is None or row["views"] is None:
        return None
    return float(row["views"])


def _video_format(is_shorts: Any) -> str:
    """Формат видео по фолгу: 'short' или 'long'. NULL читается как полное."""
    return "short" if is_shorts == 1 else "long"


def _clean_format(fmt: str | None) -> str | None:
    """'short'/'long' как есть, всё остальное (в т.ч. None) — None."""
    return fmt if fmt in ("short", "long") else None


def _format_where(fmt: str | None, alias: str = "v") -> str | None:
    """SQL-условие формата или None для смешанной выборки.

    long включает NULL: до миграции старый пул мог не знать формата.
    """
    fmt = _clean_format(fmt)
    if fmt == "short":
        return f"{alias}.is_shorts = 1"
    if fmt == "long":
        return f"COALESCE({alias}.is_shorts, 0) = 0"
    return None


def _channel_views(conn, channel_id: str, fmt: str | None = None) -> list[float]:
    """Последние просмотры видео канала для базы сравнения.

    Видео с is_ai = 0 в медиану НЕ попадают: иначе базу размывают ролики,
    не относящиеся к ИИ-контуру. При заданном fmt берутся только видео
    того же формата (is_shorts=1 для short, is_shorts=0 для long).
    """
    where = ["v.channel_id = ?", "(vc.is_ai IS NULL OR vc.is_ai = 1)"]
    params: list[Any] = [channel_id]
    fmt_where = _format_where(fmt, "v")
    if fmt_where:
        where.append(fmt_where)
    rows = conn.execute(
        f"""
        SELECT s.views AS views
        FROM videos v
        JOIN snapshots s ON s.id = (
            SELECT id FROM snapshots
            WHERE video_id = v.video_id AND views IS NOT NULL
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
        )
        LEFT JOIN video_classification vc ON vc.video_id = v.video_id
        WHERE {' AND '.join(where)}
        """,
        params,
    ).fetchall()
    return [float(r["views"]) for r in rows if r["views"] is not None]


def _outlier_from(views: float | None, channel_views: Sequence[float]) -> float | None:
    """Outlier = просмотры / медиана выборки. Мало данных или ноль — None."""
    if views is None:
        return None
    if len(channel_views) < MIN_CHANNEL_VIDEOS:
        return None
    med = _median(channel_views)
    if not med or med <= 0:
        return None
    return float(views) / med


def outlier_score(conn, video_id: str, fmt: str | None = None) -> float | None:
    """Во сколько раз видео обошло медиану просмотров своего формата.

    Правила:
    - база — медиана просмотров видео того же канала И того же формата
      (short/long) — смешивать их нельзя;
    - если fmt не задан, формат берётся у самого видео;
    - видео с is_ai = 0 в медиану не включаются;
    - меньше MIN_CHANNEL_VIDEOS (5) видео с данными — None, не выдумываем.
    """
    vrow = conn.execute(
        "SELECT channel_id, is_shorts FROM videos WHERE video_id = ?", (video_id,)
    ).fetchone()
    if vrow is None:
        return None
    channel_id = vrow["channel_id"]
    if not channel_id:
        return None
    effective = _clean_format(fmt) or _video_format(vrow["is_shorts"])
    views = _latest_views(conn, video_id)
    return _outlier_from(views, _channel_views(conn, channel_id, effective))


def _outlier_map(conn, fmt: str | None = None) -> dict[str, float | None]:
    """Outlier для всех видео одним проходом (для тёмных лошадок).

    Медиана считается отдельно по паре (канал, формат), поэтому шортсы
    с сотнями тысяч просмотров не раздувают outlier полного видео.
    Если fmt задан, в результат попадают только видео этого формата.
    """
    target = _clean_format(fmt)
    target_flag = 1 if target == "short" else (0 if target == "long" else None)
    rows = conn.execute(
        """
        SELECT v.video_id AS video_id, v.channel_id AS channel_id,
               v.is_shorts AS is_shorts,
               s.views AS views, vc.is_ai AS is_ai
        FROM videos v
        JOIN snapshots s ON s.id = (
            SELECT id FROM snapshots
            WHERE video_id = v.video_id AND views IS NOT NULL
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
        )
        LEFT JOIN video_classification vc ON vc.video_id = v.video_id
        WHERE s.views IS NOT NULL
        """
    ).fetchall()

    per_key: dict[tuple[str, int], list[float]] = {}
    video_views: dict[str, float] = {}
    channel_of: dict[str, str | None] = {}
    format_of: dict[str, int] = {}
    for r in rows:
        vid = r["video_id"]
        flag = 1 if r["is_shorts"] == 1 else 0
        video_views[vid] = float(r["views"])
        channel_of[vid] = r["channel_id"]
        format_of[vid] = flag
        if r["is_ai"] == 0:
            continue
        ch = r["channel_id"]
        if not ch:
            continue
        per_key.setdefault((ch, flag), []).append(float(r["views"]))

    medians: dict[tuple[str, int], float] = {}
    for key, vals in per_key.items():
        if len(vals) >= MIN_CHANNEL_VIDEOS:
            med = _median(vals)
            if med and med > 0:
                medians[key] = med

    out: dict[str, float | None] = {}
    for vid, views in video_views.items():
        if target_flag is not None and format_of[vid] != target_flag:
            continue
        ch = channel_of.get(vid)
        med = medians.get((ch, format_of[vid])) if ch else None
        out[vid] = (views / med) if med else None
    return out


def _snapshot_stats(conn) -> dict[str, int]:
    """Разбивка замеров: всего, видео, пригодные/короткие/первый, со скоростью.

    'ok' — интервал достаточен, скорость посчитана (только такие замеры
    попадают в ранжирование). 'short' — интервал короче порога, скорость
    обнулена. 'first' — первый замер видео, пары нет.
    """
    total = int(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
    videos = int(
        conn.execute("SELECT COUNT(DISTINCT video_id) FROM snapshots").fetchone()[0]
    )
    ok = int(
        conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE interval_quality='ok'"
        ).fetchone()[0]
    )
    short = int(
        conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE interval_quality='short'"
        ).fetchone()[0]
    )
    first = int(
        conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE interval_quality='first'"
        ).fetchone()[0]
    )
    with_speed = int(
        conn.execute(
            "SELECT COUNT(DISTINCT video_id) FROM snapshots "
            "WHERE interval_quality='ok' AND views_per_day IS NOT NULL"
        ).fetchone()[0]
    )
    return {
        "total": total,
        "videos": videos,
        "ok": ok,
        "short": short,
        "first": first,
        "with_speed": with_speed,
    }


def _no_data_line(conn, need: int = REQUIRED_SNAPSHOTS_PER_VIDEO) -> str:
    """Честная строка про отсутствие данных с реальным счётчиком замеров."""
    stats = _snapshot_stats(conn)
    return (
        f"Нет данных: нужно {need} замера на видео, "
        f"накоплено замеров: {stats['total']} (видео с замером: {stats['videos']}; "
        f"пригодны для скорости: {stats['ok']}, слишком короткие: {stats['short']}, "
        f"без пары (первый замер): {stats['first']}; "
        f"видео со скоростью: {stats['with_speed']})."
    )


def _parse_tags(raw: Any) -> list[str]:
    """Теги видео из JSON-строки (или списка). Мусор — пустой список."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t) for t in raw if str(t).strip()]
    text = str(raw).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return [str(t) for t in data if str(t).strip()]
    return []


_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_NUM_RE = re.compile(r"\d")


def _title_features(title: str) -> dict[str, Any]:
    """Признаки заголовка для SEO-сборника."""
    title = title or ""
    return {
        "title_length": len(title),
        "title_words": len(title.split()),
        "title_has_number": bool(_NUM_RE.search(title)),
        "title_has_year": bool(_YEAR_RE.search(title)),
    }


def _msk_parts(published_at: Any) -> tuple[int | None, int | None, str | None]:
    """Час и день недели публикации по Москве."""
    if published_at is None:
        return None, None, None
    dt = datetime.fromtimestamp(int(published_at), tz=MSK)
    return dt.hour, dt.weekday(), WEEKDAYS_RU[dt.weekday()]


# --- 1. Общий тренд --------------------------------------------------------


def rising(conn, days: int = 10, limit: int = 50, lang: str | None = None,
           fmt: str | None = None) -> list[dict[str, Any]]:
    """Видео с is_ai = 1, у которых есть свежий замер со скоростью.

    days — окно, за которое замер считается актуальным.
    lang='ru' — только русские; lang='world' — все кроме русских.
    fmt='short'/'long' — только этот формат; None — оба потока.
    Сортировка по views_per_day убыв.
    """
    now = int(config.now_ts())
    cutoff = now - int(days) * 86400

    where = ["vc.is_ai = 1", "s.captured_at >= ?", "s.interval_quality = 'ok'"]
    params: list[Any] = [cutoff]
    if lang == "ru":
        where.append(_is_russian_expr())
    elif lang == "world":
        where.append(f"NOT {_is_russian_expr()}")
    fmt_where = _format_where(fmt, "v")
    if fmt_where:
        where.append(fmt_where)

    sql = f"""
        SELECT
            s.video_id           AS video_id,
            v.title              AS title,
            vc.title_ru          AS title_ru,
            c.title              AS channel_title,
            c.subscriber_count   AS subscriber_count,
            s.views              AS views,
            s.views_per_day      AS views_per_day,
            s.views_per_hour     AS views_per_hour,
            s.likes              AS likes,
            s.comments           AS comments,
            vc.topic             AS topic,
            v.published_at       AS published_at,
            v.thumbnail_url      AS thumbnail_url
        FROM snapshots s
        JOIN (
            SELECT MAX(id) AS mid FROM snapshots
            WHERE interval_quality = 'ok' AND views_per_day IS NOT NULL
            GROUP BY video_id
        ) t ON t.mid = s.id
        JOIN videos v ON v.video_id = s.video_id
        JOIN video_classification vc ON vc.video_id = s.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE {' AND '.join(where)}
        ORDER BY s.views_per_day DESC
        LIMIT ?
    """
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    items = [_rising_item(r) for r in rows]
    if items:
        omap = _outlier_map(conn, fmt)
        for it in items:
            it["outlier"] = omap.get(it["video_id"])
    return items


def _rising_item(r) -> dict[str, Any]:
    """Строка БД -> позиция отчёта."""
    return {
        "video_id": r["video_id"],
        "title": r["title"],
        "title_ru": r["title_ru"] or r["title"],
        "channel_title": r["channel_title"],
        "subscriber_count": r["subscriber_count"],
        "views": r["views"],
        "views_per_day": r["views_per_day"],
        "views_per_hour": r["views_per_hour"],
        "likes": r["likes"],
        "comments": r["comments"],
        "likes_per_1000": _per_1000(r["likes"], r["views"]),
        "comments_per_1000": _per_1000(r["comments"], r["views"]),
        "outlier": None,
        "topic": r["topic"],
        "published_at": r["published_at"],
        "url": _video_url(r["video_id"]),
    }


# --- 1b. Топ по индексу виральности (ТЗ виральности, часть 2) ---------------


def _viral_axes(raw: Any) -> tuple[dict[str, float], dict[str, Any] | None]:
    """Разбивка индекса по осям из score_parts['viral'].

    score_parts хранит ещё и разбивку скора упаковки; здесь читается вложенный
    блок ``viral``. Мусор и отсутствие блока дают пустой словарь осей, а не
    ошибку: отчёт не должен падать из-за формата JSON.
    """
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    block = data.get(viral.VIRAL_PARTS_KEY)
    if not isinstance(block, dict):
        return {}, None
    raw_axes = block.get("axes")
    axes: dict[str, float] = {}
    if isinstance(raw_axes, dict):
        for key, value in raw_axes.items():
            try:
                axes[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return axes, block


def _viral_candidate_rows(conn, days: int, fmt: str | None, topic: str | None):
    """Видео окна роста с их последним индексом виральности.

    Тема фильтруется по ``video_classification.topic`` (реальное поле темы), а
    не по ``videos.primary_topic``: последнее пусто у всех видео и молча даёт
    ноль записей. Роль ``s`` — последний пригодный для скорости замер.
    """
    now = int(config.now_ts())
    cutoff = now - int(days) * 86400
    where = ["vc.is_ai = 1", "s.captured_at >= ?", "s.interval_quality = 'ok'"]
    params: list[Any] = [cutoff]
    fmt_where = _format_where(fmt, "v")
    if fmt_where:
        where.append(fmt_where)
    if topic:
        where.append("vc.topic = ?")
        params.append(topic)
    sql = f"""
        SELECT
            s.video_id AS video_id, s.views AS views,
            s.views_per_day AS views_per_day, s.likes AS likes,
            s.comments AS comments,
            v.title AS title, v.published_at AS published_at,
            vc.title_ru AS title_ru, vc.topic AS topic,
            c.title AS channel_title, c.subscriber_count AS subscriber_count,
            vs.viral_index AS viral_index, vs.score_parts AS score_parts
        FROM snapshots s
        JOIN (
            SELECT MAX(id) AS mid FROM snapshots
            WHERE interval_quality = 'ok' AND views_per_day IS NOT NULL
            GROUP BY video_id
        ) t ON t.mid = s.id
        JOIN videos v ON v.video_id = s.video_id
        JOIN video_classification vc ON vc.video_id = s.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        LEFT JOIN video_scores vs ON vs.video_id = s.video_id
            AND vs.computed_at = (
                SELECT MAX(computed_at) FROM video_scores WHERE video_id = s.video_id
            )
        WHERE {' AND '.join(where)}
    """
    return conn.execute(sql, params).fetchall()


def _viral_item(r) -> dict[str, Any]:
    """Строка БД -> позиция топа по индексу, с сырыми метриками справочно."""
    axes, block = _viral_axes(r["score_parts"])
    likes = r["likes"]
    return {
        "video_id": r["video_id"],
        "title": r["title"],
        "title_ru": r["title_ru"] or r["title"],
        "channel_title": r["channel_title"],
        "subscriber_count": r["subscriber_count"],
        "views": r["views"],
        "views_per_day": r["views_per_day"],
        "likes": likes,
        "comments": r["comments"],
        "likes_per_1000": _per_1000(likes, r["views"]),
        "comments_per_1000": _per_1000(r["comments"], r["views"]),
        "viral_index": r["viral_index"],
        "axes": axes,
        "capped": bool(block.get("capped")) if isinstance(block, dict) else False,
        "likes_zero": likes == 0,
        "likes_null": likes is None,
        "topic": r["topic"],
        "published_at": r["published_at"],
        "url": _video_url(r["video_id"]),
    }


def _viral_items(conn, days: int, fmt: str | None, topic: str | None) -> list[dict[str, Any]]:
    return [
        _viral_item(r)
        for r in _viral_candidate_rows(conn, days, fmt, topic)
    ]


def _honest_candidates(conn, days: int, fmt: str | None,
                       topic: str | None) -> list[dict[str, Any]]:
    """Кандидаты честного топа: есть индекс и лайки не равны нулю.

    Видео с NULL-индексом (меньше двух осей) в топ не идут, видео с лайками = 0
    показывает отдельный блок «БЕЗ РЕАКЦИЙ». Показной порог здесь ещё НЕ
    применён: он нужен и блоку «МАЛАЯ ВЫБОРКА».
    """
    items = _viral_items(conn, days, fmt, topic)
    return [
        it for it in items
        if it["viral_index"] is not None and not it["likes_zero"]
    ]


def _by_index_desc(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сортировка позиций топа: индекс убывает, при равенстве — скорость."""
    return sorted(
        items,
        key=lambda it: (-it["viral_index"], -(it["views_per_day"] or 0)),
    )


def viral_top(conn, days: int = 10, limit: int = 20, fmt: str | None = None,
              topic: str | None = None) -> list[dict[str, Any]]:
    """Честный топ по индексу виральности.

    Ранжируются только видео с индексом (NULL — не в топ). Видео с лайками,
    равными нулю, исключены: реакции нет, для них отдельный блок
    (:func:`viral_likes_zero`). Видео с несобранными лайками (NULL) остаются в
    топе: эта ось у них просто отсутствует, это не мусор.

    Видео ниже порога показов (``config.viral_min_views(fmt)``) в честный топ
    не идут: на микровыборке ось реакций раздувается и обгоняет реальные топа.
    Отсечённые не теряются — их показывает :func:`viral_small_sample`.
    """
    min_views = config.viral_min_views(fmt)
    honest = [
        it for it in _honest_candidates(conn, days, fmt, topic)
        if (it["views"] or 0) >= min_views
    ]
    return _by_index_desc(honest)[:limit]


def viral_small_sample(conn, days: int = 10, limit: int = 20,
                       fmt: str | None = None,
                       topic: str | None = None) -> list[dict[str, Any]]:
    """Видео ниже порога показов — «малая выборка», не идут в честный топ.

    Это ровно те кандидаты, что прошли бы в честный топ по индексу, но у них
    меньше ``config.viral_min_views(fmt)`` просмотров: микровыборка раздувает
    ось реакций. Показываем их отдельным блоком, чтобы отсечение не выглядело
    пропажей. Сортировка та же — по индексу.
    """
    min_views = config.viral_min_views(fmt)
    small = [
        it for it in _honest_candidates(conn, days, fmt, topic)
        if (it["views"] or 0) < min_views
    ]
    return _by_index_desc(small)[:limit]


def viral_small_sample_count(conn, days: int = 10, fmt: str | None = None,
                             topic: str | None = None) -> int:
    """Сколько всего видео отсечено порогом показов (не только показанных)."""
    min_views = config.viral_min_views(fmt)
    return sum(
        1 for it in _honest_candidates(conn, days, fmt, topic)
        if (it["views"] or 0) < min_views
    )


def viral_likes_zero(conn, days: int = 10, limit: int = 10,
                     fmt: str | None = None, topic: str | None = None) -> list[dict[str, Any]]:
    """Видео с лайками ровно 0: реакции нет, показываем отдельным блоком."""
    items = _viral_items(conn, days, fmt, topic)
    zero = [it for it in items if it["likes_zero"]]
    zero.sort(key=lambda it: (it["viral_index"] is None, -(it["viral_index"] or 0)))
    return zero[:limit]


def viral_window_stats(conn, days: int = 10, fmt: str | None = None,
                       topic: str | None = None) -> dict[str, int]:
    """Счётчики индекса на окне: всего, с индексом, NULL, ноль/нет лайков.

    ``candidates_before_topic`` добавляется, когда задан ``topic``, — чтобы
    показать, что фильтр по теме реально режет выборку (требование п.2.3).
    """
    items = _viral_items(conn, days, fmt, topic)
    min_views = config.viral_min_views(fmt)
    honest = [
        it for it in items
        if it["viral_index"] is not None and not it["likes_zero"]
    ]
    stats = {
        "candidates": len(items),
        "indexed": sum(1 for it in items if it["viral_index"] is not None),
        "null_index": sum(1 for it in items if it["viral_index"] is None),
        "likes_zero": sum(1 for it in items if it["likes_zero"]),
        "likes_null": sum(1 for it in items if it["likes_null"]),
        "million_likes_null": sum(
            1 for it in items
            if it["likes_null"] and (it["views"] or 0) > MILLION_VIEWS
        ),
        # Порог показов и счётчики отсечения (ТЗ порога показов).
        "min_views": min_views,
        "honest_candidates": len(honest),
        "honest_above_min_views": sum(
            1 for it in honest if (it["views"] or 0) >= min_views
        ),
        "below_min_views": sum(
            1 for it in honest if (it["views"] or 0) < min_views
        ),
        "axis_capped": sum(1 for it in items if it["capped"]),
    }
    if topic is not None:
        before = _viral_items(conn, days, fmt, None)
        stats["candidates_before_topic"] = len(before)
    return stats


def _axes_text(axes: dict[str, float]) -> str:
    """«просмотры ×285.53, лайки ×1.26» — вклад осей по убыванию."""
    if not axes:
        return "нет осей"
    ordered = sorted(axes.items(), key=lambda kv: kv[1], reverse=True)
    return ", ".join(
        f"{viral.AXIS_LABELS_RU.get(key, key)} ×{_fmt_float(value, 2)}"
        for key, value in ordered
    )


def _viral_line(item: dict[str, Any], with_axes: bool = True) -> str:
    """Позиция топа виральности: индекс, вклад осей и сырые метрики справочно."""
    text = (
        f"{item['title_ru']} ({item['channel_title']}, "
        f"{_fmt_int(item['subscriber_count'])} подписчиков) — "
        f"индекс {_fmt_float(item['viral_index'], 2)}, "
        f"просмотры {_fmt_int(item['views'])}, "
        f"скорость {_fmt_float(item['views_per_day'], 0)}/сутки, "
        f"лайки {_fmt_int(item['likes'])}, "
        f"лайки на 1000 {_fmt_float(item['likes_per_1000'])}"
    )
    if with_axes:
        text += f"; вклад осей: {_axes_text(item['axes'])}"
    return text


# --- 2. Тёмные лошадки -----------------------------------------------------


def dark_horses(conn, limit: int = 20, max_subs_ratio: float = 1.0,
                fmt: str | None = None) -> list[dict[str, Any]]:
    """Видео маленьких каналов, обогнавшие свой обычный уровень.

    Отбор: outlier >= 3.0 и канал меньше медианного по подписчикам.
    fmt — считать outlier только внутри одного формата.
    Пометки: outlier >= 10 — suspicious (проверка накрутки),
    5 <= outlier < 10 — priority (приоритет разбора).
    """
    subs_rows = conn.execute(
        "SELECT subscriber_count FROM channels WHERE subscriber_count IS NOT NULL"
    ).fetchall()
    med_subs = _median([r["subscriber_count"] for r in subs_rows])
    if med_subs is None:
        return []

    omap = _outlier_map(conn, fmt)
    candidates = [
        (vid, out) for vid, out in omap.items()
        if out is not None and out >= DARK_HORSE_MIN_OUTLIER
    ]
    candidates.sort(key=lambda pair: pair[1], reverse=True)

    results: list[dict[str, Any]] = []
    for vid, out in candidates:
        if len(results) >= limit:
            break
        row = conn.execute(
            """
            SELECT
                v.video_id AS video_id, v.title AS title,
                v.published_at AS published_at,
                vc.title_ru AS title_ru, vc.topic AS topic,
                c.title AS channel_title, c.subscriber_count AS subscriber_count,
                s.views AS views, s.views_per_day AS views_per_day,
                s.likes AS likes, s.comments AS comments
            FROM videos v
            LEFT JOIN video_classification vc ON vc.video_id = v.video_id
            LEFT JOIN channels c ON c.channel_id = v.channel_id
            LEFT JOIN snapshots s ON s.video_id = v.video_id
            WHERE v.video_id = ?
            ORDER BY s.captured_at DESC, s.id DESC
            LIMIT 1
            """,
            (vid,),
        ).fetchone()
        if row is None:
            continue
        subs = row["subscriber_count"]
        if subs is None or subs >= med_subs * max_subs_ratio:
            continue
        results.append({
            "video_id": vid,
            "title": row["title"],
            "title_ru": row["title_ru"] or row["title"],
            "channel_title": row["channel_title"],
            "subscriber_count": subs,
            "views": row["views"],
            "views_per_day": row["views_per_day"],
            "likes": row["likes"],
            "comments": row["comments"],
            "likes_per_1000": _per_1000(row["likes"], row["views"]),
            "comments_per_1000": _per_1000(row["comments"], row["views"]),
            "outlier": out,
            "topic": row["topic"],
            "published_at": row["published_at"],
            "url": _video_url(vid),
            "suspicious": out >= SUSPICIOUS_OUTLIER,
            "priority": PRIORITY_OUTLIER <= out < SUSPICIOUS_OUTLIER,
        })
    return results


# --- 3. Темы ---------------------------------------------------------------


def by_topic(conn, fmt: str | None = None) -> list[dict[str, Any]]:
    """Разбивка ростущих видео по темам config.TOPICS.

    fmt='short'/'long' — только этот формат. Темы без видео не выбрасываются:
    has_data=False, top пустой.
    """
    items = rising(conn, days=config.LONG_LIVED_MAX_DAYS, limit=100000, lang=None, fmt=fmt)
    grouped: dict[str, list[dict[str, Any]]] = {t: [] for t in config.TOPICS}
    for it in items:
        topic = it["topic"]
        if topic in grouped:
            grouped[topic].append(it)

    out: list[dict[str, Any]] = []
    for topic in config.TOPICS:
        group = sorted(
            grouped[topic],
            key=lambda x: (x["views_per_day"] is None, -(x["views_per_day"] or 0)),
        )
        total = sum(x["views_per_day"] or 0 for x in group)
        out.append({
            "topic": topic,
            "count": len(group),
            "total_views_per_day": total if group else None,
            "top": group[:3],
            "has_data": bool(group),
        })
    return out


# --- 4. Комментарии --------------------------------------------------------


def top_comment(conn, video_id: str) -> dict[str, Any] | None:
    """Самый залайканный комментарий видео из video_comments (или None)."""
    row = conn.execute(
        """
        SELECT author, text, likes, published_at
        FROM video_comments
        WHERE video_id=?
        ORDER BY likes DESC, published_at DESC
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def comment_leaders(conn, limit: int = 20, fmt: str | None = None) -> list[dict[str, Any]]:
    """Видео по скорости роста комментариев за последний замер.

    Скорость = delta_comments / (interval_seconds / 86400).
    Замеры с interval_seconds < 600 с не берём: слишком короткий интервал
    даёт шум, а не рост. fmt='short'/'long' — только этот формат.
    """
    fmt_where = _format_where(fmt, "v")
    extra = f" AND {fmt_where}" if fmt_where else ""
    rows = conn.execute(
        f"""
        SELECT
            s.video_id AS video_id,
            v.title AS title, vc.title_ru AS title_ru, vc.topic AS topic,
            c.title AS channel_title, c.subscriber_count AS subscriber_count,
            s.views AS views, s.likes AS likes, s.comments AS comments,
            s.delta_comments AS delta_comments,
            s.interval_seconds AS interval_seconds,
            s.captured_at AS captured_at
        FROM snapshots s
        JOIN (
            SELECT MAX(id) AS mid FROM snapshots
            WHERE delta_comments IS NOT NULL
            GROUP BY video_id
        ) t ON t.mid = s.id
        JOIN videos v ON v.video_id = s.video_id
        LEFT JOIN video_classification vc ON vc.video_id = s.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE s.interval_seconds >= ?{extra}
        ORDER BY (CAST(s.delta_comments AS REAL) / (s.interval_seconds / 86400.0)) DESC
        LIMIT ?
        """,
        (MIN_COMMENT_INTERVAL_SECONDS, int(limit)),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        interval = r["interval_seconds"]
        delta = r["delta_comments"]
        per_day = None
        if delta is not None and interval:
            per_day = float(delta) / (float(interval) / 86400.0)
        out.append({
            "video_id": r["video_id"],
            "title": r["title"],
            "title_ru": r["title_ru"] or r["title"],
            "channel_title": r["channel_title"],
            "subscriber_count": r["subscriber_count"],
            "views": r["views"],
            "likes": r["likes"],
            "comments": r["comments"],
            "delta_comments": delta,
            "interval_seconds": interval,
            "comments_per_day": per_day,
            "comments_per_1000": _per_1000(r["comments"], r["views"]),
            "likes_per_1000": _per_1000(r["likes"], r["views"]),
            "topic": r["topic"],
            "url": _video_url(r["video_id"]),
        })
    return out


# --- 5. Русский срез -------------------------------------------------------


def ru_slice(conn, limit: int = 20, fmt: str | None = None) -> list[dict[str, Any]]:
    """Русские видео: lang='ru' либо канал помечен is_russian=1.

    Смешивать с мировым срезом нельзя — это отдельный рынок.
    fmt='short'/'long' — только этот формат.
    """
    return rising(conn, days=config.LONG_LIVED_MAX_DAYS, limit=limit, lang="ru", fmt=fmt)


# --- 6. SEO-сборник --------------------------------------------------------


def seo_pack(conn, limit: int = 30, fmt: str | None = None) -> dict[str, Any]:
    """Разбор оформления верхних ростущих видео.

    Собираем только наблюдаемое: заголовок, описание, теги, длительность,
    тайминг, обложку, лайки/комментарии на 1000. CTR и удержание чужого
    видео не существуют в открытых данных и здесь не выводятся.
    fmt='short'/'long' — только один поток: у шортсов и полных разное
    оформление, общий разбор по обоим сразу бессмыслен.
    """
    items = rising(conn, days=config.LONG_LIVED_MAX_DAYS, limit=limit, lang=None, fmt=fmt)

    videos: list[dict[str, Any]] = []
    tag_counter: dict[str, int] = {}
    hour_dist: dict[int, int] = {}
    weekday_dist: dict[int, int] = {}
    shorts = 0

    for it in items:
        row = conn.execute(
            """
            SELECT description, tags, duration_seconds, is_shorts,
                   thumbnail_url, thumbnail_width, thumbnail_height, published_at
            FROM videos WHERE video_id = ?
            """,
            (it["video_id"],),
        ).fetchone()
        if row is None:
            continue
        title = it["title_ru"] or it["title"] or ""
        feats = _title_features(title)
        tags = _parse_tags(row["tags"])
        for tag in tags:
            tag_counter[tag] = tag_counter.get(tag, 0) + 1
        hour, weekday, weekday_name = _msk_parts(row["published_at"])
        if hour is not None:
            hour_dist[hour] = hour_dist.get(hour, 0) + 1
        if weekday is not None:
            weekday_dist[weekday] = weekday_dist.get(weekday, 0) + 1
        duration = row["duration_seconds"]
        if row["is_shorts"] is not None:
            is_short = row["is_shorts"] == 1
        else:
            is_short = bool(config.is_probable_shorts(duration))
        if is_short:
            shorts += 1
        videos.append({
            "video_id": it["video_id"],
            "title": it["title"],
            "title_ru": it["title_ru"],
            **feats,
            "description_head": (row["description"] or "")[:200],
            "tags": tags,
            "tags_count": len(tags),
            "duration_seconds": duration,
            "format": "Shorts" if is_short else "Видео",
            "published_hour_msk": hour,
            "published_weekday": weekday,
            "published_weekday_name": weekday_name,
            "thumbnail_url": row["thumbnail_url"],
            "thumb_width": row["thumbnail_width"],
            "thumb_height": row["thumbnail_height"],
            "views": it["views"],
            "views_per_day": it["views_per_day"],
            "likes_per_1000": it["likes_per_1000"],
            "comments_per_1000": it["comments_per_1000"],
        })

    top_tags = sorted(tag_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    total = len(videos)
    avg_title_length = (
        sum(v["title_length"] for v in videos) / total if total else None
    )
    summary = {
        "count": total,
        "avg_title_length": avg_title_length,
        "top_tags": [{"tag": t, "count": n} for t, n in top_tags],
        "by_hour": dict(sorted(hour_dist.items())),
        "by_weekday": dict(sorted(weekday_dist.items())),
        "shorts_share": (shorts / total) if total else None,
    }
    return {"videos": videos, "summary": summary}


# --- 7. Текстовый отчёт ----------------------------------------------------


def _fmt_int(value: Any) -> str:
    if value is None:
        return "нет данных"
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return "нет данных"


def _fmt_float(value: Any, digits: int = 1) -> str:
    if value is None:
        return "нет данных"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "нет данных"


def _line(item: dict[str, Any]) -> str:
    """Одна позиция отчёта в требуемом формате."""
    return (
        f"{item['title_ru']} ({item['channel_title']}, "
        f"{_fmt_int(item.get('subscriber_count'))} подписчиков) — "
        f"скорость {_fmt_float(item.get('views_per_day'), 0)} просмотров/сутки, "
        f"всего {_fmt_int(item.get('views'))}, "
        f"лайков на 1000: {_fmt_float(item.get('likes_per_1000'))}."
    )


_STREAM_TITLES = {
    "short": "## Шортсы",
    "long": "## Полные видео",
}


def _stream_section(conn, parts: list[str], days: int, fmt: str,
                    topic: str | None = None) -> None:
    """Один поток отчёта: свои топы и свои медианы, без смешивания форматов.

    ``topic`` — фильтр по ``video_classification.topic``; он применяется к топу
    виральности и блоку «без реакций», где тема реально режет выборку.
    """
    sep = "=" * 60
    parts.append("")
    parts.append(sep)
    parts.append(_STREAM_TITLES.get(fmt, f"## {fmt}"))
    parts.append(sep)

    # --- Что растёт (ранжирует индекс виральности, не сырые просмотры) ---
    parts.append("")
    parts.append(sep)
    parts.append("ЧТО РАСТЁТ (топ по индексу виральности, внутри своего формата)")
    parts.append(sep)
    stats = viral_window_stats(conn, days=days, fmt=fmt, topic=topic)
    half_life = config.viral_half_life_days()
    parts.append(
        f"Индекс виральности: период полураспада свежести "
        f"{_fmt_float(half_life)} дней. Видео на окне: {stats['candidates']}; "
        f"индекс посчитан у {stats['indexed']}, NULL у {stats['null_index']} "
        f"(меньше двух осей); лайки не собраны у {stats['likes_null']}, "
        f"реакций нет (лайки = 0) у {stats['likes_zero']}; миллионников с "
        f"несобранными лайками: {stats['million_likes_null']}."
    )
    if topic is not None:
        before = stats.get("candidates_before_topic", stats["candidates"])
        parts.append(
            f"Фильтр по теме «{topic}» (video_classification.topic): видео на окне "
            f"до фильтра {before}, после {stats['candidates']}."
        )
    if stats["candidates"] and stats["indexed"] == 0:
        parts.append(
            "ВНИМАНИЕ: индекс виральности не посчитан ни у одного видео на окне — "
            "запустите `tuber viral-refresh` и повторите отчёт."
        )
    # Порог показов и потолок оси: сколько отсечено и сколько осталось.
    top_limit = 50
    min_views = stats["min_views"]
    axis_cap = config.viral_axis_cap()
    trend = viral_top(conn, days=days, limit=top_limit, fmt=fmt, topic=topic)
    below = stats["below_min_views"]
    parts.append(
        f"Порог показов честного топа: {min_views} просмотров. Отсечено ниже "
        f"порога: {below} видео — они уходят в блок «МАЛАЯ ВЫБОРКА (не идут в "
        f"честный топ)» и показаны отдельно. Потолок отношения осей лайков и "
        f"комментариев: ×{_fmt_float(axis_cap)}; ось обрезана потолком у "
        f"{stats['axis_capped']} видео на окне (ось просмотров не ограничивается)."
    )
    parts.append(
        f"В честном топе: {len(trend)} из {top_limit} запрошенных позиций."
    )
    if len(trend) < top_limit:
        parts.append(
            f"Список короче лимита ({len(trend)} < {top_limit}) — добивать его "
            f"мелочью ниже {min_views} просмотров не будем; остальное смотрите "
            f"в блоке малой выборки."
        )
    if trend:
        for i, it in enumerate(trend, 1):
            parts.append(f"{i}. {_viral_line(it)}.")
    else:
        parts.append(_no_data_line(conn))

    # --- Малая выборка: индекс есть, но просмотров меньше порога ---
    small = viral_small_sample(conn, days=days, limit=20, fmt=fmt, topic=topic)
    if small:
        parts.append("")
        parts.append(sep)
        parts.append("МАЛАЯ ВЫБОРКА (не идут в честный топ)")
        parts.append(sep)
        parts.append(
            f"Отсечено порогом показов (< {min_views} просмотров): {below} видео; "
            f"ниже показаны {len(small)} с наибольшим индексом. На микровыборке "
            f"ось реакций раздувается (лайки на 1000 при единицах просмотров), "
            f"поэтому в честный топ такие видео не идут."
        )
        for i, it in enumerate(small, 1):
            parts.append(f"{i}. {_viral_line(it)}.")

    # --- Без реакций: лайки ровно 0, в честный топ не идут ---
    zero = viral_likes_zero(conn, days=days, limit=10, fmt=fmt, topic=topic)
    if zero:
        parts.append("")
        parts.append(sep)
        parts.append("БЕЗ РЕАКЦИЙ (лайки = 0: реакции нет, в честный топ не идут)")
        parts.append(sep)
        for i, it in enumerate(zero, 1):
            line = _viral_line(it, with_axes=False)
            if it["viral_index"] is None:
                line += "; индекс не считается (меньше двух осей)"
            parts.append(f"{i}. {line}.")

    # --- Темы ---
    parts.append("")
    parts.append(sep)
    parts.append("ТЕМЫ (куда смещается интерес)")
    parts.append(sep)
    topics = by_topic(conn, fmt=fmt)
    if any(t["has_data"] for t in topics):
        for t in topics:
            if not t["has_data"]:
                parts.append(f"- {t['topic']}: нет данных.")
                continue
            parts.append(
                f"- {t['topic']}: видео {t['count']}, "
                f"сумма скоростей {_fmt_float(t['total_views_per_day'], 0)} просмотров/сутки."
            )
            for it in t["top"]:
                parts.append(f"    * {_line(it)}")
    else:
        parts.append(_no_data_line(conn))

    # --- Что обсуждают ---
    parts.append("")
    parts.append(sep)
    parts.append("ЧТО ОБСУЖДАЮТ (рост комментариев)")
    parts.append(sep)
    comments = comment_leaders(conn, limit=20, fmt=fmt)
    if comments:
        for i, it in enumerate(comments, 1):
            parts.append(
                f"{i}. {it['title_ru']} ({it['channel_title']}) — "
                f"комментариев/сутки: {_fmt_float(it['comments_per_day'])}, "
                f"всего комментариев: {_fmt_int(it['comments'])}, "
                f"комментариев на 1000: {_fmt_float(it['comments_per_1000'])}."
            )
            best = top_comment(conn, it["video_id"])
            if best and best.get("text"):
                text = " ".join(str(best["text"]).split())
                if len(text) > 140:
                    text = text[:140]
                likes = best.get("likes")
                likes = int(likes) if isinstance(likes, (int, float)) else 0
                parts.append(
                    f"    Лучший комментарий (лайков {likes}): {text}"
                )
    else:
        parts.append(_no_data_line(conn))

    # --- Русский ютуб ---
    parts.append("")
    parts.append(sep)
    parts.append("РУССКИЙ ЮТУБ (отдельно от мирового)")
    parts.append(sep)
    ru_items = ru_slice(conn, limit=20, fmt=fmt)
    if ru_items:
        for i, it in enumerate(ru_items, 1):
            parts.append(f"{i}. {_line(it)}")
    else:
        parts.append(_no_data_line(conn))

    # --- Тёмные лошадки ---
    parts.append("")
    parts.append(sep)
    parts.append("ТЁМНЫЕ ЛОШАДКИ (маленькие каналы выше своего уровня)")
    parts.append(sep)
    horses = dark_horses(conn, limit=20, fmt=fmt)
    if horses:
        for i, it in enumerate(horses, 1):
            mark = ""
            if it["suspicious"]:
                mark = " — подозрительно, проверить на накрутку"
            elif it["priority"]:
                mark = " — приоритет разбора"
            parts.append(
                f"{i}. {it['title_ru']} ({it['channel_title']}, "
                f"{_fmt_int(it['subscriber_count'])} подписчиков) — "
                f"outlier {_fmt_float(it['outlier'])}x, "
                f"скорость {_fmt_float(it['views_per_day'], 0)} просмотров/сутки"
                f"{mark}."
            )
    else:
        parts.append(_no_data_line(conn))

    # --- SEO-сборник ---
    parts.append("")
    parts.append(sep)
    parts.append("SEO-СБОРНИК (как оформлены растущие видео)")
    parts.append(sep)
    seo = seo_pack(conn, limit=30, fmt=fmt)
    s = seo["summary"]
    if s["count"]:
        parts.append(f"Отобрано видео: {s['count']}.")
        parts.append(
            f"Средняя длина заголовка: {_fmt_float(s['avg_title_length'])} знаков."
        )
        if s["shorts_share"] is not None:
            threshold = config.shorts_max_seconds()
            parts.append(
                f"Доля вероятных шортсов (до {threshold} секунд): "
                f"{_fmt_float(s['shorts_share'] * 100)}%."
            )
        if s["top_tags"]:
            tags_txt = ", ".join(f"{t['tag']} ({t['count']})" for t in s["top_tags"])
            parts.append(f"Частые теги: {tags_txt}.")
        else:
            parts.append("Частые теги: нет данных.")
        if s["by_hour"]:
            hours_txt = ", ".join(f"{h}:00 — {n}" for h, n in s["by_hour"].items())
            parts.append(f"Публикации по часам (МСК): {hours_txt}.")
        else:
            parts.append("Публикации по часам (МСК): нет данных.")
        if s["by_weekday"]:
            days_txt = ", ".join(
                f"{WEEKDAYS_RU[d]} — {n}" for d, n in s["by_weekday"].items()
            )
            parts.append(f"Публикации по дням недели: {days_txt}.")
        else:
            parts.append("Публикации по дням недели: нет данных.")
        for i, v in enumerate(seo["videos"][:10], 1):
            parts.append(
                f"{i}. {v['title_ru']} — заголовок {v['title_length']} знаков, "
                f"{v['title_words']} слов, число: {'да' if v['title_has_number'] else 'нет'}, "
                f"год: {'да' if v['title_has_year'] else 'нет'}, "
                f"формат: {v['format']}, тегов: {v['tags_count']}, "
                f"лайков на 1000: {_fmt_float(v['likes_per_1000'])}, "
                f"комментариев на 1000: {_fmt_float(v['comments_per_1000'])}."
            )
    else:
        parts.append(_no_data_line(conn))


def _seo_section(conn, parts: list[str], fmt: str) -> None:
    """Раздел «SEO-оформление» по залетевшим. Только чтение, без сети.

    Опирается на ``seo.patterns`` (боевая база). Если ``seo_fields`` пуста,
    выводится одна честная строка и отчёт не ломается.
    """
    from . import seo as seo_module

    sep = "=" * 60
    parts.append("")
    parts.append(sep)
    parts.append("SEO-ОФОРМЛЕНИЕ (как оформлены залетевшие)")
    parts.append(sep)

    try:
        analyzed = int(
            conn.execute("SELECT COUNT(*) FROM seo_fields").fetchone()[0]
        )
    except Exception:
        analyzed = 0
    if not analyzed:
        parts.append("SEO-разбор не выполнялся: запустите `tuber seo --analyze`.")
        return

    pattern_fmt = fmt if fmt in ("short", "long") else None
    res = seo_module.patterns(conn, min_outlier=3.0, fmt=pattern_fmt)
    n_out = res["outliers"]["n"]
    if not n_out:
        parts.append(
            "Залетевших (выброс ≥ 3x) не найдено: разбор оформления пуст."
        )
        parts.append(
            "Точные метрики вовлечённости (кликабельность, доходимость) "
            "по чужим видео недоступны — их не выводим."
        )
        return

    t = res["title"]["outlier"]
    med = (_fmt_float(t["median_length"], 0)
           if t["median_length"] is not None else "нет данных")
    parts.append(
        f"Заголовки у залетевших: медиана {med} знаков (n={t['n']}), "
        f"с цифрой {_fmt_float((t['share_number'] or 0) * 100)}%, "
        f"с вопросом {_fmt_float((t['share_question'] or 0) * 100)}%."
    )

    peaks = res["timing"]["outlier"]["peak_hours"]
    if peaks:
        peak_txt = ", ".join(f"{p['hour']}:00 ({p['count']})" for p in peaks)
        parts.append(f"Время публикации: пики — {peak_txt} МСК.")
    else:
        parts.append("Время публикации: нет данных о времени.")

    g = res["tags"]["outlier"]
    parts.append(
        f"Теги: есть у {_fmt_float((g['share_with_tags'] or 0) * 100)}% "
        f"залетевших, среднее {_fmt_float(g['avg_tags'])} "
        f"(база с тегами {g['base_with_tags']}); без тегов "
        f"{g['without_tags']} видео."
    )

    dur = res["duration"]
    median_long_min = (
        _fmt_float(float(dur["long"]["median_seconds"]) / 60.0)
        if dur["long"]["median_seconds"] is not None else "нет данных"
    )
    best_zone = None
    for bucket in dur["long_buckets"]:
        if bucket["n"] and bucket["median_views"] is not None:
            if best_zone is None or bucket["median_views"] > best_zone["median_views"]:
                best_zone = bucket
    if best_zone is not None:
        parts.append(
            f"Длительность: медиана полных {median_long_min} мин, лучшая зона "
            f"«{best_zone['label']}» (медиана просмотров "
            f"{_fmt_int(best_zone['median_views'])}, n={best_zone['n']})."
        )
    else:
        parts.append(
            f"Длительность: медиана полных {median_long_min} мин, "
            f"лучшая зона — нет данных."
        )

    parts.append(
        "Точные метрики вовлечённости (кликабельность, доходимость) "
        "по чужим видео недоступны — их не выводим."
    )


def build_report(conn, cfg: Any = config, days: int = 10, fmt: str = "all",
                 topic: str | None = None) -> str:
    """Собрать текстовый отчёт на русском. Только БД, без сети.

    fmt='all' — оба раздела («## Шортсы» и «## Полные видео»);
    fmt='short' или 'long' — только этот раздел. Смешанный рейтинг шортсов
    с полными видео не строится: это разные единицы измерения.
    ``topic`` — необязательный фильтр топа виральности по теме из
    ``video_classification.topic`` (не по ``videos.primary_topic``).
    """
    fmt = fmt if fmt in ("short", "long") else "all"
    now = datetime.fromtimestamp(int(cfg.now_ts()), tz=MSK)
    parts: list[str] = []

    parts.append("ТРЕНДЫ ИИ-ВИДЕО — ЕЖЕДНЕВНЫЙ ОТЧЁТ")
    parts.append(f"Дата: {now:%d.%m.%Y %H:%M} (МСК). Окно роста: {days} дней.")
    stats = _snapshot_stats(conn)
    speed_threshold = config.min_interval_for_speed_seconds(cfg)
    parts.append(
        f"Замеров в базе: {stats['total']}; видео с замером: {stats['videos']}. "
        f"Пригодны для скорости (интервал от {speed_threshold} с): {stats['ok']}; "
        f"слишком короткие: {stats['short']}; "
        f"без пары (первый замер): {stats['first']}; "
        f"видео со скоростью: {stats['with_speed']}."
    )

    streams = ("short", "long") if fmt == "all" else (fmt,)
    for stream in streams:
        _stream_section(conn, parts, days, stream, topic=topic)

    _seo_section(conn, parts, fmt)

    return "\n".join(parts)
