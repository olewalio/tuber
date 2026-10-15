"""Тесты объединённой выдачи ``tuber report`` (ТЗ-5 §2).

Собирается синтетическое ядро (схема + несколько строк на каждую платформу) и
проверяются ключевые правила секций: порог показов YouTube (50k/10k ru), отсев
ретвитов в X, внутриканальное ранжирование Telegram, поиск сквозного сюжета и
честные оговорки в подвале. Сеть и LLM не используются.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tuber.analysis import report
from tuber.core import db, schema, urls


def _iso(days_ago: float = 1.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def core(tmp_path):
    path = str(tmp_path / "core.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    with db.write_tx(conn):
        conn.execute("INSERT INTO source(id, platform, external_id, handle, title, status)"
                     " VALUES (1,'youtube','UC1','ytchan','Канал YouTube',NULL),"
                     "        (2,'x','x1','xa','X аккаунт',NULL),"
                     "        (3,'telegram','ch1','ch1','Канал TG 1','active'),"
                     "        (4,'telegram','ch2','ch2','Канал TG 2','active')")
        # YouTube: ru 20000 (порог 10k — проходит), en 20000 (порог 50k — нет),
        # en 60000 (проходит).
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "lang,title,url,kind) VALUES"
            " (100,'youtube',1,'vid_ru',?,'ru','Русское видео',"
            "  'https://www.youtube.com/watch?v=vid_ru','video'),"
            " (101,'youtube',1,'vid_en_small',?,'en','Small EN',"
            "  'https://www.youtube.com/watch?v=vid_en_small','video'),"
            " (102,'youtube',1,'vid_en_big',?,'en','Big EN',"
            "  'https://www.youtube.com/watch?v=vid_en_big','video')",
            (_iso(1), _iso(1), _iso(1)))
        for cid, ext, views, vpd in ((100, "vid_ru", 20000, 900),
                                     (101, "vid_en_small", 20000, 800),
                                     (102, "vid_en_big", 60000, 700)):
            conn.execute(
                "INSERT INTO metric_snapshot(content_id,captured_at,interval_quality,"
                "views,views_per_day,platform,external_id)"
                " VALUES (?,?,'ok',?,?,'youtube',?)",
                (cid, _iso(0.5), views, vpd, ext))
        # X: обычный пост с 100 лайками и ретвит с 9999 лайками (должен отсеяться).
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "text,author_handle,url,is_repost) VALUES"
            " (200,'x',2,'tw1',?,'обычный пост','xa',"
            "  'https://x.com/xa/status/tw1',0),"
            " (201,'x',2,'tw2',?,'ЭТО_РЕТВИТ_МАРКЕР','xa',"
            "  'https://x.com/xa/status/tw2',1)",
            (_iso(1), _iso(1)))
        conn.execute("INSERT INTO content_latest(content_id,likes,platform,external_id)"
                     " VALUES (200,100,'x','tw1'),(201,9999,'x','tw2')")
        # Telegram: два канала, значимость ранжируется внутри канала.
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,text,url)"
            " VALUES (300,'telegram',3,'ch1/10',?,'пост ch1 слабый','https://t.me/ch1/10'),"
            " (301,'telegram',3,'ch1/11',?,'пост ch1 сильный','https://t.me/ch1/11'),"
            " (302,'telegram',4,'ch2/20',?,'пост ch2','https://t.me/ch2/20')",
            (_iso(1), _iso(1), _iso(1)))
        conn.execute(
            "INSERT INTO score(content_id,computed_at,significance,platform,axes_json)"
            " VALUES (300,?,1.0,'telegram','{\"eng_channel\": 1.5}'),"
            " (301,?,9.0,'telegram','{\"eng_channel\": 9.5}'),"
            " (302,?,3.0,'telegram','{}')",
            (_iso(0.5), _iso(0.5), _iso(0.5)))
        # Сквозной сюжет: пост X и пост Telegram в одном story.
        conn.execute("INSERT INTO story(id,platform,topic) VALUES (7,'x','тема')")
        conn.execute("INSERT INTO story_member(story_id,content_id,handle)"
                     " VALUES (7,200,'xa'),(7,300,'ch1')")
    yield conn
    conn.close()


def test_youtube_threshold_ru_and_foreign(core):
    lines, stats = report.youtube_section(core, _iso(10))
    body = "\n".join(lines)
    assert "Русское видео" in body            # 20k ru >= 10k
    assert "Big EN" in body                   # 60k en >= 50k
    assert "Small EN" not in body             # 20k en < 50k
    assert stats["above"] == 2


def test_x_excludes_retweets(core):
    lines, _ = report.x_section(core, (datetime.now(timezone.utc)
                                       - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"))
    body = "\n".join(lines)
    assert "обычный пост" in body
    assert "ЭТО_РЕТВИТ_МАРКЕР" not in body


def test_telegram_ranks_within_channel(core):
    lines, _ = report.telegram_section(core, (datetime.now(timezone.utc)
                                              - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"))
    body = "\n".join(lines)
    # Сильный пост канала ch1 идёт раньше слабого (внутриканальная сортировка).
    assert body.index("пост ch1 сильный") < body.index("пост ch1 слабый")
    assert "significance 9.000" in body


def test_cross_story_found(core):
    lines, stats = report.cross_story_section(core)
    body = "\n".join(lines)
    assert stats["stories"] == 1
    assert "платформы: telegram, x" in body
    assert "https://x.com/xa/status/tw1" in body
    assert "https://t.me/ch1/10" in body


@pytest.mark.parametrize("platform,ext,handle,url", [
    ("youtube", "abc", "ch", "https://www.youtube.com/watch?v=abc"),
    ("x", "123", "h", "https://x.com/h/status/123"),
    ("telegram", "ch/45", None, "https://t.me/ch/45"),
])
def test_content_url_built_from_external_id(platform, ext, handle, url):
    assert urls.content_url(platform, ext, handle) == url


def test_build_has_all_sections_and_caveats(core):
    text = report.build(core, days=10, db_path=":memory:")
    for header in ("1. YouTube", "2. X", "3. Telegram", "4. Сквозной сюжет",
                   "ПОДВАЛ"):
        assert header in text
    assert "content.url пуст" in text
