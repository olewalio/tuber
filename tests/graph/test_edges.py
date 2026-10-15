"""ТЗ-8 Р1.5/Р1.6: запись рёбер и бэкфилл (идемпотентность)."""
from __future__ import annotations

import json

from tuber.core import graph

from tests.graph.conftest import add_content, add_source


def test_record_post_edges_kinds(con):
    add_source(con, 1, "x", "author")
    add_content(con, 10, "x", 1, "100", published="2026-09-01 00:00:00")
    res = graph.record_post_edges(
        con, platform="x", from_content_id=10, from_source_id=1, from_handle="author",
        links=json.dumps(["https://t.me/chan", "https://habr.com/a", "https://youtu.be/abcdefghijk"]),
        mentions=json.dumps(["alice"]), orig_handle="bob", is_quote=True, is_repost=False,
        origin="collect")
    con.commit()
    kinds = {r[0] for r in con.execute("SELECT kind FROM edge")}
    assert kinds == {"link_tg", "link_web", "link_yt", "mention", "quote"}
    assert res["created"] == 5
    # цитата указывает на автора оригинала (то, что терялось в парсере)
    row = con.execute("SELECT target_value FROM edge WHERE kind='quote'").fetchone()
    assert row[0] == "bob"


def test_record_edge_idempotent_seen_count(con):
    add_source(con, 1, "x", "author")
    add_content(con, 10, "x", 1, "100")
    for _ in range(2):
        graph.record_post_edges(con, platform="x", from_content_id=10, from_source_id=1,
                                links=json.dumps(["https://habr.com/a"]), origin="collect")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM edge").fetchone()[0] == 1
    assert con.execute("SELECT seen_count FROM edge").fetchone()[0] == 2


def test_competitor_edge_flagged_but_created(con):
    add_source(con, 1, "telegram", "chan")
    add_content(con, 10, "telegram", 1, "chan/1")
    graph.record_post_edges(con, platform="telegram", from_content_id=10, from_source_id=1,
                            links=json.dumps(["https://rutube.ru/video"]), origin="collect")
    con.commit()
    row = con.execute("SELECT target_value, competitor FROM edge").fetchone()
    assert row[0] == "rutube.ru" and row[1] == 1


def test_backfill_idempotent_and_no_quote_repost(con):
    add_source(con, 1, "telegram", "chanA")
    add_content(con, 10, "telegram", 1, "chanA/1",
                links=json.dumps(["https://habr.com/a", "https://habr.com/b"]),
                mentions=json.dumps(["@friend"]))
    s1 = graph.backfill(con, platforms=("telegram",))
    rows1 = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    s2 = graph.backfill(con, platforms=("telegram",))
    rows2 = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    assert rows1 == rows2 == s1["created"] == 2
    assert s2["created"] == 0 and s2["updated"] >= rows2
    # quote/repost бэкфиллом НЕ восстанавливаются
    assert s1["lost_kinds"] == ["quote", "repost"]
    assert con.execute("SELECT COUNT(*) FROM edge WHERE kind IN ('quote','repost')"
                       ).fetchone()[0] == 0
    # from_handle берётся из источника — found_via получит осмысленный проект
    row = con.execute("SELECT from_handle FROM edge LIMIT 1").fetchone()
    assert row[0] == "chanA"


def test_backfill_skips_count_reasons(con):
    add_source(con, 1, "telegram", "chan")
    add_content(con, 10, "telegram", 1, "chan/1",
                links=json.dumps(["https://bit.ly/x", "https://t.me/+invite"]))
    s = graph.backfill(con, platforms=("telegram",))
    assert s["skipped"] == 2
    assert s["skip_reasons"].get("skip_host") == 1
    assert s["skip_reasons"].get("invite") == 1
    assert con.execute("SELECT COUNT(*) FROM edge").fetchone()[0] == 0


def test_edge_weight_ordering_and_independence():
    assert graph.edge_weight("quote") > graph.edge_weight("repost")
    assert graph.edge_weight("repost") > graph.edge_weight("mention")
    assert graph.edge_weight("mention") > graph.edge_weight("link_web")
    assert graph.edge_weight("link_web", distinct_sources=5) > graph.edge_weight("link_web")
