#!/usr/bin/env python3
"""Приёмка ТЗ-10: аварийное переключение на резерв без потери аккаунтов (П1–П9).

ЖЁСТКИЕ ПРАВИЛА:
  * рабочая БД `data/tuber_x.db` открывается ТОЛЬКО на чтение; все изменяющие
    прогоны идут на КОПИЯХ (копия делается перед каждым сценарием);
  * каталог `reports/` не трогается — сверяются sha256 ДО/ПОСЛЕ;
  * внешней сети нет: транспорт Nitter в мок-сценариях подменён, а «мёртвый»
    Nitter — это реальный `http://127.0.0.1:9` (соединение отклонено локально);
    резервный канал x_ssr подменён мок-транспортом (живой x.com = платный трафик
    и внешняя сеть, запрещены условиями ТЗ);
  * в планировщик Hermes и crontab ничего не пишется.

Проверки:
  П1 Nitter здоров            — резерв не используется;
  П2 Nitter мёртв с начала    — все аккаунты через резерв, fail=0, потерь 0;
  П3 отказ по одному аккаунту — резерв ровно для него, системного переключения нет;
  П4 404 и пустая лента       — счётчик отказов не растёт, резерв не включается;
  П5 успешный фид             — счётчик обнулён, деградация снята, резерва нет;
  П6 потолок XSSR_MAX_PER_RUN — второй аккаунт в резерв не идёт;
  П7 полный pytest зелёный;
  П8 миграция схемы, user_version повышен, повторный запуск идемпотентен;
  П9 двойной прогон по копии — дублей по tweet_id нет.

Запуск: python3 tools/acceptance_tz10.py
Вывод:  docs/acceptance-log-10.txt (полный stdout) + консоль.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import channels, collect, config, store as db, health  # noqa: E402
from tests.mocking import FakeSsrTransport, VClock, make_feed  # noqa: E402

PROD_DB = os.path.realpath(config.DB_PATH)
REPORTS_DIR = os.path.join(ROOT, "reports")
TMP = tempfile.mkdtemp(prefix="tuber_x_acc10_")

_out = []
_results = []

# аккаунты, которые берём в сценарии (из копии боевой базы, тир A)
ACCOUNT_LIMIT = 3


def p(line=""):
    print(line)
    _out.append(str(line))


def hr(title):
    p("")
    p("=" * 78)
    p(title)
    p("=" * 78)


def check(num, what, ok, data):
    _results.append({"num": num, "what": what, "ok": bool(ok), "data": data})
    p(f"[{num:>2}] {'OK  ' if ok else 'FAIL'} {what}")
    p(f"      данные: {data}")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_snapshot(d):
    snap = {}
    for name in sorted(os.listdir(d)):
        full = os.path.join(d, name)
        snap[name] = sha256_file(full) if os.path.isfile(full) else "nonregular"
    return snap


def prod_snapshot(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            "user_version": con.execute("PRAGMA user_version").fetchone()[0],
            "accounts": con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            "posts": con.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "requests": con.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
            "runs": con.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "run_log": con.execute("SELECT COUNT(*) FROM run_log").fetchone()[0],
            "instances": con.execute("SELECT COUNT(*) FROM instances").fetchone()[0],
            "metrics_daily": con.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0],
            "accounts_tier_status": tuple(con.execute(
                "SELECT tier, status, COUNT(*) FROM accounts GROUP BY tier, status"
                " ORDER BY tier, status").fetchall()),
        }
    finally:
        con.close()


def short(snap):
    return (f"user_version={snap['user_version']} accounts={snap['accounts']} "
            f"posts={snap['posts']} requests={snap['requests']} runs={snap['runs']} "
            f"run_log={snap['run_log']} instances={snap['instances']} "
            f"metrics_daily={snap['metrics_daily']}")


def fresh_copy(tag):
    """Копия боевой БД (с -wal/-shm) + штатная миграция. Боевая — только чтение."""
    dst = os.path.join(TMP, f"{tag}.db")
    shutil.copy2(PROD_DB, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(PROD_DB + suffix):
            shutil.copy2(PROD_DB + suffix, dst + suffix)
    if os.path.realpath(dst) == PROD_DB:
        raise RuntimeError("копия совпала с рабочей БД")
    con = db.init_db(dst)
    con.close()
    return dst


def scope_accounts(path, limit=ACCOUNT_LIMIT):
    """Оставить в тире A ровно `limit` аккаунтов копии (остальные -> тир Z).

    Так сценарий детерминирован: какие аккаунты пойдут в прогон, известно
    заранее, и мок-транспорт настраивается точно на них. Работаем на КОПИИ
    боевой базы, боевые данные не трогаем.
    """
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT handle FROM accounts WHERE tier='A' AND status IN"
            " ('active','provisional','candidate') ORDER BY handle LIMIT ?", (limit,)
        ).fetchall()
        handles = [r["handle"] for r in rows]
        marks = ",".join("?" * len(handles))
        con.execute(f"UPDATE accounts SET tier='Z' WHERE tier='A' AND handle NOT IN ({marks})",
                    handles)
        con.commit()
        return handles
    finally:
        con.close()


def ssr_for(handles, clock):
    tr = FakeSsrTransport(clock=clock)
    for h in handles:
        tr.set(h)
    return tr


class MockNitter:
    """Подменный транспорт Nitter: health жив, фиды по режиму аккаунта."""

    def __init__(self, clock, modes=None):
        self.clock = clock
        self.modes = dict(modes or {})

    def __call__(self, url, timeout):
        path = urllib.parse.urlparse(url).path
        if path.endswith("/nasa/rss"):
            return 200, {}, make_feed(20)
        handle = path.strip("/").split("/")[0].lower()
        mode = self.modes.get(handle.lower(), "ok")
        # разные id на разные аккаунты: сдвигаем старт по хэшу handle
        shift = sum(ord(c) for c in handle) % 500
        start = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc) - timedelta(
            minutes=shift)
        if mode == "ok":
            return 200, {}, make_feed(20, handle=handle, start=start)
        if mode == "empty":
            return 200, {}, make_feed(0)
        if mode == "404":
            return 404, {}, "<html>not found</html>"
        return 0, {}, "__transport_error__:ConnectionRefusedError: refused"


def run_collect(path, handles, *, nitter_transport=None, ssr_transport,
                instances=None, max_accounts=None, cap=None, real=False):
    """Прогон collect на копии. Возвращает (summary, router, con, ssr_transport)."""
    clock = None if real else VClock()
    kwargs = {"db_path": path, "instances": instances}
    if nitter_transport is not None:
        kwargs["nitter_transport"] = nitter_transport
    if ssr_transport is not None:
        kwargs["ssr_transport"] = ssr_transport
    if not real:
        kwargs["clock"] = clock
        kwargs["sleeper"] = clock.advance
    router = channels.ChannelRouter(**kwargs)
    con = db.connect(path)
    old_cap = config.XSSR_MAX_PER_RUN
    if cap is not None:
        config.XSSR_MAX_PER_RUN = cap
    try:
        rid = db.start_run(con, "acceptance:tz10")
        router.set_run_id(rid)
        s = collect.collect_tier(con, router.nitter, "A", max_accounts=max_accounts,
                                 run_id=rid, router=router)
    finally:
        config.XSSR_MAX_PER_RUN = old_cap
    return s, router, con, ssr_transport


def reserve_row(con):
    """Суммарный счётчик фактических отказов сбора по всем инстансам."""
    return con.execute(
        "SELECT COALESCE(SUM(collect_fail_streak),0) n FROM instances").fetchone()["n"]


def summary_line(s):
    return (f"аккаунтов={s['accounts_total']} ok={s['accounts_ok']} "
            f"fail={s['accounts_fail']} x_ssr={s.get('ssr_used', 0)} "
            f"резерв={s.get('reserve_used', 0)} "
            f"отказов_Nitter={s.get('nitter_fails', 'n/a')} "
            f"потеряно={s.get('accounts_lost', s['accounts_fail'])} "
            f"причина={s.get('reserve_reason', 'n/a')}")


def git_worktree(ref):
    """Worktree предыдущей ревизии для замера «ДО» (если git доступен)."""
    try:
        subprocess.run(["git", "rev-parse", "--verify", ref], cwd=ROOT,
                       capture_output=True, text=True, check=True)
    except Exception as exc:  # noqa: BLE001
        return None, f"нет ревизии {ref}: {exc}"
    wt = tempfile.mkdtemp(prefix="tuber_x_before10_")
    r = subprocess.run(["git", "worktree", "add", "--detach", wt, ref], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, f"git worktree не создан: {r.stderr.strip()[:200]}"
    shutil.copy2(os.path.join(ROOT, "tools", "measure_tz10.py"),
                 os.path.join(wt, "tools", "measure_tz10.py"))
    return wt, None


# ===================================================================== main
def main():
    started = datetime.now(timezone.utc)
    hr("ПРИЁМКА ТЗ-10 (2.1–2.3): резерв по факту отказов, без потери аккаунтов")
    p(f"начало:          {started:%Y-%m-%d %H:%M:%S} UTC")
    p(f"рабочая БД (RO): {PROD_DB}")
    p(f"каталог reports: {REPORTS_DIR}")
    p(f"временный каталог приёмки: {TMP}")
    if not os.path.exists(PROD_DB):
        p(f"ОШИБКА: рабочая БД не найдена: {PROD_DB}")
        return 2

    reports_before = dir_snapshot(REPORTS_DIR)
    db_before = prod_snapshot(PROD_DB)
    p("")
    p(f"боевая БД ДО:  {short(db_before)}")
    p(f"файлов reports/: {len(reports_before)}")

    # ------------------------------------------------------------- ДО/ПОСЛЕ
    hr("ЗАМЕР ДО/ПОСЛЕ: Nitter мёртв (реальный 127.0.0.1:9), резерв подменён")
    before_ref = os.environ.get("TZ10_BEFORE_REF", "HEAD~1")
    before_dir, err = git_worktree(before_ref)
    if before_dir:
        r = subprocess.run([sys.executable, "tools/measure_tz10.py", "2"], cwd=before_dir,
                           capture_output=True, text=True)
        p(f"ДО  ({before_ref}, старый код):")
        for line in (r.stdout or r.stderr).strip().splitlines():
            p("   " + line)
    else:
        p(f"ДО  — замер не выполнен ({err}); приёмка продолжается")
    r = subprocess.run([sys.executable, "tools/measure_tz10.py", "2"], cwd=ROOT,
                       capture_output=True, text=True)
    p("ПОСЛЕ (текущий код):")
    for line in (r.stdout or r.stderr).strip().splitlines():
        p("   " + line)
    after_line = (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else ""
    after_ok = r.returncode == 0 and "fail=0" in after_line and "потеряно=0" in after_line

    # ================================================================= П1
    hr("П1. Nitter здоров: резерв не используется")
    path = fresh_copy("p1")
    clock = VClock()
    handles = scope_accounts(path)
    modes = {h: "ok" for h in handles}
    ssr = ssr_for(handles, clock)
    s, router, con, _ = run_collect(path, handles, nitter_transport=MockNitter(clock, modes),
                                    ssr_transport=ssr)
    p(f"  {summary_line(s)}")
    routed = [d["handle"] for d in s["details"] if d.get("fallback") == "x_ssr"]
    check(1, "Nitter здоров: обращений к резерву 0, ни один аккаунт не через резерв",
          s["reserve_used"] == 0 and ssr.calls == [] and not routed
          and s["accounts_lost"] == 0,
          f"резерв={s['reserve_used']} вызовов_x_ssr={len(ssr.calls)} "
          f"через_резерв={routed} потеряно={s['accounts_lost']}")
    router.close()
    con.close()

    # ================================================================= П2
    hr("П2. Nitter недоступен с самого начала: КАЖДЫЙ аккаунт через резерв")
    path = fresh_copy("p2")
    clock = VClock()
    handles = scope_accounts(path, 2)
    ssr = ssr_for(handles, clock)
    # РЕАЛЬНЫЙ мёртвый адрес (соединение отклонено локально), реальные часы
    s, router, con, _ = run_collect(path, handles, nitter_transport=None,
                                    ssr_transport=ssr, instances=["http://127.0.0.1:9"],
                                    max_accounts=len(handles), real=True)
    line = summary_line(s)
    p(f"  ИТОГОВАЯ СТРОКА: {line}")
    rows = db.connect(path)
    route = [d["handle"] for d in s["details"]
             if d.get("ok") and d.get("fallback") == "x_ssr"]
    reserve_since = db.reserve_active_since(rows)
    rows.close()
    check(2, "Nitter мёртв с начала: каждый аккаунт через резерв, fail=0, потерь 0",
          s["reserve_used"] == len(handles) and s["accounts_fail"] == 0
          and s["accounts_lost"] == 0 and len(route) == len(handles)
          and reserve_since is not None,
          f"{line}; через_резерв={route}; резерв_активен_с={reserve_since}")
    router.close()
    con.close()

    # ================================================================= П3
    hr("П3. Отказ Nitter только по одному аккаунту (остальные отдают фид)")
    path = fresh_copy("p3")
    clock = VClock()
    handles = scope_accounts(path, 3)
    modes = {h: "ok" for h in handles}
    modes[handles[1]] = "dead"          # только один сломан
    ssr = ssr_for(handles, clock)
    s, router, con, _ = run_collect(path, handles, nitter_transport=MockNitter(clock, modes),
                                    ssr_transport=ssr)
    p(f"  {summary_line(s)}")
    routed = [d["handle"] for d in s["details"] if d.get("fallback") == "x_ssr"]
    check(3, "через резерв ровно один (сломанный) аккаунт, системного переключения нет",
          s["reserve_used"] == 1 and routed == [handles[1]]
          and s["accounts_ok"] == len(handles) and s["accounts_lost"] == 0,
          f"резерв={s['reserve_used']} через_резерв={routed} "
          f"ожидался=[{handles[1]}] ok={s['accounts_ok']}")
    router.close()
    con.close()

    # ================================================================= П4
    hr("П4. 404 и пустая лента — не отказ Nitter: счётчик не растёт, резерв молчит")
    path = fresh_copy("p4")
    clock = VClock()
    handles = scope_accounts(path, 3)
    modes = {handles[0]: "404", handles[1]: "empty", handles[2]: "ok"}
    ssr = ssr_for(handles, clock)
    s, router, con, _ = run_collect(path, handles, nitter_transport=MockNitter(clock, modes),
                                    ssr_transport=ssr)
    p(f"  {summary_line(s)}")
    fails = reserve_row(con)
    check(4, "404 и пустая лента не считаются отказом: счётчик отказов = 0, резерв не включён",
          fails == 0 and s["reserve_used"] == 0 and ssr.calls == []
          and s.get("nitter_fails", 0) == 0,
          f"сумма_счётчиков_отказов={fails} резерв={s['reserve_used']} "
          f"вызовов_x_ssr={len(ssr.calls)} отказов_Nitter={s.get('nitter_fails')}")
    router.close()
    con.close()

    # ================================================================= П5
    hr("П5. Успешный фид обнуляет счётчик, снимает деградацию и резерв не нужен")
    path = fresh_copy("p5")
    clock = VClock()
    handles = scope_accounts(path, 2)
    ssr = ssr_for(handles, clock)
    # прогон 1: Nitter мёртв => резерв, деградация включена
    dead = MockNitter(clock, {h: "dead" for h in handles})
    s1, router, con, _ = run_collect(path, handles, nitter_transport=dead,
                                     ssr_transport=ssr, max_accounts=1)
    since1 = db.reserve_active_since(con)
    degraded1 = router.nitter.all_degraded()
    router.close()
    con.close()
    # прогон 2: Nitter ожил
    clock2 = VClock()
    ok = MockNitter(clock2, {h: "ok" for h in handles})
    ssr2 = ssr_for(handles, clock2)
    con = db.connect(path)
    con.execute("UPDATE accounts SET last_success_at=NULL")
    con.commit()
    con.close()
    s2, router, con, _ = run_collect(path, handles, nitter_transport=ok,
                                     ssr_transport=ssr2)
    p(f"  прогон1 (Nitter мёртв):  {summary_line(s1)} резерв_активен_с={since1}")
    p(f"  прогон2 (Nitter ожил):   {summary_line(s2)}")
    fails2 = reserve_row(con)
    since2 = db.reserve_active_since(con)
    degraded2 = router.nitter.all_degraded()
    check(5, "после успешного фида счётчик=0, деградация снята, резерв не используется",
          s1["reserve_used"] >= 1 and since1 is not None and degraded1 is True
          and fails2 == 0 and degraded2 is False and since2 is None
          and s2["reserve_used"] == 0 and s2["accounts_lost"] == 0,
          f"прогон1 резерв={s1['reserve_used']} деградация={degraded1}; "
          f"прогон2 счётчик={fails2} деградация={degraded2} "
          f"резерв_активен_с={since2} резерв={s2['reserve_used']}")
    router.close()
    con.close()

    # ================================================================= П6
    hr("П6. Потолок XSSR_MAX_PER_RUN соблюдается (искусственно 1)")
    path = fresh_copy("p6")
    clock = VClock()
    handles = scope_accounts(path, 2)
    ssr = ssr_for(handles, clock)
    dead = MockNitter(clock, {h: "dead" for h in handles})
    s, router, con, _ = run_collect(path, handles, nitter_transport=dead,
                                    ssr_transport=ssr, cap=1)
    p(f"  {summary_line(s)} (потолок={s.get('reserve_cap')})")
    p(f"  вызовов x_ssr: {len(ssr.calls)}")
    check(6, "при потолке 1 второй аккаунт в резерв не идёт",
          s["reserve_used"] == 1 and len(ssr.calls) == 1
          and s.get("reserve_capped") is True and s["accounts_lost"] == 1,
          f"резерв={s['reserve_used']} вызовов_x_ssr={len(ssr.calls)} "
          f"потолок_достигнут={s.get('reserve_capped')} потеряно={s['accounts_lost']}")
    router.close()
    con.close()

    # ================================================================= П8
    hr("П8. Миграция схемы: user_version повышен, повторный запуск идемпотентен")
    raw = os.path.join(TMP, "p8_raw.db")
    shutil.copy2(PROD_DB, raw)
    con0 = sqlite3.connect(raw)
    v_before = con0.execute("PRAGMA user_version").fetchone()[0]
    con0.close()
    con1 = db.init_db(raw)
    v_after = con1.execute("PRAGMA user_version").fetchone()[0]
    cols = {r[1] for r in con1.execute("PRAGMA table_info(instances)")}
    con1.close()
    con2 = db.init_db(raw)         # повторный init
    v_again = con2.execute("PRAGMA user_version").fetchone()[0]
    cols2 = {r[1] for r in con2.execute("PRAGMA table_info(instances)")}
    con2.close()
    check(8, "миграция отработала: user_version повышен, повторный запуск идемпотентен",
          v_before < v_after and v_after == db.SCHEMA_VERSION >= 6
          and v_again == v_after
          and {"collect_fail_streak", "reserve_since"} <= cols
          and cols == cols2,
          f"user_version {v_before}->{v_after}->{v_again}; "
          f"колонки={sorted({'collect_fail_streak', 'reserve_since'} & cols)}")

    # ================================================================= П9
    hr("П9. Двойной прогон по копии: дублей постов нет (UNIQUE(tweet_id))")
    path = fresh_copy("p9")
    handles = scope_accounts(path, 2)
    clock = VClock()
    ok1 = MockNitter(clock, {h: "ok" for h in handles})
    ssr1 = ssr_for(handles, clock)
    s1, router, con, _ = run_collect(path, handles, nitter_transport=ok1, ssr_transport=ssr1)
    router.close()
    con.close()
    clock2 = VClock()
    ok2 = MockNitter(clock2, {h: "ok" for h in handles})
    ssr2 = ssr_for(handles, clock2)
    s2, router, con, _ = run_collect(path, handles, nitter_transport=ok2, ssr_transport=ssr2)
    total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    distinct = con.execute("SELECT COUNT(DISTINCT tweet_id) FROM posts").fetchone()[0]
    check(9, "двойной прогон не создаёт дублей постов (уникальность tweet_id сохранена)",
          total == distinct and s2["posts_new"] == 0,
          f"прогон1 новых={s1['posts_new']}; прогон2 новых={s2['posts_new']} "
          f"(обновлено={s2['posts_upd']}); всего_постов={total} уникальных={distinct}")
    router.close()
    con.close()

    # ================================================================= П7
    hr("П7. Полный набор тестов (мок-транспорт, без сети)")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                          capture_output=True, text=True, env=dict(os.environ))
    tail = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    check(7, "полный набор тестов зелёный", proc.returncode == 0,
          f"pytest rc={proc.returncode}; {tail}")

    # ============================================== сторож: новые проверки 2.3
    hr("Проверка 2.3 (сторож): ALERT при резерве дольше 6 ч и при отказах сбора")
    hpath = fresh_copy("health")
    hcon = db.connect(hpath)
    pre = {c["name"]: c["alert"] for c in health.run(hcon)["checks"]}
    p(f"  в норме: reserve_active alert={pre.get('reserve_active')}, "
      f"collect_failures alert={pre.get('collect_failures')}")
    old = db.iso(datetime.now(timezone.utc) - timedelta(hours=7))
    db.mark_reserve_active(hcon, when=old)
    r1 = health.check_reserve_active(hcon)
    p(f"  резерв 7 ч -> {r1['msg']} (alert={r1['alert']})")
    # резерв оставляем активным: сторож обязан увидеть его в этом же прогоне
    for _ in range(config.HEALTH_COLLECT_FAIL_PER_HOUR + 1):
        hcon.execute("INSERT INTO requests (host, ts, kind, status) VALUES ('h',?,?,?)",
                     (db.utcnow_iso(), "feed", 0))
    hcon.commit()
    r2 = health.check_collect_failures(hcon)
    p(f"  отказы сбора -> {r2['msg']} (alert={r2['alert']})")
    alerts = [c["name"] for c in health.run(hcon)["alerts"]]
    p(f"  ALERT-проверки прогона сторожа: {alerts}")
    check(20, "сторож: в норме новые проверки молчат, резерв>6ч и отказы сбора дают ALERT",
          pre.get("reserve_active") is False and pre.get("collect_failures") is False
          and r1["alert"] is True and r2["alert"] is True
          and "reserve_active" in alerts and "collect_failures" in alerts,
          f"норма reserve_active={pre.get('reserve_active')} "
          f"collect_failures={pre.get('collect_failures')}; "
          f"резерв7ч={r1['alert']}; отказы={r2['alert']}")
    hcon.close()

    # --------------------------------------------------- боевая БД и reports
    hr("Боевая БД (только чтение) и каталог reports/")
    db_after = prod_snapshot(PROD_DB)
    reports_after = dir_snapshot(REPORTS_DIR)
    p(f"боевая БД ДО:    {short(db_before)}")
    p(f"боевая БД ПОСЛЕ: {short(db_after)}")
    check(21, "боевая БД не изменена приёмкой (счётчики ДО/ПОСЛЕ совпали)",
          db_before == db_after,
          f"совпало={db_before == db_after}")
    check(22, "каталог reports/ не изменён (sha256 файлов совпали)",
          reports_before == reports_after,
          f"файлов={len(reports_after)}; совпало={reports_before == reports_after}")
    check(23, "замер ПОСЛЕ: Nitter мёртв -> каждый аккаунт через резерв, потерь 0",
          after_ok, after_line)

    # --------------------------------------------------------- сводка
    hr("СВОДКА")
    ok_n = sum(1 for r in _results if r["ok"])
    p(f"проверок: {len(_results)}, OK: {ok_n}, FAIL: {len(_results) - ok_n}")
    for r in _results:
        p(f"  {r['num']:>3} {'OK  ' if r['ok'] else 'FAIL'} {r['what']}")

    if before_dir:
        subprocess.run(["git", "worktree", "remove", "--force", before_dir], cwd=ROOT,
                       capture_output=True, text=True)
    out_path = os.path.join(ROOT, "docs", "acceptance-log-10.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    p(f"\nжурнал приёмки сохранён: {out_path}")
    return 0 if all(r["ok"] for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
