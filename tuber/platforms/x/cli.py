"""CLI Tuber-x (Р1). Все команды идемпотентны.

    python3 -m tuber x init
    python3 -m tuber x instances --check
    python3 -m tuber x registry add <handle> --tier A --source <запрос>
    python3 -m tuber x registry add-file <путь.csv>
    python3 -m tuber x registry list [--status active] [--tier A]
    python3 -m tuber x registry verify <handle>
    python3 -m tuber x collect --tier A|B|C --max-accounts N [--dry-run]
    python3 -m tuber x backfill <handle> --pages 3
    python3 -m tuber x budget
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

from . import (blocklist, channels, classify, collect, config, discover, enrich,
               feeds, health, registry, report, scoring, scores, seeds, stories,
               store as db)
from .broker import NitterBroker, NitterError


def _broker(args, run_id=None):
    return NitterBroker(instances=config.nitter_instances(), run_id=run_id)


def _router(args, run_id=None):
    """Роутер каналов ТЗ-4: единственный владелец квоты всех каналов."""
    return channels.ChannelRouter(instances=config.nitter_instances(), run_id=run_id)


# --------------------------------------------------------------------- init
def cmd_init(args):
    con = db.init_db()
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    wal = con.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"БД: {config.DB_PATH}")
    print(f"journal_mode: {wal}")
    print("таблицы:", ", ".join(tables))
    con.close()
    return 0


# ---------------------------------------------------------------- instances
def cmd_instances(args):
    broker = _broker(args)
    print(f"Пул инстансов ({len(broker.instances)}):")
    if args.check:
        rows = []
        for host in broker.instances:
            res = broker.check_instance(host)
            rows.append(res)
            flag = "ЖИВОЙ" if res["healthy"] else "не живой"
            print(f"  {host:32s} {flag:10s} http={res['status']:>3} items={res['items']:>3}"
                  f" latency={res['latency_ms']:>5}ms {res['error'] or ''}")
        alive = [r for r in rows if r["healthy"]]
        print(f"Живых: {len(alive)} из {len(rows)} (порог: HTTP 200 и items >= "
              f"{config.HEALTH_MIN_ITEMS})")
        broker.close()
        return 0 if len(alive) >= 2 else 1
    st = broker.stats()
    for host, s in st.items():
        print(f"  {host:32s} запросов_сегодня={s['requests_today']:>6} "
              f"({s['used_pct']}% потолка) cooldown={s['cooldown_sec']}s "
              f"healthy={s['healthy']}")
    broker.close()
    return 0


# ----------------------------------------------------------------- registry
def cmd_registry_add(args):
    con = db.init_db()
    res = registry.add_account(con, args.handle, args.tier, args.source,
                               notes=args.notes, topic_guess=args.topic,
                               lang=args.lang, status=args.status)
    if not res["ok"]:
        print(f"отказ: {res['reason']} (хендл {args.handle!r})")
        con.close()
        return 2
    print(f"{'добавлен' if res['created'] else 'обновлён'}: @{res['handle']} "
          f"tier={res['tier']} status={args.status} source={args.source or '-'}")
    con.close()
    return 0


def cmd_registry_add_file(args):
    con = db.init_db()
    res = registry.add_file(con, args.path, default_tier=args.tier)
    print(f"add-file: добавлено={res['added']} обновлено={res['updated']} "
          f"отклонено={res['rejected']}")
    for e in res["errors"][:20]:
        print("  ", e)
    con.close()
    return 0


def cmd_registry_list(args):
    con = db.init_db()
    rows = registry.list_accounts(con, status=args.status, tier=args.tier,
                                  limit=args.limit)
    print(f"аккаунтов: {len(rows)}"
          + (f" (status={args.status})" if args.status else "")
          + (f" (tier={args.tier})" if args.tier else ""))
    print(f"{'handle':20s} {'tier':4s} {'status':10s} {'постов':>7s} {'п/сутки':>8s} "
          f"{'cv':>5s} {'last_success_at':20s} {'fail':>4s}")
    for r in rows:
        ppd = r["posts_per_day"] if r["posts_per_day"] is not None else 0.0
        cv = r["cv_interval"] if r["cv_interval"] is not None else 0.0
        print(f"@{r['handle']:19s} {r['tier']:4s} {r['status']:10s} "
              f"{r['posts_collected'] or 0:>7d} {ppd:>8.2f} {cv:>5.2f} "
              f"{str(r['last_success_at'] or '-'):20s} {r['fail_streak'] or 0:>4d}")
    con.close()
    return 0


def _print_verify(res):
    print(f"@{res['handle']}: {'ПРИНЯТ' if res['ok'] else 'ОТКЛОНЁН/В ОЧЕРЕДИ'}"
          + (f" — {res['reason']}" if res["reason"] else ""))
    print(f"  статус={res['status']} items={res['items']} "
          f"авторов_ядра={res['mentioners']} "
          f"({', '.join(res['mentioners_list']) or '-'}) "
          f"ai_density={res.get('ai_density')} источник_признаков={res['feature_source']}")
    f = res["features"]
    print(f"  постов/сутки={f['posts_per_day']} cv={f['cv_interval']} "
          f"ссылки={f['link_ratio']} ретвиты={f['rt_ratio']} дубли={f['dup_ratio']}")
    for k, v in res["checks"].items():
        print(f"    {'OK ' if v else 'FAIL'} {k}")


def cmd_registry_verify(args):
    con = db.init_db()
    broker = _broker(args)
    handle = getattr(args, "handle", None)
    if handle:
        run_id = db.start_run(con, f"verify:{handle}")
        broker.run_id = run_id
        res = registry.verify_account(con, broker, handle, run_id=run_id)
        db.finish_run(con, run_id, accounts_ok=1 if res["ok"] else 0,
                      accounts_fail=0 if res["ok"] else 1,
                      errors=1 if res.get("fetch_error") else 0)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            _print_verify(res)
        broker.close()
        con.close()
        return 0 if res["ok"] else 1
    # Р3.1: пакетная верификация верхних кандидатов очереди
    max_verify = args.max_verify or config.DISCOVERY_MAX_VERIFY_DEFAULT
    run_id = db.start_run(con, f"verify:batch:{max_verify}")
    broker.run_id = run_id
    res = discover.verify_candidates(con, broker, max_verify=max_verify, run_id=run_id)
    db.finish_run(con, run_id, accounts_ok=res["active"] + res["provisional"],
                  accounts_fail=res["rejected"], errors=0)
    print(f"=== верификация очереди (max-verify={max_verify})")
    print(f"проверено={res['checked']} active={res['active']} "
          f"provisional={res['provisional']} rejected={res['rejected']} "
          f"в_стоп-лист={res['blocked']}")
    if res["reasons"]:
        print("причины отказов:", ", ".join(f"{k}={v}" for k, v in
                                            sorted(res["reasons"].items())))
    for d in res["details"]:
        print(f"  @{d['handle']:20s} {str(d['status']):11s} "
              f"reason={d['reason'] or '-'} items={d['items']} "
              f"ai={d['ai_density']} core={d['mentioners']}")
    if res.get("promoted"):
        for p in res["promoted"]:
            print(f"  промоушен @{p['handle']} by={p['by']}")
    broker.close()
    con.close()
    return 0


def _simulate_tenure_on_copy(args):
    """ТЗ-2-ФИКС/П9: промоушен по тенуру выполняется ТОЛЬКО на копии рабочей БД.

    Рабочая БД из конфига не открывается на запись: из неё лишь копируется файл
    (вместе с `-wal`/`-shm`). Повышение и эмуляция дат идут на копии, после чего
    копия удаляется. Если копию создать нельзя — команда завершается с ошибкой.
    """
    prod = os.path.realpath(config.DB_PATH)
    if not os.path.exists(config.DB_PATH):
        print(f"ошибка: рабочая БД не найдена: {config.DB_PATH}")
        return 2
    tmpdir = tempfile.mkdtemp(prefix="tuber_x_sim_")
    copy_path = os.path.join(tmpdir, f"tuber_x_sim_{os.getpid()}.db")
    try:
        shutil.copy2(config.DB_PATH, copy_path)
        for suffix in ("-wal", "-shm"):
            src = config.DB_PATH + suffix
            if os.path.exists(src):
                shutil.copy2(src, copy_path + suffix)
    except OSError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"ошибка: не удалось создать копию рабочей БД ({e}); запись отменена")
        return 2

    # Защита от случайного совпадения копии с рабочей БД (например, путь не совпал).
    real_copy = os.path.realpath(copy_path)
    if real_copy == prod:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("ошибка: simulation_refuses_production_db — копия совпала с рабочей БД,"
              " запись отменена")
        return 3
    # ТЗ-6 задача 5: тот же запрет общим предохранителем (ловит любой способ
    # подставить рабочую БД вместо копии).
    try:
        db.ensure_not_production_db(copy_path, "симуляция промоушена")
    except RuntimeError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"ошибка: simulation_refuses_production_db — {e}")
        return 3

    try:
        con = db.connect(copy_path)
        run_id = db.start_run(con, "registry:promote:simulate")
        n = registry.simulate_tenure(con, days=args.days)
        print(f"[sim] рабочая БД не открывается; работаем на копии: {copy_path}")
        print(f"[sim] эмуляция непрерывного сбора: provisional_since сдвинут на "
              f"{args.days} дн. (затронуто provisional: {n})")
        promoted = registry.promote_provisional(con, simulate_days=args.days, run_id=run_id)
        db.finish_run(con, run_id, accounts_ok=len(promoted), errors=0)
        print(f"=== [sim] промоушен provisional -> active: {len(promoted)}")
        for p in promoted:
            extra = " ".join(f"{k}={v}" for k, v in p.items()
                             if k not in ("handle", "by"))
            print(f"  @{p['handle']:20s} by={p['by']} {extra}")
        if not promoted:
            print("  (нет подходящих: нужен путь а после bootstrap или 14 дней сбора)")
        con.close()
        print("рабочая БД не изменялась (симуляция выполнена на копии)")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


def cmd_registry_promote(args):
    """Р2-БИС.2/П9: промоушен provisional -> active (путь а или б)."""
    if args.simulate_tenure:
        return _simulate_tenure_on_copy(args)
    con = db.init_db()
    run_id = db.start_run(con, "registry:promote")
    promoted = registry.promote_provisional(con, run_id=run_id)
    db.finish_run(con, run_id, accounts_ok=len(promoted), errors=0)
    print(f"=== промоушен provisional -> active: {len(promoted)}")
    for p in promoted:
        extra = " ".join(f"{k}={v}" for k, v in p.items() if k not in ("handle", "by"))
        print(f"  @{p['handle']:20s} by={p['by']} {extra}")
    if not promoted:
        print("  (нет подходящих: нужен путь а после bootstrap или 14 дней сбора)")
    con.close()
    return 0


def cmd_discover(args):
    con = db.init_db()
    if args.report:
        discover.report(con)
        con.close()
        return 0
    broker = _broker(args)
    run_id = db.start_run(con, f"discover:{args.lang}:{args.budget}")
    broker.run_id = run_id
    summary = discover.run_cycle(con, broker, budget=args.budget, lang=args.lang,
                                 run_id=run_id)
    print(f"=== discover lang={args.lang} budget={args.budget}"
          f"{' (пропущено)' if summary['skipped'] else ''}")
    if summary["skipped"]:
        print(f"причина: {summary['reason']} "
              f"(расход за сутки {discover.search_requests_today(con)}"
              f"/{config.DISCOVERY_DAILY_BUDGET})")
    else:
        print(f"запросов={summary['requests']} страниц={summary['pages']} "
              f"постов={summary['posts']} кандидатов_найдено={summary['candidates_found']}")
        print("по рубрикам: " + ", ".join(
            f"{k}={v}" for k, v in summary["rubrics"].items() if v))
    verify_res = None
    if not summary["skipped"] and not args.no_verify:
        verify_res = discover.verify_candidates(con, broker, max_verify=args.max_verify,
                                                run_id=run_id)
        print(f"верификация: проверено={verify_res['checked']} "
              f"active={verify_res['active']} provisional={verify_res['provisional']} "
              f"rejected={verify_res['rejected']} в_стоп-лист={verify_res['blocked']}")
        if verify_res["reasons"]:
            print("причины отказов: " + ", ".join(
                f"{k}={v}" for k, v in sorted(verify_res["reasons"].items())))
    db.finish_run(con, run_id,
                  accounts_ok=(verify_res["active"] + verify_res["provisional"]) if verify_res else 0,
                  accounts_fail=verify_res["rejected"] if verify_res else 0,
                  errors=0)
    broker.close()
    con.close()
    return 0


# ------------------------------------------------------------------ collect
def cmd_collect(args):
    con = db.init_db()
    router = _router(args)
    broker = router.nitter
    run_id = None if args.dry_run else db.start_run(con, f"collect:{args.tier}")
    broker.run_id = run_id
    router.run_id = run_id
    summary = collect.collect_tier(
        con, broker, args.tier, max_accounts=args.max_accounts,
        dry_run=args.dry_run, min_interval_hours=args.min_interval_hours,
        run_id=run_id, router=router)
    print(f"=== collect tier={summary['tier']} "
          f"{'(dry-run: без записи в БД)' if summary['dry_run'] else ''}")
    print(f"аккаунтов: {summary['accounts_total']} ok={summary['accounts_ok']} "
          f"fail={summary['accounts_fail']} страниц={summary['pages']} "
          f"новых={summary['posts_new']} обновлено={summary['posts_upd']} "
          f"без_даты={summary['skipped_no_date']} x_ssr={summary.get('ssr_used', 0)}")
    # ТЗ-10 2.3: итоговая строка наблюдаемости резерва.
    print(f"резерв: аккаунтов_через_резерв={summary.get('reserve_used', 0)} "
          f"отказов_Nitter={summary.get('nitter_fails', 0)} "
          f"потеряно={summary.get('accounts_lost', summary['accounts_fail'])} "
          f"потолок={summary.get('reserve_cap', '?')} "
          f"причина={summary.get('reserve_reason', 'не требовалось')}")
    for d in summary["details"]:
        if d.get("ok"):
            print(f"  @{d['handle']:20s} items={d['items']:>3} pages={d['pages']} "
                  f"new={d['new']:>3} upd={d['upd']:>3} skipped_no_date={d['skipped_no_date']}")
        else:
            print(f"  @{d['handle']:20s} ОТКАЗ: {d['error']} "
                  f"(fail_streak={d['fail_streak']}, status={d['status']})")
    if args.verify_completeness:
        if summary["dry_run"]:
            print("--verify-completeness несовместим с --dry-run")
        else:
            res = collect.verify_completeness(con, broker, summary, args.tier)
            summary["verify_completeness"] = res
            print(f"=== полнота: повторный обход {res['accounts_checked']} аккаунтов, "
                  f"новых id = {res['new_ids']}")
            if res["new_ids"] == 0:
                print("НОРМА: 0 новых id — успеваем за потоком")
            else:
                print(f"ВНИМАНИЕ: {res['new_ids']} новых id при повторном обходе — "
                      f"не успеваем за потоком, учащайте обход (§5 методологии), "
                      f"окно не увеличивать")
    if not summary["dry_run"]:
        db.finish_run(con, run_id,
                      accounts_ok=summary["accounts_ok"],
                      accounts_fail=summary["accounts_fail"],
                      posts_new=summary["posts_new"],
                      posts_upd=summary["posts_upd"],
                      errors=summary["errors"])
    broker.close()
    con.close()
    return 0


# ------------------------------------------------------------- ТЗ-4: enrich
def cmd_enrich(args):
    con = db.init_db()
    router = _router(args)
    run_id = None if args.dry_run else db.start_run(con, f"enrich:{args.batch}")
    router.set_run_id(run_id)
    batch = args.limit if getattr(args, "limit", None) else args.batch
    summary = enrich.enrich_batch(con, router, batch=batch,
                                  backfill_days=args.backfill_days,
                                  dry_run=args.dry_run, run_id=run_id)
    scope = f", backfill {args.backfill_days} дн." if args.backfill_days else ""
    print(f"=== enrich (канал cdn_tweet{scope})"
          f"{' (dry-run: без записи в БД)' if args.dry_run else ''}")
    print(f"выбрано={summary['selected']} ok={summary['ok']} errors={summary['errors']} "
          f"invalid={summary['invalid']} not_found={summary['not_found']} "
          f"deleted={summary['deleted']} rate_limited={summary['rate_limited']}")
    print(f"текст_заполнен={summary['text_filled']} текст_сохранён(полный)="
          f"{summary['text_preserved']} нужно_добрать_через_Nitter={summary['needs_nitter']}"
          f" (в этой пачке: {summary.get('needs_nitter_batch', 0)})")
    if not summary["dry_run"]:
        db.finish_run(con, run_id, accounts_ok=summary["ok"],
                      accounts_fail=summary["errors"],
                      errors=summary["errors"],
                      note=f"enrich ok={summary['ok']}")
    cov = scoring.coverage(con)
    print(f"покрытие метриками: {cov['enriched']}/{cov['total']} "
          f"({(cov['ratio'] or 0):.1%}), медиана лайков={cov['likes_median']}")
    router.close()
    con.close()
    return 0


# ------------------------------------------------------------ ТЗ-5: fulltext
def cmd_fulltext(args):
    """ТЗ-5 задача 1: добор полного текста через Nitter RSS (резерв — x_ssr)."""
    con = db.init_db()
    router = _router(args)
    broker = router.nitter
    run_id = None if args.dry_run else db.start_run(con, "fulltext")
    broker.run_id = run_id
    router.set_run_id(run_id)
    summary = collect.refetch_fulltext(con, broker, router=router,
                                       limit=args.limit, run_id=run_id,
                                       dry_run=args.dry_run)
    print(f"=== fulltext (очередь неполного текста)"
          f"{' (dry-run: без записи)' if summary['dry_run'] else ''}")
    print(f"нужно={summary['before']} цель_в_очереди={summary['targets']} "
          f"аккаунтов={summary['accounts']} nitter_ok={summary['nitter_ok']} "
          f"x_ssr={summary['ssr_used']} отказов={summary['failed']} "
          f"осталось={summary['after']}")
    for d in summary["details"]:
        if d.get("error"):
            print(f"  @{d.get('handle','?')}: ОТКАЗ {d['error']}")
        else:
            print(f"  @{d['handle']:20s} источник={d['source']:6s} "
                  f"items={d['items']:>3} new={d['new']:>3} upd={d['upd']:>3}")
    if not summary["dry_run"]:
        db.finish_run(con, run_id, accounts_ok=summary["nitter_ok"] + summary["ssr_used"],
                      accounts_fail=summary["failed"], errors=summary["failed"],
                      note=f"fulltext before={summary['before']} after={summary['after']}")
    broker.close()
    con.close()
    return 0


# -------------------------------------------------------- ТЗ-4: synd_snapshot
def cmd_synd_snapshot(args):
    """Разовое уточнение по репостам. НЕ в регулярном расписании (ТЗ-4 2.3)."""
    con = db.init_db()
    router = _router(args)
    tier = (args.tier or "A").upper()
    rows = con.execute(
        "SELECT handle FROM accounts WHERE tier=? AND status IN"
        " ('active','provisional') ORDER BY ai_density DESC, posts_collected DESC"
        " LIMIT ?", (tier, args.accounts)).fetchall()
    run_id = db.start_run(con, f"synd_snapshot:{tier}")
    router.set_run_id(run_id)
    print(f"=== synd_snapshot (разовый канал, бюджет {config.SYND_DAILY_BUDGET}/сутки,"
          f" тир {tier}, запрошено аккаунтов: {len(rows)})")
    ok = quota = 0
    for r in rows:
        handle = r["handle"]
        try:
            posts = router.timeline_snapshot(handle)
        except channels.QuotaExceeded as e:
            quota += 1
            print(f"  @{handle:20s} бюджет канала исчерпан: {e}")
            break
        except Exception as e:
            print(f"  @{handle:20s} ОТКАТ канала: {e}")
            continue
        res = collect.store_posts(con, _account_id(con, handle), posts)
        for p in posts:
            con.execute(
                "UPDATE posts SET pinned=?, is_retweet=?, likes=COALESCE(?, likes),"
                " replies=COALESCE(?, replies), retweet_count=?, spread_src='synd',"
                " metrics_src=COALESCE(metrics_src,'synd'), metrics_at=?"
                " WHERE tweet_id=?",
                (p.get("pinned", 0), p.get("is_retweet", 0), p.get("likes"),
                 p.get("replies"), p.get("retweet_count"), db.utcnow_iso(),
                 p["tweet_id"]))
        con.commit()
        ok += 1
        pinned = sum(1 for p in posts if p.get("pinned"))
        rts = sum(1 for p in posts if p.get("is_retweet"))
        print(f"  @{handle:20s} постов={len(posts)} новых={res['new']} "
              f"закреплённых={pinned} ретвитов={rts}")
    db.finish_run(con, run_id, accounts_ok=ok, errors=quota)
    print(f"итог: аккаунтов_уточнено={ok} бюджет_исчерпан={quota}")
    if quota:
        print("канал разовый: в этом окне больше не повторяем, сбор идёт через Nitter")
    router.close()
    con.close()
    return 0


def _account_id(con, handle):
    row = con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()
    return row["id"] if row else None


# --------------------------------------------------------------- ТЗ-4: health
def cmd_health(args):
    con = db.init_db()
    run_id = db.start_run(con, "health")
    res = health.run(con, run_id=run_id)
    db.finish_run(con, run_id, accounts_ok=0, accounts_fail=0,
                  errors=len(res["alerts"]), note="health")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(health.format_report(res))
    con.close()
    return 0 if res["ok"] else 1


# --------------------------------------------------------------- ТЗ-3: classify
def cmd_classify(args):
    con = db.init_db()
    run_id = None if args.dry_run else db.start_run(con, "classify")
    summary = classify.run(con, limit=args.limit, batch=args.batch,
                           dry_run=args.dry_run, run_id=run_id,
                           use_prefilter=args.prefilter,
                           check_budget=not args.no_budget)
    print(f"=== classify (модель {summary['model']}, канал deepseek, партия "
          f"{args.batch}, предфильтр {'ВКЛ' if args.prefilter else 'ВЫКЛ'})"
          + (" (dry-run)" if args.dry_run else ""))
    if not summary["budget_ok"]:
        print(f"прогон не выполнен: {summary['budget_reason']}")
    print(f"выбрано={summary['selected']} классифицировано={summary['classified']} "
          f"эвристика(предфильтр)={summary['heuristic']} отказов={summary['failed']}")
    print(f"вызовов модели={summary['model_calls']} партий={summary['batches']} "
          f"стоимость=${summary['cost_usd']:.6f} "
          f"(дневной потолок {summary['daily_cap']}, израсходовано {summary['daily_used']})")
    cov = classify.coverage(con)
    print(f"покрытие темой: {cov['with_topic']}/{cov['classified']} "
          f"({(cov['ratio'] or 0):.1%}); постов всего {cov['total_posts']}, "
          f"heuristic={cov['heuristic']}, failed={cov['failed']}")
    if not summary["dry_run"]:
        db.finish_run(con, run_id, accounts_ok=summary["classified"],
                      accounts_fail=summary["failed"],
                      errors=summary["failed"],
                      note=f"classify ok={summary['classified']} "
                           f"cost=${summary['cost_usd']:.6f}")
    con.close()
    return 0


# ---------------------------------------------------------------- ТЗ-3: stories
def cmd_stories(args):
    con = db.init_db()
    run_id = None if args.dry_run else db.start_run(con, "stories")
    summary = stories.run(con, window_hours=args.window, threshold=args.threshold,
                          run_id=run_id, dry_run=args.dry_run)
    print(f"=== stories (окно {summary['window_hours']} ч, порог simhash "
          f"{summary['threshold']} из 64 бит)")
    print(f"постов в окне={summary['posts']} сюжетов={summary['stories']} "
          f"из них с xconf>=2={summary['multi']} одиночных={summary['single']} "
          f"новых сущностей={summary['new_entity']} suspect={summary['suspect']}")
    print(f"{'xconf':>5s} {'posts':>5s} {'lead_min':>8s} {'single':>6s} "
          f"{'suspect':>7s} рубрика | first_mover")
    for s in stories.main_stories(con, limit=args.limit, include_single=True):
        topics = ", ".join(stories._json_list(s["topics"]) or []) or "-"
        lead = "-" if s["lead_time_min"] is None else f"{s['lead_time_min']:.0f}"
        print(f"{s['xconf']:>5d} {s['post_count']:>5d} {lead:>8s} "
              f"{s['is_single']:>6d} {s['suspect']:>7d} {topics} | @{s['first_mover']}")
    if not summary["dry_run"]:
        db.finish_run(con, run_id, posts_new=summary["stories"],
                      note=f"stories={summary['stories']} "
                           f"threshold={summary['threshold']}")
    con.close()
    return 0


# ------------------------------------------- сквозной сюжет (несколько платформ)
def cmd_cross_stories(args):
    """Сюжеты, объединяющие материалы РАЗНЫХ платформ (ТЗ «сквозной сюжет»)."""
    from tuber.analysis import cross_stories
    from tuber.core import urls
    con = db.init_db()
    run_id = None if args.dry_run else db.start_run(con, "cross-stories")
    summary = cross_stories.run(
        con, window_hours=args.window, threshold=args.threshold,
        text_min=args.text_min, anchor_max_df=args.anchor_df, run_id=run_id,
        dry_run=args.dry_run)
    print(f"=== cross-stories (окно {summary['window_hours']} ч, порог "
          f"{summary['threshold']}, текст>={summary['text_min']}, "
          f"якорь df<={summary['anchor_max_df']})")
    print(f"content в окне={summary['posts']} (X-контекст={summary['context_posts']}) "
          f"пар-кандидатов={summary['candidate_pairs']} "
          f"принято пар={summary['positive_pairs']} "
          f"сквозных сюжетов={summary['stories']} "
          f"участников={summary['members']}")
    shown = 0
    for s in cross_stories.cross_stories(con):
        if shown >= args.limit:
            break
        members = cross_stories.story_members(con, s["id"])
        plats = sorted({m["platform"] for m in members})
        print(f"  сюжет {s['id']} xconf={s['xconf']} "
              f"участников={s['content_count']} платформы={', '.join(plats)}")
        for m in members:
            handle = m["author_handle"] or m["handle"]
            link = urls.material_url(
                m["url"], m["platform"], m["external_id"], handle)
            print(f"     {m['platform']:8s} {link or 'нет ссылки'}")
        shown += 1
    if not summary["dry_run"]:
        db.finish_run(con, run_id, posts_new=summary["stories"],
                      note=f"cross-stories={summary['stories']} "
                           f"members={summary['members']}")
    con.close()
    return 0


# ----------------------------------------------------------------- ТЗ-3: scores
def cmd_scores(args):
    con = db.init_db()
    run_id = None if args.dry_run else db.start_run(con, "scores")
    result = scores.run(con, persist=not args.dry_run, limit=args.limit)
    print(f"=== значимость (ТЗ-3 Р3): формула Р3.1/Р3.1-бис; "
          f"веса engage={config.SIGNIFICANCE_ENGAGE_W} "
          f"spread={config.SIGNIFICANCE_SPREAD_W} "
          f"затухание={config.SIGNIFICANCE_AGE_EXP}")
    if args.account:
        handle = args.account.lstrip("@").lower()
        print(f"аккаунт @{handle}: ретвиты не оцениваются как самостоятельные посты")
        rows = con.execute(
            "SELECT tweet_id, is_retweet, is_quote, links, text, likes, replies"
            " FROM posts WHERE lower(COALESCE(author_handle, owner_handle,''))=?"
            " ORDER BY published_at_utc DESC LIMIT 50", (handle,)).fetchall()
        if not rows:
            print("  (постов не найдено)")
        for r in rows:
            if not r["is_retweet"] and not r["is_quote"]:
                continue
            orig = scores.resolve_original_tweet_id(r)
            print(f"  {r['tweet_id']} ретвит/цитата -> оригинал "
                  f"{orig or 'не разрешён (ссылки нет)'}; likes={r['likes']}")
        top = scores.rank(con, limit=20)
        zero = [t["tweet_id"] for t in top if (t["likes"] or 0) == 0]
        print(f"  топ-20 значимости: нулевых лайков — {len(zero)}"
              f" ({', '.join(zero) if zero else 'нет'})")
    top = scores.rank(con, limit=min(args.limit or 20, 20))
    print(f"{'significance':>12s} {'branch':>10s} {'miss':>4s} {'xconf':>5s} "
          f"{'likes6h':>7s} {'age_h':>6s} handle")
    for r in top:
        age = "-" if r["metrics_age_hours"] is None else f"{r['metrics_age_hours']:.1f}"
        print(f"{r['significance']:>12.4f} {r['branch']:>10s} "
              f"{r['metrics_missing']:>4d} {r['xconf']:>5d} "
              f"{str(r['likes_at_6h']):>7s} {age:>6s} @{r['owner_handle'] or '?'}"
              f" ({r['tweet_id']})")
    if not top:
        print("  (оценок нет: сначала `cli stories` и `cli scores`)")
    fm = result["first_movers"]
    print(f"--- first_mover_score (полураспад {config.FIRST_MOVER_HALFLIFE_DAYS:.0f} дн.): "
          f"{len(fm)} аккаунтов")
    for h, d in sorted(fm.items(), key=lambda kv: -kv[1]["score"])[:5]:
        print(f"  @{h}: {d['score']:.3f} (primary {d['primary']} из {d['stories']})")
    print(f"--- тёмные лошадки (>= {config.DARKS_MIN_STORIES} сюжетов за неделю, "
          f"вне топ-{config.DARKS_TOP_N}): {len(result['darks'])}")
    for d in result["darks"][:10]:
        print(f"  @{d['handle']}: {d['stories_cur']} сюжетов (было "
              f"{d['stories_prev']}, рост {d['growth']})")
    sus = scores.suspect_stories(con)
    print(f"--- удалено устаревших оценок (выпали из выборки): "
          f"{result.get('removed_stale', 0)}")
    print(f"--- suspect-сюжеты: {len(sus)}")
    for s in sus:
        print(f"  сюжет {s['id']}: xconf={s['xconf']}, авторы с дублями")
    if not fm:
        print("  first_mover_score пуст: нет сюжетов (сначала `cli stories`)")
    if not result["darks"]:
        print("  тёмных лошадок нет: нужно >= "
              f"{config.DARKS_MIN_STORIES} сюжетов у аккаунта за неделю")
    if not args.dry_run:
        db.finish_run(con, run_id, posts_new=result["scored"],
                      note=f"scores={result['scored']} darks={len(result['darks'])}")
    con.close()
    return 0


# --------------------------------------------------------------- ТЗ-3: report
def cmd_report(args):
    con = db.init_db()
    date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    translator = False if args.no_model else None
    text = report.build(con, date=date, translator=translator)
    if not args.stdout_only:
        path = report.write(text, date=date)
        print(f"отчёт сохранён: {path}")
    print(text)
    # Задача 6 ТЗ виральности: «успешный» отчёт по пустым суткам маскировал
    # сломанный сбор. Пусто (0 постов или 0 строк значимости) — ненулевой код,
    # чтобы обёртка scripts/x/tuber_x_report.sh объявила ALERT, а сторож это заметил.
    start, end = report.day_bounds(date)
    status = report.data_status(con, start, end)
    con.close()
    if status["empty"]:
        print(f"ВНИМАНИЕ: за {date} нет данных — проверь сбор "
              f"(постов {status['posts']}, строк значимости {status['significance']})",
              file=sys.stderr)
        return 4
    return 0


def cmd_import_candidates(args):
    """ТЗ-18: приём кандидатов X из фидов (только наполнение очереди, без сети)."""
    con = db.init_db()
    try:
        res = feeds.import_candidates(con, args.feed, limit=args.limit, dry=args.dry)
    except feeds.FeedError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        con.close()
        return 2
    print(json.dumps(res, ensure_ascii=False))
    con.close()
    return 0


def cmd_export_candidates(args):
    """ТЗ-21/C: выгрузка очереди кандидатов в канонический JSONL-фид (только чтение)."""
    con = db.init_db()
    try:
        res = feeds.export_queue(con, args.out, limit=args.limit)
    except OSError as e:
        print(f"ошибка записи: {e}", file=sys.stderr)
        con.close()
        return 2
    print(json.dumps(res, ensure_ascii=False))
    con.close()
    return 0


def cmd_backfill(args):
    con = db.init_db()
    broker = _broker(args)
    run_id = db.start_run(con, f"backfill:{args.handle}")
    broker.run_id = run_id
    try:
        res = collect.backfill(con, broker, args.handle, pages=args.pages,
                               run_id=run_id)
    except (ValueError, NitterError) as e:
        db.finish_run(con, run_id, accounts_ok=0, accounts_fail=1, errors=1,
                      note=str(e)[:200])
        print(f"ошибка: {e}")
        broker.close()
        con.close()
        return 2
    print(f"=== backfill @{res['handle']}: страниц={res['pages']} новых={res['new']} "
          f"уже_было={res['upd']} без_даты={res['skipped_no_date']}")
    print(f"уникальных постов в БД по аккаунту: {res['unique_posts_total']}")
    broker.close()
    con.close()
    return 0


# ------------------------------------------------------------------- budget
def cmd_budget(args):
    con = db.init_db()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"=== бюджет Nitter за {day} (потолок {config.DAILY_REQUEST_CAP} запросов "
          f"на инстанс = 80% ёмкости)")
    rows = con.execute(
        """SELECT host, COUNT(*) n, SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) n429,
                  SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) n200,
                  SUM(COALESCE(items,0)) items
           FROM requests WHERE ts LIKE ? GROUP BY host ORDER BY n DESC""",
        (day + "%",)).fetchall()
    seen = set()
    for r in rows:
        seen.add(r["host"])
        pct = 100.0 * r["n"] / config.DAILY_REQUEST_CAP
        print(f"  {r['host']:32s} запросов={r['n']:>6} ({pct:5.2f}% потолка) "
              f"200={r['n200']:>5} items={r['items']:>6} 429={r['n429']}")
    for host in config.INSTANCES:
        if host.rstrip("/") not in {h.rstrip("/") for h in seen}:
            print(f"  {host:32s} запросов=     0 ( 0.00% потолка)")
    cd = con.execute(
        "SELECT COUNT(*) FROM instances WHERE cooldown_until IS NOT NULL AND"
        " cooldown_until > ?", (db.utcnow_iso(),)).fetchone()[0]
    alive = con.execute("SELECT COUNT(*) FROM instances WHERE healthy=1").fetchone()[0]
    tot = con.execute("SELECT COUNT(*) FROM requests WHERE ts LIKE ?",
                      (day + "%",)).fetchone()[0]
    n429 = con.execute("SELECT COUNT(*) FROM requests WHERE ts LIKE ? AND status=429",
                       (day + "%",)).fetchone()[0]
    share429 = (100.0 * n429 / tot) if tot else 0.0
    print(f"инстансов в cooldown: {cd} | живых по последней проверке: {alive}")
    print(f"всего запросов: {tot} | 429: {n429} ({share429:.2f}% — порог 10%)")
    r = con.execute("SELECT * FROM metrics_daily ORDER BY day DESC LIMIT 7").fetchall()
    if r:
        print("--- metrics_daily (последние дни)")
        for m in r:
            print(f"  {m['day']} ingested={m['posts_ingested']} dup={m['dup_rate']} "
                  f"coverage={m['coverage']} fail={m['fail_rate']} "
                  f"p95={m['latency_p95_min']} valid_date={m['valid_date_ratio']} "
                  f"alive={m['instances_alive']}")
    con.close()
    return 0


# ---------------------------------------------------------------- blocklist
def cmd_blocklist_list(args):
    con = db.init_db()
    rows = blocklist.list_blocked(con)
    print(f"в стоп-листе: {len(rows)} (служебные: {', '.join(sorted(blocklist.SERVICE_HANDLES))})")
    for r in rows:
        print(f"  @{r['handle']:20s} {r['reason']:16s} {r['added_at']}")
    con.close()
    return 0


def cmd_blocklist_add(args):
    con = db.init_db()
    ok = blocklist.add(con, args.handle, args.reason)
    print(f"{'добавлен в стоп-лист' if ok else 'не добавлен'}: @{args.handle} "
          f"reason={args.reason}")
    con.close()
    return 0


# --------------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(prog="tuber x", description="Tuber-x: ядро сбора")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="создать БД")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("instances", help="состояние пула инстансов")
    sp.add_argument("--check", action="store_true", help="проверить живость (items >= 15)")
    sp.set_defaults(func=cmd_instances)

    sp = sub.add_parser("registry", help="реестр аккаунтов")
    rsub = sp.add_subparsers(dest="rcmd", required=True)

    r = rsub.add_parser("add", help="добавить хендл (без сетевой проверки)")
    r.add_argument("handle")
    r.add_argument("--tier", default=config.DEFAULT_TIER)
    r.add_argument("--source", default=None)
    r.add_argument("--notes", default=None)
    r.add_argument("--topic", default=None)
    r.add_argument("--lang", default=None)
    r.add_argument("--status", default="candidate")
    r.set_defaults(func=cmd_registry_add)

    r = rsub.add_parser("add-file", help="добавить из CSV")
    r.add_argument("path")
    r.add_argument("--tier", default=None)
    r.set_defaults(func=cmd_registry_add_file)

    r = rsub.add_parser("list", help="показать реестр")
    r.add_argument("--status", default=None)
    r.add_argument("--tier", default=None)
    r.add_argument("--limit", type=int, default=None)
    r.set_defaults(func=cmd_registry_list)

    r = rsub.add_parser("verify", help="проверить кандидата или очередь (Р4.3, Р3.1)")
    r.add_argument("handle", nargs="?", default=None,
                   help="хендл; без него — пакетная проверка очереди")
    r.add_argument("--max-verify", type=int, default=None,
                   help=f"сколько верхних кандидатов проверить"
                        f" (по умолчанию {config.DISCOVERY_MAX_VERIFY_DEFAULT})")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_registry_verify)

    r = rsub.add_parser("promote", help="промоушен provisional -> active (Р2-БИС.2)")
    r.add_argument("--simulate-tenure", action="store_true",
                   help="эмулировать непрерывный сбор (П9)")
    r.add_argument("--days", type=int, default=config.TENURE_DAYS)
    r.set_defaults(func=cmd_registry_promote)

    sp = sub.add_parser("discover", help="дискавери и рост реестра (ТЗ-2)")
    sp.add_argument("--budget", type=int, default=config.DISCOVERY_DAILY_BUDGET,
                    help="бюджет поисковых запросов за прогон")
    sp.add_argument("--lang", default="all", choices=["all", "ru", "en"])
    sp.add_argument("--report", action="store_true", help="отчёт о росте реестра (Р5)")
    sp.add_argument("--max-verify", type=int, default=config.DISCOVERY_MAX_VERIFY_DEFAULT)
    sp.add_argument("--no-verify", action="store_true",
                    help="только собрать кандидатов, без верификации")
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("blocklist", help="стоп-лист дискавери (Р2.5)")
    bsub = sp.add_subparsers(dest="bcmd", required=True)
    b = bsub.add_parser("list")
    b.set_defaults(func=cmd_blocklist_list)
    b = bsub.add_parser("add")
    b.add_argument("handle")
    b.add_argument("--reason", default="manual")
    b.set_defaults(func=cmd_blocklist_add)

    sp = sub.add_parser("collect", help="обход реестра")
    sp.add_argument("--tier", required=True, choices=["A", "B", "C", "a", "b", "c"])
    sp.add_argument("--max-accounts", type=int, default=None)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verify-completeness", action="store_true")
    sp.add_argument("--min-interval-hours", type=float, default=None,
                    help="пропускать аккаунты, обойдённые раньше этого интервала")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("import_candidates",
                        help="приём кандидатов X из фидов описаний/постов (ТЗ-18)")
    sp.add_argument("--feed", action="append", required=True, dest="feed",
                    help="путь к JSONL-фиду (можно несколько)")
    sp.add_argument("--limit", type=int, default=config.FEED_IMPORT_LIMIT,
                    help=f"максимум принятых кандидатов за прогон"
                         f" (по умолчанию {config.FEED_IMPORT_LIMIT})")
    sp.add_argument("--dry", action="store_true", help="без записи в БД")
    sp.set_defaults(func=cmd_import_candidates)

    sp = sub.add_parser("export_candidates",
                        help="выгрузка очереди кандидатов в канонический фид (ТЗ-21)")
    sp.add_argument("--out", required=True, help="путь к выходному JSONL-фиду")
    sp.add_argument("--limit", type=int, default=None, help="максимум строк")
    sp.set_defaults(func=cmd_export_candidates)

    sp = sub.add_parser("backfill", help="бэкфилл по курсору")
    sp.add_argument("handle")
    sp.add_argument("--pages", type=int, default=3)
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser("budget", help="расход квоты за сутки")
    sp.set_defaults(func=cmd_budget)

    # ------------------------------------------------------------- ТЗ-4
    sp = sub.add_parser("enrich", help="обогащение метриками через cdn_tweet (ТЗ-4 2.2)")
    sp.add_argument("--batch", type=int, default=config.ENRICH_BATCH)
    sp.add_argument("--limit", type=int, default=None,
                    help="синоним --batch (ТЗ-3 П9)")
    sp.add_argument("--backfill-days", type=int, default=None,
                    help="бэкфилл метрик задним числом: только посты новее N дней")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("synd-snapshot",
                        help="разовое уточнение по репостам (ТЗ-4 2.3, не регулярно)")
    sp.add_argument("--tier", default="A")
    sp.add_argument("--accounts", type=int, default=config.SYND_DAILY_BUDGET)
    sp.set_defaults(func=cmd_synd_snapshot)

    sp = sub.add_parser("fulltext",
                        help="добор полного текста неполных постов (ТЗ-5 задача 1)")
    sp.add_argument("--limit", type=int, default=None,
                    help="сколько постов очереди обработать (по умолчанию все)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_fulltext)

    sp = sub.add_parser("health", help="сторож свежести и бюджетов каналов (ТЗ-4 Р6)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("scores", help="три оси и оценки значимости (ТЗ-3 Р3)")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--account", default=None,
                    help="показать ретвиты аккаунта и их оригиналы (ТЗ-3 П11)")
    sp.add_argument("--dry-run", action="store_true", help="без записи в БД")
    sp.set_defaults(func=cmd_scores)

    # ------------------------------------------------------------- ТЗ-3
    sp = sub.add_parser("classify", help="классификация постов моделью DeepSeek (ТЗ-3 Р1)")
    sp.add_argument("--limit", type=int, default=None, help="сколько постов обработать")
    sp.add_argument("--batch", type=int, default=config.CLASSIFY_BATCH)
    sp.add_argument("--prefilter", action="store_true",
                    help="включить предфильтр по словам config.AI_TEXT_INDICATORS"
                         " (по умолчанию ВЫКЛ: все посты уходят к модели, ТЗ-6 задача 1)")
    sp.add_argument("--no-budget", action="store_true",
                    help="не проверять бюджет DeepSeek перед прогоном")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_classify)

    sp = sub.add_parser("stories", help="сюжеты за окно (ТЗ-3 Р2)")
    sp.add_argument("--window", type=int, default=config.STORIES_WINDOW_HOURS)
    sp.add_argument("--threshold", type=float, default=None,
                    help="порог балла склейки сюжета (по умолчанию "
                         f"{config.STORY_MATCH_MIN})")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_stories)

    sp = sub.add_parser(
        "cross-stories",
        help="сквозные сюжеты: материалы одного события с разных платформ")
    sp.add_argument("--window", type=int, default=config.STORIES_WINDOW_HOURS)
    sp.add_argument("--threshold", type=float, default=None,
                    help="порог балла склейки (по умолчанию "
                         f"{config.STORY_MATCH_MIN})")
    sp.add_argument("--text-min", dest="text_min", type=float, default=None,
                    help="минимальная доля IDF-взвешенного пересечения токенов "
                         "(по умолчанию 0.30, см. docs/CROSS-STORIES.md)")
    sp.add_argument("--anchor-df", dest="anchor_df", type=int, default=None,
                    help="максимальная частота якорной сущности по корпусу "
                         "(по умолчанию 50)")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_cross_stories)

    sp = sub.add_parser("report", help="ежедневная выдача, 7 блоков (ТЗ-3 Р4)")
    sp.add_argument("--date", default=None, help="YYYY-MM-DD (по умолчанию сегодня UTC)")
    sp.add_argument("--no-model", action="store_true",
                    help="без перевода моделью (только русские шаблоны)")
    sp.add_argument("--stdout-only", action="store_true",
                    help="не сохранять в reports/, только напечатать")
    sp.set_defaults(func=cmd_report)
    return p


def _apply_db_override(argv):
    """Вынуть глобальный ``--db PATH`` из argv и переопределить путь к базе.

    Приоритет (ТЗ-3 §2): ``--db`` → ``TUBER_X_DB`` → ``TUBER_DB`` (их читает
    :mod:`tuber.platforms.x.config` при импорте) → ``data/tuber.db`` единой базы.
    Глобальный флаг работает в любой позиции: ``tuber x --db copy.db report``.
    """
    out, i, value = [], 0, None
    while i < len(argv):
        a = argv[i]
        if a == "--db":
            if i + 1 >= len(argv):
                raise SystemExit("--db требует путь")
            value = argv[i + 1]
            i += 2
            continue
        if a.startswith("--db="):
            value = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    if value:
        config.DB_PATH = value
        # тот же путь увидят модули, которые импортируют константу напрямую
        os.environ["TUBER_X_DB"] = value
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        argv = _apply_db_override(argv)
    except SystemExit as exc:
        print(exc.code or "неверные аргументы", file=sys.stderr)
        return 2
    if not argv:
        build_parser().print_help()
        return 2
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
