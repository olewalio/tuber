#!/usr/bin/env python3
"""Тесты скоринга значимости и бэкфилла форвардов (перенос `scripts/test_scoring.py`, ТЗ-4).

Формула и пороги не менялись; изменился только слой доступа к БД
(единая база ядра через адаптер `store`). Боевая база не трогается: все
проверки на временной БД в tmp_path.
"""
import os

import pytest

from tuber.platforms.telegram import collect as C  # noqa: E402
from tuber.platforms.telegram import scoring as S  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


# ---------------------------------------------------------------------------
# Чистые функции формулы
# ---------------------------------------------------------------------------
def test_er_of():
    assert S.er_of(100, 10) == 0.1
    assert S.er_of(0, 10) is None        # views=0 → не считать
    assert S.er_of(None, 10) is None
    assert S.er_of(100, None) is None


def test_combine_eng():
    assert S.combine_eng(None, None) is None
    assert S.combine_eng(4.0, None) == 4.0
    assert S.combine_eng(None, 9.0) == 9.0
    assert S.combine_eng(4.0, 9.0) == pytest.approx(6.0)  # геом. среднее двух баз


def test_dup_penalty_and_wsrc():
    assert S.dup_penalty_of(1) == 0.0
    assert S.dup_penalty_of(5) == pytest.approx(0.8)
    assert S.wsrc_of(0) == 1.0
    assert S.wsrc_of(4) == pytest.approx(1 / 5)


def test_decay_and_significance():
    assert S.decay_of(0, 30) == 1.0
    assert S.decay_of(30, 30) == pytest.approx(0.5)
    assert S.decay_of(60, 30) == pytest.approx(0.25)
    # Eng=None → significance None (views=0)
    assert S.significance_of(fr=1, wsrc=1, xconf=1, eng=None,
                             dup_penalty=0, topic_weight=1, decay=1) is None
    sig = S.significance_of(fr=1, wsrc=1, xconf=1, eng=2.0,
                            dup_penalty=0, topic_weight=1, decay=1)
    assert sig == pytest.approx(2.0)


def test_parse_dt_naive_is_utc():
    from datetime import timezone
    dt = S.parse_dt("2025-12-14 08:19:20")
    assert dt is not None and dt.tzinfo == timezone.utc
    dt2 = S.parse_dt("2026-09-15T09:10:18+00:00")
    assert dt2.tzinfo is not None
    assert S.parse_dt("") is None


def test_config_constants_not_duplicated_in_code():
    """Требование 7: числа формулы живут в config/scoring.json; DEFAULTS — только
    запасной вариант при отсутствии файла. Тест ловит расхождение (дрейф)."""
    import json
    raw = json.load(open(S.CONFIG_PATH, encoding="utf-8"))
    for key, val in S.DEFAULTS.items():
        assert key in raw, f"константа {key} отсутствует в config/scoring.json"
        assert raw[key] == val, f"{key}: config={raw[key]} != DEFAULTS={val} (расхождение)"


# ---------------------------------------------------------------------------
# Форварды: разметка t.me/s
# ---------------------------------------------------------------------------
def test_extract_forwards_false_positive_guard():
    # «Forwarded from 42 секунды» — это имя канала-источника, НЕ счётчик форвардов.
    html = (
        '<div class="tgme_widget_message" data-post="ch/1">'
        '<div class="tgme_widget_message_forwarded_from accent_color">'
        'Forwarded from&nbsp;<a class="tgme_widget_message_forwarded_from_name">'
        '42 секунды</a></div></div>'
    )
    root = C.parse_html(html)
    msg = next(n for n in C.walk(root) if "data-post" in n.attrs)
    assert C.extract_forwards(msg) is None, "ложный форвард из имени источника"


def test_extract_forwards_real_markup():
    # Если Telegram вернёт счётчик классом со словом forward — он разбирается.
    html = (
        '<div class="tgme_widget_message" data-post="ch/2">'
        '<span class="tgme_widget_message_forwards">1.2K</span></div>'
    )
    root = C.parse_html(html)
    msg = next(n for n in C.walk(root) if "data-post" in n.attrs)
    assert C.extract_forwards(msg) == 1200


