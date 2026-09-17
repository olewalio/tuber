"""ТЗ-4 Р6: сторож свежести, отказов и бюджетов каналов.
ТЗ-13 Р1: сторож проверяет факт доставки сводки, а не только наличие файла.
"""
import json
from datetime import datetime, timedelta, timezone

from tuber.platforms.x import config, store as db, health


class Row(dict):
    pass


def _acc(con, handle="a"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,'A','active')",
                (handle,))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def test_stale_alert_when_no_posts(con):
    res = health.check_stale(con)
    assert res["alert"] is True


def test_stale_ok_with_recent_post(con):
    a = _acc(con)
    con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
                " first_seen_at) VALUES (?, '1', ?, 'rss', ?)",
                (a, db.utcnow_iso(), db.utcnow_iso()))
    con.commit()
    assert health.check_stale(con)["alert"] is False


def _req(con, kind, status, ts=None):
    con.execute("INSERT INTO requests (host, ts, kind, status) VALUES ('h',?,?,?)",
                (ts or db.utcnow_iso(), kind, status))
    con.commit()


def test_cdn_429_alert(con):
    for i in range(config.HEALTH_CDN_429_PER_HOUR + 1):
        _req(con, "cdn_tweet", 429)
    res = health.check_cdn_429(con)
    assert res["alert"] is True
    assert res["value"] == config.HEALTH_CDN_429_PER_HOUR + 1


def test_fail_rate_alert(con):
    for _ in range(12):
        _req(con, "cdn_tweet", 500)
    for _ in range(4):
        _req(con, "cdn_tweet", 200)
    res = health.check_fail_rate(con)
    assert res["alert"] is True


def test_long_text_gap_alert(con):
    a = _acc(con)
    con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
                " text, is_long, first_seen_at) VALUES (?, '1', ?, 'rss', ?, 1, ?)",
                (a, db.utcnow_iso(), "short", db.utcnow_iso()))
    con.commit()
    res = health.check_long_text_gap(con)
    assert res["alert"] is True


