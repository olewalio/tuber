"""Тесты CLI-диспетчера."""

from __future__ import annotations

from tuber import cli


def test_help_returns_zero():
    assert cli.main(["--help"]) == 0
    assert cli.main(["help"]) == 0


def test_no_args_returns_two():
    assert cli.main([]) == 2


def test_unknown_command_returns_two():
    assert cli.main(["nope"]) == 2


def test_schema_command(tmp_path):
    from tuber.core import db, schema

    target = str(tmp_path / "s.db")
    conn = db.connect(target)
    try:
        schema.init_schema(conn)
    finally:
        conn.close()
    assert cli.main(["schema", "--target", target]) == 0
    # Обёртка tools поддерживается на уровне диспетчера.
    assert cli.main(["tools", "schema", "--target", target]) == 0


def test_migrate_dispatch(tmp_path, legacy_paths):
    target = str(tmp_path / "m.db")
    rc = cli.main([
        "migrate", "--target", target,
        "--os", legacy_paths["os"], "--x", legacy_paths["x"], "--tg", legacy_paths["tg"],
    ])
    assert rc == 0
