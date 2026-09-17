"""Скоринг значимости на реальных метриках вовлечённости (ТЗ-4 Р4).

Три оси:
  * `score_engage` — скорость набора лайков, нормированная на возраст;
  * `score_spread` — распространение по ГРАФУ упоминаний реестра (не по репостам,
    которых в CDN нет вообще);
  * `score_first`  — насколько рано тему подхватил второй независимый автор.

Итог: `score = 0,45*engage + 0,35*spread + 0,20*first` (веса стартовые,
калибруемая часть, зафиксировано в METHODOLOGY.md).

Чего у нас НЕТ и что нельзя выдумывать: репосты, число цитат, закладки,
просмотры. CDN их не отдаёт, а ленточный канал, где есть `retweet_count`, —
разовый. Поэтому массовый скоринг репостов не использует, и это честно.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta, timezone

from . import config, store as db

EXCLUDE_SQL = ("COALESCE(p.is_retweet,0)=0 AND COALESCE(p.pinned,0)=0"
               " AND COALESCE(p.metrics_src,'') != 'cdn_rt'")


# ---------------------------------------------------------------- velocity
def _measurements(con, tweet_id):
    rows = con.execute(
        "SELECT age_hours, likes, replies FROM post_metrics_history"
        " WHERE tweet_id=? AND age_hours IS NOT NULL ORDER BY age_hours",
        (str(tweet_id),)).fetchall()
    return [(float(r["age_hours"]), r["likes"] or 0, r["replies"] or 0) for r in rows]


def velocity_6h(con, tweet_id, published_at_utc=None, target=None):
    """Скорость набора на фиксированном возрасте (ТЗ-4 Р4).

    Возвращает (velocity, likes_at_6h, replies_at_6h, estimated).
    Замер в окне ±1,5 ч от 6 ч — точный. Иначе линейная переоценка по
    ближайшему замеру (помечается estimated=True) — иначе посты моложе 6 ч
    вообще нельзя сравнивать.
    """
    target = float(target if target is not None else config.VELOCITY_TARGET_HOURS)
    ms = _measurements(con, tweet_id)
    if not ms and published_at_utc is not None:
        row = con.execute("SELECT likes, replies FROM posts WHERE tweet_id=?",
                          (str(tweet_id),)).fetchone()
        if row is not None and row["likes"] is not None:
            age = age_hours_from_iso(published_at_utc)
            if age is not None and age > 0:
                ms = [(age, row["likes"], row["replies"] or 0)]
    if not ms:
        return 0.0, 0, 0, True
    best = min(ms, key=lambda m: abs(m[0] - target))
    age, likes, replies = best
    if abs(age - target) <= 1.5:
        return likes / target, likes, replies, False
    if age <= 0:
        return 0.0, 0, 0, True
    factor = target / age
    return likes * factor / target, int(round(likes * factor)), int(round(replies * factor)), True


def age_hours_from_iso(published_at_utc):
    p = db.parse_iso(published_at_utc)
    if not p:
        return None
    return (db.utcnow() - p).total_seconds() / 3600.0


def score_engage(velocity, replies_at_6h):
    return round(math.log10(1.0 + velocity * 10.0 + (replies_at_6h or 0) * 2.0), 6)


# ------------------------------------------------------------------ граф
def _json_list(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return value if isinstance(value, list) else [value]


def verified_authors(con):
    """Хендлы аккаунтов с зафиксированной галочкой автора (слабый признак)."""
    rows = con.execute(
        "SELECT DISTINCT COALESCE(author_handle, owner_handle) AS h FROM posts"
        " WHERE author_verified=1"
        " AND COALESCE(author_handle, owner_handle) IS NOT NULL").fetchall()
    return {r["h"].lower() for r in rows}


def spread_authors(con, post):
    """Независимые аккаунты реестра, подхватившие пост (ТЗ-4 Р4).

    Сигналы: упоминание автора (`mentions`), цитата/репост (`orig_handle`) и
    прямая ссылка на пост (`links`). Каждый такой автор — с весом 1,5, плюс
    +10% за галочку верификации (никогда не фильтр).
    """
    owner = (db.post_author(post) or "").lower()
    pub = db.parse_iso(post["published_at_utc"])
    tid = str(post["tweet_id"])
    if not owner or pub is None:
        return set(), []
    lo = db.iso(pub)
    hi = db.iso(pub + timedelta(hours=config.FIRST_MOVER_WINDOW_HOURS))
    rows = con.execute(
        "SELECT p.owner_handle, p.author_handle, p.mentions, p.links,"
        " p.orig_handle, p.tweet_id, a.handle AS acc_handle"
        " FROM posts p LEFT JOIN accounts a ON a.id=p.account_id"
        " WHERE p.published_at_utc >= ? AND p.published_at_utc <= ?"
        " AND p.tweet_id != ? AND COALESCE(p.is_retweet,0)=0"
        " AND COALESCE(p.pinned,0)=0",
        (lo, hi, tid)).fetchall()
    verified = verified_authors(con)
    found = {}
    for r in rows:
        handle = (db.post_author(r) or "").lower()
        if not handle or handle == owner:
            continue
        mentions = [str(m).lower() for m in _json_list(r["mentions"])]
        links = [str(l) for l in _json_list(r["links"])]
        orig = (r["orig_handle"] or "").lower()
        hit = (owner in mentions or orig == owner
               or any(tid in l for l in links))
        if not hit:
            continue
        weight = config.SPREAD_AUTHOR_WEIGHT
        if handle in verified:
            weight *= (1.0 + config.SPREAD_VERIFIED_BONUS)
        weight = round(weight, 6)
        found[handle] = max(found.get(handle, 0.0), weight)
    return set(found), sorted(found.items())


def score_spread(author_weights):
    """Распространение = сумма весов независимых авторов (1,5 за каждого)."""
    return round(sum(w for _h, w in author_weights), 6)


def mark_spread_src(con, tweet_id, src="graph"):
    con.execute("UPDATE posts SET spread_src=? WHERE tweet_id=?", (src, str(tweet_id)))
    con.commit()
    return src


# -------------------------------------------------------------- первоисточник
def lead_time_min(con, post):
    """Минуты до второго независимого автора темы (ТЗ-4 Р4).

    Без ТЗ-3 (сюжеты) тема приближается пересечением сущностей: общая ссылка
    или общее упоминание. Это приближение, оно помечено в METHODOLOGY.md.
    """
    owner = (db.post_author(post) or "").lower()
    pub = db.parse_iso(post["published_at_utc"])
    if not owner or pub is None:
        return None
    my_links = {l for l in _json_list(post["links"])}
    my_mentions = {str(m).lower() for m in _json_list(post["mentions"])}
    if not my_links and not my_mentions:
        return None
    hi = db.iso(pub + timedelta(hours=config.FIRST_MOVER_WINDOW_HOURS))
    rows = con.execute(
        "SELECT p.owner_handle, p.author_handle, p.published_at_utc, p.links,"
        " p.mentions, a.handle AS acc_handle"
        " FROM posts p LEFT JOIN accounts a ON a.id=p.account_id"
        " WHERE p.published_at_utc > ? AND p.published_at_utc <= ?"
        " AND p.tweet_id != ? AND COALESCE(p.is_retweet,0)=0"
        " AND COALESCE(p.pinned,0)=0",
        (db.iso(pub), hi, str(post["tweet_id"]))).fetchall()
    best = None
    for r in rows:
        handle = (db.post_author(r) or "").lower()
        if not handle or handle == owner:
            continue
        links = {l for l in _json_list(r["links"])}
        mentions = {str(m).lower() for m in _json_list(r["mentions"])}
        if not (links & my_links or mentions & my_mentions):
            continue
        t = db.parse_iso(r["published_at_utc"])
        if t is None:
            continue
        delta = (t - pub).total_seconds() / 60.0
        if delta >= 0 and (best is None or delta < best):
            best = delta
    return None if best is None else round(best, 3)


def score_first(lead_minutes):
    """Чем раньше подхватили — тем выше. Монотонно убывает, 0 при отсутствии."""
    if lead_minutes is None:
        return 0.0
    return round(1.0 / (1.0 + lead_minutes / 60.0), 6)


# ------------------------------------------------------------------ итог
def score_post(con, post):
    vel, likes6, replies6, estimated = velocity_6h(
        con, post["tweet_id"], post["published_at_utc"])
    engage = score_engage(vel, replies6)
    spread_src = "graph"
    rt = post["retweet_count"] if "retweet_count" in post.keys() else None
    if post["spread_src"] == "synd" and rt is not None:
        # Уточнение из разового ленточного канала: помечено spread_src='synd',
        # в один рейтинг с графиком без пометки не смешивается (ТЗ-4 Р4).
        spread = round(config.SPREAD_AUTHOR_WEIGHT * (rt or 0), 6)
        authors, weights = set(), []
        spread_src = "synd"
    else:
        authors, weights = spread_authors(con, post)
        spread = score_spread(weights)
    lead = lead_time_min(con, post)
    first = score_first(lead)
    total = (config.SCORE_W_ENGAGE * engage + config.SCORE_W_SPREAD * spread
             + config.SCORE_W_FIRST * first)
    return {
        "tweet_id": str(post["tweet_id"]),
        "handle": db.post_author(post),
        "published_at_utc": post["published_at_utc"],
        "likes": post["likes"], "replies": post["replies"],
        "velocity_6h": round(vel, 6), "likes_at_6h": likes6,
        "replies_at_6h": replies6, "velocity_estimated": estimated,
        "score_engage": engage, "score_spread": spread,
        "spread_authors": sorted(authors), "score_first": first,
        "lead_time_min": lead, "spread_src": spread_src,
        "score": round(total, 6),
    }


def excluded(post):
    """Ретвиты, закреплённые и cdn_rt в агрегаты не подаются (ТЗ-4 Р4)."""
    return bool(post["is_retweet"] or post["pinned"]
                or (post["metrics_src"] or "") == "cdn_rt")


def rank(con, limit=50, since_hours=None):
    """Рейтинг значимости. Исключения — SQL-фильтром EXCLUDE_SQL."""
    q = ("SELECT p.* FROM posts p WHERE " + EXCLUDE_SQL +
         " AND p.deleted_at IS NULL AND p.metrics_at IS NOT NULL")
    params = []
    if since_hours:
        cutoff = db.iso(datetime.now(timezone.utc) - timedelta(hours=since_hours))
        q += " AND p.published_at_utc >= ?"
        params.append(cutoff)
    q += " ORDER BY p.published_at_utc DESC LIMIT ?"
    params.append(int(limit))
    rows = list(con.execute(q, params))
    out = [score_post(con, r) for r in rows]
    out.sort(key=lambda d: (-d["score"], d["tweet_id"]))
    return out


def coverage(con):
    """Доля постов с метриками (для П10/сторожа)."""
    total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    done = con.execute("SELECT COUNT(*) FROM posts WHERE metrics_at IS NOT NULL").fetchone()[0]
    return {"total": total, "enriched": done,
            "ratio": (done / total) if total else None,
            "likes_median": _likes_median(con)}


def _likes_median(con):
    vals = [r[0] for r in con.execute(
        "SELECT likes FROM posts WHERE likes IS NOT NULL AND metrics_at IS NOT NULL")]
    return statistics.median(vals) if vals else None
