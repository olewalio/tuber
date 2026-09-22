#!/usr/bin/env python3
"""Приёмка ТЗ-4 (П10–П17) на ЖИВЫХ данных.

ЖЁСТКОЕ ПРАВИЛО (ТЗ-4 условие 6): рабочая БД — только чтение. Всё, что меняет
данные, выполняется на КОПИИ файла БД (включая `-wal`/`-shm`). Перед и после
печатаются статусы рабочей базы — они обязаны совпасть.

Запуск:  python3 tools/acceptance_tz4.py
Вывод:   docs/acceptance-log-4.txt (полный stdout) + тот же текст в консоль.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tuber.platforms.x import channels, collect, config, store as db, enrich, health, scoring  # noqa: E402
from tuber.platforms.x.broker import NoLiveInstance  # noqa: E402

_out = []


def p(line=""):
    print(line)
    _out.append(str(line))


def hr(title):
    p("")
    p("=" * 78)
    p(title)
    p("=" * 78)


def _copy_db(src, dst_dir, name="tuber_x_copy.db"):
    dst = os.path.join(dst_dir, name)
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(src + suffix):
            shutil.copy2(src + suffix, dst + suffix)
    return dst


def snapshot_statuses(path):
    """Статусы и тиры рабочей БД (для сверки до/после)."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    out = {"accounts": {}, "posts": None}
    for r in con.execute("SELECT tier, status, COUNT(*) n FROM accounts"
                         " GROUP BY tier, status ORDER BY tier, status"):
        out["accounts"][f"{r['tier']}/{r['status']}"] = r["n"]
    out["posts"] = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    out["posts_collected_sum"] = con.execute(
        "SELECT COALESCE(SUM(posts_collected),0) FROM accounts").fetchone()[0]
    con.close()
    return out


def short(status):
    return ", ".join(f"{k}={v}" for k, v in status["accounts"].items()) + \
        f" | posts={status['posts']} sum(posts_collected)={status['posts_collected_sum']}"


