"""Оборона от перекрытия ежечасных прогонов «Tuber-x: полный текст».

Проверяем поведение обёртки ``scripts/x/tuber_x_fulltext.sh`` без сети и без
боевой базы (``TUBER_PYTHON`` подменён скриптом-заглушкой, корни проекта и
журнала — временные):

  * при занятом локе второй прогон выходит кодом 0, пишет в журнал строку
    ``fulltext skipped: параллельный прогон уже идёт`` и НЕ вызывает CLI;
  * при занятом локе stdout пуст (тихая обёртка);
  * норма — тишина в stdout, код 0, CLI вызван один раз;
  * лок освобождается по завершении прогона (два последовательных прогона
    проходят оба);
  * обрыв по внутреннему ``timeout`` (``TUBER_X_FULLTEXT_TIMEOUT``) трактуется
    как СБОЙ: строка ``ALERT`` в stdout, запись ``fulltext timeout`` в журнал;
  * ненулевой код и пустой вывод тоже дают ALERT, а не «успех».
"""
from __future__ import annotations

import fcntl
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "x", "tuber_x_fulltext.sh")

# Заглушка вместо TUBER_PYTHON: фиксирует вызов CLI, умеет «спать» (обрыв по
# timeout), печатать подготовленный вывод и отдавать заданный код возврата.
FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_X_RECORD"
if [ -n "${FAKE_FULLTEXT_SLEEP:-}" ]; then sleep "$FAKE_FULLTEXT_SLEEP"; fi
if [ -n "${FAKE_FULLTEXT_OUT:-}" ]; then printf '%s\\n' "$FAKE_FULLTEXT_OUT"; fi
exit "${FAKE_FULLTEXT_RC:-0}"
"""

NORMAL_OUT = "нужно=1012 цель_в_очереди=1012 аккаунтов=179 nitter_ok=177 x_ssr=2"


def _run(tmp_path, *, rc="0", out=NORMAL_OUT, sleep="", timeout=None):
    """Запустить обёртку с подменённым интерпретатором. Возвращает (proc, rec, log)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.txt"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    logdir = tmp_path / "logs"
    logdir.mkdir(exist_ok=True)
    log_path = logdir / "tuber_x_fulltext.log"
    env = dict(os.environ)
    env.update({
        "TUBER_PYTHON": str(fake),
        "TUBER_X_PROJECT": str(project),
        "TUBER_X_LOG_DIR": str(logdir),
        "TUBER_X_RECORD": str(record),
        "FAKE_FULLTEXT_RC": rc,
        "FAKE_FULLTEXT_OUT": out,
        "FAKE_FULLTEXT_SLEEP": sleep,
    })
    if timeout is not None:
        env["TUBER_X_FULLTEXT_TIMEOUT"] = str(timeout)
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    rec = record.read_text(encoding="utf-8") if record.exists() else ""
    return proc, rec, log