def test_synd_budget_alert(con):
    con.execute("INSERT INTO instances (host, requests_today, day) VALUES (?,?,?)",
                (config.SYND_HOST, config.SYND_DAILY_BUDGET,
                 datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    con.commit()
    for _ in range(config.SYND_DAILY_BUDGET):
        _req(con, "synd_timeline", 429)
    res = health.check_synd_budget(con)
    assert res["alert"] is True


def test_run_returns_alerts_and_logs(con):
    res = health.run(con)
    assert res["ok"] is False and res["alerts"]
    n = con.execute("SELECT COUNT(*) FROM run_log WHERE msg LIKE 'health:%'").fetchone()[0]
    assert n >= 5


# ---------------- зрелость метрики: сторож не требует невозможного ----------------
def _post(con, tid="1", published=None):
    a = _acc(con)
    ts = published or db.utcnow_iso()
    con.execute("INSERT INTO posts (account_id, tweet_id, published_at_utc, published_src,"
                " first_seen_at) VALUES (?, ?, ?, 'rss', ?)",
                (a, tid, ts, db.utcnow_iso()))
    con.commit()


def _score(con, tid="1", computed=None):
    con.execute("INSERT INTO scores (tweet_id, computed_at, significance) VALUES (?,?,1.0)",
                (tid, computed or db.utcnow_iso()))
    con.commit()


def test_report_data_ok_when_scores_fresh_but_posts_young(con):
    """Оценка свежая, а посты в окне младше зрелости метрики -> тревоги нет."""
    hours = 6
    _post(con)  # опубликован только что: снимка «6 ч» ещё быть не может
    _score(con)  # но оценка пересчитана сейчас
    res = health.check_report_data(con, hours=hours)
    assert res["alert"] is False
    assert res["scores_fresh"] == 1 and res["value"] == 1


def test_report_data_alert_when_scores_stale(con):
    """Защита от «просто заглушить»: оценки старше окна -> тревога."""
    hours = 6
    _post(con)
    _score(con, computed=db.iso(datetime.now(timezone.utc) - timedelta(hours=10)))
    res = health.check_report_data(con, hours=hours)
    assert res["alert"] is True and res["value"] == 1 and res["scores_fresh"] == 0


def test_report_data_alert_when_no_posts_in_window(con):
    """Постов в окне нет вовсе (при свежих оценках) -> тревога."""
    hours = 6
    _post(con, published=db.iso(datetime.now(timezone.utc) - timedelta(hours=10)))
    _score(con)
    res = health.check_report_data(con, hours=hours)
    assert res["alert"] is True and res["value"] == 0 and res["scores_fresh"] == 1


# ------------------------------- ТЗ-13 Р1: факт доставки сводки, а не файл
REPORT_JOB_ID = "a3a782010a06"


def _hermes(tmp_path, scripts=(health.REPORT_JOB_SCRIPT,), lines=(), name="agent.log"):
    """Конфигурация планировщика + каталог журналов с заданным содержимым."""
    cfg = tmp_path / "jobs.json"
    jobs = [{"id": REPORT_JOB_ID, "script": s} for s in scripts]
    cfg.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    logd = tmp_path / "logs"
    logd.mkdir()
    (logd / name).write_text("\n".join(lines) + ("\n" if lines else ""),
                             encoding="utf-8")
    return str(cfg), str(logd)


def _line(marker, when, jid=REPORT_JOB_ID):
    ts = when.strftime("%Y-%m-%d %H:%M:%S") + ",000"
    if marker == "ok":
        return f"{ts} INFO cron.scheduler: Job '{jid}': delivered to telegram:1 via live adapter"
    return f"{ts} ERROR cron.scheduler: Job '{jid}': delivery error: Telegram send failed: Timed out"


def test_report_delivery_alert_without_success_line(tmp_path):
    """Р1.3: нет строки доставки за 30 ч -> ALERT."""
    cfg, logd = _hermes(tmp_path, lines=["2026-09-15 18:00:00,000 INFO нечто"])
    res = health.check_report_delivery(None, config_path=cfg, log_dir=logd)
    assert res["alert"] is True and res["severity"] == "ALERT"
    assert res["last_success"] is None


def test_report_delivery_silent_on_success(tmp_path):
    """Р1.3: успешная доставка в окне -> молчание (alert=False)."""
    cfg, logd = _hermes(tmp_path, lines=[_line("ok", datetime.now() - timedelta(hours=2))])
    res = health.check_report_delivery(None, config_path=cfg, log_dir=logd)
    assert res["alert"] is False
    assert res["last_success"] is not None


def test_report_delivery_warn_when_last_attempt_failed(tmp_path):
    """Р1.3: доставка была, но последняя попытка с ошибкой -> WARN."""
    now = datetime.now()
    cfg, logd = _hermes(tmp_path, lines=[
        _line("ok", now - timedelta(hours=2)),
        _line("err", now - timedelta(minutes=5)),
    ])
    res = health.check_report_delivery(None, config_path=cfg, log_dir=logd)
    assert res["alert"] is True and res["severity"] == "WARN"
    assert res["last_error"] > res["last_success"]


def test_report_delivery_warn_on_stale_error_only(tmp_path):
    """Ошибка без успеха тоже молчанием не оборачивается: ALERT."""
    cfg, logd = _hermes(tmp_path, lines=[_line("err", datetime.now() - timedelta(hours=1))])
    res = health.check_report_delivery(None, config_path=cfg, log_dir=logd)
    assert res["alert"] is True and res["severity"] == "ALERT"
    assert res["last_error"] is not None


def test_report_delivery_warn_without_config(tmp_path):
    """Р1.4: нет конфигурации планировщика -> WARN, не падение и не молчание."""
    logd = tmp_path / "logs"
    logd.mkdir()
    res = health.check_report_delivery(
        None, config_path=str(tmp_path / "нет.json"), log_dir=str(logd))
    assert res["alert"] is True and res["severity"] == "WARN"
    assert res["reason"] == "no_config"


def test_report_delivery_warn_without_logs(tmp_path):
    """Р1.4: нет каталога журналов -> WARN с честной причиной."""
    cfg, _ = _hermes(tmp_path)
    res = health.check_report_delivery(
        None, config_path=cfg, log_dir=str(tmp_path / "нет-каталога"))
    assert res["alert"] is True and res["severity"] == "WARN"
    assert res["reason"] == "no_logs"


def test_report_delivery_warn_when_job_absent(tmp_path):
    """Р1.1/Р1.4: задания отчёта нет в конфигурации -> WARN."""
    cfg, logd = _hermes(tmp_path, scripts=("tuber_x_collect_a.sh",))
    res = health.check_report_delivery(None, config_path=cfg, log_dir=logd)
    assert res["alert"] is True and res["severity"] == "WARN"
    assert res["reason"] == "no_job"


def test_report_delivery_ignores_other_jobs(tmp_path):
    """Чужая доставка не выдаётся за доставку сводки отчёта."""
    cfg, logd = _hermes(tmp_path, lines=[
        _line("ok", datetime.now() - timedelta(hours=1), jid="deadbeef01")])
    res = health.check_report_delivery(None, config_path=cfg, log_dir=logd)
    assert res["alert"] is True and res["severity"] == "ALERT"
