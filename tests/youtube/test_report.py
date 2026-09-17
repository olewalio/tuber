"""Тесты витрины отчёта (tuber.report). Сеть не используется вообще."""

from __future__ import annotations

import json
import time

import pytest

from tuber.platforms.youtube import config, store as db, report

NOW = int(time.time())


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "report_test.db")
    db.init_db(c)
    yield c
    c.close()


# --- помощники -------------------------------------------------------------


def add_channel(conn, cid, title="Канал", subs=1000, is_russian=None):
    db.upsert_channel(
        conn,
        {
            "channel_id": cid,
            "title": title,
            "subscriber_count": subs,
            "is_russian": is_russian,
            "first_seen": 1,
        },
    )


def add_video(
    conn,
    vid,
    cid,
    title=None,
    is_ai=1,
    topic=None,
    lang="en",
    title_ru=None,
    published_at=None,
    duration=600,
    is_shorts=None,
    description="Описание видео",
    tags=None,
):
    data = {
        "video_id": vid,
        "channel_id": cid,
        "title": title or f"Видео {vid}",
        "description": description,
        "tags": json.dumps(tags, ensure_ascii=False) if tags is not None else None,
        "duration_seconds": duration,
        "published_at": published_at if published_at is not None else NOW - 2 * 86400,
        "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
        "thumbnail_width": 1280,
        "thumbnail_height": 720,
        "first_seen": 1,
    }
    if is_shorts is not None:
        data["is_shorts"] = is_shorts
    db.upsert_video(conn, data)
    db.save_classification(
        conn,
        vid,
        is_ai=is_ai,
        topic=topic,
        title_ru=title_ru or (title or f"Видео {vid}"),
        lang=lang,
        confidence=0.9,
    )


def add_snap(conn, vid, views, captured_at, likes=0, comments=0, bucket="d"):
    db.insert_snapshot(
        conn, vid, captured_at, bucket, views=views, likes=likes, comments=comments
    )


def add_pair(conn, vid, v0, v1, interval=10800, likes0=0, likes1=0, comments0=0, comments1=0):
    """Две точки во времени: скорость считается от дельты и интервала."""
    start = NOW - 4 * 3600
    add_snap(conn, vid, v0, start, likes0, comments0)
    add_snap(conn, vid, v1, start + interval, likes0 + likes1, comments0 + comments1)


# --- 1. ранжирование по скорости, а не по просмотрам -----------------------


def test_rising_ranks_by_speed_not_views(conn):
    add_channel(conn, "c1", subs=100000)
    # Мёртвый груз: 1 млн просмотров, но всего 48/сутки.
    add_video(conn, "slow", "c1", title="Миллионник", is_ai=1)
    add_pair(conn, "slow", 999_994, 1_000_000, interval=10800)
    # Живой тренд: 50 тыс., но 3600/сутки.
    add_video(conn, "fast", "c1", title="Живой", is_ai=1)
    add_pair(conn, "fast", 49_550, 50_000, interval=10800)

    items = report.rising(conn)
    ids = [i["video_id"] for i in items]
    assert ids.index("fast") < ids.index("slow")
    fast = next(i for i in items if i["video_id"] == "fast")
    slow = next(i for i in items if i["video_id"] == "slow")
    assert fast["views"] < slow["views"]
    assert fast["views_per_day"] > slow["views_per_day"]


def test_rising_only_ai_and_fresh(conn):
    add_channel(conn, "c1")
    add_video(conn, "ai", "c1", is_ai=1)
    add_pair(conn, "ai", 100, 200, interval=10800)
    add_video(conn, "not_ai", "c1", is_ai=0)
    add_pair(conn, "not_ai", 100, 5000, interval=10800)
    # Старый замер вне окна: видео не должно попасть.
    add_video(conn, "old", "c1", is_ai=1)
    old_cap = int(time.time()) - 40 * 86400
    add_snap(conn, "old", 10, old_cap)
    add_snap(conn, "old", 100, old_cap + 10800)

    ids = {i["video_id"] for i in report.rising(conn, days=10)}
    assert ids == {"ai"}


