"""ТЗ-6: предфильтр по умолчанию выключен, обёртки расписания, ключ модели,
аудит отката рабочей БД. Сеть не используется, БД — временная (conftest).
"""
import os
import subprocess
import sys

import pytest

from tuber.platforms.x import classify, config, store as db, report
from tuber.platforms.x.cli import build_parser

from tests.x.test_tz3 import acc, post, FakeModel
from tests.x.test_tz5 import _acc, _story
from tests.x.mocking import mark_ai

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts", "x")


# ============================================= ТЗ-6 задача 1: предфильтр
def test_prefilter_off_by_default_sends_all_to_model(con):
    """Без флага ни один пост не помечается heuristic (ТЗ-6 задача 1)."""
    a = acc(con, "a")
    post(con, a, "1", "Totally unrelated note about weather and lunch", handle="a")
    post(con, a, "2", "Another non-technical remark about coffee", handle="a")
    fake = FakeModel()
    s = classify.run(con, client=fake, check_budget=False)  # use_prefilter по умолчанию
    assert s["heuristic"] == 0, "предфильтр обязан быть выключен по умолчанию"
    assert s["classified"] == 2
    assert fake.calls >= 1, "все посты должны уйти к модели"
    statuses = {r["status"] for r in con.execute("SELECT status FROM classified")}
    assert statuses == {"classified"}
    assert con.execute("SELECT COUNT(*) FROM classified WHERE status='heuristic'"
                       ).fetchone()[0] == 0


def test_prefilter_flag_still_marks_heuristic(con):
    """С явным use_prefilter=True режим прежний: heuristic, не classified."""
    a = acc(con, "a")
    post(con, a, "1", "Totally unrelated note about weather and lunch", handle="a")
    fake = FakeModel()
    s = classify.run(con, client=fake, check_budget=False, use_prefilter=True)
    assert s["heuristic"] == 1 and s["classified"] == 0 and fake.calls == 0
    row = con.execute("SELECT * FROM classified WHERE tweet_id='1'").fetchone()
    assert row["status"] == "heuristic"


def test_cli_prefilter_flag_is_opt_in():
    p = build_parser()
    args = p.parse_args(["classify"])
    assert args.prefilter is False
    args = p.parse_args(["classify", "--prefilter"])
    assert args.prefilter is True
    with pytest.raises(SystemExit):
        p.parse_args(["classify", "--no-prefilter"])  # прежний флаг убран


# ===================================== ТЗ-6 задача 2: обёртки и ключ модели
def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_classify_and_report_wrappers_exist_and_are_executable():
    for name in ("tuber_x_classify.sh", "tuber_x_report.sh"):
        path = os.path.join(SCRIPTS, name)
        assert os.path.exists(path), f"нет обёртки {name}"
        assert os.access(path, os.X_OK), f"обёртка {name} не исполняемая"


def test_wrappers_load_key_from_env_file_without_printing():
    for name in ("tuber_x_classify.sh", "tuber_x_report.sh"):
        text = _read(os.path.join(SCRIPTS, name))
        assert "DEEPSEEK_API_KEY" in text
        assert "/root/.hermes/.env" in text
        # значение ключа не печатается ни в stdout, ни в лог
        for bad in ('echo "$DEEPSEEK_API_KEY"', "echo $DEEPSEEK_API_KEY",
                    'printf \'%s\' "$DEEPSEEK_API_KEY"'):
            assert bad not in text


def test_wrappers_are_silent_in_stdout_and_log_to_file():
    for name in ("tuber_x_classify.sh", "tuber_x_report.sh"):
        text = _read(os.path.join(SCRIPTS, name))
        assert '>>"$LOG"' in text, "вывод должен уходить в журнал, а не в stdout"
        assert "/root/.hermes/logs/tuber_x_" in text


def test_classify_wrapper_respects_daily_cap_by_default():
    """Без явного лимита CLI берёт остаток CLASSIFY_DAILY_CAP."""
    text = _read(os.path.join(SCRIPTS, "tuber_x_classify.sh"))
    assert "CLASSIFY_DAILY_CAP" in text
    # лимит не подставляется жёстко, если аргумент не передан
    assert 'py -m' not in text
    assert 'if [ -n "$LIMIT" ]' in text


def test_scores_wrapper_runs_stories_before_scores():
    text = _read(os.path.join(SCRIPTS, "tuber_x_scores.sh"))
    assert text.index("x stories") < text.index("x scores")


# ============================================ ТЗ-6 задача 3: расписание
def test_schedule_md_full_chain_order_and_notes():
    text = _read(os.path.join(ROOT, "docs", "x", "docs", "schedule.md"))
    chain = "collect → enrich → fulltext → classify → scores → report → health"
    assert chain in text, "нет полной цепочки в правильном порядке"
    assert "МСК" in text, "не сказано, что время московское"
    assert "Hermes" in text, "не сказано, что расписание ставит планировщик Hermes"
    assert "CLASSIFY_DAILY_CAP" in text, "не оговорён дневной потолок classify"
    assert "нет данных за сутки" in text, "не оговорён честный пустой отчёт"
    # порядок шагов внутри самой цепочки
    idx = [chain.index(step) for step in
           ("collect", "enrich", "fulltext", "classify", "scores", "report", "health")]
    assert idx == sorted(idx), "шаги цепочки перечислены не по порядку"


