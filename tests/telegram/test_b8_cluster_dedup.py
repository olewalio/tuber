"""D-38/D-39: кластер почти-дублей simhash для Xconf и dup_ratio.

Проверяем: единый simhash (tuber.core.textsim), кластеризацию почти-дублей,
несклейку пустых и непохожих, ловлю почти-дублей кластером (там, где точный
text_hash бессилен) и интеграцию в скоринг Telegram на временной БД.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from tuber.platforms.telegram import dedup
from tuber.platforms.telegram import scoring as S
from tuber.core import textsim


def test_threshold_frozen_and_documented():
    """Порог закреплён константой; калибровка описана в докстроке модуля."""
    assert dedup.SIMHASH_CLUSTER_THRESHOLD == 6
    text = (dedup.__doc__ or "") + (dedup.cluster_by_text.__doc__ or "")
    assert "калибровк" in text.lower()
    # simhash — единый источник, не вторая реализация
    assert dedup.simhash64 is textsim.simhash64
    assert dedup.hamming is textsim.hamming


def test_cluster_merges_near_duplicates_not_unrelated():
    items = [
        (1, "Биткоин пробил отметку сто тысяч долларов сегодня утром"),
        (2, "Биткоин пробил отметку сто тысяч долларов сегодня утром!"),
        (3, "Совсем другой текст про кошек, собак и зоомагазин на углу"),
    ]
    cluster_of, clusters = dedup.cluster_by_text(items)
    assert cluster_of[1] == cluster_of[2], "почти-дубли не склеены"
    assert cluster_of[3] != cluster_of[1], "непохожие склеены (ложная склейка)"
    assert len(clusters) == 2


def test_empty_texts_are_singletons():
    cluster_of, _ = dedup.cluster_by_text([(1, ""), (2, None), (3, "   ")])
    assert len({cluster_of[1], cluster_of[2], cluster_of[3]}) == 3


def test_dup_posts_by_channel_catches_near_dup_across_channels():
    t = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        (1, "Общий текст новости про запуск ракеты и старт миссии", "a", t),
        (2, "Общий текст новости про запуск ракеты и старт миссии!", "b", t),
        (3, "Совсем уникальный текст про урожай и фермеров", "a", t),
    ]
    dup, total = dedup.dup_posts_by_channel(rows, dup_hours=48)
    assert dup.get("a") == 1 and dup.get("b") == 1
    assert total.get("a") == 2


def _mk_db(path):
    con = S.connect(str(path))
    now = S.utcnow()
    for h in ("a", "b"):
        con.execute("INSERT INTO channels(handle,title,status,is_author)"
                    " VALUES (?,?, 'active', 1)", (h, h))
    ids = {r["handle"]: r["id"] for r in con.execute("SELECT id, handle FROM channels")}

    def post(handle, mid, text, age_days=0):
        con.execute(
            "INSERT INTO posts(channel_id,message_id,date_utc,text,text_hash,views,"
            "reactions,is_ad) VALUES (?,?,?,?,?,?,?,0)",
            (ids[handle], mid, (now - S.timedelta(days=age_days)).isoformat(), text,
             hashlib.sha1(text.encode()).hexdigest(), 100, 10))

    # наполнение каналов (фоны разные, чтобы не склеивались случайно)
    for i in range(6):
        post("a", 100 + i, f"спорт футбол матч номер {i} завершился со счётом")
        post("b", 200 + i, f"кулинария рецепт супа номер {i} готовится быстро")
    # почти-дубль через границу каналов: тексты РАЗНЫЕ (разный text_hash)
    post("a", 150, "Прорыв в термоядерном синтезе подтверждён лабораторией")
    post("b", 250, "Прорыв в термоядерном синтезе подтверждён лабораторией!")
    con.commit()
    return con, ids


def test_xconf_counts_cluster_not_only_exact_hash(tmp_path):
    """D-38: Xconf видит почти-дубль, которого нет в точном text_hash."""
    con, _ids = _mk_db(tmp_path / "d38.db")
    try:
        cfg = S.load_config()
        S.compute_baselines(con, cfg)
        S.compute_scores(con, cfg)
        post_id = con.execute("SELECT id FROM posts WHERE message_id=150").fetchone()["id"]
        xconf = con.execute("SELECT xconf FROM scores WHERE post_id=?", (post_id,)).fetchone()["xconf"]
        assert xconf >= 2, f"кластер не увидел почти-дубль (xconf={xconf})"
        # точный хеш этого поста уникален по каналам — старый путь дал бы xconf=1
        same = con.execute(
            "SELECT COUNT(DISTINCT channel_id) FROM posts"
            " WHERE text_hash=(SELECT text_hash FROM posts WHERE message_id=150)"
        ).fetchone()[0]
        assert same == 1, "тексты не должны совпадать точно (иначе тест не о том)"
    finally:
        con.close()


def test_dup_ratio_uses_cluster(tmp_path):
    """D-39: dup_ratio канала ловит почти-дубль (точный хеш его не видит)."""
    con, ids = _mk_db(tmp_path / "d39.db")
    try:
        cfg = S.load_config()
        S.compute_baselines(con, cfg)
        row = con.execute("SELECT dup_posts, dup_ratio FROM channel_baselines"
                          " WHERE channel_id=?", (ids["a"],)).fetchone()
        assert row is not None
        assert row["dup_posts"] >= 1, "кластерный dup_ratio не поймал почти-дубль"
        assert row["dup_ratio"] > 0
    finally:
        con.close()