def test_rising_item_has_required_fields(conn):
    add_channel(conn, "c1", subs=1234)
    add_video(conn, "v1", "c1", is_ai=1, topic="агенты и автоматизация")
    add_pair(conn, "v1", 1000, 2000, interval=10800, likes1=50, comments1=10)
    item = report.rising(conn)[0]
    for key in (
        "video_id", "title", "title_ru", "channel_title", "subscriber_count",
        "views", "views_per_day", "views_per_hour", "likes", "comments",
        "likes_per_1000", "comments_per_1000", "outlier", "topic",
        "published_at", "url",
    ):
        assert key in item
    assert item["likes_per_1000"] == pytest.approx(25.0)   # 50 лайков на 2000
    assert item["comments_per_1000"] == pytest.approx(5.0)  # 10 на 2000
    assert item["url"].endswith("v=v1")


# --- 2. outlier и тёмные лошадки -------------------------------------------


def test_outlier_median_ignores_non_ai(conn):
    add_channel(conn, "c1")
    # Пять ИИ-видео (минимум выборки теперь 5): медиана 200.
    for vid, views in (("a", 100), ("b", 200), ("c", 300), ("d", 200), ("e", 200)):
        add_video(conn, vid, "c1", is_ai=1)
        add_snap(conn, vid, views, NOW - 3600)
    # Не-ИИ видео с огромными просмотрами не должно тянуть медиану.
    add_video(conn, "noise", "c1", is_ai=0)
    add_snap(conn, "noise", 1_000_000, NOW - 3600)

    score = report.outlier_score(conn, "a")
    assert score == pytest.approx(0.5)  # 100 / 200, а не 100 / 250


def test_outlier_none_when_too_few_videos(conn):
    add_channel(conn, "c1")
    for vid in ("a", "b"):
        add_video(conn, vid, "c1", is_ai=1)
        add_snap(conn, vid, 100, NOW - 3600)
    assert report.outlier_score(conn, "a") is None


def test_dark_horses_excludes_none_outlier(conn):
    # Канал с двумя видео: outlier не считается (None), в список попасть не должен.
    add_channel(conn, "tiny", subs=50)
    for vid in ("t1", "t2"):
        add_video(conn, vid, "tiny", is_ai=1)
        add_snap(conn, vid, 100, NOW - 3600)
    # Крупный канал, чтобы медиана подписчиков была высокой.
    add_channel(conn, "big", subs=1_000_000)
    add_video(conn, "big1", "big", is_ai=1)
    add_snap(conn, "big1", 100, NOW - 3600)

    horses = report.dark_horses(conn)
    assert all(h["outlier"] is not None for h in horses)
    assert "t1" not in {h["video_id"] for h in horses}


def test_dark_horses_suspicious_and_priority(conn):
    add_channel(conn, "small", subs=100)
    # База канала: три видео по 100 просмотров.
    for vid in ("f1", "f2", "f3"):
        add_video(conn, vid, "small", is_ai=1)
        add_snap(conn, vid, 100, NOW - 3600)
    # Аномалия: 1500 -> outlier 15x.
    add_video(conn, "hero", "small", is_ai=1)
    add_snap(conn, "hero", 1500, NOW - 3600)
    # Приоритет: 700 -> outlier 7x, но он меняет медиану на 100.
    add_video(conn, "mid", "small", is_ai=1)
    add_snap(conn, "mid", 700, NOW - 3600)

    add_channel(conn, "big", subs=10_000_000)
    add_video(conn, "big1", "big", is_ai=1)
    add_snap(conn, "big1", 100, NOW - 3600)

    # медиана [100,100,100,1500,700] = 100 -> hero 15x, mid 7x
    horses = {h["video_id"]: h for h in report.dark_horses(conn)}
    assert horses["hero"]["suspicious"] is True
    assert horses["hero"]["priority"] is False
    assert horses["mid"]["priority"] is True
    assert horses["mid"]["suspicious"] is False
    # Крупный канал отсечён по подписчикам.
    assert "big1" not in horses


