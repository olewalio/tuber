"""ТЗ-11: дискавери в расписании, осмысленная проверка роста реестра.

Проверяется:
  * обёртка `scripts/tuber_x_discover.sh`: молчит в норме, даёт короткую сводку
    при изменении состояния, печатает ALERT при отказе, не повторяет прогон при
    исчерпанной квоте (в журнал — `skipped: quota exhausted`, код 0);
  * голова сторожa: `check_discover_freshness` (ALERT) и `check_registry_growth`
    (WARN только при реальном простое реестра);
  * установщик: в тестовом режиме печатает 11 заданий, включая дискавери с
    расписанием `50 4 * * *`, боевой каталог не трогает.

Сеть и рабочая БД не используются: python3 подменяется заглушкой, БД — фикстура.
"""
import os
import subprocess
from datetime import timedelta

from tuber.platforms.x import store as db, health

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts", "x")
DISCOVER = os.path.join(SCRIPTS, "tuber_x_discover.sh")
INSTALLER = os.path.join(SCRIPTS, "install_hermes_cron.sh")


# =========================================================== заглушка python3
def _make_fake_python3(bindir):
    """python3-заглушка: heredoc-скрипту отдаёт FAKE_CONFIG, CLI — FAKE_*."""
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "python3"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-" ]; then\n'
        '  payload="$(cat)"\n'
        '  case "$payload" in\n'
        '    *budget_remaining*) printf "%b\\n" "${FAKE_CONFIG:-120\\n30\\n120}" ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf \'%s\\n\' "$@" >> "$TUBER_X_RECORD"\n'
        'if [ -n "${FAKE_DISCOVER_OUT:-}" ]; then printf \'%s\' "$FAKE_DISCOVER_OUT"; fi\n'
        'exit "${FAKE_DISCOVER_RC:-0}"\n'
    )
    script.chmod(0o755)


def _run_wrapper(tmp_path, *, fake_config=None, discover_out="", discover_rc=0):
    bindir = tmp_path / "bin"
    record = tmp_path / "record.txt"
    _make_fake_python3(bindir)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    logdir = tmp_path / "logs"
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["TUBER_X_PROJECT"] = str(project)
    env["TUBER_X_LOG_DIR"] = str(logdir)
    env["TUBER_X_RECORD"] = str(record)
    # ТЗ-3c: интерпретатор закреплён ЯВНО (не по PATH) — подменяем его.
    env["TUBER_PYTHON"] = str(bindir / "python3")
    env["FAKE_DISCOVER_OUT"] = discover_out
    env["FAKE_DISCOVER_RC"] = str(discover_rc)
    env["FAKE_CONFIG"] = fake_config if fake_config is not None else "120\n30\n120"
    proc = subprocess.run([DISCOVER], cwd=str(project), capture_output=True,
                          text=True, env=env)
    rec = record.read_text(encoding="utf-8") if record.exists() else ""
    log = (logdir / "tuber_x_discover.log")
    log_text = log.read_text(encoding="utf-8") if log.exists() else ""
    return proc, rec, log_text


NORMAL_OUT = ("=== discover lang=all budget=120\n"
              "запросов=5 страниц=5 постов=100 кандидатов_найдено=0\n"
              "верификация: проверено=0 active=0 provisional=0 rejected=0"
              " в_стоп-лист=0\n")


