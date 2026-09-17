"""Тесты SEO-разбора (tuber.seo). Сеть не используется."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from tuber.platforms.youtube import cli, config, store as db, report, seo

NOW = int(time.time())


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "seo_test.db")
    db.init_db(c)
    yield c
    c.close()


def add_video(conn, vid, cid="c1", title="Видео", description="Описание",
              tags=None, published_at=None, duration=600, is_shorts=None,
              topic=None, classify=True):
    data = {
        "video_id": vid,
        "channel_id": cid,
        "title": title,
        "description": description,
        "tags": json.dumps(tags, ensure_ascii=False) if tags is not None else None,
        "duration_seconds": duration,
        "published_at": published_at if published_at is not None else NOW - 86400,
        "first_seen": 1,
    }
    if is_shorts is not None:
        data["is_shorts"] = is_shorts
    db.upsert_video(conn, data)
    if classify:
        db.save_classification(conn, vid, is_ai=1, topic=topic, lang="ru",
                               confidence=0.9)


def add_channel(conn, cid="c1"):
    db.upsert_channel(conn, {"channel_id": cid, "title": "Канал", "first_seen": 1})


def _fields(conn, vid):
    return conn.execute(
        "SELECT * FROM seo_fields WHERE video_id = ?", (vid,)
    ).fetchone()


# --- 1. analyze заполняет поля и счётчики ----------------------------------


def test_analyze_fills_fields_and_counters(conn):
    add_channel(conn)
    add_video(conn, "v1", title="AI: 5 трендов 2026", description="Текст",
              tags=["AI"], topic="модели и релизы")
    add_video(conn, "v2", title="Второе видео", description="Текст")
    summary = seo.analyze(conn)
    assert summary == {"videos": 2, "written": 2, "skipped": 0, "no_title": 0}
    row = _fields(conn, "v1")
    assert row["title_length"] == len("AI: 5 трендов 2026")
    assert row["title_has_number"] == 1
    assert row["title_has_colon"] == 1
    assert row["desc_length"] == len("Текст")
    assert row["tags_count"] == 1
    assert row["published_hour_local"] is None


# --- 2. идемпотентность ----------------------------------------------------


def test_analyze_idempotent(conn):
    add_channel(conn)
    add_video(conn, "v1", title="Повторный прогон", description="Описание " * 20)
    seo.analyze(conn)
    before = dict(_fields(conn, "v1"))
    second = seo.analyze(conn)
    assert second["written"] == 0
    assert second["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM seo_fields").fetchone()[0] == 1
    assert dict(_fields(conn, "v1")) == before


# --- 3. force пересчитывает после изменения заголовка ----------------------


def test_force_recomputes_after_title_change(conn):
    add_channel(conn)
    add_video(conn, "v1", title="Старый заголовок", description="Описание")
    seo.analyze(conn)
    assert _fields(conn, "v1")["title_length"] == len("Старый заголовок")
    conn.execute("UPDATE videos SET title = ? WHERE video_id = 'v1'",
                 ("Новый заголовок с цифрой 7",))
    conn.commit()
    seo.analyze(conn, force=True)
    row = _fields(conn, "v1")
    assert row["title_length"] == len("Новый заголовок с цифрой 7")
    assert row["title_has_number"] == 1


# --- 4. признаки заголовка -------------------------------------------------


def test_title_features_number_question_colon(conn):
    add_channel(conn)
    add_video(conn, "v1", title="Что нового? 10 фактов: разбор")
    seo.analyze(conn)
    row = _fields(conn, "v1")
    assert row["title_has_number"] == 1
    assert row["title_has_question"] == 1
    assert row["title_has_colon"] == 1
    add_video(conn, "v2", title="AI-агенты сегодня")
    seo.analyze(conn)
    assert _fields(conn, "v2")["title_has_colon"] == 1  # дефис тоже двоеточие
    add_video(conn, "v3", title="Просто слова без знаков")
    seo.analyze(conn)
    row3 = _fields(conn, "v3")
    assert row3["title_has_number"] == 0
    assert row3["title_has_question"] == 0
    assert row3["title_has_colon"] == 0


# --- 5. доля заглавных -----------------------------------------------------


def test_title_caps_ratio(conn):
    add_channel(conn)
    add_video(conn, "caps", title="ЭТО ВЕСЬ КАПС")
    add_video(conn, "norm", title="Обычный заголовок видео")
    seo.analyze(conn)
    assert _fields(conn, "caps")["title_caps_ratio"] == 1.0
    assert _fields(conn, "norm")["title_caps_ratio"] < seo.TITLE_CAPS_MAX_RATIO


# --- 6. эмодзи -------------------------------------------------------------


def test_emoji_count(conn):
    add_channel(conn)
    add_video(conn, "v1", title="Новости 🚀 уже здесь 🔥")
    seo.analyze(conn)
    assert _fields(conn, "v1")["title_emoji_count"] == 2
    add_video(conn, "v2", title="Без эмодзи")
    seo.analyze(conn)
    assert _fields(conn, "v2")["title_emoji_count"] == 0


# --- 7. разбор описания ----------------------------------------------------


def test_description_features(conn):
    add_channel(conn)
    desc = ("Ссылка https://a.example и http://b.example\n"
            "#ai #нейросети #технологии\n"
            "0:00 старт\n"
            "Подпишись на канал!")
    add_video(conn, "v1", description=desc)
    seo.analyze(conn)
    row = _fields(conn, "v1")
    assert row["desc_links"] == 2
    assert row["desc_hashtags"] == 3
    assert row["desc_timestamps"] == 1
    assert row["desc_cta"] == 1
    add_video(conn, "v2", description="Просто текст без всего")
    seo.analyze(conn)
    row2 = _fields(conn, "v2")
    assert row2["desc_links"] == 0
    assert row2["desc_hashtags"] == 0
    assert row2["desc_timestamps"] == 0
    assert row2["desc_cta"] == 0


# --- 8. теги ---------------------------------------------------------------


def test_tags_parsing_and_count(conn):
    add_channel(conn)
    add_video(conn, "v1", tags=["AI", "Нейросети", "обзор"])
    add_video(conn, "v2", tags=[])
    add_video(conn, "v3", tags=None)
    seo.analyze(conn)
    row = _fields(conn, "v1")
    assert row["tags_count"] == 3
    assert json.loads(row["tags_common"]) == ["ai", "нейросети", "обзор"]
    assert _fields(conn, "v2")["tags_count"] == 0
    assert _fields(conn, "v3")["tags_count"] == 0


# --- 9. тайминг ------------------------------------------------------------


def test_published_hour_msk_and_local(conn):
    add_channel(conn)
    ts = int(datetime(2026, 9, 10, 12, 30,
                      tzinfo=report.MSK).timestamp())
    add_video(conn, "v1", published_at=ts)
    seo.analyze(conn)
    row = _fields(conn, "v1")
    hour, weekday, _ = report._msk_parts(ts)
    assert row["published_hour_msk"] == hour
    assert row["published_weekday"] == weekday
    # Таймзона канала неизвестна — поле обязано остаться NULL.
    assert row["published_hour_local"] is None


# --- 10. соответствие теме -------------------------------------------------


def test_title_matches_topic(conn):
    add_channel(conn)
    add_video(conn, "match", title="Обзор новых моделей GPT",
              topic="модели и релизы")
    add_video(conn, "nomatch", title="Кулинарный рецепт борща",
              topic="модели и релизы")
    db.upsert_video(conn, {
        "video_id": "noclass", "channel_id": "c1",
        "title": "Видео без классификации", "description": "текст",
        "duration_seconds": 600, "published_at": NOW - 86400, "first_seen": 1,
    })
    seo.analyze(conn)
    assert _fields(conn, "match")["title_matches_topic"] == 1
    assert _fields(conn, "nomatch")["title_matches_topic"] == 0
    assert _fields(conn, "noclass")["title_matches_topic"] is None


# --- 11. оценка упаковки ---------------------------------------------------


def _good_video(conn, vid="good"):
    title = "Обзор новых моделей ИИ: релизы 2026 года сегодня"
    desc = ("Подробный разбор свежих моделей и релизов. " * 5
            + "\n#ai #модели #обзор\n0:00 вступление\nПодпишись!")
    add_video(conn, vid, title=title, description=desc,
              tags=[f"tag{i}" for i in range(10)],
              topic="модели и релизы", duration=600)


def test_score_video_full_beats_poor(conn):
    add_channel(conn)
    _good_video(conn)
    add_video(conn, "bad",
              title="Кулинарный рецепт борща на каждый день недели и праздники "
                    "для всей семьи сегодня вечером обязательно",
              description="", tags=[], topic="модели и релизы", duration=600)
    seo.analyze(conn)
    good = seo.score_video(conn, "good")
    bad = seo.score_video(conn, "bad")
    assert good["score"] > bad["score"]
    assert 0 <= bad["score"] <= 100
    assert set(good["parts"]) == set(seo.SCORE_WEIGHTS)
    assert bad["issues"], "плохое видео обязано иметь объяснённые проблемы"
    for issue in bad["issues"]:
        assert issue["level"] in ("high", "mid", "low")
        assert issue["code"] and issue["text"]


def test_score_has_not_applicable_and_checks(conn):
    add_channel(conn)
    add_video(conn, "v1", title="Тема без классификации", description="")
    result = seo.score_video(conn, "v1")
    assert result["checks"] >= 1
    # нет описания и нет классификации — обе причины названы честно
    assert any("хэштеги" in n for n in result["not_applicable"])
    assert any("тем" in n for n in result["not_applicable"])


# --- 12. недоступное не выдумано -------------------------------------------


def _all_keys(obj):
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(str(k))
            keys.extend(_all_keys(v))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            keys.extend(_all_keys(item))
    return keys


def test_no_invented_metrics_keys(conn):
    add_channel(conn)
    _good_video(conn, "outlier")
    for i in range(4):
        add_video(conn, f"bg{i}", title=f"Фоновое видео {i}",
                  description="Описание фона " * 20, tags=["x"])
    for i, vid in enumerate(["outlier", "bg0", "bg1", "bg2", "bg3"]):
        db.insert_snapshot(conn, vid, NOW - 7200, "d", views=100 + i)
    seo.analyze(conn)
    score = seo.score_video(conn, "outlier")
    res = seo.patterns(conn, min_outlier=3.0)
    banned = ("ctr", "retention", "удержан", "watch_time", "avg_view_duration")
    for payload in (score, res):
        for key in _all_keys(payload):
            low = key.lower()
            assert not any(b in low for b in banned), key
    assert res["not_available"], "список недоступного обязателен"


# --- 13. patterns ----------------------------------------------------------


def _patterns_pool(conn, outlier=True):
    add_channel(conn)
    for i in range(5):
        add_video(conn, f"v{i}", title=f"Видео номер {i}",
                  description="Описание " * 20, tags=["tag"])
        views = 1000 if (outlier and i == 0) else 100
        db.insert_snapshot(conn, f"v{i}", NOW - 3600 * (i + 1), "d", views=views)


def test_patterns_shares_and_sizes(conn):
    _patterns_pool(conn, outlier=True)
    res = seo.patterns(conn, min_outlier=3.0)
    assert res["outliers"]["n"] == 1
    assert res["background"]["n"] == 4
    assert res["title"]["outlier"]["n"] == 1
    assert res["title"]["background"]["n"] == 4
    assert 0.0 <= res["title"]["outlier"]["share_number"] <= 1.0
    assert res["description"]["outlier"]["base_with_description"] == 1
    assert res["tags"]["outlier"]["base_with_tags"] == 1
    assert res["sample"] and res["sample"][0]["outlier_score"] >= 3.0


def test_patterns_without_outliers_is_empty_not_error(conn):
    _patterns_pool(conn, outlier=False)
    res = seo.patterns(conn, min_outlier=3.0)
    assert res["outliers"]["n"] == 0
    assert res["sample"] == []
    assert res["top_words"] == []
    assert res["title"]["outlier"]["n"] == 0
    assert res["background"]["n"] == 5


def test_patterns_skipped_without_median(conn):
    add_channel(conn)
    for i in range(3):  # меньше MIN_CHANNEL_VIDEOS=5, медианы нет
        add_video(conn, f"v{i}")
        db.insert_snapshot(conn, f"v{i}", NOW - 3600, "d", views=100)
    res = seo.patterns(conn, min_outlier=3.0)
    assert res["outliers"]["n"] == 0
    assert res["skipped_no_median"] == 3
    assert res["population"] == 3


# --- метка паттерна и лимит ------------------------------------------------


def test_seo_pattern_labels(conn):
    add_channel(conn)
    add_video(conn, "v1", title="Что? 5 фактов: AI 🚀")
    seo.analyze(conn)
    pattern = _fields(conn, "v1")["seo_pattern"]
    for label in ("вопрос", "цифра", "двоеточие", "эмодзи", "короткий"):
        assert label in pattern
    add_video(conn, "v2", title="Обычный заголовок без особых признаков тут")
    seo.analyze(conn)
    assert _fields(conn, "v2")["seo_pattern"] == "базовый"


def test_analyze_limit_and_no_title(conn):
    add_channel(conn)
    add_video(conn, "v1", title="Первое видео")
    add_video(conn, "v2", title="  ", description="x")
    summary = seo.analyze(conn, limit=2)
    assert summary["videos"] == 2
    assert summary["no_title"] == 1
    assert summary["written"] == 1


# --- 14. CLI ---------------------------------------------------------------


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "tuber.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "tuber.log")
    monkeypatch.setattr(seo, "AUDIT_DIR", tmp_path / "docs" / "audits")
    monkeypatch.setattr(cli.config, "load_env", lambda *a, **k: 0)
    return tmp_path


def test_cli_seo_analyze_returns_zero_and_text(sandbox, capsys):
    c = db.connect(sandbox / "tuber.db")
    db.init_db(c)
    add_channel(c)
    add_video(c, "v1", title="Клип для CLI", description="Описание")
    c.close()
    rc = cli.main(["seo", "--analyze"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip()
    assert "SEO-разбор" in out

    rc = cli.main(["seo", "--analyze", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["command"] == "seo"
    assert payload["mode"] == "analyze"


def test_cli_seo_score_and_patterns(sandbox, capsys):
    c = db.connect(sandbox / "tuber.db")
    db.init_db(c)
    add_channel(c)
    add_video(c, "v1", title="Клип для оценки", description="Описание")
    c.close()
    rc = cli.main(["seo", "--score", "v1"])
    assert rc == 0
    assert "упаковка" in capsys.readouterr().out

    rc = cli.main(["seo", "--patterns"])
    assert rc == 0
    assert "Паттерны упаковки" in capsys.readouterr().out


# ===========================================================================
# Этап 2: бриф, аудит канала, блок SEO в отчёте
# ===========================================================================


def _pool(conn, cid="c1", n_out=5, out_views=1000, bg=20, topic=None,
          descriptions=None, tags=None, durations=None, hours=None):
    """Пул из n_out залетевших (10x медианы) и bg фоновых видео.

    Медиана канала держится на фоне, поэтому залетевшие получают выброс ~10x.
    """
    add_channel(conn, cid)
    for i in range(n_out):
        desc = "Описание " * 20
        if descriptions is not None:
            desc = descriptions[i % len(descriptions)]
        t = tags if tags is not None else ["ai", "agent"]
        dur = 300 if durations is None else durations[i % len(durations)]
        ts = NOW - 86400 * (i + 1)
        if hours is not None:
            h = hours[i % len(hours)]
            ts = int(datetime(2026, 9, 8, h, 0, tzinfo=report.MSK).timestamp())
        add_video(conn, f"out{i}", cid=cid, title=f"Залетевшее видео {i}",
                  description=desc, tags=t, duration=dur, is_shorts=0,
                  published_at=ts, topic=topic)
        db.insert_snapshot(conn, f"out{i}", NOW - 3600, "d", views=out_views)
    for i in range(bg):
        add_video(conn, f"bg{i}", cid=cid, title=f"Фоновое видео {i}",
                  description="текст", tags=["x"], duration=600, is_shorts=0)
        db.insert_snapshot(conn, f"bg{i}", NOW - 3600, "d", views=100)


# --- brief -----------------------------------------------------------------


def test_brief_not_enough_returns_false_without_recommendations(conn):
    _pool(conn, n_out=3, bg=20)
    res = seo.brief(conn)
    assert res["enough"] is False
    assert res["sample_size"] == 3
    assert "нужно минимум" in res["reason"]
    # Никаких выдуманных шаблонов и рекомендаций на трёх видео.
    assert "title" not in res
    assert "templates" not in json.dumps(res, ensure_ascii=False)
    assert "checklist" not in res
    assert res["not_available"]


def test_brief_median_and_percentiles(conn):
    _pool(conn, n_out=5, bg=20, durations=[100, 200, 300, 400, 500])
    res = seo.brief(conn)
    assert res["enough"] is True
    assert res["sample_size"] == 5
    lengths = sorted(len(f"Залетевшее видео {i}") for i in range(5))
    expected_median = (
        lengths[2] if len(lengths) % 2 else
        (lengths[len(lengths) // 2 - 1] + lengths[len(lengths) // 2]) / 2
    )
    assert res["title"]["median_length"] == expected_median
    assert res["duration"]["long"]["median_seconds"] == 300.0
    assert res["duration"]["long"]["p25_seconds"] == 200.0
    assert res["duration"]["long"]["p75_seconds"] == 400.0


def test_brief_top_tags_with_counts(conn):
    _pool(conn, n_out=5, bg=20, tags=["ai", "agent"])
    res = seo.brief(conn)
    top = {x["tag"]: x["count"] for x in res["tags"]["top_tags"]}
    assert top["ai"] == 5
    assert top["agent"] == 5
    assert res["tags"]["base_with_tags"] == 5


def test_brief_templates_use_real_top_words(conn):
    _pool(conn, n_out=5, bg=20)
    res = seo.brief(conn)
    templates = res["title"]["templates"]
    assert 3 <= len(templates) <= 5
    top_words = {w["word"] for w in res["title"]["top_words"]}
    for tmpl in templates:
        assert any(w in tmpl for w in top_words), tmpl


def test_brief_keyword_typo_variants(conn):
    _pool(conn, n_out=5, bg=20)
    res = seo.brief(conn, keyword="claude")
    variants = res["tags"]["typo_variants"]
    assert variants, "перестановки букв ключа обязаны появиться"
    assert all(v != "claude" for v in variants)
    assert "lcaude" in variants
    assert res["tags"]["keyword"] == "claude"


def test_brief_timing_and_hashtags(conn):
    _pool(conn, n_out=5, bg=20, hours=[16, 16, 22, 22, 22],
          descriptions=["#ai #agent #tools #news текст " * 5] * 5)
    res = seo.brief(conn)
    peaks = {p["hour"]: p["count"] for p in res["timing"]["peak_hours"]}
    assert peaks.get(22) == 3
    assert peaks.get(16) == 2
    assert "ai" in res["hashtags"]["recommended"]
    assert "agent" in res["hashtags"]["recommended"]
    assert 3 <= len(res["hashtags"]["recommended"]) <= 5


def test_brief_personal_result_regex():
    assert seo.has_personal_result("Я заработал $5000 за 30 дней") == 1
    assert seo.has_personal_result("Мой путь в ИИ") == 1
    assert seo.has_personal_result("Разбор новых моделей") == 0
    assert seo.has_personal_result(None) == 0


def test_brief_topic_filter(conn):
    _pool(conn, cid="c1", n_out=5, bg=20, topic="агенты и автоматизация")
    res_match = seo.brief(conn, topic="агенты и автоматизация")
    assert res_match["enough"] is True
    res_other = seo.brief(conn, topic="наука и медицина")
    assert res_other["enough"] is False
    assert res_other["sample_size"] == 0


def test_brief_has_no_ctr_or_retention_keys(conn):
    _pool(conn, n_out=5, bg=20)
    res = seo.brief(conn, keyword="agent")
    banned = ("ctr", "retention", "удержан", "watch_time", "avg_view_duration")
    for key in _all_keys(res):
        low = key.lower()
        assert not any(b in low for b in banned), key


# --- audit_channel ---------------------------------------------------------


def test_audit_counts_shares_from_channel_videos(conn):
    add_channel(conn, "c1")
    add_video(conn, "a", cid="c1", title="Короткий заголовок",
              description="Описание с таймкодом 0:00 старт #ai #agent",
              tags=["ai", "x"], duration=600, is_shorts=0)
    add_video(conn, "b", cid="c1", title="Ещё один заголовок подлиннее",
              description="Просто описание", tags=["ai"], duration=600,
              is_shorts=0)
    add_video(conn, "c", cid="c1", title="Без тегов и описания",
              description="", tags=None, duration=600, is_shorts=0)
    add_video(conn, "d", cid="c1", title="Четвёртое видео канала",
              description="", tags=None, duration=600, is_shorts=0)
    res = seo.audit_channel(conn, "c1")
    assert res["videos"] == 4
    assert res["with_tags"] == 2
    assert res["share_with_tags"] == 0.5
    assert res["without_tags"] == 2
    assert res["description"]["share_with_description"] == 0.5
    assert res["description"]["share_timestamps"] == 0.5
    assert res["description"]["share_hashtags"] == 0.5
    assert 0.0 <= res["title_length"]["share_over_60"] <= 1.0
    assert res["enough"] is True


def test_audit_problems_aggregated_by_frequency(conn):
    add_channel(conn, "c1")
    for i in range(4):
        add_video(conn, f"v{i}", cid="c1", title=f"Видео без описания {i}",
                  description="", tags=None, duration=600, is_shorts=0)
    res = seo.audit_channel(conn, "c1")
    counts = {p["code"]: p["count"] for p in res["problems"]}
    assert counts.get("description_no_snippet") == 4
    assert counts.get("tags_missing") == 4
    ordered = [p["count"] for p in res["problems"]]
    assert ordered == sorted(ordered, reverse=True)
    for p in res["problems"]:
        assert p["level"] in ("high", "mid", "low")
        assert p["text"]


def test_audit_best_and_worst_ordering(conn):
    add_channel(conn, "c1")
    _good_video(conn, "good")
    add_video(conn, "bad", cid="c1",
              title="Кулинарный рецепт борща на каждый день недели и праздники "
                    "для всей семьи сегодня вечером обязательно",
              description="", tags=[], duration=600, is_shorts=0)
    res = seo.audit_channel(conn, "c1")
    assert res["best"] and res["worst"]
    assert res["best"][0]["score"] >= res["worst"][0]["score"]
    assert res["best"][0]["video_id"] == "good"
    for row in res["best"] + res["worst"]:
        assert set(row) == {"video_id", "title", "views", "score"}


def test_audit_has_no_ctr_or_retention_keys(conn):
    add_channel(conn, "c1")
    add_video(conn, "v1", cid="c1", title="Видео канала", description="текст",
              tags=["x"])
    res = seo.audit_channel(conn, "c1")
    banned = ("ctr", "retention", "удержан", "watch_time", "avg_view_duration")
    for key in _all_keys(res):
        low = key.lower()
        assert not any(b in low for b in banned), key
    assert res["not_available"]


def test_audit_empty_channel_is_honest(conn):
    res = seo.audit_channel(conn, "nope")
    assert res["videos"] == 0
    assert res["enough"] is False


# --- блок SEO в отчёте -----------------------------------------------------


def test_report_seo_block_with_data(conn):
    _pool(conn, cid="c1", n_out=5, bg=20, hours=[16, 17, 22, 22, 22])
    seo.analyze(conn)
    text = report.build_report(conn)
    assert "SEO-ОФОРМЛЕНИЕ" in text
    assert "Заголовки у залетевших" in text
    assert "Время публикации: пики" in text
    assert "Теги:" in text
    assert "Длительность:" in text
    assert "кликабельность" in text
    # Запрещённых слов из чужих тестов не добавляем.
    assert "ctr" not in text.lower()
    assert "удержан" not in text.lower()


def test_report_seo_block_empty_seo_fields(conn):
    add_channel(conn, "c1")
    add_video(conn, "v1", cid="c1", title="Видео без SEO-разбора",
              description="текст")
    text = report.build_report(conn)
    assert "SEO-разбор не выполнялся" in text
    assert "tuber seo --analyze" in text


# --- косметика вывода patterns ---------------------------------------------


def test_patterns_empty_group_has_no_dangling_unit(conn):
    _pool(conn, cid="c1", n_out=5, bg=20)  # шортсов нет вовсе
    res = seo.patterns(conn, min_outlier=3.0)
    text = seo.format_patterns(res)
    assert "нет данных с" not in text
    assert "шортсы: n=0, медиана нет данных" in text


# --- CLI -------------------------------------------------------------------


def test_cli_seo_brief_and_audit_return_zero(sandbox, capsys):
    c = db.connect(sandbox / "tuber.db")
    db.init_db(c)
    _pool(c, cid="c1", n_out=5, bg=20)
    c.close()

    rc = cli.main(["seo", "--brief"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SEO-бриф" in out
    assert "Образцов: 5" in out

    rc = cli.main(["seo", "--audit", "c1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SEO-аудит канала c1" in out


# ===========================================================================
# ТЗ-21: отличающие слова + срез по каналу
# ===========================================================================


def _titles(titles):
    return [{"title": t} for t in titles]


def test_discriminative_word_only_in_outliers():
    out = _titles(["нейросеть один", "нейросеть два", "нейросеть три",
                   "нейросеть четыре", "нейросеть пять"])
    bg = _titles(["обычное видео"] * 5)
    res = seo.discriminative_words(out, bg)
    assert res and res[0]["word"] == "нейросеть"
    assert res[0]["p_out"] == 1.0
    assert res[0]["p_bg"] == 0.0
    assert abs(res[0]["diff"] - 1.0) < 1e-9
    assert res[0]["z"] > 0
    assert res[0]["n_out"] == 5 and res[0]["n_bg"] == 5


def test_discriminative_word_only_in_background_negative_diff():
    out = _titles(["основное видео"] * 5)
    bg = _titles(["маркетинг один", "маркетинг два", "маркетинг три",
                  "маркетинг четыре", "маркетинг пять"])
    res = seo.discriminative_words(out, bg)
    by_word = {x["word"]: x for x in res}
    assert "маркетинг" in by_word
    assert by_word["маркетинг"]["diff"] < 0
    assert by_word["маркетинг"]["p_bg"] > by_word["маркетинг"]["p_out"]


def test_discriminative_equal_word_is_not_leader():
    out = _titles(["нейросеть залетело"] * 5)
    bg = _titles(["нейросеть фоновое"] * 5)
    res = seo.discriminative_words(out, bg, min_count=1)
    by_word = {x["word"]: x for x in res}
    assert abs(by_word["нейросеть"]["diff"]) < 1e-9
    assert by_word["нейросеть"]["z"] == 0.0
    # при слове только у выбросов нейросеть не должна быть лидером
    assert res[0]["word"] != "нейросеть"
    assert res[0]["diff"] > by_word["нейросеть"]["diff"]


def test_discriminative_min_count_cutoff():
    out = _titles(["уникальноеслово раз", "раз два", "раз три"])
    bg = _titles(["раз четыре", "раз пять"])
    res = seo.discriminative_words(out, bg)  # min_count=5 по умолчанию
    assert "уникальноеслово" not in {x["word"] for x in res}
    res1 = seo.discriminative_words(out, bg, min_count=1)
    assert "уникальноеслово" in {x["word"] for x in res1}


def test_discriminative_sorted_by_diff_desc():
    out = _titles(["альфа бета", "альфа бета", "альфа бета", "альфа", "альфа"])
    bg = _titles(["альфа фон", "фон", "фон", "фон", "фон"])
    res = seo.discriminative_words(out, bg, min_count=3)
    words = [x["word"] for x in res]
    assert words[:2] == ["альфа", "бета"]
    assert res[0]["diff"] > res[1]["diff"] > 0


def test_discriminative_empty_inputs_return_empty():
    assert seo.discriminative_words([], _titles(["видео"])) == []
    assert seo.discriminative_words(_titles(["видео"]), []) == []
    assert seo.discriminative_words([], []) == []


def _channel_pool(conn, cid, prefix, word, n_out=5, bg=20, out_views=1000):
    add_channel(conn, cid)
    for i in range(n_out):
        add_video(conn, f"{prefix}out{i}", cid=cid,
                  title=f"{word} залетело {i}", description="Описание " * 20,
                  tags=["a"], duration=300, is_shorts=0)
        db.insert_snapshot(conn, f"{prefix}out{i}", NOW - 3600, "d",
                           views=out_views)
    for i in range(bg):
        add_video(conn, f"{prefix}bg{i}", cid=cid,
                  title=f"обычное фоновое {i}", description="текст",
                  tags=["x"], duration=600, is_shorts=0)
        db.insert_snapshot(conn, f"{prefix}bg{i}", NOW - 3600, "d", views=100)


def test_patterns_channel_does_not_mix_other_channels(conn):
    _channel_pool(conn, "c1", "a", word="нейросеть")
    _channel_pool(conn, "c2", "b", word="маркетинг")
    res = seo.patterns(conn, min_outlier=3.0, channel="c1")
    assert res["channel"] == "c1"
    assert res["population"] == 25
    assert res["outliers"]["n"] == 5
    assert res["background"]["n"] == 20
    words = {x["word"] for x in res["discriminative_words"]}
    assert "нейросеть" in words
    assert "маркетинг" not in words


def test_patterns_channel_small_sample_prints_low_data(conn):
    _channel_pool(conn, "c1", "a", word="нейросеть", n_out=3, bg=20)
    res = seo.patterns(conn, min_outlier=3.0, channel="c1")
    assert res["outliers"]["n"] == 3
    text = seo.format_patterns(res)
    assert "Мало данных (n=3)" in text
    assert "Отличающие слова канала" not in text


def test_patterns_channel_big_sample_prints_timing_and_words(conn):
    _channel_pool(conn, "c1", "a", word="нейросеть", n_out=6, bg=20)
    res = seo.patterns(conn, min_outlier=3.0, channel="c1")
    text = seo.format_patterns(res)
    assert "Срез по каналу c1" in text
    assert "часы:" in text and "дни:" in text
    assert "формат: шортс=" in text
    assert "Длительности выбросов канала" in text
    assert "нейросеть —" in text
    assert "n=6/20" in text


def test_cli_seo_patterns_channel_flag(sandbox, capsys):
    c = db.connect(sandbox / "tuber.db")
    db.init_db(c)
    _channel_pool(c, "c1", "a", word="нейросеть", n_out=6, bg=20)
    _channel_pool(c, "c2", "b", word="маркетинг", n_out=6, bg=20)
    c.close()
    rc = cli.main(["seo", "--patterns", "--channel", "c1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Срез по каналу c1" in out
    assert "нейросеть" in out
    assert "маркетинг" not in out


# ===========================================================================
# ТЗ-22: пакетный аудит каналов и бриф по каналу
# ===========================================================================


def _two_channels_analyzed(conn):
    """Два канала с разным числом выбросов; seo_fields заполнены."""
    _channel_pool(conn, "c1", "a", word="нейросеть", n_out=5, bg=20)
    _channel_pool(conn, "c2", "b", word="маркетинг", n_out=2, bg=20)
    seo.analyze(conn)


def test_audit_channels_multiple_ids_each_get_report(conn, tmp_path):
    _two_channels_analyzed(conn)
    out = tmp_path / "audits"
    batch = seo.audit_channels(conn, ["c1", "c2"], out_dir=out)
    assert [r["channel_id"] for r in batch["reports"]] == ["c1", "c2"]
    assert batch["skipped"] == []
    for r in batch["reports"]:
        assert r["videos_in_analysis"] > 0
        assert r["avg_score"] is not None
        assert len(r["top_problems"]) <= 3
        assert set(r) == {
            "channel_id", "channel_title", "videos_in_analysis",
            "avg_score", "top_problems", "report_path", "report_size",
        }


def test_audit_report_file_written_and_nonempty(conn, tmp_path):
    _two_channels_analyzed(conn)
    out = tmp_path / "audits"
    seo.audit_channels(conn, ["c1"], out_dir=out)
    path = out / "c1.md"
    assert path.exists()
    assert path.stat().st_size > 0
    text = path.read_text(encoding="utf-8")
    assert "SEO-аудит канала" in text
    assert "c1" in text


def test_audit_top_sorts_channels_by_outlier_count(conn):
    _two_channels_analyzed(conn)
    # у c1 пять выбросов, у c2 два — c1 идёт первым
    assert seo.top_channels_by_outliers(conn, 1, min_outlier=3.0) == ["c1"]
    assert seo.top_channels_by_outliers(conn, 2, min_outlier=3.0) == ["c1", "c2"]


def test_audit_channels_skips_channel_without_seo_fields(conn, tmp_path):
    _two_channels_analyzed(conn)
    # c2 добавляется уже после analyze: у него нет ни одной строки seo_fields.
    add_channel(conn, "c3")
    add_video(conn, "c3v", cid="c3", title="Без разбора", description="текст")
    out = tmp_path / "audits"
    batch = seo.audit_channels(conn, ["c1", "c3"], out_dir=out)
    assert batch["skipped"] == ["c3"]
    assert [r["channel_id"] for r in batch["reports"]] == ["c1"]
    assert not (out / "c3.md").exists()
    text = seo.format_audit_batch(batch)
    assert "пропущены" in text and "c3" in text


def test_brief_channel_does_not_mix_channels(conn):
    _two_channels_analyzed(conn)
    res = seo.brief(conn, channel_id="c1")
    assert res["filters"]["channel_id"] == "c1"
    assert res["sample_size"] == 5
    words = {w["word"] for w in res["title"]["top_words"]}
    assert "нейросеть" in words
    assert "маркетинг" not in words


def test_brief_channel_low_data_warning(conn):
    _channel_pool(conn, "c1", "a", word="нейросеть", n_out=4, bg=5)
    seo.analyze(conn)
    res = seo.brief(conn, channel_id="c1")
    assert res["channel_data_size"] == 9
    assert res["low_data"] is True
    text = seo.format_brief(res)
    assert "мало данных (n=9), выводы слабые" in text
    # бриф всё равно печатается: образцы названы, даже если их мало
    assert "Образцов найдено 4" in text


def _two_channels_same_topic(conn, n_out=5, bg=20, topic="агенты и автоматизация",
                            topic2=None):
    """c1 и c2: разные каналы, тема по умолчанию совпадает."""
    if topic2 is None:
        topic2 = topic
    for cid, prefix, t in (("c1", "a", topic), ("c2", "b", topic2)):
        add_channel(conn, cid)
        for i in range(n_out):
            add_video(conn, f"{prefix}out{i}", cid=cid,
                      title=f"Видео {i}", description="Описание " * 20,
                      tags=["ai"], duration=300, is_shorts=0,
                      topic=t)
            db.insert_snapshot(conn, f"{prefix}out{i}", NOW - 3600, "d",
                               views=1000)
        for i in range(bg):
            add_video(conn, f"{prefix}bg{i}", cid=cid,
                      title=f"Фоновое видео {i}", description="текст",
                      tags=["x"], duration=600, is_shorts=0)
            db.insert_snapshot(conn, f"{prefix}bg{i}", NOW - 3600, "d",
                               views=100)


def test_brief_channel_scope_counts_own_and_topic(conn):
    _two_channels_same_topic(conn)
    res = seo.brief(conn, channel_id="c1")
    # канал c1 даёт 5 своих, канал c2 прибавляет ещё 5 по той же теме
    assert res["sample_own_channel"] == 5
    assert res["sample_by_topic"] == 5
    assert res["sample_size"] == 10
    assert res["sample_own_channel"] + res["sample_by_topic"] == res["sample_size"]


def test_brief_scope_fields_none_without_channel(conn):
    _pool(conn, n_out=5, bg=20)
    res = seo.brief(conn)
    assert res["sample_size"] == 5
    assert res["sample_own_channel"] is None
    assert res["sample_by_topic"] is None
    # без канала строка остаётся прежней, одной цифрой
    assert "Образцов: 5." in seo.format_brief(res)


def test_brief_format_channel_scope_line(conn):
    _two_channels_same_topic(conn)
    res = seo.brief(conn, channel_id="c1")
    text = seo.format_brief(res)
    assert "Образцов: 10 (свой канал 5 + 5 по темам канала)." in text


def test_brief_channel_scope_own_only_without_topic_match(conn):
    # у c2 чужая тема: по темам c1 ничего не добавляется
    _two_channels_same_topic(conn, topic2="наука и медицина")
    res = seo.brief(conn, channel_id="c1")
    assert res["sample_own_channel"] == 5
    assert res["sample_by_topic"] == 0
    assert res["sample_size"] == 5


def test_cli_seo_audit_multiple_ids_and_top(sandbox, capsys):
    c = db.connect(sandbox / "tuber.db")
    db.init_db(c)
    _channel_pool(c, "c1", "a", word="нейросеть", n_out=5, bg=20)
    _channel_pool(c, "c2", "b", word="маркетинг", n_out=2, bg=20)
    seo.analyze(c)
    c.close()

    rc = cli.main(["seo", "--audit", "c1,c2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SEO-аудит каналов" in out
    assert (sandbox / "docs" / "audits" / "c1.md").stat().st_size > 0

    rc = cli.main(["seo", "--audit-top", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Канал (c1)" in out  # у c1 больше выбросов, чем у c2


def test_cli_seo_brief_channel_flag(sandbox, capsys):
    c = db.connect(sandbox / "tuber.db")
    db.init_db(c)
    _channel_pool(c, "c1", "a", word="нейросеть", n_out=6, bg=20)
    _channel_pool(c, "c2", "b", word="маркетинг", n_out=6, bg=20)
    seo.analyze(c)
    c.close()
    rc = cli.main(["seo", "--brief", "--channel", "c1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "нейросеть" in out
    assert "маркетинг" not in out