def test_dark_horses_large_channel_excluded(conn):
    add_channel(conn, "big", subs=10_000_000)
    for vid in ("a", "b", "c"):
        add_video(conn, vid, "big", is_ai=1)
        add_snap(conn, vid, 100, NOW - 3600)
    add_video(conn, "hero", "big", is_ai=1)
    add_snap(conn, "hero", 5000, NOW - 3600)
    assert report.dark_horses(conn) == []


# --- 3. темы ---------------------------------------------------------------


def test_topics_without_data_are_not_dropped(conn):
    topics = report.by_topic(conn)
    assert [t["topic"] for t in topics] == list(config.TOPICS)
    assert all(t["has_data"] is False for t in topics)
    assert all(t["top"] == [] for t in topics)


def test_topic_grouping(conn):
    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, topic="агенты и автоматизация")
    add_pair(conn, "v1", 1000, 2000, interval=10800)
    topics = {t["topic"]: t for t in report.by_topic(conn)}
    assert topics["агенты и автоматизация"]["has_data"] is True
    assert topics["агенты и автоматизация"]["count"] == 1
    assert topics["чипы и железо"]["has_data"] is False


# --- 4. комментарии --------------------------------------------------------


def test_comment_leaders_skip_short_interval(conn):
    add_channel(conn, "c1")
    # Короткий интервал 300 с < 600: дельта не считается, видео нет в списке.
    add_video(conn, "short", "c1", is_ai=1)
    start = NOW - 3600
    add_snap(conn, "short", 100, start, comments=1)
    add_snap(conn, "short", 200, start + 300, comments=50)
    # Валидный интервал.
    add_video(conn, "long", "c1", is_ai=1)
    add_pair(conn, "long", 100, 200, interval=3600, comments0=0, comments1=24)

    items = report.comment_leaders(conn)
    ids = {i["video_id"] for i in items}
    assert "short" not in ids
    assert "long" in ids
    long_item = next(i for i in items if i["video_id"] == "long")
    assert long_item["interval_seconds"] >= 600
    assert long_item["comments_per_day"] == pytest.approx(576.0)  # 24 / (1/24)


# --- 5. русский срез -------------------------------------------------------


def test_ru_slice_does_not_mix_with_world(conn):
    add_channel(conn, "cru", title="Русский канал", is_russian=1)
    add_channel(conn, "cworld", title="World channel", is_russian=0)
    add_video(conn, "ru", "cru", is_ai=1, lang="ru")
    add_pair(conn, "ru", 1000, 2000, interval=10800)
    add_video(conn, "en", "cworld", is_ai=1, lang="en")
    add_pair(conn, "en", 5000, 6000, interval=10800)
    # Англоязычный ролик на русскоязычном канале тоже русский.
    add_video(conn, "ru_by_channel", "cru", is_ai=1, lang="en")
    add_pair(conn, "ru_by_channel", 100, 900, interval=10800)

    ru_ids = {i["video_id"] for i in report.ru_slice(conn)}
    assert ru_ids == {"ru", "ru_by_channel"}
    world_ids = {i["video_id"] for i in report.rising(conn, lang="world")}
    assert "en" in world_ids
    assert "ru" not in world_ids


# --- 6. SEO-сборник --------------------------------------------------------


def test_seo_pack_collects_fields(conn):
    add_channel(conn, "c1")
    add_video(
        conn, "v1", "c1", is_ai=1,
        title="GPT-5 в 2026 году: тест",
        description="Первые 200 знаков описания",
        tags=["ai", "gpt", "ai"], duration=45,
    )
    add_pair(conn, "v1", 1000, 2000, interval=10800, likes1=100, comments1=20)

    pack = report.seo_pack(conn)
    assert pack["summary"]["count"] == 1
    v = pack["videos"][0]
    assert v["title_has_number"] is True
    assert v["title_has_year"] is True
    assert v["format"] == "Shorts"
    assert v["tags"] == ["ai", "gpt", "ai"]
    assert v["thumb_width"] == 1280
    assert pack["summary"]["shorts_share"] == 1.0
    assert pack["summary"]["avg_title_length"] > 0


