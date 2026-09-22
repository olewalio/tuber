"""ТЗ-44B: обёртка суточных снимков подписчиков X (`scripts/x/tuber_x_followers.sh`).

Проверяется ГЛАВНОЕ решение обёртки — расчёт `--limit`:
  * потолок = min(реестр, остаток суточного бюджета сессии, жёсткий потолок
    `TUBER_X_FOLLOWERS_LIMIT`);
  * обход идёт ПОРЦИЯМИ по остатку окна сессии (иначе транспорт сам отказал бы
    на 51-м запросе);
  * при исчерпанном бюджете прогон пропускается МОЛЧА (код 0, в сеть не ходим);
  * при `blocked`/cooldown — одна строка `ALERT` и остановка.

Сеть и боевая база не используются: вместо `TUBER_PYTHON` подставляется
скрипт-заглушка, который отдаёт подготовленный «план» (реестр + бюджет) и
записывает вызовы CLI. Так проверяется именно арифметика обёртки.
"""
import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRAPPER = os.path.join(ROOT, "scripts", "x", "tuber_x_followers.sh")

FAKE = """#!/usr/bin/env bash
is_helper=1
for a in "$@"; do [ "$a" = "followers" ] && is_helper=0; done
if [ "$is_helper" = "1" ]; then
  printf '%s\\n' "$TUBER_X_FAKE_TOTAL" "$TUBER_X_FAKE_PENDING" \\
    "$TUBER_X_FAKE_DAY_USED" "$TUBER_X_FAKE_DAY_CAP" \\
    "$TUBER_X_FAKE_WIN_USED" "$TUBER_X_FAKE_WIN_CAP" \\
    "$TUBER_X_FAKE_WIN_FREE" "$TUBER_X_FAKE_COOLDOWN" \\
    "$TUBER_X_FAKE_BLOCKED"
  exit 0
fi
printf '%s\\n' "$@" >> "$TUBER_X_RECORD"
if [ "${TUBER_X_FAKE_REFUSAL:-0}" = "1" ]; then
  printf '  @probe: отказ транспорта (XSessionRateLimited)\\n'
fi
printf 'подписчики X: обновлено %s, отложено %s, отказов %s\\n' \\
  "${TUBER_X_FAKE_OK:-0}" "${TUBER_X_FAKE_DEFERRED:-0}" "${TUBER_X_FAKE_FAIL:-0}"
"""


def _run(tmp_path, *, total="350", pending=None, day_used="215", day_cap="800",
         win_used="0", win_cap="50", win_free="", cooldown="", blocked="0",
         ok="50", fail="0", deferred="0", refusal="0", env_extra=None,
         dryrun=False):
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
        "TUBER_X_PROJECT": str(project),
        "TUBER_X_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_X_RECORD": str(record),
        "TUBER_X_FAKE_TOTAL": total,
        "TUBER_X_FAKE_PENDING": total if pending is None else pending,
        "TUBER_X_FAKE_DAY_USED": day_used,
        "TUBER_X_FAKE_DAY_CAP": day_cap,
        "TUBER_X_FAKE_WIN_USED": win_used,
        "TUBER_X_FAKE_WIN_CAP": win_cap,
        "TUBER_X_FAKE_WIN_FREE": win_free,
        "TUBER_X_FAKE_COOLDOWN": cooldown,
        "TUBER_X_FAKE_BLOCKED": blocked,
        "TUBER_X_FAKE_OK": ok,
        "TUBER_X_FAKE_FAIL": fail,
        "TUBER_X_FAKE_DEFERRED": deferred,
        "TUBER_X_FAKE_REFUSAL": refusal,
    })
    if dryrun:
        env["TUBER_LAUNCHER_DRYRUN"] = "1"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    limits = [int(lines[i + 1]) for i, v in enumerate(lines) if v == "--limit"]
    return proc, limits


def test_limit_is_registry_when_budget_allows(tmp_path):
    """Бюджета хватает на весь реестр — цель = реестр, но порциями по окну."""
    proc, limits = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "при норме обёртка обязана молчать"
    assert sum(limits) == 350, limits
    assert all(lim <= 50 for lim in limits), limits  # ни одна порция не больше окна


def test_limit_is_capped_by_daily_budget_remaining(tmp_path):
    """Остаток суточного бюджета меньше реестра — берём остаток, не реестр."""
    proc, limits = _run(tmp_path, total="350", day_used="780", day_cap="800", ok="20")
    assert proc.returncode == 0, proc.stderr
    assert limits == [20], limits


def test_hard_limit_env_caps_target(tmp_path):
    """`TUBER_X_FOLLOWERS_LIMIT` — жёсткий потолок обхода за прогон."""
    proc, limits = _run(tmp_path, env_extra={"TUBER_X_FOLLOWERS_LIMIT": "5"}, ok="5")
    assert proc.returncode == 0, proc.stderr
    assert limits == [5], limits


