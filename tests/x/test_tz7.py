"""ТЗ-7: запускалки расписания для планировщика Hermes.

Задача 1 — обёртка `tuber_x_collect.sh` определяет тир по имени файла, если
аргумент не передан; аргумент имеет приоритет; без того и другого — код 2.

Задача 2 — `scripts/install_hermes_cron.sh` кладёт в каталог планировщика
десять символических ссылок, идемпотентен, посторонних файлов не трогает и
ничего не пишет в crontab.

Сеть и рабочая БД не используются: обёртки запускаются с подменённым
`python3` (записывает аргументы) и подменёнными корнями проекта/каталога.
"""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts", "x")
COLLECT = os.path.join(SCRIPTS, "tuber_x_collect.sh")
INSTALLER = os.path.join(SCRIPTS, "install_hermes_cron.sh")

# имя запускалки -> обёртка проекта, на которую она должна указывать
WRAPPER_FOR = {
    "tuber_x_collect_a.sh": "tuber_x_collect.sh",
    "tuber_x_collect_b.sh": "tuber_x_collect.sh",
    "tuber_x_collect_c.sh": "tuber_x_collect.sh",
    "tuber_x_enrich.sh": "tuber_x_enrich.sh",
    "tuber_x_fulltext.sh": "tuber_x_fulltext.sh",
    "tuber_x_classify.sh": "tuber_x_classify.sh",
    "tuber_x_scores.sh": "tuber_x_scores.sh",
    "tuber_x_cross_stories.sh": "tuber_x_cross_stories.sh",
    "tuber_x_report.sh": "tuber_x_report.sh",
    "tuber_x_synd.sh": "tuber_x_synd.sh",
    "tuber_x_health.sh": "tuber_x_health.sh",
    "tuber_x_discover.sh": "tuber_x_discover.sh",
    "tuber_x_followers.sh": "tuber_x_followers.sh",
}

# явный аргумент тира, который шим обязан передать (ТЗ-9); "" — без аргумента
TIER_FOR = {
    "tuber_x_collect_a.sh": "A",
    "tuber_x_collect_b.sh": "B",
    "tuber_x_collect_c.sh": "C",
    "tuber_x_enrich.sh": "",
    "tuber_x_fulltext.sh": "",
    "tuber_x_classify.sh": "",
    "tuber_x_scores.sh": "",
    "tuber_x_cross_stories.sh": "",
    "tuber_x_report.sh": "",
    "tuber_x_synd.sh": "",
    "tuber_x_health.sh": "",
    "tuber_x_discover.sh": "",
    "tuber_x_followers.sh": "",
}


def _expected_cmd(project, name):
    cmd = f"exec {project}/scripts/x/{WRAPPER_FOR[name]}"
    tier = TIER_FOR[name]
    return f"{cmd} {tier}" if tier else cmd


# ------------------------------------------------------------------ helpers
def _make_fake_python3(bindir):
    """python3-заглушка: дописывает свои аргументы в файл из TUBER_X_RECORD."""
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "python3"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" >> "$TUBER_X_RECORD"\n'
    )
    script.chmod(0o755)


def _run_collect(tmp_path, invoked_name, args=()):
    """Запустить обёртку под именем `invoked_name` (через ссылку) с аргументами."""
    bindir = tmp_path / "bin"
    record = tmp_path / "record.txt"
    _make_fake_python3(bindir)
    link = tmp_path / invoked_name
    os.symlink(COLLECT, link)
    (tmp_path / "proj").mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["TUBER_X_PROJECT"] = str(tmp_path / "proj")
    env["TUBER_X_LOG_DIR"] = str(tmp_path / "logs")
    env["TUBER_X_RECORD"] = str(record)
    # ТЗ-3c: интерпретатор закреплён ЯВНО (не по PATH) — подменяем его.
    env["TUBER_PYTHON"] = str(bindir / "python3")
    proc = subprocess.run([str(link), *args], capture_output=True, text=True, env=env)
    rec = record.read_text(encoding="utf-8") if record.exists() else ""
    return proc, rec