# ============================================ ТЗ-6 задача 4: качество отчёта
def test_report_runs_even_with_no_posts_and_creates_file(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_DIR", str(tmp_path))
    text = report.build(con, date="2026-09-15", translator=False)
    for header in ("1. Главное за сутки", "2. По темам", "3. Деньги и запуски",
                   "4. Новинки и инструменты", "5. Русскоязычный срез",
                   "6. Тёмные лошадки и первые авторы", "7. Служебный блок"):
        assert header in text, f"нет блока: {header}"
    assert report.NO_DATA in text, "пустой день обязан честно писать «нет данных»"
    path = report.write(text, date="2026-09-15")
    assert os.path.exists(path) and os.path.getsize(path) > 0


def test_report_no_duplicate_kind_patterns(con):
    a = _acc(con, "author")
    _story(con, a, "m1", topic="инвестиции и раунды", subtopic="раунд Sugar",
           claim_type="funding", topics=["инвестиции и раунды"])
    _story(con, a, "m2", topic="релизы моделей", subtopic="запуск локальные модели",
           claim_type="release", topics=["релизы моделей"])
    text = report.build(con, date="2026-09-15", translator=False)
    assert "Раунд: раунд" not in text
    assert "Запуск: запуск" not in text
    assert "Раунд: Sugar" in text
    assert "Запуск: локальные модели" in text


# ==================================== ТЗ-6 задача 5: защита рабочей БД
def test_db_guard_refuses_production_path():
    with pytest.raises(RuntimeError):
        db.ensure_not_production_db(config.DB_PATH, "тест")
    assert db.is_production_db(config.DB_PATH) is True
    assert db.is_production_db(os.path.join(config.DATA_DIR, "copy.db")) is False
    # копия проходит без исключения
    assert db.ensure_not_production_db("/tmp/tuber_x_copy.db", "тест")


def test_audit_finds_no_writeback_or_rollback_paths():
    sys.path.insert(0, ROOT)
    from scripts.acceptance import x_audit_writeback as audit_writeback
    res = audit_writeback.scan()
    assert res["hazards"] == [], f"найдены опасные пути: {res['hazards']}"
    assert res["files_scanned"] > 10


def test_repo_has_no_restore_scripts():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.splitlines()
    bad = [f for f in tracked
           if any(t in os.path.basename(f).lower() for t in ("restore", "откат", "rollback"))]
    assert bad == [], f"в репозитории есть скрипты отката: {bad}"


# ==================================== ТЗ-6: починка ежедневной цепочки (stories)
def test_stories_cli_does_not_crash_and_reports_dry_run(con, monkeypatch):
    """Регресс: `cli stories` падал с KeyError 'dry_run' и ломал цепочку."""
    from tuber.platforms.x import cli
    a = acc(con, "a")
    post(con, a, "1", "OpenAI released GPT-5 model for developers", handle="a")
    con.close()
    rc = cli.main(["stories", "--limit", "5"])
    assert rc == 0
    rc_dry = cli.main(["stories", "--limit", "5", "--dry-run"])
    assert rc_dry == 0


def test_stories_dry_run_writes_nothing(con):
    from tuber.platforms.x import stories
    a = acc(con, "a")
    post(con, a, "1", "OpenAI released GPT-5 model for developers", handle="a")
    mark_ai(con)
    s = stories.run(con, dry_run=True)
    assert s["dry_run"] is True and s["stories"] >= 1
    assert con.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM story_posts").fetchone()[0] == 0


# TODO(debt-D-53): закрыт в ТЗ-52 — тест переписан на детерминированный вход
# (фиксированный якорь времени + фиксированный набор материалов), см. TECH-DEBT.md.
def test_report_translates_and_caches_in_report_texts(con):
    """Провенанс перевода: report_texts пополняется при переводе главного сюжета.

    D-53: тест НЕ зависит от живой базы и текущей даты. Якорь ``stories.run``
    фиксирован (иначе окно кластеризации уезжает от материалов), а ``report.build``
    вызывается на ту же дату.
    """
    from datetime import datetime, timezone
    from tuber.platforms.x import report as rp
    from tuber.platforms.x import stories
    # Якорь — вечер зафиксированного дня: окно кластеризации (72 ч) накрывает
    # посты 2026-09-15, и сюжет попадает именно в суточное окно отчёта.
    anchored_now = datetime(2026, 9, 15, 23, 0, tzinfo=timezone.utc)
    a = acc(con, "first", tier="A")
    b = acc(con, "second", tier="A")
    post(con, a, "t1", "OpenAI ships a new agent toolkit for developers",
         handle="first", published="2026-09-15T05:00:00")
    post(con, b, "t2", "OpenAI ships a new agent toolkit for developers now",
         handle="second", published="2026-09-15T06:00:00")
    mark_ai(con)
    stories.run(con, now=anchored_now)

    class FakeTranslator:
        def __init__(self):
            self.calls = 0

        def translate(self, text_hash, text):
            self.calls += 1
            con.execute(
                "INSERT OR REPLACE INTO report_texts (text_hash, ru, model,"
                " created_at, src) VALUES (?,?,?,?,'model')",
                (text_hash, "Открытый ИИ выпустил агентский набор", "fake", "2026-09-15"))
            con.commit()
            return "Открытый ИИ выпустил агентский набор"

        def close(self):
            pass

    t = FakeTranslator()
    out = rp.build(con, date="2026-09-15", translator=t)
    assert "Открытый ИИ выпустил агентский набор" in out
    assert t.calls == 1
    assert con.execute("SELECT COUNT(*) FROM report_texts").fetchone()[0] == 1
