#!/usr/bin/env python3
"""Графики к странице «Что залетело» (ТЗ-4 §4).

Три PNG кладутся рядом с ``index.html`` и подключаются обычными
``<img src="имя.png" alt="…">`` (НЕ base64):

* ``topics_daily.png`` — динамика топ-5 тем по дням окна (линии);
* ``topics_bar.png``   — топ-12 тем по числу материалов (горизонтальные полосы);
* ``platform_daily.png`` — поступление по платформам по дням (X/YouTube/Telegram).

Честность чисел:

* только чтение боевой базы (``file:…?mode=ro``), без сети и LLM;
* окно — то же КАЛЕНДАРНОЕ, что у раздела «Темы окна» (полночь ``сегодня −
  days`` UTC): заголовки берут диапазон дат из фактических данных;
* пустой день — честный ноль (дни инициализируются нулём, линия не врёт);
* подписи осей и легенд русские, шрифт крупный (страница читается с телефона),
  эмодзи нет; числа на осях — целые, без экспоненциальной записи;
* похожесть темы — только по фактическим ярлыкам ``classification.topic``;
  NULL/пусто/«(без темы)» не считаются.

Запуск::

    python3 tools/build_charts.py --days 10
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Рисуем без экрана (крон/сервер): только файлы.
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tuber import config
from tuber.core import db as core_db

#: Крупный шрифт и мобильная ширина: страница читается с телефона.
plt.rcParams.update({
    "font.size": 13,
    "axes.titlesize": 16,
    "axes.labelsize": 14,
    "xtick.labelsize": 12,
    "ytick.labelsize": 12,
    "legend.fontsize": 12,
    "figure.dpi": 130,
    "savefig.dpi": 130,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.grid": True,
    "grid.alpha": 0.25,
    "axes.axisbelow": True,
})
#: Размер фигуры под мобильную ширину (~1040 px при dpi 130).
FIG_SIZE = (8.0, 5.0)
#: Цвета и подписи платформ — как на странице.
PLATFORM_COLORS = {"x": "#0b0d0f", "youtube": "#b3261e", "telegram": "#20649b"}
PLATFORM_LABELS = {"x": "X", "youtube": "YouTube", "telegram": "Телеграм"}
_PLATFORM_ORDER = ("x", "youtube", "telegram")
#: Ярлык-заглушка темы, не участвует в графиках.
_TOPIC_BLANK = "(без темы)"
#: Сколько тем на линейном и сколько на полосовом графике.
DAILY_TOPICS = 5
BAR_TOPICS = 12


def _window(days: int, now: datetime | None = None) -> tuple[date, date, list]:
    """Календарное окно как у раздела тем: список дней (старт … сегодня)."""
    now = now or datetime.now(timezone.utc)
    start = now.date() - timedelta(days=days)
    days_list = [start + timedelta(days=i) for i in range(days + 1)]
    return start, now.date(), days_list


def _topic_ok(topic) -> bool:
    t = " ".join((topic or "").split())
    return bool(t) and t.lower() != _TOPIC_BLANK


def _daily_topic_counts(conn, start: date, end: date) -> dict:
    """``{topic: {день: материалов}}`` — с честным нулём по всем дням окна."""
    lo = start.isoformat() + " 00:00:00"
    hi = (end + timedelta(days=1)).isoformat() + " 00:00:00"
    rows = conn.execute(
        "SELECT date(c.published_at) AS d, cl.topic AS t, COUNT(*) AS n"
        " FROM classification cl JOIN content c ON c.id = cl.content_id"
        " WHERE c.published_at >= ? AND c.published_at < ?"
        "   AND cl.topic IS NOT NULL AND TRIM(cl.topic) <> ''"
        " GROUP BY d, t", (lo, hi))
    days_list = [start + timedelta(days=i)
                 for i in range((end - start).days + 1)]
    out: dict = defaultdict(lambda: {d: 0 for d in days_list})
    for row in rows:
        topic = row["t"]
        if not _topic_ok(topic):
            continue
        try:
            d = date.fromisoformat(str(row["d"])[:10])
        except (TypeError, ValueError):
            continue
        if d in out[topic]:
            out[topic][d] = int(row["n"])
    return out


def _daily_platform_counts(conn, start: date, end: date) -> dict:
    """``{platform: {день: материалов}}`` — платформы базы, честный ноль."""
    lo = start.isoformat() + " 00:00:00"
    hi = (end + timedelta(days=1)).isoformat() + " 00:00:00"
    rows = conn.execute(
        "SELECT date(c.published_at) AS d, c.platform AS p, COUNT(*) AS n"
        " FROM content c"
        " WHERE c.published_at >= ? AND c.published_at < ?"
        " GROUP BY d, p", (lo, hi))
    days_list = [start + timedelta(days=i)
                 for i in range((end - start).days + 1)]
    out: dict = {p: {d: 0 for d in days_list} for p in _PLATFORM_ORDER}
    for row in rows:
        p = row["p"]
        if p not in out:
            continue
        try:
            d = date.fromisoformat(str(row["d"])[:10])
        except (TypeError, ValueError):
            continue
        if d in out[p]:
            out[p][d] = int(row["n"])
    return out


def _range_label(start: date, end: date) -> str:
    return f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"


def _int_formatter():
    """Целые подписи без экспоненты и смещения."""
    return FuncFormatter(lambda v, _pos: f"{round(v):d}")


def _save(fig, path: Path) -> int:
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path.stat().st_size


def chart_topics_daily(conn, start: date, end: date, out: Path) -> int:
    """Линейный график динамики топ-5 тем по дням окна."""
    counts = _daily_topic_counts(conn, start, end)
    totals = {t: sum(v.values()) for t, v in counts.items()}
    top = sorted(totals, key=lambda t: (-totals[t], t))[:DAILY_TOPICS]
    days_list = sorted(next(iter(counts.values())).keys()) if counts else []
    fig, ax = plt.subplots(figsize=FIG_SIZE)
    xs = list(range(len(days_list)))
    for topic in top:
        ys = [counts[topic][d] for d in days_list]
        ax.plot(xs, ys, marker="o", linewidth=2.2, label=topic)
    ax.set_xticks(xs)
    ax.set_xticklabels([d.strftime("%d.%m") for d in days_list],
                       rotation=45, ha="right")
    ax.set_xlabel("День окна")
    ax.set_ylabel("Материалов за день")
    ax.set_title("Темы окна: динамика топ-5 по дням\n" + _range_label(start, end))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.yaxis.set_major_formatter(_int_formatter())
    ax.legend(loc="best", framealpha=0.9, fontsize=11)
    return _save(fig, out)


def chart_topics_bar(conn, start: date, end: date, out: Path) -> int:
    """Горизонтальные полосы: топ-12 тем по числу материалов."""
    counts = _daily_topic_counts(conn, start, end)
    totals = {t: sum(v.values()) for t, v in counts.items()}
    top = sorted(totals, key=lambda t: (-totals[t], t))[:BAR_TOPICS]
    top.reverse()  # крупнейшая — сверху после invert
    fig, ax = plt.subplots(figsize=(8.0, 6.4))
    ax.barh(range(len(top)), [totals[t] for t in top], color="#20649b")
    ax.set_yticks(range(len(top)))
    ax.set_yticklabels(top)
    ax.set_xlabel("Материалов за окно")
    ax.set_title("Темы окна: топ-" + str(len(top)) +
                 " по числу материалов\n" + _range_label(start, end))
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.xaxis.set_major_formatter(_int_formatter())
    ax.grid(axis="y", visible=False)
    return _save(fig, out)


def chart_platform_daily(conn, start: date, end: date, out: Path) -> int:
    """Поступление материалов по платформам по дням."""
    counts = _daily_platform_counts(conn, start, end)
    days_list = sorted(next(iter(counts.values())).keys())
    fig, ax = plt.subplots(figsize=FIG_SIZE)
    xs = list(range(len(days_list)))
    for platform in _PLATFORM_ORDER:
        ys = [counts[platform][d] for d in days_list]
        ax.plot(xs, ys, marker="o", linewidth=2.2,
                color=PLATFORM_COLORS[platform],
                label=PLATFORM_LABELS[platform])
    ax.set_xticks(xs)
    ax.set_xticklabels([d.strftime("%d.%m") for d in days_list],
                       rotation=45, ha="right")
    ax.set_xlabel("День окна")
    ax.set_ylabel("Материалов за день")
    ax.set_title("Поступление по платформам по дням\n"
                 + _range_label(start, end))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.yaxis.set_major_formatter(_int_formatter())
    ax.legend(loc="best", framealpha=0.9)
    return _save(fig, out)


def build_charts(db_path: str, out_dir: str, days: int = 10) -> dict:
    """Собрать три PNG в ``out_dir``; вернуть ``{файл: размер_в_байтах}``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    start, end, _ = _window(days)
    conn = core_db.connect(db_path, readonly=True)
    try:
        sizes = {
            "topics_daily.png": chart_topics_daily(
                conn, start, end, out / "topics_daily.png"),
            "topics_bar.png": chart_topics_bar(
                conn, start, end, out / "topics_bar.png"),
            "platform_daily.png": chart_platform_daily(
                conn, start, end, out / "platform_daily.png"),
        }
    finally:
        conn.close()
    return sizes


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_charts",
        description="Собрать PNG-графики для страницы «Что залетело»")
    parser.add_argument("--days", type=int, default=10,
                        help="окно в днях (по умолчанию 10)")
    parser.add_argument("--out", default=str(_ROOT / "reports" / "boards"),
                        help="куда писать PNG (по умолчанию reports/boards)")
    parser.add_argument("--db", dest="db_path", default=None,
                        help="путь к единой базе (по умолчанию боевая)")
    args = parser.parse_args(argv)

    db_path = config.db_path(args.db_path)
    if not Path(db_path).exists():
        print(f"ошибка: базы нет: {db_path}", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc)
    sizes = build_charts(db_path, args.out, args.days)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    start, end, _ = _window(args.days)
    print(f"графики в: {args.out} (окно {_range_label(start, end)})")
    for name, size in sizes.items():
        print(f"  {name}: {size} байт")
    print(f"сборка {elapsed:.2f} с")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