# ============================================ Задача 1: поведение обёртки
def test_wrapper_silent_on_normal_run(tmp_path):
    proc, rec, log = _run_wrapper(tmp_path, discover_out=NORMAL_OUT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"в норме stdout должен быть пуст: {proc.stdout!r}"
    # CLI вызван с явными потолками из конфига (120 / 30)
    assert rec.splitlines() == ["-m", "tuber", "x", "discover",
                                "--budget", "120", "--max-verify", "30"], rec
    assert "кандидатов_найдено=0" in log


def test_wrapper_summary_on_state_change(tmp_path):
    out = ("=== discover lang=all budget=120\n"
           "запросов=40 страниц=12 постов=800 кандидатов_найдено=7\n"
           "верификация: проверено=7 active=0 provisional=3 rejected=2"
           " в_стоп-лист=2\n")
    proc, _, _ = _run_wrapper(tmp_path, discover_out=out)
    assert proc.returncode == 0, proc.stderr
    assert "кандидатов_найдено=7" in proc.stdout
    assert "проверено=7" in proc.stdout and "provisional=3" in proc.stdout


def test_wrapper_alert_on_failure(tmp_path):
    proc, _, log = _run_wrapper(tmp_path, discover_out="", discover_rc=1)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert "отказ" in proc.stdout.lower()


def test_wrapper_skips_when_quota_exhausted(tmp_path):
    proc, rec, log = _run_wrapper(tmp_path, fake_config="120\n30\n0")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "исчерпанная квота — не изменение состояния"
    assert rec == "", "при исчерпанной квоте CLI дискавери запускать нельзя"
    assert "skipped: quota exhausted" in log


# ============================================ Задача 3: проверки сторожа
def _run_mode(con, mode, *, hours_ago=0.0, finished=True, errors=0):
    started = db.iso(db.utcnow() - timedelta(hours=hours_ago))
    fin = db.iso(db.utcnow()) if finished else None
    con.execute("INSERT INTO runs (started_at, finished_at, mode, errors)"
                " VALUES (?,?,?,?)", (started, fin, mode, errors))
    con.commit()


def test_discover_freshness_alert_without_any_run(con):
    res = health.check_discover_freshness(con)
    assert res["alert"] is True and res["severity"] == "ALERT"


def test_discover_freshness_alert_when_40h_stale(con):
    _run_mode(con, "discover:all:120", hours_ago=40)
    res = health.check_discover_freshness(con)
    assert res["alert"] is True
    assert "40" in res["msg"] or "не запускался" in res["msg"]


def test_discover_freshness_alert_on_failed_run(con):
    _run_mode(con, "discover:all:120", hours_ago=2, finished=True, errors=1)
    assert health.check_discover_freshness(con)["alert"] is True
    _run_mode(con, "discover:all:120", hours_ago=1, finished=False)
    assert health.check_discover_freshness(con)["alert"] is True


def test_discover_freshness_ok_recent_success(con):
    _run_mode(con, "discover:all:120", hours_ago=2)
    res = health.check_discover_freshness(con)
    assert res["alert"] is False


def test_registry_growth_warn_when_stalled_7d(con):
    res = health.check_registry_growth(con)
    assert res["alert"] is True and res["severity"] == "WARN"
    assert res["active_48h"] == 0  # справочная строка, не тревога


def test_registry_growth_silent_on_new_provisional(con):
    con.execute("INSERT INTO candidates (handle, validated, verified_at, first_seen_at)"
                " VALUES ('x', 'provisional', ?, ?)",
                (db.utcnow_iso(), db.utcnow_iso()))
    con.commit()
    assert health.check_registry_growth(con)["alert"] is False


def test_registry_growth_silent_on_new_candidate(con):
    con.execute("INSERT INTO candidates (handle, validated, first_seen_at)"
                " VALUES ('y', NULL, ?)", (db.utcnow_iso(),))
    con.commit()
    assert health.check_registry_growth(con)["alert"] is False


def test_registry_growth_stall_only_counts_within_window(con):
    old = db.iso(db.utcnow() - timedelta(days=30))
    con.execute("INSERT INTO candidates (handle, validated, first_seen_at, verified_at)"
                " VALUES ('z', 'provisional', ?, ?)", (old, old))
    con.commit()
    # единственный provisional и кандидат — 30 суток назад: вне окна 7 суток
    assert health.check_registry_growth(con)["alert"] is True


def test_no_warn_for_zero_active(con):
    """ТЗ-11 задача 3: ноль новых active — норма, а не WARN."""
    _run_mode(con, "discover:all:120", hours_ago=2)
    con.execute("INSERT INTO candidates (handle, first_seen_at) VALUES ('n', ?)",
                (db.utcnow_iso(),))
    con.commit()
    res = health.run(con)
    growth = [c for c in res["checks"] if c["name"] == "registry_growth"][0]
    assert growth["alert"] is False
    assert growth["active_48h"] == 0


# ============================================ Задача 2: установщик
def _run_installer(tmp_path, project, install_dir, *, dryrun=True, env_extra=None):
    env = dict(os.environ)
    env["TUBER_X_PROJECT"] = str(project)
    env["HERMES_SCRIPTS_DIR"] = str(install_dir)
    if dryrun:
        env["TUBER_X_INSTALL_DRYRUN"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run([INSTALLER], capture_output=True, text=True, env=env)


def _make_project_scripts(project):
    scripts = project / "scripts" / "x"
    scripts.mkdir(parents=True)
    for wrapper in ("tuber_x_collect.sh", "tuber_x_enrich.sh", "tuber_x_fulltext.sh",
                    "tuber_x_classify.sh", "tuber_x_scores.sh",
                    "tuber_x_cross_stories.sh", "tuber_x_report.sh",
                    "tuber_x_synd.sh", "tuber_x_health.sh", "tuber_x_discover.sh"):
        f = scripts / wrapper
        f.write_text("#!/usr/bin/env bash\nexit 0\n")
        f.chmod(0o755)


def test_installer_test_mode_prints_discover_job_and_touches_nothing(tmp_path):
    project = tmp_path / "proj"
    _make_project_scripts(project)
    install = tmp_path / "hermes_scripts"  # намеренно НЕ создаём
    proc = _run_installer(tmp_path, project, install, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    # боевой каталог планировщика не создан и не тронут
    assert not install.exists(), "тестовый режим не должен создавать каталог"
    # ровно одно задание дискавери, расписание 50 4 * * *
    job_disc = [ln for ln in proc.stdout.splitlines()
                if "50 4 * * *" in ln and "tuber_x_discover.sh" in ln]
    assert len(job_disc) == 1, job_disc
    assert "расширение реестра (дискавери)" in job_disc[0]
    # всего 12 заданий в списке
    job_lines = [ln for ln in proc.stdout.splitlines()
                 if ln.startswith("  ") and ".sh" in ln and "*" in ln]
    assert len(job_lines) == 12, job_lines


def test_installer_test_mode_idempotent_one_discover_job(tmp_path):
    project = tmp_path / "proj"
    _make_project_scripts(project)
    install = tmp_path / "hermes_scripts"
    p1 = _run_installer(tmp_path, project, install, dryrun=True)
    p2 = _run_installer(tmp_path, project, install, dryrun=True)
    assert p1.returncode == 0 and p2.returncode == 0
    assert p1.stdout == p2.stdout, "два прогона тестового режима должны совпасть"
    assert sum(1 for ln in p2.stdout.splitlines()
               if "50 4 * * *" in ln and "tuber_x_discover.sh" in ln) == 1
