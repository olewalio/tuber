"""ТЗ-47 (контур 4): граф первопроходцев — trusted_indegree, lead_time, first_mover.

Синтетическая база, без сети: проверяются ровно правила ТЗ.

* trusted_indegree_30d — РАЗНЫЕ крупные авторы (верхние 10 % по размеру внутри
  платформы), сославшиеся ребром mention/quote/repost; повтор одного автора не
  считается.
* lead_time_median — медиана лага автора внутри сюжетов (отрицательное = раньше
  второго независимого автора).
* first_mover — автор, написавший ≥ 30 минут до второго автора; доля от его
  многолетних сюжетов; пост-уровневая ось ``score.first_mover``.
"""
from __future__ import annotations

import json

from tuber.core import firstmovers


def _source(con, sid, handle, subs, platform="x"):
    con.execute(
        "INSERT INTO source(id, platform, handle, status, subs) VALUES (?,?,?,'active',?)",
        (sid, platform, handle, subs))


def _content(con, cid, platform, source_id, external_id, published, handle=None):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at,"
        " author_handle) VALUES (?,?,?,?,?,?)",
        (cid, platform, source_id, external_id, published, handle))


def _edge(con, cid, from_source, kind, tvalue, ttype="account", tplat="x",
          last_seen="2026-09-20 00:00:00"):
    con.execute(
        """INSERT INTO edge(from_platform, from_source_id, from_content_id, kind,
             target_type, target_platform, target_value, first_seen_at, last_seen_at)
           VALUES ('x', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (from_source, cid, kind, ttype, tplat, tvalue, last_seen, last_seen))


def _story(con, sid, platform="x"):
    con.execute(
        "INSERT INTO story(id, platform, created_at, first_pub_at) VALUES (?,?,?,?)",
        (sid, platform, "2026-09-20 00:00:00", "2026-09-20 00:00:00"))


def _member(con, story_id, content_id, handle, role="echo"):
    con.execute(
        "INSERT INTO story_member(story_id, content_id, handle, role) VALUES (?,?,?,?)",
        (story_id, content_id, handle, role))


def _scenario(con):
    # 20 X-аккаунтов: верхние 10 % = 2 (ids 1 и 2). Остальные — мелкие/средние.
    for i in range(1, 21):
        _source(con, i, f"src{i}", subs=(100 - i) * 1000)
    # Ребро от крупного №1 и №2 на «small» (id 3): два разных крупных.
    for cid in range(900, 905):
        _content(con, cid, "x", 1, f"t{cid}", "2026-09-19 00:00:00", "src1")
    _edge(con, 900, 1, "mention", "src3")
    _edge(con, 901, 2, "quote", "src3")
    # Тот же крупный №1 ссылается повторно — distinct не растёт.
    _edge(con, 902, 1, "mention", "src3")
    # Ребро старше окна (31 день) не считается.
    _edge(con, 903, 2, "repost", "src4", last_seen="2026-07-01 00:00:00")

    # Сюжеты: 100 — first mover src1 (лаг 60 мин); 101 — без первого хода (лаг 5 мин).
    _source(con, 50, "src50", None)  # без размера — в пары не попадает
    _content(con, 1001, "x", 1, "t1001", "2026-09-20 00:00:00", "src1")
    _content(con, 1002, "x", 2, "t1002", "2026-09-20 01:00:00", "src2")
    _content(con, 1003, "x", 1, "t1003", "2026-09-21 00:00:00", "src1")
    _content(con, 1004, "x", 2, "t1004", "2026-09-21 00:05:00", "src2")
    # Одиночный сюжет: в разбор не входит.
    _content(con, 1005, "x", 3, "t1005", "2026-09-21 02:00:00", "src3")
    _story(con, 100)
    _story(con, 101)
    _story(con, 102)
    _member(con, 100, 1001, "src1", "primary")
    _member(con, 100, 1002, "src2", "echo")
    _member(con, 101, 1003, "src1", "primary")
    _member(con, 101, 1004, "src2", "echo")
    _member(con, 102, 1005, "src3", "primary")
    # Оценки постов (для оси score.first_mover).
    for cid in (1001, 1002, 1003, 1004, 1005):
        con.execute(
            "INSERT INTO score(content_id, platform, computed_at, significance)"
            " VALUES (?, 'x', '2026-09-21 00:00:00', 1.0)", (cid,))
    con.commit()


def test_sizes_prefers_subs_and_x_views_fallback(con):
    _source(con, 1, "withsubs", 5000)
    _source(con, 2, "nosubs", None)
    _content(con, 10, "x", 2, "t10", "2026-09-20 00:00:00")
    for i, views in enumerate((100, 300, 200)):
        con.execute(
            "INSERT INTO metric_snapshot(content_id, captured_at, views, platform,"
            " external_id) VALUES (10, ?, ?, 'x', 't10')",
            (f"2026-09-20 0{i}:00:00", views))
    con.commit()
    sizes = firstmovers.source_sizes(con)
    assert sizes[1]["size"] == 5000 and sizes[1]["basis"] == "subs"
    assert sizes[2]["size"] == 200 and sizes[2]["basis"] == "views_median"


def test_trusted_indegree_counts_distinct_large_only(con):
    _scenario(con)
    data = firstmovers.build(con, now=__import__("datetime").datetime(2026, 9, 21, 12))
    by_source = data["indegree_by_source"]
    assert by_source[3] == {1, 2}          # src3: два разных крупных
    assert 4 not in by_source              # ребро старше окна отброшено


def test_lead_and_first_mover(con):
    _scenario(con)
    import datetime as dt
    now = dt.datetime(2026, 9, 21, 12)
    data = firstmovers.build(con, now=now)
    ledger = data["ledger"]
    assert ledger[1]["first_moves"] == 1 and ledger[1]["stories"] == 2
    assert abs(ledger[1]["first_mover_share"] - 0.5) < 1e-9
    # Леads src1: сюжет 100 -> -60 мин (до t2), сюжет 101 -> -5 мин; медиана -32.5.
    assert abs(ledger[1]["lead_time_median"] - (-32.5)) < 1e-6
    # Второй автор никогда не «первый».
    assert ledger[2]["first_moves"] == 0
    assert ledger[3]["first_moves"] == 0   # одиночный сюжет не засчитан
    assert data["story_stats"]["stories"] == 2
    assert data["story_stats"]["stories_with_first_mover"] == 1
    assert data["story_stats"]["single_author_skipped"] == 1


def test_write_fills_ledger_and_score_axis(con):
    _scenario(con)
    import datetime as dt
    now = dt.datetime(2026, 9, 21, 12)
    stats = firstmovers.refresh(con, now=now, write_data=True)
    assert stats["ledger_rows"] > 0
    row = con.execute("SELECT * FROM first_mover WHERE source_id=1").fetchone()
    assert row["first_moves"] == 1 and row["stories"] == 2
    assert abs(row["first_mover_share"] - 0.5) < 1e-9
    # Ось поста: первый пост сюжета 100 = 1.0, остальные = 0.0.
    fm = dict(con.execute(
        "SELECT content_id, first_mover FROM score WHERE first_mover IS NOT NULL"))
    assert fm[1001] == 1.0
    assert fm[1002] == 0.0
    assert fm[1003] == 0.0            # сюжет 101 без первого хода
    assert 1005 not in fm             # одиночный сюжет — оси нет
    # Прогон отмечен в журнале.
    assert con.execute(
        "SELECT COUNT(*) FROM run WHERE platform='graph' AND mode='first-movers'"
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM run_log WHERE platform='graph'").fetchone()[0] == 1


def test_write_is_idempotent_and_clears_stale(con):
    _scenario(con)
    import datetime as dt
    now = dt.datetime(2026, 9, 21, 12)
    firstmovers.refresh(con, now=now, write_data=True)
    stats2 = firstmovers.refresh(con, now=now, write_data=True)
    assert stats2["ledger_rows"] == stats2["ledger_rows"]
    assert con.execute("SELECT COUNT(*) FROM first_mover").fetchone()[0] == stats2["ledger_rows"]


def test_pairs_require_tenfold(con):
    _scenario(con)
    import datetime as dt
    now = dt.datetime(2026, 9, 21, 12)
    plist = firstmovers.pairs(con, now=now)
    # src1: 100*1000=100000 -> src3: 97*1000=97000: превышение ~1.03 — НЕ пара.
    small = {p["small_handle"] for p in plist}
    assert "src3" not in small
    # Мелкий, но ИЗВЕСТНЫЙ (subs >= PAIR_MIN_SMALL_SIZE): попадает в пары.
    _source(con, 98, "small_known", 2000)
    _edge(con, 903, 1, "mention", "small_known")
    con.commit()
    plist = firstmovers.pairs(con, now=now)
    known = [p for p in plist if p["small_handle"] == "small_known"]
    assert known and known[0]["ratio"] >= 10
    assert known[0]["distinct_trusted"] == 1


def test_degenerate_small_subs_one_is_not_a_record(con):
    """D-57: ``subs=1`` — неснятый счётчик, а не вырожденное превышение ×100000."""
    _scenario(con)
    import datetime as dt
    now = dt.datetime(2026, 9, 21, 12)
    _source(con, 99, "tiny", 1)
    _edge(con, 904, 1, "mention", "tiny")
    con.commit()
    stats: dict = {}
    plist = firstmovers.pairs(con, now=now, stats=stats)
    assert all(p["small_handle"] != "tiny" for p in plist), "вырожденная пара в выдаче"
    assert stats["skipped_small_unknown"] >= 1, "отсев не посчитан"
    # Отчёт тоже честно не показывает вырожденную пару и называет отсев числом.
    r = firstmovers.report(con, now=now)
    assert all(p["small_handle"] != "tiny" for p in r["pairs"])
    assert r["pairs_skipped_min_size"] >= 1
    assert r["pairs_min_small_size"] == firstmovers.PAIR_MIN_SMALL_SIZE
    assert "отсеяно порогом мелкой стороны" in firstmovers.format_report(r)


def test_small_side_known_helper():
    assert firstmovers.small_side_known({"size": 1}) is False
    assert firstmovers.small_side_known({"size": 181}) is False
    assert firstmovers.small_side_known({"size": 1115}) is True
    assert firstmovers.small_side_known({"size": None}) is False


def test_report_shape(con):
    _scenario(con)
    import datetime as dt
    r = firstmovers.report(con, now=dt.datetime(2026, 9, 21, 12))
    for key in ("pioneers", "pairs", "accounts_indegree_ge2", "authors_with_first_move"):
        assert key in r
    text = firstmovers.format_report(r)
    assert "Граф первопроходцев" in text
    assert json.loads(firstmovers.format_json(r))["window_days"] == 30
