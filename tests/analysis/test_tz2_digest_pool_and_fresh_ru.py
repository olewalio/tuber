"""Тесты ТЗ №2: широкий пул кандидатов + добор разделов после памяти выдачи,
честная строка при исчерпании пула и русские описания X в «Новом за сутки».

Сеть и LLM не используются: живой перевод подменяется заглушкой в тесте либо
берётся из кэша ``report_text``. Проверяются требования ТЗ №2 §1.1–§1.4 и
§2.1–§2.3.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.analysis import digest_memory, report
from tuber.core import db
from tests.analysis.test_tz2_report_ru_memory_cap import build_core, _iso


# --------------------------------------------------------------------------- #
# §1.1. Широкий пул кандидатов: кратность и нижняя граница
# --------------------------------------------------------------------------- #
def test_candidate_factor_default_and_env(monkeypatch):
    monkeypatch.delenv("TUBER_CANDIDATE_FACTOR", raising=False)
    assert report.candidate_factor() == report.CANDIDATE_FACTOR == 8
    assert report.candidate_pool(15) == max(15 * 8, 200) == 200
    assert report.candidate_pool(3) == 200          # нижняя граница пула
    assert report.candidate_pool(100) == 800
    monkeypatch.setenv("TUBER_CANDIDATE_FACTOR", "20")
    assert report.candidate_factor() == 20
    assert report.candidate_pool(15) == 300
    monkeypatch.setenv("TUBER_CANDIDATE_FACTOR", "опечатка")
    assert report.candidate_factor() == 8           # откат нельзя сломать
    monkeypatch.setenv("TUBER_CANDIDATE_FACTOR", "0")
    assert report.candidate_factor() == 8


# --------------------------------------------------------------------------- #
# §1.2. Добор раздела из пула после исключения показанных
# --------------------------------------------------------------------------- #
def test_youtube_tops_up_from_wide_pool(tmp_path):
    """Показанные 10 видео исключены, раздел добрал 15 из широкого пула."""
    yt = [(i, {"handle": f"yt{i}", "title": f"видео {i}", "views": 100_000,
               "vpd": 1_000 - i, "age": 2.0}) for i in range(40)]
    conn = build_core(tmp_path, yt=yt)
    try:
        recorded: list = []
        text = report.build_compact(conn, days=10, db_path=":memory:",
                                    limit=200_000, record_sink=recorded)
        assert "видео 0" in text                       # без памяти — видно
        first = [cid for cid, plat, _ in recorded if plat == "youtube"]
        assert len(first) == report.YOUTUBE_LIMIT      # штатный размер
        digest_memory.ensure_schema(conn)
        digest_memory.mark_sent(conn, [(cid, "youtube", "YouTube")
                                       for cid in first[:10]])
        recorded2: list = []
        text2 = report.build_compact(conn, days=10, db_path=":memory:",
                                     limit=200_000, record_sink=recorded2)
        second = [cid for cid, plat, _ in recorded2 if plat == "youtube"]
        # (а) раздел не схлопнулся: добрано до штатного размера из пула.
        assert len(second) == report.YOUTUBE_LIMIT, (first, second)
        # (б) память работает: пересечение близко к нулю.
        assert not (set(first[:10]) & set(second))
        # (в) ни одно показанное ранее видео не повторяется.
        assert all(f"видео {i} — канал" not in text2 for i in range(10))
    finally:
        conn.close()


def test_x_tops_up_from_wide_pool(tmp_path):
    """Показанные 10 X-постов исключены, раздел добрал 15 из широкого пула."""
    x = [{"author": f"a{i}", "source": f"s{i}", "text": f"сырой {i}",
          "likes": 5_000 - i, "age": 0.5, "text_hash": f"h{i}"}
         for i in range(40)]
    conn = build_core(tmp_path, x=x)
    try:
        recorded: list = []
        report.build_compact(conn, days=10, db_path=":memory:",
                             limit=200_000, record_sink=recorded)
        first = [cid for cid, plat, _ in recorded if plat == "x"]
        assert len(first) == report.X_LIMIT
        digest_memory.ensure_schema(conn)
        digest_memory.mark_sent(conn, [(cid, "x", "X") for cid in first[:10]])
        recorded2: list = []
        report.build_compact(conn, days=10, db_path=":memory:",
                             limit=200_000, record_sink=recorded2)
        second = [cid for cid, plat, _ in recorded2 if plat == "x"]
        assert len(second) == report.X_LIMIT, (first, second)
        assert not (set(first[:10]) & set(second))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §1.3. Пул исчерпан — честная строка, а не исчезнувший раздел
# --------------------------------------------------------------------------- #
def test_pool_exhausted_prints_honest_line(tmp_path):
    yt = [(1, {"handle": "yt1", "title": "единственное видео", "views": 100_000,
               "vpd": 900, "age": 5.0})]
    conn = build_core(tmp_path, yt=yt)
    try:
        digest_memory.ensure_schema(conn)
        digest_memory.mark_sent(conn, [(2000, "youtube", "YouTube")])
        text = report.build_compact(conn, days=10, db_path=":memory:",
                                    limit=200_000)
        assert "все позиции показывались" in text
        assert "(пул исчерпан)" in text
    finally:
        conn.close()


def test_partial_pool_exhaustion_line(tmp_path):
    """Пул исчерпан частично: раздел добрал сколько смог и честно сообщил."""
    yt = [(i, {"handle": f"yt{i}", "title": f"видео {i}", "views": 100_000,
               "vpd": 1_000 - i, "age": 2.0}) for i in range(20)]
    conn = build_core(tmp_path, yt=yt)
    try:
        digest_memory.ensure_schema(conn)
        digest_memory.mark_sent(conn, [(2000 + i, "youtube", "YouTube")
                                       for i in range(13)])
        text = report.build_compact(conn, days=10, db_path=":memory:",
                                    limit=200_000)
        assert "пул исчерпан" in text
        assert "осталось 7 из 15" in text
    finally:
        conn.close()


def test_viral_section_kept_when_pool_exhausted(tmp_path, monkeypatch):
    """«Виральные» не исчезают молча, если весь пул показывался ранее (З2.4)."""
    item = report.CompactItem(head="   ", desc="виральное видео",
                              tail=" — охват 286.81; https://youtu.be/x",
                              content_id=777, author_key="yt")
    monkeypatch.setattr(report, "viral_compact_items",
                        lambda conn, cutoff, days: [item])
    yt = [(1, {"handle": "yt1", "title": "обычное", "views": 100_000,
               "vpd": 900, "age": 5.0})]
    conn = build_core(tmp_path, yt=yt)
    try:
        digest_memory.ensure_schema(conn)
        digest_memory.mark_sent(conn, [(777, "viral", "Виральные")])
        text = report.build_compact(conn, days=10, db_path=":memory:",
                                    limit=200_000)
        assert "Виральные — охват против своей аудитории:" in text
        assert "(пул исчерпан)" in text
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §1.4. В память выдачи пишутся только фактически напечатанные позиции
# --------------------------------------------------------------------------- #
def test_record_sink_matches_printed_items(tmp_path):
    yt = [(i, {"handle": f"yt{i}", "title": f"видео {i}", "views": 100_000,
               "vpd": 1_000 - i, "age": 2.0}) for i in range(20)]
    conn = build_core(tmp_path, yt=yt)
    try:
        recorded: list = []
        text = report.build_compact(conn, days=10, db_path=":memory:",
                                    limit=200_000, record_sink=recorded)
        assert recorded, "память пуста — нечего записывать"
        # Каждая записанная позиция реально напечатана (по ссылке/заголовку).
        for cid, plat, _name in recorded:
            row = conn.execute("SELECT url FROM content WHERE id=?", (cid,)).fetchone()
            if row and row[0]:
                assert row[0] in text
            else:
                assert f"видео {cid - 2000}" in text
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §2.1–§2.3. Русские описания X в «Новом за сутки»
# --------------------------------------------------------------------------- #
def _fresh_cutoff() -> str:
    return (datetime.now(timezone.utc)
            - timedelta(hours=report.FRESH_WINDOW_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S")


def test_fresh_x_uses_translator_stub(tmp_path, monkeypatch):
    """X-пост «Нового за сутки» переводится тем же помощником, что раздел 2."""
    x = [{"author": "davidsacks", "source": "davidsacks",
          "text": "Thanks to President Trump's leadership on AI",
          "likes": 5_064, "age": 0.05, "text_hash": "fh1"}]
    conn = build_core(tmp_path, x=x)
    try:
        monkeypatch.setattr(report, "_live_translation_allowed",
                            lambda conn: True)
        monkeypatch.setattr(report, "_translate_and_cache",
                            lambda conn, h, t: "Русский перевод поста")
        items = report.new_today_items(conn, _fresh_cutoff(), set())
        assert items, "«Новое за сутки» пусто"
        descs = [it.desc for it in items]
        assert any("Русский перевод поста" in d for d in descs), descs
        assert not any("Thanks to President" in d for d in descs)
    finally:
        conn.close()


def test_fresh_x_ru_disabled_keeps_raw(tmp_path, monkeypatch):
    """TUBER_RU_DESC=0 возвращает прежнее поведение — сырой текст."""
    x = [{"author": "davidsacks", "source": "davidsacks",
          "text": "Thanks to President Trump's leadership on AI",
          "likes": 5_064, "age": 0.05, "text_hash": "fh2"}]
    conn = build_core(tmp_path, x=x)
    try:
        monkeypatch.setattr(report, "_live_translation_allowed",
                            lambda conn: True)
        monkeypatch.setattr(report, "_translate_and_cache",
                            lambda conn, h, t: "Русский перевод поста")
        monkeypatch.setenv("TUBER_RU_DESC", "0")
        items = report.new_today_items(conn, _fresh_cutoff(), set())
        descs = [it.desc for it in items]
        assert any("Thanks to President" in d for d in descs), descs
        assert not any("Русский перевод" in d for d in descs)
    finally:
        monkeypatch.delenv("TUBER_RU_DESC", raising=False)
        conn.close()


def test_fresh_x_uses_translation_cache(tmp_path):
    """Перевод из кэша ``report_text`` применяется без сети."""
    x = [{"author": "paulg", "source": "paulg",
          "text": "Remember how eagerly the press swallowed stories",
          "likes": 8_976, "age": 0.05, "text_hash": "fh3"}]
    conn = build_core(tmp_path, x=x)
    try:
        with db.write_tx(conn):
            conn.execute(
                "INSERT INTO report_text(text_hash, ru, created_at, src)"
                " VALUES ('fh3','Перевод из кэша', ?, 'model')", (_iso(0.1),))
        items = report.new_today_items(conn, _fresh_cutoff(), set())
        assert any("Перевод из кэша" in it.desc for it in items)
    finally:
        conn.close()
