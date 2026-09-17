"""Обход реестра, курсоры и запись постов (Р5).

Расписание сборщик не держит: период приходит аргументом/конфигом, сам
обход запускается кроном (ТЗ-4).
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

from . import config, store as db, registry
from .broker import (NitterError, NoLiveInstance, NitterTransportError,
                            EmptyFeedError)

# Статусы, которые обходим: candidate и provisional тоже — иначе кандидата нечем
# измерить (Р4.3), а provisional не наберёт 10 постов для графа упоминаний (Р2-БИС.1).
COLLECT_STATUSES = ("active", "provisional", "candidate")


def _select_accounts(con, tier, limit=None, min_interval_hours=None, now=None):
    q = ("SELECT * FROM accounts WHERE tier=?"
         " AND status IN ('active','provisional','candidate')"
         " ORDER BY (last_success_at IS NULL) DESC, last_success_at ASC")
    rows = list(con.execute(q, (tier.upper(),)))
    if min_interval_hours:
        now = now or datetime.now(timezone.utc)
        keep = []
        for r in rows:
            last = db.parse_iso(r["last_success_at"])
            if last is None or (now - last) >= timedelta(hours=min_interval_hours):
                keep.append(r)
        rows = keep
    if limit:
        rows = rows[:limit]
    return rows


def _json(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def store_posts(con, account_id, posts, *, dry_run=False, text_src="nitter"):
    """Р5.3: INSERT OR IGNORE по UNIQUE(tweet_id). Счётчики честные.

    Посты с неразобранной датой не пишутся: published_at_utc NOT NULL и не может
    быть пустым/1970/будущим (инвариант Р2). Такие посты считаются в skipped.

    `text_src` (ТЗ-5 задача 1): «откуда текст». Для штатного сбора Nitter RSS —
    'nitter'; для аварийного дублёра x_ssr и разовой ленты synd — None (это не
    Nitter RSS, и текст там может быть пустым). При обновлении существующей
    строки пустой/None-текст НЕ затирает уже сохранённый полный текст, а
    `text_src` не понижается (COALESCE).
    """
    new = upd = skipped = 0
    for p in posts:
        tid = str(p.get("tweet_id") or "").strip()
        if not tid:
            skipped += 1
            continue
        published = p.get("published_at_utc")
        if not published:
            skipped += 1
            continue
        dup = 1 if p.get("published_src") == "dup" else 0
        if p.get("text_hash"):
            text_hash = p["text_hash"]
        else:
            text_hash = registry.text_hash(p.get("text"))
        if dry_run:
            exists = con.execute("SELECT 1 FROM posts WHERE tweet_id=?", (tid,)).fetchone()
            new += 0 if exists else 1
            upd += 1 if exists else 0
            continue
        exists = con.execute("SELECT id FROM posts WHERE tweet_id=?", (tid,)).fetchone()
        if exists:
            con.execute(
                """UPDATE posts SET published_at_utc=?, published_src=?,
                     text=COALESCE(?, text), text_hash=COALESCE(?, text_hash),
                     text_src=COALESCE(?, text_src),
                     links=?, mentions=?, hashtags=?, is_retweet=?,
                     is_quote=?, is_reply=?, owner_handle=?, orig_handle=?, media_kind=?
                   WHERE tweet_id=?""",
                (published, p.get("published_src") or "unknown", p.get("text"), text_hash,
                 text_src,
                 _json(p.get("links")), _json(p.get("mentions")), _json(p.get("hashtags")),
                 1 if p.get("is_retweet") else 0, 1 if p.get("is_quote") else 0,
                 1 if p.get("is_reply") else 0, p.get("owner_handle"),
                 p.get("orig_handle"), p.get("media_kind"), tid))
            upd += 1
        else:
            before = con.total_changes
            con.execute(
                """INSERT OR IGNORE INTO posts
                     (account_id, tweet_id, published_at_utc, published_src, text,
                      text_hash, text_src, links, mentions, hashtags, is_retweet, is_quote,
                      is_reply, owner_handle, orig_handle, media_kind)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, tid, published, p.get("published_src") or "unknown",
                 p.get("text"), text_hash, text_src, _json(p.get("links")),
                 _json(p.get("mentions")),
                 _json(p.get("hashtags")), 1 if p.get("is_retweet") else 0,
                 1 if p.get("is_quote") else 0, 1 if p.get("is_reply") else 0,
                 p.get("owner_handle"), p.get("orig_handle"), p.get("media_kind")))
            # rowcount у представления всегда 0 — считаем фактические вставки
            # через total_changes (TECH-DEBT D-23). Ноль изменений = сработал
            # INSERT OR IGNORE (пост уже есть); любое > 0 — строка новая.
            if db.changes_since(con, before) > 0:
                new += 1
            else:
                upd += 1
    if not dry_run:
        # не держим открытую транзакцию: брокеру тоже надо писать в БД
        con.commit()
    return {"new": new, "upd": upd, "skipped": skipped}


