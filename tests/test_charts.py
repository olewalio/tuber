"""Тесты графикам бордов (tools/build_charts.py, ТЗ-4 §4).

Проверяем: три PNG создаются на синтетической мини-базе и непусты; файлы —
настоящие PNG; пустые дни честно нулевые (линия не врёт); подписи чисел на
осях — целые, без экспоненты. Сеть и LLM не используются.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tuber.core import db, schema

ROOT = Path(__file__).resolve().parents[1]
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _load_build_charts():
    path = ROOT / "tools" / "build_charts.py"
    spec = importlib.util.spec_from_file_location("build_charts", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


build_charts = _load_build_charts()


def _iso(days_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _build_db(tmp_path) -> str:
    path = str(tmp_path / "charts.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    cid = 1
    with db.write_tx(conn):
        def add(platform, days_ago, topic=None):
            nonlocal cid
            conn.execute(
                "INSERT INTO content(id,platform,external_id,published_at,"
                "lang) VALUES (?,?,?,?, 'ru')",
                (cid, platform, f"c{cid}", _iso(days_ago)))
            if topic:
                conn.execute(
                    "INSERT INTO classification(content_id,topic,platform,"
                    "external_id) VALUES (?,?,?,?)",
                    (cid, topic, platform, f"c{cid}"))
            cid += 1

        # Тема «альфа» — только один день (остальные дни должны быть нулём).
        add("telegram", 1.0, "альфа")
        # Тема «бета» — два дня, по несколько материалов.
        for _ in range(3):
            add("youtube", 1.0, "бета")
        for _ in range(2):
            add("x", 3.0, "бета")
        # Материалы без темы — в тематические графики не попадают.
        add("telegram", 2.0, None)
    conn.close()
    return path


def test_charts_created_and_png(tmp_path):
    db_path = _build_db(tmp_path)
    out = tmp_path / "boards"
    sizes = build_charts.build_charts(db_path, str(out), days=10)
    assert set(sizes) == {"topics_daily.png", "topics_bar.png",
                          "platform_daily.png"}
    for name, size in sizes.items():
        f = out / name
        assert f.exists() and size > 0
        assert f.read_bytes()[:8] == _PNG_MAGIC, name


def test_daily_counts_zero_fill_and_exclude_blank(tmp_path):
    db_path = _build_db(tmp_path)
    conn = db.connect(db_path, readonly=True)
    try:
        start = datetime.now(timezone.utc).date() - timedelta(days=10)
        end = datetime.now(timezone.utc).date()
        counts = build_charts._daily_topic_counts(conn, start, end)
    finally:
        conn.close()
    # Тема «альфа» есть в словаре, но пустые дни заполнены честным нулём.
    assert "альфа" in counts
    assert sum(counts["альфа"].values()) == 1
    assert all(isinstance(v, int) for v in counts["альфа"].values())
    assert len(counts["альфа"]) == 11  # старт … сегодня включительно
    assert min(counts["альфа"].values()) == 0
    # «бета»: 3 youtube + 2 x = 5.
    assert sum(counts["бета"].values()) == 5


def test_platform_counts_cover_all_platforms(tmp_path):
    db_path = _build_db(tmp_path)
    conn = db.connect(db_path, readonly=True)
    try:
        start = datetime.now(timezone.utc).date() - timedelta(days=10)
        end = datetime.now(timezone.utc).date()
        counts = build_charts._daily_platform_counts(conn, start, end)
    finally:
        conn.close()
    assert set(counts) == {"x", "youtube", "telegram"}
    # Telegram: 1 материал с темой + 1 без темы = 2 (поле темы не влияет).
    assert sum(counts["telegram"].values()) == 2
    assert sum(counts["youtube"].values()) == 3
    assert sum(counts["x"].values()) == 2


def test_int_formatter_has_no_exponent():
    fmt = build_charts._int_formatter()
    assert fmt(1000, 0) == "1000"
    assert fmt(1_000_000, 0) == "1000000"
    assert "e" not in fmt(2_500_000, 0).lower()


def test_window_days_list():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    start, end, days = build_charts._window(10, now)
    assert start.isoformat() == "2026-09-28"
    assert end.isoformat() == "2026-10-08"
    assert len(days) == 11 and days[0] == start and days[-1] == end
