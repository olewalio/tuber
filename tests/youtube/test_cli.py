"""Тесты консольного входа (tuber.cli).

Сеть не используется. Боевая БД не затрагивается: все пути подменяются на
временные через фикстуру sandbox.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import time
from pathlib import Path

import pytest

from tuber.platforms.youtube import cli, config, store as db

# Настоящая load_env, снятая до автоподмены фикстурой.
_REAL_LOAD_ENV = config.load_env


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    """По умолчанию не читаем реальный /root/.hermes/.env в тестах."""
    monkeypatch.setattr(cli.config, "load_env", lambda *a, **k: 0)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Изолированные пути: БД, замок и лог во временной папке."""
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "tuber.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "tuber.log")
    return tmp_path


def _add_unclassified_video(path) -> None:
    """Положить в тестовую БД одно неразобранное видео."""
    conn = db.connect(path)
    db.init_db(conn)
    db.upsert_channel(
        conn, {"channel_id": "ch1", "title": "Канал", "first_seen": 1}
    )
    db.upsert_video(
        conn,
        {
            "video_id": "v1",
            "channel_id": "ch1",
            "title": "Тестовое видео",
            "description": "Описание",
            "duration_seconds": 600,
            "published_at": 1,
            "first_seen": 1,
        },
    )
    conn.close()


# --- базовый отчёт ---------------------------------------------------------


