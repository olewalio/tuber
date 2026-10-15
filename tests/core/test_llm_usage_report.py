"""P0-3: отчёт расхода читает ТОЛЬКО ``llm_usage``.

Склейка с ``classify_daily`` запрещена (двойной счёт), поэтому тест кладёт в
``classify_daily`` заведомо другие числа и проверяет, что отчёт их не видит.
"""
from __future__ import annotations

from tuber.analysis import llm_usage
from tuber.core import db, schema


def _init(path):
    conn = db.connect(path)
    schema.init_schema(conn)
    return conn


def _add(conn, platform, stage, tin, tout, cost, created_at):
    conn.execute(
        "INSERT INTO llm_usage(platform, stage, model, tokens_in, tokens_out,"
        " cost_usd, created_at) VALUES(?,?,?,?,?,?,?)",
        (platform, stage, "m", tin, tout, cost, created_at))
    conn.commit()


def test_report_sums_only_llm_usage(tmp_path):
    path = str(tmp_path / "r.db")
    conn = _init(path)
    _add(conn, "youtube", "classify", 10, 5, 1.50, "2026-09-10 00:00:00")
    _add(conn, "x", "classify", 20, 10, 2.25, "2026-09-20 00:00:00")
    _add(conn, "telegram", "ads", 30, 15, 0.25, "2026-09-21 00:00:00")
    # Мусор в classify_daily: отчёт обязан его игнорировать.
    conn.execute(
        "INSERT INTO classify_daily(day, platform, posts, cost_usd)"
        " VALUES('2026-09-20','x',5,999.0)")
    conn.commit()
    conn.close()

    ro = db.connect(path, readonly=True)
    try:
        rep = llm_usage.summary(ro)
    finally:
        ro.close()
    assert abs(rep["total"]["cost_usd"] - 4.0) < 1e-9
    assert rep["total"]["calls"] == 3
    platforms = set(rep["by_platform"])
    assert platforms == {"youtube", "x", "telegram"}
    assert rep["first_created_at"] == "2026-09-10 00:00:00"
    # Сумма по youtube — только llm_usage.
    assert abs(rep["by_platform"]["youtube"]["cost_usd"] - 1.50) < 1e-9


def test_report_window_filters_by_created_at(tmp_path):
    path = str(tmp_path / "r.db")
    conn = _init(path)
    _add(conn, "x", "classify", 1, 1, 1.0, "2026-01-01 00:00:00")
    _add(conn, "x", "classify", 1, 1, 2.0, "2026-10-02 00:00:00")
    conn.close()

    ro = db.connect(path, readonly=True)
    try:
        rep = llm_usage.summary(ro, days=2, now="2026-10-03 00:00:00")
    finally:
        ro.close()
    assert abs(rep["total"]["cost_usd"] - 2.0) < 1e-9
    assert rep["window_start"] == "2026-10-01 00:00:00"
    # Начало учёта — по всей таблице, а не по окну.
    assert rep["first_created_at"] == "2026-01-01 00:00:00"


def test_report_cli_runs(tmp_path, capsys):
    path = str(tmp_path / "r.db")
    conn = _init(path)
    _add(conn, "x", "classify", 1, 1, 0.5, "2026-10-02 00:00:00")
    conn.close()
    rc = llm_usage.main(["--db", path, "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"cost_usd": 0.5' in out
