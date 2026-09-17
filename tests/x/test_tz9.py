"""ТЗ-9: запускалки Hermes — настоящие файлы-шимы вместо симлинков.

Проверяется:
  * установщик кладёт в каталог планировщика НАСТОЯЩИЕ исполняемые файлы
    (не симлинки), ровно десять, только по известным именам;
  * для тиров A/B/C шим передаёт тир явным аргументом;
  * `TUBER_X_LAUNCHER_DRYRUN=1` печатает ровно одну строку с командой и не
    запускает ничего;
  * прежние симлинки под нашими именами снимаются, посторонние файлы целы;
  * повторный запуск идемпотентен; в crontab ничего не пишется.

Сеть и рабочая БД не используются.
"""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts", "x")
INSTALLER = os.path.join(SCRIPTS, "install_hermes_cron.sh")

# имя шима -> (обёртка проекта, аргумент тира или "")
EXPECTED = {
    "tuber_x_collect_a.sh": ("tuber_x_collect.sh", "A"),
    "tuber_x_collect_b.sh": ("tuber_x_collect.sh", "B"),
    "tuber_x_collect_c.sh": ("tuber_x_collect.sh", "C"),
    "tuber_x_enrich.sh": ("tuber_x_enrich.sh", ""),
    "tuber_x_fulltext.sh": ("tuber_x_fulltext.sh", ""),
    "tuber_x_classify.sh": ("tuber_x_classify.sh", ""),
    "tuber_x_scores.sh": ("tuber_x_scores.sh", ""),
    "tuber_x_cross_stories.sh": ("tuber_x_cross_stories.sh", ""),
    "tuber_x_report.sh": ("tuber_x_report.sh", ""),
    "tuber_x_synd.sh": ("tuber_x_synd.sh", ""),
    "tuber_x_health.sh": ("tuber_x_health.sh", ""),
    "tuber_x_discover.sh": ("tuber_x_discover.sh", ""),
}


def _expected_cmd(project, name):
    wrapper, tier = EXPECTED[name]
    cmd = f"exec {project}/scripts/x/{wrapper}"
    return f"{cmd} {tier}" if tier else cmd


def _make_project(project):
    scripts = project / "scripts" / "x"
    scripts.mkdir(parents=True)
    for wrapper, _ in EXPECTED.values():
        f = scripts / wrapper
        f.write_text("#!/usr/bin/env bash\nexit 0\n")
        f.chmod(0o755)


def _run_installer(tmp_path, project, install_dir):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "crontab_called"
    crontab = bindir / "crontab"
    crontab.write_text('#!/usr/bin/env bash\nprintf invoked >> "$CRONTAB_MARKER"\n')
    crontab.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["TUBER_X_PROJECT"] = str(project)
    env["HERMES_SCRIPTS_DIR"] = str(install_dir)
    env["CRONTAB_MARKER"] = str(marker)
    proc = subprocess.run([INSTALLER], capture_output=True, text=True, env=env)
    return proc, marker


def test_installer_writes_ten_real_executable_shims(tmp_path):
    project = tmp_path / "proj"
    _make_project(project)
    install = tmp_path / "hermes_scripts"
    install.mkdir()

    proc, marker = _run_installer(tmp_path, project, install)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), "crontab трогать нельзя"

    found = sorted(p.name for p in install.iterdir())
    assert found == sorted(EXPECTED), f"лишние/недостающие файлы: {found}"
    for name in EXPECTED:
        p = install / name
        assert not p.is_symlink(), f"{name} — симлинк, а должен быть файлом"
        assert p.is_file()
        assert os.access(p, os.X_OK), f"{name} без +x"
        assert _expected_cmd(project, name) in p.read_text(encoding="utf-8")
    assert "шимы установлены: 12 (симлинков: 0)" in proc.stdout


def test_dryrun_prints_command_without_running(tmp_path):
    project = tmp_path / "proj"
    _make_project(project)
    install = tmp_path / "hermes_scripts"
    install.mkdir()
    assert _run_installer(tmp_path, project, install)[0].returncode == 0

    # ловушка: если dryrun что-то исполнит, обёртка оставит след
    marker = tmp_path / "wrapper_ran"
    wrapper = project / "scripts" / "x" / "tuber_x_collect.sh"
    wrapper.write_text(f'#!/usr/bin/env bash\ntouch {marker}\n')

    env = dict(os.environ)
    env["TUBER_X_LAUNCHER_DRYRUN"] = "1"
    for name in ("tuber_x_collect_a.sh", "tuber_x_collect_b.sh", "tuber_x_collect_c.sh"):
        r = subprocess.run([str(install / name)], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert r.stdout == _expected_cmd(project, name) + "\n", r.stdout
    assert not marker.exists(), "dryrun не должен запускать сбор"


def test_installer_removes_old_symlinks_and_keeps_foreign_files(tmp_path):
    project = tmp_path / "proj"
    _make_project(project)
    install = tmp_path / "hermes_scripts"
    install.mkdir()
    # старый симлинк на постороннюю цель под нашим именем
    elsewhere = tmp_path / "elsewhere.sh"
    elsewhere.write_text("#!/usr/bin/env bash\nexit 0\n")
    elsewhere.chmod(0o755)
    os.symlink(elsewhere, install / "tuber_x_collect_b.sh")
    foreign = install / "signalstream_health.py"
    foreign.write_text("чужое — не трогать", encoding="utf-8")

    proc, _ = _run_installer(tmp_path, project, install)
    assert proc.returncode == 0, proc.stderr
    assert not (install / "tuber_x_collect_b.sh").is_symlink()
    assert foreign.read_text(encoding="utf-8") == "чужое — не трогать"
    assert foreign.exists()


def test_installer_idempotent_second_run(tmp_path):
    project = tmp_path / "proj"
    _make_project(project)
    install = tmp_path / "hermes_scripts"
    install.mkdir()
    assert _run_installer(tmp_path, project, install)[0].returncode == 0
    first = {p.name: p.read_bytes() for p in install.iterdir()}
    proc2, marker = _run_installer(tmp_path, project, install)
    assert proc2.returncode == 0, proc2.stderr
    assert not marker.exists()
    second = {p.name: p.read_bytes() for p in install.iterdir()}
    assert first == second
    assert "шимы установлены: 12 (симлинков: 0)" in proc2.stdout


def test_installer_never_writes_crontab():
    with open(INSTALLER, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            assert not stripped.startswith("crontab ")
            assert "| crontab" not in stripped