def test_report_text_runs(sandbox, capsys):
    rc = cli.main(["report", "--days", "10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ЕЖЕДНЕВНЫЙ ОТЧЁТ" in out


def test_report_json_is_single_valid_object(sandbox, capsys):
    """--json печатает ровно один JSON-объект и ничего больше."""
    rc = cli.main(["report", "--days", "10", "--json"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    data = json.loads(out)  # ничего лишнего — иначе парсинг упал бы
    assert data["command"] == "report"
    assert data["days"] == 10
    assert "report_text" in data
    assert set(data["snapshots"]) == {"total", "videos", "with_speed"}


# --- замок -----------------------------------------------------------------


def test_lock_busy_exits_zero(sandbox, capsys):
    """Если замок занят — код 0 и сообщение, без ошибок."""
    lock_path = sandbox / ".lock"
    fh = open(lock_path, "w")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = cli.main(["report"])
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    assert rc == 0
    captured = capsys.readouterr()
    assert "прогон уже идёт" in captured.out
    assert captured.err == ""


# --- код возврата при ошибке ----------------------------------------------


def test_error_returns_one(sandbox, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("сломалось нарочно")

    monkeypatch.setattr(cli.report, "build_report", boom)
    rc = cli.main(["report"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "сломалось нарочно" in err


# --- ключ из .env ----------------------------------------------------------


def test_command_works_without_env_var_but_with_dotenv(sandbox, monkeypatch):
    """Без DEEPSEEK_API_KEY в окружении, но с файлом .env команда работает."""
    _add_unclassified_video(sandbox / "tuber.db")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    env_file = sandbox / "creds.env"
    env_file.write_text("DEEPSEEK_API_KEY=from-file-key\n", encoding="utf-8")

    real_load_env = _REAL_LOAD_ENV
    monkeypatch.setattr(
        cli.config,
        "load_env",
        lambda *a, **k: real_load_env(str(env_file)),
    )

    # Сеть не трогаем: один вызов модели отдаёт пустой JSON-массив.
    seen: dict = {}

    def fake_chat(api_key, messages, session, model, timeout=60.0):
        seen["key"] = api_key
        return "[]", {}

    monkeypatch.setattr(cli.classify, "_chat", fake_chat)

    rc = cli.main(["classify", "--limit", "1"])
    assert rc == 0
    assert seen["key"] == "from-file-key"
    assert os.environ.get("DEEPSEEK_API_KEY") == "from-file-key"


def test_classify_without_key_returns_one(sandbox, monkeypatch, capsys):
    """Нет ключа ни в окружении, ни в .env — код 1 и причина в stderr."""
    _add_unclassified_video(sandbox / "tuber.db")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli.config, "load_env", lambda *a, **k: 0)

    rc = cli.main(["classify"])
    assert rc == 1
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


# --- прочие команды (сеть и YouTube замоканы) ------------------------------


def test_collect_and_snapshots_mocked(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.collect,
        "run_collect",
        lambda *a, **k: {
            "queries": 1,
            "queries_done": 1,
            "requests": 1,
            "found": 3,
            "new": 2,
            "channels_scanned": 0,
            "units": 103,
            "errors": [],
            "stopped_by_budget": False,
        },
    )
    monkeypatch.setattr(
        cli.schedule,
        "run_snapshots",
        lambda *a, **k: {
            "planned": 2,
            "captured": 2,
            "batches": 1,
            "failed_batches": 0,
            "errors": [],
        },
    )

    assert cli.main(["collect", "--queries", "1", "--no-classify"]) == 0
    assert "Сбор завершён" in capsys.readouterr().out
    assert cli.main(["snapshots", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["command"] == "snapshots"
    assert payload["captured"] == 2


def test_log_written(sandbox):
    cli.main(["report"])
    log_text = (sandbox / "tuber.log").read_text(encoding="utf-8")
    assert "ИТОГ report" in log_text


def test_daily_full_cycle_mocked(sandbox, monkeypatch, capsys):
    """Суточный цикл проходит целиком (сеть и модель замоканы)."""
    monkeypatch.setattr(
        cli.collect,
        "run_collect",
        lambda *a, **k: {
            "queries": 30, "queries_done": 30, "requests": 30, "found": 10,
            "new": 4, "channels_scanned": 1, "units": 3000, "errors": [],
            "stopped_by_budget": False,
        },
    )
    monkeypatch.setattr(
        cli.classify,
        "classify_videos",
        lambda *a, **k: {
            "requested": 4, "batches": 1, "classified": 4, "ai": 3, "not_ai": 1,
            "failed": 0, "topics": {"модели и релизы": 3}, "tokens_in": 10,
            "tokens_out": 5, "cost_usd": 0.001, "errors": [],
        },
    )
    monkeypatch.setattr(
        cli.schedule,
        "run_snapshots",
        lambda *a, **k: {
            "planned": 3, "captured": 3, "batches": 1, "failed_batches": 0,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        cli.expand,
        "run_expand",
        lambda *a, **k: {
            "dry_run": False, "budget": 2000, "sources": {"mention": 0},
            "probed": 2, "accepted": 1, "rejected": 1, "units": 5,
            "stopped_reason": None, "steps": [],
        },
    )

    assert cli.main(["daily", "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert "Суточный цикл" in out
    assert "ЕЖЕДНЕВНЫЙ ОТЧЁТ" in out


def _mock_daily_io(monkeypatch, order=None, seo_impl=None):
    """Замокать сетевые шаги суточного цикла.

    ``order`` — общий список, куда пишутся маркеры порядка вызовов.
    ``seo_impl`` — подмена ``seo.analyze`` (по умолчанию возвращает словарь).
    """
    def _mark(name):
        if order is not None:
            order.append(name)

    monkeypatch.setattr(
        cli.collect, "run_collect",
        lambda *a, **k: (_mark("collect"), {
            "queries": 1, "queries_done": 1, "requests": 1, "found": 1,
            "new": 1, "channels_scanned": 1, "units": 1, "errors": [],
            "stopped_by_budget": False,
        })[1],
    )
    monkeypatch.setattr(
        cli.classify, "classify_videos",
        lambda *a, **k: (_mark("classify"), {
            "requested": 1, "batches": 1, "classified": 1, "ai": 1, "not_ai": 0,
            "failed": 0, "topics": {}, "tokens_in": 1, "tokens_out": 1,
            "cost_usd": 0.0, "errors": [],
        })[1],
    )
    monkeypatch.setattr(
        cli.expand, "run_expand",
        lambda *a, **k: (_mark("expand"), {
            "dry_run": False, "budget": 1, "sources": {}, "probed": 0,
            "accepted": 0, "rejected": 0, "units": 0, "stopped_reason": None,
            "steps": [],
        })[1],
    )
    monkeypatch.setattr(
        cli.schedule, "run_snapshots",
        lambda *a, **k: (_mark("snapshots"), {
            "planned": 1, "captured": 1, "batches": 1, "failed_batches": 0,
            "errors": [],
        })[1],
    )
    if seo_impl is not None:
        monkeypatch.setattr(cli.seo, "analyze",
                            lambda *a, **k: (_mark("seo"), seo_impl(*a, **k))[1])


def _seo_stub(*a, **k):
    return {"videos": 5, "written": 2, "skipped": 3, "no_title": 0}


def test_daily_calls_seo_analyze(sandbox, monkeypatch, capsys):
    """Суточный цикл обязан вызвать seo.analyze."""
    calls = []
    _mock_daily_io(
        monkeypatch,
        seo_impl=lambda *a, **k: (calls.append(1), _seo_stub())[1],
    )

    assert cli.main(["daily", "--days", "3"]) == 0
    assert len(calls) == 1


def test_daily_json_has_seo_key(sandbox, monkeypatch, capsys):
    """В JSON-сводке daily есть ключ seo с результатом разбора."""
    _mock_daily_io(monkeypatch, seo_impl=_seo_stub)

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["seo"]["written"] == 2
    assert payload["seo"]["videos"] == 5


def test_daily_seo_failure_does_not_break_cycle(sandbox, monkeypatch, capsys):
    """Падение SEO-разбора не роняет цикл: код 0, ключ error, шаги выполнены."""
    done = []
    _mock_daily_io(monkeypatch, order=done)

    def boom(*a, **k):
        raise RuntimeError("seo сломался")

    monkeypatch.setattr(cli.seo, "analyze", boom)

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" in payload["seo"]
    assert "seo сломался" in payload["seo"]["error"]
    assert payload["collect"]["new"] == 1
    assert payload["snapshots"]["captured"] == 1


def test_daily_seo_runs_before_report(sandbox, monkeypatch, capsys):
    """Порядок: seo.analyze вызван до report.build_report."""
    order = []
    _mock_daily_io(monkeypatch, order=order, seo_impl=_seo_stub)
    monkeypatch.setattr(
        cli.report, "build_report",
        lambda *a, **k: (order.append("report"), "ИТОГ")[1],
    )

    assert cli.main(["daily", "--days", "3"]) == 0
    assert "seo" in order
    assert "report" in order
    assert order.index("seo") < order.index("report")


def _report_files(sandbox):
    """Все файлы отчётов новой схемы в порядке имён."""
    reports_dir = sandbox / "reports"
    if not reports_dir.is_dir():
        return []
    return sorted(reports_dir.glob("report-*.txt"))


def _report_path(sandbox):
    files = _report_files(sandbox)
    assert len(files) == 1
    return files[0]


def test_daily_saves_report_file(sandbox, monkeypatch, capsys):
    """Суточный цикл сохраняет отчёт в reports/report-ДАТА_ВРЕМЯ.txt (МСК)."""
    _mock_daily_io(monkeypatch)

    assert cli.main(["daily", "--days", "3"]) == 0
    capsys.readouterr()

    files = _report_files(sandbox)
    assert len(files) == 1
    assert re.fullmatch(r"report-\d{4}-\d{2}-\d{2}_\d{4}\.txt", files[0].name)
    assert config.now_msk().strftime("%Y-%m-%d") in files[0].name
    assert "ЕЖЕДНЕВНЫЙ ОТЧЁТ" in files[0].read_text(encoding="utf-8")


def test_daily_report_file_path_and_line(sandbox, monkeypatch, capsys):
    """Путь отчёта есть в JSON-сводке и строкой «Отчёт:» в текстовом выводе."""
    _mock_daily_io(monkeypatch)

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    expected = str(_report_path(sandbox))
    assert payload["report_file"] == expected

    assert cli.main(["daily", "--days", "3"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("Отчёт: ")]
    assert len(lines) == 1
    second = lines[0][len("Отчёт: "):]
    assert second.startswith(str(sandbox / "reports"))
    assert Path(second).exists()


def test_daily_report_file_failure_does_not_break_cycle(sandbox, monkeypatch, capsys):
    """Сбой записи файла отчёта не роняет цикл: код 0 и ключ report_file_error."""
    _mock_daily_io(monkeypatch)

    def boom(*a, **k):
        raise OSError("диск полон")

    monkeypatch.setattr(cli, "save_run_report", boom)

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "диск полон" in payload["report_file_error"]
    assert payload["collect"]["new"] == 1
    assert payload["snapshots"]["captured"] == 1


def _count_scores(path) -> int:
    """Число строк в video_scores временной БД."""
    conn = db.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM video_scores").fetchone()[0]
    finally:
        conn.close()


def test_daily_fills_video_scores(sandbox, monkeypatch, capsys):
    """daily сам наполняет video_scores (реальные seo.analyze и refresh_scores)."""
    _add_unclassified_video(cli.DB_PATH)
    _mock_daily_io(monkeypatch)  # сетевые шаги замоканы, SEO-шаги настоящие

    assert cli.main(["daily", "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert "SEO-скоры: записано" in out
    assert _count_scores(cli.DB_PATH) == 1


def test_daily_scores_are_idempotent(sandbox, monkeypatch, capsys):
    """Повторный daily не дублирует строки video_scores."""
    _add_unclassified_video(cli.DB_PATH)
    _mock_daily_io(monkeypatch)

    assert cli.main(["daily", "--days", "3"]) == 0
    capsys.readouterr()
    first = _count_scores(cli.DB_PATH)
    assert first == 1

    assert cli.main(["daily", "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert _count_scores(cli.DB_PATH) == first
    assert "SEO-скоры: записано 0" in out
    assert "пропущено (уже есть) 1" in out


def test_daily_scores_failure_does_not_break_cycle(sandbox, monkeypatch, capsys):
    """Сбой refresh_scores не роняет цикл: код 0, строка о сбое, шаги живы."""
    _mock_daily_io(monkeypatch, seo_impl=_seo_stub)

    def boom(*a, **k):
        raise RuntimeError("скоры сломались")

    monkeypatch.setattr(cli.seo, "refresh_scores", boom)

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "скоры сломались" in payload["seo_scores"]["error"]
    assert payload["collect"]["new"] == 1
    assert payload["snapshots"]["captured"] == 1


def test_daily_scores_failure_phrase_in_header(sandbox, monkeypatch, capsys):
    """В текстовом отчёте сбой SEO-скоров виден и не мешает остальным строкам."""
    _mock_daily_io(monkeypatch, seo_impl=_seo_stub)
    monkeypatch.setattr(
        cli.seo, "refresh_scores",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("нет таблицы")),
    )

    assert cli.main(["daily", "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert "SEO-скоры: сбой (нет таблицы), цикл продолжен" in out
    assert "Суточный цикл" in out
    assert "ЕЖЕДНЕВНЫЙ ОТЧЁТ" in out


# --- миграция шортсов и формат отчёта --------------------------------------


def _add_shorts_pool(path) -> None:
    conn = db.connect(path)
    db.init_db(conn)
    db.upsert_channel(conn, {"channel_id": "ch1", "title": "Канал", "first_seen": 1})
    for vid, dur in (("a", 90), ("b", 200)):
        db.upsert_video(
            conn,
            {
                "video_id": vid,
                "channel_id": "ch1",
                "title": f"Видео {vid}",
                "duration_seconds": dur,
                "is_shorts": 0,
                "published_at": 1,
                "first_seen": 1,
            },
        )
    conn.close()


def test_migrate_shorts_command(sandbox, capsys):
    _add_shorts_pool(sandbox / "tuber.db")
    rc = cli.main(["migrate-shorts", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert data["command"] == "migrate-shorts"
    assert data["changed"] == 1  # 90 с стал шортсом, 200 с остался полным
    assert data["is_shorts_before"] == 0
    assert data["is_shorts_after"] == 1


def test_report_format_sections(sandbox, capsys):
    cli.main(["report", "--format", "short"])
    out = capsys.readouterr().out
    assert "## Шортсы" in out
    assert "## Полные видео" not in out

    cli.main(["report", "--format", "all"])
    out_all = capsys.readouterr().out
    assert "## Шортсы" in out_all
    assert "## Полные видео" in out_all


# --- расширение поиска (этап 9) ---------------------------------------------


def test_expand_dry_run_no_network(sandbox, capsys):
    rc = cli.main(["expand", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Расширение" in out
    assert "dry-run" in out


def test_expand_json_single_object(sandbox, capsys):
    rc = cli.main(["expand", "--dry-run", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["command"] == "expand"
    assert payload["dry_run"] is True
    assert payload["units"] == 0


# --- сбор комментариев -----------------------------------------------------


def test_comments_command_json(sandbox, monkeypatch, capsys):
    """Команда comments печатает один JSON-объект со сводкой."""
    seen = {}

    def fake_run(conn, cfg=config, limit=None):
        seen["limit"] = limit
        return {"videos": 2, "comments": 5, "skipped": 1, "errors": 0, "units": 2}

    monkeypatch.setattr(cli.comments, "run", fake_run)
    rc = cli.main(["comments", "--limit", "7", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["command"] == "comments"
    assert payload["videos"] == 2
    assert payload["comments"] == 5
    assert payload["units"] == 2
    assert seen["limit"] == 7


def test_comments_command_text(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.comments, "run",
        lambda conn, cfg=config, limit=None: {
            "videos": 3, "comments": 9, "skipped": 0, "errors": 0, "units": 3,
        },
    )
    rc = cli.main(["comments", "--limit", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "разобрано видео 3" in out
    assert "записано строк 9" in out
    assert "3 units" in out


# --- D-06 / D-09: каталог отчётов и ротация ---------------------------------


def test_save_run_report_name_has_msk_datetime(sandbox):
    """Имя файла отчёта содержит дату и время по МСК (UTC+3)."""
    when = datetime.datetime(2026, 9, 13, 15, 42, tzinfo=config.MSK_TZ)
    path = cli.save_run_report("текст", when=when)
    assert path.endswith(os.path.join("reports", "report-2026-09-13_1542.txt"))
    assert Path(path).read_text(encoding="utf-8") == "текст"


def test_save_run_report_same_minute_versions(sandbox):
    """Повтор в ту же минуту не затирает файл: появляется суффикс -2."""
    when = datetime.datetime(2026, 9, 13, 15, 42, tzinfo=config.MSK_TZ)
    p1 = cli.save_run_report("первый", when=when)
    p2 = cli.save_run_report("второй", when=when)
    assert p1 != p2
    assert p1.endswith("report-2026-09-13_1542.txt")
    assert p2.endswith("report-2026-09-13_1542-2.txt")
    assert Path(p1).read_text(encoding="utf-8") == "первый"
    assert Path(p2).read_text(encoding="utf-8") == "второй"


def test_daily_two_runs_produce_two_files(sandbox, monkeypatch, capsys):
    """Два прогона подряд дают два файла, первый не затёрт."""
    _mock_daily_io(monkeypatch)

    assert cli.main(["daily", "--days", "3"]) == 0
    capsys.readouterr()
    first = _report_files(sandbox)[0]
    first_text = first.read_text(encoding="utf-8")

    assert cli.main(["daily", "--days", "3"]) == 0
    capsys.readouterr()

    files = _report_files(sandbox)
    assert len(files) == 2
    assert first.exists()
    assert first.read_text(encoding="utf-8") == first_text


def test_rotate_moves_old_keeps_fresh(sandbox):
    """Старый файл уходит в archive/, свежий остаётся на месте."""
    reports = sandbox / "reports"
    reports.mkdir()
    old = reports / "report-2026-01-01_0000.txt"
    fresh = reports / "report-2099-01-01_0000.txt"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    past = time.time() - 200 * 86400
    os.utime(old, (past, past))

    moved = cli.rotate_reports(reports, 180)
    assert moved == 1
    assert not old.exists()
    assert fresh.exists()
    archived = reports / "archive" / old.name
    assert archived.exists()
    assert archived.read_text(encoding="utf-8") == "old"


def test_rotate_leaves_manual_reports_in_place(sandbox):
    """ТЗ-33: маска с датой — ручные report-*.txt ротация не трогает."""
    reports = sandbox / "reports"
    reports.mkdir()
    dated = reports / "report-2026-09-01_0700.txt"
    dated.write_text("daily", encoding="utf-8")
    past = time.time() - 200 * 86400
    os.utime(dated, (past, past))
    # Ручной отчёт без даты рядом с суточными и в каталоге data/.
    manual_in_reports = reports / "report-all.txt"
    manual_in_reports.write_text("manual", encoding="utf-8")
    manual_legacy = sandbox / "report-long.txt"
    manual_legacy.write_text("manual", encoding="utf-8")

    moved = cli.rotate_reports(reports, 180)

    assert moved == 1
    assert not dated.exists()
    assert (reports / "archive" / dated.name).exists()
    assert manual_in_reports.exists()
    assert manual_legacy.exists()


def test_rotate_carries_legacy_reports_once(sandbox):
    """Остатки старой схемы из data/ уезжают в архив один раз."""
    legacy = sandbox / "report-2026-01-01.txt"
    legacy.write_text("legacy", encoding="utf-8")
    reports = sandbox / "reports"

    assert cli.rotate_reports(reports, 180) == 1
    assert not legacy.exists()
    archived = reports / "archive" / legacy.name
    assert archived.exists()
    assert archived.read_text(encoding="utf-8") == "legacy"

    # Повторная ротация безопасна: переносить больше нечего.
    assert cli.rotate_reports(reports, 180) == 0


def test_daily_rotates_reports_and_reports_count(sandbox, monkeypatch, capsys):
    """daily уносит старое в архив и пишет число перенесённого в сводку."""
    _mock_daily_io(monkeypatch)
    legacy = sandbox / "report-2020-01-01.txt"
    legacy.write_text("legacy", encoding="utf-8")

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["report_rotated"] == 1
    assert not legacy.exists()
    assert (sandbox / "reports" / "archive" / legacy.name).exists()


def test_daily_rotates_nothing_when_all_fresh(sandbox, monkeypatch, capsys):
    """Без старых файлов счётчик ротации равен нулю."""
    _mock_daily_io(monkeypatch)

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["report_rotated"] == 0


# --- единая сборка сводки суточного цикла (ТЗ-35, п. 2) --------------------


def _daily_summary():
    """Сводка через единый сборщик: аргументы-заглушки, без сети и БД."""
    return cli.build_daily_summary(
        3,
        collect={"new": 1},
        classify={"classified": 1, "ai": 1},
        expand={"accepted": 0},
        snapshots={"captured": 1},
        seo={"written": 0},
        seo_scores={"scored": 0},
        thumbs={"done": 0},
        comments={"videos": 2, "comments": 5, "units": 2},
        report_file="/tmp/report-test.txt",
        report_rotated=1,
    )


def test_build_daily_summary_carries_report_and_comments():
    """В сводке есть все четыре ключа, путь отчёта и блок комментариев.

    Обёртка tuber_daily.sh читает из stdout именно report_file,
    report_file_error, report_rotated и comments — значит, они обязаны быть
    в том же словаре, что уходит в лог.
    """
    summary = _daily_summary()
    for key in ("report_file", "report_file_error", "report_rotated", "comments"):
        assert key in summary, key
    assert summary["report_file"] == "/tmp/report-test.txt"
    assert summary["report_file_error"] is None
    assert summary["comments"] == {"videos": 2, "comments": 5, "units": 2}
    assert summary["report_rotated"] == 1


def test_build_daily_summary_error_replaces_report_file():
    """При сбое записи report_file пуст, а причина лежит в report_file_error."""
    summary = cli.build_daily_summary(
        3,
        collect={}, classify={}, expand={}, snapshots={}, seo={},
        seo_scores={}, thumbs={}, comments={"error": "сбой"},
        report_file=None, report_rotated=0,
        report_file_error="диск полон",
    )
    assert summary["report_file"] is None
    assert summary["report_file_error"] == "диск полон"
    assert "comments" in summary
    assert "report_rotated" in summary


def test_daily_json_always_has_report_and_comments_keys(sandbox, monkeypatch,
                                                        capsys):
    """В JSON-сводке daily все четыре ключа есть даже без сбоев.

    На fdc4186 ключа report_file_error в успешном прогоне не было.
    """
    _mock_daily_io(monkeypatch)

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    for key in ("report_file", "report_file_error", "report_rotated", "comments"):
        assert key in payload, key
    assert payload["report_file_error"] is None
    assert payload["report_file"].startswith(str(sandbox / "reports"))


def test_daily_json_report_file_missing_when_write_fails(sandbox, monkeypatch,
                                                         capsys):
    """При сбое записи ключ report_file присутствует и равен None."""
    _mock_daily_io(monkeypatch)

    def boom(*a, **k):
        raise OSError("диск полон")

    monkeypatch.setattr(cli, "save_run_report", boom)

    assert cli.main(["daily", "--days", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["report_file"] is None
    assert "диск полон" in payload["report_file_error"]


# --- виральность: код возврата отчёта и защита viral-refresh ---------------


def _seed_viral_video(path):
    """Канал из трёх видео с парой замеров и скором — кандидаты на окне отчёта.

    Три видео нужны, чтобы медиана канала (база нормировки) вообще считалась.
    """
    conn = db.connect(path)
    db.init_db(conn)
    now = config.now_ts()
    db.upsert_channel(conn, {"channel_id": "ch1", "title": "Канал", "first_seen": 1})
    for vid, likes in (("v1", 40), ("v2", 4), ("v3", 4)):
        db.upsert_video(conn, {
            "video_id": vid, "channel_id": "ch1", "title": f"Видео {vid}",
            "duration_seconds": 600, "published_at": now - 86400, "first_seen": 1,
        })
        db.save_classification(conn, vid, is_ai=1, topic="модели и релизы",
                               lang="ru", confidence=0.9, classified_at=now)
        db.insert_snapshot(conn, vid, now - 7200, "h", 1000, 1, 1)
        db.insert_snapshot(conn, vid, now - 3600, "h", 5000, likes, 5)
        db.save_score(conn, vid, now, outlier_score=4.0,
                      likes_per_1000=float(likes), packaging_score=50.0)
    conn.close()


def test_report_nonzero_when_index_missing(sandbox, capsys):
    """Кандидаты есть, индекса нет — предупреждение и код 1 (п.2.4)."""
    _seed_viral_video(cli.DB_PATH)
    rc = cli.main(["report", "--days", "10", "--format", "long"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ВНИМАНИЕ: индекс виральности не посчитан" in out


def test_viral_refresh_then_report_ok(sandbox, capsys):
    """После viral-refresh индекс есть, отчёт успешен и показывает вклад осей."""
    _seed_viral_video(cli.DB_PATH)
    assert cli.main(["viral-refresh"]) == 0
    capsys.readouterr()
    rc = cli.main(["report", "--days", "10", "--format", "long"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "вклад осей" in out


def test_viral_refresh_rejects_production_without_flag(sandbox, monkeypatch, capsys):
    """Рабочая БД защищена: без --allow-production запись запрещена."""
    working = config.TUBER_DIR / "data" / "tuber.db"
    monkeypatch.setattr(cli, "DB_PATH", working)
    rc = cli.main(["viral-refresh"])
    assert rc == 2
    assert "рабочая БД" in capsys.readouterr().out
