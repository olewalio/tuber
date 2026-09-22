"""ТЗ-3c: совместимость адаптеров с системным SQLite (3.45.1).

Дефект: TEMP-триггеры адаптеров писали в ядро КВАЛИФИЦИРОВАННЫМ именем
(``INSERT INTO main.run_log``). SQLite ≤ 3.45.1 запрещает квалифицированный DML
в теле триггера (``qualified table names are not allowed …``), поэтому
``store.connect()`` падал на системном ``/usr/bin/python3``. Здесь проверяется:

1. соединение и запись конфликтующих таблиц работают под /usr/bin/python3
   (если его нет — skip с явной причиной);
2. в тексте создаваемых TEMP-представлений/триггеров нет квалифицированных имён;
3. версия SQLite определяется и сообщается в диагностике (``tuber doctor``);
4. трансляция legacy-имён конфликтующих таблиц (см. :mod:`tuber.core.sqlcompat`).

Сеть не используется.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tuber.core import sqlcompat
from tuber.platforms.x import store as xstore
from tuber.platforms.youtube import store as ytstore

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PYTHON = "/usr/bin/python3"

# Подсистемный прогон обоих адаптеров: соединение + запись конфликтующих
# таблиц (именно то, что падало на 3.45.1) + чтение через legacy-имена.
_SUBPROCESS_SCRIPT = textwrap.dedent(
    """
    import os, sys, tempfile
    sys.path.insert(0, os.environ["TUBER_SRC"])
    from tuber.platforms.x import store as xstore
    from tuber.platforms.youtube import store as ytstore

    sqlite_version = __import__("sqlite3").sqlite_version
    d = tempfile.mkdtemp()
    x = xstore.connect(os.path.join(d, "x.db"))
    x.execute("INSERT INTO blocklist (handle, reason, added_at) VALUES ('h','r','2026-09-17T00:00:00')")
    x.execute("INSERT INTO run_log (run_id, ts, level, handle, msg) VALUES (NULL,'2026-09-17T00:00:00','INFO','h','m')")
    x.execute("INSERT INTO metrics_daily (day, posts_ingested) VALUES ('2026-09-17', 5)")
    x.execute("INSERT INTO classify_daily (day, posts) VALUES ('2026-09-17', 5)")
    x.commit()
    assert x.execute("SELECT COUNT(*) FROM main.blocklist").fetchone()[0] == 1
    assert x.execute("SELECT COUNT(*) FROM main.run_log").fetchone()[0] == 1
    assert x.execute("SELECT handle FROM blocklist").fetchone()[0] == "h"
    x.close()

    y = ytstore.connect(os.path.join(d, "yt.db"))
    y.execute("INSERT INTO thumbnail_vision (video_id, model) VALUES ('v1','m')")
    y.execute("INSERT INTO llm_usage (stage, model, tokens_in, tokens_out, cost_usd) VALUES ('s','m',1,2,0.01)")
    y.commit()
    assert y.execute("SELECT COUNT(*) FROM main.thumbnail_vision").fetchone()[0] == 1
    assert y.execute("SELECT COUNT(*) FROM main.llm_usage").fetchone()[0] == 1
    y.close()
    print("OK", sqlite_version)
    """
)


def _system_python() -> str:
    if not os.path.exists(SYSTEM_PYTHON):
        pytest.skip(f"нет системного интерпретатора {SYSTEM_PYTHON}")
    return SYSTEM_PYTHON


@pytest.mark.parametrize("executable", [SYSTEM_PYTHON, sys.executable])
def test_connect_and_collision_writes_in_subprocess(executable, tmp_path):
    """connect() и запись конфликтующих таблиц — на системном и venv Python."""
    if not os.path.exists(executable):
        pytest.skip(f"нет интерпретатора {executable}")
    env = dict(os.environ, TUBER_SRC=str(REPO_ROOT))
    proc = subprocess.run(
        [executable, "-c", _SUBPROCESS_SCRIPT],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"{executable} упал:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "OK" in proc.stdout


def test_no_qualified_names_in_compat_ddl():
    """В тексте CREATE TEMP VIEW/TRIGGER нет ``main.``/``temp.`` имён."""
    for store in (xstore, ytstore):
        creates = [
            s for s in store._COMPAT_STATEMENTS
            if s.lstrip().upper().startswith(("CREATE TEMP VIEW", "CREATE TEMP TRIGGER"))
        ]
        assert creates, f"{store.__name__}: нет CREATE-утверждений"
        for stmt in creates:
            assert "main." not in stmt, f"{store.__name__}: main. в DDL: {stmt[:80]}"
            assert "temp." not in stmt, f"{store.__name__}: temp. в DDL: {stmt[:80]}"


def test_compat_ddl_creates_on_old_sqlite_semantics():
    """Ни одна CREATE TEMP TRIGGER не содержит квалифицированного DML-таргета."""
    import re

    for store in (xstore, ytstore):
        for stmt in store._COMPAT_STATEMENTS:
            if not stmt.lstrip().upper().startswith("CREATE TEMP TRIGGER"):
                continue
            assert not re.search(r"(?is)\b(?:INSERT|UPDATE|DELETE)\b[^;]*\b(?:main|temp)\.\w+", stmt)


def test_sqlite_version_reported():
    """Версия SQLite определяется и совпадает с драйвером."""
    info = sqlcompat.sqlite_info()
    assert info["sqlite_version"] == sqlite3.sqlite_version
    assert info["executable"] == sys.executable
    assert info["python_version"]


def test_doctor_reports_sqlite_version():
    """``python3 -m tuber doctor`` сообщает версию SQLite и интерпретатор."""
    proc = subprocess.run(
        [sys.executable, "-m", "tuber", "doctor"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SQLite" in proc.stdout
    assert sqlite3.sqlite_version in proc.stdout
    assert sys.executable in proc.stdout


def test_unsupported_sqlite_raises_clear_error(monkeypatch):
    """Старый SQLite — понятная ошибка с версией, интерпретатором и подсказкой."""
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 0, 0), raising=False)
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.0.0", raising=False)
    with pytest.raises(RuntimeError) as exc:
        sqlcompat.ensure_supported_sqlite()
    msg = str(exc.value)
    assert "3.0.0" in msg
    assert sys.executable in msg
    assert "Подсказка" in msg


# ---------------------------------------------------------------------------
# Трансляция legacy-имён конфликтующих таблиц
# ---------------------------------------------------------------------------

X_MAP = xstore.X_COMPAT_VIEWS


@pytest.mark.parametrize("legacy,view", sorted(X_MAP.items()))
def test_translation_rewrites_table_positions(legacy, view):
    sql = f"SELECT * FROM {legacy} WHERE 1"
    assert f"FROM {view}" in sqlcompat.translate_legacy_sql(sql, X_MAP)
    assert view in sqlcompat.translate_legacy_sql(
        f"INSERT INTO {legacy} (a) VALUES (1)", X_MAP)
    assert view in sqlcompat.translate_legacy_sql(f"UPDATE {legacy} SET a=1", X_MAP)
    assert view in sqlcompat.translate_legacy_sql(f"DELETE FROM {legacy}", X_MAP)
    assert view in sqlcompat.translate_legacy_sql(f"PRAGMA table_info({legacy})", X_MAP)


def test_translation_ignores_qualified_and_literals():
    sql = "SELECT 'FROM run_log' FROM main.run_log JOIN main.run_log r ON 1"
    assert sqlcompat.translate_legacy_sql(sql, X_MAP) == sql


def test_collision_views_are_renamed_in_temp():
    """Legacy-имя больше не затеняет таблицу ядра; представление — под своим."""
    assert xstore.compat_view_name("run_log") == "x_run_log"
    assert ytstore.compat_view_name("thumbnail_vision") == "yt_thumbnail_vision"


def test_reduced_path_wrapper_pins_interpreter(tmp_path):
    """Обёртка с урезанным PATH берёт интерпретатор ЯВНО, а не из PATH.

    Подменяем интерпретатор заглушкой через TUBER_PYTHON: если бы обёртка
    звала ``python3`` по имени, при ``PATH=/usr/bin:/bin`` она ушла бы на
    системный ``/usr/bin/python3`` (SQLite 3.45.1).
    """
    wrapper = REPO_ROOT / "scripts" / "x" / "tuber_x_collect.sh"
    assert wrapper.exists()

    fake = tmp_path / "fakepy"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$REC"\n')
    fake.chmod(0o755)
    rec = tmp_path / "rec.txt"
    link = tmp_path / "tuber_x_collect_a.sh"
    os.symlink(wrapper, link)

    proc = subprocess.run(
        [str(link)],
        cwd=str(REPO_ROOT),
        env={
            "PATH": "/usr/bin:/bin",
            "TUBER_PYTHON": str(fake),
            "REC": str(rec),
            "TUBER_X_PROJECT": str(tmp_path),
            "TUBER_X_LOG_DIR": str(tmp_path / "logs"),
        },
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert rec.read_text().splitlines() == ["-m", "tuber", "x", "collect", "--tier", "A"]