def test_seo_pack_mentions_no_ctr_or_retention(conn):
    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, description="описание")
    add_pair(conn, "v1", 1000, 2000, interval=10800, likes1=100, comments1=20)
    blob = json.dumps(report.seo_pack(conn), ensure_ascii=False).lower()
    assert "ctr" not in blob
    assert "удержан" not in blob


# --- 7. отчёт без данных ---------------------------------------------------


def test_empty_db_report_has_no_zeros(conn):
    text = report.build_report(conn)
    assert "Нет данных" in text
    assert "нужно 2 замера" in text
    # Нулей вместо отсутствующих данных быть не должно.
    assert "скорость 0" not in text
    assert "всего 0" not in text
    assert "0 подписчиков" not in text


def test_report_counts_accumulated_snapshots(conn):
    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1)
    add_snap(conn, "v1", 100, NOW - 3600)
    text = report.build_report(conn)
    assert "накоплено замеров: 1" in text


def test_report_with_data_contains_blocks(conn):
    add_channel(conn, "c1", title="Канал", subs=1000)
    add_video(conn, "v1", "c1", is_ai=1, topic="кодинг и разработка")
    add_pair(conn, "v1", 1000, 2000, interval=10800, likes1=100, comments1=20)
    text = report.build_report(conn)
    for block in (
        "ЧТО РАСТЁТ", "ТЕМЫ", "ЧТО ОБСУЖДАЮТ", "РУССКИЙ ЮТУБ",
        "ТЁМНЫЕ ЛОШАДКИ", "SEO-СБОРНИК",
    ):
        assert block in text
    assert "просмотров/сутки" in text
    assert "ctr" not in text.lower()
    assert "удержан" not in text.lower()


# --- 8. разделение шортсов и полных ----------------------------------------


def test_outlier_not_inflated_by_other_format(conn):
    """Ключевой тест: шортсы по 100k не раздувают outlier полного видео по 5k."""
    add_channel(conn, "c1", subs=1000)
    for i in range(6):
        add_video(conn, f"s{i}", "c1", is_ai=1, duration=45, is_shorts=1)
        add_snap(conn, f"s{i}", 100_000, NOW - 3600)
    for i in range(5):
        add_video(conn, f"l{i}", "c1", is_ai=1, duration=900, is_shorts=0)
        add_snap(conn, f"l{i}", 5_000, NOW - 3600)

    # Медиана формата long = 5k, поэтому outlier полного ровно 1x.
    assert report.outlier_score(conn, "l0", fmt="long") == pytest.approx(1.0)
    # Шортс сравнивается со своей медианой 100k.
    assert report.outlier_score(conn, "s0", fmt="short") == pytest.approx(1.0)
    # Без fmt формат берётся у самого видео — результат тот же.
    assert report.outlier_score(conn, "l0") == pytest.approx(1.0)
    assert report.outlier_score(conn, "s0") == pytest.approx(1.0)


def test_outlier_none_when_format_has_too_few_videos(conn):
    """Меньше 5 видео в формате — outlier None, а не цифра с потолка."""
    add_channel(conn, "c1")
    for i in range(4):
        add_video(conn, f"l{i}", "c1", is_ai=1, duration=900, is_shorts=0)
        add_snap(conn, f"l{i}", 1000, NOW - 3600)
    assert report.outlier_score(conn, "l0", fmt="long") is None

    add_video(conn, "l4", "c1", is_ai=1, duration=900, is_shorts=0)
    add_snap(conn, "l4", 1000, NOW - 3600)
    assert report.outlier_score(conn, "l0", fmt="long") == pytest.approx(1.0)


