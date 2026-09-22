"""Тесты секции «Виральные — охват против своей аудитории» (ТЗ-35, ТЗ-36).

Синтетическое ядро: каналы YouTube/X/Telegram с подписчиками, просмотрами,
лайками, темами, датами публикации и базой нормы. Проверяются метрика охвата
(просмотры ÷ подписчики), полы по границам, разделение полезное/фермы по
``PRODUCT_TOPICS`` (тема вне списка, «прочее», NULL → блок 2), свежесть внутри
окна во всех блоках, осмысленность «нормы канала» (≥ 3 материала), возраст
материала в полном отчёте и сводке, нижняя граница медианы автора X, оба
варианта ярлыков продуктов, три подсписка блока 3 с разными полами, полы и
лимиты на канал/автора, сходимость статистики, дедупликация и порядок сводки по
``content_id`` и лимит байт. Сеть и LLM не используются.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tuber.analysis import report
from tuber.core import db, schema

LIMIT = report.COMPACT_LIMIT


def _iso(days_ago: float = 1.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _yt(src, title, subs, topic, views, vpd=1000, lang="en", age=1.0):
    return {"src": src, "handle": f"yt{src}", "title": title, "subs": subs,
            "topic": topic, "views": views, "vpd": vpd, "lang": lang, "age": age}


def _x(author, likes, replies=0, topic=None, age=1.0, text=None, lang="en"):
    return {"author": author, "likes": likes, "replies": replies, "topic": topic,
            "age": age, "text": text or f"X {author} {likes}", "lang": lang}


def _tg(src, handle, views, baseline=None, topic=None, age=1.0, lang="ru"):
    return {"src": src, "handle": handle, "title": handle, "views": views,
            "baseline": baseline, "topic": topic, "age": age, "lang": lang}


def build_core(tmp_path, *, yt=(), x=(), tg=()):
    """Собрать синтетическое ядро по спецификациям видео/постов."""
    conn = db.connect(str(tmp_path / "viral.db"))
    schema.init_schema(conn)
    cid = 1_000
    with db.write_tx(conn):
        yt_src, x_src, tg_src = {}, {}, {}
        for v in yt:
            yt_src.setdefault(v["src"], v)
        for p in x:
            x_src.setdefault(p["author"], p)
        for p in tg:
            tg_src.setdefault(p["src"], p)

        for src, v in yt_src.items():
            conn.execute(
                "INSERT INTO source(id,platform,external_id,handle,title,subs,lang)"
                " VALUES (?,?,?,?,?,?,?)",
                (src, "youtube", f"UC{src}", v["handle"], v["title"], v["subs"],
                 v.get("lang")))
        x_ids = {}
        for i, (author, p) in enumerate(x_src.items()):
            sid = 500 + i
            x_ids[author] = sid
            conn.execute(
                "INSERT INTO source(id,platform,external_id,handle,title)"
                " VALUES (?,?,?,?,?)", (sid, "x", f"xd{sid}", author, author))
        for src, p in tg_src.items():
            # status='active': Telegram-раздел сводки/полного отчёта фильтрует
            # профиль по статусу реестра (ТЗ-A), otherwise telegram_items пуст.
            conn.execute(
                "INSERT INTO source(id,platform,external_id,handle,title,lang,status)"
                " VALUES (?,?,?,?,?,?,'active')",
                (src, "telegram", f"tg{src}", p["handle"], p["title"],
                 p.get("lang")))
            if p.get("baseline") is not None:
                conn.execute(
                    "INSERT INTO source_baseline(source_id,window_days,"
                    "median_views,computed_at) VALUES (?,?,?,?)",
                    (src, 90, p["baseline"], _iso(0.5)))

        for i, v in enumerate(yt):
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,lang,title,url,kind) VALUES (?,?,?,?,?,?,?,?,'video')",
                (cid, "youtube", v["src"], f"v{v['src']}_{i}", _iso(v["age"]),
                 v["lang"], v["title"], f"https://www.youtube.com/watch?v={cid}"))
            conn.execute(
                "INSERT INTO metric_snapshot(content_id,captured_at,"
                "interval_quality,views,views_per_day,platform,external_id)"
                " VALUES (?,?,'ok',?,?,?,?)",
                (cid, _iso(0.5), v["views"], v["vpd"], "youtube", f"v{v['src']}_{i}"))
            if v["topic"] is not None:
                conn.execute(
                    "INSERT INTO classification(content_id,platform,topic,lang)"
                    " VALUES (?,?,?,?)", (cid, "youtube", v["topic"], v["lang"]))
            cid += 1

        for i, p in enumerate(x):
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,author_handle,url,is_repost,lang)"
                " VALUES (?,?,?,?,?,?,?,?,0,?)",
                (cid, "x", x_ids[p["author"]], f"x{i}", _iso(p["age"]),
                 p["text"], p["author"], f"https://x.com/{p['author']}/status/{cid}",
                 p["lang"]))
            conn.execute(
                "INSERT INTO content_latest(content_id,likes,replies,platform,"
                "external_id) VALUES (?,?,?,?,?)",
                (cid, p["likes"], p["replies"], "x", f"x{i}"))
            if p["topic"] is not None:
                conn.execute(
                    "INSERT INTO classification(content_id,platform,topic,lang)"
                    " VALUES (?,?,?,?)", (cid, "x", p["topic"], p["lang"]))
            cid += 1

        for i, p in enumerate(tg):
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,url,lang) VALUES (?,?,?,?,?,?,?,?)",
                (cid, "telegram", p["src"], f"{p['handle']}/{i}", _iso(p["age"]),
                 p["title"], f"https://t.me/{p['handle']}/{i}", p["lang"]))
            conn.execute(
                "INSERT INTO content_latest(content_id,views,platform,external_id)"
                " VALUES (?,?,?,?)", (cid, p["views"], "telegram",
                                      f"{p['handle']}/{i}"))
            if p["topic"] is not None:
                conn.execute(
                    "INSERT INTO classification(content_id,platform,topic,lang)"
                    " VALUES (?,?,?,?)", (cid, "telegram", p["topic"], p["lang"]))
            cid += 1
    return conn


@pytest.fixture
def core(tmp_path):
    conn = build_core(tmp_path)
    yield conn
    conn.close()


def _body(conn, days: int = 10):
    lines, stats = report.viral_section(conn, _iso(days), days)
    return "\n".join(lines), stats


def _region(text: str, start: str, end: str | None) -> str:
    i = text.index(start)
    if end is None:
        return text[i:]
    j = text.index(end, i)
    return text[i:j]


def _line_with(text: str, needle: str) -> str:
    """Первая строка, содержащая подстроку (для проверки одной позиции)."""
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"нет строки с {needle!r}")


# --------------------------------------------------------------------------- #
# 1. Метрика: охват = просмотры ÷ подписчики, а не кратность к медиане
# --------------------------------------------------------------------------- #
def test_coverage_is_views_over_subs_not_channel_ratio(tmp_path):
    yt = [_yt(1, f"A базовое {i}", 100_000, "наука и медицина", 1_000, vpd=100)
          for i in range(6)]
    yt.append(_yt(1, "A блокбастер", 100_000, "наука и медицина", 100_000,
                  vpd=100_000))
    yt.append(_yt(2, "B виральный", 1_000, "наука и медицина", 10_000, vpd=500))
    conn = build_core(tmp_path, yt=yt)
    body, stats = _body(conn)
    block1 = _region(body, "   Блок 1. YouTube — полезное", "   Блок 2.")
    # охват B = 10000/1000 = 10.0 попадает, охват A = 100000/100000 = 1.0 — нет,
    # хотя по кратности к медиане A был бы ×1000.
    assert "B виральный" in block1
    assert "охват на подписчика 10.00" in block1
    assert "A блокбастер" not in block1
    assert stats["block1"]["passed"] == 1


# --------------------------------------------------------------------------- #
# 2. Подписчики NULL/0 → материал отброшен
# --------------------------------------------------------------------------- #
def test_null_or_zero_subs_dropped(tmp_path):
    yt = [
        _yt(1, "нет подписчиков", None, "наука и медицина", 500_000, vpd=10_000),
        _yt(2, "ноль подписчиков", 0, "наука и медицина", 500_000, vpd=10_000),
    ]
    conn = build_core(tmp_path, yt=yt)
    body, stats = _body(conn)
    assert "нет подписчиков" not in body
    assert "ноль подписчиков" not in body
    assert stats["block1"]["passed"] == 0
    assert stats["block1"]["shown"] == 0


# --------------------------------------------------------------------------- #
# 3. Полы по границам: ровно на пороге — попадает
# --------------------------------------------------------------------------- #
def test_boundary_floors_inclusive(tmp_path):
    yt = [_yt(1, f"edge base {i}", 2_000, "наука и медицина", 1_000)
          for i in range(6)]
    yt.append(_yt(1, "ровно охват 3.0", 2_000, "наука и медицина", 6_000, vpd=500))
    yt.append(_yt(1, "чуть ниже охвата", 2_000, "наука и медицина", 5_999, vpd=500))
    yt.append(_yt(2, "ровно 1000 подписчиков", 1_000, "наука и медицина", 5_000,
                  vpd=500))
    yt.append(_yt(3, "999 подписчиков", 999, "наука и медицина", 500_000, vpd=500))
    conn = build_core(tmp_path, yt=yt)
    body, _ = _body(conn)
    block1 = _region(body, "   Блок 1. YouTube — полезное", "   Блок 2.")
    assert "ровно охват 3.0" in block1        # subs 2000, views 6000 → 3.00 ровно
    assert "чуть ниже охвата" not in block1  # 5999/2000 = 2.9995 < 3.0
    assert "ровно 1000 подписчиков" in block1  # subs ровно 1000, views ровно 5000
    assert "999 подписчиков" not in block1    # суб-пол не пройден


# --------------------------------------------------------------------------- #
# 4. Разделение полезное/фермы по PRODUCT_TOPICS (ТЗ-36)
# --------------------------------------------------------------------------- #
def test_useful_vs_farm_split_by_product_topics(tmp_path):
    """Полезное — только PRODUCT_TOPICS; фермы, «прочее» и NULL уходят в блок 2."""
    yt = [
        _yt(1, "полезный материал", 1_000, "агенты и автоматизация", 20_000,
            vpd=500),
        _yt(2, "ии-инструменты", 1_000, "ИИ-инструменты для обычных людей",
            20_000, vpd=500),
        _yt(3, "прочее видео", 1_000, "прочее", 20_000, vpd=500),
        _yt(4, "без темы видео", 1_000, None, 20_000, vpd=500),
    ]
    conn = build_core(tmp_path, yt=yt)
    body, stats = _body(conn)
    block1 = _region(body, "   Блок 1. YouTube — полезное", "   Блок 2.")
    block2 = _region(body, "   Блок 2. YouTube — развлекательное", "   Блок 3.")
    assert "полезный материал" in block1
    assert "полезный материал" not in block2  # полезное в блок 2 не дублируется
    for title in ("ии-инструменты", "прочее видео", "без темы видео"):
        assert title not in block1
        assert title in block2
    assert stats["block1"]["passed"] == 1
    assert stats["block2"]["passed"] == 3


# --------------------------------------------------------------------------- #
# 5. Старая и новая формулировки ярлыков тем запусков
# --------------------------------------------------------------------------- #
def test_product_topics_both_label_variants(tmp_path):
    yt = [
        _yt(1, "новая формулировка", 1_000, "модели и релизы", 60_000, vpd=500),
        _yt(2, "старая формулировка", 1_000, "релизы моделей", 60_000, vpd=500),
        _yt(3, "инструменты", 1_000, "инструменты разработчика", 60_000, vpd=500),
    ]
    conn = build_core(tmp_path, yt=yt)
    body, stats = _body(conn)
    sub = _region(body, "      3.1. YouTube", "      3.2. X")
    assert "новая формулировка" in sub
    assert "старая формулировка" in sub
    assert "инструменты" in sub
    assert stats["block3"]["youtube"]["passed"] == 3


# --------------------------------------------------------------------------- #
# 6. Не больше 2 позиций на канал/автора в каждом блоке
# --------------------------------------------------------------------------- #
def test_max_two_per_channel_each_block(tmp_path):
    yt = [_yt(1, f"та же виральная {i}", 1_000, "наука и медицина", 50_000)
          for i in range(3)]
    yt += [_yt(2, f"продукт {i}", 1_000, "модели и релизы", 60_000) for i in range(3)]
    x = [_x("sameauth", 600, topic="модели и релизы") for _ in range(3)]
    tg = [_tg(10, "samechan", 20_000, baseline=1_000) for _ in range(3)]
    conn = build_core(tmp_path, yt=yt, x=x, tg=tg)
    body, _ = _body(conn)
    block1 = _region(body, "   Блок 1. YouTube — полезное", "   Блок 2.")
    assert block1.count("канал yt1") <= report.VIRAL_YT_PER_CHANNEL
    sub31 = _region(body, "      3.1. YouTube", "      3.2. X")
    assert sub31.count("канал yt2") <= report.VIRAL_PRODUCT_PER_CHANNEL
    sub32 = _region(body, "      3.2. X", "      3.3. Telegram")
    assert sub32.count("@sameauth") <= report.VIRAL_PRODUCT_PER_CHANNEL
    block5 = _region(body, "   Блок 5. Telegram", None)
    assert block5.count("[samechan]") <= report.VIRAL_TG_PER_CHANNEL


# --------------------------------------------------------------------------- #
# 7. Сортировка по охвату убыв., а не по абсолютным просмотрам
# --------------------------------------------------------------------------- #
def test_sort_by_coverage_not_views(tmp_path):
    yt = [
        _yt(1, "мало просмотров высокий охват", 1_000, "наука и медицина", 50_000),
        _yt(2, "много просмотров низкий охват", 10_000, "наука и медицина",
            100_000),
    ]
    conn = build_core(tmp_path, yt=yt)
    body, _ = _body(conn)
    block1 = _region(body, "   Блок 1. YouTube — полезное", "   Блок 2.")
    assert block1.index("высокий охват") < block1.index("низкий охват")


# --------------------------------------------------------------------------- #
# 8. Блок 3 — три подсписка с разными полами
# --------------------------------------------------------------------------- #
def test_product_three_sublists_three_floors(tmp_path):
    yt = [
        _yt(1, "yt проходит", 1_000, "запуски и анонсы", 60_000),
        _yt(2, "yt не проходит", 1_000, "запуски и анонсы", 40_000),
    ]
    x = [
        _x("xpass", 600, topic="стартапы и бизнес"),
        _x("xfail", 400, topic="стартапы и бизнес"),
    ]
    tg = [
        _tg(10, "tgpass", 20_000, baseline=1_000, topic="инвестиции и раунды"),
        _tg(11, "tgfail", 5_000, baseline=1_000, topic="инвестиции и раунды"),
    ]
    conn = build_core(tmp_path, yt=yt, x=x, tg=tg)
    body, stats = _body(conn)
    sub1 = _region(body, "      3.1. YouTube", "      3.2. X")
    sub2 = _region(body, "      3.2. X", "      3.3. Telegram")
    sub3 = _region(body, "      3.3. Telegram", "   Блок 4.")
    assert "yt проходит" in sub1 and "yt не проходит" not in sub1
    assert "xpass" in sub2 and "xfail" not in sub2
    assert "tgpass" in sub3 and "tgfail" not in sub3
    assert stats["block3"]["youtube"]["passed"] == 1
    assert stats["block3"]["x"]["passed"] == 1
    assert stats["block3"]["telegram"]["passed"] == 1


# --------------------------------------------------------------------------- #
# 9. X: кратность к медиане автора; < 5 постов → отброшен
# --------------------------------------------------------------------------- #
def test_x_median_and_min_posts(tmp_path):
    x = [
        _x("median_author", 5_000),
        _x("median_author", 5_000),
        _x("median_author", 5_000),
        _x("median_author", 5_000),
        _x("median_author", 5_000),
        _x("median_author", 100, text="X median_author low"),  # ниже пола 200
        _x("median_author", 250, text="X median_author 2.5x"),  # 250/5000 → 0.05
        _x("median_author", 50_000, text="X median_author spike"),
        _x("few_posts", 50_000, text="X few_posts spike"),
        _x("few_posts", 1, text="X few_posts p1"),
        _x("few_posts", 1, text="X few_posts p2"),
        _x("few_posts", 1, text="X few_posts p3"),
    ]
    conn = build_core(tmp_path, x=x)
    body, stats = _body(conn)
    block4 = _region(body, "   Блок 4. X — истории с реакциями", "   Блок 5.")
    assert "X median_author spike" in block4
    assert "медиана автора 5 000" in block4
    assert "кратность 10.0" in block4
    assert "X median_author low" not in block4
    assert "X few_posts spike" not in block4   # у автора только 4 поста в окне
    assert stats["block4"]["shown"] == 1


def test_x_author_norm_window_is_30_days(tmp_path):
    """Норма автора считается за 30 дней, сам материал — из окна отчёта."""
    x = [_x("old_author", 100, age=25, text=f"X old_author {i}") for i in range(4)]
    x.append(_x("old_author", 1_000, age=1, text="X old_author fresh spike"))
    conn = build_core(tmp_path, x=x)
    body, stats = _body(conn)
    block4 = _region(body, "   Блок 4. X — истории с реакциями", "   Блок 5.")
    assert "X old_author fresh spike" in block4
    assert stats["block4"]["shown"] == 1


# --------------------------------------------------------------------------- #
# 10. Telegram: без базы в source_baseline материал отброшен
# --------------------------------------------------------------------------- #
def test_telegram_without_baseline_dropped(tmp_path):
    tg = [
        _tg(10, "withoutbase", 500_000),           # базы нет — мимо
        _tg(11, "withbase", 5_000, baseline=1_000),  # ×5 — проходит
        _tg(12, "belowfloor", 900, baseline=100),  # < 1000 — мимо
    ]
    conn = build_core(tmp_path, tg=tg)
    body, stats = _body(conn)
    block5 = _region(body, "   Блок 5. Telegram", None)
    assert "withoutbase" not in block5
    assert "withbase" in block5
    assert "ниже базовой нормы" not in block5
    assert stats["block5"]["shown"] == 1


# --------------------------------------------------------------------------- #
# 11. Сводка: нет дублей между разделами 1–3 и «Виральными» по content_id
# --------------------------------------------------------------------------- #
def test_compact_dedup_by_content_id(tmp_path):
    # A проходит и в раздел 1 (просмотры ≥ 50 000), и в блок 1 (охват ≥ 3);
    # B — виральное (охват 20), но ниже порога раздела 1, поэтому не дубль.
    yt = [
        _yt(1, "дедуп видео", 1_000, "наука и медицина", 100_000, vpd=100_000),
        _yt(2, "не дубль виральное", 1_000, "наука и медицина", 20_000, vpd=500),
    ]
    conn = build_core(tmp_path, yt=yt)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    link = "https://www.youtube.com/watch?v=1000"
    assert text.count(link) == 1
    # В разделе 1 видео есть, в «Виральных» его уже нет.
    yt_block = text.split("2. X")[0]
    assert "дедуп видео" in yt_block
    viral_block = _region(text, "3. Виральные", "4. Новое за сутки")
    assert "дедуп видео" not in viral_block
    assert "не дубль виральное" in viral_block


# --------------------------------------------------------------------------- #
# 12. Размер сводки ≤ лимита, раздел «Виральные» присутствует
# --------------------------------------------------------------------------- #
def test_compact_size_and_viral_section(tmp_path):
    yt = [_yt(i, f"виральное {i}", 1_000, "наука и медицина", 5_000 * (i + 1))
          for i in range(1, 8)]
    conn = build_core(tmp_path, yt=yt)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert "3. Виральные — охват против своей аудитории:" in text
    assert len(text.encode("utf-8")) <= LIMIT
    assert LIMIT == 12000
    # ссылки не обрезаны по байтам mid-line: каждая целая в полном отчёте
    full = report.build(conn, days=10, db_path=":memory:")
    for link in re.findall(r"https?://\S+", text):
        assert link in full


# --------------------------------------------------------------------------- #
# 13. Статистика блока (рассмотрено/прошло/показано) считается
# --------------------------------------------------------------------------- #
def test_stats_considered_passed_shown(tmp_path):
    yt = [
        _yt(1, "виральное", 1_000, "наука и медицина", 50_000, vpd=500),
        _yt(1, "низкое", 1_000, "наука и медицина", 1_000, vpd=100),
        _yt(2, "развлекательное", 1_000, "медиа и творчество", 50_000, vpd=500),
    ]
    conn = build_core(tmp_path, yt=yt)
    body, stats = _body(conn)
    for block in ("block1", "block2", "block4", "block5"):
        st = stats[block]
        assert st["considered"] >= st["passed"] >= st["shown"]
    assert stats["block1"]["shown"] == 1
    assert "рассмотрено" in body and "прошло полы" in body and "показано" in body
    total_yt = conn.execute(
        "SELECT COUNT(*) FROM content WHERE platform='youtube'"
        " AND deleted_at IS NULL").fetchone()[0]
    assert stats["block1"]["considered"] == total_yt


# --------------------------------------------------------------------------- #
# 14. Полный отчёт: секция 5 переименована, блоки на месте
# --------------------------------------------------------------------------- #
def test_full_report_has_viral_section(tmp_path):
    conn = build_core(tmp_path)
    text = report.build(conn, days=10, db_path=":memory:")
    assert "5. Виральные — охват против своей аудитории." in text
    assert "   Блок 1. YouTube — полезное" in text
    assert "   Блок 2. YouTube — развлекательное" in text
    assert "   Блок 3. Новые продукты" in text
    assert "   Блок 4. X — истории с реакциями" in text
    assert "   Блок 5. Telegram — выше своей нормы" in text
    # Честная оговорка про отсутствие подписчиков X в правилах блока 4.
    assert "подписчики X в базе НЕ собираются" in text
    # Подвал считает покрытие ЗАПРОСОМ, а не хардкодом.
    assert "Виральные: подписчики YouTube есть у" in text
    assert "Виральные: база нормы Telegram" in text


# =========================================================================== #
# ТЗ-36: свежесть, PRODUCT_TOPICS, норма канала, возраст, X-медиана, сводка
# =========================================================================== #
# --------------------------------------------------------------------------- #
# 15. Свежесть: материал старше окна выпадает из всех пяти блоков
# --------------------------------------------------------------------------- #
def test_material_older_than_window_dropped_from_all_blocks(tmp_path):
    yt = [
        _yt(1, "полезное старое", 1_000, "наука и медицина", 500_000, age=15),
        _yt(2, "ферма старая", 1_000, "прочее", 500_000, age=15),
        _yt(3, "продукт старый", 1_000, "модели и релизы", 900_000, age=15),
    ]
    x = [_x("stale", 20, age=1, text=f"X fresh base {i}") for i in range(4)]
    x.append(_x("stale", 5_000, age=15, text="X stale spike"))
    x.append(_x("stale_prod", 900, age=15, topic="стартапы и бизнес",
                text="X stale product"))
    tg = [
        _tg(10, "stalechan", 20_000, baseline=1_000, age=15),
        _tg(11, "staleprod", 30_000, baseline=1_000, topic="инвестиции и раунды",
            age=15),
    ]
    conn = build_core(tmp_path, yt=yt, x=x, tg=tg)
    body, stats = _body(conn)
    for title in ("полезное старое", "ферма старая", "продукт старый",
                  "X stale spike", "X stale product", "stalechan", "staleprod"):
        assert title not in body, title
    assert stats["block1"]["passed"] == 0
    assert stats["block2"]["passed"] == 0
    assert stats["block3"]["youtube"]["passed"] == 0
    assert stats["block3"]["x"]["passed"] == 0
    assert stats["block3"]["telegram"]["passed"] == 0
    assert stats["block4"]["passed"] == 0
    assert stats["block5"]["passed"] == 0


def test_fresh_material_still_passes_window(tmp_path):
    """Граница та же, что у секций 1–4: свежее внутри окна проходит."""
    yt = [_yt(1, "свежий продукт", 1_000, "чипы и железо", 20_000, age=9)]
    conn = build_core(tmp_path, yt=yt)
    body, stats = _body(conn)
    block1 = _region(body, "   Блок 1. YouTube — полезное", "   Блок 2.")
    assert "свежий продукт" in block1
    assert stats["block1"]["passed"] == 1


# --------------------------------------------------------------------------- #
# 16. «Норма канала»: число только при ≥ 3 материалах, иначе «мало данных»
# --------------------------------------------------------------------------- #
def test_channel_norm_printed_only_with_enough_materials(tmp_path):
    yt = [_yt(1, f"много {i}", 1_000, "наука и медицина", 10_000)
          for i in range(3)]
    yt.append(_yt(2, "единственный", 1_000, "наука и медицина", 10_000))
    conn = build_core(tmp_path, yt=yt)
    body, stats = _body(conn)
    many = _line_with(body, "много 0")
    single = _line_with(body, "единственный")
    assert "кратность к норме канала" in many and "(норма 10 000)" in many
    assert "норма: мало данных" in single
    assert stats["block1"]["shown"] == 3  # 2 с канала 1 (лимит) + 1 с канала 2


def test_single_material_channel_ratio_one_not_printed(tmp_path):
    """«Кратность 1.0 (норма = сам материал)» больше не число, а «мало данных»."""
    yt = [_yt(1, "одинокое видео", 2_000, "наука и медицина", 6_000)]
    conn = build_core(tmp_path, yt=yt)
    body, _ = _body(conn)
    line = _line_with(body, "одинокое видео")
    assert "норма: мало данных" in line
    assert "кратность к норме канала 1.0" not in line
    assert "кратность к норме канала" not in line


# --------------------------------------------------------------------------- #
# 17. Возраст материала: полный отчёт и сводка; неизвестная дата — честно
# --------------------------------------------------------------------------- #
def test_age_printed_in_full_report_and_compact(tmp_path):
    yt = [_yt(1, "свежее видео", 1_000, "наука и медицина", 10_000, age=4.0)]
    conn = build_core(tmp_path, yt=yt)
    body, _ = _body(conn)
    assert "опубликовано 4 дн. назад" in _line_with(body, "свежее видео")
    compact = report.build_compact(conn, days=10, db_path=":memory:")
    assert "4 дн" in _line_with(compact, "свежее видео")


def test_unknown_published_date_printed_honestly():
    row = report.ViralRow(content_id=1, title="без даты", channel="канал",
                          channel_key="канал", platform="youtube",
                          published_at=None)
    assert report._age_note(None) == "дата неизвестна"
    assert report._age_note_short("") == "дата неизвестна"
    assert "дата неизвестна" in report._viral_yt_line(row)
    assert "дата неизвестна" in report._viral_compact_item(row).text


# --------------------------------------------------------------------------- #
# 18. X: нижняя граница медианы автора (ТЗ-36 §3.4)
# --------------------------------------------------------------------------- #
def test_x_author_median_floor_drops_noise(tmp_path):
    """Медиана 1 лайк → автор отброшен; медиана 20 → всплеск проходит."""
    x = [_x("lowmed", 1, text=f"X lowmed base {i}") for i in range(5)]
    x.append(_x("lowmed", 5_000, text="X lowmed spike"))
    x += [_x("okmed", 20, text=f"X okmed base {i}") for i in range(4)]
    x.append(_x("okmed", 5_000, text="X okmed spike"))
    conn = build_core(tmp_path, x=x)
    body, stats = _body(conn)
    block4 = _region(body, "   Блок 4. X — истории с реакциями", "   Блок 5.")
    assert "X lowmed spike" not in block4
    assert "X okmed spike" in block4
    assert stats["block4"]["shown"] == 1


def test_x_author_median_not_mean(tmp_path):
    """Пол считается по медиане, а не по среднему (набор с выбросом)."""
    x = [_x("mean_author", 10, text=f"X mean base {i}") for i in range(4)]
    x.append(_x("mean_author", 1_000, text="X mean spike"))
    conn = build_core(tmp_path, x=x)
    body, _ = _body(conn)
    block4 = _region(body, "   Блок 4. X — истории с реакциями", "   Блок 5.")
    # медиана = 10 < 20 → автор отброшен, хотя среднее (208) пол бы прошло
    assert "X mean spike" not in block4
    assert report.VIRAL_X_MIN_AUTHOR_MEDIAN == 20.0


# --------------------------------------------------------------------------- #
# 19. Сводка: блок 2 (фермы) в «Виральные» не попадает никогда
# --------------------------------------------------------------------------- #
def test_compact_viral_excludes_block2_farm(tmp_path):
    yt = [
        _yt(1, "ферма максимальный охват", 1_000, "прочее", 900_000, vpd=500),
        _yt(2, "полезное в сводке", 1_000, "наука и медицина", 20_000, vpd=100),
    ]
    conn = build_core(tmp_path, yt=yt)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    viral = _region(text, "3. Виральные", "4. Новое за сутки")
    assert "ферма максимальный охват" not in viral
    assert "полезное в сводке" in viral
    # ферма при этом не потеряна: она в блоке 2 полного отчёта
    full = report.build(conn, days=10, db_path=":memory:")
    block2 = _region(full, "   Блок 2. YouTube", "   Блок 3.")
    assert "ферма максимальный охват" in block2


# --------------------------------------------------------------------------- #
# 20. Сводка: порядок «полезное → продукты 3.1 → X» (Telegram исключён, ТЗ-37)
# --------------------------------------------------------------------------- #
def test_compact_viral_order_useful_product_x(tmp_path):
    # Три полезных материала (бо́льший охват) занимают блок 1, продукт идёт
    # позицией 3.1, затем X. Telegram-позиция заведомо проходная, но её в сводке
    # быть не должно (ТЗ-37 §2.2).
    yt = [
        _yt(1, "Полезное первое", 1_000, "наука и медицина", 50_000, vpd=100),
        _yt(2, "Полезное второе", 1_000, "наука и медицина", 40_000, vpd=100),
        _yt(3, "Полезное третье", 1_000, "наука и медицина", 30_000, vpd=100),
        _yt(4, "Продукт второй", 5_000, "модели и релизы", 50_000, vpd=100),
    ]
    x = [_x("story_x", 20, text=f"X story base {i}") for i in range(4)]
    x.append(_x("story_x", 5_000, text="X четвёртый"))
    tg = [_tg(10, "tgchan", 20_000, baseline=1_000)]
    conn = build_core(tmp_path, yt=yt, x=x, tg=tg)
    items = report.viral_compact_items(conn, _iso(10), 10)
    titles = [it.desc for it in items]
    assert titles == ["Полезное первое", "Полезное второе", "Полезное третье",
                      "Продукт второй", "X четвёртый"]


# --------------------------------------------------------------------------- #
# 21. Сводка: размер с новыми полями возраста
# --------------------------------------------------------------------------- #
def test_compact_size_with_ages_within_limits(tmp_path):
    yt = [_yt(i, f"виральное {i}", 1_000, "наука и медицина", 5_000 * (i + 1),
              age=1.5 + i)
          for i in range(1, 10)]
    conn = build_core(tmp_path, yt=yt)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    size = len(text.encode("utf-8"))
    assert size <= report.COMPACT_LIMIT
    assert size <= 4096  # лимит Telegram
    assert " дн" in text  # возраст печатается


# --------------------------------------------------------------------------- #
# 22. Статистика блока 3 (три подсписка) — рассмотрено/прошло/показано
# --------------------------------------------------------------------------- #
def test_block3_stats_considered_passed_shown(tmp_path):
    yt = [_yt(1, "продукт", 1_000, "модели и релизы", 60_000, vpd=500)]
    x = [_x("prod", 600, topic="стартапы и бизнес")]
    tg = [_tg(10, "prodchan", 20_000, baseline=1_000, topic="инвестиции и раунды")]
    conn = build_core(tmp_path, yt=yt, x=x, tg=tg)
    body, stats = _body(conn)
    for sub in ("youtube", "x", "telegram"):
        st = stats["block3"][sub]
        assert st["considered"] >= st["passed"] >= st["shown"]
    for label in ("3.1", "3.2", "3.3"):
        assert f"{label}: рассмотрено" in body
        assert "прошло полы" in body and "показано" in body


# --------------------------------------------------------------------------- #
# 23. Список полезных тем — ровно из ТЗ-36 §3.1 (оба варианта ярлыков)
# --------------------------------------------------------------------------- #
def test_product_topics_exact_spec_list():
    assert report.PRODUCT_TOPICS == frozenset({
        "модели и релизы", "релизы моделей", "агенты и автоматизация",
        "кодинг и разработка", "чипы и железо", "инфраструктура и железо",
        "роботы и физический ИИ", "запуски и анонсы", "стартапы и бизнес",
        "инвестиции и раунды", "инструменты разработчика", "наука и медицина",
        "кейсы внедрения", "дата-центры и энергия",
    })
    assert len(report.PRODUCT_TOPICS) == 14
    # продукты блока 3 — подмножество полезных тем
    assert report.VIRAL_PRODUCT_TOPICS <= report.PRODUCT_TOPICS
    # тема ферм в полезный список не входит
    assert "ИИ-инструменты для обычных людей" not in report.PRODUCT_TOPICS
    assert "прочее" not in report.PRODUCT_TOPICS


# =========================================================================== #
# ТЗ-37: сводка — «Виральные» только охват ≥ 3, Telegram исключён (D-52)
# =========================================================================== #
def _titles(items):
    return [it.desc for it in items]


def _item_with(items, needle):
    for it in items:
        if needle in it.text:
            return it
    raise AssertionError(f"нет позиции сводки с {needle!r}")


# --------------------------------------------------------------------------- #
# 24. Позиция 3.1 с охватом < 3 (огромные просмотры): сводка — НЕТ, полный — ЕСТЬ
# --------------------------------------------------------------------------- #
def test_tz37_low_coverage_product_excluded_from_compact_kept_in_full(tmp_path):
    yt = [
        _yt(1, "низкий охват продукт", 10_000_000, "модели и релизы", 3_522_093),
        _yt(2, "высокий охват продукт", 1_000, "модели и релизы", 100_000),
    ]
    conn = build_core(tmp_path, yt=yt)
    items = report.viral_compact_items(conn, _iso(10), 10)
    titles = _titles(items)
    assert "высокий охват продукт" in titles      # охват 100.00 ≥ 3
    assert "низкий охват продукт" not in titles   # охват 0.35 < 3 — в сводку нельзя
    full = report.build(conn, days=10, db_path=":memory:")
    sub31 = _region(full, "      3.1. YouTube", "      3.2. X")
    assert "низкий охват продукт" in sub31        # в полном отчёте остаётся


# --------------------------------------------------------------------------- #
# 25. Позиция 3.1 с охватом ≥ 3: в сводке есть пометка, охват и возраст
# --------------------------------------------------------------------------- #
def test_tz37_high_coverage_product_marked_with_coverage_and_age(tmp_path):
    # Три полезных материала с бо́льшим охватом занимают блок 1, поэтому
    # продукт остаётся именно позицией 3.1 (а не дедуплицируется в блок 1).
    yt = [
        _yt(1, "полезное 1", 1_000, "наука и медицина", 50_000, vpd=100),
        _yt(2, "полезное 2", 1_000, "наука и медицина", 40_000, vpd=100),
        _yt(3, "полезное 3", 1_000, "наука и медицина", 30_000, vpd=100),
        _yt(4, "высокий охват продукт", 5_000, "модели и релизы", 50_000,
            vpd=100, age=4.0),
    ]
    conn = build_core(tmp_path, yt=yt)
    items = report.viral_compact_items(conn, _iso(10), 10)
    line = _item_with(items, "высокий охват продукт").text
    assert "(продукт)" in line
    assert "охват 10.00" in line
    assert "4 дн" in line


# --------------------------------------------------------------------------- #
# 26. Telegram (блок 5): сводка — НЕТ, полный отчёт — ЕСТЬ
# --------------------------------------------------------------------------- #
def test_tz37_telegram_excluded_from_compact_present_in_full(tmp_path):
    tg = [_tg(10, "tgchan", 500_000, baseline=1_000)]
    conn = build_core(tmp_path, tg=tg)
    items = report.viral_compact_items(conn, _iso(10), 10)
    assert all("tgchan" not in it.text for it in items)  # в сводке Telegram нет
    full = report.build(conn, days=10, db_path=":memory:")
    block5 = _region(full, "   Блок 5. Telegram", None)
    assert "tgchan" in block5                            # в полном блоке 5 — есть


# --------------------------------------------------------------------------- #
# 27. Инвариант: в сводке ни одна YouTube-строка «Виральных» не имеет охват < 3
# --------------------------------------------------------------------------- #
def test_tz37_all_compact_viral_youtube_rows_coverage_ge_3(tmp_path):
    # lang="ru" и просмотры < 10 000: материал НЕ попадает в раздел 1 сводки и
    # поэтому не дедуплицируется — «Виральные» на месте, охват парсится.
    yt = [
        _yt(1, "полезное a", 1_000, "наука и медицина", 9_000, lang="ru"),
        _yt(2, "полезное b", 1_000, "наука и медицина", 8_000, lang="ru"),
        _yt(3, "полезное c", 1_000, "наука и медицина", 7_000, lang="ru"),
        _yt(4, "низкий охват d", 10_000_000, "модели и релизы", 3_522_093),
    ]
    conn = build_core(tmp_path, yt=yt)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    viral = _region(text, "3. Виральные", "4. Новое за сутки")
    covs = [float(m) for m in re.findall(r"охват ([0-9]+\.[0-9]+)", viral)]
    assert covs, "в «Виральных» нет строк с охватом"
    assert all(c >= 3.0 for c in covs)
    assert "низкий охват d" not in viral


# --------------------------------------------------------------------------- #
# 28. Нехватка кандидатов: добора «абсолютными» позициями нет
# --------------------------------------------------------------------------- #
def test_tz37_shortage_no_absolute_fill(tmp_path):
    yt = [_yt(1, "низкий охват продукт", 10_000_000, "модели и релизы",
              3_522_093)]
    conn = build_core(tmp_path, yt=yt)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    # кандидатов блока 1 и 3.1 с охватом ≥ 3 нет → раздела «Виральные» нет,
    # «Новое за сутки» печатается всегда и забирает номер 3, а «Сквозной сюжет»
    # получает следующий свободный (4: YouTube, X, Новое).
    assert "3. Виральные" not in text
    assert "4. Сквозной сюжет" in text


# --------------------------------------------------------------------------- #
# 29. Дедупликация: позиция блока 1 не повторяется как (продукт)
# --------------------------------------------------------------------------- #
def test_tz37_block1_item_not_repeated_as_product(tmp_path):
    # материал проходит и блок 1 (тема полезная), и двойной пол 3.1
    yt = [_yt(1, "один материал", 1_000, "модели и релизы", 100_000)]
    conn = build_core(tmp_path, yt=yt)
    items = report.viral_compact_items(conn, _iso(10), 10)
    assert len(items) == 1
    assert "(продукт)" not in items[0].text


# --------------------------------------------------------------------------- #
# 30. Пометки ровно там, где нужно: (продукт) у 3.1, (X) у 4, у блока 1 — нет
# --------------------------------------------------------------------------- #
def test_tz37_marks_only_product_and_x(tmp_path):
    yt = [
        _yt(1, "полезное A", 1_000, "наука и медицина", 50_000, vpd=100),
        _yt(2, "полезное B", 1_000, "наука и медицина", 40_000, vpd=100),
        _yt(3, "полезное C", 1_000, "наука и медицина", 30_000, vpd=100),
        _yt(4, "продукт P", 5_000, "модели и релизы", 50_000, vpd=100),
    ]
    x = [_x("authorx", 20, text=f"X base {i}") for i in range(4)]
    x.append(_x("authorx", 5_000, text="X всплеск"))
    conn = build_core(tmp_path, yt=yt, x=x)
    items = report.viral_compact_items(conn, _iso(10), 10)
    assert "(продукт)" in _item_with(items, "продукт P").text
    assert "(X)" in _item_with(items, "X всплеск").text
    assert "(продукт)" not in _item_with(items, "полезное A").text
    assert "(X)" not in _item_with(items, "полезное A").text


# --------------------------------------------------------------------------- #
# 31. Лимиты сводки: не больше 2 позиций 3.1 и не больше 1 позиции X
# --------------------------------------------------------------------------- #
def test_tz37_compact_caps_block31_two_and_x_one(tmp_path):
    # Полезное (блок 1) — по охвату; продукты 3.1 — по просмотрам. Делаем так,
    # чтобы топ-2 по просмотрам (D, E) не попали в топ-3 по охвату (A, B, C):
    # тогда в сводку войдут ровно 2 помеченных продукта, а F отсечётся лимитом.
    yt = [
        _yt(1, "A полезное", 10_000, "наука и медицина", 10_000_000),
        _yt(2, "B полезное", 10_000, "наука и медицина", 9_000_000),
        _yt(3, "C полезное", 10_000, "наука и медицина", 8_000_000),
        _yt(4, "D продукт", 50_000, "модели и релизы", 290_000),
        _yt(5, "E продукт", 50_000, "модели и релизы", 285_000),
        _yt(6, "F продукт", 50_000, "модели и релизы", 280_000),
    ]
    x = [_x("capx", 20, text=f"X cap base {i}") for i in range(4)]
    x.append(_x("capx", 5_000, text="X cap spike 1"))
    x.append(_x("capx", 6_000, text="X cap spike 2"))
    conn = build_core(tmp_path, yt=yt, x=x)
    items = report.viral_compact_items(conn, _iso(10), 10)
    prod = [it for it in items if "(продукт)" in it.text]
    xs = [it for it in items if "(X)" in it.text]
    assert len(prod) == 2
    assert len(xs) == 1


# --------------------------------------------------------------------------- #
# 32. Продукты X (блок 3.2) в сводку не попадают (только YouTube 3.1)
# --------------------------------------------------------------------------- #
def test_tz37_x_product_not_in_compact(tmp_path):
    x = [_x("xprod", 600, topic="стартапы и бизнес", text="X xprod продукт")]
    x += [_x("xstory", 20, text=f"X story base {i}") for i in range(4)]
    x.append(_x("xstory", 5_000, text="X story spike"))
    conn = build_core(tmp_path, x=x)
    items = report.viral_compact_items(conn, _iso(10), 10)
    assert all("xprod" not in it.text for it in items)  # 3.2 в сводку не идёт
    assert any("X story spike" in it.text for it in items)  # блок 4 — идёт


# --------------------------------------------------------------------------- #
# 33. Техдолг D-52: маркер в report.py ↔ строка в TECH-DEBT.md (связка)
# --------------------------------------------------------------------------- #
def test_tz37_debt_d52_marker_and_registry_row():
    src = Path(report.__file__).read_text(encoding="utf-8")
    assert "TODO(debt-D-52)" in src
    root = Path(report.__file__).resolve().parents[2]
    registry = (root / "TECH-DEBT.md").read_text(encoding="utf-8")
    assert re.search(r"^\|\s*D-52\s*\|", registry, re.M)


# =========================================================================== #
# ТЗ-38: сводка — Telegram-раздел убран целиком, предохранитель по t.me/
# =========================================================================== #
def test_tz38_compact_has_no_telegram_and_no_links(tmp_path):
    """1. В сводке нет ни строки t.me/, ни Telegram-раздела."""
    yt = [_yt(1, "полезное сводки", 1_000, "наука и медицина", 50_000, vpd=100)]
    tg = [_tg(10, "tgchan", 500_000, baseline=1_000)]
    conn = build_core(tmp_path, yt=yt, tg=tg)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert "t.me/" not in text
    assert "Telegram" not in text
    assert "3. Telegram — топ постов за окно:" not in text


def test_tz38_full_report_keeps_telegram_section(tmp_path):
    """2. Полный отчёт Telegram-раздел СОХРАНЯЕТ (позитивная проверка)."""
    tg = [_tg(10, "tgchan", 500_000, baseline=1_000)]
    conn = build_core(tmp_path, tg=tg)
    full = report.build(conn, days=10, db_path=":memory:")
    assert "3. Telegram — свежие посты за окно." in full
    assert "https://t.me/tgchan/0" in full
    assert "   Блок 5. Telegram — выше своей нормы" in full


def test_tz38_compact_footer_has_no_telegram(tmp_path):
    """3. Подвал «Скрыто по платформам» не упоминает Telegram."""
    yt = [_yt(1, "полезное сводки", 1_000, "наука и медицина", 50_000, vpd=100)]
    tg = [_tg(10, "tgchan", 500_000, baseline=1_000)]
    conn = build_core(tmp_path, yt=yt, tg=tg)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    footer = [ln for ln in text.splitlines()
              if ln.startswith("Скрыто по платформам")]
    assert footer, "в сводке нет строки счётчика скрытых"
    assert "Telegram" not in footer[0]
    assert "YouTube" in footer[0] and "X" in footer[0]


def test_tz38_safeguard_drops_injected_telegram_section(tmp_path, monkeypatch):
    """4a. Предохранитель: подсунутый Telegram-раздел сборка выбрасывает."""
    yt = [_yt(1, "полезное сводки", 1_000, "наука и медицина", 50_000, vpd=100)]
    conn = build_core(tmp_path, yt=yt)
    evil = report.CompactItem("", "злой телеграм",
                              " — https://t.me/evil/1", content_id=999)
    monkeypatch.setattr(report, "telegram_items",
                        lambda c, cut: ([evil], {"considered": 1}))
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert "t.me/" not in text
    assert "злой телеграм" not in text
    assert "Telegram" not in text


def test_tz38_safeguard_drops_any_section_with_tme_link(tmp_path, monkeypatch):
    """4b. Любая строка с t.me/ выбрасывает свой раздел из сводки."""
    conn = build_core(tmp_path)
    leak = report.CompactItem("", "протечка ссылки",
                              " — https://t.me/leak/1", content_id=1)
    monkeypatch.setattr(report, "youtube_items",
                        lambda c, cut: ([leak], {"considered": 1}))
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert "t.me/" not in text
    assert "протечка ссылки" not in text
    assert "1. YouTube" not in text


def test_tz38_guard_function_units():
    """4c. Юнит-проверка предохранителя: имя Telegram и метка t.me/."""
    tme = report.CompactItem("", "x", " https://t.me/a/1")
    clean = report.CompactItem("", "x", " https://example.com/a")
    assert report._compact_section_is_telegram("Telegram", [])
    assert report._compact_section_is_telegram("YouTube", [tme])
    assert not report._compact_section_is_telegram("YouTube", [clean])
    assert not report._compact_section_is_telegram("X", [])


def test_tz38_other_sections_present_by_name(tmp_path):
    """5. Остальные разделы сводки на месте (по названиям)."""
    yt = [
        _yt(1, "Полезное первое", 1_000, "наука и медицина", 50_000, vpd=100),
        _yt(2, "Полезное второе", 1_000, "наука и медицина", 40_000, vpd=100),
        _yt(3, "Полезное третье", 1_000, "наука и медицина", 30_000, vpd=100),
    ]
    x = [_x("story_x", 20, text=f"X story base {i}") for i in range(4)]
    x.append(_x("story_x", 5_000, text="X всплеск"))
    tg = [_tg(10, "tgchan", 20_000, baseline=1_000)]
    conn = build_core(tmp_path, yt=yt, x=x, tg=tg)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    for name in ("1. YouTube", "2. X",
                 "Виральные — охват против своей аудитории:",
                 "Сквозной сюжет:"):
        assert name in text, name
    assert "Telegram" not in text
    assert len(text.encode("utf-8")) <= report.COMPACT_LIMIT


def test_tz38_compact_limit_unchanged():
    """COMPACT_LIMIT поднят до 12 000 байт (ТЗ-Tuber ч.1 §1, было 4 000)."""
    assert report.COMPACT_LIMIT == 12000


def test_tz38_debt_d52_markers_in_two_sites():
    """6. Маркер TODO(debt-D-52) в обоих местах; строка реестра расширена."""
    src = Path(report.__file__).read_text(encoding="utf-8")
    assert src.count("TODO(debt-D-52)") >= 2
    root = Path(report.__file__).resolve().parents[2]
    registry = (root / "TECH-DEBT.md").read_text(encoding="utf-8")
    assert re.search(r"^\|\s*D-52\s*\|", registry, re.M)
    assert "топ платформ" in registry


# =========================================================================== #
# ТЗ-39: сводка — нумерация разделов считается, а не печатается константой
# =========================================================================== #
#: Точные заголовки разделов сводки (с любым ведущим номером). Матчим полное
#: название после номера, чтобы не спутать заголовок раздела с нумерованным
#: пунктом внутри него.
_HEAD_RE = re.compile(
    r"^(\d+)\. (YouTube — топ по просмотрам/сутки:|"
    r"X — топ по лайкам:|Telegram — топ постов за окно:|"
    r"Виральные — охват против своей аудитории:|"
    r"Новое за сутки — вышедшее за 24 ч:|Сквозной сюжет:)")


def _section_numbers(text: str) -> list[int]:
    return [int(m.group(1)) for line in text.splitlines()
            if (m := _HEAD_RE.match(line))]


def _assert_contiguous(nums: list[int]) -> None:
    assert nums == list(range(1, len(nums) + 1)), f"дыра в нумерации: {nums}"


def test_tz39_compact_section_numbers_contiguous(tmp_path):
    """1. Номера заголовков сводки — ровно 1..N без пропусков."""
    # Полезное в раздел 1 (просмотры ≥ 50 000), отдельное виральное — в раздел 3;
    # так в сводке YouTube, X, «Виральные» и «Сквозной сюжет».
    yt = [
        _yt(1, "полезное для раздела 1", 1_000, "наука и медицина", 100_000),
        _yt(2, "виральное отдельное", 1_000, "наука и медицина", 20_000, vpd=500),
    ]
    x = [_x("author1", 20)]
    conn = build_core(tmp_path, yt=yt, x=x)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    nums = _section_numbers(text)
    assert nums == [1, 2, 3, 4, 5], (
        f"ожидались YouTube/X/Виральные/Новое/сюжет: {text}")
    _assert_contiguous(nums)


def test_tz39_empty_x_keeps_contiguous(tmp_path):
    """2. Пустой X не оставляет дыру: остальные разделы всё равно 1..N."""
    yt = [_yt(2, "виральное отдельное", 1_000, "наука и медицина", 20_000, vpd=500)]
    conn = build_core(tmp_path, yt=yt)  # X не создаётся вовсе
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert "2. X — топ по лайкам:" in text
    assert "нет данных за окно." in text
    _assert_contiguous(_section_numbers(text))


def test_tz39_telegram_returned_gets_next_number(tmp_path, monkeypatch):
    """3. Заготовка возврата Telegram (D-52): раздел встаёт на свободный номер."""
    yt = [
        _yt(1, "полезное для раздела 1", 1_000, "наука и медицина", 100_000),
        _yt(2, "виральное отдельное", 1_000, "наука и медицина", 20_000, vpd=500),
    ]
    x = [_x("author1", 20)]
    conn = build_core(tmp_path, yt=yt, x=x)
    clean = report.CompactItem("", "телеграм-пост после D-52",
                               " — https://tg.example/post", content_id=777)
    # Имитируем возврат Telegram: предохранитель ТЗ-38 отключён, данные поданы.
    monkeypatch.setattr(report, "_compact_section_is_telegram",
                        lambda name, items: False)
    monkeypatch.setattr(report, "telegram_items",
                        lambda c, cut: ([clean], {"considered": 1}))
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert "3. Telegram — топ постов за окно:" in text
    assert "4. Виральные — охват против своей аудитории:" in text
    assert "5. Новое за сутки — вышедшее за 24 ч:" in text
    assert "6. Сквозной сюжет:" in text
    _assert_contiguous(_section_numbers(text))


def test_tz39_compact_has_no_telegram_links(tmp_path):
    """4. Инвариант ТЗ-38 не сломан: в сводке нет t.me/."""
    yt = [_yt(1, "полезное", 1_000, "наука и медицина", 100_000)]
    tg = [_tg(10, "tgchan", 500_000, baseline=1_000)]
    conn = build_core(tmp_path, yt=yt, tg=tg)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert "t.me/" not in text
    assert "Telegram" not in text


def test_tz39_compact_size_within_limit(tmp_path):
    """5. Размер сводки ≤ COMPACT_LIMIT (12 000 байт) при многих пунктах."""
    yt = [_yt(i, f"виральное {i}", 1_000, "наука и медицина", 5_000 * (i + 1),
              age=1.5 + i) for i in range(1, 12)]
    conn = build_core(tmp_path, yt=yt)
    text = report.build_compact(conn, days=10, db_path=":memory:")
    assert len(text.encode("utf-8")) <= report.COMPACT_LIMIT
    _assert_contiguous(_section_numbers(text))


def test_tz39_full_report_block_numbers_unchanged(tmp_path):
    """6. Полный отчёт сохраняет смысловую нумерацию 1..5 без изменений."""
    tg = [_tg(10, "tgchan", 500_000, baseline=1_000)]
    conn = build_core(tmp_path, tg=tg)
    full = report.build(conn, days=10, db_path=":memory:")
    for head in ("1. YouTube", "2. X", "3. Telegram", "4. Сквозной сюжет",
                 "5. Виральные"):
        assert head in full, head
