"""ТЗ-2-ФИКС: приёмка не должна писать в рабочую базу (П9, П11, П12).

Сеть не используется: проверяются предохранители промоушена и симуляция на копии.
"""
import os
from datetime import datetime, timedelta, timezone

from tuber.platforms.x import cli, config, store as db, registry


def _old_iso(days=20):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _seed_provisional(con, handle, *, posts_collected, ai_density=0.9, days=20):
    con.execute(
        "INSERT INTO accounts (handle, tier, status, added_by, posts_collected,"
        " ai_density, ai_density_src, provisional_since, last_success_at)"
        " VALUES (?, 'C', 'provisional', 'test', ?, ?, 'heuristic', ?, ?)",
        (handle, posts_collected, ai_density, _old_iso(days), db.utcnow_iso()))
    con.commit()


def _statuses(con):
    return dict(con.execute(
        "SELECT status, COUNT(*) FROM accounts GROUP BY status").fetchall())


def test_promotion_refuses_zero_collected_posts(con):
    """П12: provisional с posts_collected = 0 не повышается, отказ пишется в run_log."""
    for handle in ("zero1", "zero2"):
        _seed_provisional(con, handle, posts_collected=0)
    promoted = registry.promote_provisional(con, simulate_days=14)
    assert promoted == [], promoted
    assert con.execute("SELECT COUNT(*) FROM accounts WHERE status='active'"
                       ).fetchone()[0] == 0
    msgs = [r[0] for r in con.execute("SELECT msg FROM run_log").fetchall()]
    assert any("нет собранных постов" in m for m in msgs), msgs
    # аккаунты остались provisional, даты не подделаны
    assert _statuses(con) == {"provisional": 2}


def test_simulation_runs_on_copy_and_leaves_working_db_untouched(db_path):
    """П9/П11: --simulate-tenure работает на копии, рабочая БД не меняется."""
    con = db.connect(db_path)
    _seed_provisional(con, "real1", posts_collected=25)
    _seed_provisional(con, "real2", posts_collected=25)
    before_statuses = _statuses(con)
    before_logs = con.execute("SELECT COUNT(*) FROM run_log").fetchone()[0]
    before_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    con.close()

    rc = cli.main(["registry", "promote", "--simulate-tenure", "--days", "14"])
    assert rc == 0

    con2 = db.connect(db_path)
    after_statuses = _statuses(con2)
    after_logs = con2.execute("SELECT COUNT(*) FROM run_log").fetchone()[0]
    after_runs = con2.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    con2.close()

    # рабочая БД не изменилась ни по статусам, ни по журналам
    assert after_statuses == before_statuses, (before_statuses, after_statuses)
    assert "active" not in after_statuses
    assert after_logs == before_logs
    assert after_runs == before_runs


def test_simulation_refuses_production_db_path(monkeypatch, db_path, tmp_path, capsys):
    """П9: если копия по realpath совпала с рабочей БД — отказ, ничего не пишем."""
    from tuber.platforms.x.cli import _simulate_tenure_on_copy

    class _Args:
        days = 14

    prod = os.path.realpath(config.DB_PATH)
    monkeypatch.setattr("tuber.platforms.x.cli.tempfile.mkdtemp", lambda prefix="": str(tmp_path))
    monkeypatch.setattr("tuber.platforms.x.cli.os.path.exists", lambda p: True)
    monkeypatch.setattr("tuber.platforms.x.cli.shutil.copy2", lambda src, dst: None)
    # копия по realpath совпадает с рабочей БД -> защита обязана отказать
    monkeypatch.setattr("tuber.platforms.x.cli.os.path.realpath", lambda p: prod)

    rc = _simulate_tenure_on_copy(_Args())
    out = capsys.readouterr().out
    assert rc == 3
    assert "simulation_refuses_production_db" in out