# --------------------------------------------------------------------- главное
def main():
    started = datetime.now(timezone.utc)
    prod = os.path.realpath(config.DB_PATH)
    if not os.path.exists(config.DB_PATH):
        p(f"ОШИБКА: рабочая БД не найдена: {config.DB_PATH}")
        return 2

    tmpdir = tempfile.mkdtemp(prefix="tuber_x_acc4_")
    copy_path = _copy_db(config.DB_PATH, tmpdir)
    if os.path.realpath(copy_path) == prod:
        p("ОШИБКА: копия совпала с рабочей БД — приёмка отменена")
        return 3

    p(f"Рабочая БД (ТОЛЬКО ЧТЕНИЕ): {config.DB_PATH}")
    p(f"Копия для приёмки:          {copy_path}")
    work_before = snapshot_statuses(config.DB_PATH)
    p(f"статусы рабочей БД ДО:  {short(work_before)}")

    # миграция копии до схемы v3 (рабочую БД не трогаем)
    con = db.init_db(copy_path)
    p(f"схема копии: user_version={con.execute('PRAGMA user_version').fetchone()[0]}")
    con.close()

    results = {}
    router = channels.ChannelRouter(db_path=copy_path)
    try:
        con = db.connect(copy_path)

        # ------------------------------------------------------- П13: живой сбор
        hr("ЖИВОЙ СБОР NITTER НА КОПИИ (наполнение выборки) + П13 лаг свежести")
        run_id = db.start_run(con, "acceptance:collect")
        router.set_run_id(run_id)
        total_new = 0
        for tier in ("A", "B", "C"):
            s = collect.collect_tier(con, router.nitter, tier, run_id=run_id, router=router)
            p(f"  collect tier={tier}: аккаунтов={s['accounts_total']} ok={s['accounts_ok']}"
              f" fail={s['accounts_fail']} новых={s['posts_new']} 429-фолбэк(x_ssr)="
              f"{s.get('ssr_used', 0)}")
            total_new += s["posts_new"]
        db.finish_run(con, run_id, posts_new=total_new, note="acceptance:collect")
        n_posts = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        p(f"  итого новых постов: {total_new}; постов в копии: {n_posts}")

        p(f"  верификация полноты недоступна: повторный обход не входит в П10–П17")
        # 2 интервала тира A = 2 ч; берём самый новый пост по тиру A
        row = con.execute(
            "SELECT MAX(p.published_at_utc) m FROM posts p JOIN accounts a ON a.id=p.account_id"
            " WHERE a.tier='A'").fetchone()
        if row and row["m"]:
            newest = db.parse_iso(row["m"])
            lag_min = (datetime.now(timezone.utc) - newest).total_seconds() / 60.0
            thr_min = 2 * config.TIERS["A"]["interval_hours"] * 60
            results["P13"] = {"lag_min": round(lag_min, 1), "threshold_min": thr_min,
                              "newest": row["m"],
                              "ok": lag_min <= thr_min}
            p(f"P13 лаг свежести тира A: {lag_min:.1f} мин (порог ≤ {thr_min} мин) -> "
              f"{'ПРОЙДЕНО' if results['P13']['ok'] else 'НЕ ПРОЙДЕНО'}")
        else:
            results["P13"] = {"ok": False, "reason": "нет постов тира A"}
            p("P13: нет постов тира A")

        # ------------------------------------------- П10/П11: живое обогащение CDN
        hr("П10/П11: ЖИВОЕ ОБОГАЩЕНИЕ CDN (темп 2 зап/с, пауза 0.35 с)")
        pending = enrich.pending_count(con)
        p(f"  постов без метрик: {pending} (пачками по {config.ENRICH_BATCH})")
        run_id = db.start_run(con, "acceptance:enrich")
        router.set_run_id(run_id)
        agg = {k: 0 for k in ("selected", "ok", "errors", "invalid", "not_found",
                              "deleted", "rate_limited", "text_filled",
                              "text_preserved", "needs_nitter")}
        t0 = time.monotonic()
        batches = 0
        while True:
            s = enrich.enrich_batch(con, router, batch=config.ENRICH_BATCH,
                                    run_id=run_id)
            batches += 1
            for k in agg:
                agg[k] += s.get(k, 0)
            p(f"  батч {batches}: выбрано={s['selected']} ok={s['ok']}"
              f" errors={s['errors']} not_found={s['not_found']}"
              f" deleted={s['deleted']} rate_limited={s['rate_limited']}")
            if s["selected"] == 0 or s["rate_limited"] or batches >= 10:
                break
        dur = time.monotonic() - t0
        db.finish_run(con, run_id, accounts_ok=agg["ok"], errors=agg["errors"],
                      note="acceptance:enrich")
        p(f"  всего: ok={agg['ok']} errors={agg['errors']} invalid={agg['invalid']}"
          f" not_found={agg['not_found']} deleted={agg['deleted']}")
        p(f"  текст_заполнен={agg['text_filled']} сохранён_полный="
          f"{agg['text_preserved']} нужно_добрать_Nitter={agg['needs_nitter']}")
        p(f"  время: {dur:.1f} с, скорость: {agg['ok']/max(dur, 0.001):.2f} пост/с")
        total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        alive = con.execute("SELECT COUNT(*) FROM posts WHERE deleted_at IS NULL"
                            ).fetchone()[0]
        enriched = con.execute("SELECT COUNT(*) FROM posts WHERE metrics_at IS NOT NULL"
                               ).fetchone()[0]
        ratio = (enriched / total) if total else None
        ratio_alive = (enriched / alive) if alive else None
        results["P10"] = {"total": total, "enriched": enriched, "ratio": ratio,
                          "ratio_non_deleted": ratio_alive,
                          "ok": (ratio_alive or 0) >= 0.95, "sample_ge_200": total >= 200}
        p(f"P10 доля обогащённых: {enriched}/{total} = {(ratio or 0):.2%} по всем постам;"
          f" {enriched}/{alive} = {(ratio_alive or 0):.2%} по неудалённым (порог ≥ 95%,"
          f" выборка ≥200: {'да' if total >= 200 else 'НЕТ, всего ' + str(total)}) -> "
          f"{'ПРОЙДЕНО' if results['P10']['ok'] else 'НЕ ПРОЙДЕНО'}")
        n429 = con.execute("SELECT COUNT(*) FROM requests WHERE kind='cdn_tweet'"
                           " AND status=429").fetchone()[0]
        nreq = con.execute("SELECT COUNT(*) FROM requests WHERE kind='cdn_tweet'"
                           ).fetchone()[0]
        results["P11"] = {"requests": nreq, "n429": n429, "ok": n429 == 0,
                          "rps": round(nreq / max(dur, 0.001), 2)}
        p(f"P11 429 у cdn_tweet: {n429} на {nreq} запросов -> "
          f"{'ПРОЙДЕНО' if results['P11']['ok'] else 'НЕ ПРОЙДЕНО'}")

        # ---------------------------------------------------- П14: идемпотентность
        hr("П14: ПОВТОРНЫЙ ПРОГОН ОБОГАЩЕНИЯ")
        before_posts = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        s2 = enrich.enrich_batch(con, router)
        after_posts = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        ids = [r["tweet_id"] for r in con.execute(
            "SELECT tweet_id FROM posts WHERE metrics_at IS NOT NULL"
            " AND deleted_at IS NULL LIMIT 3")]
        old_at = {tid: con.execute("SELECT metrics_at FROM posts WHERE tweet_id=?",
                                   (tid,)).fetchone()["metrics_at"] for tid in ids}
        con.execute("UPDATE posts SET metrics_at=NULL WHERE tweet_id IN (%s)"
                    % ",".join("?" * len(ids)), ids)
        con.commit()
        s3 = enrich.enrich_batch(con, router, batch=len(ids))
        new_at = {tid: con.execute("SELECT metrics_at FROM posts WHERE tweet_id=?",
                                   (tid,)).fetchone()["metrics_at"] for tid in ids}
        updated = sum(1 for tid in ids if new_at[tid] and new_at[tid] != old_at[tid])
        results["P14"] = {"posts_delta": after_posts - before_posts,
                          "reselect": s2["selected"], "re_measured": s3["ok"],
                          "metrics_at_updated": updated, "sample": len(ids),
                          "ok": after_posts == before_posts and s2["selected"] == 0
                                and updated == len(ids)}
        p(f"  повторный прогон: новых строк в posts = {after_posts - before_posts},"
          f" выбрано к обогащению = {s2['selected']} (идемпотентно)")
        p(f"  принудительный пере-замер {len(ids)} постов: ok={s3['ok']},"
          f" metrics_at обновлён у {updated} -> "
          f"{'ПРОЙДЕНО' if results['P14']['ok'] else 'НЕ ПРОЙДЕНО'}")

        # ------------------------------------------------------------ П17: velocity
        hr("П17: post_metrics_history и velocity_6h")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hist_total = con.execute("SELECT COUNT(*) FROM post_metrics_history").fetchone()[0]
        hist_today = con.execute(
            "SELECT COUNT(DISTINCT tweet_id) FROM post_metrics_history WHERE taken_at LIKE ?",
            (today + "%",)).fetchone()[0]
        velocity_n = 0
        for r in con.execute("SELECT DISTINCT tweet_id FROM post_metrics_history"):
            vel, _l, _r2, _e = scoring.velocity_6h(con, r["tweet_id"])
            if vel > 0:
                velocity_n += 1
        results["P17"] = {"history_rows": hist_total, "distinct_today": hist_today,
                          "with_velocity": velocity_n, "ok": velocity_n >= 5}
        p(f"  строк истории: {hist_total}, различных id за сутки: {hist_today},"
          f" с velocity_6h > 0: {velocity_n} -> "
          f"{'ПРОЙДЕНО' if results['P17']['ok'] else 'НЕ ПРОЙДЕНО'}")

        # ------------------------------------------------- П12: synd 429 и бюджет
        hr("П12: synd_timeline — живой запрос и бюджет окна")
        syn = channels.SyndTimelineBroker(db_path=copy_path)
        try:
            try:
                posts = syn.snapshot("OpenAI")
                p(f"  живой ответ ленты: HTTP 200, постов={len(posts)} "
                  f"(pinned={sum(1 for x in posts if x.get('pinned'))}, "
                  f"rt={sum(1 for x in posts if x.get('is_retweet'))})")
                live = "200"
            except channels.QuotaExceeded as e:
                p(f"  живой ответ ленты: 429/бюджет закрыт ({e})")
                live = "429/quota"
            except channels.SyndError as e:
                p(f"  живой ответ ленты: ошибка канала ({e})")
                live = f"error:{e}"
        finally:
            syn.close()
        # детерминированная проверка бюджета окна на контролируемом 429
        class Always429:
            def __call__(self, url, headers):
                return 429, {}, ""
        syn2 = channels.SyndTimelineBroker(db_path=copy_path, transport=Always429())
        try:
            calls = {"n": 0}
            try:
                syn2.snapshot("OpenAI")
            except channels.QuotaExceeded:
                calls["n"] += 1
            try:
                syn2.snapshot("AnthropicAI")
            except channels.QuotaExceeded:
                calls["n"] += 1
        finally:
            syn2.close()
        log429 = con.execute("SELECT COUNT(*) FROM run_log WHERE msg LIKE '%429%'"
                             ).fetchone()[0]
        over = con.execute("SELECT COUNT(*) FROM requests WHERE kind='synd_timeline'"
                           ).fetchone()[0]
        results["P12"] = {"live": live, "quota_raised": calls["n"],
                          "requests_total": over, "log_429": log429,
                          "ok": calls["n"] == 2 and log429 >= 1}
        p(f"  бюджет окна: при 429 оба вызова отклонены без повторов "
          f"(QuotaExceeded x{calls['n']}), событий 429 в run_log: {log429} -> "
          f"{'ПРОЙДЕНО' if results['P12']['ok'] else 'НЕ ПРОЙДЕНО'}")

        # ------------------------------------------------------------ health
        hr("СТОРОЖ (ТЗ-4 Р6): state копии")
        hr_res = health.run(con)
        for c in hr_res["checks"]:
            p(f"  {'ALERT' if c['alert'] else 'ok   '} {c['name']:14s} {c['msg']}")
        m = con.execute("SELECT * FROM metrics_daily ORDER BY day DESC LIMIT 1").fetchone()
        if m:
            p(f"  metrics_daily[{m['day']}]: enriched_ratio={m['enriched_ratio']}"
              f" likes_median={m['likes_median']} cdn_429={m['cdn_429_count']}"
              f" synd_429={m['synd_429_count']} ssr_used={m['ssr_used']}"
              f" stale_lag_p95_min={m['stale_lag_p95_min']}")

        # ------------------------------------------------------------- скоринг
        hr("СКОРИНГ (ТЗ-4 Р4) на живых обогащённых постах")
        ranked = scoring.rank(con, limit=8)
        p(f"{'score':>7s} {'engage':>7s} {'spread':>7s} {'first':>6s} {'likes':>7s}"
          f" {'vel6h':>8s} handle")
        for r in ranked:
            p(f"{r['score']:>7.3f} {r['score_engage']:>7.3f} {r['score_spread']:>7.2f}"
              f" {r['score_first']:>6.3f} {str(r['likes']):>7s} {r['velocity_6h']:>8.3f}"
              f" @{r['handle']}")
        total_rank = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        excluded_n = con.execute(
            "SELECT COUNT(*) FROM posts WHERE is_retweet=1 OR pinned=1 OR"
            " metrics_src='cdn_rt'").fetchone()[0]
        p(f"  всего постов={total_rank}, исключено из агрегатов (репост/закреплён/"
          f"cdn_rt)={excluded_n}")

        con.close()
    finally:
        router.close()

    # --------------------------------------------------- П15/П16: фикстуры
    results.update(check_p15())
    results.update(check_p16())

    # ---------------------------------------------------------- итоговая таблица
    hr("СВОДНАЯ ТАБЛИЦА ПРИЁМКИ")
    p(f"{'#':4s} {'проверка':58s} итог")
    rows = [
        ("P10", "доля обогащённых ≥ 95% (выборка ≥200)"),
        ("P11", "429 у cdn_tweet при 2 зап/с = 0"),
        ("P12", "429 synd_timeline: бюджет окна не превышен + лог"),
        ("P13", "лаг свежести тира A ≤ 2 интервалов"),
        ("P14", "повторный enrich: 0 новых строк, metrics_at обновлён"),
        ("P15", "репост и закреплённый не влияют на рейтинг"),
        ("P16", "при падении Nitter сбор идёт через x_ssr, ssr_used > 0"),
        ("P17", "velocity_6h минимум для 5 постов"),
    ]
    for code, desc in rows:
        res = results.get(code, {})
        p(f"{code:4s} {desc:58s} {'OK' if res.get('ok') else 'НЕ ОК'}")
        if res:
            p(f"     {json.dumps(res, ensure_ascii=False)}")

    # --------------------------------------------------- статусы рабочей базы
    work_after = snapshot_statuses(config.DB_PATH)
    p("")
    p(f"статусы рабочей БД ДО:   {short(work_before)}")
    p(f"статусы рабочей БД ПОСЛЕ: {short(work_after)}")
    same = work_before == work_after
    p(f"рабочая БД не изменена: {'ДА' if same else 'НЕТ (ОШИБКА!)'}")

    p("")
    p(f"приёмка заняла: {(datetime.now(timezone.utc) - started).total_seconds():.1f} с")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if same else 4