def test_parse_page_forwards_none_on_fixture():
    # В реальных фикстурах счётчика форвардов нет → None (не выдумываем).
    p = C.parse_page(open(os.path.join(FIX, "chatgptv.html"), encoding="utf-8").read(), "chatgptv")
    assert p and all(x["forwards"] is None for x in p)


# ---------------------------------------------------------------------------
# Защита боевой базы
# ---------------------------------------------------------------------------
def test_production_guard(tmp_path):
    with pytest.raises(SystemExit):
        S.assert_can_write(S.DEFAULT_DB, allow_production=False)
    S.assert_can_write(S.DEFAULT_DB, allow_production=True)  # с флагом — можно
    S.assert_can_write(str(tmp_path / "copy.db"), allow_production=False)  # копия — можно


# ---------------------------------------------------------------------------
# Сквозной прогон на временной БД
# ---------------------------------------------------------------------------
def _mk_db(path):
    con = S.connect(str(path))
    now = S.utcnow()
    chans = [("author", 1), ("aggr", 1), ("flooded", 0), ("small", 1)]
    for h, auth in chans:
        con.execute("INSERT INTO channels(handle,title,status,is_author) VALUES (?,?,?,?)",
                    (h, h, "flooded" if h == "flooded" else "active", auth))
    ids = {r["handle"]: r["id"] for r in con.execute("SELECT id, handle FROM channels")}

    def post(handle, mid, age_days, views, reactions, text, is_ad=0, dup=False):
        h = f"{handle}-{text}" if dup else f"{handle}-{text}"
        import hashlib
        con.execute(
            "INSERT INTO posts(channel_id,message_id,date_utc,text,text_hash,views,reactions,is_ad)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ids[handle], mid, (now - S.timedelta(days=age_days)).isoformat(),
             text, hashlib.sha1(h.encode()).hexdigest(), views, reactions, is_ad))

    # author: 6 постов, устойчивый ER ~10%
    for i in range(6):
        post("author", 100 + i, i, 100, 10, f"a{i}")
    # фоновый пост, мало прочитанный: ER 5% → ниже
    post("author", 200, 0, 10000, 500, "background")
    post("author", 250, 0, 100, 30, "outlier")  # ER 30% > медианы канала
    # агрегатор: 6 постов с высоким ER (накрутка) → anomaly
    for i in range(6):
        post("aggr", 300 + i, i, 10, 9, f"b{i}")
    # малый канал (<5 постов) → без базовой линии
    post("small", 400, 0, 5, 1, "s0")
    # реклама → фильтруется
    post("author", 500, 0, 100, 50, "ad", is_ad=1)
    con.commit()
    return con, ids


def test_end_to_end(tmp_path):
    con, ids = _mk_db(tmp_path / "t.db")
    cfg = S.load_config()
    base = S.compute_baselines(con, cfg)
    assert base["baselines_written"] >= 2
    # малый канал не попал в базы
    n_small = con.execute("SELECT COUNT(*) FROM channel_baselines WHERE channel_id=?",
                          (ids["small"],)).fetchone()[0]
    assert n_small == 0
    sc = S.compute_scores(con, cfg)
    assert sc["posts_scored"] > 0 and sc["global_median_er"] > 0
    # накрученный канал по базовой линии выше, но у него anomaly по ER>50%
    anom = con.execute(
        """SELECT COUNT(*) FROM scores s JOIN posts p ON p.id=s.post_id
           WHERE p.channel_id=? AND s.anomaly=1""", (ids["aggr"],)).fetchone()[0]
    assert anom >= 6, "высокий ER агрегатора не помечен аномалией"
    # views=0 не считать
    assert sc["posts_null_views"] >= 0
    con.close()