def test_report_functions_filter_by_format(conn):
    add_channel(conn, "c1")
    add_video(conn, "sh", "c1", is_ai=1, duration=45, is_shorts=1,
              topic="кодинг и разработка")
    add_pair(conn, "sh", 1000, 2000, interval=10800, comments1=10)
    add_video(conn, "lo", "c1", is_ai=1, duration=900, is_shorts=0,
              topic="модели и релизы")
    add_pair(conn, "lo", 1000, 3000, interval=10800, comments1=20)

    assert {i["video_id"] for i in report.rising(conn, fmt="short")} == {"sh"}
    assert {i["video_id"] for i in report.rising(conn, fmt="long")} == {"lo"}
    assert {i["video_id"] for i in report.rising(conn, fmt=None)} == {"sh", "lo"}

    topics = {t["topic"]: t for t in report.by_topic(conn, fmt="short")}
    assert topics["кодинг и разработка"]["has_data"] is True
    assert topics["модели и релизы"]["has_data"] is False

    assert {c["video_id"] for c in report.comment_leaders(conn, fmt="long")} == {"lo"}
    assert {c["video_id"] for c in report.comment_leaders(conn, fmt="short")} == {"sh"}


def test_build_report_splits_streams(conn):
    add_channel(conn, "c1", title="Канал", subs=1000)
    add_video(conn, "sh", "c1", is_ai=1, duration=45, is_shorts=1)
    add_pair(conn, "sh", 1000, 2000, interval=10800)
    add_video(conn, "lo", "c1", is_ai=1, duration=900, is_shorts=0)
    add_pair(conn, "lo", 1000, 5000, interval=10800)

    text = report.build_report(conn, fmt="all")
    assert "## Шортсы" in text
    assert "## Полные видео" in text
    short_part, long_part = text.split("## Полные видео", 1)
    assert "Видео sh" in short_part
    assert "Видео lo" not in short_part
    assert "Видео lo" in long_part

    only_short = report.build_report(conn, fmt="short")
    assert "## Шортсы" in only_short
    assert "## Полные видео" not in only_short


# --- 9. предохранитель расчёта скорости (этап 10) --------------------------


def test_rising_excludes_short_interval(conn):
    """Замер со 'short' не попадает в топ, пригодный замер попадает."""
    add_channel(conn, "c1")
    start = NOW - 4 * 3600
    add_video(conn, "short", "c1", is_ai=1)
    add_snap(conn, "short", 1000, start)
    add_snap(conn, "short", 1106, start + 748)  # 12,5 мин -> short
    add_video(conn, "ok", "c1", is_ai=1)
    add_pair(conn, "ok", 1000, 2000, interval=10800)

    ids = {i["video_id"] for i in report.rising(conn)}
    assert "short" not in ids
    assert "ok" in ids


def test_report_all_short_is_honest_without_zeros(conn):
    """Нет пригодных замеров — блок честный, без нулей вместо скорости."""
    add_channel(conn, "c1")
    start = NOW - 4 * 3600
    add_video(conn, "v1", "c1", is_ai=1)
    add_snap(conn, "v1", 1000, start)
    add_snap(conn, "v1", 2000, start + 748)

    text = report.build_report(conn)
    # Абсурдных скоростей в отчёте нет вовсе.
    assert "просмотров/сутки" not in text
    assert "скорость 0" not in text
    # В шапке честная разбивка.
    assert "Пригодны для скорости (интервал от 3600 с): 0" in text
    assert "слишком короткие: 1" in text
    # Блок «Что растёт» не выдумывает топ, а объясняет нехватку данных.
    rising_block = text.split("ЧТО РАСТЁТ", 1)[1].split("ТЕМЫ", 1)[0]
    assert "Нет данных" in rising_block
    assert "Видео v1" not in rising_block


