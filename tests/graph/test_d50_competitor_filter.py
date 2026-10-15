"""ТЗ-B6 D-50: переключатель `--exclude-competitors` в выдаче графа.

Флаг ``edge.competitor=1`` пишется при сборе рёбер на домены-конкуренты
(``graph.COMPETITOR_HOSTS``), но выдача «на подключение» его игнорировала. Теперь
``graph consume``/``graph report --exclude-competitors`` отсеивают такие цели по
умолчанию ВЫКЛ, а в отчёте видна строка ``competitors=excluded|included`` и число
исключённых.
"""
from __future__ import annotations

import json

from tuber.core import graph

from tests.graph.conftest import add_content, add_source


def _competitor_edge(con):
    """Ребро на домен-конкурент (vk.com) с competitor=1.

    Условие D-50 известно: ``edge.competitor=1`` для целевого источника.
    """
    con.execute(
        """INSERT INTO edge (from_platform, from_source_id, from_content_id, from_handle,
             kind, target_type, target_platform, target_value, target_url, weight,
             competitor, first_seen_at, last_seen_at)
           VALUES ('telegram', 1, 1, 'a', 'link_web', 'domain', 'web', 'vk.com',
                   'https://vk.com/', 0.3, 1, '2026-09-01 00:00:00',
                   '2026-09-01 00:00:00')""")
    con.commit()


def _normal_edge(con):
    con.execute(
        """INSERT INTO edge (from_platform, from_source_id, from_content_id, from_handle,
             kind, target_type, target_platform, target_value, target_url, weight,
             competitor, first_seen_at, last_seen_at)
           VALUES ('telegram', 2, 2, 'b', 'link_web', 'domain', 'web', 'habr.com',
                   'https://habr.com/', 0.3, 0, '2026-09-01 00:00:00',
                   '2026-09-01 00:00:00')""")
    con.commit()


def _seed(con):
    add_source(con, 1, "telegram", "a")
    add_content(con, 1, "telegram", 1, "a/1")
    add_source(con, 2, "telegram", "b")
    add_content(con, 2, "telegram", 2, "b/1")
    _competitor_edge(con)
    _normal_edge(con)


def test_consume_includes_competitors_by_default(con):
    _seed(con)
    stats = graph.consume(con, platforms=("telegram",))
    handles = {r["handle"] for r in con.execute("SELECT handle FROM candidate")}
    assert "vk.com" in handles and "habr.com" in handles
    assert stats["excluded_competitors"] == 0


def test_consume_excludes_competitors_when_enabled(con):
    _seed(con)
    stats = graph.consume(con, platforms=("telegram",), exclude_competitors=True)
    handles = {r["handle"] for r in con.execute("SELECT handle FROM candidate")}
    assert "vk.com" not in handles
    assert "habr.com" in handles
    assert stats["excluded_competitors"] == 1


def test_report_shows_competitors_mode_and_count(con):
    _seed(con)
    graph.consume(con, platforms=("telegram",))
    r_off = graph.report(con)
    assert r_off["competitors"] == "included"
    assert r_off["competitors_excluded"] == 0
    assert r_off["competitor_targets"] == 1

    r_on = graph.report(con, exclude_competitors=True)
    assert r_on["competitors"] == "excluded"
    assert r_on["competitors_excluded"] == 1
    # список предложений домена-конкурента больше не содержит
    assert all(d["target_value"] != "vk.com" for d in r_on["top_domains"])
    assert any(d["target_value"] == "vk.com" for d in r_off["top_domains"])

    text = graph.format_report(con, exclude_competitors=True)
    assert "competitors=excluded" in text
    assert "конкурентов исключено из выдачи: 1" in text
    assert "competitors=included" in graph.format_report(con)
