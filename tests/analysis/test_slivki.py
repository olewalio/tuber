"""ТЗ-45 (контуры 2-3): рейтинг «сливки» — outlier, breakout, честная история.

Синтетическая база, без сети. Проверяются ровно правила ТЗ:

* относительный выброс = метрика / медиана автора НА ТОМ ЖЕ возрасте поста;
* breakout = взвешенная сумма робастных z внутри размерного класса;
* неполная история (``g7`` требует 7 дней) — ось НЕ подставляется нулём, веса
  пересчитываются, в выдаче видно «история N дн из 7»;
* пороги отбора (1к–500к, ≥3 поста/14д, g7 по классу подписчиков, ≥1 выброс,
  blocklist);
* метки «восходящий» и «до самой сути».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tuber.analysis import slivki as S
from tuber.core import db, schema

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def con(tmp_path):
    conn = db.connect(str(tmp_path / "slivki.db"))
    schema.init_schema(conn)
    yield conn
    conn.close()


def _source(con, sid, handle, subs, platform="telegram"):
    con.execute(
        "INSERT INTO source(id, platform, handle, status, subs) VALUES (?,?,?,'active',?)",
        (sid, platform, handle, subs))


def _content(con, cid, platform, sid, published, external_id=None):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at, url)"
        " VALUES (?,?,?,?,?,?)",
        (cid, platform, sid, external_id or f"e{cid}", published,
         f"https://example.invalid/{cid}"))


def _snap(con, cid, platform, captured, views=None, likes=None):
    con.execute(
        "INSERT INTO metric_snapshot(content_id, platform, captured_at, views, likes)"
        " VALUES (?,?,?,?,?)",
        (cid, platform, captured, views, likes))


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Классы и корзины
# ---------------------------------------------------------------------------

def test_size_class_boundaries():
    assert S.size_class(999) == "<5k"
    assert S.size_class(5000) == "5–25k"
    assert S.size_class(25000) == "25–100k"
    assert S.size_class(100000) == "100–500k"
    assert S.size_class(500000) == ">500k"
    assert S.size_class(None) is None
    assert S.size_class(0) is None


def test_age_bucket_boundaries():
    assert S.age_bucket(0.5) == "<1ч"
    assert S.age_bucket(2) == "1–6ч"
    assert S.age_bucket(10) == "6–24ч"
    assert S.age_bucket(30) == "24–72ч"
    assert S.age_bucket(100) == ">72ч"
    assert S.age_bucket(None) == ">72ч"


def test_axis_class_separates_platforms():
    tg = {"platform": "telegram", "subs": 1000}
    x = {"platform": "x", "subs": 1000}
    assert S.axis_class(tg) != S.axis_class(x)
    assert S.axis_class({"platform": "x", "subs": None}) is None


# ---------------------------------------------------------------------------
# Робастная статистика и веса
# ---------------------------------------------------------------------------

def test_robust_z_median_mad():
    values = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0, 5: 100.0}
    z = S.robust_z(values, class_of=lambda k: "c")
    assert z[5] > 0
    assert abs(z[5]) > abs(z[4])
    assert z[3] == pytest.approx((3 - 3) / (1.0 * S.MAD_TO_SIGMA))


def test_robust_z_zero_mad_is_zero_not_explosion():
    """Больше половины класса совпадает (MAD=0) → z=0, а не сотни."""
    values = {1: 0.0, 2: 0.0, 3: 0.0, 4: 1.0}
    z = S.robust_z(values, class_of=lambda k: "c")
    assert all(v == 0.0 for v in z.values())


def test_reweight_renormalizes_over_available_axes():
    weights = S.reweight({"median_likes_24h": 5.0, "trusted_indegree_30d": 0})
    assert set(weights) == {"median_likes_24h", "trusted_indegree_30d"}
    assert sum(weights.values()) == pytest.approx(1.0)
    # Без g7 вес 0,45 распределяется по остальным осям.
    assert weights["median_likes_24h"] == pytest.approx(0.25 / 0.45)
    assert weights["trusted_indegree_30d"] == pytest.approx(0.20 / 0.45)


def test_reweight_empty_when_no_axes():
    assert S.reweight({}) == {}


# ---------------------------------------------------------------------------
# Относительный выброс
# ---------------------------------------------------------------------------

def test_outlier_uses_same_age_bucket_median(con):
    """Медиана автора — внутри корзины возраста, а не «как попался снимок»."""
    _source(con, 1, "author", 10_000, "telegram")
    # 3 зрелых поста (возраст 30 ч) со 100 просмотров и 1 свежий (1 ч) с 100.
    for i, cid in enumerate((1, 2, 3)):
        _content(con, cid, "telegram", 1, _iso(NOW - timedelta(days=3, minutes=i)))
        _snap(con, cid, "telegram", _iso(NOW - timedelta(days=3, hours=-30, minutes=i)),
              views=100)
    _content(con, 4, "telegram", 1, _iso(NOW - timedelta(hours=1)))
    _snap(con, 4, "telegram", _iso(NOW), views=100)
    metrics = S.load_latest_metrics(con)
    medians = S.author_age_medians(metrics)
    # У зрелых 3 постов одна корзина 24–72ч; свежий попадает в <1ч и медианы
    # (там 1 пост < MIN_AUTHOR_POSTS_PER_BUCKET) не получает.
    assert any(bucket == "24–72ч" for _sid, bucket in medians)
    outliers = S.compute_outliers(metrics, medians)
    assert 4 not in outliers  # свежий пост не сравнивается с зрелым


def test_outlier_flags_viral_post(con):
    _source(con, 1, "author", 10_000, "telegram")
    for cid, views in ((1, 100), (2, 100), (3, 100), (4, 1000)):
        published = NOW - timedelta(days=2, minutes=cid)
        _content(con, cid, "telegram", 1, _iso(published))
        _snap(con, cid, "telegram", _iso(published + timedelta(hours=30)), views=views)
    metrics = S.load_latest_metrics(con)
    outliers = S.compute_outliers(metrics, S.author_age_medians(metrics))
    assert outliers[4] == pytest.approx(10.0)


def test_outlier_all_three_platforms(con):
    for sid, plat in ((1, "youtube"), (2, "telegram"), (3, "x")):
        _source(con, sid, f"h{sid}", 10_000, plat)
        for cid in range(sid * 10, sid * 10 + 4):
            published = NOW - timedelta(days=2, minutes=cid)
            _content(con, cid, plat, sid, _iso(published))
            metric = 1000 if cid == sid * 10 + 3 else 100
            if plat == "x":
                _snap(con, cid, plat, _iso(published + timedelta(hours=30)), likes=metric)
            else:
                _snap(con, cid, plat, _iso(published + timedelta(hours=30)), views=metric)
    metrics = S.load_latest_metrics(con)
    outliers = S.compute_outliers(metrics, S.author_age_medians(metrics))
    by_platform = {}
    for cid, o in outliers.items():
        by_platform.setdefault(metrics[cid]["platform"], []).append(o)
    for plat in ("youtube", "telegram", "x"):
        assert by_platform.get(plat), f"нет выброса для {plat}"
        assert max(by_platform[plat]) >= S.OUTLIER_THRESHOLD


# ---------------------------------------------------------------------------
# История и g7
# ---------------------------------------------------------------------------

def test_g7_from_series_requires_seven_days():
    today = "2026-09-21"
    rows = [{"day": "2026-09-21", "subs": 1100}]
    assert S.g7_from_series(rows, today) is None  # нет опорной точки
    rows = [{"day": "2026-09-14", "subs": 1000}, {"day": "2026-09-21", "subs": 1100}]
    assert S.g7_from_series(rows, today) == pytest.approx(0.10)


def test_history_depth_days():
    assert S.history_depth_days([], "2026-09-21") == 0
    assert S.history_depth_days([{"day": "2026-09-21"}], "2026-09-21") == 0
    assert S.history_depth_days(
        [{"day": "2026-09-14"}, {"day": "2026-09-21"}], "2026-09-21") == 7


def test_capture_writes_snapshot_and_is_idempotent(con):
    _source(con, 1, "author", 1000, "telegram")
    for cid in range(1, 5):
        published = NOW - timedelta(hours=20, minutes=cid)
        _content(con, cid, "telegram", 1, _iso(published))
        _snap(con, cid, "telegram", _iso(published + timedelta(hours=1)),
              views=100 if cid < 4 else 500)
    stats = S.capture(con, now=NOW)
    assert stats["rows"] == 1
    row = con.execute("SELECT * FROM source_metric_history WHERE source_id=1").fetchone()
    assert row["subs"] == 1000
    assert row["posts_7d"] == 4
    assert row["viral_posts_14d"] == 1
    assert row["median_likes_24h"] == pytest.approx(100)
    # Повторный прогон не дублирует строку дня.
    S.capture(con, now=NOW)
    assert con.execute("SELECT COUNT(*) FROM source_metric_history").fetchone()[0] == 1


def test_backfill_uses_existing_evidence(con):
    con.execute(
        "INSERT INTO source(id, platform, handle, status, subs, subs_at, meta_json)"
        " VALUES (1,'telegram','a','active',1000,'2026-09-14 00:00:00',"
        " '{\"followers_history\":[{\"at\":\"2026-09-14 00:00:00\",\"subs\":1000}]}')")
    stats = S.backfill_from_evidence(con, now=NOW)
    assert stats["written"] == 1
    row = con.execute("SELECT day, subs FROM source_metric_history WHERE source_id=1"
                      ).fetchone()
    assert row["day"] == "2026-09-14" and row["subs"] == 1000


# ---------------------------------------------------------------------------
# Breakout и честность к неполной истории
# ---------------------------------------------------------------------------

def _two_class_scenario(con, *, history_days=0):
    """Два класса Telegram: у каждого 4 автора с разбросом (MAD > 0)."""
    small = {1: 100, 2: 1000, 3: 200, 4: 400}
    big = {11: 10_000, 12: 100_000, 13: 20_000, 14: 40_000}
    for sid in small:
        _source(con, sid, f"small{sid}", 2000, "telegram")
    for sid in big:
        _source(con, sid, f"big{sid}", 50_000, "telegram")
    for sid, base in {**small, **big}.items():
        for j in range(4):
            cid = sid * 100 + j
            published = NOW - timedelta(hours=20, minutes=j)
            _content(con, cid, "telegram", sid, _iso(published))
            views = base
            if sid == 2 and j == 3:
                views = base * 3  # виральный пост: ×3 к своей медиане
            _snap(con, cid, "telegram", _iso(published + timedelta(hours=2)), views=views)
    if history_days:
        for sid, base in {**small, **big}.items():
            con.execute(
                "INSERT INTO source_metric_history(source_id, day, subs, median_likes_24h,"
                " trusted_indegree_30d) VALUES (?,?,?,?,0)",
                (sid, (NOW - timedelta(days=history_days)).strftime("%Y-%m-%d"),
                 con.execute("SELECT subs FROM source WHERE id=?", (sid,)).fetchone()[0],
                 base))
    con.commit()


def test_breakout_excludes_g7_without_history(con):
    _two_class_scenario(con, history_days=0)
    data = S.build(con, now=NOW)
    assert data["history_max_depth"] < 7
    for rec in data["candidates_growth_unknown"] + data["candidates_verified"]:
        assert "g7" not in rec["axes_used"]
        assert rec["g7"] is None
    # Веса пересчитаны: сумма 1.0 по доступным осям.
    for rec in data["candidates_growth_unknown"]:
        assert sum(rec["weights"].values()) == pytest.approx(1.0)


def test_breakout_within_size_class_favors_small(con):
    """Без классов гигант всегда впереди; классы это чинят."""
    _two_class_scenario(con, history_days=0)
    data = S.build(con, now=NOW)
    recs = {r["handle"]: r for r in
            data["candidates_growth_unknown"] + data["candidates_verified"]}
    # small2 втрое обогнал свой класс → большой положительный breakout.
    assert "small2" in recs
    assert recs["small2"]["breakout"] > 0
    # Кросс-классовое сравнение не делается: z считается внутри класса.
    assert recs["small2"]["size_class"] == "<5k"
    assert S.axis_class({"platform": "telegram", "subs": 50_000}) == "telegram|25–100k"


def test_selection_gate_requires_outlier_and_posts(con):
    _source(con, 1, "quiet", 10_000, "telegram")
    # Один пост — меньше порога ≥3 поста за 14 дней.
    _content(con, 1, "telegram", 1, _iso(NOW - timedelta(days=1)))
    _snap(con, 1, "telegram", _iso(NOW), views=100)
    con.commit()
    data = S.build(con, now=NOW)
    assert not data["candidates_verified"]
    assert not data["candidates_growth_unknown"]


def test_selection_gate_respects_blocklist(con):
    _two_class_scenario(con, history_days=7)
    con.execute("INSERT INTO blocklist(platform, handle) VALUES ('telegram','small2')")
    con.commit()
    data = S.build(con, now=NOW)
    handles = {r["handle"] for r in
               data["candidates_verified"] + data["candidates_growth_unknown"]}
    assert "small2" not in handles


# ---------------------------------------------------------------------------
# Метки
# ---------------------------------------------------------------------------

def test_to_the_point_label(con):
    _two_class_scenario(con, history_days=0)
    con.execute(
        "INSERT INTO first_mover(source_id, platform, handle, trusted_indegree_30d,"
        " lead_time_median) VALUES (1,'telegram','small1',2,-45.0)")
    con.commit()
    data = S.build(con, now=NOW)
    handles = {r["handle"] for r in data["to_the_point"]}
    assert "small1" in handles


def test_rising_unavailable_without_history(con):
    _two_class_scenario(con, history_days=0)
    data = S.build(con, now=NOW)
    assert data["rising"] == []
    assert data["rising_days_available"] < S.RISING_WINDOW_DAYS


def test_rising_requires_two_of_three_days(con):
    """Три дня истории breakout и ≥2 дня выше +2σ класса — метка ставится.

    Четыре автора класса <5k, у одного медиана втрое выше: MAD класса > 0,
    порог считается, и один и тот же автор держится выше порога все 3 дня.
    """
    for sid in (1, 2, 3, 4):
        _source(con, sid, f"rise{sid}", 2000, "telegram")
    for day_offset in range(0, 3):
        day = (NOW - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        for sid, metric in ((1, 100.0), (2, 3000.0), (3, 200.0), (4, 300.0)):
            value = metric * (1.0 + 0.01 * day_offset)
            con.execute(
                "INSERT INTO source_metric_history(source_id, day, subs,"
                " median_likes_24h, trusted_indegree_30d, viral_posts_14d)"
                " VALUES (?,?,?,?,0,0)", (sid, day, 2000, value))
    con.commit()
    sources = S.load_sources(con)
    series = S.history_series(con)
    today = NOW.strftime("%Y-%m-%d")
    depth = {sid: S.history_depth_days(rows, today) for sid, rows in series.items()}
    rising, _thresholds, available = S._rising(con, NOW, sources, series, depth,
                                               None, today)
    assert available == S.RISING_WINDOW_DAYS
    assert 2 in rising, "лидер класса должен получать метку «восходящий»"


def test_verified_growth_selection(con):
    """g7 ≥ +10 % (<5k) подтверждает рост и попадает в основной блок."""
    _source(con, 1, "grower", 2000, "telegram")
    for cid in range(1, 5):
        published = NOW - timedelta(hours=20, minutes=cid)
        _content(con, cid, "telegram", 1, _iso(published))
        _snap(con, cid, "telegram", _iso(published + timedelta(hours=2)),
              views=100 if cid < 4 else 1000)
    today = NOW.strftime("%Y-%m-%d")
    ref = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
    con.execute(
        "INSERT INTO source_metric_history(source_id, day, subs) VALUES (1,?,1000)"
        " ON CONFLICT(source_id, day) DO UPDATE SET subs=1000", (ref,))
    con.execute(
        "INSERT INTO source_metric_history(source_id, day, subs, median_likes_24h)"
        " VALUES (1,?,2000,100)", (today,))
    con.commit()
    data = S.build(con, now=NOW)
    assert [r["handle"] for r in data["candidates_verified"]] == ["grower"]
    rec = data["candidates_verified"][0]
    assert rec["g7"] == pytest.approx(1.0)  # 1000 → 2000 = +100 %
    assert "g7" in rec["axes_used"]

