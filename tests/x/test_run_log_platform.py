"""D-24: представление X ``run_log`` различает платформы явной колонкой.

Раньше представление показывало ``run_id IS NULL OR run_id IN (X-run)``: строки
вне прогона «по допущению» принадлежали X. Теперь принадлежность — колонка
``main.run_log.platform``, и чужая (Telegram) строка в представление X не
попадает, даже если у неё ``run_id IS NULL``.
"""
from __future__ import annotations

from tuber.platforms.x import store as db


def test_adapter_log_run_sets_platform_x(con):
    db.log_run(con, "INFO", "внутри прогона", run_id=None)
    db.log_run(con, "WARN", "вне прогона")
    con.commit()
    rows = con.execute("SELECT platform, msg FROM main.run_log").fetchall()
    assert rows and all(r["platform"] == "x" for r in rows)
    # Через представление X строки видны (это X-овы).
    visible = {r["msg"] for r in con.execute("SELECT msg FROM run_log")}
    assert "вне прогона" in visible


def test_x_view_hides_foreign_platform_rows(con):
    # Чужая строка: Telegram, вне прогона (run_id NULL), без связи с X.
    con.execute(
        "INSERT INTO main.run_log(run_id, platform, ts, level, ref, msg)"
        " VALUES (NULL, 'telegram', '2026-09-15 00:00:00', 'INFO', 'ch', 'telegram-строка')")
    db.log_run(con, "INFO", "x-строка")
    con.commit()
    msgs = {r["msg"] for r in con.execute("SELECT msg FROM run_log")}
    assert "x-строка" in msgs
    assert "telegram-строка" not in msgs