def _tier_from_rec(rec):
    """Вытащить значение --tier из записанной команды python-заглушки."""
    argv = rec.splitlines()
    idx = argv.index("--tier")
    return argv[idx + 1]


# ============================================ Задача 1: тир из имени файла
def test_collect_tier_comes_from_filename_suffix(tmp_path):
    for suffix, tier in (("a", "A"), ("b", "B"), ("c", "C")):
        case = tmp_path / suffix
        case.mkdir()
        proc, rec = _run_collect(case, f"tuber_x_collect_{suffix}.sh")
        assert proc.returncode == 0, proc.stderr
        argv = rec.splitlines()
        assert argv[:4] == ["-m", "tuber", "x", "collect"], rec
        assert _tier_from_rec(rec) == tier, f"{suffix}: {rec!r}"


def test_collect_tier_uppercase_suffix_also_works(tmp_path):
    case = tmp_path / "up"
    case.mkdir()
    proc, rec = _run_collect(case, "tuber_x_collect_B.sh")
    assert proc.returncode == 0, proc.stderr
    assert _tier_from_rec(rec) == "B"


def test_collect_argument_has_priority_over_filename(tmp_path):
    # файл назван _c, но аргумент B обязан победить
    case = tmp_path / "prio"
    case.mkdir()
    proc, rec = _run_collect(case, "tuber_x_collect_c.sh", args=("B",))
    assert proc.returncode == 0, proc.stderr
    assert _tier_from_rec(rec) == "B", rec

    # регистр аргумента не важен
    case2 = tmp_path / "prio2"
    case2.mkdir()
    proc2, rec2 = _run_collect(case2, "tuber_x_collect_a.sh", args=("b",))
    assert proc2.returncode == 0, proc2.stderr
    assert _tier_from_rec(rec2) == "B", rec2


def _argv_value(rec, flag):
    argv = rec.splitlines()
    return argv[argv.index(flag) + 1]


def test_collect_pacing_defaults_per_tier(tmp_path):
    """ТЗ-13: у каждого тира свои дефолты interval/max_accounts."""
    expected = {"A": ("12", "40"), "B": ("24", "15"), "C": ("24", "10")}
    for suffix, (interval, maximum) in expected.items():
        case = tmp_path / f"pace_{suffix}"
        case.mkdir()
        proc, rec = _run_collect(case, f"tuber_x_collect_{suffix.lower()}.sh")
        assert proc.returncode == 0, proc.stderr
        assert _argv_value(rec, "--min-interval-hours") == interval, rec
        assert _argv_value(rec, "--max-accounts") == maximum, rec


