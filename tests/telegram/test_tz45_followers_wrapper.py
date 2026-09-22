"""ТЗ-45: обёртка суточных снимков подписчиков Telegram.

Проверяем ГЛАВНОЕ поведение обёртки `scripts/telegram/tuber_tg_followers.sh`:
  * при норме (отказов 0) МОЛЧИТ, код 0;
  * при отказе печатает ОДНУ строку `ALERT:` по-русски, код 0;
  * прогон не вернул сводку (код != 0 / пусто) — тоже `ALERT`;
  * `TUBER_LAUNCHER_DRYRUN=1` — печатает план и в сеть не ходит (CLI не зовётся);
  * `TUBER_TG_FOLLOWERS_LIMIT` пробрасывается как `--limit`;
  * ТЗ-45F: план И реальный прогон ОБЯЗАНЫ нести `--allow-production` (иначе
    гейт `scoring.assert_can_write` глушит боевую запись), а `TUBER_DB=<копия>`
    работает без помех.

Сеть и боевая база не используются: вместо `TUBER_PYTHON` подставляется
скрипт-заглушка, который записывает вызов CLI и печатает подготовленную сводку.
"""
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "telegram", "tuber_tg_followers.sh")

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$TUBER_TG_RECORD"
if [ "${TUBER_TG_FAKE_RC:-0}" != "0" ]; then
  echo "boom" >&2
  exit "$TUBER_TG_FAKE_RC"
fi
if [ "${TUBER_TG_FAKE_EMPTY:-0}" = "1" ]; then exit 0; fi
printf 'обновлено %s, недоступно 0, нет канала 0, пропущено 0, отказов %s, время 1с\\n' \\
  "${TUBER_TG_FAKE_OK:-0}" "${TUBER_TG_FAKE_FAIL:-0}"
"""


def _run(tmp_path, *, limit="", ok="5", fail="0", rc="0", empty="0",
         dryrun=False, db=""):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.txt"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({
        "TUBER_PYTHON": str(fake),
        "TUBER_TG_PROJECT": str(project),
        "TUBER_TG_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_TG_RECORD": str(record),
        "TUBER_TG_FAKE_OK": ok,
        "TUBER_TG_FAKE_FAIL": fail,
        "TUBER_TG_FAKE_RC": rc,
        "TUBER_TG_FAKE_EMPTY": empty,
        "TUBER_TG_FOLLOWERS_TIME_CAP_SEC": "5",
    })
    if limit:
        env["TUBER_TG_FOLLOWERS_LIMIT"] = limit
    if db:
        env["TUBER_DB"] = db
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return proc, lines


def test_norm_is_silent_and_exit_zero(tmp_path):
    proc, lines = _run(tmp_path, limit="5")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "при норме обёртка обязана молчать"
    assert "--limit" in lines and "5" in lines, lines


def test_no_limit_env_means_no_flag(tmp_path):
    proc, lines = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "--limit" not in lines, lines


def test_refusals_alert(tmp_path):
    proc, _ = _run(tmp_path, limit="5", ok="0", fail="2")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert proc.stdout.count("\n") == 1, proc.stdout


def test_nonzero_rc_alerts(tmp_path):
    proc, _ = _run(tmp_path, rc="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout


def test_empty_output_alerts(tmp_path):
    proc, _ = _run(tmp_path, empty="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout


def test_dryrun_prints_plan_without_network(tmp_path):
    proc, lines = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == (
        f"{tmp_path}/bin/python3 -m tuber tg followers --allow-production"), proc.stdout
    assert lines == [], "в dry-run CLI не должен запускаться"


def test_dryrun_prints_limit_in_plan(tmp_path):
    proc, lines = _run(tmp_path, dryrun=True, limit="3")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == (
        f"{tmp_path}/bin/python3 -m tuber tg followers --allow-production --limit 3"), proc.stdout
    assert lines == []


def test_dryrun_plan_has_allow_production(tmp_path):
    """ТЗ-45F: обёртка — боевой планировщик, план ОБЯЗАН объявлять запись в боевую.

    Без `--allow-production` команда `tuber tg followers` гейтится
    `scoring.assert_can_write` и отрабатывает каждую ночь вхолостую (ноль
    снимков, rc=1). Этот тест-страж ловит регрессию, если флаг снова уберут.
    """
    proc, _ = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert "--allow-production" in proc.stdout, (
        "dry-run план обязан печатать --allow-production: " + repr(proc.stdout))


def test_run_passes_allow_production_to_cli(tmp_path):
    """Реальный прогон тоже передаёт --allow-production в CLI."""
    proc, lines = _run(tmp_path, limit="5")
    assert proc.returncode == 0, proc.stderr
    assert "--allow-production" in lines, lines


def test_run_with_db_copy_still_works(tmp_path):
    """TUBER_DB=<копия>: прогон не ломается, флаг копии не мешает (ТЗ-45F)."""
    copy_db = tmp_path / "copy.db"
    proc, lines = _run(tmp_path, limit="5", db=str(copy_db))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert "--allow-production" in lines, lines
    assert "--limit" in lines and "5" in lines, lines