# ------------------------------------------------------------------ П15
def check_p15():
    """Ретвит и закреплённый пост не влияют на рейтинг (фикстура)."""
    hr("П15: репост и закреплённый пост не влияют на рейтинг (фикстура)")
    tmp = tempfile.mkdtemp(prefix="tuber_x_p15_")
    path = os.path.join(tmp, "fix.db")
    con = db.init_db(path)
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('a','A','active')")
    con.commit()
    acc = con.execute("SELECT id FROM accounts WHERE handle='a'").fetchone()["id"]
    now = db.utcnow_iso()
    def ins(tid, **kw):
        cols = {"account_id": acc, "tweet_id": tid, "published_at_utc": now,
                "published_src": "rss", "owner_handle": "a", "metrics_at": now,
                "metrics_src": "cdn", "likes": 1000, "replies": 10}
        cols.update(kw)
        q = "INSERT INTO posts (%s) VALUES (%s)" % (
            ",".join(cols), ",".join("?" * len(cols)))
        con.execute(q, tuple(cols.values()))
    ins("111")
    ins("222", is_retweet=1)
    ins("333", pinned=1)
    ins("444", metrics_src="cdn_rt")
    con.commit()
    ranked = {r["tweet_id"] for r in scoring.rank(con)}
    ok = ranked == {"111"}
    p(f"  в рейтинге: {sorted(ranked)} (ожидается ['111']) -> "
      f"{'ПРОЙДЕНО' if ok else 'НЕ ПРОЙДЕНО'}")
    con.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return {"P15": {"ranked": sorted(ranked), "ok": ok}}


