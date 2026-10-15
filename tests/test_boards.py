"""Тесты витрины «Борды залёта» (tools/build_boards.py).

Проверяем приёмку ТЗ: генератор собирается на синтетической мини-базе; в HTML
ровно три доски (X, YouTube, Телеграм) в правильном порядке; число карточек на
доску равно ``min(limit, найденное)``; нет карточек с пустым заголовком или
каналом; карточки с ``url`` отдают ссылку; на канал/автора не больше двух
карточек; страница самодостаточна (нет внешних ``http(s)://`` в
``<link>``/``<script>``/``<img>``). Сеть и LLM не используются.
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tuber.analysis import report as report_mod
from tuber.core import db, schema

ROOT = Path(__file__).resolve().parents[1]


def _load_build_boards():
    """Загрузить tools/build_boards.py как модуль (tools/ — не пакет)."""
    path = ROOT / "tools" / "build_boards.py"
    spec = importlib.util.spec_from_file_location("build_boards", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


build_boards = _load_build_boards()

PER_KEY = build_boards.PER_KEY


def _iso(days_ago: float = 1.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _build_db(tmp_path) -> str:
    """Мини-ядро: X (3 автора с постами-основами и всплесками), YouTube и TG.

    Запас данных заведомо больше доски, чтобы проверить и кэп на автора, и
    ограничение ``limit``. Один пост без ``url`` — проверка карточки-«дива».
    """
    path = str(tmp_path / "boards.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    cid = 1_000
    with db.write_tx(conn):
        # X: у каждого автора ≥ 5 постов → норма (медиана) осмысленна.
        x_authors = {"alpha": (20, 5_000), "beta": (100, 2_000),
                     "gamma": (50, 1_000)}
        for i, (author, (base, spike)) in enumerate(x_authors.items()):
            sid = 500 + i
            conn.execute(
                "INSERT INTO source(id,platform,external_id,handle,title)"
                " VALUES (?,?,?,?,?)", (sid, "x", f"xd{sid}", author, author))
            for _ in range(5):
                conn.execute(
                    "INSERT INTO content(id,platform,source_id,external_id,"
                    "published_at,text,author_handle,url,is_repost,lang)"
                    " VALUES (?,?,?,?,?,?,?,?,0,'en')",
                    (cid, "x", sid, f"x{cid}", _iso(1.0), f"X {author} base",
                     author, f"https://x.com/{author}/status/{cid}"))
                conn.execute(
                    "INSERT INTO content_latest(content_id,likes,replies,"
                    "platform,external_id) VALUES (?,?,0,'x',?)",
                    (cid, base, f"x{cid}"))
                cid += 1
            # всплеск без url — карточка должна стать <div>, а не <a>
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,author_handle,url,is_repost,lang)"
                " VALUES (?,?,?,?,?,?,?,NULL,0,'en')",
                (cid, "x", sid, f"x{cid}", _iso(1.0), f"X {author} spike",
                 author))
            conn.execute(
                "INSERT INTO content_latest(content_id,likes,replies,platform,"
                "external_id) VALUES (?,?,0,'x',?)", (cid, spike, f"x{cid}"))
            cid += 1

        # X: медиа-пост без текста («Image»). Он был бы главным по кратности
        # (медиана 100, всплеск 100000), но карточки-заглушки быть не должно.
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title)"
            " VALUES (?,?,?,?,?)", (599, "x", "xdelta", "delta", "delta"))
        for _ in range(5):
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,author_handle,url,is_repost,lang)"
                " VALUES (?,?,?,?,?,?,?,?,0,'en')",
                (cid, "x", 599, f"x{cid}", _iso(1.0), "delta base", "delta",
                 f"https://x.com/delta/status/{cid}"))
            conn.execute(
                "INSERT INTO content_latest(content_id,likes,replies,"
                "platform,external_id) VALUES (?,?,0,'x',?)",
                (cid, 100, f"x{cid}"))
            cid += 1
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,"
            "published_at,text,author_handle,url,is_repost,lang)"
            " VALUES (?,?,?,?,?,?,?,?,0,'en')",
            (cid, "x", 599, f"x{cid}", _iso(1.0), "Image", "delta",
             f"https://x.com/delta/status/{cid}"))
        conn.execute(
            "INSERT INTO content_latest(content_id,likes,replies,platform,"
            "external_id) VALUES (?,?,0,'x',?)", (cid, 100_000, f"x{cid}"))
        cid += 1

        # YouTube: канал yt_one — 3 видео (кэп 2), yt_two — 1 видео.
        # yt_small (500 подписчиков) и yt_lowcov (5000 подписчиков, 1000
        # просмотров) в окне, но НЕ проходят полы отчёта: карточек из них быть
        # не должно (ТЗ-3 §1–§2) — иначе список разойдётся со счётчиком шапки.
        yt = [("yt_one", 20000), ("yt_one", 19000), ("yt_one", 18000),
              ("yt_two", 15000), ("yt_small", 9000), ("yt_lowcov", 1000)]
        yt_subs = {"yt_one": 1_000, "yt_two": 1_000, "yt_small": 500,
                   "yt_lowcov": 5_000}
        yt_src = {}
        for name, subs in yt_subs.items():
            sid = len(yt_src) + 1
            yt_src[name] = sid
            conn.execute(
                "INSERT INTO source(id,platform,external_id,handle,title,subs,"
                "lang) VALUES (?,?,?,?,?,?,?)",
                (sid, "youtube", f"UC{name}", name, name, subs, "en"))
        for i, (name, views) in enumerate(yt):
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,lang,title,url,kind)"
                " VALUES (?,?,?,?,?,'en',?,?,'video')",
                (cid, "youtube", yt_src[name], f"v{i}", _iso(1.0),
                 f"видео {name} {i}", f"https://www.youtube.com/watch?v={cid}"))
            conn.execute(
                "INSERT INTO metric_snapshot(content_id,captured_at,"
                "interval_quality,views,views_per_day,platform,external_id)"
                " VALUES (?,?,'ok',?,?, 'youtube',?)",
                (cid, _iso(0.5), views, 1000, f"v{i}"))
            cid += 1
        # yt_one — первоисточник (опережает остальных): метка на карточке (ТЗ-3 §4.4).
        conn.execute(
            "INSERT INTO first_mover(source_id,platform,handle,first_moves,"
            "lead_time_median,computed_at) VALUES (?,?,?,?,?,?)",
            (yt_src["yt_one"], "youtube", "yt_one", 3, -42.5, _iso(0.5)))


        # Telegram: канал tg_one — 3 поста (кэп 2), tg_two — 1 пост.
        tg = [("tg_one", 5000), ("tg_one", 4000), ("tg_one", 3000),
              ("tg_two", 2000)]
        tg_src = {}
        for name in ("tg_one", "tg_two"):
            sid = 100 + len(tg_src)
            tg_src[name] = sid
            conn.execute(
                "INSERT INTO source(id,platform,external_id,handle,title,lang,"
                "status) VALUES (?,?,?,?,?,'ru','active')",
                (sid, "telegram", f"tg{name}", name, name))
            conn.execute(
                "INSERT INTO source_baseline(source_id,window_days,median_views,"
                "computed_at) VALUES (?,90,1000,?)", (sid, _iso(0.5)))
        for i, (name, views) in enumerate(tg):
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,url,lang) VALUES (?,?,?,?,?,?,?,'ru')",
                (cid, "telegram", tg_src[name], f"{name}/{i}", _iso(1.0),
                 f"tg {name} {i}", f"https://t.me/{name}/{i}"))
            conn.execute(
                "INSERT INTO content_latest(content_id,views,platform,"
                "external_id) VALUES (?,?,'telegram',?)",
                (cid, views, f"{name}/{i}"))
            cid += 1
    conn.close()
    return path


class _Collector(HTMLParser):
    """Собрать карточки, доски и внешние ресурсы из HTML."""

    def __init__(self):
        super().__init__()
        self.cards: list[tuple[str, dict]] = []
        self.boards: list[str] = []
        self.panels: list[str] = []
        self.images: list[dict] = []
        self.external: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get("class", "").split()
        if tag == "section" and "board" in classes:
            self.boards.append(a.get("id", ""))
        if tag == "section" and "panel" in classes:
            self.panels.append(a.get("id", ""))
        if tag in ("a", "div") and "card" in classes:
            self.cards.append((tag, a))
        if tag == "img":
            self.images.append(a)
        if tag in ("link", "script", "img"):
            for key, value in attrs:
                if value and value.startswith(("http://", "https://", "//")):
                    self.external.append((tag, key, value))


@pytest.fixture
def built(tmp_path):
    """Собрать витрину на мини-базе и вернуть (html, data, out_path)."""
    db_path = _build_db(tmp_path)
    out = tmp_path / "boards" / "index.html"
    data = build_boards.build(db_path, days=10, limit=4, out_path=str(out))
    return out.read_text(encoding="utf-8"), data, out


def test_generator_builds_without_errors(built):
    html, _data, out = built
    assert out.exists()
    assert (out.parent / "boards.json").exists()
    assert html.startswith("<!doctype html>")
    assert "Что залетело" in html


def test_boards_json_roundtrip(built):
    _, data, out = built
    saved = json.loads((out.parent / "boards.json").read_text(encoding="utf-8"))
    assert saved["boards"].keys() == data["boards"].keys()
    assert saved["days"] == 10 and saved["limit"] == 4


def test_exactly_three_boards_in_order(built):
    html, _data, _out = built
    collector = _Collector()
    collector.feed(html)
    assert collector.boards == ["x", "youtube", "telegram"]


def test_card_count_is_min_limit_found(built):
    html, data, _ = built
    collector = _Collector()
    collector.feed(html)
    for pid in ("x", "youtube", "telegram"):
        board = data["boards"][pid]
        # found — сколько карточек осталось после кэпа на автора (до limit).
        assert board["shown"] == min(4, board["found"])
        in_html = sum(1 for _, a in collector.cards
                      if a.get("data-platform") == pid)
        # В HTML попадают только карточки, а не пустая заглушка доски.
        if board["found"]:
            assert in_html == board["shown"]


def test_no_empty_title_or_channel(built):
    _, data, _ = built
    for pid, board in data["boards"].items():
        for card in board["cards"]:
            assert card["title"].strip(), (pid, card)
            assert card["channel"].strip(), (pid, card)


def test_cards_with_url_have_link(built):
    html, data, _ = built
    collector = _Collector()
    collector.feed(html)
    anchors = [a for tag, a in collector.cards if tag == "a"]
    divs = [a for tag, a in collector.cards if tag == "div"]
    with_url = [c for b in data["boards"].values() for c in b["cards"] if c["url"]]
    without = [c for b in data["boards"].values() for c in b["cards"] if not c["url"]]
    # Каждая карточка-ссылка — это <a> с ровно своим href; без url — <div>.
    assert len(anchors) == len(with_url)
    assert len(divs) == len(without)
    assert {a.get("href") for a in anchors} == {c["url"] for c in with_url}


def test_max_two_cards_per_channel(built):
    _, data, _ = built
    for pid, board in data["boards"].items():
        counts: dict[str, int] = {}
        for card in board["cards"]:
            counts[card["channel"]] = counts.get(card["channel"], 0) + 1
        assert all(n <= PER_KEY for n in counts.values()), (pid, counts)


def test_self_contained_no_external_assets(built):
    html, _, _ = built
    collector = _Collector()
    collector.feed(html)
    assert collector.external == []
    # Никаких внешних скриптов/шрифтов/CDN в разметке.
    assert "<script" not in html.lower()
    for needle in ('<link rel="stylesheet"', "cdn.", "fonts.googleapis"):
        assert needle not in html


def test_telegram_and_youtube_have_cards(built):
    _, data, _ = built
    assert data["boards"]["youtube"]["found"] >= 1
    assert data["boards"]["telegram"]["found"] >= 1


@pytest.mark.parametrize("text", [
    "Image", "image", "IMAGE", "Image 2", "image  12", "Video", "Photo",
    "GIF", "gif 3", "Медиа", "медиа 7", "Untitled", "", "   ",
])
def test_service_label_helper_matches(text):
    assert build_boards._is_blank_or_service_title(text)


@pytest.mark.parametrize("text", [
    "Image of a cat", "New image model released", "GIF maker", "Видео об ИИ",
    "Untitled draft about agents", "Медиапространство", "Photo voltaic",
])
def test_service_label_helper_keeps_real_text(text):
    assert not build_boards._is_blank_or_service_title(text)


def test_service_label_card_is_dropped(built):
    """Медиа-пост «Image» (главный по кратности) на доску не попадает."""
    _, data, _ = built
    x = data["boards"]["x"]
    titles = [c["title"] for c in x["cards"]]
    assert titles, "доска X не должна опустеть из-за заглушки"
    assert not any(t.strip().lower().startswith("image") for t in titles), titles
    # Шапка доски честно показывает found (после отбраковки и кэпа на автора).
    assert x["found"] == len(titles) or x["shown"] == min(4, x["found"])


def test_found_honest_when_only_service_candidates(tmp_path):
    """Заглушка-всплеск отброшена; found считает только настоящие карточки."""
    path = str(tmp_path / "only_stubs.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    cid = 1
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title)"
            " VALUES (1,'x','xone','one','one')")
        for likes, text in ((10, "base"), (10, "base"), (10, "base"),
                            (10, "base"), (10, "base"), (500, "Image")):
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,author_handle,url,is_repost,lang)"
                " VALUES (?,?,?,?,?,?,?,?,0,'en')",
                (cid, "x", 1, f"x{cid}", _iso(1.0), text, "one",
                 f"https://x.com/one/status/{cid}"))
            conn.execute(
                "INSERT INTO content_latest(content_id,likes,replies,platform,"
                "external_id) VALUES (?,?,0,'x',?)", (cid, likes, f"x{cid}"))
            cid += 1
    conn.close()
    out = tmp_path / "o" / "index.html"
    data = build_boards.build(path, days=10, limit=15, out_path=str(out))
    x = data["boards"]["x"]
    titles = [c["title"] for c in x["cards"]]
    assert titles, "настоящие карточки (base) должны остаться"
    assert all(t.strip().lower() != "image" for t in titles), titles
    # found = после отбраковки заглушки и кэпа на автора (5 → 2), shown = found.
    assert x["found"] == build_boards.PER_KEY == x["shown"]
    html = out.read_text(encoding="utf-8")
    assert "<p class=\"card__desc\">Image</p>" not in html


def test_x_ru_title_from_sibling_by_text_hash(tmp_path):
    """Тот же текст в другой content-строке: русский берётся по text_hash."""
    path = str(tmp_path / "ru.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    cid = 1
    text = "We turned Qwen into a multimodal decision model"
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title)"
            " VALUES (1,'x','xa','a','a')")
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title)"
            " VALUES (2,'x','xb','b','b')")
        # Размеченная строка (автор a) — носитель русского перевода.
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "text,text_hash,author_handle,url,is_repost,lang)"
            " VALUES (?,?,?,?,?,?,?,?,?,0,'en')",
            (cid, "x", 1, "c1", _iso(1.0), text, "h1", "a",
             "https://x.com/a/status/1"))
        conn.execute(
            "INSERT INTO content_latest(content_id,likes,replies,platform,"
            "external_id) VALUES (?,?,0,'x',?)", (cid, 100, "c1"))
        conn.execute(
            "INSERT INTO classification(content_id,platform,external_id,"
            "title_ru,summary_ru) VALUES (?, 'x','c1','Русский заголовок',"
            "'Русская сводка про Qwen')", (cid,))
        cid += 1
        # «Соседняя» строка того же текста (автор b) без разметки — именно она
        # идёт в топ по лайкам; перевод должен подтянуться по text_hash.
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "text,text_hash,author_handle,url,is_repost,lang)"
            " VALUES (?,?,?,?,?,?,?,?,?,0,'en')",
            (cid, "x", 2, "c2", _iso(1.0), text, "h1", "b",
             "https://x.com/b/status/2"))
        conn.execute(
            "INSERT INTO content_latest(content_id,likes,replies,platform,"
            "external_id) VALUES (?,?,0,'x',?)", (cid, 999, "c2"))
        cid += 1
    conn.close()
    out = tmp_path / "o" / "index.html"
    data = build_boards.build(path, days=10, limit=5, out_path=str(out))
    titles = [c["title"] for c in data["boards"]["x"]["cards"]]
    assert any("Русск" in t for t in titles), titles
    assert not any(t == text for t in titles), titles


# --------------------------------------------------------------------------- #
# ТЗ-3: доска YouTube строится только из строк, прошедших полы отчёта
# --------------------------------------------------------------------------- #
def _youtube_pool_norm(conn, days=10):
    """Пул и норма YouTube теми же обёртками, что и отчёт (без своего SQL)."""
    cutoff, _ = report_mod._now_iso(days)
    pool = report_mod.viral_youtube_pool(conn, cutoff)
    fresh = [r for r in pool if report_mod._in_window(r["published_at"], cutoff)]
    norm = report_mod._norm_map(fresh, lambda r: r["source_id"],
                                lambda r: r["views"])
    return pool, norm, cutoff


def test_youtube_cards_match_passed_and_floors(tmp_path):
    """ТЗ-3 §1–§2: карточек-кандидатов ровно столько, сколько прошло полы.

    На текущем коде доска строится из СЫРОГО пула и в неё попадают
    yt_small (500 подписчиков) и yt_lowcov (охват 0,2): тест обязан падать.
    После правки карточки берутся из ``select_viral_youtube``.
    """
    db_path = _build_db(tmp_path)
    conn = db.connect(db_path, readonly=True)
    try:
        pool, norm, cutoff = _youtube_pool_norm(conn)
        _, st1 = report_mod.select_viral_youtube(pool, fun=False, norm=norm,
                                                 cutoff=cutoff)
        _, st2 = report_mod.select_viral_youtube(pool, fun=True, norm=norm,
                                                 cutoff=cutoff)
    finally:
        conn.close()
    out = tmp_path / "y" / "index.html"
    data = build_boards.build(db_path, days=10, limit=50, out_path=str(out))
    yt = data["boards"]["youtube"]
    passed = st1["passed"] + st2["passed"]
    assert passed > 0
    # Счётчик шапки и список карточек сходятся.
    assert yt["candidates"] == passed, (yt.get("candidates"), st1, st2)
    assert yt["passed"] == passed
    assert yt["cards"], "карточки YouTube не должны опустеть"
    # Полы отчёта действуют и на КАЖДУЮ карточку списка.
    for card in yt["cards"]:
        assert card["subs"] >= report_mod.VIRAL_YT_MIN_SUBS, card
        assert card["views"] >= report_mod.VIRAL_YT_MIN_VIEWS, card
        assert (card["views"] / card["subs"]
                >= report_mod.VIRAL_YT_MIN_COVERAGE), card


def test_youtube_lang_breakdown_sums_to_passed(tmp_path):
    """ТЗ-3 §4.3: разбивка по языку в шапке сходится со счётчиком прошедших."""
    db_path = _build_db(tmp_path)
    out = tmp_path / "y" / "index.html"
    data = build_boards.build(db_path, days=10, limit=50, out_path=str(out))
    yt = data["boards"]["youtube"]
    br = yt["lang_breakdown"]
    assert sum(br.values()) == yt["passed"]
    # В фикстуре языки каналов — 'en', поэтому все прошедшие в группе 'en'.
    assert br["en"] == yt["passed"]


def test_youtube_card_channel_lang_label(built):
    """ТЗ-3 §4.2: на карточке YouTube — метка языка канала («англ»)."""
    _, data, _ = built
    for card in data["boards"]["youtube"]["cards"]:
        assert card["lang_label"] == "англ", card


def test_youtube_first_mover_badge(built):
    """ТЗ-3 §4.4: канал с first_moves>0 и отрицательным лидом — «первоисточник»."""
    html, data, _ = built
    yt_cards = data["boards"]["youtube"]["cards"]
    marked = [c for c in yt_cards if c["note"] == "первоисточник"]
    assert marked, yt_cards
    assert all(c["channel"] == "yt_one" for c in marked)
    assert "первоисточник" in html
    # Нет строки first_mover — нет метки.
    assert all(c["channel"] == "yt_one" for c in marked)


def test_youtube_norm_is_main_number_plus_coverage(tmp_path):
    """ТЗ-3 §3: главное число — кратность к норме канала, охват справочно."""
    db_path = _build_db(tmp_path)
    out = tmp_path / "y" / "index.html"
    data = build_boards.build(db_path, days=10, limit=50, out_path=str(out))
    yt = data["boards"]["youtube"]
    for card in yt["cards"]:
        # Охват остаётся во второй строке справочно.
        assert "охват" in card["base"]
        # У yt_one 3 видео (норма осмысленна) — главное число есть; у yt_two
        # только 1 видео — данных мало, число не выдумывается (прочерк).
        if card["channel"] == "yt_two":
            assert card["main"] == "—"
            assert card["main_value"] is None
        if card["channel"] == "yt_one":
            assert card["main"] != "—"
            assert card["main_value"] is not None
            assert "медиана" in card["base"]


def test_backfill_source_lang_idempotent(tmp_path):
    """ТЗ-3 §4.1: бэкфилл языка идемпотентен и берёт самый частый язык видео."""
    path = str(tmp_path / "lang.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title,lang)"
            " VALUES (1,'youtube','UCa','a','a',NULL)")
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title,lang)"
            " VALUES (2,'youtube','UCb','b','b','')")
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title,lang)"
            " VALUES (3,'youtube','UCc','c','c','ru')")
        langs = {10: "en", 11: "en", 12: "ru"}  # a: en x2 → 'en'
        for cid, lang in langs.items():
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,lang,"
                "published_at) VALUES (?,?,?,?,?,?)",
                (cid, "youtube", 1, f"v{cid}", lang, _iso(1.0)))
        # b: единственное видео без языка → восстановить нельзя, не выдумываем.
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,lang,"
            "published_at) VALUES (20,?,?,?,NULL,?)",
            ("youtube", 2, "v20", _iso(1.0)))
    # dry-run ничего не пишет.
    assert db.backfill_source_lang(conn, dry_run=True) == 1
    assert conn.execute("SELECT lang FROM source WHERE id=1").fetchone()[0] is None
    # боевой прогон заполняет только восстановимое.
    assert db.backfill_source_lang(conn) == 1
    assert conn.execute("SELECT lang FROM source WHERE id=1").fetchone()[0] == "en"
    assert conn.execute("SELECT lang FROM source WHERE id=2").fetchone()[0] == ""
    assert conn.execute("SELECT lang FROM source WHERE id=3").fetchone()[0] == "ru"
    # повторный прогон ничего не меняет.
    assert db.backfill_source_lang(conn) == 0
    conn.close()


# --------------------------------------------------------------------------- #
# ТЗ-4: разделы «Темы окна», «Залетевшие сюжеты», «ИИ-инструменты» + графики
# --------------------------------------------------------------------------- #
def _build_tz4_db(tmp_path) -> str:
    """Мини-база под разделы ТЗ-4: темы, сюжеты и ИИ-инструменты.

    Тема A — 5 материалов в окне (3 обычных + 2 участника сюжета) и 1 в прошлом
    (ускорение 5.0); тема B — 5 и 5 (ускорение 1.0); ярлык-заглушка «(без темы)»
    не должна попасть в раздел.
    Сюжет 1 — русский заголовок канонического, лид 456.967 мин; сюжет 2 —
    только оригинал и NULL-лид. YouTube-инструмент проходит полы (всплеск к
    медиане канала), Telegram-инструмент — к базе канала.
    """
    path = str(tmp_path / "tz4.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    state = {"cid": 1000}

    def add_content(platform, days_ago, *, source_id=None, external_id=None,
                    title=None, text=None, url=None, lang="ru"):
        cid = state["cid"]
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "title,text,url,lang) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, platform, source_id, external_id or f"c{cid}",
             _iso(days_ago), title, text, url, lang))
        state["cid"] += 1
        return cid

    def classify(content_id, topic, subtopic=None, title_ru=None):
        conn.execute(
            "INSERT INTO classification(content_id,topic,subtopic,title_ru,"
            "platform,external_id) VALUES (?,?,?,?,'telegram',?)",
            (content_id, topic, subtopic, title_ru, f"c{content_id}"))

    with db.write_tx(conn):
        # Тема A: 3 в окне, 1 в прошлом окне.
        for i in range(3):
            c = add_content("telegram", 1.0 + i * 0.01,
                            url=f"https://t.me/a/{i}")
            classify(c, "тема один")
        classify(add_content("telegram", 12.0, url="https://t.me/a/prev"),
                 "тема один")
        # Тема B: 5 в окне, 5 в прошлом окне.
        for i in range(5):
            classify(add_content("telegram", 1.0 + i * 0.01,
                                 url=f"https://t.me/b/{i}"), "тема два")
        for i in range(5):
            classify(add_content("telegram", 12.0 + i * 0.01,
                                 url=f"https://t.me/b/p{i}"), "тема два")
        # Заглушка: в раздел тем не попадает.
        classify(add_content("telegram", 1.0), "(без темы)")

        # Сюжеты.
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title)"
            " VALUES (9000,'telegram','fmsrc','fm','FM')")
        c1 = add_content("telegram", 1.0, title="Оригинал",
                         url="https://t.me/s/1")
        classify(c1, "тема один", title_ru="Русский заголовок")
        c2 = add_content("telegram", 1.5, url="https://t.me/s/2")
        classify(c2, "тема один")
        conn.execute(
            "INSERT INTO story(id,platform,title,canonical_content_id,"
            "first_content_id,first_mover_source_id,first_pub_at,source_count,"
            "content_count,lead_time_min)"
            " VALUES (1,'cross','st1',?,?,9000,?,5,5,456.967)",
            (c1, c1, _iso(1.0)))
        for cid in (c1, c2):
            conn.execute("INSERT INTO story_member(story_id,content_id)"
                         " VALUES (1,?)", (cid,))
        c3 = add_content("telegram", 2.0, title="Original only",
                         url="https://t.me/s/3")
        classify(c3, "тема два")
        conn.execute(
            "INSERT INTO story(id,platform,title,canonical_content_id,"
            "first_content_id,first_pub_at,source_count,content_count,"
            "lead_time_min) VALUES (2,'cross','st2',?,?,?,2,2,NULL)",
            (c3, c3, _iso(2.0)))
        conn.execute("INSERT INTO story_member(story_id,content_id)"
                     " VALUES (2,?)", (c3,))

        # ИИ-инструменты: YouTube (норма канала) и Telegram (база канала).
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title,subs,lang)"
            " VALUES (8000,'youtube','UCx','yt_ai','YT AI',1000,'ru')")
        for i, views in enumerate((3000, 3000, 3000, 30000)):
            c = add_content("youtube", 1.0, source_id=8000,
                            external_id=f"v{i}", title=f"видео {i}",
                            url=f"https://youtu.be/{i}")
            classify(c, build_boards.AI_TOOLS_TOPIC,
                     subtopic=("ИИ-трейдинг" if i == 3 else None))
            conn.execute(
                "INSERT INTO metric_snapshot(content_id,captured_at,"
                "interval_quality,views,views_per_day,platform,external_id)"
                " VALUES (?,?,'ok',?,1000,'youtube',?)",
                (c, _iso(0.5), views, f"v{i}"))
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title,lang)"
            " VALUES (8100,'telegram','tgtool','tg_ai','TG AI','ru')")
        conn.execute(
            "INSERT INTO source_baseline(source_id,window_days,median_views,"
            "computed_at) VALUES (8100,90,1000,?)", (_iso(0.5),))
        c = add_content("telegram", 1.0, source_id=8100, title="tg tool",
                        url="https://t.me/tg/1")
        classify(c, build_boards.AI_TOOLS_TOPIC, subtopic="генерация видео",
                 title_ru="ТГ инструмент")
        conn.execute(
            "INSERT INTO content_latest(content_id,views,platform,external_id)"
            " VALUES (?,5000,'telegram',?)", (c, f"c{c}"))
    conn.close()
    return path


@pytest.fixture
def tz4(tmp_path):
    """Собрать страницу на мини-базе ТЗ-4 и вернуть (html, data)."""
    db_path = _build_tz4_db(tmp_path)
    out = tmp_path / "boards" / "index.html"
    data = build_boards.build(db_path, days=10, limit=4, out_path=str(out))
    return out.read_text(encoding="utf-8"), data


def test_window_starts_are_calendar_midnights():
    now = datetime(2026, 10, 8, 17, 30, tzinfo=timezone.utc)
    cur, prev = build_boards._window_starts(10, now)
    assert cur == "2026-09-28 00:00:00"
    assert prev == "2026-09-18 00:00:00"


def test_topics_section_sorted_by_accel_and_excludes_blank(tz4):
    _, data = tz4
    topics = data["topics"]
    assert topics["window_start"] == build_boards._window_starts(10)[0]
    names = [r["topic"] for r in topics["items"]]
    assert "(без темы)" not in names
    # Тема A: 5 материалов в окне (3 обычных + 2 участника сюжета) и 1 в
    # прошлом → ускорение 5.0; тема B (5 и 5) — 1.0; у инструментов прошлого
    # окна нет — честный прочерк и место в конце.
    assert names[0] == "тема один"
    assert topics["items"][0]["accel"] == pytest.approx(5.0)
    assert topics["items"][0]["materials"] == 5
    assert topics["items"][0]["stories"] == 1
    ai_row = next(r for r in topics["items"]
                  if r["topic"] == build_boards.AI_TOOLS_TOPIC)
    assert ai_row["accel"] is None
    assert ai_row["previous"] is None


def test_stories_section_title_lead_and_topic(tz4):
    _, data = tz4
    items = data["stories"]["items"]
    assert items[0]["story_id"] == 1
    assert items[0]["title"] == "Русский заголовок"
    assert items[0]["topic"] == "тема один"
    assert items[0]["source_count"] == 5
    assert items[0]["lead_hours"] == pytest.approx(456.967 / 60.0)
    assert items[0]["first_mover"] == "fm"
    assert items[0]["url"] == "https://t.me/s/1"
    # Оригинал как есть (русского нет) и NULL-лид — прочерк, а не ноль.
    second = items[1]
    assert second["title"] == "Original only"
    assert second["lead_hours"] is None
    assert build_boards._fmt_lead(second["lead_hours"]) == "—"


def test_ai_tools_section_floors_and_subtopics(tz4):
    _, data = tz4
    ai = data["ai_tools"]
    assert ai["materials"] == 5  # 4 YouTube + 1 Telegram в окне
    assert ai["stats"]["youtube"]["passed"] == 1
    assert ai["stats"]["telegram"]["passed"] == 1
    assert ai["cards"], "карточки инструментов не должны опустеть"
    # Главное число — кратность: 30000/3000 = 10 (YouTube), 5000/1000 = 5 (TG).
    ratios = sorted(round(c["main_value"], 3) for c in ai["cards"])
    assert ratios == [5.0, 10.0]
    names = {s["name"] for s in ai["subtopics_7d"]}
    assert "ИИ-трейдинг" in names and "генерация видео" in names


def test_html_has_new_panels_and_chart_images(tz4):
    html, _ = tz4
    collector = _Collector()
    collector.feed(html)
    assert collector.boards == ["x", "youtube", "telegram"]
    # Новые разделы — панели (не доски), порядок фиксирован.
    assert collector.panels == ["topics", "ai-tools", "stories", "platforms"]
    srcs = {a.get("src") for a in collector.images}
    assert {"topics_daily.png", "topics_bar.png",
            "platform_daily.png"} <= srcs
    # Все графики — локальные <img> с русским alt, без base64 и внешних URL.
    for attrs in collector.images:
        assert not attrs["src"].startswith(("http", "//", "data:"))
        assert attrs.get("alt", "").strip()
    assert collector.external == []


def test_story_topic_majority_uses_members_not_story_topic(tmp_path):
    """Поле story.topic пустое — тема берётся большинством участников."""
    db_path = _build_tz4_db(tmp_path)
    conn = db.connect(db_path, readonly=True)
    try:
        majority = build_boards._story_topic_majority(conn)
    finally:
        conn.close()
    assert majority[1] == "тема один"
    assert majority[2] == "тема два"


# --------------------------------------------------------------------------- #
# ТЗ-9: секция «Растущие каналы» (источник — slivki._rising)
# --------------------------------------------------------------------------- #
def _rec(sid, handle, g7, *, subs=1000, platform="telegram", **kw):
    """Мок записи ``slivki.build()['rising']`` (или кандидата) для тестов."""
    rec = {"source_id": sid, "platform": platform, "handle": handle,
           "title": handle.title(), "url": f"https://t.me/{handle}",
           "subs": subs, "size_class": "<5k", "g7": g7,
           "breakout": 10.0, "viral_post": {"outlier": 0.0, "url": None}}
    rec.update(kw)
    return rec


def test_rising_cards_fields_sort_and_limit():
    """Карточка: поля, сортировка по росту, ограничение топ-лимитом."""
    recs = [_rec(1, "a", 0.05), _rec(2, "b", 0.30), _rec(3, "c", 0.10)]
    out = build_boards.rising_cards(recs, topics={2: "Тема Б"}, limit=2)
    assert out["total"] == 3 and out["found"] == 3 and out["shown"] == 2
    assert [c["handle"] for c in out["items"]] == ["b", "c"]
    card = out["items"][0]
    assert card["channel"] == "B"
    assert card["g7"] == pytest.approx(0.30)
    assert card["growth_text"] == "+30,00%"
    assert card["growth_unit"] == build_boards.RISING_GROWTH_UNIT
    assert card["topic"] == "Тема Б"
    assert card["subs"] == 1000 and card["size_class"] == "<5k"
    assert out["items"][1]["topic"] is None


def test_rising_cards_unknown_never_invents_number():
    """``g7 is None`` / ``growth_status='unknown'`` — числа нет, счётчик растёт."""
    recs = [
        _rec(1, "a", None),
        _rec(2, "b", 0.20),
        {"source_id": 3, "platform": "x", "handle": "c", "subs": 10,
         "g7": 0.30, "growth_status": "unknown", "viral_post": {}},
    ]
    out = build_boards.rising_cards(recs)
    assert out["unknown"] == 2
    assert out["found"] == 1
    assert [c["handle"] for c in out["items"]] == ["b"]
    # У оставшейся карточки число — измеренный рост, ни у кого нет выдумки.
    assert all(c["g7"] is not None and c["growth_text"] for c in out["items"])


def test_rising_cards_tz8_filters():
    """ТЗ-8: кандидат, неактивный статус и промо-источник в карточки не идут."""
    recs = [_rec(1, "a", 0.1), _rec(2, "b", 0.2), _rec(3, "c", 0.3)]
    out = build_boards.rising_cards(
        recs, statuses={1: "private", 2: "candidate"}, promo_ids={3})
    assert out["skip_status"] == 1
    assert out["skip_candidate"] == 1
    assert out["skip_promo_source"] == 1
    assert out["items"] == [] and out["total"] == 3


def test_rising_section_absent_when_empty():
    """Пустой rising → секции нет (как остальные блоки при пустоте)."""
    empty = {"items": [], "found": 0, "shown": 0, "unknown": 0, "total": 0}
    assert build_boards._section_rising(empty, datetime.now(timezone.utc)) == ""
    assert build_boards.rising_cards([])["items"] == []


def test_rising_section_rendered_from_selector(tmp_path, monkeypatch):
    """Связка: rising из отборщика → секция на странице, число — рост 7д.

    Рост не пересчитывается: подменяем ``slivki.build`` моком записей (то же
    место вызова, что у боевой метки). Проверяем и HTML, и блок boards.json.
    """
    db_path = _build_db(tmp_path)
    mock = {
        "rising": [
            {"source_id": 500, "platform": "x", "handle": "alpha",
             "title": "Alpha", "url": "https://x.com/alpha", "subs": 5000,
             "size_class": "5–25k", "g7": 0.25, "breakout": 20.0,
             "viral_post": {"outlier": 4.5,
                            "url": "https://x.com/alpha/status/1"}},
            {"source_id": 501, "platform": "x", "handle": "beta",
             "title": "Beta", "url": "https://x.com/beta", "subs": 2000,
             "size_class": "<5k", "g7": 0.10, "breakout": 5.0,
             "viral_post": {"outlier": 0.0, "url": None}},
            {"source_id": 599, "platform": "x", "handle": "delta",
             "title": "Delta", "subs": 5000, "size_class": "5–25k",
             "g7": None, "breakout": 9.0, "viral_post": {}},
        ],
        "day": "2026-10-08", "rising_days_available": 3,
        "rising_days_required": 2,
    }
    monkeypatch.setattr(build_boards.slivki_mod, "build",
                        lambda conn, *, now=None, limit=20: mock)
    out = tmp_path / "r" / "index.html"
    data = build_boards.build(db_path, days=10, limit=4, out_path=str(out))
    rc = data["rising_channels"]
    assert rc["total"] == 3 and rc["found"] == 2 and rc["unknown"] == 1
    assert [c["handle"] for c in rc["items"]] == ["alpha", "beta"]
    html = out.read_text(encoding="utf-8")
    assert 'id="rising"' in html
    assert "Растущие каналы" in html
    assert "+25,00%" in html          # главное число — рост 7д в процентах
    assert "рост подписчиков за 7 дней" in html
    assert "рост 7д не измерим" in html
    # Число для Delta (g7=None) не выдумывается: его карточки в секции нет.
    assert "Delta" not in html
    assert html.count('class="rcard"') == 2
    saved = json.loads((out.parent / "boards.json").read_text(encoding="utf-8"))
    assert saved["rising_channels"]["found"] == 2
    assert saved["rising_channels"]["shown"] == 2


@pytest.mark.skipif(
    os.environ.get("TUBER_TZ9_SNAPSHOT") != "1",
    reason="тяжёлый снимок на боевой базе (минуты): включить TUBER_TZ9_SNAPSHOT=1")
@pytest.mark.skipif(not (ROOT / "data" / "tuber.db").exists(),
                    reason="нет боевой базы — снимать нечего")
def test_snapshot_real_db_readonly(tmp_path, monkeypatch):
    """Снимок на боевой базе: открытие только на чтение, страница разумного размера."""
    db_path = str(ROOT / "data" / "tuber.db")
    seen: dict = {}
    real_connect = build_boards.core_db.connect

    def spy(path, *args, **kwargs):
        seen["readonly"] = kwargs.get("readonly")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(build_boards.core_db, "connect", spy)
    out = tmp_path / "real" / "index.html"
    data = build_boards.build(db_path, days=10, limit=15, out_path=str(out))
    assert out.exists()
    # Боевая база открывается ТОЛЬКО на чтение (mtree не проверяем: рядом пишет
    # планировщик, и файл может обновиться во время снимка).
    assert seen.get("readonly") is True
    size = out.stat().st_size
    assert 50_000 < size < 5_000_000, size
    rc = data["rising_channels"]
    assert {"items", "found", "shown", "unknown", "total"} <= set(rc)
    assert rc["shown"] <= 15 and rc["shown"] == len(rc["items"])
    for card in rc["items"]:
        assert card["g7"] is not None and card["growth_text"].endswith("%")