def _max_tweet_id(con, account_id):
    row = con.execute(
        "SELECT MAX(CAST(tweet_id AS INTEGER)) AS m FROM posts WHERE account_id=?",
        (account_id,)).fetchone()
    return row["m"]


def _refresh_account(con, account_id, posts):
    """Р5.4: обновить состояние аккаунта и пересчитать признаки."""
    rows = con.execute(
        "SELECT published_at_utc, text, text_hash, links, is_retweet FROM posts"
        " WHERE account_id=? ORDER BY published_at_utc DESC LIMIT ?",
        (account_id, config.RECENT_POSTS_WINDOW)).fetchall()
    feats = registry.compute_features(rows)
    ai_density = registry.compute_ai_density(rows)
    total = con.execute("SELECT COUNT(*) FROM posts WHERE account_id=?",
                        (account_id,)).fetchone()[0]
    cursor = None
    for p in posts:
        if p.get("cursor_next"):
            cursor = p["cursor_next"]
            break
    con.execute(
        """UPDATE accounts SET last_success_at=?, last_attempt_at=?, fail_streak=0,
             last_error=NULL, cursor=COALESCE(?, cursor), posts_collected=?,
             posts_per_day=?, cv_interval=?, link_ratio=?, rt_ratio=?, dup_ratio=?,
             ai_density=?,
             ai_density_src=CASE WHEN ai_density_src='classifier' THEN ai_density_src
                                 ELSE ? END
           WHERE id=?""",
        (db.utcnow_iso(), db.utcnow_iso(), cursor, total, feats["posts_per_day"],
         feats["cv_interval"], feats["link_ratio"], feats["rt_ratio"],
         feats["dup_ratio"], ai_density, config.AI_DENSITY_SRC_HEURISTIC, account_id))
    con.execute(
        """INSERT INTO cursors (kind, ref, cursor, last_page_at, items_total)
           VALUES ('account', (SELECT handle FROM accounts WHERE id=?), ?, ?, ?)""",
        (account_id, cursor, db.utcnow_iso(), len(posts)))
    con.commit()
    return feats


def _fail_account(con, account, error, run_id=None, dry_run=False):
    """Р5.5: один отказ не прерывает обход остальных."""
    streak = (account["fail_streak"] or 0) + 1
    status = account["status"]
    if streak >= 5 and status != "dead":
        status = "dead"
        if not dry_run:
            db.log_run(con, "WARN", f"5 отказов подряд ({error}), аккаунт помечен dead",
                       handle=account["handle"], run_id=run_id)
    if not dry_run:
        con.execute(
            "UPDATE accounts SET fail_streak=?, last_error=?, last_attempt_at=?, status=?"
            " WHERE id=?",
            (streak, str(error)[:400], db.utcnow_iso(), status, account["id"]))
        con.commit()
    return streak, status