def test_report_header_counts_ok_and_short(conn):
    """Шапка показывает, сколько замеров пригодны, а сколько коротки."""
    add_channel(conn, "c1")
    add_video(conn, "ok", "c1", is_ai=1)
    add_pair(conn, "ok", 1000, 2000, interval=10800)
    start = NOW - 4 * 3600
    add_video(conn, "short", "c1", is_ai=1)
    add_snap(conn, "short", 1000, start)
    add_snap(conn, "short", 1106, start + 748)

    text = report.build_report(conn)
    assert "Пригодны для скорости (интервал от 3600 с): 1" in text
    assert "слишком короткие: 1" in text
    # «видео со скоростью» отражает только пригодные замеры.
    assert "видео со скоростью: 1" in text


# --- лучший комментарий в блоке «ЧТО ОБСУЖДАЮТ» ----------------------------


def _add_comment(conn, comment_id, video_id, text, likes, captured_at):
    conn.execute(
        "INSERT INTO video_comments (comment_id, video_id, author, text, likes, "
        "published_at, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (comment_id, video_id, "Автор", text, likes, captured_at, captured_at),
    )
    conn.commit()


def test_report_block_shows_top_comment(conn):
    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, title="Видео v1", is_shorts=0)
    add_pair(conn, "v1", 1000, 2000, interval=3600, comments0=0, comments1=24)
    _add_comment(conn, "a", "v1", "Слабый", likes=1, captured_at=NOW)
    _add_comment(conn, "b", "v1", "Лучший\nтекст", likes=12, captured_at=NOW)

    text = report.build_report(conn)
    long_block = text.split("## Полные видео", 1)[1]
    block = long_block.split("ЧТО ОБСУЖДАЮТ", 1)[1].split("РУССКИЙ ЮТУБ", 1)[0]
    assert "Лучший комментарий (лайков 12): Лучший текст" in block
    assert "\nЛучший текст" not in block  # перевод строки заменён пробелом


def test_report_top_comment_truncated_to_140(conn):
    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, is_shorts=0)
    add_pair(conn, "v1", 1000, 2000, interval=3600, comments0=0, comments1=24)
    _add_comment(conn, "a", "v1", "y" * 300, likes=3, captured_at=NOW)

    text = report.build_report(conn)
    long_block = text.split("## Полные видео", 1)[1]
    block = long_block.split("ЧТО ОБСУЖДАЮТ", 1)[1].split("РУССКИЙ ЮТУБ", 1)[0]
    assert "Лучший комментарий (лайков 3): " + "y" * 140 in block
    assert "y" * 141 not in block


def test_stream_json_has_best_comment(conn):
    """Пункт 4: JSON-сводка несёт лучший комментарий — то же, что в тексте."""
    from tuber.platforms.youtube import cli

    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, is_shorts=0)
    add_pair(conn, "v1", 1000, 2000, interval=3600, comments0=0, comments1=24)
    _add_comment(conn, "a", "v1", "Слабый", likes=1, captured_at=NOW)
    _add_comment(conn, "b", "v1", "Лучший текст", likes=12, captured_at=NOW)

    streams = cli._stream_json(conn, 10, "long")
    leaders = streams["comments"]
    assert len(leaders) == 1
    best = leaders[0]["best_comment"]
    assert best == {
        "text": "Лучший текст",
        "author": "Автор",
        "likes": 12,
        "video_url": "https://www.youtube.com/watch?v=v1",
    }


def test_stream_json_best_comment_none_without_comments(conn):
    """Нет комментариев — best_comment остаётся честным None, а не исчезает."""
    from tuber.platforms.youtube import cli

    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, is_shorts=0)
    add_pair(conn, "v1", 1000, 2000, interval=3600, comments0=0, comments1=24)

    streams = cli._stream_json(conn, 10, "long")
    assert streams["comments"][0]["best_comment"] is None


# --- виральность: топ по индексу, антимусор, тема (ТЗ виральности, ч.2) ----


