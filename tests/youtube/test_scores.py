"""Тесты истории скоров упаковки (video_scores, ТЗ-20). Сеть не используется."""

from __future__ import annotations

import json
import time

import pytest

from tuber.platforms.youtube import store as db, report, seo

NOW = int(time.time())


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "scores_test.db")
    db.init_db(c)
    yield c
    c.close()


def add_channel(conn, cid="c1"):
    db.upsert_channel(conn, {"channel_id": cid, "title": "Канал", "first_seen": 1})


def add_video(conn, vid, cid="c1", title="Видео про AI", published_at=None,
              is_shorts=None, duration_seconds=600):
    db.upsert_video(
        conn,
        {
            "video_id": vid,
            "channel_id": cid,
            "title": title,
            "description": "Описание",
            "duration_seconds": duration_seconds,
            "is_shorts": is_shorts,
            "published_at": published_at if published_at is not None else NOW - 86400,
            "first_seen": 1,
        },
    )
    db.save_classification(conn, vid, is_ai=1, topic=None, lang="ru",
                           confidence=0.9, classified_at=NOW)


def add_snapshot(conn, vid, views=1000, likes=100, comments=10, captured_at=None):
    db.insert_snapshot(
        conn, vid, captured_at if captured_at is not None else NOW, "h",
        views, likes, comments,
    )


def score_rows(conn, vid=None):
    if vid is None:
        return conn.execute("SELECT * FROM video_scores ORDER BY computed_at").fetchall()
    return conn.execute(
        "SELECT * FROM video_scores WHERE video_id = ? ORDER BY computed_at", (vid,)
    ).fetchall()


# --- 1. запись строки -------------------------------------------------------