def test_lock_held_skips_without_cli_and_is_silent(tmp_path):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    lock_path = logdir / "tuber_x_fulltext.lock"
    # Держим лок так же, как `flock -n 9` в обёртке: flock(2) через fcntl.
    with open(lock_path, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc, rec, log = _run(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "", "при занятом локе stdout обязан быть пуст"
        assert "fulltext skipped: параллельный прогон уже идёт" in log, log
        assert rec == "", "при занятом локе CLI звать нельзя"
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def test_normal_run_is_silent_and_calls_cli_once(tmp_path):
    proc, rec, log = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "в норме обёртка обязана молчать"
    assert "ALERT" not in proc.stdout
    assert rec.splitlines() == ["-m", "tuber", "x", "fulltext"], rec
    assert "fulltext start" in log
    assert "fulltext done rc=0" in log
    assert NORMAL_OUT in log


def test_lock_is_released_after_run(tmp_path):
    # Два последовательных прогона в ОДНОМ каталоге журнала (тот же файл лока):
    # если бы лок не освобождался, второй прогон был бы пропущен как
    # «параллельный». Запись вызовов и журнал сбрасываем, чтобы наблюдать
    # именно второй прогон.
    proc1, rec1, _log1 = _run(tmp_path)
    (tmp_path / "record.txt").unlink()
    (tmp_path / "logs" / "tuber_x_fulltext.log").unlink()
    proc2, rec2, log2 = _run(tmp_path)
    assert proc1.returncode == 0 and proc2.returncode == 0
    assert rec1.splitlines() == ["-m", "tuber", "x", "fulltext"]
    assert rec2.splitlines() == ["-m", "tuber", "x", "fulltext"]
    assert "skipped" not in log2
    assert "fulltext done rc=0" in log2


def test_internal_timeout_is_failure_not_success(tmp_path):
    proc, rec, log = _run(tmp_path, sleep="5", timeout=1)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert "timeout" in proc.stdout
    assert "fulltext timeout после 1s" in log, log
    assert rec != "", "CLI стартовал и был прерван — вызов должен быть записан"


def test_nonzero_rc_alerts(tmp_path):
    proc, _rec, log = _run(tmp_path, rc="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert proc.stdout.count("\n") == 1, proc.stdout


def test_empty_output_alerts(tmp_path):
    proc, _rec, _log = _run(tmp_path, out="")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout


def test_invalid_timeout_env_falls_back_to_default(tmp_path):
    # Нечисловое значение не должно ломать запуск: обёртка берёт дефолт и в
    # норме молчит.
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    logdir = tmp_path / "logs"
    logdir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({
        "TUBER_PYTHON": str(fake),
        "TUBER_X_PROJECT": str(project),
        "TUBER_X_LOG_DIR": str(logdir),
        "TUBER_X_RECORD": str(tmp_path / "record.txt"),
        "FAKE_FULLTEXT_OUT": NORMAL_OUT,
        "TUBER_X_FULLTEXT_TIMEOUT": "abc",
    })
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


# Потолок по умолчанию обязан быть 4800 с и строго меньше потолка планировщика
# 5400 с. Проверяем НАБЛЮДАЕМО: подкладываем в PATH фальшивый `timeout`, который
# записывает свой первый аргумент (сам потолок), а затем исполняет остаток
# команды. Обёртка запускается БЕЗ TUBER_X_FULLTEXT_TIMEOUT, поэтому в записи
# оказывается именно дефолт.
SCHEDULER_TIMEOUT_S = 5400
FAKE_TIMEOUT = """#!/usr/bin/env bash
printf '%s\\n' "$1" >> "$TUBER_X_TIMEOUT_RECORD"
shift
exec "$@"
"""


def test_default_timeout_is_4800_and_below_scheduler_ceiling(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    fake_timeout = bindir / "timeout"
    fake_timeout.write_text(FAKE_TIMEOUT)
    fake_timeout.chmod(fake_timeout.stat().st_mode | stat.S_IEXEC)
    timeout_record = tmp_path / "timeout_record.txt"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    logdir = tmp_path / "logs"
    logdir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({
        "TUBER_PYTHON": str(fake),
        "TUBER_X_PROJECT": str(project),
        "TUBER_X_LOG_DIR": str(logdir),
        "TUBER_X_RECORD": str(tmp_path / "record.txt"),
        "TUBER_X_TIMEOUT_RECORD": str(timeout_record),
        "FAKE_FULLTEXT_OUT": NORMAL_OUT,
        # Дефолт обязан взяться сам: переменную НЕ задаём.
        "PATH": str(bindir) + os.pathsep + env.get("PATH", ""),
    })
    env.pop("TUBER_X_FULLTEXT_TIMEOUT", None)
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert timeout_record.exists(), "фальшивый timeout обязан быть вызван обёрткой"
    recorded = timeout_record.read_text(encoding="utf-8").splitlines()
    assert recorded == ["4800"], recorded
    assert int(recorded[0]) < SCHEDULER_TIMEOUT_S
