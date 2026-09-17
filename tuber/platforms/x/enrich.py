"""Обогащение постов метриками через канал `cdn_tweet` (ТЗ-4 2.2).

Схема: выбрать посты без метрик (`metrics_at IS NULL`) пачкой, спросить CDN,
записать лайки/ответы/признаки/историю. Текст из CDN — ТОЛЬКО для заполнения
пустого места: в `tweet-result` он обрезан на ~280 символах, и перезапись
полного текста Nitter уничтожила бы данные (ТЗ-4 2.2 п.6).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config, store as db

TIER_ORDER = "CASE a.tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END"


def age_hours(published_at_utc, taken_at=None):
    """Возраст поста на момент замера, часы (для post_metrics_history)."""
    p = db.parse_iso(published_at_utc)
    t = db.parse_iso(taken_at) if taken_at else db.utcnow()
    if not p or not t:
        return None
    return round((t - p).total_seconds() / 3600.0, 4)


def select_pending(con, batch=None, backfill_days=None, now=None):
    """Посты без метрик. Свежие вперёд, затем приоритет тира A/B/C (2.2 п.1-2)."""
    batch = config.ENRICH_BATCH if batch is None else int(batch)
    params = []
    q = (f"SELECT p.*, a.tier AS acc_tier FROM posts p"
         f" JOIN accounts a ON a.id=p.account_id"
         f" WHERE p.metrics_at IS NULL AND p.deleted_at IS NULL")
    if backfill_days is not None:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=int(backfill_days))
        q += " AND p.published_at_utc >= ?"
        params.append(db.iso(cutoff))
    q += f" ORDER BY p.published_at_utc DESC, {TIER_ORDER} LIMIT ?"
    params.append(batch)
    return list(con.execute(q, params))


def pending_count(con):
    return con.execute(
        "SELECT COUNT(*) FROM posts WHERE metrics_at IS NULL AND deleted_at IS NULL"
    ).fetchone()[0]


# ------------------------------------------------- ТЗ-5 задача 1: провенанс текста
def _needs_full_text_where():
    """Условие «нужен полный текст» (ТЗ-5 задача 1).

    Пост неполон, если он помечен длинным (`is_long=1`), но текст не длиннее
    предела обрезки CDN (`FULLTEXT_CDN_LIMIT`, замер — 279–280 символов), ЛИБО
    текст взят из CDN (`text_src='cdn'`).
    """
    return ("deleted_at IS NULL AND ("
            " (is_long=1 AND COALESCE(length(text),0) <= ?)"
            " OR text_src='cdn')")


def needs_full_text_count(con):
    """Сколько постов сейчас в очереди добыра полного текста (честное число)."""
    return con.execute(
        f"SELECT COUNT(*) FROM posts WHERE {_needs_full_text_where()}",
        (int(config.FULLTEXT_CDN_LIMIT),)).fetchone()[0]


def needs_full_text_posts(con, limit=None):
    """Список постов, которым нужен полный текст (для очереди добыра)."""
    q = ("SELECT tweet_id, account_id, owner_handle, is_long, text_src,"
         " published_at_utc, COALESCE(length(text),0) AS text_len"
         f" FROM posts WHERE {_needs_full_text_where()}"
         " ORDER BY published_at_utc DESC")
    params = [int(config.FULLTEXT_CDN_LIMIT)]
    if limit:
        q += " LIMIT ?"
        params.append(int(limit))
    return list(con.execute(q, params))


def apply_metrics(con, tweet_id, fields, *, is_retweet=0, taken_at=None):
    """Записать метрики в posts (+ история). Возвращает что реально обновлено.

    Текст из CDN пишем только если в базе пусто или короче ENRICH_MIN_TEXT_LEN
    (ТЗ-4 2.2 п.6). `is_retweet=1` -> `metrics_src='cdn_rt'`: метрики ретвита
    принадлежат оригиналу и в агрегаты не подаются (питфолл 7.4).
    """
    taken = taken_at or db.utcnow_iso()
    tid = str(tweet_id)
    row = con.execute("SELECT * FROM posts WHERE tweet_id=?", (tid,)).fetchone()
    if row is None:
        return {"updated": 0, "reason": "no_row"}
    metrics_src = "cdn_rt" if is_retweet else "cdn"
    # Задача 1 ТЗ виральности: `owner_handle` — владелец ЛЕНТЫ (для фидов это
    # сам handle ленты, а не автор поста). CDN отдаёт реального автора в
    # `user.screen_name`. Пишем его в отдельную колонку `author_handle`, не
    # трогая owner_handle. NULL-ответ колонку не затирает (COALESCE).
    author_handle = (fields.get("screen_name") or "").lstrip("@") or None
    con.execute(
        """UPDATE posts SET likes=?, replies=?, has_quote=?, is_long=?,
             lang=COALESCE(NULLIF(lang,''), ?), metrics_at=?, metrics_src=?,
             author_verified=?, author_handle=COALESCE(?, author_handle)
           WHERE tweet_id=?""",
        (fields.get("likes"), fields.get("replies"), fields.get("has_quote"),
         fields.get("is_long"), fields.get("lang"), taken, metrics_src,
         fields.get("author_verified"), author_handle, tid))
    text = fields.get("text")
    text_written = 0
    if text:
        before = con.total_changes
        con.execute(
            "UPDATE posts SET text=?, text_hash=COALESCE(text_hash, ?), text_src='cdn'"
            " WHERE tweet_id=? AND (text IS NULL OR length(text) < ?)",
            (text, _text_hash(text), tid, config.ENRICH_MIN_TEXT_LEN))
        # rowcount у представления всегда 0 — считаем через total_changes (D-23).
        text_written = db.changes_since(con, before)
    con.execute(
        """INSERT OR IGNORE INTO post_metrics_history
             (tweet_id, taken_at, age_hours, likes, replies, src)
           VALUES (?,?,?,?,?,?)""",
        (tid, taken, age_hours(row["published_at_utc"], taken),
         fields.get("likes"), fields.get("replies"), metrics_src))
    con.commit()
    is_long = int(fields.get("is_long") or 0)
    new_len = len(text) if text_written else len(row["text"] or "")
    full_text_len = new_len
    needs_nitter = bool((is_long and new_len <= config.FULLTEXT_CDN_LIMIT)
                        or text_written)
    return {"updated": 1, "metrics_src": metrics_src, "text_written": text_written,
            "needs_nitter": needs_nitter, "full_text_len": full_text_len}


def _text_hash(text):
    from .registry import text_hash
    return text_hash(text)


def mark_deleted(con, tweet_id, taken_at=None):
    """404/Tombstone -> `deleted_at`, это не ошибка прогона (2.2)."""
    before = con.total_changes
    con.execute(
        "UPDATE posts SET deleted_at=? WHERE tweet_id=? AND deleted_at IS NULL",
        (taken_at or db.utcnow_iso(), str(tweet_id)))
    con.commit()
    # rowcount у представления всегда 0 — считаем через total_changes (D-23).
    return db.changes_since(con, before)


def enrich_batch(con, router, *, batch=None, backfill_days=None, dry_run=False,
                 run_id=None):
    """Обогатить пачку постов. Идемпотентно: повтор даёт 0 новых строк."""
    rows = select_pending(con, batch=batch, backfill_days=backfill_days)
    summary = {
        "selected": len(rows), "ok": 0, "errors": 0, "invalid": 0, "not_found": 0,
        "deleted": 0, "rate_limited": 0, "text_filled": 0, "text_preserved": 0,
        "needs_nitter": 0, "needs_nitter_batch": 0, "new_posts": 0, "dry_run": dry_run,
        "cdn_429": 0, "details": [],
    }
    if not rows or dry_run:
        # Честное число считается по всей базе, а не только по этой пачке:
        # ранее обогащённые посты с обрезанным текстом тоже должны попасть в сводку.
        summary["needs_nitter"] = needs_full_text_count(con)
        if dry_run and rows:
            for r in rows[:20]:
                summary["details"].append({"tweet_id": r["tweet_id"],
                                           "handle": _owner(r)})
        return summary

    for row in rows:
        tid = row["tweet_id"]
        try:
            kind, fields, status = router.enrich_tweet(tid)
        except Exception as e:  # канал не должен ронять прогон
            summary["errors"] += 1
            summary["details"].append({"tweet_id": tid, "kind": "exception",
                                       "error": repr(e)})
            continue
        if kind == "ok":
            res = apply_metrics(con, tid, fields, is_retweet=row["is_retweet"] or 0)
            summary["ok"] += 1
            if res.get("text_written"):
                summary["text_filled"] += 1
            elif fields.get("text"):
                summary["text_preserved"] += 1
            if res.get("needs_nitter"):
                summary["needs_nitter_batch"] += 1
                db.log_run(con, "WARN",
                           f"is_long=1, но полного текста нет: {tid} — в очередь на Nitter",
                           handle=_owner(row), run_id=run_id)
                con.commit()
        elif kind in ("not_found", "deleted"):
            mark_deleted(con, tid)
            summary["deleted" if kind == "deleted" else "not_found"] += 1
        elif kind == "invalid":
            summary["invalid"] += 1
        elif kind == "rate_limited":
            summary["rate_limited"] += 1
            break  # канал залип: не долбим остаток пачки
        else:
            summary["errors"] += 1
        summary["details"].append({"tweet_id": tid, "kind": kind, "status": status})
    summary["cdn_429"] = getattr(router.cdn, "requests_429", 0)
    # Честное число для сводки — по всей базе, включая посты, обогащённые ранее.
    summary["needs_nitter"] = needs_full_text_count(con)
    _refresh_daily(con)
    return summary


def _owner(row):
    try:
        return row["owner_handle"]
    except (KeyError, IndexError, TypeError):
        return None


def _refresh_daily(con):
    try:
        from .collect import update_metrics_daily
        update_metrics_daily(con)
    except Exception:
        pass
