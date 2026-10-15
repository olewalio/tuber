"""Тесты ТЗ-E: русские описания X во ВСЕЙ выдаче Tuber (репозиторий /root/tuber).

Проверяется единый путь описания X-строк «Виральных» через
``_x_description_from``: приоритет ``summary_ru``/``title_ru`` → кэш ``report_text``
→ сырой текст (последний фолбэк со счётчиком деградации). Живой перевод в тестах
выключен автоматически (``pytest`` в ``sys.modules``), поэтому сеть не трогается.

T1: кэш-хит → русский текст.
T2: без кэша и ключа → сырой текст, ``_apply_ru_to_viral_x`` возвращает 1.
T3: ``title_ru`` приоритетнее сырого текста, кэш при этом не читается.
T4: сводка и полный отчёт дают ОДИН и тот же русский текст (сводка ⊆ полный).
T5: оболочка ``tuber_report.sh`` синтаксически цела и содержит загрузку ключа
    и ALERT про его отсутствие.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from tuber import config
from tuber.analysis import report
from tuber.core import db, schema


def _iso(days_ago: float = 1.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


#: Виральный пост блока 4: у автора 5 постов, медиана лайков 100, у одного
#: 1000 лайков (кратность 10 ≥ 3.0, лайков ≥ 200). Остальные 4 — ниже порога.
_VIRAL_TEXT = "Viral English story number five"
_VIRAL_HASH = "hash-viral-five"


def _build_core(tmp_path, *, title_ru=None, ru_cache=None):
    """Синтетическое ядро: один автор X с 5 постами за окно (один — виральный)."""
    conn = db.connect(str(tmp_path / "tz_e.db"))
    schema.init_schema(conn)
    likes = [100, 100, 100, 100, 1000]
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO source(id,platform,external_id,handle,title)"
            " VALUES (1,'x','xsrc','author_x','Author X')")
        for i, likes_i in enumerate(likes):
            cid = 100 + i
            is_viral = i == len(likes) - 1
            text = _VIRAL_TEXT if is_viral else f"ordinary english post {i}"
            text_hash = _VIRAL_HASH if is_viral else f"hash-{i}"
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,"
                "published_at,text,text_hash,author_handle,url,is_repost,lang)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,'en')",
                (cid, "x", 1, f"x{i}", _iso(1), text, text_hash, "author_x",
                 f"https://x.com/author_x/status/{cid}"))
            conn.execute(
                "INSERT INTO content_latest(content_id,likes,replies,platform,"
                "external_id) VALUES (?,?,0,'x',?)", (cid, likes_i, f"x{i}"))
            if title_ru is not None and is_viral:
                conn.execute(
                    "INSERT INTO classification(content_id,platform,lang,title_ru)"
                    " VALUES (?,'x','en',?)", (cid, title_ru))
            else:
                conn.execute(
                    "INSERT INTO classification(content_id,platform,lang)"
                    " VALUES (?,'x','en')", (cid,))
        if ru_cache is not None:
            conn.execute(
                "INSERT INTO report_text(text_hash,ru,model,created_at,src)"
                " VALUES (?,?,'test',?,'model')",
                (ru_cache[0], ru_cache[1], _iso(1)))
    return conn


def _full_text(conn, days: int = 10) -> str:
    lines, _ = report.viral_section(conn, _iso(days), days)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# T1: кэш-хит → русский текст
# --------------------------------------------------------------------------- #
def test_t1_cache_hit_prints_russian(tmp_path):
    conn = _build_core(tmp_path, ru_cache=(_VIRAL_HASH, "РУССКИЙ ТЕКСТ ИЗ КЭША"))
    try:
        text = _full_text(conn)
    finally:
        conn.close()
    assert "РУССКИЙ ТЕКСТ ИЗ КЭША" in text
    assert _VIRAL_TEXT not in text


# --------------------------------------------------------------------------- #
# T2: без кэша и ключа → сырой текст + счётчик деградации = 1
# --------------------------------------------------------------------------- #
def test_t2_no_cache_no_key_raw_text_and_degraded_one(tmp_path):
    conn = _build_core(tmp_path)
    try:
        pool = report._viral_x_pool(conn, _iso(30))
        shown, _ = report.viral_x_items(pool, _iso(10))
        assert len(shown) == 1
        degraded = report._apply_ru_to_viral_x(conn, shown)
        assert degraded == 1
        assert shown[0].title == _VIRAL_TEXT
        # И в готовой выдаче строка печатается сырым текстом.
        text = _full_text(conn)
    finally:
        conn.close()
    assert degraded == 1
    assert _VIRAL_TEXT in text


# --------------------------------------------------------------------------- #
# T3: title_ru классификации важнее сырого текста; кэш не читается
# --------------------------------------------------------------------------- #
def test_t3_title_ru_priority_over_raw_and_cache(tmp_path, monkeypatch):
    conn = _build_core(tmp_path, title_ru="РУССКИЙ ЗАГОЛОВОК КЛАССИФИКАЦИИ",
                       ru_cache=(_VIRAL_HASH, "ЭТО ИЗ КЭША, НЕ ДОЛЖНО ПОЯВИТЬСЯ"))

    def _boom(*_a, **_k):
        raise AssertionError("кэш/живой перевод не должен вызываться при title_ru")

    monkeypatch.setattr(report, "_ru_description", _boom)
    try:
        text = _full_text(conn)
    finally:
        conn.close()
    assert "РУССКИЙ ЗАГОЛОВОК КЛАССИФИКАЦИИ" in text
    assert "ЭТО ИЗ КЭША" not in text
    assert _VIRAL_TEXT not in text


# --------------------------------------------------------------------------- #
# T4: инвариант «сводка ⊆ полный» — один и тот же русский текст
# --------------------------------------------------------------------------- #
def test_t4_compact_and_full_same_russian(tmp_path):
    conn = _build_core(tmp_path, ru_cache=(_VIRAL_HASH, "РУССКИЙ ТЕКСТ ИЗ КЭША"))
    try:
        compact = report.viral_compact_items(conn, _iso(10), 10)
        x_items = [it for it in compact if it.head.startswith("@")]
        assert x_items, "виральный X-пункт обязан попасть в сводку"
        desc = x_items[0].desc
        text = _full_text(conn)
    finally:
        conn.close()
    assert desc == "РУССКИЙ ТЕКСТ ИЗ КЭША"
    assert desc in text
    assert _VIRAL_TEXT not in text


# --------------------------------------------------------------------------- #
# T5: оболочка — синтаксис и наличие загрузки ключа + ALERT
# --------------------------------------------------------------------------- #
def test_t5_shell_syntax_and_key_loading():
    script = config.ROOT / "scripts" / "common" / "tuber_report.sh"
    body = script.read_text(encoding="utf-8")
    assert "load_key DEEPSEEK_API_KEY" in body
    assert 'ENV_FILE=/root/.hermes/.env' in body
    assert "ALERT Tuber report: нет DEEPSEEK_API_KEY" in body
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash недоступен")
    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