def _walk_account(con, broker, account, *, tier, max_pages, dry_run=False):
    """Один проход по аккаунту: страница ленты + добор курсором при отставании."""
    handle = account["handle"]
    pages = []
    posts = broker.fetch_feed(handle, priority="collect")
    pages.append(posts)
    base = posts or []
    if not base:
        # ТЗ-10 2.2/П4: пустая лента — НЕ отказ Nitter (счётчик не растёт,
        # резерв не включается), но и не успех конкретного аккаунта.
        raise EmptyFeedError(f"лента пуста (0 item): {handle}")

    known_max = _max_tweet_id(con, account["id"]) if not dry_run else None
    cursor = base[0].get("cursor_next")
    fetched_pages = 1
    # Р5.2: если новейший известный пост отстаёт более чем на страницу — добор
    if (known_max and cursor and fetched_pages < max_pages
            and min(int(p["tweet_id"]) for p in base if str(p["tweet_id"]).isdigit()) > known_max):
        while cursor and fetched_pages < max_pages:
            nxt = broker.fetch_feed(handle, cursor=cursor, priority="collect")
            if not nxt:
                break
            pages.append(nxt)
            fetched_pages += 1
            if any(str(p["tweet_id"]).isdigit() and int(p["tweet_id"]) <= known_max for p in nxt):
                break
            cursor = nxt[0].get("cursor_next")
    merged = [p for page in pages for p in page]
    return merged, fetched_pages


def _try_ssr(router, handle):
    """Аварийный дублёр `x_ssr` — только при деградации Nitter (ТЗ-4 2.1).

    Возвращает список постов или None. Не бросает: отказ дублёра не должен
    менять логику обхода.
    """
    if router is None:
        return None
    try:
        if not router.nitter_degraded():
            return None
        return router.fetch_ssr(handle) or None
    except Exception:
        return None


def _fetch_ssr(router, handle):
    """Обращение к резерву без повторной проверки деградации.

    Нужно, когда решение о резерве уже принято по конкретному отказу
    (`_reserve_allowed`): повторная проверка `nitter_degraded()` внутри одного
    прогона избыточна. Не бросает.
    """
    if router is None:
        return None
    try:
        return router.fetch_ssr(handle) or None
    except Exception:
        return None


def _reserve_allowed(router, error, *, reserve_used, max_per_run):
    """ТЗ-10 2.2: можно ли собрать ЭТОТ аккаунт через резерв в этом прогоне.

    Условия: (1) именно фактический отказ Nitter — транспортный отказ или
    «нет живых инстансов»; 404 и пустая лента резерв не включают (П4);
    (2) для транспортного отказа — инстансы признаны деградировавшими;
    (3) не исчерпан потолок резерва на прогон (`XSSR_MAX_PER_RUN`).
    Nitter всегда остаётся первым: резерв — только после отказа по аккаунту.
    """
    if router is None:
        return False
    if reserve_used >= max_per_run:
        return False
    if isinstance(error, NoLiveInstance):
        # Инстансов нет вообще (cooldown/блок/потолок) — это уже деградация.
        return True
    if isinstance(error, NitterTransportError):
        try:
            return bool(router.nitter_degraded())
        except Exception:
            return False
    return False