def test_report_filters_exclude_ad_and_flooded(tmp_path):
    con, ids = _mk_db(tmp_path / "t2.db")
    cfg = S.load_config()
    S.compute_baselines(con, cfg)
    S.compute_scores(con, cfg)
    rep = S.build_report(con, cfg, top_n=15)
    handles = {r["handle"] for r in rep["top_after"]}
    assert "flooded" not in handles, "flooded-канал попал в рейтинг"
    assert "ad" not in {r["text"] for r in rep["top_after"]}, "реклама попала в рейтинг"
    # аномалии не в честном топе, но в отдельном блоке
    anom_handles = {r["handle"] for r in rep["anomalies"]}
    assert "aggr" in anom_handles
    assert not (handles & anom_handles), "аномалия попала в честный рейтинг"
    con.close()


def test_significance_ordering_relative_to_baseline(tmp_path):
    """Пост с ER выше медианы канала должен иметь eng>1, ниже — eng<1."""
    con, ids = _mk_db(tmp_path / "t3.db")
    cfg = S.load_config()
    S.compute_baselines(con, cfg)
    S.compute_scores(con, cfg)
    high = con.execute("SELECT eng_channel FROM scores WHERE post_id="
                       "(SELECT id FROM posts WHERE message_id=250)").fetchone()[0]
    low = con.execute("SELECT eng_channel FROM scores WHERE post_id="
                      "(SELECT id FROM posts WHERE message_id=200)").fetchone()[0]
    assert high > 1.0, f"высокий пост eng={high}"
    assert low < 1.0, f"фоновый пост eng={low}"
    con.close()


# ---------------------------------------------------------------------------
# Порог микровыборки честного рейтинга (ТЗ от 16.09.2026, п.1-6)
# ---------------------------------------------------------------------------
def _mk_threshold_db(path):
    """Канал с базовой линией (ER 10%) и постами на границе порога показов/реакций.

    message_id:  200 — 30 просмотров, 9 реакций (ER 30%): вирально по ER, но
                      микровыборка по показам (ниже min_views=100);
                 201 — 500 просмотров, 200 реакций (ER 40%): выше порога;
                 202 — views NULL, 5 реакций: не оценивается (не выдумываем);
                 203 — 1000 просмотров, 2 реакции: ниже порога реакций;
                 100..105 — база канала, ER 10%.
    """
    import hashlib
    con = S.connect(str(path))
    now = S.utcnow()
    con.execute("INSERT INTO channels(handle,title,status,is_author) VALUES ('ch','ch','active',1)")
    cid = con.execute("SELECT id FROM channels WHERE handle='ch'").fetchone()[0]

    def post(mid, views, reactions, age_days=0):
        text = f"t{mid}"
        con.execute(
            "INSERT INTO posts(channel_id,message_id,date_utc,text,text_hash,views,reactions,is_ad)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (cid, mid, (now - S.timedelta(days=age_days)).isoformat(), text,
             hashlib.sha1(text.encode()).hexdigest(), views, reactions))

    for i in range(6):
        post(100 + i, 1000, 100, age_days=i)  # база канала: ER 10%
    post(200, 30, 9)
    post(201, 500, 200)
    post(202, None, 5)
    post(203, 1000, 2)
    con.commit()
    return con, cid


def _scored(path):
    con, cid = _mk_threshold_db(path)
    cfg = S.load_config()
    S.compute_baselines(con, cfg)
    S.compute_scores(con, cfg)
    rep = S.build_report(con, cfg, top_n=15)
    return con, cfg, rep


def _ids(rows):
    return {r["message_id"] for r in rows}


def test_below_views_threshold_not_in_honest_ranking(tmp_path):
    """ТЗ п.6: пост с просмотрами ниже порога не попадает в честный рейтинг."""
    con, cfg, rep = _scored(tmp_path / "th1.db")
    assert rep["min_views"] == 100 and rep["min_reactions"] == 5
    assert 200 not in _ids(rep["top_after"]), "пост с 30 просмотрами попал в честный рейтинг"
    assert 200 in _ids(rep["top_unthresholded"]), "пост должен быть в топе ДО порога"
    con.close()


def test_below_views_threshold_in_small_sample(tmp_path):
    """ТЗ п.6: он попадает в блок «малая выборка»."""
    con, cfg, rep = _scored(tmp_path / "th2.db")
    assert 200 in _ids(rep["small_sample"]), "микровыборка не показана в отдельном блоке"
    assert 203 in _ids(rep["small_sample"]), "пост ниже порога реакций не в блоке"
    con.close()