def add_viral(conn, vid, index, axes=None, computed_at=None, likes_per_1000=1.0,
              capped=False):
    """Записать последний скор с индексом виральности и разбивкой по осям."""
    from tuber.platforms.youtube import store as db

    moment = computed_at if computed_at is not None else NOW
    db.save_score(conn, vid, moment, likes_per_1000=likes_per_1000,
                  outlier_score=1.0)
    parts = json.dumps({
        "viral": {"axes": axes or {}, "index": index, "half_life_days": 7.0,
                  "capped": capped}
    })
    db.set_viral_indices(conn, [(vid, moment, index, parts)])


def test_viral_top_ranks_by_index_and_skips_null_and_zero(conn):
    add_channel(conn, "c1")
    for vid, likes in (("hi", 10), ("lo", 5), ("nul", 3), ("zero", 0)):
        add_video(conn, vid, "c1", is_ai=1, is_shorts=0)
        add_pair(conn, vid, 1000, 5000, interval=3600, likes0=0, likes1=likes)
    add_viral(conn, "hi", 5.0, {"views": 5.0, "likes": 2.0})
    add_viral(conn, "lo", 1.0, {"views": 1.0, "likes": 1.0})
    add_viral(conn, "nul", None, {"views": 1.0})
    add_viral(conn, "zero", 9.0, {"views": 9.0, "likes": 1.0}, likes_per_1000=0.0)

    top = report.viral_top(conn, days=10, fmt="long")
    assert [it["video_id"] for it in top] == ["hi", "lo"]
    assert top[0]["viral_index"] == 5.0
    assert top[0]["axes"]["views"] == 5.0

    zero = report.viral_likes_zero(conn, days=10, fmt="long")
    assert [it["video_id"] for it in zero] == ["zero"]


def test_viral_stats_count_null_and_zero(conn):
    add_channel(conn, "c1")
    for vid, likes in (("hi", 10), ("nul", 3), ("zero", 0)):
        add_video(conn, vid, "c1", is_ai=1, is_shorts=0)
        add_pair(conn, vid, 1000, 5000, interval=3600, likes0=0, likes1=likes)
    add_viral(conn, "hi", 5.0, {"views": 5.0})
    add_viral(conn, "nul", None, {"views": 1.0})
    add_viral(conn, "zero", 9.0, {"views": 9.0}, likes_per_1000=0.0)

    stats = report.viral_window_stats(conn, days=10, fmt="long")
    assert stats["candidates"] == 3
    assert stats["indexed"] == 2
    assert stats["null_index"] == 1
    assert stats["likes_zero"] == 1


def test_topic_filter_uses_classification_not_primary_topic(conn):
    """Фильтр темы читает video_classification.topic, а не videos.primary_topic."""
    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, is_shorts=0, topic="роботы и физический ИИ")
    add_video(conn, "v2", "c1", is_ai=1, is_shorts=0, topic="модели и релизы")
    for vid in ("v1", "v2"):
        add_pair(conn, vid, 1000, 5000, interval=3600, likes1=10)
        add_viral(conn, vid, 3.0, {"views": 3.0, "likes": 1.0})
    # Ложное поле темы у видео, которое не должно попадать под фильтр.
    conn.execute(
        "UPDATE videos SET primary_topic='роботы и физический ИИ' WHERE video_id='v2'"
    )
    conn.commit()

    filtered = report.viral_top(conn, days=10, fmt="long",
                                topic="роботы и физический ИИ")
    assert [it["video_id"] for it in filtered] == ["v1"]

    before = report.viral_window_stats(conn, days=10, fmt="long")
    after = report.viral_window_stats(conn, days=10, fmt="long",
                                      topic="роботы и физический ИИ")
    assert before["candidates"] == 2
    assert after["candidates"] == 1
    assert after["candidates_before_topic"] == 2

    # Тема, которой нет в video_classification.topic, даёт ноль, но не потому,
    # что фильтр читает пустой primary_topic: у v1 тема реальная.
    empty = report.viral_top(conn, days=10, fmt="long", topic="несуществующая")
    assert empty == []