def collect_tier(con, broker, tier, *, max_accounts=None, dry_run=False,
                 min_interval_hours=None, run_id=None, mode=None, router=None):
    """Р5: обход одного тира. Возвращает сводку прогона.

    `router` (ТЗ-4, `channels.ChannelRouter`) включает аварийный дублёр: если
    Nitter отказал по конкретному аккаунту (после штатных повторных попыток), а
    инстансы признаны деградировавшими, аккаунт добирается через `x_ssr` в ТОМ
    ЖЕ прогоне (ТЗ-10 2.2). Nitter при этом всегда остаётся первым.

    Сводка содержит ключи наблюдаемости (ТЗ-10 2.3):
      reserve_used   — сколько аккаунтов собрано через резерв (`ssr_used` — алиас);
      nitter_fails   — сколько фактических отказов Nitter случилось за прогон;
      accounts_lost  — сколько аккаунтов потеряно (цель: 0);
      reserve_reason — причина переключения на резерв.
    """
    tier = tier.upper()
    if tier not in config.TIERS:
        raise ValueError(f"неизвестный тир: {tier}")
    max_pages = config.TIERS[tier].get("max_pages", config.BACKFILL_MAX_PAGES)
    accounts = _select_accounts(con, tier, max_accounts, min_interval_hours)
    max_reserve = int(getattr(config, "XSSR_MAX_PER_RUN", 50))
    summary = {"tier": tier, "accounts_total": len(accounts), "accounts_ok": 0,
               "accounts_fail": 0, "posts_new": 0, "posts_upd": 0, "errors": 0,
               "skipped_no_date": 0, "pages": 0, "dry_run": dry_run, "ssr_used": 0,
               "reserve_used": 0, "nitter_fails": 0, "accounts_lost": 0,
               "reserve_cap": max_reserve, "reserve_capped": False,
               "reserve_reason": "не требовалось",
               "details": [], "verify_completeness": None}
    if not accounts:
        return summary

    if not dry_run:
        mode = mode or f"collect:{tier}"
        run_id = run_id or db.start_run(con, mode)

    fails_before = broker.collect_fail_events() if hasattr(
        broker, "collect_fail_events") else 0
    reserve_used = 0

    for account in accounts:
        handle = account["handle"]
        try:
            posts, pages = _walk_account(con, broker, account, tier=tier,
                                         max_pages=max_pages, dry_run=dry_run)
        except EmptyFeedError as e:
            # П4: пустая лента — не отказ Nitter: ни резерв, ни счётчик отказов.
            summary["accounts_fail"] += 1
            summary["errors"] += 1
            streak, status = _fail_account(con, account, e, run_id=run_id,
                                           dry_run=dry_run)
            summary["details"].append({"handle": handle, "ok": False, "error": str(e),
                                       "fail_streak": streak, "status": status,
                                       "nitter_failure": False})
            continue
        except (NitterError, NoLiveInstance) as e:
            eligible = (not dry_run) and _reserve_allowed(
                router, e, reserve_used=reserve_used, max_per_run=max_reserve)
            if (not dry_run and not eligible and router is not None
                    and reserve_used >= max_reserve
                    and isinstance(e, (NoLiveInstance, NitterTransportError))):
                summary["reserve_capped"] = True
            posts = _fetch_ssr(router, handle) if eligible else None
            if posts:
                res = store_posts(con, account["id"], posts, text_src=None)
                summary["posts_new"] += res["new"]
                summary["posts_upd"] += res["upd"]
                summary["skipped_no_date"] += res["skipped"]
                reserve_used += 1
                summary["reserve_used"] = reserve_used
                summary["ssr_used"] = reserve_used
                _refresh_account(con, account["id"], posts)
                summary["accounts_ok"] += 1
                summary["details"].append({"handle": handle, "ok": True,
                                           "items": len(posts), "pages": 0,
                                           "new": res["new"], "upd": res["upd"],
                                           "skipped_no_date": res["skipped"],
                                           "fallback": "x_ssr",
                                           "nitter_error": str(e)})
                if not dry_run:
                    db.log_run(con, "WARN",
                               f"Nitter отказал ({e}); аккаунт собран через резерв"
                               f" x_ssr (резерв за прогон: {reserve_used}/{max_reserve})",
                               handle=handle, run_id=run_id)
                    con.commit()
                continue
            summary["accounts_fail"] += 1
            summary["errors"] += 1
            streak, status = _fail_account(con, account, e, run_id=run_id, dry_run=dry_run)
            if not dry_run:
                db.log_run(con, "WARN", f"обход не удался ({e}); fail_streak={streak}",
                           handle=handle, run_id=run_id)
                con.commit()
            summary["details"].append({"handle": handle, "ok": False, "error": str(e),
                                       "fail_streak": streak, "status": status,
                                       "nitter_failure": True,
                                       "reserve_tried": bool(eligible)})
            continue
        except Exception as e:  # никакой отказ не должен ронять обход
            summary["accounts_fail"] += 1
            summary["errors"] += 1
            streak, status = _fail_account(con, account, e, run_id=run_id, dry_run=dry_run)
            summary["details"].append({"handle": handle, "ok": False, "error": repr(e),
                                       "fail_streak": streak, "status": status})
            continue


        res = store_posts(con, account["id"], posts, dry_run=dry_run)
        summary["posts_new"] += res["new"]
        summary["posts_upd"] += res["upd"]
        summary["skipped_no_date"] += res["skipped"]
        summary["pages"] += pages
        if not dry_run:
            _refresh_account(con, account["id"], posts)
        summary["accounts_ok"] += 1
        summary["details"].append({"handle": handle, "ok": True, "items": len(posts),
                                   "pages": pages, "new": res["new"], "upd": res["upd"],
                                   "skipped_no_date": res["skipped"]})

    fails_after = broker.collect_fail_events() if hasattr(
        broker, "collect_fail_events") else 0
    summary["nitter_fails"] = max(0, fails_after - fails_before)
    summary["accounts_lost"] = summary["accounts_fail"]
    summary["reserve_used"] = reserve_used
    summary["ssr_used"] = reserve_used
    parts = []
    if reserve_used:
        thr = int(getattr(config, "XSSR_DEGRADED_STREAK", 3))
        parts.append("Nitter деградировал: суммарные фактические отказы сбора"
                     f" инстансов >= {thr} (инстансов в пуле:"
                     f" {len(getattr(broker, 'instances', []) or [])})")
    if summary["reserve_capped"]:
        parts.append(f"достигнут потолок резерва XSSR_MAX_PER_RUN={max_reserve}")
    summary["reserve_reason"] = "; ".join(parts) if parts else "не требовалось"

    if not dry_run:
        db.finish_run(con, run_id, accounts_ok=summary["accounts_ok"],
                      accounts_fail=summary["accounts_fail"],
                      posts_new=summary["posts_new"], posts_upd=summary["posts_upd"],
                      errors=summary["errors"])
        update_metrics_daily(con)
    summary["run_id"] = run_id
    return summary