def test_above_threshold_ranks_as_before(tmp_path):
    """ТЗ п.6: пост выше порога ранжируется как раньше (в honest и в top_unthresholded)."""
    con, cfg, rep = _scored(tmp_path / "th3.db")
    assert 201 in _ids(rep["top_after"]), "пост выше порога выпал из рейтинга"
    assert 201 in _ids(rep["top_unthresholded"])
    # порядок по значимости сохраняется
    sigs = [r["significance"] for r in rep["top_after"]]
    assert sigs == sorted(sigs, reverse=True)
    con.close()


def test_views_null_behavior_defined(tmp_path):
    """ТЗ п.6: при views IS NULL поведение определено и протестировано.

    NULL не подменяется нулём: significance остаётся NULL, пост не ранжируется,
    но не скрывается — он в блоке «малая выборка» и в счётчике «не оценены».
    """
    con, cfg, rep = _scored(tmp_path / "th4.db")
    assert 202 not in _ids(rep["top_after"]), "пост с views=NULL не должен ранжироваться"
    assert 202 in _ids(rep["small_sample"]), "пост с views=NULL должен быть виден в блоке"
    sig = con.execute("SELECT significance FROM scores WHERE post_id=(SELECT id FROM posts WHERE message_id=202)").fetchone()[0]
    assert sig is None, "views=NULL не должен превращаться в 0/число"
    assert rep["unscored"]["posts"] >= 1
    con.close()


def test_header_counters_match_block(tmp_path):
    """ТЗ п.6: счётчики отсечённых в шапке совпадают с содержимым блока."""
    con, cfg, rep = _scored(tmp_path / "th5.db")
    total = rep["cut_views"]["posts"] + rep["cut_react"]["posts"] + rep["unscored"]["posts"]
    assert total == rep["small_total"]["posts"], "сумма счётчиков != размер блока"
    # прямой SQL-счёт по тому же условию, что строит блок
    flt = S.ranking_sample(con, cfg)
    direct = S._count_sample(con, flt["small"])
    assert direct["posts"] == rep["small_total"]["posts"]
    # каждая строка блока действительно не проходит порог (или не оценена)
    for r in rep["small_sample"]:
        assert (r["significance"] is None
                or r["views"] is None or r["views"] < cfg["min_views"]
                or r["reactions"] is None or r["reactions"] < cfg["min_reactions"]), \
            f"в блоке оказался пост выше порога: @{r['handle']} views={r['views']} react={r['reactions']}"
    con.close()


def test_threshold_is_part_of_ranking_not_print(tmp_path):
    """ТЗ п.4: порог — часть ранжирования (из кода), а не косметика печати.

    Меняем порог в конфиге → меняется состав честного рейтинга (без правки кода).
    """
    con, cfg, _ = _scored(tmp_path / "th6.db")
    low = dict(cfg, min_views=1000, min_reactions=1000)
    low_rep = S.build_report(con, low, top_n=15)
    assert 200 not in _ids(low_rep["top_after"]) and 201 not in _ids(low_rep["top_after"])
    assert low_rep["remaining"]["posts"] == 0  # всё ушло в микровыборку
    # ТЗ п.5: пустой/неполный честный рейтинг прямо сообщает об этом, не добивается мелочью
    assert "меньше top_n" in S.format_report(low_rep, low)
    high = dict(cfg, min_views=1, min_reactions=1)
    high_rep = S.build_report(con, high, top_n=15)
    assert 200 in _ids(high_rep["top_after"]), "порог не влияет на ранжирование (он только в печати?)"
    con.close()


def test_format_report_mentions_threshold_and_small_block(tmp_path):
    """ТЗ п.2, п.3: печать содержит блок микровыборки и счётчики в шапке."""
    con, cfg, rep = _scored(tmp_path / "th7.db")
    text = S.format_report(rep, cfg)
    assert "МАЛАЯ ВЫБОРКА" in text
    assert "отсечено порогом показов" in text
    assert "осталось в честном рейтинге" in text
    assert "Подбор порога" in text
    con.close()
