"""Рейтинг «сливки» (ТЗ-45, контуры 2 и 3 плана «Сливки»).

Зачем
-----
Рейтинг считал ПОСТЫ, а не авторов: не было ни одной агрегированной оси
«растёт». Поля ``posts_per_day``/``avg_views``/``ai_density``/``first_mover_score``
в ``source`` есть, но «восходящих» авторов система не показывала. Этот модуль
добавляет ось автора и выдачу:

* **``source_metric_history``** — суточный снимок метрик источника (``subs``,
  ``posts_7d``, ``median_likes_24h``, ``viral_posts_14d``,
  ``trusted_indegree_30d``, ``lead_time_median``). Пишется после сбора
  (:func:`capture`), читается для роста ``g7`` и для метки «восходящий».
* **``outlier``** — одно число для всех платформ вместе:
  ``метрика / медиана_автора_на_том_же_возрасте_поста``. Платформенная метрика
  одна: YouTube — просмотры, Telegram — просмотры, X — лайки. Медиана автора
  считается ВНУТРИ возрастной корзины (:data:`AGE_BUCKETS`), а не «как попался
  снимок»: иначе свежий пост всегда проигрывает зрелому.
* **``breakout``** — ``0,45·z(g7) + 0,25·z(median_likes_24h) +
  0,20·z(trusted_indegree_30d) + 0,10·new_entity_rate``, где ``z`` — робастный
  z-скор (медиана и MAD) ВНУТРИ размерного класса подписчиков
  (:data:`SIZE_CLASSES`). Без классов рейтинг всегда выигрывают гиганты.
* **Честность к неполной истории.** Если для оси нет данных нужной глубины
  (``g7`` требует 7 дней), ось НЕ подставляется нулём. Веса пересчитываются по
  фактически доступным осям, а в выдаче печатается «история N дн из 7, веса
  пересчитаны». Выдуманных значений нет: чего не измерили — того не показываем.

Метки:

* **«восходящий»** — ``breakout ≥ +2σ`` своего класса 2 дня из 3 подряд (не 3
  из 3: источники мерцают, требование «строго каждый день» режет живые
  аккаунты). Нужна история ≥ 3 дней, иначе метки нет честно.
* **«до самой сути»** — ``trusted_indegree_30d ≥ 2`` (на него сослались ≥ 2
  разных крупных автора) и ``lead_time_median ≤ −30`` мин (пишет раньше других
  внутри сюжета).

Пороги отбора (:data:`SELECT_*`): подписчиков 1 000–500 000; ≥ 3 поста за 14
дней; ``g7 ≥ +3 %`` при ``subs ≥ 5k`` либо ``≥ +10 %`` при ``subs < 5k``;
≥ 1 относительный выброс за 14 дней; не в blocklist.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

from tuber import config
from tuber.core import db as _db
from tuber.core import firstmovers, timeutil

#: Метрика «силы» поста по платформе (контур 2): просмотры / просмотры / лайки.
PLATFORM_METRIC: dict[str, str] = {
    "youtube": "views",
    "telegram": "views",
    "x": "likes",
}

#: Возрастные корзины поста (часы от публикации до снимка). Медиана автора
#: считается внутри той же корзины: сравнение «пост в 1 час» с «постом в 3 дня»
#: нечестно. Последняя корзина ``None`` — «старше 72 ч» (зрелые просмотры).
AGE_BUCKETS: tuple[tuple[float | None, str], ...] = (
    (1.0, "<1ч"),
    (6.0, "1–6ч"),
    (24.0, "6–24ч"),
    (72.0, "24–72ч"),
    (None, ">72ч"),
)

#: Размерные классы подписчиков (верхняя граница эксклюзивна). ``None`` —
#: последний класс, «всё, что выше».
SIZE_CLASSES: tuple[tuple[int | None, str], ...] = (
    (5_000, "<5k"),
    (25_000, "5–25k"),
    (100_000, "25–100k"),
    (500_000, "100–500k"),
    (None, ">500k"),
)

#: Веса осей breakout (ТЗ-45 §3).
BREAKOUT_WEIGHTS: dict[str, float] = {
    "g7": 0.45,
    "median_likes_24h": 0.25,
    "trusted_indegree_30d": 0.20,
    "new_entity_rate": 0.10,
}

#: Минимальная глубина истории (дней) для оси. Нет глубины — ось недоступна.
AXIS_MIN_HISTORY_DAYS: dict[str, int] = {
    "g7": 7,
    "median_likes_24h": 1,
    "trusted_indegree_30d": 1,
    "new_entity_rate": 1,
}

#: Кратность автора-медианы, с которой пост считается относительным выбросом.
OUTLIER_THRESHOLD = 3.0
#: Сколько постов автора нужно в возрастной корзине для честной медианы.
MIN_AUTHOR_POSTS_PER_BUCKET = 3

#: Пороги отбора «сливок».
SELECT_SUBS_MIN = 1_000
SELECT_SUBS_MAX = 500_000
SELECT_POSTS_14D_MIN = 3
SELECT_G7_MIN_BIG = 0.03      # при subs >= 5k
SELECT_G7_MIN_SMALL = 0.10    # при subs < 5k
SELECT_G7_BIG_SUBS = 5_000

#: Метка «восходящий»: breakout >= +SIGMA_MULT·σ класса.
RISING_SIGMA_MULT = 2.0
#: Сколько дней подряд смотрятся для метки и сколько из них нужно.
RISING_WINDOW_DAYS = 3
RISING_DAYS_REQUIRED = 2
#: Метка «до самой сути».
PITHY_MIN_INDEGREE = 2
PITHY_MAX_LEAD_MIN = -30.0

#: Робастный множитель MAD → σ (для нормального распределения).
MAD_TO_SIGMA = 1.4826

#: Метка платформы/режима для журналов ``run``/``run_log``.
RUN_PLATFORM = "rating"
RUN_MODE = "slivki"
RUN_MODE_CAPTURE = "slivki-capture"

#: Сколько позиций в основной выдаче по умолчанию.
DEFAULT_LIMIT = 20


# ---------------------------------------------------------------------------
# Время и мелкие помощники
# ---------------------------------------------------------------------------

def _now(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    parsed = timeutil.parse_any(now)
    return datetime.strptime(parsed, timeutil.ISO_FMT).replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(timeutil.ISO_FMT)


def _day(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _parse_dt(value) -> datetime | None:
    iso = timeutil.parse_any(value)
    if iso is None:
        return None
    return datetime.strptime(iso, timeutil.ISO_FMT).replace(tzinfo=timezone.utc)


def age_bucket(age_hours: float | None) -> str:
    """Корзина возраста поста по числу часов от публикации до снимка."""
    if age_hours is None:
        return ">72ч"
    for bound, name in AGE_BUCKETS:
        if bound is None or age_hours < bound:
            return name
    return ">72ч"


def size_class(subs) -> str | None:
    """Размерный класс подписчиков или ``None``, если подписчики неизвестны."""
    if subs is None:
        return None
    value = float(subs)
    if value <= 0:
        return None
    for bound, name in SIZE_CLASSES:
        if bound is None or value < bound:
            return name
    return None


def axis_class(src: dict) -> str | None:
    """Класс для робастного z: ``«платформа|размер»``.

    Размерный класс обязателен (иначе рейтинг выигрывают гиганты), но
    сравнивать внутри него X и Telegram нельзя: у X ось — лайки, у Telegram —
    просмотры, и общая медиана смешала бы шкалы. Поэтому класс считается
    ВНУТРИ платформы.
    """
    cls = size_class(src.get("subs"))
    if cls is None:
        return None
    return f"{src.get('platform')}|{cls}"


# ---------------------------------------------------------------------------
# Робастная статистика
# ---------------------------------------------------------------------------

def robust_z(values: dict, *, class_of) -> dict:
    """Робастный z-скор внутри размерного класса.

    ``values`` — ``{key: number}``; ``class_of(key) -> class_name``. Для каждого
    класса считается медиана и MAD (median absolute deviation), z =
    ``(x − медиана) / (1.4826·MAD)``. При ``MAD = 0`` берётся обычное σ; если и
    оно ноль — z = 0.0 (все равны, выделять нечего).
    """
    by_class: dict[str, list[float]] = {}
    for key, value in values.items():
        cls = class_of(key)
        if cls is None or value is None:
            continue
        by_class.setdefault(cls, []).append(float(value))

    stats: dict[str, tuple[float, float]] = {}
    for cls, vals in by_class.items():
        if not vals:
            continue
        med = statistics.median(vals)
        sigma = _robust_sigma(vals, med)
        stats[cls] = (med, sigma)

    out: dict = {}
    for key, value in values.items():
        cls = class_of(key)
        if cls is None or value is None or cls not in stats:
            continue
        med, sigma = stats[cls]
        out[key] = 0.0 if sigma <= 0 else (float(value) - med) / sigma
    return out


def _robust_sigma(vals: list[float], med: float) -> float:
    """Робастная σ класса: ``1,4826·MAD``, иначе 0.

    Когда больше половины класса совпадает (типичная картина для
    ``new_entity_rate`` или медианы лайков), ``MAD = 0``. Подставлять сюда
    обычное σ нельзя: оно вырождается в микровеличину, и единственный
    отличающийся автор получает z порядка сотен — шкала рвётся. Если MAD = 0,
    класс различать нечем, и честный ответ — z = 0 для всех.
    """
    if len(vals) > 1:
        mad = statistics.median([abs(v - med) for v in vals])
        if mad > 0:
            return mad * MAD_TO_SIGMA
    return 0.0


def reweight(available: dict[str, float | None]) -> dict[str, float]:
    """Пересчитать веса :data:`BREAKOUT_WEIGHTS` по ФАКТИЧЕСКИ доступным осям.

    Возвращает ``{axis: weight}`` с суммой 1.0 по доступным осям. Пустой словарь
    — нет ни одной оси (тогда breakout не считается).
    """
    present = {axis: BREAKOUT_WEIGHTS[axis] for axis in BREAKOUT_WEIGHTS
               if axis in available}
    total = sum(present.values())
    if total <= 0:
        return {}
    return {axis: w / total for axis, w in present.items()}


def weighted_breakout(z_by_axis: dict[str, float], weights: dict[str, float],
                      raw: dict[str, float | None]) -> float | None:
    """Взвешенная сумма z-скоров по доступным осям (``None`` — z нет ни для одной)."""
    total = 0.0
    used = 0
    for axis, weight in weights.items():
        if raw.get(axis) is None:
            continue
        z = z_by_axis.get(axis)
        if z is None:
            continue
        total += weight * z
        used += 1
    if used == 0:
        return None
    return total


# ---------------------------------------------------------------------------
# Загрузка срезов базы
# ---------------------------------------------------------------------------

def load_sources(con) -> dict[int, dict]:
    """Реестр источников: ``{source_id: row-dict}`` (только с handle)."""
    out: dict[int, dict] = {}
    for r in con.execute(
            "SELECT id, platform, handle, title, url, subs, status, meta_json FROM source"):
        out[r["id"]] = dict(r)
    return out


def load_latest_metrics(con) -> dict[int, dict]:
    """Последняя метрика каждого материала: ``{content_id: {...}}``.

    Возвращает ``metric`` (просмотры для YouTube/Telegram, лайки для X), ``age``
    (часы от публикации до снимка), ``bucket``, ``source_id``, ``platform``,
    ``published_at`` и ``url``.
    """
    rows = con.execute(
        """
        SELECT c.id AS cid, c.source_id AS sid, c.platform AS platform,
               c.external_id AS external_id, c.author_handle AS author_handle,
               c.url AS url, c.published_at AS published_at,
               ms.views AS views, ms.likes AS likes, ms.captured_at AS captured_at,
               ms.age_hours AS stored_age
          FROM content c
          JOIN metric_snapshot ms ON ms.content_id = c.id
         WHERE ms.captured_at = (
                 SELECT MAX(captured_at) FROM metric_snapshot WHERE content_id = c.id)
        """).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        platform = r["platform"]
        metric = r["likes"] if PLATFORM_METRIC.get(platform) == "likes" else r["views"]
        if metric is None:
            continue
        age = r["stored_age"]
        if age is None:
            pub, cap = _parse_dt(r["published_at"]), _parse_dt(r["captured_at"])
            age = (cap - pub).total_seconds() / 3600.0 if pub and cap else None
        out[r["cid"]] = {
            "content_id": r["cid"], "source_id": r["sid"], "platform": platform,
            "metric": float(metric), "age": age, "bucket": age_bucket(age),
            "published_at": r["published_at"], "url": r["url"],
            "external_id": r["external_id"], "author_handle": r["author_handle"],
        }
    return out


def author_age_medians(metrics: dict[int, dict], *,
                       min_posts: int = MIN_AUTHOR_POSTS_PER_BUCKET) -> dict:
    """``{(source_id, bucket): медиана метрики}`` по постам автора в корзине.

    Автор с меньшим числом постов в корзине медианы не получает: медиана по
    одному посту — это сам пост, и любой пост был бы «выбросом».
    """
    groups: dict[tuple[int, str], list[float]] = {}
    for m in metrics.values():
        groups.setdefault((m["source_id"], m["bucket"]), []).append(m["metric"])
    return {k: statistics.median(v) for k, v in groups.items()
            if len(v) >= min_posts}


def compute_outliers(metrics: dict[int, dict], medians: dict) -> dict[int, float]:
    """``{content_id: outlier}`` = метрика / медиана автора в той же корзине."""
    out: dict[int, float] = {}
    for cid, m in metrics.items():
        med = medians.get((m["source_id"], m["bucket"]))
        if not med or med <= 0:
            continue
        out[cid] = m["metric"] / med
    return out


def _window_cutoff(now: datetime, days: int) -> str:
    return _iso(now - timedelta(days=days))


def posts_windows(con, now: datetime) -> dict[int, dict]:
    """Число постов автора в окнах 24ч / 7д / 14д: ``{source_id: {...}}``."""
    out: dict[int, dict] = {}
    ranges = (("posts_24h", 1), ("posts_7d", 7), ("posts_14d", 14))
    for name, days in ranges:
        for r in con.execute(
                "SELECT source_id AS sid, COUNT(*) AS n FROM content "
                "WHERE source_id IS NOT NULL AND published_at >= ? GROUP BY source_id",
                (_window_cutoff(now, days),)):
            out.setdefault(r["sid"], {})[name] = int(r["n"])
    return out


def median_metric_24h(con, now: datetime) -> dict[int, float]:
    """Медиана метрики постов автора, опубликованных за 24 ч: ``{source_id: медиана}``.

    Метрика — платформенная (:data:`PLATFORM_METRIC`): лайки X, просмотры
    Telegram/YouTube. Колонка носит имя ``median_likes_24h`` из ТЗ.

    TODO(debt-D-59): имя колонки обещает лайки, значение — метрику внимания —
    см. TECH-DEBT.md.
    """
    cutoff = _window_cutoff(now, 1)
    by_source: dict[int, list[float]] = {}
    for r in con.execute(
            "SELECT c.source_id AS sid, c.platform AS platform, "
            "       ms.likes AS likes, ms.views AS views "
            "  FROM content c JOIN metric_snapshot ms ON ms.content_id = c.id "
            " WHERE c.published_at >= ? AND ms.captured_at = ("
            "   SELECT MAX(captured_at) FROM metric_snapshot WHERE content_id = c.id)",
            (cutoff,)):
        metric = r["likes"] if PLATFORM_METRIC.get(r["platform"]) == "likes" else r["views"]
        if metric is None:
            continue
        by_source.setdefault(r["sid"], []).append(float(metric))
    return {sid: statistics.median(vals) for sid, vals in by_source.items() if vals}


def new_entity_counts(con, now: datetime) -> dict[int, int]:
    """Число постов автора в сюжетах с новой сущностью за 14 дней."""
    cutoff = _window_cutoff(now, 14)
    out: dict[int, int] = {}
    for r in con.execute(
            """SELECT c.source_id AS sid, COUNT(DISTINCT sm.content_id) AS n
                 FROM story_member sm
                 JOIN content c ON c.id = sm.content_id
                 JOIN story s ON s.id = sm.story_id
                WHERE s.is_new_entity = 1 AND c.published_at >= ?
                  AND c.source_id IS NOT NULL
                GROUP BY c.source_id""", (cutoff,)):
        out[r["sid"]] = int(r["n"])
    return out


def firstmover_ledger(con) -> dict[int, dict]:
    """Реестр первопроходцев: таблица ``first_mover``, иначе живой расчёт.

    Таблица пуста, пока ``tuber graph first-movers`` не прогонялся (в бою это
    ночная обёртка). Чтобы рейтинг не зависел от порядка запусков, при пустой
    таблице оси считаются тем же кодом (:mod:`tuber.core.firstmovers`) без записи.
    """
    if con.execute("SELECT COUNT(*) FROM first_mover").fetchone()[0] > 0:
        return {r["source_id"]: dict(r) for r in con.execute("SELECT * FROM first_mover")}
    data = firstmovers.build(con)
    return data["ledger"]


# ---------------------------------------------------------------------------
# История: снимок и чтение
# ---------------------------------------------------------------------------

def capture(con, *, now=None, day: str | None = None) -> dict:
    """Записать суточный снимок ``source_metric_history`` для всех источников.

    Источники без единой доступной оси в снимок не попадают (не пишем строку из
    одних NULL). Повторный прогон в тот же день перезаписывает строку.
    """
    now_dt = _now(now)
    day = day or _day(now_dt)
    captured_at = _iso(now_dt)

    sources = load_sources(con)
    metrics = load_latest_metrics(con)
    medians = author_age_medians(metrics)
    outliers = compute_outliers(metrics, medians)
    windows = posts_windows(con, now_dt)
    med24 = median_metric_24h(con, now_dt)
    ledger = firstmover_ledger(con)

    #: Лучший (максимальный) выброс автора за 14 дней.
    cutoff14 = _window_cutoff(now_dt, 14)
    viral: dict[int, int] = {}
    for cid, o in outliers.items():
        m = metrics.get(cid)
        if m is None or o < OUTLIER_THRESHOLD:
            continue
        pub = _parse_dt(m["published_at"])
        if pub is None or _iso(pub) < cutoff14:
            continue
        viral[m["source_id"]] = viral.get(m["source_id"], 0) + 1

    rows = 0
    with_subs = 0
    for sid, src in sources.items():
        win = windows.get(sid, {})
        fm = ledger.get(sid, {})
        subs = src["subs"]
        values = {
            "subs": int(subs) if subs is not None else None,
            "posts_7d": win.get("posts_7d"),
            "median_likes_24h": med24.get(sid),
            "viral_posts_14d": viral.get(sid),
            "trusted_indegree_30d": fm.get("trusted_indegree_30d"),
            "lead_time_median": fm.get("lead_time_median"),
        }
        if all(v is None for v in values.values()):
            continue
        con.execute(
            """INSERT INTO source_metric_history
                 (source_id, day, subs, posts_7d, median_likes_24h, viral_posts_14d,
                  trusted_indegree_30d, lead_time_median, captured_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id, day) DO UPDATE SET
                 subs=excluded.subs, posts_7d=excluded.posts_7d,
                 median_likes_24h=excluded.median_likes_24h,
                 viral_posts_14d=excluded.viral_posts_14d,
                 trusted_indegree_30d=excluded.trusted_indegree_30d,
                 lead_time_median=excluded.lead_time_median,
                 captured_at=excluded.captured_at""",
            (sid, day, values["subs"], values["posts_7d"], values["median_likes_24h"],
             values["viral_posts_14d"], values["trusted_indegree_30d"],
             values["lead_time_median"], captured_at))
        rows += 1
        if values["subs"] is not None:
            with_subs += 1
    con.commit()
    return {"day": day, "rows": rows, "with_subs": with_subs,
            "sources": len(sources), "captured_at": captured_at}


def backfill_from_evidence(con, *, now=None) -> dict:
    """Засеять историю УЖЕ имеющимися в базе точками (без выдумывания).

    Источники точки:

    * ``source.meta_json.followers_history`` — точки ``{at, subs}``, которые
      писали обёртки подписчиков (ТЗ-44/ТЗ-45);
    * ``source.subs`` + ``source.subs_at`` — последнее известное значение.

    Пишутся только ``subs`` (остальные оси на те даты неизвестны и остаются
    NULL). День точки — дата ``at``/``subs_at``. Сегодняшний день не трогается:
    его пишет :func:`capture`.
    """
    now_dt = _now(now)
    today = _day(now_dt)
    src_rows = con.execute(
        "SELECT id, subs, subs_at, meta_json FROM source WHERE subs IS NOT NULL").fetchall()
    points: list[tuple[int, str, int]] = []
    for r in src_rows:
        history = []
        if r["meta_json"]:
            try:
                history = (json.loads(r["meta_json"]) or {}).get("followers_history") or []
            except (ValueError, TypeError):
                history = []
        for h in history:
            if not isinstance(h, dict):
                continue
            at, subs = h.get("at"), h.get("subs")
            dt = _parse_dt(at)
            if dt is None or subs is None:
                continue
            points.append((r["id"], _day(dt), int(subs)))
        if r["subs_at"] is not None and r["subs"] is not None:
            dt = _parse_dt(r["subs_at"])
            if dt is not None:
                points.append((r["id"], _day(dt), int(r["subs"])))

    written = 0
    # Дедуп по (источник, день): точка ряда подписчиков и ``subs_at`` могут
    # указывать на один и тот же день — строка дня одна.
    dedup: dict[tuple[int, str], int] = {}
    for sid, day, subs in points:
        if day == today:
            continue
        dedup[(sid, day)] = subs
    for (sid, day), subs in dedup.items():
        con.execute(
            """INSERT INTO source_metric_history (source_id, day, subs, captured_at)
                 VALUES (?,?,?,?)
               ON CONFLICT(source_id, day) DO UPDATE SET
                 subs=excluded.subs, captured_at=excluded.captured_at""",
            (sid, day, subs, _iso(now_dt)))
        written += 1
    con.commit()
    return {"points": len(points), "written": written, "today": today}


def history_series(con) -> dict[int, list[dict]]:
    """``{source_id: [row, ...]}`` — строки истории по возрастанию дня.

    Строка — dict с ``day`` и осями снимка (``subs``, ``median_likes_24h``,
    ``trusted_indegree_30d`` и т.д.). Читается один раз, чтобы пересчёт breakout
    по дням не ходил в базу на каждый источник.
    """
    out: dict[int, list[dict]] = {}
    for r in con.execute(
            "SELECT source_id AS sid, day, subs, posts_7d, median_likes_24h, "
            "       viral_posts_14d, trusted_indegree_30d, lead_time_median "
            "  FROM source_metric_history ORDER BY source_id, day"):
        out.setdefault(r["sid"], []).append(dict(r))
    return out


def _row_at(rows: list[dict], day: str) -> dict | None:
    """Строка истории с наибольшим ``day <= day`` (или ``None``)."""
    best = None
    for row in rows:
        if row["day"] <= day:
            best = row
        else:
            break
    return best


def history_depth_days(rows: list[dict], today: str) -> int:
    """Глубина истории источника, дней: ``today − самый ранний день``.

    Это ИМЕННО пройденный интервал (сколько дней разделяют точки), поэтому для
    пары снимков «7 дней назад» и «сегодня» глубина равна 7 и ``g7`` измерим.
    Одна точка сегодня — глубина 0 (роста ещё не из чего считать).
    """
    days = [r["day"] for r in rows]
    earliest = min(days) if days else None
    if earliest is None:
        return 0
    t0 = datetime.strptime(today, "%Y-%m-%d")
    t1 = datetime.strptime(earliest, "%Y-%m-%d")
    return max(0, (t0 - t1).days)


def g7_from_series(rows: list[dict], today: str) -> float | None:
    """Рост подписчиков за 7 дней по ряду истории.

    Берётся самая свежая точка не позже ``today − 7`` и сегодняшняя. Нет точки
    нужной глубины либо база нулевая — ``None`` (ось недоступна, а не ноль).
    """
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    ref_day = today_dt - timedelta(days=7)
    current = None
    ref = None
    for row in rows:
        subs = row.get("subs")
        if subs is None:
            continue
        dt = datetime.strptime(row["day"], "%Y-%m-%d")
        if dt == today_dt:
            current = subs
        if dt <= ref_day and (ref is None or dt > ref[0]):
            ref = (dt, subs)
    if current is None or ref is None or ref[1] <= 0:
        return None
    return (current - ref[1]) / ref[1]


# ---------------------------------------------------------------------------
# Сборка рейтинга
# ---------------------------------------------------------------------------

def _author_url(src: dict) -> str | None:
    """Ссылка на профиль автора (существующая или синтезированная)."""
    existing = (src.get("url") or "").strip()
    if existing:
        return existing
    platform, handle = src.get("platform"), src.get("handle")
    if not handle:
        return None
    if platform == "telegram":
        return f"https://t.me/{handle}"
    if platform == "x":
        return f"https://x.com/{handle}"
    if platform == "youtube":
        return f"https://www.youtube.com/channel/{handle}"
    return None


def _best_viral(metrics: dict[int, dict], outliers: dict[int, float],
                by_source, now_dt, *, days: int = 14):
    """Лучший виральный пост автора за окно: максимум ``outlier``."""
    cutoff = _window_cutoff(now_dt, days)
    for cid, o in outliers.items():
        m = metrics.get(cid)
        if m is None or o < OUTLIER_THRESHOLD:
            continue
        pub = _parse_dt(m["published_at"])
        if pub is None or _iso(pub) < cutoff:
            continue
        sid = m["source_id"]
        best = by_source.get(sid)
        if best is None or o > best["outlier"]:
            by_source[sid] = {"outlier": o, "metric": m["metric"],
                              "published_at": m["published_at"],
                              "url": m["url"], "bucket": m["bucket"]}


def breakout_for_day(con, *, day: str, sources: dict[int, dict], series: dict,
                     axes_live: dict[int, dict] | None = None,
                     history_depth: dict[int, int] | None = None) -> tuple[dict, dict]:
    """Breakout всех источников за конкретный день + веса осей.

    ``axes_live`` — живые оси дня (только для сегодняшнего дня: ``new_entity_rate``
    в истории не хранится). Для исторических дней ``new_entity_rate`` недоступна
    и веса пересчитываются по оставшимся осям.
    """
    raw: dict[int, dict[str, float | None]] = {}
    for sid, src in sources.items():
        subs = src["subs"]
        if subs is None or subs <= 0:
            continue
        live = (axes_live or {}).get(sid, {})
        depth = (history_depth or {}).get(sid, 0)
        rows = series.get(sid, [])
        if axes_live is not None:
            g7 = live.get("g7")
            med24 = live.get("median_likes_24h")
            indeg = live.get("trusted_indegree_30d")
            ne = live.get("new_entity_rate")
        else:
            g7 = g7_from_series(rows, day) if depth >= AXIS_MIN_HISTORY_DAYS["g7"] else None
            stored = _row_at(rows, day) or {}
            med24 = stored.get("median_likes_24h")
            indeg = stored.get("trusted_indegree_30d")
            # TODO(debt-D-58): new_entity_rate в снимке не хранится (набор колонок
            # из ТЗ-45), поэтому прошлые дни считаются без этой оси — см.
            # TECH-DEBT.md.
            ne = None
        raw[sid] = {
            "g7": g7 if depth >= AXIS_MIN_HISTORY_DAYS["g7"] else None,
            "median_likes_24h": med24,
            "trusted_indegree_30d": indeg,
            "new_entity_rate": ne,
        }

    # z внутри размерного класса — ОТДЕЛЬНО по каждой оси.
    z_axes: dict[str, dict[int, float]] = {}
    for axis in BREAKOUT_WEIGHTS:
        values = {sid: axes[axis] for sid, axes in raw.items() if axes.get(axis) is not None}
        z_axes[axis] = robust_z(
            values, class_of=lambda sid: axis_class(sources[sid]))

    breakout: dict[int, float] = {}
    weights_used: dict[int, dict] = {}
    for sid, axes in raw.items():
        available = {a: v for a, v in axes.items() if v is not None}
        weights = reweight(available)
        weights_used[sid] = weights
        value = weighted_breakout({a: z_axes.get(a, {}).get(sid) for a in weights},
                                  weights, available)
        if value is not None:
            breakout[sid] = value
    return breakout, weights_used


def build(con, *, now=None, limit: int = DEFAULT_LIMIT) -> dict:
    """Посчитать выдачу «сливки»: оси, breakout, отбор, метки.

    Ничего не пишет. Возвращает структуру для :func:`format_report`/``--json``.
    """
    now_dt = _now(now)
    today = _day(now_dt)
    sources = load_sources(con)
    metrics = load_latest_metrics(con)
    medians = author_age_medians(metrics)
    outliers = compute_outliers(metrics, medians)
    windows = posts_windows(con, now_dt)
    med24 = median_metric_24h(con, now_dt)
    new_ent = new_entity_counts(con, now_dt)
    ledger = firstmover_ledger(con)
    series = history_series(con)
    depth = {sid: history_depth_days(row, today) for sid, row in series.items()}

    # --- оси на сегодня ---
    live_axes: dict[int, dict] = {}
    for sid in sources:
        ne_total = windows.get(sid, {}).get("posts_14d")
        ne_rate = (new_ent.get(sid, 0) / ne_total) if ne_total else None
        live_axes[sid] = {
            "g7": g7_from_series(series.get(sid, []), today)
                  if depth.get(sid, 0) >= AXIS_MIN_HISTORY_DAYS["g7"] else None,
            "median_likes_24h": med24.get(sid),
            "trusted_indegree_30d": (ledger.get(sid, {}) or {}).get("trusted_indegree_30d"),
            "new_entity_rate": ne_rate,
        }

    history_max_depth = max(depth.values()) if depth else 0
    with_ge7 = sum(1 for d in depth.values() if d >= 7)
    with_subs_history = sum(1 for rows in series.values()
                            if any(r.get("subs") is not None for r in rows))

    breakout, weights_used = breakout_for_day(
        con, day=today, sources=sources, series=series, axes_live=live_axes,
        history_depth=depth)

    # --- лучший виральный пост автора за 14 дней ---
    best: dict[int, dict] = {}
    _best_viral(metrics, outliers, best, now_dt, days=14)

    # --- метка «восходящий»: breakout ≥ +2σ класса 2 дня из 3 ---
    rising_ids, class_thresholds, rising_days_available = _rising(
        con, now_dt, sources, series, depth, live_axes, today)

    # --- отбор «сливок» ---
    blocklist = {(r["platform"], (r["handle"] or "").lower())
                 for r in con.execute("SELECT platform, handle FROM blocklist")}

    def make_record(sid: int) -> dict:
        return _record(sid, sources[sid], live_axes.get(sid, {}), breakout.get(sid),
                       weights_used.get(sid), best.get(sid), _author_url(sources[sid]),
                       depth.get(sid, 0), ledger)

    selected: list[dict] = []
    growth_unverified: list[dict] = []
    for sid, src in sources.items():
        subs = src["subs"]
        if subs is None or not (SELECT_SUBS_MIN <= subs <= SELECT_SUBS_MAX):
            continue
        if (src["platform"], (src["handle"] or "").lower()) in blocklist:
            continue
        win = windows.get(sid, {})
        posts14 = win.get("posts_14d", 0)
        if posts14 < SELECT_POSTS_14D_MIN:
            continue
        if sid not in best:
            continue  # нет относительного выброса за 14 дней
        g7 = live_axes.get(sid, {}).get("g7")
        record = make_record(sid)
        if g7 is None:
            record["growth_status"] = "unknown"
            record["g7_threshold"] = (SELECT_G7_MIN_BIG if subs >= SELECT_G7_BIG_SUBS
                                      else SELECT_G7_MIN_SMALL)
            growth_unverified.append(record)
            continue
        threshold = SELECT_G7_MIN_BIG if subs >= SELECT_G7_BIG_SUBS else SELECT_G7_MIN_SMALL
        if g7 < threshold:
            continue
        record["growth_status"] = "verified"
        record["g7_threshold"] = threshold
        selected.append(record)

    key = lambda r: (r["breakout"] if r["breakout"] is not None else -1e9,
                     r["subs"] or 0)
    selected.sort(key=key, reverse=True)
    growth_unverified.sort(key=key, reverse=True)

    # Метки считаются по ВСЕМ источникам базы, а не только по прошедшим отбор:
    # «до самой сути» и «восходящий» — самостоятельные оси автора.
    pithy: list[dict] = []
    rising: list[dict] = []
    for sid, src in sources.items():
        fm = ledger.get(sid, {}) or {}
        indeg = (live_axes.get(sid, {}) or {}).get("trusted_indegree_30d")
        lead = fm.get("lead_time_median")
        if (indeg is not None and indeg >= PITHY_MIN_INDEGREE
                and lead is not None and lead <= PITHY_MAX_LEAD_MIN):
            pithy.append(make_record(sid))
        if sid in rising_ids:
            rec = make_record(sid)
            rec["rising"] = True
            rising.append(rec)
    pithy.sort(key=lambda r: (-(r["trusted_indegree_30d"] or 0),
                              r["lead_time_median"] if r["lead_time_median"] is not None else 1e9))
    rising.sort(key=key, reverse=True)

    return {
        "generated_at": _iso(now_dt),
        "day": today,
        "history_max_depth": history_max_depth,
        "sources_with_history": len(series),
        "sources_with_subs_history": with_subs_history,
        "sources_with_history_ge7": with_ge7,
        "history_required_days": AXIS_MIN_HISTORY_DAYS["g7"],
        "rising_days_available": rising_days_available,
        "rising_days_required": RISING_DAYS_REQUIRED,
        "class_thresholds": class_thresholds,
        "outlier_by_platform": _outlier_counts(metrics, outliers, sources, now_dt),
        "candidates_verified": selected,
        "candidates_growth_unknown": growth_unverified,
        "rising": rising,
        "to_the_point": pithy,
        "limit": limit,
    }


def _outlier_counts(metrics, outliers, sources, now_dt) -> dict[str, dict]:
    """Сколько постов-выбросов (за 14 дней) и с какой медианой по платформам."""
    cutoff = _window_cutoff(now_dt, 14)
    out: dict[str, dict] = {}
    for cid, o in outliers.items():
        m = metrics.get(cid)
        if m is None:
            continue
        pub = _parse_dt(m["published_at"])
        if pub is None or _iso(pub) < cutoff:
            continue
        platform = sources.get(m["source_id"], {}).get("platform", "?")
        entry = out.setdefault(platform, {"posts": 0, "viral": 0, "max_outlier": 0.0})
        entry["posts"] += 1
        if o >= OUTLIER_THRESHOLD:
            entry["viral"] += 1
        entry["max_outlier"] = max(entry["max_outlier"], round(o, 2))
    return out


def _rising(con, now_dt, sources, series, depth, live_axes, today):
    """Метка «восходящий» по ряду breakout за последние 3 дня.

    Возвращает ``(set(source_id), {class: порог σ}, доступных_дней)``. Если дней
    истории меньше :data:`RISING_WINDOW_DAYS`, метки нет ни у кого (честно).
    """
    days = [(now_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(RISING_WINDOW_DAYS - 1, -1, -1)]

    by_day: dict[str, dict[int, float]] = {}
    thresholds: dict[str, float] = {}
    days_with_data = 0
    for d in days:
        axes = live_axes if d == today else None
        breakout, _ = breakout_for_day(con, day=d, sources=sources, series=series,
                                       axes_live=axes, history_depth=depth)
        by_day[d] = breakout
        if breakout:
            days_with_data += 1
    if days_with_data < RISING_WINDOW_DAYS:
        return set(), {}, days_with_data

    for d in days:
        # порог σ класса — по breakout всех источников этого класса за день
        per_class: dict[str, list[float]] = {}
        for sid, value in by_day[d].items():
            cls = axis_class(sources[sid])
            if cls is None:
                continue
            per_class.setdefault(cls, []).append(value)
        for cls, vals in per_class.items():
            if len(vals) < 2:
                continue
            med = statistics.median(vals)
            sigma = _robust_sigma(vals, med)
            if sigma > 0:
                thresholds[cls] = med + RISING_SIGMA_MULT * sigma

    rising: set[int] = set()
    for sid in sources:
        thr = thresholds.get(axis_class(sources[sid]))
        if thr is None:
            continue
        hits = 0
        for d in days:
            value = by_day.get(d, {}).get(sid)
            if value is not None and value >= thr:
                hits += 1
        if hits >= RISING_DAYS_REQUIRED:
            rising.add(sid)
    return rising, {k: round(v, 4) for k, v in thresholds.items()}, days_with_data


def _record(sid, src, axes, breakout, weights, viral, url, depth, ledger) -> dict:
    fm = ledger.get(sid, {}) or {}
    indeg = axes.get("trusted_indegree_30d")
    lead = fm.get("lead_time_median")
    to_the_point = bool(indeg is not None and indeg >= PITHY_MIN_INDEGREE
                        and lead is not None and lead <= PITHY_MAX_LEAD_MIN)
    viral = viral or {}
    return {
        "source_id": sid,
        "platform": src["platform"],
        "handle": src["handle"],
        "title": src.get("title"),
        "url": url,
        "subs": src["subs"],
        "size_class": size_class(src["subs"]),
        "g7": axes.get("g7"),
        "history_days": depth,
        "history_required": AXIS_MIN_HISTORY_DAYS["g7"],
        "median_likes_24h": axes.get("median_likes_24h"),
        "trusted_indegree_30d": indeg,
        "new_entity_rate": axes.get("new_entity_rate"),
        "lead_time_median": lead,
        "breakout": breakout,
        "weights": {k: round(v, 4) for k, v in (weights or {}).items()},
        "axes_used": sorted((weights or {}).keys()),
        "viral_post": {
            "url": viral.get("url"),
            "outlier": round(viral.get("outlier", 0.0), 2),
            "metric": viral.get("metric"),
            "published_at": viral.get("published_at"),
            "bucket": viral.get("bucket"),
        },
        "rising": False,
        "to_the_point": to_the_point,
    }


# ---------------------------------------------------------------------------
# Формат выдачи
# ---------------------------------------------------------------------------

def _pct(value) -> str:
    return "нет истории" if value is None else f"{value * 100:+.2f}%"


def _num(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def format_report(data: dict, *, limit: int = DEFAULT_LIMIT) -> str:
    """Человекочитаемая выдача: числа, класс, выброс, ссылка, блоки меток."""
    lines: list[str] = []
    depth = data["history_max_depth"]
    req = data["history_required_days"]
    lines.append("=== Сливки: рейтинг растущих авторов (ТЗ-45) ===")
    lines.append(f"день: {data['day']} | история max: {depth} дн из {req} | "
                 f"источников со снимками: {data['sources_with_history']} "
                 f"(с подписчиками: {data['sources_with_subs_history']})")
    lines.append(f"история ≥ {req} дней: {data['sources_with_history_ge7']} авторов")
    if depth < req:
        lines.append(f"! история {depth} дн из {req}: ось g7 недоступна, "
                     f"веса breakout пересчитаны по фактически доступным осям")
    lines.append("")
    lines.append("-- относительный выброс по платформам (метрика / медиана автора "
                 "на том же возрасте) --")
    for platform in ("youtube", "telegram", "x"):
        e = data["outlier_by_platform"].get(platform)
        if not e:
            lines.append(f"  {platform}: выбросов нет (0)")
            continue
        lines.append(f"  {platform}: постов {e['posts']}, из них выбросов "
                     f"≥{OUTLIER_THRESHOLD:g}×: {e['viral']}, макс. {e['max_outlier']}")
    lines.append("")

    verified = data["candidates_verified"]
    unknown = data["candidates_growth_unknown"]
    merged = sorted(verified + unknown,
                    key=lambda r: (r["breakout"] if r["breakout"] is not None else -1e9,
                                   r["subs"] or 0),
                    reverse=True)
    lines.append(f"-- растущие авторы (1к–500к подписчиков, ≥3 поста/14д, ≥1 выброс) — "
                 f"найдено {len(merged)}, рост 7д подтверждён у {len(verified)} --")
    if not merged:
        lines.append("  (никого)")
    for r in merged[:limit]:
        lines.append(_format_author(r))
    if len(merged) > limit:
        lines.append(f"  ... ещё {len(merged) - limit} (показано {limit})")
    lines.append("")

    lines.append("-- восходящие (breakout ≥ +2σ класса, 2 дня из 3) --")
    if data["rising_days_available"] < RISING_WINDOW_DAYS:
        lines.append(f"  (метка недоступна: истории breakout "
                     f"{data['rising_days_available']} дн из {RISING_WINDOW_DAYS})")
    elif not data["rising"]:
        lines.append("  (нет)")
    for r in data["rising"][:limit]:
        lines.append(_format_author(r))
    lines.append("")

    lines.append("-- до самой сути (trusted_indegree_30d ≥ 2 и "
                 "lead_time_median ≤ −30 мин) --")
    if not data["to_the_point"]:
        lines.append("  (нет)")
    for r in data["to_the_point"][:limit]:
        lines.append(_format_author(r))
    return "\n".join(lines)


def _format_author(r: dict) -> str:
    viral = r["viral_post"]
    link = viral.get("url") or "нет ссылки"
    cls = r["size_class"] or "?"
    axes = ",".join(r["axes_used"])
    growth = {"verified": "рост подтверждён",
              "unknown": "рост 7д не измерим"}.get(r.get("growth_status"), "")
    tail = f", {growth}" if growth else ""
    return (f"  {r['handle']} ({r['platform']}, {cls}, {r['subs']} подписчиков): "
            f"g7 {_pct(r['g7'])}, breakout {_num(r['breakout'])}, "
            f"осей {len(r['axes_used'])} [{axes}], "
            f"выброс ×{viral['outlier']}, лучший пост {link}{tail}")


# ---------------------------------------------------------------------------
# CLI: python3 -m tuber rating slivki [capture|report]
# ---------------------------------------------------------------------------

def _db_path(args) -> str:
    if args.db:
        return args.db
    return os.environ.get("TUBER_DB") or config.db_path()


def _is_production(path: str) -> bool:
    """Путь ведёт на боевую единую базу (по realpath, как у графа ТЗ-47)."""
    try:
        return os.path.realpath(str(path)) == os.path.realpath(str(config.DEFAULT_DB_PATH))
    except OSError:
        return False


def _connect(path: str):
    """Соединение с ядром + штатная аддитивная миграция схемы."""
    from tuber.core import schema
    con = _db.connect(path)
    schema.migrate_schema(con)
    return con


def cmd_capture(args) -> int:
    """Снять суточный снимок ``source_metric_history`` (гейт записи)."""
    path = _db_path(args)
    if not args.dry_run and _is_production(path) and not args.allow_production:
        print("отказ: запись в боевую базу без --allow-production"
              " (приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        return 2
    con = _connect(path)
    try:
        now = _now(None)
        if args.dry_run:
            stats = {"dry_run": True}
            stats["backfill"] = {"written": 0}
            stats["snapshot"] = {"rows": 0, "with_subs": 0}
        else:
            run_id = None
            from tuber.core import storage as _storage
            if args.allow_production or not _is_production(path):
                run_id = _storage.add_run(con, RUN_PLATFORM, started_at=_iso(now),
                                          mode=RUN_MODE_CAPTURE)
            backfill = backfill_from_evidence(con, now=now)
            snapshot = capture(con, now=now)
            stats = {"dry_run": False, "backfill": backfill, "snapshot": snapshot}
            if run_id is not None:
                _storage.log_run(
                    con, run_id, _iso(now), "info", None,
                    "slivki-capture: снимок %s, строк %d (с подписчиками %d), "
                    "бэкфилл из истории %d"
                    % (snapshot["day"], snapshot["rows"], snapshot["with_subs"],
                       backfill["written"]), platform=RUN_PLATFORM)
                con.execute(
                    "UPDATE run SET finished_at=?, ok_count=?, items_new=?, errors=0, note=?"
                    " WHERE id=?",
                    (_iso(_now(None)), snapshot["rows"], snapshot["rows"],
                     "rating slivki capture", run_id))
                con.commit()
                stats["run_id"] = run_id
        print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        con.close()


def cmd_report(args) -> int:
    """Выдача рейтинга «сливки» (только чтение)."""
    con = _connect(_db_path(args))
    try:
        data = build(con, limit=args.limit)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        else:
            print(format_report(data, limit=args.limit))
        return 0
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tuber rating slivki",
                                 description="Рейтинг растущих авторов (ТЗ-45)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="путь к базе (иначе TUBER_DB)")
    common.add_argument("--limit", type=int, default=None,
                        help="сколько позиций печатать в блоке")
    common.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--db", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")

    gate = argparse.ArgumentParser(add_help=False)
    gate.add_argument("--dry-run", action="store_true", help="без записи в БД")
    gate.add_argument("--allow-production", action="store_true",
                      help="разрешить запись в боевую базу (иначе отказ)")

    sp = sub.add_parser("report", parents=[common], help="выдача рейтинга (только чтение)")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("capture", parents=[common, gate],
                        help="суточный снимок метрик (запись)")
    sp.set_defaults(func=cmd_capture)

    # По умолчанию (без подкоманды) — выдача рейтинга: ровно
    # `python3 -m tuber rating slivki`.
    ap.set_defaults(func=cmd_report)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Диспетчер зовёт `tuber rating slivki ...` — сам префикс «slivki» не
    # подкоманда, а имя рейтинга (чтобы `rating` мог вырасти другими осями).
    if argv and argv[0] == "slivki":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "db", None) is None:
        args.db = None
    if getattr(args, "limit", None) is None:
        args.limit = DEFAULT_LIMIT
    if not hasattr(args, "json"):
        args.json = False
    if not hasattr(args, "dry_run"):
        args.dry_run = False
    if not hasattr(args, "allow_production"):
        args.allow_production = False
    return args.func(args)