def verify_completeness(con, broker, summary, tier):
    """Р6.1: повторный обход тех же аккаунтов, норма — 0 новых id."""
    handles = [d["handle"] for d in summary["details"] if d.get("ok")]
    new_ids = 0
    checked = 0
    skipped_no_date = 0
    for h in handles:
        row = con.execute("SELECT * FROM accounts WHERE handle=?", (h,)).fetchone()
        if row is None:
            continue
        try:
            posts = broker.fetch_feed(h, priority="collect", force=True)
        except NitterError:
            continue
        checked += 1
        for p in posts:
            tid = str(p.get("tweet_id") or "")
            if not tid:
                continue
            # пост без восстановимой даты не хранится по инварианту Р2 —
            # это не потеря покрытия, а отбраковка по дате
            if not p.get("published_at_utc"):
                skipped_no_date += 1
                continue
            if con.execute("SELECT 1 FROM posts WHERE tweet_id=?", (tid,)).fetchone() is None:
                new_ids += 1
    return {"accounts_checked": checked, "new_ids": new_ids,
            "skipped_no_date": skipped_no_date, "ok": new_ids == 0}


def update_metrics_daily(con, day=None):
    """Р5.6: строка в metrics_daily по итогам прогонов за сутки."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = con.execute(
        """SELECT COALESCE(SUM(accounts_ok),0) ok, COALESCE(SUM(accounts_fail),0) fail,
                  COALESCE(SUM(posts_new),0) new, COALESCE(SUM(posts_upd),0) upd
           FROM runs WHERE started_at LIKE ?""", (day + "%",)).fetchone()
    ok, fail, new, upd = r["ok"], r["fail"], r["new"], r["upd"]
    total = ok + fail
    posts_new_d = con.execute(
        "SELECT COUNT(*) FROM posts WHERE first_seen_at LIKE ?", (day + "%",)).fetchone()[0]
    bad_date = con.execute(
        "SELECT COUNT(*) FROM posts WHERE first_seen_at LIKE ? AND"
        " (published_at_utc IS NULL OR published_at_utc='' OR published_at_utc LIKE '1970%')",
        (day + "%",)).fetchone()[0]
    valid_ratio = 1.0 if posts_new_d == 0 else round(1.0 - bad_date / posts_new_d, 6)
    lat = [row[0] for row in con.execute(
        "SELECT latency_ms FROM requests WHERE ts LIKE ? AND latency_ms IS NOT NULL",
        (day + "%",)).fetchall()]
    p95_min = round(statistics.quantiles(lat, n=20)[18] / 60000.0, 4) if len(lat) >= 20 else (
        round(max(lat) / 60000.0, 4) if lat else None)
    alive = con.execute("SELECT COUNT(*) FROM instances WHERE healthy=1").fetchone()[0]
    dup_rate = round(upd / (new + upd), 6) if (new + upd) else None
    coverage = round(ok / total, 6) if total else None
    fail_rate = round(fail / total, 6) if total else None
    # --- ТЗ-4 3.3: метрики вовлечённости и бюджеты каналов
    likes_vals = [r[0] for r in con.execute(
        "SELECT likes FROM posts WHERE metrics_at LIKE ? AND likes IS NOT NULL",
        (day + "%",)).fetchall()]
    likes_median = round(statistics.median(likes_vals), 4) if likes_vals else None
    total_posts = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    enriched = con.execute(
        "SELECT COUNT(*) FROM posts WHERE metrics_at IS NOT NULL").fetchone()[0]
    enriched_ratio = round(enriched / total_posts, 6) if total_posts else None
    def _cnt(sql, params):
        return con.execute(sql, params).fetchone()[0]
    cdn_429 = _cnt("SELECT COUNT(*) FROM requests WHERE kind='cdn_tweet' AND status=429"
                   " AND ts LIKE ?", (day + "%",))
    synd_429 = _cnt("SELECT COUNT(*) FROM requests WHERE kind='synd_timeline'"
                    " AND status=429 AND ts LIKE ?", (day + "%",))
    ssr_used = _cnt("SELECT COUNT(*) FROM requests WHERE kind='x_ssr' AND ts LIKE ?",
                    (day + "%",))
    lags = []
    now_utc = datetime.now(timezone.utc)
    for row in con.execute("SELECT MAX(published_at_utc) m FROM posts"
                           " GROUP BY account_id").fetchall():
        dt = db.parse_iso(row["m"])
        if dt:
            lags.append((now_utc - dt).total_seconds() / 60.0)
    stale_lag = None
    if lags:
        stale_lag = round(statistics.quantiles(lags, n=20)[18], 4) if len(lags) >= 20 \
            else round(max(lags), 4)
    con.execute(
        """INSERT INTO metrics_daily (day, posts_ingested, dup_rate, coverage, fail_rate,
             latency_p95_min, valid_date_ratio, instances_alive,
             likes_median, enriched_ratio, cdn_429_count, synd_429_count, ssr_used,
             stale_lag_p95_min)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (day, posts_new_d, dup_rate, coverage, fail_rate, p95_min, valid_ratio, alive,
         likes_median, enriched_ratio, cdn_429, synd_429, ssr_used, stale_lag))
    con.commit()
    return {"day": day, "accounts_ok": ok, "accounts_fail": fail, "posts_new": new,
            "posts_upd": upd, "coverage": coverage, "fail_rate": fail_rate,
            "dup_rate": dup_rate, "latency_p95_min": p95_min,
            "valid_date_ratio": valid_ratio, "instances_alive": alive,
            "likes_median": likes_median, "enriched_ratio": enriched_ratio,
            "cdn_429_count": cdn_429, "synd_429_count": synd_429, "ssr_used": ssr_used,
            "stale_lag_p95_min": stale_lag}


