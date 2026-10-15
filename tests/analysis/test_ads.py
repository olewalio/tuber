"""Тесты ТЗ-8: детектор рекламы/промо, промо-источники и метка «пересказ».

Проверяем на РЕАЛЬНЫХ строках замера боевой борды 08.10.2026
(``reports/boards/boards.json``) и на синтетической мини-базе:

* :func:`tuber.analysis.ads.is_promotional` — позитивы (реклама Альфа-Банка,
  «Реклама World Camera», «Реклама Telegram-бота Буквица») и негативы (настоящие
  виральные заголовки того же дня), чтобы фильтр не «выкосил живое»;
* :func:`tuber.analysis.source_quality.is_promo_heavy` / ``promo_heavy_sources``;
* отсев источника ``status='candidate'`` из борды (счётчик ``skipped_candidate``);
* отсев промо-тяжёлого канала (счётчик ``skipped_promo_source``);
* агрегатор остаётся в выдаче, но с ``origin='retelling'``.

Сеть и LLM не используются (LLM-проход выключен по умолчанию).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tuber.analysis import ads, report, source_quality
from tuber.core import db, schema

ROOT = Path(__file__).resolve().parents[2]


def _load_build_boards():
    path = ROOT / "tools" / "build_boards.py"
    spec = importlib.util.spec_from_file_location("build_boards_ads", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


build_boards = _load_build_boards()


def _iso(days_ago: float = 1.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# §1.1. Детектор рекламы: позитивы из замера
# --------------------------------------------------------------------------- #
POSITIVES = [
    (
        "КЭШБЭК ДО 40% на спокойствие в вашем доме. Это Альфа-Пятница в «Квартире "
        "под защитой» ❤️🏠"
    ),
    (
        "Вам пришёл ПУШ — от тарифа «СВОБОДНЫЙ» в Альфа-Мобайл 😂❤️ Он расскажет "
        "вам, что интернет и звонки бывают безлимитными."
    ),
    "Реклама World Camera: съёмочная команда из одного кадра",
    "Реклама Telegram-бота Буквица: подпишись и получи разбор",
    "Промокод на скидку 50% — переходи по ссылке в описании",
    "Розыгрыш iPhone: подписывайтесь и жми на кнопку",
]

#: Настоящие виральные заголовки того же дня — НЕ реклама.
NEGATIVES = [
    "Новая эра ИИ только началась",
    ("Модель Kolibri на 78B параметров (3,46B активных) с контекстом до 1M "
     "токенов выпущена в Европе под Apache 2.0."),
    "Как SpaceX могла бы выводить серверы на орбиту",
    ("Представлена модель Griffin, первая прошедшая видео-тест Тьюринга: 48% "
     "собеседников приняли её за человека."),
    ("У «Самолета» случился техдефолт: застройщик выплатил только около 670 млн "
     "рублей из 2,5 млрд по облигациям, сообщает НРД."),
    ("09.10.2026 с 16:00 по 17:00 буду выступать на Дизайн-выходных в Туле. "
     "Приходите! https://designweekend.ru/tula2026"),
    ("«МАГИЧЕСКАЯ БИТВА» СКОПИРОВАЛА «КЛИНОК»? Фанаты заметили сходство — Аниме "
     "Новости #53"),
    "Follow + comment GPU and I'll send you the full install guide.",
    "Small bird, fast wings, Kolibri is here. 78B parameters. 3.46B active.",
]


def test_is_promotional_positive_real_headlines():
    for text in POSITIVES:
        assert ads.is_promotional(text) is True, text


def test_is_promotional_negative_real_headlines():
    for text in NEGATIVES:
        assert ads.is_promotional(text) is False, text


def test_is_promotional_empty_and_service():
    assert ads.is_promotional("") is False
    assert ads.is_promotional("   ") is False
    assert ads.is_promotional("Image") is False


def test_promo_signals_explain_decision():
    sig = ads.promo_signals("КЭШБЭК ДО 40% на спокойствие")
    assert sig["marker"] is True
    assert ads.promo_signals("Обычная новость про ИИ")["marker"] is False


def test_llm_second_pass_off_by_default(monkeypatch):
    monkeypatch.delenv(ads.ADS_LLM_ENV_FLAG, raising=False)
    assert ads.ads_llm_enabled() is False
    # Флаг выключен — функция возвращает результат первого прохода, без сети.
    assert ads.refine_promotional(None, "Обычный пост") is False


# --------------------------------------------------------------------------- #
# §1.3. Агрегаторы и пересказы
# --------------------------------------------------------------------------- #
def test_is_aggregator_markers():
    assert ads.is_aggregator("Источник: Reuters. Компания закрыла раунд.")
    assert ads.is_aggregator("Новость. По материалам «Ведомостей».")
    assert ads.is_aggregator("via @somechannel текст")
    assert ads.is_aggregator("https://t.me/src/1\n\nтолько ссылка")
    assert not ads.is_aggregator("Модель Kolibri выпущена под Apache 2.0.")


def test_classify_origin():
    assert ads.classify_origin("Источник: Reuters") == "retelling"
    assert ads.classify_origin("Обычная новость") == "original"


# --------------------------------------------------------------------------- #
# Синтетическая мини-база: статус, промо-источник, origin
# --------------------------------------------------------------------------- #
def _build_db(tmp_path) -> str:
    """Telegram-мини-ядро: промо-канал, канал-кандидат и хороший канал.

    * ``promo_chan`` (active): 6 постов, 5 из них — промо → промо-источник;
    * ``cand_chan`` (candidate): 1 виральный пост → не должен попасть в борду;
    * ``good_chan`` (active): 2 обычных поста, один — с меткой «Источник:» →
      остаётся, но с ``origin='retelling'``.
    """
    path = str(tmp_path / "ads.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    cid = 5_000
    with db.write_tx(conn):
        specs = [
            (1, "promo_chan", "Промо-канал", "active", 1000),
            (2, "cand_chan", "Канал-кандидат", "candidate", 300),
            (3, "good_chan", "Хороший канал", "active", 500),
        ]
        for sid, handle, title, status, base in specs:
            conn.execute(
                "INSERT INTO source(id,platform,external_id,handle,title,lang,"
                "status) VALUES (?,?,?,?,?,'ru',?)",
                (sid, "telegram", f"tg{sid}", handle, title, status))
            conn.execute(
                "INSERT INTO source_baseline(source_id,window_days,median_views,"
                "computed_at) VALUES (?,90,?,?)", (sid, base, _iso(0.5)))

        def post(source_id, text, views, external_id):
            nonlocal cid
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,url,lang) VALUES (?,?,?,?,?,?,?,'ru')",
                (cid, "telegram", source_id, external_id, _iso(1.0), text,
                 f"https://t.me/{external_id}"))
            conn.execute(
                "INSERT INTO content_latest(content_id,views,platform,"
                "external_id) VALUES (?,?,'telegram',?)",
                (cid, views, external_id))
            cid += 1

        # Промо-канал: 5 промо + 1 обычный (доля 5/6 ≥ 0.5 при 6 материалах).
        for i in range(5):
            post(1, f"СКИДКА {20 + i}% — промокод, переходи по ссылке",
                 900_000 + i, f"promo_chan/{i}")
        post(1, "Обычная редкая новость промо-канала", 800_000, "promo_chan/5")

        # Канал-кандидат: один виральный пост.
        post(2, "Обычная новость канала-кандидата", 999_999, "cand_chan/1")

        # Хороший канал: обычный пост и пост-агрегатор.
        post(3, "Хорошая новость без меток", 200_000, "good_chan/1")
        post(3, "Источник: Reuters. Другая новость как есть.", 150_000,
             "good_chan/2")
    conn.close()
    return path


def test_candidate_source_excluded_from_board(tmp_path):
    """ТЗ-8 §1.2: ``status='candidate'`` не попадает в карточки, счётчик растёт."""
    path = _build_db(tmp_path)
    conn = db.connect(path, readonly=True)
    try:
        pool = report.viral_tg_pool(conn, _iso(10))
        items, stats = report.viral_telegram_items(pool, _iso(10))
    finally:
        conn.close()
    channels = {it.channel for it in items}
    assert "Канал-кандидат" not in channels, [c for c in channels]
    assert stats["skipped_candidate"] >= 1, stats


def test_promo_heavy_source_excluded(tmp_path):
    """ТЗ-8 §1.2: промо-тяжёлый канал исключается, счётчик ``skipped_promo_source``."""
    path = _build_db(tmp_path)
    conn = db.connect(path, readonly=True)
    try:
        promo = source_quality.promo_heavy_sources(conn, [1, 2, 3])
        assert 1 in promo and 3 not in promo, promo
        assert source_quality.is_promo_heavy(conn, 1) is True
        assert source_quality.is_promo_heavy(conn, 3) is False
        pool = report.viral_tg_pool(conn, _iso(10))
        items, stats = report.viral_telegram_items(
            pool, _iso(10), promo_sources=promo)
    finally:
        conn.close()
    channels = {it.channel for it in items}
    assert "Промо-канал" not in channels, channels
    assert stats["skipped_promo_source"] >= 1, stats


def test_aggregator_kept_with_retelling_label(tmp_path):
    """ТЗ-8 §1.3: агрегатор НЕ удаляется, а получает ``origin='retelling'``."""
    path = _build_db(tmp_path)
    out = tmp_path / "b" / "index.html"
    data = build_boards.build(path, days=10, limit=15, out_path=str(out))
    tg = data["boards"]["telegram"]
    agg = [c for c in tg["cards"] if c["title"].startswith("Источник:")]
    assert agg, tg["cards"]
    assert agg[0]["origin"] == "retelling"
    assert any(c.get("origin") == "original" for c in tg["cards"])
    # Промо-источник и кандидат в карточки не попали, счётчики сохранены.
    channels = {c["channel"] for c in tg["cards"]}
    assert "Промо-канал" not in channels, channels
    assert "Канал-кандидат" not in channels, channels
    assert tg["skipped_candidate"] >= 1, tg
    assert tg["skipped_promo_source"] >= 1, tg
    # Присутствие настоящего «живого» поста — фильтр не выкосил всё.
    assert any("Хорошая новость" in c["title"] for c in tg["cards"])


def test_source_status_eligible_rule():
    assert source_quality.source_status_eligible("active") is True
    assert source_quality.source_status_eligible(None) is True
    assert source_quality.source_status_eligible("") is True
    assert source_quality.source_status_eligible("candidate") is False
    assert source_quality.source_status_eligible("rejected") is False
    assert source_quality.source_status_eligible("dead") is False
    assert source_quality.source_status_eligible("private") is False
    assert source_quality.source_status_eligible("provisional") is False


def test_promo_heavy_sources_window_ignores_old_posts(tmp_path):
    """ТЗ-10: промо-тяжесть считается по окну — старые посты вне окна не влияют.

    Источник: 5 СТАРЫХ (30 дней) промо-постов + 1 обычный свежий. По всей истории
    он промо-тяжёлый (5/6 ≥ 0.5), но в окне 10 дней промо-постов 0 из 1 → не
    промо-тяжёлый. Окно обязано отсекать старые посты, а не сканировать всё.
    """
    path = str(tmp_path / "window.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title,lang,status)"
            " VALUES (1,'telegram','tgw','w','Бывший промо-канал','ru','active')")
        cid = 9_000
        for i in range(5):  # старые промо — ВНЕ окна
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,url,lang) VALUES (?,?,?,?,?,?,?,'ru')",
                (cid, "telegram", 1, f"w/old{i}", _iso(30.0),
                 f"СКИДКА {i}% — промокод, переходи по ссылке",
                 f"https://t.me/w/old{i}"))
            cid += 1
        conn.execute(  # свежий обычный — внутри окна
            "INSERT INTO content(id,platform,source_id,external_id,"
            "published_at,text,url,lang) VALUES (?,?,?,?,?,?,?,'ru')",
            (cid, "telegram", 1, "w/new", _iso(1.0),
             "Обычная свежая новость без меток", "https://t.me/w/new"))
    conn.close()

    conn = db.connect(path, readonly=True)
    try:
        # Обратная совместимость: без окна (cutoff=None) считаем по всей истории.
        assert 1 in source_quality.promo_heavy_sources(conn, [1])
        # Окно 10 дней: старые промо НЕ учитываются → источник не промо-тяжёлый.
        assert 1 not in source_quality.promo_heavy_sources(
            conn, [1], cutoff=_iso(10))
        # Тот же порог прокинут из отчёта (ТЗ-10 §1).
        assert 1 not in report.viral_promo_sources(conn, [1], _iso(10))
    finally:
        conn.close()
