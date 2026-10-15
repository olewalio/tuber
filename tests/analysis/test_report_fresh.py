"""Тесты раздела сводки «Новое за сутки — вышедшее за 24 ч:» (ТЗ-Tuber ч.2).

Проверяются: окно 24 ч по ``content.published_at``, ранжирование YouTube по
просмотрам/сутки, X — по лайкам/час (не по абсолютным лайкам), дедупликация по
``content_id`` с разделами 1–3, предел 10 позиций, честное пустое состояние и
целостность ссылок. Сеть и LLM не используются.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from tuber.analysis import report
from tests.analysis.test_report_viral import _x, _yt, build_core


def _hours_ago(hours: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _fresh_region(text: str) -> str:
    start = text.index("Новое за сутки — вышедшее за 24 ч:")
    end = text.index("Сквозной сюжет:", start)
    return text[start:end]


# --------------------------------------------------------------------------- #
# 1. Окно 24 ч: свежий материал попадает, старше суток — нет
# --------------------------------------------------------------------------- #
def test_fresh_window_hours_boundary(tmp_path):
    yt = [
        _yt(1, "свежее видео", 1_000, "наука и медицина", 100_000, vpd=100,
            age=0.2),
        _yt(2, "видео двухдневной давности", 1_000, "наука и медицина", 100_000,
            vpd=100, age=2.0),
    ]
    conn = build_core(tmp_path, yt=yt)
    items = report.new_today_items(
        conn, _hours_ago(report.FRESH_WINDOW_HOURS), set())
    titles = [it.desc for it in items]
    assert "свежее видео" in titles
    assert "видео двухдневной давности" not in titles
    assert report.FRESH_WINDOW_HOURS == 24


# --------------------------------------------------------------------------- #
# 2. YouTube ранжируется по просмотрам/сутки (убыв.)
# --------------------------------------------------------------------------- #
def test_fresh_youtube_ranked_by_views_per_day(tmp_path):
    yt = [
        _yt(1, "медленное", 1_000, "наука и медицина", 100_000, vpd=100, age=0.3),
        _yt(2, "быстрое", 1_000, "наука и медицина", 100_000, vpd=900, age=0.3),
    ]
    conn = build_core(tmp_path, yt=yt)
    items = report.new_today_items(
        conn, _hours_ago(report.FRESH_WINDOW_HOURS), set())
    titles = [it.desc for it in items]
    assert titles.index("быстрое") < titles.index("медленное")


# --------------------------------------------------------------------------- #
# 3. X ранжируется по лайкам/час, а не по абсолютным лайкам
# --------------------------------------------------------------------------- #
def test_fresh_x_ranked_by_likes_per_hour(tmp_path):
    # A: 100 лайков за 1 ч = 100/ч; B: 300 лайков за 20 ч = 15/ч.
    x = [
        _x("authora", 100, text="X быстрый", age=1.0 / 24),
        _x("authorb", 300, text="X медленный", age=20.0 / 24),
    ]
    conn = build_core(tmp_path, x=x)
    items = report.new_today_items(
        conn, _hours_ago(report.FRESH_WINDOW_HOURS), set())
    titles = [it.desc for it in items]
    assert titles.index("X быстрый") < titles.index("X медленный")


# --------------------------------------------------------------------------- #
# 4. Не более 10 позиций раздела
# --------------------------------------------------------------------------- #
def test_fresh_capped_at_ten(tmp_path):
    # Разные каналы: проверяем именно предел раздела в 10 позиций; кэп на
    # автора проверяется отдельно (test_fresh_capped_per_author).
    yt = [_yt(i, f"видео {i}", 1_000, "наука и медицина", 100_000,
              vpd=100 + i, age=0.3) for i in range(15)]
    conn = build_core(tmp_path, yt=yt)
    items = report.new_today_items(
        conn, _hours_ago(report.FRESH_WINDOW_HOURS), set())
    assert len(items) == report.FRESH_LIMIT == 10


def test_fresh_capped_per_author(tmp_path):
    """Кэп на канал/автора действует и в «Новом за сутки» (ТЗ-Tuber §3.1).

    Пять свежих видео одного канала дают не больше :data:`report.MAX_PER_AUTHOR`
    позиций; раздел добирается видео других каналов.
    """
    yt = [_yt(1, f"одноканальное {i}", 1_000, "наука и медицина", 100_000,
              vpd=1_000 - i, age=0.3) for i in range(5)]
    yt += [_yt(2, "другой канал", 1_000, "наука и медицина", 100_000,
               vpd=1, age=0.3)]
    conn = build_core(tmp_path, yt=yt)
    items = report.new_today_items(
        conn, _hours_ago(report.FRESH_WINDOW_HOURS), set())
    one_channel = [it for it in items if "одноканальное" in it.desc]
    assert len(one_channel) == report.MAX_PER_AUTHOR == 2
    assert any("другой канал" in it.desc for it in items)  # добор разделу


# --------------------------------------------------------------------------- #
# 5. Пустое состояние: раздел печатается всегда и честно сообщает «нет»
# --------------------------------------------------------------------------- #
def test_fresh_empty_state_printed(tmp_path):
    yt = [_yt(1, "старое видео", 1_000, "наука и медицина", 100_000, vpd=100,
              age=3.0)]
    conn = build_core(tmp_path, yt=yt, x=[_x("author1", 50, age=3.0)])
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert "Новое за сутки — вышедшее за 24 ч:" in text
    assert "нет новых материалов за сутки." in text


# --------------------------------------------------------------------------- #
# 6. Дедупликация: материал из раздела 1 не повторяется в «Новом за сутки»
# --------------------------------------------------------------------------- #
def test_fresh_dedup_with_section1(tmp_path):
    # 20 старых видео заполняют раздел 1 (топ-15 по views/сутки); свежее видео с
    # самым большим vpd тоже попадает в раздел 1 и потому в «Новое» не идёт.
    yt = [_yt(9, f"старое {i}", 1_000, "наука и медицина", 60_000,
              vpd=10 + i, age=2.0) for i in range(20)]
    yt.append(_yt(1, "свежий хит", 1_000, "наука и медицина", 200_000,
                  vpd=10_000, age=0.2))
    # Свежее «низкое» видео в топ-15 не входит — это и есть материал раздела.
    # Тема «прочее» вне PRODUCT_TOPICS, поэтому «Виральные» его не забирают.
    yt.append(_yt(2, "свежий хвост", 1_000, "прочее", 40_000,
                  vpd=1, age=0.2))
    conn = build_core(tmp_path, yt=yt)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    regional = _fresh_region(text)
    assert "свежий хвост" in regional
    assert "свежий хит" not in regional
    # «свежий хит» показывается ровно одной строкой — в разделе 1.
    hit_lines = [ln for ln in text.splitlines() if "свежий хит" in ln]
    assert len(hit_lines) == 1, hit_lines


# --------------------------------------------------------------------------- #
# 7. Ссылка в разделе — полная (не укорочена)
# --------------------------------------------------------------------------- #
def test_fresh_links_are_full(tmp_path):
    yt = [_yt(2, "свежий хвост", 1_000, "наука и медицина", 60_000,
              vpd=1, age=0.2)]
    x = [_x("authorx", 42, text="X свежий", age=0.1)]
    conn = build_core(tmp_path, yt=yt, x=x)
    items = report.new_today_items(
        conn, _hours_ago(report.FRESH_WINDOW_HOURS), set())
    assert items, "в «Новом за сутки» нет ни одной позиции"
    for it in items:
        assert "…" not in it.tail, f"ссылка/цифры укорочены: {it.tail!r}"
        assert re.search(r"; https?://\S+$", it.text), it.text