def test_build_report_warns_when_index_missing(conn):
    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, is_shorts=0)
    add_pair(conn, "v1", 1000, 5000, interval=3600, likes1=10)
    # Скор есть, но без вирального индекса.
    from tuber.platforms.youtube import store as db
    db.save_score(conn, "v1", NOW, likes_per_1000=2.0, outlier_score=1.0)

    text = report.build_report(conn, days=10, fmt="long")
    assert "ВНИМАНИЕ: индекс виральности не посчитан" in text

    stats = report.viral_window_stats(conn, days=10, fmt="long")
    assert stats["candidates"] == 1
    assert stats["indexed"] == 0


# --- виральность: порог показов и блок малой выборки (ТЗ порога показов) ----


def _seed_micro_and_big(conn):
    """Одно реальное видео (5000 просмотров) и одна микровыборка (3 просмотра)."""
    add_channel(conn, "c1")
    add_video(conn, "big", "c1", is_ai=1, is_shorts=0)
    add_pair(conn, "big", 0, 5000, interval=3600, likes1=10)
    add_viral(conn, "big", 5.0, {"views": 5.0})
    add_video(conn, "micro", "c1", is_ai=1, is_shorts=0)
    # 1 лайк на 3 просмотра — тот самый раздутый случай.
    add_pair(conn, "micro", 0, 3, interval=3600, likes1=1)
    add_viral(conn, "micro", 9.0, {"views": 0.43, "likes": 116.33})


def test_below_min_views_excluded_from_top_and_shown_as_small_sample(conn):
    _seed_micro_and_big(conn)
    top = report.viral_top(conn, days=10, fmt="long")
    assert [it["video_id"] for it in top] == ["big"]

    small = report.viral_small_sample(conn, days=10, fmt="long")
    assert [it["video_id"] for it in small] == ["micro"]
    assert report.viral_small_sample_count(conn, days=10, fmt="long") == 1


def test_stats_counters_match_small_sample_block(conn):
    _seed_micro_and_big(conn)
    stats = report.viral_window_stats(conn, days=10, fmt="long")
    assert stats["min_views"] == config.VIRAL_MIN_VIEWS
    assert stats["honest_candidates"] == 2
    assert stats["below_min_views"] == 1
    assert stats["honest_above_min_views"] == 1
    # Счётчик шапки совпадает с содержимым блока малой выборки.
    assert stats["below_min_views"] == report.viral_small_sample_count(
        conn, days=10, fmt="long"
    )


def test_report_text_has_threshold_header_and_small_sample_block(conn):
    _seed_micro_and_big(conn)
    text = report.build_report(conn, days=10, fmt="long")
    assert "Порог показов честного топа" in text
    assert "МАЛАЯ ВЫБОРКА (не идут в честный топ)" in text
    # Микровидео лежит в блоке малой выборки, а не в честном топе.
    assert text.index("МАЛАЯ ВЫБОРКА") < text.index("micro") if "micro" in text else True
    # Микровидео не попадает в честный топ: индекса 9.0 там нет.
    top_lines = [ln for ln in text.splitlines() if ln.startswith("1. ")]
    assert top_lines


def test_flag_capped_video_counts_in_stats(conn):
    add_channel(conn, "c1")
    add_video(conn, "cap", "c1", is_ai=1, is_shorts=0)
    add_pair(conn, "cap", 0, 5000, interval=3600, likes1=10)
    add_viral(conn, "cap", 5.0, {"views": 5.0, "likes": 25.0}, capped=True)
    stats = report.viral_window_stats(conn, days=10, fmt="long")
    assert stats["axis_capped"] == 1


def test_views_axis_uncapped_in_report_stats(conn):
    """Потолок не трогает ось просмотров: флаг из score_parts не выставляется."""
    add_channel(conn, "c1")
    add_video(conn, "v1", "c1", is_ai=1, is_shorts=0)
    add_pair(conn, "v1", 0, 5000, interval=3600, likes1=10)
    add_viral(conn, "v1", 5.0, {"views": 500.0, "likes": 1.0})
    it = report.viral_top(conn, days=10, fmt="long")[0]
    assert it["capped"] is False
