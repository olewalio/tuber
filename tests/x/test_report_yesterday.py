"""X-сводка за ЗАВЕРШЁННЫЕ сутки (дефект 2026-10-03).

Задание стоит в 07:00 UTC, сбор X — в 11:00 UTC, сборка сюжетов — каждые 2 ч.
Отчёт за ТЕКУЩИЕ сутки на момент запуска пуст по построению (код 4, ALERT
каждый день), а обёртка Р1.5 запрещает перезапись уже созданного файла.

Проверяем:
  * без флага поведение прежнее (сегодня / stdout-only при существующем файле);
  * `--yesterday` и TUBER_X_REPORT_YESTERDAY=1 дают ВЧЕРАШНЮЮ дату UTC и режим
    записи (файл перезаписывается);
  * окно завершённых суток включает вчерашние данные и не включает сегодняшние,
    поэтому непустая база не даёт «пустой сбор».

Плановый режим обёртки (TUBER_LAUNCHER_DRYRUN=1) печатает дату/режим/аргументы
без обращения к базе — им и проверяем контракт дат.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "x", "tuber_x_report.sh")


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday():
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _run(tmp_path, *args, extra_env=None):
    """Плановый прогон обёртки: база и сеть не трогаются."""
    env = dict(os.environ)
    env["TUBER_LAUNCHER_DRYRUN"] = "1"
    env["TUBER_X_REPORT_DIR"] = str(tmp_path)
    env["TUBER_PYTHON"] = sys.executable
    env.pop("TUBER_X_REPORT_YESTERDAY", None)
    env.update(extra_env or {})
    return subprocess.run(["bash", SCRIPT, *args], env=env,
                          capture_output=True, text=True)


def _plan(res):
    assert res.returncode == 0, res.stderr
    m = re.search(r"^DRYRUN tuber_x_report: date=(\S+) mode=(\S+) args=(.*)$",
                  res.stdout, re.M)
    assert m, f"нет строки плана в stdout:\n{res.stdout!r}\n{res.stderr!r}"
    return m.group(1), m.group(2), m.group(3).strip()


def test_default_without_file_writes_today(tmp_path):
    date, mode, args = _plan(_run(tmp_path))
    assert date == _today()
    assert mode == "write"
    # Без явной даты CLI сам берёт сегодняшние сутки: аргумент --date не нужен.
    assert args == "report"


def test_default_with_existing_file_is_stdout_only(tmp_path):
    (tmp_path / f"{_today()}.md").write_text("# старый отчёт\n", encoding="utf-8")
    date, mode, args = _plan(_run(tmp_path))
    assert date == _today()
    assert mode == "stdout-only"
    assert args == "report --stdout-only"


def test_yesterday_flag_uses_completed_day_and_writes(tmp_path):
    date, mode, args = _plan(_run(tmp_path, "--yesterday"))
    assert date == _yesterday()
    assert mode == "write"
    assert args == f"report --date {_yesterday()}"


def test_yesterday_env_var(tmp_path):
    date, mode, args = _plan(_run(
        tmp_path, extra_env={"TUBER_X_REPORT_YESTERDAY": "1"}))
    assert date == _yesterday()
    assert mode == "write"
    assert args == f"report --date {_yesterday()}"


def test_yesterday_rewrites_even_if_file_exists(tmp_path):
    """Завершённые сутки — явный прогон: файл перезаписывается (не stdout-only)."""
    (tmp_path / f"{_yesterday()}.md").write_text("# пустой отчёт\n", encoding="utf-8")
    date, mode, args = _plan(_run(tmp_path, "--yesterday"))
    assert date == _yesterday()
    assert mode == "write"
    assert args == f"report --date {_yesterday()}"


def test_explicit_date_still_writes(tmp_path):
    date, mode, args = _plan(_run(tmp_path, "2026-01-02"))
    assert date == "2026-01-02"
    assert mode == "write"
    assert args == "report --date 2026-01-02"


def test_unknown_argument_is_rejected(tmp_path):
    res = _run(tmp_path, "--tomorrow")
    assert res.returncode == 2
    assert res.stdout == ""
    assert "неизвестный аргумент" in res.stderr


# --------------------------------------------------------------------- окно
def _insert_post(con, acc_id, tid, published_iso, text="пост"):
    con.execute(
        """INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,
             text, text_hash, mentions, links, is_retweet, is_quote, owner_handle,
             lang, first_seen_at)
           VALUES (?,?,?,'rss',?,?, '[]','[]',0,0,?, 'ru', ?)""",
        (acc_id, tid, published_iso, text, "h_" + text, "acc", published_iso))
    con.execute("INSERT INTO scores (tweet_id, computed_at, significance)"
                " VALUES (?,?,?)", (tid, published_iso, 1.0))


def _insert_story(con, tid, published_iso, topic):
    con.execute(
        """INSERT INTO stories (created_at, window_hours, threshold, first_tweet_id,
             first_mover, published_at, xconf, post_count, lead_time_min, topics,
             entities, is_new_entity, is_single, suspect, claimed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?)""",
        (published_iso.replace("T", " "), 24, 1, tid, "acc", published_iso, 3, 1,
         0, '["%s"]' % topic, "[]", published_iso.replace("T", " ")))


def test_completed_day_window_includes_yesterday_not_today(con):
    """Вчерашние данные попадают в отчёт, сегодняшние — нет."""
    from tuber.platforms.x import report

    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0,
                                               microsecond=0)
    yday = today - timedelta(days=1)
    y_iso = yday.strftime("%Y-%m-%dT%H:%M:%S")
    t_iso = today.strftime("%Y-%m-%dT%H:%M:%S")
    y_str = yday.strftime("%Y-%m-%d")
    t_str = today.strftime("%Y-%m-%d")

    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('acc','A','active')")
    acc_id = con.execute(
        "SELECT id FROM accounts WHERE handle='acc'").fetchone()["id"]

    _insert_post(con, acc_id, "y1", y_iso, text="вчерашний")
    _insert_post(con, acc_id, "t1", t_iso, text="сегодняшний")
    _insert_story(con, "y1", y_iso, "вчера-тема")
    _insert_story(con, "t1", t_iso, "сегодня-тема")
    con.commit()

    # Окно завершённых суток видит ровно вчерашний пост и не пусто.
    start, end = report.day_bounds(y_str)
    st = report.data_status(con, start, end)
    assert st["posts"] == 1, st
    assert st["significance"] == 1, st
    assert not st["empty"], st

    # Окно сегодняшних суток — ровно сегодняшний пост.
    t_start, t_end = report.day_bounds(t_str)
    assert report.data_status(con, t_start, t_end)["posts"] == 1

    text = report.build(con, date=y_str, translator=False)
    assert "вчера-тема" in text
    assert "сегодня-тема" not in text
