"""Тесты инструмента сверки parity (ТЗ-1 §7)."""

from __future__ import annotations

from tuber.core import db
from tuber.tools import parity_report


def test_parity_is_zero_on_correct_data(migrated, legacy_paths):
    target, _ = migrated
    results = parity_report.run_parity(
        target, os_path=legacy_paths["os"], x_path=legacy_paths["x"], tg_path=legacy_paths["tg"]
    )
    assert results, "правила должны были выполниться"
    for res in results:
        assert res.diff == 0, f"{res.rule.id}: legacy={res.before}, core={res.after}"


def test_parity_cli_returns_zero(migrated, legacy_paths):
    target, _ = migrated
    rc = parity_report.main([
        "--target", target, "--os", legacy_paths["os"], "--x", legacy_paths["x"],
        "--tg", legacy_paths["tg"],
    ])
    assert rc == 0


def test_parity_catches_broken_row(migrated, legacy_paths):
    target, _ = migrated
    # Ломаем данные: лишний youtube-контент, которого нет в legacy.
    conn = db.connect(target)
    try:
        with db.write_tx(conn):
            conn.execute(
                "INSERT INTO content (platform, external_id, kind, published_at) "
                "VALUES ('youtube','bogus','video','2026-01-01 00:00:00')"
            )
    finally:
        conn.close()

    results = parity_report.run_parity(
        target, os_path=legacy_paths["os"], x_path=legacy_paths["x"], tg_path=legacy_paths["tg"]
    )
    broken = [r for r in results if r.diff not in (0, None)]
    assert broken, "расхождение должно быть поймано"

    rc = parity_report.main([
        "--target", target, "--os", legacy_paths["os"], "--x", legacy_paths["x"],
        "--tg", legacy_paths["tg"],
    ])
    assert rc == 2
    assert "РАСХОЖДЕНИЕ" in parity_report.format_table(results)


def test_parity_known_diffs_are_accepted(migrated, legacy_paths):
    target, _ = migrated
    conn = db.connect(target)
    try:
        with db.write_tx(conn):
            conn.execute(
                "INSERT INTO content (platform, external_id, kind, published_at) "
                "VALUES ('youtube','bogus','video','2026-01-01 00:00:00')"
            )
    finally:
        conn.close()

    known = {"os.videos": "тестовый лишний контент добавлен осознанно"}
    rc = parity_report.main([
        "--target", target, "--os", legacy_paths["os"], "--x", legacy_paths["x"],
        "--tg", legacy_paths["tg"], "--known-diffs", _write_json(migrated, known),
    ])
    assert rc == 0


def _write_json(migrated, known):
    import json
    import os
    import tempfile

    path = os.path.join(tempfile.mkdtemp(), "known.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(known, fh)
    return path


def test_parity_broken_fixture_reports_orphans(legacy_paths_broken, target_db):
    from tuber.tools import migrate_legacy

    migrate_legacy.migrate(
        target_db, os_path=legacy_paths_broken["os"], x_path=legacy_paths_broken["x"],
        tg_path=legacy_paths_broken["tg"],
    )
    results = parity_report.run_parity(
        target_db, os_path=legacy_paths_broken["os"], x_path=legacy_paths_broken["x"],
        tg_path=legacy_paths_broken["tg"],
    )
    diffs = {r.rule.id: r.diff for r in results if r.diff not in (0, None)}
    # Ровно два объяснимых сироты: снапшот без видео и метрики без поста.
    assert diffs == {"os.snapshots": -1, "x.post_metrics_history": -1}