def backfill(con, broker, handle, pages=3, *, run_id=None):
    """Р1/П6: бэкфилл по курсору на N страниц вглубь, без дублей."""
    from .registry import validate_handle
    h, err = validate_handle(handle)
    if err:
        raise ValueError(f"bad_handle: {handle!r}")
    row = con.execute("SELECT * FROM accounts WHERE handle=?", (h,)).fetchone()
    if row is None:
        raise ValueError(f"аккаунт не в реестре: {h}")
    run_id = run_id or db.start_run(con, f"backfill:{h}")
    cursor = row["cursor"]
    seen = set()
    total_new = total_upd = total_skipped = 0
    pages_done = 0
    for i in range(int(pages)):
        posts = broker.fetch_feed(h, cursor=cursor, priority="backfill", force=True)
        if not posts:
            break
        pages_done += 1
        fresh = [p for p in posts if p["tweet_id"] not in seen]
        seen.update(p["tweet_id"] for p in posts)
        res = store_posts(con, row["id"], fresh)
        total_new += res["new"]
        total_upd += res["upd"]
        total_skipped += res["skipped"]
        cursor = posts[0].get("cursor_next") or cursor
        print(f"  страница {i + 1}: items={len(posts)} новых={res['new']}"
              f" уже_было={res['upd']} без_даты={res['skipped']}")
        if not cursor:
            break
    _refresh_account(con, row["id"], [{"cursor_next": cursor}])
    uniq = con.execute("SELECT COUNT(*) FROM posts WHERE account_id=?",
                       (row["id"],)).fetchone()[0]
    db.finish_run(con, run_id, accounts_ok=1, posts_new=total_new, posts_upd=total_upd)
    update_metrics_daily(con)
    return {"handle": h, "pages": pages_done, "new": total_new, "upd": total_upd,
            "skipped_no_date": total_skipped, "unique_posts_total": uniq,
            "cursor": cursor, "run_id": run_id}