def test_budget_exhausted_skips_silently(tmp_path):
    """Бюджет исчерпан — в сеть не ходим, в stdout тишина, код 0."""
    proc, limits = _run(tmp_path, day_used="800", day_cap="800")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert limits == [], "при исчерпанном бюджете CLI не запускается"


def test_dryrun_prints_planned_command(tmp_path):
    """`TUBER_LAUNCHER_DRYRUN=1` — печатает команду, в сеть не ходит."""
    proc, limits = _run(tmp_path, dryrun=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == \
        f"{tmp_path}/bin/python3 -m tuber x followers --limit 350", proc.stdout
    assert limits == []


def test_blocked_session_alerts_and_stops(tmp_path):
    """Сессия помечена blocked — одна строка ALERT, CLI не запускается."""
    proc, limits = _run(tmp_path, blocked="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ALERT:"), proc.stdout
    assert "blocked" in proc.stdout
    assert limits == []


def test_transport_refusal_stops_after_one_chunk(tmp_path):
    """Отказ транспорта (окно/429 без cooldown) — не крутим повторы бесконечно."""
    proc, limits = _run(tmp_path, refusal="1", ok="0", fail="1")
    assert proc.returncode == 0, proc.stderr
    assert len(limits) == 1, limits
    assert proc.stdout.startswith("ALERT:"), proc.stdout


def test_cooldown_after_refusal_alerts(tmp_path):
    """Отказ транспорта при cooldown — ALERT со ссылкой на журнал."""
    proc, limits = _run(tmp_path, refusal="1", ok="0", fail="1",
                        cooldown="2026-09-21T23:00:00")
    assert proc.returncode == 0, proc.stderr
    assert len(limits) == 1, limits
    assert "ALERT:" in proc.stdout and "cooldown" in proc.stdout, proc.stdout


# ------------------------------------------------- ТЗ-44C: остановки и пейджинг
def test_time_cap_stops_wrapper(tmp_path):
    """Потолок времени прогона: обёртка останавливается, а не ждёт окно вечно."""
    proc, limits = _run(
        tmp_path, total="5", pending="5", day_used="0", day_cap="800",
        win_used="50", win_cap="50", win_free="2099-01-01T00:00:00",
        env_extra={"TUBER_X_FOLLOWERS_TIME_CAP_SEC": "1"})
    assert proc.returncode == 0, proc.stderr
    assert limits == [], "пока окно исчерпано, CLI не зовём"
    assert "потолок времени" in proc.stdout, proc.stdout


STATEFUL_FAKE = """#!/usr/bin/env bash
is_helper=1
for a in "$@"; do [ "$a" = "followers" ] && is_helper=0; done
STATE="$TUBER_X_STATE"
n=$(cat "$STATE" 2>/dev/null || echo 0)
free=$(date -u -d "+1 second" +%Y-%m-%dT%H:%M:%S)
if [ "$is_helper" = "1" ]; then
  case "$n" in
    2) printf '%s\\n' 6 3 0 800 50 50 "$free" "" 0; echo 3 > "$STATE" ;;
    3) printf '%s\\n' 6 3 0 800 0 50 "" "" 0 ;;
    4) printf '%s\\n' 6 0 5 800 0 50 "" "" 0 ;;
    *) printf '%s\\n' 6 6 0 800 0 50 "" "" 0 ;;
  esac
  exit 0
fi
printf '%s\\n' "$@" >> "$TUBER_X_RECORD"
if [ "$n" -eq 0 ]; then
  echo 2 > "$STATE"
  printf 'подписчики X: обновлено 3, отложено 3 (бюджет окна, продолжение после %s UTC), отказов 0\\n' "$(date -u -d "+1 second" +%H:%M:%S)"
elif [ "$n" -eq 3 ]; then
  echo 4 > "$STATE"
  printf 'подписчики X: обновлено 3, отложено 0, отказов 0\\n'
else
  printf 'подписчики X: обновлено 3, отложено 0, отказов 0\\n'
fi
"""


def test_wrapper_waits_for_window_and_continues(tmp_path):
    """После отложенных обёртка ждёт окно и зовёт CLI снова (обход с места)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "python3"
    fake.write_text(STATEFUL_FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.txt"
    state = tmp_path / "state.txt"
    state.write_text("0")
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    env = dict(os.environ, **{
        "TUBER_PYTHON": str(fake),
        "TUBER_X_PROJECT": str(project),
        "TUBER_X_LOG_DIR": str(tmp_path / "logs"),
        "TUBER_X_RECORD": str(record),
        "TUBER_X_STATE": str(state),
        "TUBER_X_FOLLOWERS_TIME_CAP_SEC": "120",
    })
    proc = subprocess.run(["bash", WRAPPER], capture_output=True, text=True, env=env)
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    limits = [int(lines[i + 1]) for i, v in enumerate(lines) if v == "--limit"]
    assert proc.returncode == 0, proc.stderr
    assert limits == [6, 3], limits
    assert "обновлено 6" in proc.stdout, proc.stdout