def test_collect_pacing_env_override(tmp_path):
    case = tmp_path / "override"
    case.mkdir()
    bindir = tmp_path / "bin2"
    record = tmp_path / "record2.txt"
    _make_fake_python3(bindir)
    link = case / "tuber_x_collect_a.sh"
    os.symlink(COLLECT, link)
    (case / "proj").mkdir()
    env = dict(os.environ)
    env["TUBER_X_PROJECT"] = str(case / "proj")
    env["TUBER_X_LOG_DIR"] = str(case / "logs")
    env["TUBER_X_RECORD"] = str(record)
    env["TUBER_PYTHON"] = str(bindir / "python3")
    env["TUBER_X_MIN_INTERVAL_H"] = "6"
    env["TUBER_X_MAX_ACCOUNTS"] = "7"
    proc = subprocess.run([str(link)], capture_output=True, text=True, env=env)
    rec = record.read_text(encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    assert _argv_value(rec, "--min-interval-hours") == "6", rec
    assert _argv_value(rec, "--max-accounts") == "7", rec


def test_collect_max_accounts_zero_or_empty_means_no_cap(tmp_path):
    case = tmp_path / "nocap"
    case.mkdir()
    bindir = tmp_path / "bin3"
    record = tmp_path / "record3.txt"
    _make_fake_python3(bindir)
    link = case / "tuber_x_collect_a.sh"
    os.symlink(COLLECT, link)
    (case / "proj").mkdir()
    for value in ("0", ""):
        record.unlink(missing_ok=True)
        env = dict(os.environ)
        env["TUBER_X_PROJECT"] = str(case / "proj")
        env["TUBER_X_LOG_DIR"] = str(case / "logs")
        env["TUBER_X_RECORD"] = str(record)
        env["TUBER_PYTHON"] = str(bindir / "python3")
        env["TUBER_X_MAX_ACCOUNTS"] = value
        proc = subprocess.run([str(link)], capture_output=True, text=True, env=env)
        rec = record.read_text(encoding="utf-8") if record.exists() else ""
        assert proc.returncode == 0, proc.stderr
        assert "--max-accounts" not in rec.splitlines(), f"value={value!r}: {rec!r}"
        assert "--min-interval-hours" in rec.splitlines(), rec


def test_collect_dry_args_does_not_call_python(tmp_path):
    case = tmp_path / "dry"
    case.mkdir()
    link = case / "tuber_x_collect_a.sh"
    os.symlink(COLLECT, link)
    env = dict(os.environ)
    env["TUBER_X_DRY_ARGS"] = "1"
    env["TUBER_PYTHON"] = "/bin/echo"  # только проверка существования
    out = subprocess.run(
        [str(link), "A"], capture_output=True, text=True, env=env
    )
    assert out.returncode == 0, out.stderr
    assert "--tier A" in out.stdout, out.stdout
    assert "--min-interval-hours 12" in out.stdout, out.stdout
    assert "--max-accounts 40" in out.stdout, out.stdout


def test_collect_without_arg_and_without_suffix_is_error_2(tmp_path):
    case = tmp_path / "none"
    case.mkdir()
    proc, rec = _run_collect(case, "tuber_x_collect.sh")
    assert proc.returncode == 2, f"ожидался код 2, получен {proc.returncode}"
    assert rec == "", "при ошибке CLI запускать нельзя"
    assert "тир" in proc.stderr.lower(), proc.stderr


def test_collect_invalid_argument_is_error_2(tmp_path):
    case = tmp_path / "bad"
    case.mkdir()
    proc, rec = _run_collect(case, "tuber_x_collect_a.sh", args=("Z",))
    assert proc.returncode == 2
    assert rec == ""
    assert "тир" in proc.stderr.lower(), proc.stderr


# ==================================== Задача 2: установщик запускалок Hermes
def _make_project(project):
    scripts = project / "scripts" / "x"
    scripts.mkdir(parents=True)
    for wrapper in set(WRAPPER_FOR.values()):
        f = scripts / wrapper
        f.write_text("#!/usr/bin/env bash\nexit 0\n")
        f.chmod(0o755)
    return scripts


def _run_installer(tmp_path, project, install_dir):
    """Запустить установщик с подменёнными корнями и ловушкой на crontab."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "crontab_called"
    crontab = bindir / "crontab"
    crontab.write_text(
        "#!/usr/bin/env bash\n"
        'printf invoked >> "$CRONTAB_MARKER"\n'
    )
    crontab.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["TUBER_X_PROJECT"] = str(project)
    env["HERMES_SCRIPTS_DIR"] = str(install_dir)
    env["CRONTAB_MARKER"] = str(marker)
    proc = subprocess.run([INSTALLER], capture_output=True, text=True, env=env)
    return proc, marker


def test_installer_creates_exactly_ten_real_shims_idempotently(tmp_path):
    project = tmp_path / "proj"
    _make_project(project)
    install = tmp_path / "hermes_scripts"
    install.mkdir()
    foreign = install / "someone_else.sh"
    foreign.write_text("посторонний файл — не трогать", encoding="utf-8")

    proc1, marker = _run_installer(tmp_path, project, install)
    assert proc1.returncode == 0, proc1.stderr
    assert not marker.exists(), "установщик не должен вызывать crontab"

    names = sorted(p.name for p in install.iterdir() if p.name.startswith("tuber_x_"))
    assert names == sorted(WRAPPER_FOR), f"неверный набор запускалок: {names}"
    assert len(names) == 13

    for p in install.iterdir():
        if not p.name.startswith("tuber_x_"):
            continue
        # настоящий файл, не симлинк (ТЗ-9)
        assert not p.is_symlink(), f"{p.name} осталась симлинком"
        assert p.is_file(), f"{p.name} не обычный файл"
        assert os.access(p, os.X_OK), f"{p.name} без права на исполнение"
        text = p.read_text(encoding="utf-8")
        assert _expected_cmd(project, p.name) in text, f"{p.name}: {text!r}"

    # имена, которые печатает установщик для планировщика
    for name in WRAPPER_FOR:
        assert name in proc1.stdout
    assert "шимы установлены: 13 (симлинков: 0)" in proc1.stdout

    # --- повторный запуск: идемпотентность и сохранность посторонних файлов
    first = {p.name: p.read_text(encoding="utf-8") for p in install.iterdir()
             if p.name.startswith("tuber_x_")}
    proc2, marker2 = _run_installer(tmp_path, project, install)
    assert proc2.returncode == 0, proc2.stderr
    assert not marker2.exists()
    second = {p.name: p.read_text(encoding="utf-8") for p in install.iterdir()
              if p.name.startswith("tuber_x_")}
    assert first == second, "повторный запуск изменил шимы"
    assert sorted(second) == sorted(WRAPPER_FOR)
    assert foreign.read_text(encoding="utf-8") == "посторонний файл — не трогать"
    assert foreign.exists()


def test_installer_dryrun_prints_exact_command_for_tiers(tmp_path):
    project = tmp_path / "proj"
    _make_project(project)
    install = tmp_path / "hermes_scripts"
    install.mkdir()

    proc, _ = _run_installer(tmp_path, project, install)
    assert proc.returncode == 0, proc.stderr

    env = dict(os.environ)
    env["TUBER_X_LAUNCHER_DRYRUN"] = "1"
    for name, tier in (("tuber_x_collect_a.sh", "A"),
                       ("tuber_x_collect_b.sh", "B"),
                       ("tuber_x_collect_c.sh", "C")):
        r = subprocess.run([str(install / name)], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert r.stdout == _expected_cmd(project, name) + "\n", r.stdout


def test_installer_replaces_only_its_own_names(tmp_path):
    project = tmp_path / "proj"
    _make_project(project)
    install = tmp_path / "hermes_scripts"
    install.mkdir()
    # заранее лежит СИМЛИНК на что-то другое под нашим именем — обязан быть снят
    # и заменён настоящим файлом
    elsewhere = tmp_path / "elsewhere.sh"
    elsewhere.write_text("#!/usr/bin/env bash\nexit 0\n")
    elsewhere.chmod(0o755)
    stale = install / "tuber_x_health.sh"
    os.symlink(elsewhere, stale)
    other = install / "keep_me.sh"
    other.write_text("чужое", encoding="utf-8")

    proc, _ = _run_installer(tmp_path, project, install)
    assert proc.returncode == 0, proc.stderr
    assert not stale.is_symlink(), "прежний симлинк не снят"
    assert stale.is_file()
    assert _expected_cmd(project, "tuber_x_health.sh") in stale.read_text(encoding="utf-8")
    assert not other.is_symlink() and other.read_text(encoding="utf-8") == "чужое"


def test_installer_script_never_writes_crontab():
    with open(INSTALLER, encoding="utf-8") as fh:
        text = fh.read()
    assert "install_hermes_cron" in text
    # строки-команды crontab быть не должно (упоминания в комментариях — можно)
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("crontab "), f"установщик пишет в crontab: {line}"
        assert "| crontab" not in stripped, f"установщик пишет в crontab: {line}"
