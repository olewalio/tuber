"""ТЗ-8 Р2.x: потребитель рёбер — кандидаты четырёх типов, пороги, анти-самопиар."""
from __future__ import annotations

import json

from tuber.core import graph

from tests.graph.conftest import add_content, add_source


def test_distinct_sources_counts_sources_not_posts(con):
    # один канал-дайджест с сотней постов не должен дать «100 источников»
    add_source(con, 1, "telegram", "digest")
    for i in range(50):
        add_content(con, 100 + i, "telegram", 1, f"digest/{i}",
                    links=json.dumps(["https://habr.com/a"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con)
    row = con.execute("SELECT distinct_sources, seen_count, meta_json FROM candidate"
                      " WHERE handle='habr.com'").fetchone()
    assert row["distinct_sources"] == 1
    assert row["seen_count"] == 50
    assert json.loads(row["meta_json"])["verify_eligible"] is False


def test_threshold_two_sources_eligible(con):
    add_source(con, 1, "telegram", "a")
    add_source(con, 2, "telegram", "b")
    add_content(con, 1, "telegram", 1, "a/1", links=json.dumps(["https://habr.com/x"]))
    add_content(con, 2, "telegram", 2, "b/1", links=json.dumps(["https://habr.com/y"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con)
    row = con.execute("SELECT distinct_sources, meta_json FROM candidate"
                      " WHERE handle='habr.com'").fetchone()
    assert row["distinct_sources"] == 2
    assert json.loads(row["meta_json"])["verify_eligible"] is True


def test_quote_from_tier_a_eligible(con):
    add_source(con, 1, "x", "author", tier="A")
    add_content(con, 10, "x", 1, "100")
    graph.record_post_edges(con, platform="x", from_content_id=10, from_source_id=1,
                            from_handle="author", orig_handle="bigbrain",
                            is_quote=True, origin="collect")
    con.commit()
    graph.consume(con)
    row = con.execute("SELECT meta_json, distinct_sources FROM candidate"
                      " WHERE platform='x' AND handle='bigbrain'").fetchone()
    assert row is not None and row["distinct_sources"] == 1
    assert json.loads(row["meta_json"])["verify_eligible"] is True


def test_anti_selfpromo_marks_spam(con):
    add_source(con, 1, "x", "spammer")
    for i in range(graph.EDGE_SPAM_MIN_LINKS):
        add_content(con, 100 + i, "x", 1, str(1000 + i),
                    links=json.dumps([f"https://bestblogs.dev/{i}"]))
    graph.backfill(con, platforms=("x",))
    stats = graph.consume(con)
    row = con.execute("SELECT spam, meta_json FROM candidate"
                      " WHERE handle='bestblogs.dev'").fetchone()
    assert row["spam"] == 1
    assert json.loads(row["meta_json"])["verify_eligible"] is False
    assert stats["spam"] == 1


def test_candidate_types_x_tg_yt_web(con):
    add_source(con, 1, "telegram", "srcT")
    add_source(con, 2, "telegram", "srcT2")
    for sid, ext in ((1, "srcT/1"), (2, "srcT2/1")):
        add_content(con, sid, "telegram", sid, ext,
                    links=json.dumps(["https://x.com/alice", "https://t.me/newchan",
                                      "https://youtube.com/@NewYT", "https://habr.com/a"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con)
    got = {(r["platform"], r["kind"], r["handle"]) for r in
           con.execute("SELECT platform, kind, handle FROM candidate")}
    assert ("x", "handle", "alice") in got
    assert ("telegram", "channel", "newchan") in got
    assert ("youtube", "channel", "newyt") in got
    assert ("web", "feed", "habr.com") in got


def test_video_target_skipped(con):
    add_source(con, 1, "telegram", "a")
    add_source(con, 2, "telegram", "b")
    for sid in (1, 2):
        add_content(con, sid, "telegram", sid, f"x/{sid}",
                    links=json.dumps(["https://youtu.be/abcdefghijk"]))
    graph.backfill(con, platforms=("telegram",))
    stats = graph.consume(con)
    assert con.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 0
    assert stats["skipped_type"] >= 1


def test_dedup_with_existing_source_any_platform(con):
    add_source(con, 99, "web", "habr.com")
    add_source(con, 1, "telegram", "a")
    add_source(con, 2, "telegram", "b")
    for sid in (1, 2):
        add_content(con, sid, "telegram", sid, f"x/{sid}",
                    links=json.dumps(["https://habr.com/a"]))
    graph.backfill(con, platforms=("telegram",))
    stats = graph.consume(con)
    assert stats["skipped_existing"] == 1
    assert con.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 0


def test_consume_does_not_lower_registered_status(con):
    add_source(con, 1, "telegram", "a")
    add_source(con, 2, "telegram", "b")
    for sid in (1, 2):
        add_content(con, sid, "telegram", sid, f"x/{sid}",
                    links=json.dumps(["https://habr.com/a"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con)
    con.execute("UPDATE candidate SET validated='ok' WHERE handle='habr.com'")
    con.commit()
    add_content(con, 3, "telegram", 1, "a/3", links=json.dumps(["https://habr.com/a"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con)
    row = con.execute("SELECT validated, seen_count FROM candidate WHERE handle='habr.com'"
                      ).fetchone()
    assert row["validated"] == "ok"


def test_found_via_shows_origin(con):
    add_source(con, 1, "telegram", "chanZ")
    add_source(con, 2, "telegram", "chanY")
    for sid, h in ((1, "chanZ"), (2, "chanY")):
        add_content(con, sid, "telegram", sid, f"{h}/1",
                    links=json.dumps(["https://habr.com/a"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con, platforms=("telegram",))
    fv = con.execute("SELECT found_via FROM candidate WHERE handle='habr.com'").fetchone()[0]
    assert fv.startswith("link_web:tg_posts:")
