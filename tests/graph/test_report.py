"""ТЗ-8 Р5: отчёт графа и авто-отсев по отдаче."""
from __future__ import annotations

import json

from tuber.core import graph

from tests.graph.conftest import add_content, add_source


def test_report_numbers_and_top(con):
    add_source(con, 1, "telegram", "a")
    add_source(con, 2, "telegram", "b")
    for sid in (1, 2):
        add_content(con, sid, "telegram", sid, f"x/{sid}",
                    links=json.dumps([f"https://habr.com/{sid}", "https://proglib.io/p"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con, platforms=("telegram",))
    r = graph.report(con)
    assert r["edges_total"] == 4
    assert r["edges_by_kind"]["link_web"] == 4
    top = {d["target_value"]: d["distinct_sources"] for d in r["top_domains"]}
    assert top["habr.com"] == 2
    assert top["proglib.io"] == 2
    assert r["eligible"] == 2
    text = graph.format_report(con)
    assert "рёбер всего: 4" in text
    assert "habr.com" in text


def test_prune_no_yield_rejects(con):
    add_source(con, 1, "web", "quiet.example", status="active")
    add_source(con, 2, "web", "loud.example", status="active")
    for i in range(5):
        add_content(con, 100 + i, "web", 2, f"loud.example/{i}",
                    published="2026-09-10 00:00:00")
    # «loud» выживает только при посте выше порога score (условие Р5.2 — ИЛИ).
    con.execute("INSERT INTO score(content_id, computed_at, significance) VALUES (100,"
                " strftime('%Y-%m-%d %H:%M:%S','now'), 5.0)")
    con.commit()
    stats = graph.prune_no_yield(con, dry=False)
    assert stats["rejected"] == 1
    row = con.execute("SELECT status, last_error FROM source WHERE id=1").fetchone()
    assert row["status"] == "rejected" and row["last_error"] == "no_yield"
    assert con.execute("SELECT status FROM source WHERE id=2").fetchone()[0] == "active"


def test_prune_dry_run_does_not_write(con):
    add_source(con, 1, "web", "quiet.example", status="active")
    stats = graph.prune_no_yield(con, dry=True)
    assert stats["rejected"] == 1
    assert con.execute("SELECT status FROM source WHERE id=1").fetchone()[0] == "active"