def test_refresh_writes_row(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_snapshot(conn, "v1", views=1000, likes=100, comments=10)
    seo.analyze(conn)

    summary = seo.refresh_scores(conn, now=1000)
    assert summary == {"scored": 1, "skipped": 0, "total": 1}
    rows = score_rows(conn, "v1")
    assert len(rows) == 1
    row = rows[0]
    assert row["computed_at"] == 1000
    assert row["packaging_score"] is not None
    assert row["likes_per_1000"] == pytest.approx(100.0)
    assert row["comments_per_1000"] == pytest.approx(10.0)
    parts = json.loads(row["score_parts"])
    assert isinstance(parts, dict) and parts


# --- 2. идемпотентность -----------------------------------------------------


def test_refresh_idempotent_two_runs(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_snapshot(conn, "v1")
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    second = seo.refresh_scores(conn, now=2000)
    assert second == {"scored": 0, "skipped": 1, "total": 1}
    assert len(score_rows(conn)) == 1


# --- 3. NULL вместо нуля ----------------------------------------------------


def test_missing_data_is_null_not_zero(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_snapshot(conn, "v1", views=1000, likes=None, comments=None)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    row = score_rows(conn, "v1")[0]
    assert row["likes_per_1000"] is None
    assert row["comments_per_1000"] is None
    assert row["outlier_score"] is None  # в канале одно видео — медианы нет
    # Замер один и без пары: дельт и скорости нет — колонки честно NULL.
    for col in ("vpd", "vpd_ratio", "comment_velocity"):
        assert row[col] is None


def test_save_score_missing_fields_are_null(conn):
    add_channel(conn)
    add_video(conn, "v1")
    db.save_score(conn, "v1", 500, packaging_score=42.0)
    row = score_rows(conn, "v1")[0]
    assert row["packaging_score"] == 42.0
    for col in ("outlier_score", "vpd", "likes_per_1000", "score_parts"):
        assert row[col] is None


# --- 4. force пишет новую строку --------------------------------------------


def test_force_writes_new_row(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_snapshot(conn, "v1")
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    seo.refresh_scores(conn, force=True, now=2000)
    rows = score_rows(conn, "v1")
    assert [r["computed_at"] for r in rows] == [1000, 2000]


# --- 5. packaging_score совпадает с score_video -----------------------------


def test_packaging_matches_score_video(conn):
    add_channel(conn)
    add_video(conn, "v1", title="AI: 5 трендов 2026")
    seo.analyze(conn)

    expected = seo.score_video(conn, "v1")["score"]
    seo.refresh_scores(conn, now=1000)
    row = score_rows(conn, "v1")[0]
    assert row["packaging_score"] == expected
    assert json.loads(row["score_parts"]) == seo.score_video(conn, "v1")["parts"]


# --- 6. видео без seo_fields пропускается -----------------------------------


def test_refresh_skips_video_without_seo_fields(conn):
    add_channel(conn)
    add_video(conn, "v1")
    add_video(conn, "v2")
    seo.analyze(conn, limit=1)  # поля только у одного видео
    with_fields = conn.execute("SELECT video_id FROM seo_fields").fetchall()
    assert len(with_fields) == 1

    summary = seo.refresh_scores(conn, now=1000)
    assert summary == {"scored": 1, "skipped": 0, "total": 1}
    assert len(score_rows(conn)) == 1


# --- 7. limit ограничивает число кандидатов ---------------------------------


def test_refresh_limit(conn):
    add_channel(conn)
    for i in range(3):
        add_video(conn, f"v{i}", published_at=NOW - i)
    seo.analyze(conn)

    summary = seo.refresh_scores(conn, limit=2, now=1000)
    assert summary == {"scored": 2, "skipped": 0, "total": 2}
    assert len(score_rows(conn)) == 2


# --- 8. outlier переиспользует report._outlier_map --------------------------


def test_outlier_matches_report_map(conn):
    add_channel(conn)
    for i in range(6):
        add_video(conn, f"v{i}", published_at=NOW - i)
        add_snapshot(conn, f"v{i}", views=1000 + i * 1000)
    seo.analyze(conn)

    omap = report._outlier_map(conn)
    seo.refresh_scores(conn, now=1000)
    row = score_rows(conn, "v0")[0]
    assert row["outlier_score"] == pytest.approx(omap["v0"])


# --- 9. D-05: скорость просмотров (vpd) -------------------------------------


def _two_point_snapshot(conn, vid, delta_views, delta_comments, hours=2.0,
                        views0=1000, comments0=0):
    """Два замера видео через ``hours`` часов: честные дельты и интервал 'ok'."""
    db.insert_snapshot(conn, vid, NOW, "h", views0, likes=10, comments=comments0)
    db.insert_snapshot(
        conn, vid, NOW + int(hours * 3600), "h",
        views0 + delta_views, likes=10, comments=comments0 + delta_comments,
    )


def test_vpd_filled_on_ok_interval(conn):
    add_channel(conn)
    add_video(conn, "v1")
    _two_point_snapshot(conn, "v1", delta_views=1000, delta_comments=0, hours=2.0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    row = score_rows(conn, "v1")[0]
    # 1000 просмотров за 2 часа = 1000 / (2/24) = 12000 в сутки.
    assert row["vpd"] == pytest.approx(12000.0)


def test_vpd_null_on_short_interval(conn):
    add_channel(conn)
    add_video(conn, "v1")
    db.insert_snapshot(conn, "v1", NOW, "h", 1000, likes=10, comments=1)
    # 600 с — дельта честная, но скорость не считается (interval_quality='short').
    db.insert_snapshot(conn, "v1", NOW + 600, "h", 1100, likes=10, comments=2)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    assert score_rows(conn, "v1")[0]["vpd"] is None


# --- 10. D-05: скорость комментариев ----------------------------------------


def test_comment_velocity_twelve_comments_over_six_hours(conn):
    add_channel(conn)
    add_video(conn, "v1")
    _two_point_snapshot(conn, "v1", delta_views=500, delta_comments=12, hours=6.0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    row = score_rows(conn, "v1")[0]
    # 12 комментариев за 6 часов = 12 / (6/24) = 48 в сутки.
    assert row["comment_velocity"] == pytest.approx(48.0)


def test_comment_velocity_negative_delta_is_zero(conn):
    add_channel(conn)
    add_video(conn, "v1")
    # Чистка комментариев: было 20, стало 5 — рост не бывает отрицательным.
    _two_point_snapshot(conn, "v1", delta_views=500, delta_comments=-15,
                        hours=6.0, comments0=20)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    assert score_rows(conn, "v1")[0]["comment_velocity"] == pytest.approx(0.0)


# --- 11. D-05: vpd_ratio к медиане канала -----------------------------------


def test_vpd_ratio_null_when_channel_has_fewer_than_three(conn):
    add_channel(conn)
    for i in range(2):
        add_video(conn, f"v{i}", published_at=NOW - i)
        _two_point_snapshot(conn, f"v{i}", delta_views=1000, delta_comments=0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    for i in range(2):
        row = score_rows(conn, f"v{i}")[0]
        assert row["vpd"] is not None
        assert row["vpd_ratio"] is None


def test_vpd_ratio_uses_channel_median(conn):
    add_channel(conn)
    deltas = {"v0": 1000, "v1": 2000, "v2": 3000}
    for vid, delta in deltas.items():
        add_video(conn, vid)
        _two_point_snapshot(conn, vid, delta_views=delta, delta_comments=0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    # vpd: 12000 / 24000 / 36000; медиана 24000; отношения 0.5 / 1.0 / 1.5.
    assert score_rows(conn, "v0")[0]["vpd_ratio"] == pytest.approx(0.5)
    assert score_rows(conn, "v1")[0]["vpd_ratio"] == pytest.approx(1.0)
    assert score_rows(conn, "v2")[0]["vpd_ratio"] == pytest.approx(1.5)


# --- 12. D-05: аномальные замеры не дают скорости ---------------------------


def test_anomaly_snapshot_gives_no_vpd_or_comment_velocity(conn):
    add_channel(conn)
    add_video(conn, "v1")
    # Просмотры уменьшились: interval_quality='ok', но delta_views < 0 → is_anomaly=1.
    # Даже положительная дельта комментариев не даёт скорости: аномалия → неизвестно.
    _two_point_snapshot(conn, "v1", delta_views=-500, delta_comments=12, hours=2.0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    row = score_rows(conn, "v1")[0]
    assert row["vpd"] is None
    assert row["comment_velocity"] is None
    assert row["vpd_ratio"] is None


def test_channel_median_ignores_anomaly(conn):
    add_channel(conn)
    # Три нормальных видео: vpd 12000 / 24000 / 36000. Медиана по ним = 24000,
    # и минимум MIN_VPD_RATIO_VIDEOS=3 набран только по нормальным замерам.
    for vid, delta in (("v0", 1000), ("v1", 2000), ("v2", 3000)):
        add_video(conn, vid)
        _two_point_snapshot(conn, vid, delta_views=delta, delta_comments=0)
    # Аномальный замер: views_per_day=-6000, is_anomaly=1 — в медиану не входит
    # (если бы входил, медиана была бы 18000, а не 24000).
    add_video(conn, "v3")
    _two_point_snapshot(conn, "v3", delta_views=-500, delta_comments=0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    assert score_rows(conn, "v3")[0]["vpd"] is None
    assert score_rows(conn, "v3")[0]["vpd_ratio"] is None
    # Медиана 24000: отношения 0.5 / 1.0 / 1.5.
    assert score_rows(conn, "v0")[0]["vpd_ratio"] == pytest.approx(0.5)
    assert score_rows(conn, "v1")[0]["vpd_ratio"] == pytest.approx(1.0)
    assert score_rows(conn, "v2")[0]["vpd_ratio"] == pytest.approx(1.5)


def test_vpd_ratio_uses_format_median(conn):
    """ТЗ-33: медиана берётся по видео того же формата, а не по всем сразу."""
    add_channel(conn)
    # 3 шортса: 10 просмотров за 2 часа = 120/сутки (медиана формата 120).
    for i in range(3):
        add_video(conn, f"s{i}", is_shorts=1, duration_seconds=60)
        _two_point_snapshot(conn, f"s{i}", delta_views=10, delta_comments=0)
    # 3 полных: 1 просмотр за 2 часа = 12/сутки (медиана формата 12).
    for i in range(3):
        add_video(conn, f"l{i}", is_shorts=0)
        _two_point_snapshot(conn, f"l{i}", delta_views=1, delta_comments=0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    # Общая медиана канала была бы 66, и отношения вышли бы ~1.82 и ~0.18.
    # С медианой по формату каждый ролик сравнивается со своей базой → 1.0.
    assert score_rows(conn, "s0")[0]["vpd_ratio"] == pytest.approx(1.0)
    assert score_rows(conn, "l0")[0]["vpd_ratio"] == pytest.approx(1.0)


def test_vpd_ratio_format_median_differs_from_channel_median(conn):
    """ТЗ-33: база сравнения — медиана своего формата, а не канала целиком.

    Данные подобраны так, чтобы медиана формата и медиана канала РАЗЛИЧАЛИСЬ —
    иначе старый код (одна медиана канала) давал бы тот же ответ и тест был бы
    тавтологией. Здесь 4 полных (vpd 12) и 3 шортса (vpd 120): медиана канала
    = 12 (большинство полных), медиана шортсов = 120. Старый код дал бы шортсу
    120/12 = 10.0, новый — 120/120 = 1.0.
    """
    add_channel(conn)
    for i in range(4):
        add_video(conn, f"l{i}", is_shorts=0)
        _two_point_snapshot(conn, f"l{i}", delta_views=1, delta_comments=0)
    for i in range(3):
        add_video(conn, f"s{i}", is_shorts=1, duration_seconds=60)
        _two_point_snapshot(conn, f"s{i}", delta_views=10, delta_comments=0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    # Именно форматный результат: 120 / 120 = 1.0, а не канальный 120 / 12.
    assert score_rows(conn, "s0")[0]["vpd_ratio"] == pytest.approx(1.0)
    # Полный ролик со своей медианой 12 тоже даёт 1.0.
    assert score_rows(conn, "l0")[0]["vpd_ratio"] == pytest.approx(1.0)


def test_vpd_ratio_falls_back_to_channel_median(conn):
    """Откат к медиане канала, когда видео формата меньше трёх.

    Покрытие ветки ``entry.get(fmt) or entry.get('all')``: 5 полных (vpd 12) и
    2 шортса (vpd 120); медиана канала = 12, у формата шортсов медианы нет
    (2 < 3), поэтому шортс сравнивается с каналом: 120 / 12 = 10.0. На
    дореформенном коде с единственной медианой канала числа те же, поэтому тест
    не различает версии — это осознанно покрывающий тест (см. отчёт ТЗ-33).
    """
    add_channel(conn)
    for i in range(5):
        add_video(conn, f"l{i}", is_shorts=0)
        _two_point_snapshot(conn, f"l{i}", delta_views=1, delta_comments=0)
    for i in range(2):
        add_video(conn, f"s{i}", is_shorts=1, duration_seconds=60)
        _two_point_snapshot(conn, f"s{i}", delta_views=10, delta_comments=0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    assert score_rows(conn, "s0")[0]["vpd_ratio"] == pytest.approx(10.0)
    assert score_rows(conn, "l0")[0]["vpd_ratio"] == pytest.approx(1.0)


def test_normal_snapshot_still_gives_vpd(conn):
    add_channel(conn)
    add_video(conn, "v1")
    _two_point_snapshot(conn, "v1", delta_views=1000, delta_comments=0, hours=2.0)
    seo.analyze(conn)

    seo.refresh_scores(conn, now=1000)
    # Регрессия: нормальный замер (interval_quality='ok', is_anomaly=0) даёт число.
    assert score_rows(conn, "v1")[0]["vpd"] == pytest.approx(12000.0)
