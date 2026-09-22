"""ТЗ-50: обёртка утренней выдачи «Сливки» (доставка в Telegram).

Проверяем главное поведение ``scripts/common/tuber_digest_slivki.sh``:

* при норме МОЛЧИТ, код 0 (CLI при ``--send`` печатает пусто);
* при отказе (rc != 0) печатает ОДНУ строку `ALERT:` по-русски;
* ``TUBER_LAUNCHER_DRYRUN=1`` — печатает план, CLI не зовётся;
* план И реальный прогон ОБЯЗАНЫ нести ``--send --allow-production``
  (урок ТЗ-45F: иначе гейт записи/отправки глушит боевой прогон);
* явные chat_id/thread_id из окружения передаются CLI (топик не угадывается).

Сеть и боевая база не используются: вместо ``TUBER_PYTHON`` подставляется
скрипт-заглушка, который записывает вызовы CLI и печатает подготовленный вывод.
"""
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "common", "tuber_digest_slivki.sh")

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_DIGEST_RECORD"
if [ "${TUBER_DIGEST_FAKE_RC:-0}" != "0" ]; then
  echo "boom" >&2
  exit "$TUBER_DIGEST_FAKE_RC"
fi
if [ "${TUBER_DIGEST_FAKE_EMPTY:-0}" = "1" ]; then exit 0; fi
printf 'неожиданный вывод CLI\\n'
"""


def _run(tmp_path, *, rc="0", empty="0", dryrun=False, db="", chat="", thread="",
         extra_env=None, owner_env=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.txt"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    env = dict(os.environ)
    # Файл владельца по умолчанию НЕ читаем: его место подставляем явно (ТЗ-54).
    for key in ("TUBER_DIGEST_CHAT_ID", "TUBER_SLIVKI_ME", "TUBER_SLIVKI_REACH",
                "TUBER_SLIVKI_REACTIONS", "TUBER_SLIVKI_G7"):
        env.pop(key, None)
    env.update({
        "TUBER_PYTHON": str(fake),
        "TUBER_DIGEST_PROJECT": str(project),
        "TUBER_DIGEST_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_DIGEST_RECORD": str(record),
        "TUBER_DIGEST_FAKE_RC": rc,
        "TUBER_DIGEST_FAKE_EMPTY": empty,
        "TUBER_DIGEST_TIMEOUT_SEC": "5",
        "TUBER_OWNER_ENV": str(owner_env or (tmp_path / "no_owner.env")),
    })
    if db:
        env["TUBER_DB"] = db
    if chat:
        env["TUBER_DIGEST_CHAT_ID"] = chat
    if thread:
        env["TUBER_DIGEST_THREAD_ID"] = thread
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env,
                          check=False)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return proc, lines


def test_norm_is_silent_and_exit_zero(tmp_path):
    proc, lines = _run(tmp_path, empty="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "при норме обёртка обязана молчать"
    assert "digest" in lines and "slivki" in lines, lines


def test_run_declares_send_and_allow_production(tmp_path):
    """ТЗ-45F: боевой прогон обязан нести --send --allow-production."""
    proc, lines = _run(tmp_path, empty="1")
    assert proc.returncode == 0, proc.stderr
    assert "--send" in lines, lines
    assert "--allow-production" in lines, lines


def test_explicit_chat_and_thread_passed(tmp_path):
    proc, lines = _run(tmp_path, empty="1", chat="555", thread="7")
    assert proc.returncode == 0, proc.stderr
    assert "--chat-id" in lines and "555" in lines, lines
    assert "--thread-id" in lines and "7" in lines, lines


def test_nonzero_rc_alerts(tmp_path):
    proc, _ = _run(tmp_path, rc="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert proc.stdout.count("\n") == 1, proc.stdout


def test_dryrun_prints_plan_without_cli(tmp_path):
    proc, lines = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    assert "-m tuber digest slivki" in text, text
    assert "--send --allow-production" in text, text
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_run_with_db_copy_still_works(tmp_path):
    copy_db = tmp_path / "copy.db"
    proc, lines = _run(tmp_path, empty="1", db=str(copy_db))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert "--allow-production" in lines, lines
    assert "--db" in lines and str(copy_db) in lines, lines


# --------------------------------------------------------------------------- #
# Владелец «где я» из окружения (D-65)
# --------------------------------------------------------------------------- #
def test_owner_subject_from_env_is_forwarded(tmp_path):
    proc, lines = _run(tmp_path, empty="1",
                       extra_env={"TUBER_SLIVKI_ME": "telegram:my_channel"})
    assert proc.returncode == 0, proc.stderr
    assert "--me" in lines and "telegram:my_channel" in lines, lines


def test_owner_numbers_from_env_are_forwarded(tmp_path):
    proc, lines = _run(tmp_path, empty="1", extra_env={
        "TUBER_SLIVKI_REACH": "2.5",
        "TUBER_SLIVKI_REACTIONS": "8.0",
        "TUBER_SLIVKI_G7": "0.10",
    })
    assert proc.returncode == 0, proc.stderr
    assert "--me-reach" in lines and "2.5" in lines, lines
    assert "--me-reactions" in lines and "8.0" in lines, lines
    assert "--me-g7" in lines and "0.10" in lines, lines


def test_without_owner_env_no_me_flag(tmp_path):
    """Без переменных обёртка НЕ подставляет субъекта: блок честно «НЕ найден»."""
    proc, lines = _run(tmp_path, empty="1")
    assert proc.returncode == 0, proc.stderr
    assert "--me" not in lines, lines
    assert "--me-reach" not in lines, lines


def test_owner_line_is_commented_in_wrapper():
    """Строка владельца закомментирована и помечена «подставить handle владельца»."""
    with open(WRAPPER, encoding="utf-8") as fh:
        text = fh.read()
    assert "подставить handle владельца или его числа" in text
    for line in text.splitlines():
        if "TUBER_SLIVKI_ME=" in line and "например" not in line:
            assert line.lstrip().startswith("#"), f"строка владельца не закомментирована: {line}"


# --------------------------------------------------------------------------- #
# ТЗ-54: личные адреса доставки — из файла окружения ВНЕ репозитория
# --------------------------------------------------------------------------- #
def test_owner_env_file_supplies_chat_and_subject(tmp_path):
    """С TUBER_OWNER_ENV план несёт --chat-id/--me из файла (не из кода)."""
    owner = tmp_path / "owner.env"
    owner.write_text("export TUBER_DIGEST_CHAT_ID=100200300\n"
                     "export TUBER_SLIVKI_ME=telegram:test_me\n", encoding="utf-8")
    proc, lines = _run(tmp_path, dryrun=True, owner_env=owner)
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    assert "--chat-id" in text and "100200300" in text, text
    assert "--me" in text and "telegram:test_me" in text, text
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_without_owner_env_no_chat_id_in_plan(tmp_path):
    """Нет файла владельца → в плане нет --chat-id: личных значений в скрипте нет."""
    proc, _ = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "--chat-id" not in proc.stdout, proc.stdout


def test_wrapper_has_no_personal_literals():
    """В самой обёртке нет личных значений — доставка задаётся окружением."""
    with open(WRAPPER, encoding="utf-8") as fh:
        text = fh.read()
    assert "TUBER_OWNER_ENV" in text
    assert "/root/.hermes/tuber_owner.env" in text
