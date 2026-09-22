"""Тесты компактной сводки ``tuber report --compact`` (ТЗ-5-доп-2).

Планировщик Hermes для заданий ``no_agent`` доставляет владельцу ровно stdout
обёртки, поэтому сводка обязана (1) уложиться в лимит знаков, (2) не обрезать
ссылки и (3) честно считать скрытые пункты. Проверки идут на КОПИИ боевой базы
(``--db <копия>``), боевая ``data/tuber.db`` не открывается на запись.

Живая копия берётся в таком порядке: ``TUBER_TEST_LIVE_DB`` → свежая копия из
``data/backups/`` → копия, снятая штатным ``tuber db backup`` в tmp. Если базы
нет вовсе — тест пропускается.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tuber import config
from tuber.analysis import report
from tuber.core import db, schema
from tuber.tools import db_backup

LIMIT = report.COMPACT_LIMIT
PY = sys.executable


def _iso(days_ago: float = 1.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-m", "tuber", *args], cwd=str(config.ROOT),
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def _hidden_total(text: str) -> int:
    m = re.search(r"Скрыто по платформам: (.+)\.", text)
    assert m, f"в сводке нет строки счётчика скрытых:\n{text}"
    return sum(int(x) for x in re.findall(r"\d+", m.group(1)))


# --- Разбор сводки на разделы (ТЗ-5-доп-3) ---------------------------------
#: Пункт (и сводки, и полного отчёта) всегда заканчивается ссылкой или
#: «нет ссылки» — этим он отличается от строк «Правило: …».
_ITEM_END = re.compile(r";\s*(?:https?://\S+|нет ссылки.*)\s*$")
_SECTION_HEADS = (("YouTube", "1. YouTube"), ("X", "2. X"),
                  ("Telegram", "3. Telegram"))
#: Начала НЕплатформенных разделов (Виральные, сквозной сюжет, подвал).
#: Проверяются по ФРАЗЕ, а не по номеру: пункты внутри раздела нумеруются
#: «4. …», «5. …», и номер нельзя путать с номером раздела.
_SECTION_STOP_RE = re.compile(
    r"^\s*(?:\d+\.\s+)?(?:Виральные|Новое за сутки|Сквозной сюжет|"
    r"Правила ранжирования|Полный отчёт|Скрыто по платформам|ПОДВАЛ)")


def _sections(text: str) -> dict[str, list[str]]:
    """Разбить текст (сводку или полный отчёт) на блоки трёх платформ."""
    out: dict[str, list[str]] = {name: [] for name, _ in _SECTION_HEADS}
    cur: str | None = None
    for line in text.splitlines():
        head = next((n for n, h in _SECTION_HEADS if line.startswith(h)), None)
        if head is not None:
            cur = head
            continue
        if cur is not None and _SECTION_STOP_RE.match(line):
            cur = None
            continue
        if cur is not None:
            out[cur].append(line)
    return out


def _shown(block: list[str]) -> int:
    """Сколько пунктов показано в блоке раздела."""
    return sum(1 for line in block if _ITEM_END.search(line.rstrip()))


def _hidden(block: list[str]) -> int:
    """Счётчик «скрыто пунктов: N» из блока раздела."""
    for line in block:
        m = re.match(r"\s*скрыто пунктов:\s*(\d+)", line)
        if m:
            return int(m.group(1))
    raise AssertionError(f"в разделе нет счётчика скрытых: {block}")


def _section_totals(conn, cutoff: str) -> dict[str, int]:
    """Число пунктов каждого раздела в полном отчёте (по тем же выборкам)."""
    return {
        "YouTube": len(report.youtube_items(conn, cutoff)[0]),
        "X": len(report.x_items(conn, cutoff)[0]),
        "Telegram": len(report.telegram_items(conn, cutoff)[0]),
    }


def _live_copy(tmp_path: Path) -> str:
    env = os.environ.get("TUBER_TEST_LIVE_DB")
    if env and Path(env).is_file():
        return env
    backups = config.ROOT / "data" / "backups"
    if backups.is_dir():
        candidates = sorted(backups.glob("tuber-*.db"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return str(candidates[0])
    live = config.db_path(None)
    if Path(live).is_file():
        info = db_backup.backup_database(live, backup_dir=str(tmp_path), log=False)
        return info["backup"]
    pytest.skip("нет боевой базы и её копии")


@pytest.fixture(scope="module")
def live_copy(tmp_path_factory) -> str:
    """Копия боевой базы с дозаполненным ``content.url`` (D-45).

    Снимок из ``data/backups/`` мог быть снят до бэкфилла ссылок. Дозаполняем
    ссылки на КОПИИ (боевая база не открывается на запись), чтобы ``tuber report``
    читал ту же колонку, что и в бою после миграции, и проверял реальные ссылки.
    """
    src = _live_copy(tmp_path_factory.mktemp("live"))
    dst = tmp_path_factory.mktemp("live_filled") / "live.db"
    shutil.copy2(src, dst)
    conn = db.connect(str(dst))
    try:
        with db.write_tx(conn):
            schema.ensure_content_url(conn)
    finally:
        conn.close()
    return str(dst)


@pytest.fixture
def long_core(tmp_path) -> str:
    """Длинные пункты по всем платформам: сводка обязана обрезаться целиком."""
    path = str(tmp_path / "long.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    long_title = "Длинное описание пункта " + "x" * 180
    long_text = "Длинный текст поста " + "y" * 180
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO source(id, platform, external_id, handle, title, status) VALUES"
            " (1,'youtube','UC1','ytchan','Канал YouTube',NULL),"
            " (2,'x','x1','xa','X аккаунт',NULL),"
            " (3,'telegram','ch1','ch1','Канал TG 1','active'),"
            " (4,'telegram','ch2','ch2','Канал TG 2','active')")
        for i in range(5):
            cid = 100 + i
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,published_at,"
                "lang,title,url,kind) VALUES (?,?,?,?,?,?,?,?,'video')",
                (cid, 'youtube', 1, f"vid{i}", _iso(1), 'en', long_title,
                 f"https://www.youtube.com/watch?v=vid{i}"))
            conn.execute(
                "INSERT INTO metric_snapshot(content_id,captured_at,interval_quality,"
                "views,views_per_day,platform,external_id) VALUES (?,?,?,?,?,?,?)",
                (cid, _iso(0.5), 'ok', 60000 + i, 1000 - i, 'youtube', f"vid{i}"))
        for i in range(5):
            cid = 200 + i
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,published_at,"
                "text,author_handle,url,is_repost) VALUES (?,?,?,?,?,?,?,?,0)",
                (cid, 'x', 2, f"tw{i}", _iso(1), long_text, 'xa',
                 f"https://x.com/xa/status/tw{i}"))
            conn.execute(
                "INSERT INTO content_latest(content_id,likes,platform,external_id)"
                " VALUES (?,?,?,?)", (cid, 1000 - i, 'x', f"tw{i}"))
        tg_specs = [(3, 'ch1', 'ch1/10'), (3, 'ch1', 'ch1/11'), (3, 'ch1', 'ch1/12'),
                    (4, 'ch2', 'ch2/20'), (4, 'ch2', 'ch2/21')]
        for i, (src, _handle, ext) in enumerate(tg_specs):
            cid = 300 + i
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,published_at,"
                "text,url) VALUES (?,?,?,?,?,?,?)",
                (cid, 'telegram', src, ext, _iso(1), long_text,
                 f"https://t.me/{ext}"))
            conn.execute(
                "INSERT INTO score(content_id,computed_at,significance,platform,"
                "axes_json) VALUES (?,?,?,?,?)",
                (cid, _iso(0.5), float(10 - i), 'telegram', '{}'))
    conn.close()
    return path


# --------------------------------------------------------------------------- #
# Живая база (копия)
# --------------------------------------------------------------------------- #
def test_live_compact_fits_limit(live_copy):
    r = _run("report", "--compact", "--db", live_copy)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    # len(stdout) — знаки; отдельно проверяем байты (wc -c), чтобы уложиться и в
    # лимит Telegram, даже когда текст на кириллице (2 байта на знак).
    assert len(out) <= LIMIT
    assert len(out.encode("utf-8")) <= LIMIT


def test_live_compact_links_intact(live_copy):
    # Память выдачи (DIGEST_MEMORY_DAYS) НАМЕРЕННО показывает свежие материалы из
    # широкого пула, которых в полном отчёте нет в разделах 1–3, поэтому «каждая
    # ссылка сводки есть в полном отчёте» проверяем с выключенной памятью: тогда
    # сводка — подмножество полного отчёта, а проверяется целостность ссылок при
    # распределении бюджета (регресс обрезки URL по байтам). Локальная копия базы
    # заморожена фикстурой, поэтому прогон детерминирован.
    env = {"TUBER_DIGEST_MEMORY": "0"}
    compact = _run("report", "--compact", "--db", live_copy, env=env)
    full = _run("report", "--db", live_copy, env=env)
    assert compact.returncode == 0 and full.returncode == 0
    links = re.findall(r"https?://\S+", compact.stdout)
    assert links, "в сводке не оказалось ни одной ссылки"
    for link in links:
        assert link in full.stdout, f"ссылка отсутствует в полном отчёте целиком: {link}"


def test_live_compact_counts_hidden(live_copy):
    # При бюджете по умолчанию (12 000) сводка на живой базе влезает целиком,
    # поэтому счётчик «скрыто» проверяем на явно узком бюджете (--compact-limit):
    # TZ-Tuber ч.1 §1 поднял лимит именно чтобы позиции перестали скрываться.
    r = _run("report", "--compact", "--db", live_copy, "--compact-limit", "1500")
    assert r.returncode == 0, r.stderr
    assert _hidden_total(r.stdout) > 0
    # заголовок: окно, путь к базе, дата
    assert re.search(r"Окно: последние \d+ дней", r.stdout)
    assert live_copy in r.stdout
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", r.stdout)


# --------------------------------------------------------------------------- #
# Синтетика: обрезка целиком, пустая база, режимы CLI
# --------------------------------------------------------------------------- #
def test_long_report_trims_whole_items(long_core):
    conn = db.connect(long_core, readonly=True)
    try:
        # Явно узкий бюджет: на 12 000 все пункты влезают, а проверяем именно
        # механику обрезки целыми пунктами (ссылка и цифры не режутся).
        text = report.build_compact(conn, days=10, db_path=long_core, limit=1500)
        full = report.build(conn, days=10, db_path=long_core)
    finally:
        conn.close()
    assert len(text) <= LIMIT
    assert len(text.encode("utf-8")) <= 1500
    # Обрезка реально работает: счётчик скрытых больше нуля.
    assert _hidden_total(text) > 0
    # Ни одна ссылка не обрезана: каждая встречается в полном отчёте целиком.
    links = re.findall(r"https?://\S+", text)
    assert links
    for link in links:
        assert link in full


def test_compact_on_empty_db(tmp_path):
    path = str(tmp_path / "empty.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    try:
        text = report.build_compact(conn, days=10, db_path=path)
    finally:
        conn.close()
    assert len(text) <= LIMIT
    assert "сводка владельцу" in text
    assert "нет сюжетов с материалами разных платформ" in text
    assert "--save не запрошен" in text


def test_cli_compact_keeps_json_and_plain(long_core):
    plain = _run("report", "--db", long_core)
    assert plain.returncode == 0, plain.stderr
    assert "ПОДВАЛ" in plain.stdout
    assert "сводка владельцу" not in plain.stdout

    js = _run("report", "--db", long_core, "--json")
    assert js.returncode == 0, js.stderr
    payload = json.loads(js.stdout)
    assert set(payload) >= {"db", "days", "youtube", "x", "telegram", "cross_story"}

    comp = _run("report", "--db", long_core, "--compact")
    assert comp.returncode == 0, comp.stderr
    assert "сводка владельцу" in comp.stdout
    assert "ПОДВАЛ" not in comp.stdout
    assert len(comp.stdout) <= LIMIT


def test_compact_save_keeps_full_file(long_core, tmp_path):
    out = tmp_path / "report-compact-test.txt"
    r = _run("report", "--db", long_core, "--compact", "--save", "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert out.is_file()
    full = out.read_text(encoding="utf-8")
    assert "ПОДВАЛ" in full            # полный отчёт по-прежнему файлом
    assert "сводка владельцу" in r.stdout
    assert str(out) in r.stdout        # путь к полному файлу — в подвале сводки


def test_wrapper_delivers_compact():
    script = (config.ROOT / "scripts" / "common" / "tuber_report.sh").read_text(
        encoding="utf-8")
    assert "--compact" in script
    assert "--save" in script
    assert "ALERT Tuber report" in script


# --------------------------------------------------------------------------- #
# Бюджет сводки: значение и переопределение (ТЗ-Tuber ч.1 §1)
# --------------------------------------------------------------------------- #
def test_compact_limit_default_is_12000():
    assert report.DEFAULT_COMPACT_LIMIT == 12000
    assert report.COMPACT_LIMIT == 12000


def test_compact_limit_env_override(monkeypatch):
    monkeypatch.setenv("TUBER_COMPACT_LIMIT", "7777")
    assert report.compact_limit() == 7777
    # Явный аргумент сильнее переменной окружения.
    assert report.compact_limit(1234) == 1234


def test_compact_limit_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("TUBER_COMPACT_LIMIT", "не число")
    assert report.compact_limit() == report.DEFAULT_COMPACT_LIMIT
    monkeypatch.setenv("TUBER_COMPACT_LIMIT", "0")
    assert report.compact_limit() == report.DEFAULT_COMPACT_LIMIT


def test_cli_compact_limit_flag(long_core):
    r = _run("report", "--db", long_core, "--compact", "--compact-limit", "1500")
    assert r.returncode == 0, r.stderr
    assert len(r.stdout.encode("utf-8")) <= 1500
    assert "сводка владельцу" in r.stdout


def test_cli_compact_limit_env(long_core, monkeypatch):
    monkeypatch.setenv("TUBER_COMPACT_LIMIT", "1600")
    r = _run("report", "--db", long_core, "--compact")
    assert r.returncode == 0, r.stderr
    assert len(r.stdout.encode("utf-8")) <= 1600


# --------------------------------------------------------------------------- #
# Справедливое распределение мест (ТЗ-5-доп-3)
# --------------------------------------------------------------------------- #
def test_live_compact_shows_every_platform(live_copy):
    """На живой базе в сводке ≥1 пункт каждой платформы, КРОМЕ Telegram.

    Telegram-раздел в сводке не печатается вообще (ТЗ-38 §2.1, D-52).
    """
    conn = db.connect(live_copy, readonly=True)
    try:
        totals = _section_totals(conn, _iso(10))
        out = report.build_compact(conn, days=10, db_path=live_copy)
    finally:
        conn.close()
    assert totals["YouTube"] and totals["X"]
    assert totals["Telegram"] > 0, "в полном отчёте Telegram-данные должны быть"
    sections = _sections(out)
    names = ("YouTube", "X") + (("Telegram",)
                                if report.telegram_section_enabled() else ())
    for name in names:
        assert _shown(sections[name]) >= 1, (
            f"платформа {name} без пунктов (в полном отчёте {totals[name]}):\n{out}")
    if report.telegram_section_enabled():
        # ТЗ-A: флаг включён — раздел Telegram присутствует и наполнен.
        assert "Telegram" in out
    else:
        # ТЗ-38 §2.1 (дефолт OFF): Telegram-раздела в сводке нет.
        assert "Telegram" not in out
    assert "все пункты скрыты" not in out


def test_live_compact_counts_match_full_report(live_copy):
    """Показано + скрыто = число пунктов раздела в полном отчёте (живая база)."""
    conn = db.connect(live_copy, readonly=True)
    try:
        cutoff = _iso(10)
        totals = _section_totals(conn, cutoff)
        out = report.build_compact(conn, days=10, db_path=live_copy)
        full = report.build(conn, days=10, db_path=live_copy)
    finally:
        conn.close()
    full_sections = _sections(full)
    sections = _sections(out)
    names = ("YouTube", "X") + (("Telegram",)
                                if report.telegram_section_enabled() else ())
    for name in names:
        assert _shown(full_sections[name]) == totals[name], name
        shown, hidden = _shown(sections[name]), _hidden(sections[name])
        assert shown + hidden == totals[name], (
            f"{name}: показано {shown} + скрыто {hidden} != {totals[name]}")
    # Telegram в сводке есть только при включённом флаге (ТЗ-A); полный отчёт
    # держит раздел всегда (ТЗ-38 §2.1).
    assert _shown(full_sections["Telegram"]) == totals["Telegram"]
    if report.telegram_section_enabled():
        assert "Telegram" in out
    else:
        assert "Telegram" not in out


def test_compact_counts_agree_with_full_report(long_core):
    """Синтетика: сумма показано/скрыто по разделу сходится с полным отчётом."""
    conn = db.connect(long_core, readonly=True)
    try:
        compact = report.build_compact(conn, days=10, db_path=long_core)
        full = report.build(conn, days=10, db_path=long_core)
    finally:
        conn.close()
    csec, fsec = _sections(compact), _sections(full)
    names = ("YouTube", "X") + (("Telegram",)
                                if report.telegram_section_enabled() else ())
    for name in names:
        full_items = _shown(fsec[name])
        assert full_items > 0
        assert _shown(csec[name]) + _hidden(csec[name]) == full_items, name
    # Telegram-раздел в сводке появляется только при включённом флаге (ТЗ-A).
    assert _shown(fsec["Telegram"]) > 0
    if report.telegram_section_enabled():
        assert "Telegram" in compact
    else:
        assert "Telegram" not in compact


def test_compact_section_without_data_is_honest(tmp_path):
    """Раздел без данных печатает «нет данных», а не «все пункты скрыты»."""
    path = str(tmp_path / "only_x.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    with db.write_tx(conn):
        conn.execute("INSERT INTO source(id,platform,external_id,handle,title)"
                     " VALUES (2,'x','x1','xa','X аккаунт')")
        for i in range(3):
            cid = 200 + i
            conn.execute(
                "INSERT INTO content(id,platform,source_id,external_id,published_at,"
                "text,author_handle,is_repost) VALUES (?,?,?,?,?,?,?,0)",
                (cid, 'x', 2, f"tw{i}", _iso(1), "текст поста", 'xa'))
            conn.execute(
                "INSERT INTO content_latest(content_id,likes,platform,external_id)"
                " VALUES (?,?,?,?)", (cid, 100 - i, 'x', f"tw{i}"))
    try:
        text = report.build_compact(conn, days=10, db_path=path)
    finally:
        conn.close()
    assert "все пункты скрыты" not in text
    sections = _sections(text)
    assert _shown(sections["X"]) >= 1
    assert _shown(sections["YouTube"]) == 0
    assert "нет данных за окно" in "\n".join(sections["YouTube"])
    # Telegram-раздел в сводке отсутствует целиком (ТЗ-38 §2.1).
    assert "Telegram" not in text


def test_reserve_keeps_one_item_per_platform_under_small_budget(long_core):
    """Узкий бюджет: резерв даёт по пункту каждому разделу, ссылки целы."""
    conn = db.connect(long_core, readonly=True)
    try:
        text = report.build_compact(conn, days=10, db_path=long_core, limit=1400)
        full = report.build(conn, days=10, db_path=long_core)
    finally:
        conn.close()
    assert len(text.encode("utf-8")) <= 1400
    assert "все пункты скрыты" not in text
    sections = _sections(text)
    for name in ("YouTube", "X"):
        assert _shown(sections[name]) >= 1, (
            f"резерв не дал пункт разделу {name}:\n{text}")
    # Telegram-раздел в сводку не идёт (ТЗ-38 §2.1).
    assert "Telegram" not in text
    for link in re.findall(r"https?://\S+", text):
        assert link in full, f"ссылка из ужатой сводки обрезана: {link}"


# --------------------------------------------------------------------------- #
# ТЗ-A: возврат Telegram-раздела за флагом TUBER_TG_SECTION (Слой A)
# --------------------------------------------------------------------------- #
#: Заголовки разделов сводки (с ведущим номером) — для проверки нумерации.
_SECTION_HEAD_RE = re.compile(
    r"^\s*(\d+)\.\s+(YouTube — топ|X — топ|Telegram — топ постов|"
    r"Виральные — охват|Новое за сутки — вышедшее|Сквозной сюжет)")


def _section_numbers(text: str) -> list[int]:
    return [int(m.group(1)) for line in text.splitlines()
            if (m := _SECTION_HEAD_RE.match(line))]


@pytest.fixture
def tg_profile_core(tmp_path) -> str:
    """Активный Telegram-канал + candidate-канал с постами за окно (ТЗ-A §4.2.4)."""
    path = str(tmp_path / "tg_profile.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    with db.write_tx(conn):
        conn.execute(
            "INSERT INTO source(id, platform, external_id, handle, title, status) VALUES"
            " (1,'telegram','ai1','ai_chan','ИИ канал','active'),"
            " (2,'telegram','pol1','politics','Политика','candidate')")
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "text,url) VALUES"
            " (10,'telegram',1,'ai1',?,'активный ИИ пост','https://t.me/ai_chan/1'),"
            " (11,'telegram',2,'pol1',?,'запрещённый кандидат-пост','https://t.me/politics/1')",
            (_iso(1), _iso(1)))
        conn.execute(
            "INSERT INTO score(content_id,computed_at,significance,platform,axes_json)"
            " VALUES (10,?,9.0,'telegram','{}'),(11,?,8.0,'telegram','{}')",
            (_iso(0.5), _iso(0.5)))
    conn.close()
    return path


def test_tg_section_present_when_enabled(long_core, monkeypatch):
    """1. При TUBER_TG_SECTION=1 раздел Telegram присутствует и наполнен."""
    monkeypatch.setenv("TUBER_TG_SECTION", "1")
    conn = db.connect(long_core, readonly=True)
    try:
        out = report.build_compact(conn, days=10, db_path=long_core)
    finally:
        conn.close()
    assert "3. Telegram — топ постов за окно:" in out
    sections = _sections(out)
    assert _shown(sections["Telegram"]) >= 1, out


def test_tg_section_respects_budget(long_core, monkeypatch):
    """2. Бюджет не превышен с включённым Telegram и при узком лимите."""
    monkeypatch.setenv("TUBER_TG_SECTION", "1")
    conn = db.connect(long_core, readonly=True)
    try:
        wide = report.build_compact(conn, days=10, db_path=long_core)
        narrow = report.build_compact(conn, days=10, db_path=long_core, limit=1500)
    finally:
        conn.close()
    assert len(wide.encode("utf-8")) <= report.compact_limit()
    assert len(narrow.encode("utf-8")) <= 1500
    # Раздел при узком бюджете тоже на месте (резерв даёт минимум 1 пункт).
    assert "3. Telegram — топ постов за окно:" in narrow


def test_tg_section_numbering_contiguous(long_core, monkeypatch):
    """3. Номера разделов — ровно 1..N и с Telegram, и без него (ТЗ-39)."""
    conn = db.connect(long_core, readonly=True)
    try:
        monkeypatch.delenv("TUBER_TG_SECTION", raising=False)
        without = report.build_compact(conn, days=10, db_path=long_core)
        monkeypatch.setenv("TUBER_TG_SECTION", "1")
        with_tg = report.build_compact(conn, days=10, db_path=long_core)
    finally:
        conn.close()
    assert "Telegram" not in without
    nums_without = _section_numbers(without)
    nums_with = _section_numbers(with_tg)
    assert nums_without == list(range(1, len(nums_without) + 1)), nums_without
    assert nums_with == list(range(1, len(nums_with) + 1)), nums_with
    # Telegram добавляет ровно один раздел.
    assert len(nums_with) == len(nums_without) + 1


def test_tg_profile_only_active_channels(tg_profile_core, monkeypatch):
    """4. Профиль отобран: только посты active-каналов, candidate отсечён."""
    monkeypatch.setenv("TUBER_TG_SECTION", "1")
    conn = db.connect(tg_profile_core, readonly=True)
    try:
        out = report.build_compact(conn, days=10, db_path=tg_profile_core)
        full = report.build(conn, days=10, db_path=tg_profile_core)
    finally:
        conn.close()
    sections = _sections(out)
    tg_block = "\n".join(sections["Telegram"])
    assert "активный ИИ пост" in tg_block
    assert "кандидат-пост" not in tg_block
    assert "кандидат-пост" not in full
    assert "active" in tg_block.lower() or "ai_chan" in tg_block or "ИИ канал" in tg_block