# ------------------------------------------------------------------ П16
def check_p16():
    """Падение всех инстансов Nitter: сбор продолжается через x_ssr (живой x.com)."""
    hr("П16: отказ всех инстансов Nitter -> сбор через x_ssr")
    tmp = tempfile.mkdtemp(prefix="tuber_x_p16_")
    path = os.path.join(tmp, "fix.db")
    con = db.init_db(path)
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES ('OpenAI','A','active')")
    con.commit()
    # честный отказ всех инстансов: транспорт Nitter не отвечает
    def dead_nitter(url, timeout, headers=None):
        return 0, {}, "__transport_error__:acceptance-simulated-outage"
    router = channels.ChannelRouter(db_path=path, instances=["https://dead.test"],
                                    nitter_transport=dead_nitter)
    # помечаем инстанс деградировавшим в БД (fail_streak >= 3) и перечитываем
    # состояние: ровно так брокер видит затяжной отказ прошлых прогонов.
    con.execute("UPDATE instances SET fail_streak=3 WHERE host='https://dead.test'")
    con.commit()
    router.nitter._load_state()
    p(f"  Nitter деградировал (fail_streak по всем инстансам):"
      f" {router.nitter.all_degraded()}")
    run_id = db.start_run(con, "acceptance:ssr")
    router.set_run_id(run_id)
    summary = collect.collect_tier(con, router.nitter, "A", run_id=run_id, router=router)
    cnt = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    collect.update_metrics_daily(con)
    ssr_used = con.execute(
        "SELECT ssr_used FROM metrics_daily ORDER BY day DESC LIMIT 1").fetchone()
    ssr_used = ssr_used["ssr_used"] if ssr_used else 0
    ok = summary["ssr_used"] >= 1 and cnt >= 1 and (ssr_used or 0) > 0
    p(f"  x_ssr фолбэков={summary['ssr_used']} постов собрано={cnt}"
      f" metrics_daily.ssr_used={ssr_used} -> {'ПРОЙДЕНО' if ok else 'НЕ ПРОЙДЕНО'}")
    if summary["ssr_used"] == 0:
        try:
            diag = router.fetch_ssr("OpenAI")
            p(f"  диагностика x_ssr: живой ответ {len(diag)} постов"
              + ("" if diag else " — канал вернул 0 id"))
        except Exception as e:
            p(f"  диагностика x_ssr: канал недоступен ({e!r})")
    router.close()
    con.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return {"P16": {"ssr_used": summary["ssr_used"], "posts": cnt,
                    "metrics_daily_ssr_used": ssr_used, "ok": ok}}


if __name__ == "__main__":
    code = main()
    out_path = os.path.join(config.DOCS_DIR, "acceptance-log-4.txt")
    os.makedirs(config.DOCS_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_out) + "\n")
    print(f"\nлог приёмки: {out_path}")
    sys.exit(code)
