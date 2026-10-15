"""Качество источника для вирального отбора (ТЗ-8 §1.2).

Два независимых сигнала:

* :func:`is_promo_heavy` / :func:`promo_heavy_sources` — канал, который в
  основном постит рекламу/промо (доля промо-постов ≥ ``share`` при ≥
  ``min_items`` материалах), в борду не пускаем вовсе. Считается по ВСЕЙ
  истории источника через :func:`tuber.analysis.ads.is_promotional` (правила,
  без LLM и без БД-зависимостей детектора).
* :func:`source_status_eligible` — реестр источников. ``candidate`` (не
  верифицированный кандидат) на витрину не попадает: именно так промо-канал
  Альфа-Банка ``alfa_vygodno`` (``status='candidate'``) занял 1-е и 2-е места
  Telegram в замере 08.10.2026.

Оговорка по данным (честно, не выдумываем): у Telegram/X реестр статусов
наполнен, а у YouTube в боевой базе ``source.status`` пуст у ВСЕХ 13 236 строк
(и у X есть строки с NULL). Поэтому «только ``active``» трактуется как
«``active`` ИЛИ статус не проставлен»: исключаются ЯВНО неактивные статусы
(``candidate``/``rejected``/``dead``/``private``/``provisional``). Иначе доска
YouTube обнулилась бы, а требуемые к сохранению карточки (×486 Avocado Theory —
``status=NULL``, ×307 Kolibri — ``status=active``) пропали бы. Это осознанное
расширение буквы ТЗ ради его же §3 (пункт «настоящие виральные карточки
остались»).
"""

from __future__ import annotations

from collections.abc import Iterable

from tuber.analysis import ads as ads_mod

#: Доля промо-постов, при которой канал считается промо-источником (ТЗ-8 §1.2).
PROMO_HEAVY_SHARE = 0.5
#: Минимальное число материалов канала, чтобы доля была осмысленной.
PROMO_HEAVY_MIN_ITEMS = 5

#: Явно неактивные статусы реестра: их материалы в борду не берём.
NON_ELIGIBLE_STATUSES = frozenset(
    {"candidate", "rejected", "dead", "private", "provisional"})


def source_status_eligible(status: str | None) -> bool:
    """Допускать ли материал источника в борду по ``source.status`` (ТЗ-8 §1.2).

    ``active`` и пустой статус (легаси-строки YouTube/X без реестра) — да;
    явно неактивные статусы (:data:`NON_ELIGIBLE_STATUSES`) — нет.
    """
    s = (status or "").strip().lower()
    if not s:
        return True
    return s == "active"


def is_candidate(status: str | None) -> bool:
    """Источник — неверифицированный кандидат (для счётчика ``skipped_candidate``)."""
    return (status or "").strip().lower() == "candidate"


def is_promo_heavy(conn, source_id, *, share: float = PROMO_HEAVY_SHARE,
                   min_items: int = PROMO_HEAVY_MIN_ITEMS) -> bool:
    """True, если источник в основном постит промо (ТЗ-8 §1.2).

    Доля промо-постов считается по ВСЕЙ истории источника (``content`` +
    :func:`tuber.analysis.ads.is_promotional`). Пустые тексты в знаменатель НЕ
    идут (нечего классифицировать). При числe материалов меньше ``min_items``
    доля не считается — возвращается ``False`` (не выдумываем по 1–2 постам).
    """
    if source_id is None:
        return False
    rows = conn.execute(
        "SELECT text FROM content WHERE source_id = ? AND deleted_at IS NULL",
        (source_id,)).fetchall()
    items = 0
    promo = 0
    for row in rows:
        try:
            text = row["text"]
        except (TypeError, KeyError, IndexError):
            text = row[0]
        if not (text or "").strip():
            continue
        items += 1
        if ads_mod.is_promotional(text):
            promo += 1
    if items < min_items:
        return False
    return (promo / items) >= share


def promo_heavy_stats(conn, source_id, *,
                      share: float = PROMO_HEAVY_SHARE,
                      min_items: int = PROMO_HEAVY_MIN_ITEMS) -> dict:
    """Разбор источника: всего/промо/доля/вердикт (для отчёта и отладки)."""
    rows = conn.execute(
        "SELECT text FROM content WHERE source_id = ? AND deleted_at IS NULL",
        (source_id,)).fetchall()
    items = promo = 0
    for row in rows:
        try:
            text = row["text"]
        except (TypeError, KeyError, IndexError):
            text = row[0]
        if not (text or "").strip():
            continue
        items += 1
        if ads_mod.is_promotional(text):
            promo += 1
    frac = (promo / items) if items else 0.0
    return {"source_id": source_id, "items": items, "promo": promo,
            "share": frac, "promo_heavy": items >= min_items and frac >= share}


def promo_heavy_sources(conn, source_ids: Iterable, *,
                        share: float = PROMO_HEAVY_SHARE,
                        min_items: int = PROMO_HEAVY_MIN_ITEMS,
                        cutoff: str | None = None) -> set:
    """Пакетный вариант :func:`is_promo_heavy` для множества ``source_ids``.

    Один проход по ``content`` (без N+1 запросов на доску), возвращает множество
    id промо-источников. Нужен отбору/борде, чтобы пометить источник до
    ранжирования.

    ``cutoff`` — необязательная нижняя граница окна (ISO UTC
    ``%Y-%m-%d %H:%M:%S``). По умолчанию ``None`` — поведение прежнее, по ВСЕЙ
    истории источника (обратная совместимость для внешних вызовов). Внутренние
    вызовы отчёта передают тот же порог, что и остальной отбор борды: промо-тяжесть
    и норма автора считаются по ОДНОМУ окну, а не по всей базе (иначе один проход
    сканировал все ~240 тыс. текстов ≈ 4,4 мин; замер 08.10.2026). Фильтр идёт по
    ``content.published_at`` (``content_latest`` даты не хранит).
    """
    ids = [int(s) for s in source_ids if s is not None]
    if not ids:
        return set()
    stats: dict = {sid: [0, 0] for sid in ids}  # sid -> [items, promo]
    # Батчи по 500: у SQLite ограничено число плейсхолдеров, а пулы X/YouTube
    # могут нести тысячи источников.
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        params: list = list(chunk)
        window = ""
        if cutoff:
            window = " AND published_at >= ?"
            params.append(cutoff)
        rows = conn.execute(
            "SELECT source_id, text FROM content"
            f" WHERE source_id IN ({placeholders}) AND deleted_at IS NULL"
            f"{window}",
            tuple(params)).fetchall()
        for row in rows:
            try:
                sid, text = row["source_id"], row["text"]
            except (TypeError, KeyError, IndexError):
                sid, text = row[0], row[1]
            if sid is None or not (text or "").strip():
                continue
            bucket = stats.setdefault(int(sid), [0, 0])
            bucket[0] += 1
            if ads_mod.is_promotional(text):
                bucket[1] += 1
    out = set()
    for sid, (items, promo) in stats.items():
        if items >= min_items and items and (promo / items) >= share:
            out.add(sid)
    return out
