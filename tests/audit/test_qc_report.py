"""Тесты сборщика QC-отчёта `tools/audit-scripts/qc_report.py`.

Файл лежит вне пакета (каталог с дефисом), поэтому грузится по пути.
Проверяем логику отчёта: проценты, таблицу по платформам, отбор худших
примеров и чтение контекста промпта из его базы.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT = Path("/root/tuber/tools/audit-scripts/qc_report.py")


def _load():
    spec = importlib.util.spec_from_file_location("qc_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qc_report"] = mod
    spec.loader.exec_module(mod)
    return mod


def _raw(db_path: str) -> dict:
    return {
        "db": db_path, "per_platform": 2, "seed": 1,
        "generated_at": "2026-10-02T00:00:00+00:00",
        "meta": {"telegram": {"n": 2, "agree": 1, "disagree": 1},
                 "x": {"n": 2, "agree": 2, "disagree": 0}},
        "results": [
            {"platform": "telegram", "key": "111", "db_topic": "агенты",
             "fresh_topic": "агенты", "agree": True, "ai_agree": True},
            {"platform": "telegram", "key": "222", "db_topic": "кейсы",
             "fresh_topic": "скандалы", "agree": False, "ai_agree": False,
             "db_conf": 0.4, "db_is_ai": 1, "fresh_is_ai": 0,
             "snippet": "короткий текст", "url": "https://t.me/x/1"},
            {"platform": "x", "key": "333", "db_topic": "релизы",
             "fresh_topic": "релизы", "agree": True, "ai_agree": True},
            {"platform": "x", "key": "444", "db_topic": "модели",
             "fresh_topic": "модели", "agree": True, "ai_agree": True},
        ],
    }


def _report(tmp_path, monkeypatch, db_path: str) -> str:
    mod = _load()
    raw_p = tmp_path / "raw.json"
    raw_p.write_text(json.dumps(_raw(db_path), ensure_ascii=False), encoding="utf-8")
    out_p = tmp_path / "qc.md"
    monkeypatch.setattr(sys, "argv",
                        ["qc_report.py", "--raw", str(raw_p), "--out", str(out_p)])
    assert mod.main() == 0
    return out_p.read_text(encoding="utf-8")


def test_summary_shares_and_platform_table(tmp_path, monkeypatch):
    text = _report(tmp_path, monkeypatch, str(tmp_path / "nodb.db"))
    assert "Совпало тема-в-тема: 3 из 4 (75.0%)" in text
    assert "Расхождений темы: 1 из 4 (25.0%)" in text
    assert "| Telegram | 2 | 1 | 1 | 50.0% |" in text
    assert "| X (Twitter) | 2 | 2 | 0 | 0.0% |" in text


def test_worst_example_lists_id_and_ai_mismatch(tmp_path, monkeypatch):
    text = _report(tmp_path, monkeypatch, str(tmp_path / "nodb.db"))
    assert "id=222" in text
    assert "Расхождение is_ai: боевое=1, независимо=0" in text
    assert "https://t.me/x/1" in text
    worst_block = text.split("худших примеров")[1].split("## Как читать")[0]
    assert "id=111" not in worst_block


def test_db_context_reports_prompt_version(tmp_path, monkeypatch):
    db = tmp_path / "ctx.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE classification(content_id INTEGER, platform TEXT,"
                " topic TEXT, prompt_ver TEXT, classified_at TEXT)")
    con.execute("INSERT INTO classification VALUES (1,'telegram','агенты','tg-v1',"
                "'2026-09-20 10:00:00')")
    con.commit()
    con.close()
    text = _report(tmp_path, monkeypatch, str(db))
    assert "| Telegram | tg-v1 | 1 | 2026-09-20 — 2026-09-20 |" in text


def test_missing_db_does_not_break_report(tmp_path, monkeypatch):
    text = _report(tmp_path, monkeypatch, str(tmp_path / "absent.db"))
    assert "Доля ошибок разметки" in text
    assert "Контекст боевой разметки" not in text
