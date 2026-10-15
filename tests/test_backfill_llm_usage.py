"""P0-3: инструмент переноса ``tools/backfill_llm_usage.py``.

Проверяем: dry-run не пишет; повторный ``--apply`` не удваивает; при удержанном
замке сборщика — отказ и ноль записей. Сеть не используется.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import types
from pathlib import Path

import pytest

from tuber.core import db, schema

ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "backfill_llm_usage", ROOT / "tools" / "backfill_llm_usage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool():
    return _load_tool()


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "b.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    conn.close()
    return path


@pytest.fixture()
def jsonl_path(tmp_path):
    p = tmp_path / "deepseek_usage.jsonl"
    rows = [
        {"ts": "2026-09-01T00:00:00+00:00", "script": "tuber_x_classify",
         "model": "m", "prompt_tokens": 100, "completion_tokens": 50,
         "cache_read_tokens": 0, "cost_usd": 0.01},
        {"ts": "2026-09-02T00:00:00+00:00", "script": "tuber_tg_classify",
         "model": "m", "prompt_tokens": 200, "completion_tokens": 100,
         "cache_read_tokens": 0, "cost_usd": 0.02},
        {"ts": "2026-09-03T00:00:00+00:00", "script": "tuber_tg_ads",
         "model": "m", "prompt_tokens": 10, "completion_tokens": 5,
         "cache_read_tokens": 0, "cost_usd": 0.001},
        # Не tuber_*: переносу не подлежит.
        {"ts": "2026-09-03T00:00:00+00:00", "script": "datapine_classify",
         "model": "m", "prompt_tokens": 999, "completion_tokens": 999,
         "cache_read_tokens": 0, "cost_usd": 9.99},
    ]
    with open(p, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return str(p)


def _count(path):
    conn = db.connect(path, readonly=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    finally:
        conn.close()


def _no_ps(monkeypatch, tool):
    monkeypatch.setattr(tool.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=""))


def test_read_candidates_only_tuber(tool, jsonl_path):
    cands = tool.read_candidates(jsonl_path)
    assert len(cands) == 3
    assert {c["script"] for c in cands} == {
        "tuber_x_classify", "tuber_tg_classify", "tuber_tg_ads"}
    mapping = {c["script"]: (c["platform"], c["stage"]) for c in cands}
    assert mapping["tuber_x_classify"] == ("x", "classify")
    assert mapping["tuber_tg_classify"] == ("telegram", "classify")
    assert mapping["tuber_tg_ads"] == ("telegram", "ads")


def test_collector_detection_is_precise(tool):
    assert tool._collector_reason("python3 -m tuber x collect --tier A")
    assert tool._collector_reason(
        "/usr/local/lib/hermes-agent/venv/bin/python3 -m tuber yt collect")
    assert tool._collector_reason("/root/tuber/scripts/x/tuber_x_collect_a.sh")
    # Упоминание слов tuber/collect в произвольном тексте — не сборщик.
    assert tool._collector_reason(
        "jcode run # ТЗ: перенести расход tuber ... collect из jsonl") is None
    assert tool._collector_reason("python3 -m tuber usage --days 3") is None
    # Реальный ложный случай: argv агента содержит ТОЧНУЮ команду сборщика
    # текстом (например, в ТЗ). argv[0] — агент, а не интерпретатор/обёртка.
    assert tool._collector_reason(
        "jcode run 'python3 -m tuber x collect --tier A'") is None
    assert tool._collector_reason(
        "node agent.js --task=tuber_x_collect.sh") is None
    assert tool._collector_reason(
        "bash -c 'python3 -m tuber x collect'") is None


def test_until_window_excludes_postdeploy(tool, jsonl_path):
    all_c = tool.read_candidates(jsonl_path)
    assert len(all_c) == 3
    # Отсекаем всё после 01.09: остаются только записи 01.09 и 02.09.
    windowed = tool.read_candidates(jsonl_path, until="2026-09-02T12:00:00+00:00")
    assert [c["script"] for c in windowed] == [
        "tuber_x_classify", "tuber_tg_classify"]
    since = tool.read_candidates(jsonl_path, since="2026-09-02T00:00:00+00:00")
    assert {c["script"] for c in since} == {"tuber_tg_classify", "tuber_tg_ads"}


def test_dry_run_does_not_write(tool, jsonl_path, db_path, capsys):
    rc = tool.main(["--jsonl", jsonl_path, "--db", db_path, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "к переносу: 3 строк" in out
    assert "$0.0310" in out, out
    assert _count(db_path) == 0, "dry-run не должен писать"


def test_apply_then_repeat_does_not_double(tool, jsonl_path, db_path,
                                           monkeypatch, capsys):
    _no_ps(monkeypatch, tool)
    rc1 = tool.main(["--jsonl", jsonl_path, "--db", db_path, "--apply"])
    assert rc1 == 0
    assert _count(db_path) == 3
    capsys.readouterr()
    # Повторный запуск: ключи в legacy_map, новых строк нет.
    rc2 = tool.main(["--jsonl", jsonl_path, "--db", db_path, "--apply"])
    assert rc2 == 0
    out = capsys.readouterr().out
    assert "все записи уже перенесены" in out
    assert _count(db_path) == 3, "повторный --apply не должен удваивать"


def test_refuse_when_collector_lock_held(tool, jsonl_path, db_path, tmp_path,
                                         monkeypatch, capsys):
    _no_ps(monkeypatch, tool)
    lock = tmp_path / "held.lock"
    fh = open(lock, "a")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = tool.main(["--jsonl", jsonl_path, "--db", db_path, "--apply",
                        "--lock", str(lock)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "ОТКАЗ" in err
        assert _count(db_path) == 0, "при живом сборщике писать нельзя"
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def test_refuse_when_collector_process_running(tool, jsonl_path, db_path,
                                               monkeypatch, capsys):
    monkeypatch.setattr(
        tool.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(
            stdout="python3 -m tuber yt collect --loop\n"))
    rc = tool.main(["--jsonl", jsonl_path, "--db", db_path, "--apply"])
    assert rc == 1
    assert _count(db_path) == 0