def refetch_fulltext(con, broker, *, router=None, limit=None, run_id=None,
                     dry_run=False):
    """ТЗ-5 задача 1: очередь добыра полного текста.

    В очередь попадают посты, которые считаются неполными: `is_long=1` и текст
    не длиннее `FULLTEXT_CDN_LIMIT` (замер CDN — 279–280 символов), либо
    `text_src='cdn'`. Для каждого такого аккаунта перечитывается лента Nitter
    RSS (`force=True`, курсор/пагинация уже есть) — текст перезаписывается
    полным; при недоступности Nitter используется аварийный дублёр `x_ssr`
    (если передан `router`).

    Возвращает честные числа до/после и сколько пришло резервным путём.
    """
    from . import enrich as _enrich
    before = _enrich.needs_full_text_count(con)
    targets = _enrich.needs_full_text_posts(con, limit=limit)
    summary = {"before": before, "targets": len(targets), "accounts": 0,
               "nitter_ok": 0, "ssr_used": 0, "failed": 0, "after": before,
               "dry_run": dry_run, "details": []}
    if not targets or dry_run:
        summary["after"] = before
        return summary
    acc_ids = []
    for t in targets:
        if t["account_id"] not in acc_ids:
            acc_ids.append(t["account_id"])
    for aid in acc_ids:
        row = con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        if row is None:
            continue
        handle = row["handle"]
        summary["accounts"] += 1
        posts = None
        try:
            posts = broker.fetch_feed(handle, priority="collect", force=True)
            if posts:
                res = store_posts(con, aid, posts, text_src="nitter")
                summary["nitter_ok"] += 1
                _refresh_account(con, aid, posts)
                summary["details"].append({"handle": handle, "source": "nitter",
                                           "items": len(posts), "new": res["new"],
                                           "upd": res["upd"]})
                continue
        except (NitterError, NoLiveInstance):
            pass
        except Exception as e:  # отказ не должен ронять очередь
            summary["failed"] += 1
            summary["details"].append({"handle": handle, "error": repr(e)})
            continue
        ssr = _try_ssr(router, handle)
        if ssr:
            res = store_posts(con, aid, ssr, text_src=None)
            summary["ssr_used"] += 1
            _refresh_account(con, aid, ssr)
            summary["details"].append({"handle": handle, "source": "x_ssr",
                                       "items": len(ssr), "new": res["new"],
                                       "upd": res["upd"]})
        else:
            summary["failed"] += 1
            summary["details"].append({"handle": handle, "error": "nitter недоступен,"
                                                                " x_ssr недоступен"})
    summary["after"] = _enrich.needs_full_text_count(con)
    if run_id is not None:
        db.log_run(con, "INFO",
                   f"fulltext: нужно={before} обработано_аккаунтов={summary['accounts']}"
                   f" осталось={summary['after']} x_ssr={summary['ssr_used']}",
                   run_id=run_id)
        con.commit()
    return summary
