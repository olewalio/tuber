"""Три оси и оценки значимости (ТЗ-3 Р3).

Единая функция считает значимость поста по двум веткам:

* **популярность** (метрики вовлечённости доступны и свежие):
  `engagement = likes + 2*replies` (снимок ~6 ч),
  `velocity = engagement/6`, `spread = xconf - 1`,
  `significance = (ln(1+velocity) + 0.8*ln(1+spread)) / (1 + hours/24)^1.8`;
* **графово-временная** (метрик нет или они старше 24 ч):
  `significance = (xconf-1)^0.8 / (hours+2)^1.8`, пост помечается
  `metrics_missing=1`, и выдача прямо говорит, что оценка временная.

Ретвиты никогда не оцениваются как самостоятельные посты: их метрики
принадлежат оригиналу, поэтому в рейтинг они не подаются (и учитываются как
признак распространения в сюжете).

Плюс накопительные оси: `first_mover_score` (полураспад 30 дней) и `darks`
(тёмные лошадки). Сеть здесь не используется.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from . import ai_filter, config, store as db, scoring


def _hours_between(a, b):
    ta, tb = db.parse_iso(a), db.parse_iso(b)
    if not ta or not tb:
        return None
    return (tb - ta).total_seconds() / 3600.0


def snapshot_at_target(con, tweet_id, *, target=None, tol=None, published_at_utc=None):
    """Снимок метрик в возрасте ~6 ч. Возвращает (likes, replies, age, exact).

    Если точного снимка нет — линейная переоценка по ближайшему (exact=False),
    как в `scoring.velocity_6h`. `None`, если метрик нет вовсе.
    """
    target = float(target or config.SIGNIFICANCE_SNAPSHOT_HOURS)
    tol = float(tol if tol is not None else config.SIGNIFICANCE_SNAPSHOT_TOL)
    ms = list(con.execute(
        "SELECT age_hours, likes, replies FROM post_metrics_history"
        " WHERE tweet_id=? AND age_hours IS NOT NULL ORDER BY age_hours",
        (str(tweet_id),)))
    if not ms and published_at_utc is not None:
        row = con.execute("SELECT likes, replies, metrics_at FROM posts WHERE tweet_id=?",
                          (str(tweet_id),)).fetchone()
        if row is not None and row["likes"] is not None and row["metrics_at"]:
            age = _hours_between(published_at_utc, db.utcnow_iso())
            if age is not None and age >= 0:
                ms = [{"age_hours": age, "likes": row["likes"],
                       "replies": row["replies"] or 0}]
    if not ms:
        return None
    best = min(ms, key=lambda r: abs(float(r["age_hours"]) - target))
    age = float(best["age_hours"])
    likes, replies = best["likes"] or 0, best["replies"] or 0
    if abs(age - target) <= tol:
        return likes, replies, round(age, 4), True
    if age <= 0:
        return None
    factor = target / age
    return int(round(likes * factor)), int(round(replies * factor)), round(age, 4), False


def significance_values(*, likes6, replies6, xconf, hours_since, metrics_missing,
                        spread_graph=0):
    """Формула Р3.1 / Р3.1-бис. Возвращает (significance, engagement, velocity, spread).

    Задача 3 ТЗ виральности: ось «распространение» берёт МАКСИМУМ из xconf-1 и
    числа независимых авторов по графу упоминаний (`scoring.spread_authors`),
    а не только xconf. Раньше spread = xconf-1, и у 88% постов он был нулём,
    из-за чего значимость вырождалась в чистую скорость вовлечённости.
    Веса `SIGNIFICANCE_*` не менялись.
    """
    xconf = max(1, int(xconf or 1))
    spread = max(0, xconf - 1, int(spread_graph or 0))
    hours = max(0.0, float(hours_since or 0.0))
    if metrics_missing:
        sig = (spread ** 0.8) / ((hours + 2.0) ** config.SIGNIFICANCE_AGE_EXP)
        return round(sig, 6), None, None, spread
    engagement = (likes6 or 0) + 2 * (replies6 or 0)
    velocity = engagement / config.SIGNIFICANCE_SNAPSHOT_HOURS
    sig = ((config.SIGNIFICANCE_ENGAGE_W * math.log1p(velocity)
            + config.SIGNIFICANCE_SPREAD_W * math.log1p(spread))
           / ((1.0 + hours / config.SIGNIFICANCE_AGE_24H) ** config.SIGNIFICANCE_AGE_EXP))
    return round(sig, 6), round(engagement, 6), round(velocity, 6), spread


def _story_index(con):
    idx = {}
    for r in con.execute("SELECT sp.tweet_id, s.* FROM story_posts sp"
                         " JOIN stories s ON s.id=sp.story_id"):
        idx[str(r["tweet_id"])] = {"story_id": r["id"], "xconf": r["xconf"] or 1,
                                   "lead_time_min": r["lead_time_min"],
                                   "suspect": r["suspect"], "is_single": r["is_single"],
                                   "published_at": r["published_at"]}
    return idx


def score_post(con, post, *, story=None, now=None, snapshot=None):
    """Оценка одного поста. `post` — строка sqlite с полями ТЗ-4."""
    now = now or datetime.now(timezone.utc)
    hours = _hours_between(post["published_at_utc"], db.iso(now))
    hours = 0.0 if hours is None else hours
    xconf_value = int((story or {}).get("xconf") or 1)
    # Задача 3: независимые авторы по графу упоминаний (ТЗ-4 Р4). Это второй,
    # независимый от сюжетов источник оси «распространение».
    # TODO(debt-D-48): закрыт в ТЗ-7 — ось spread наполнена (замер: 371 строка,
    # >0 у 178), дефект не воспроизводится — см. TECH-DEBT.md.
    # TODO(debt-D-28): spread_authors делает отдельный SQL-запрос на каждый пост;
    # на большом корпусе это узкое место — см. docs/TECH-DEBT.md.
    try:
        graph_authors, _weights = scoring.spread_authors(con, post)
        spread_graph = len(graph_authors)
    except Exception:
        spread_graph = 0
    metrics_at = post["metrics_at"] if "metrics_at" in post.keys() else None
    age_metrics = _hours_between(metrics_at, db.iso(now)) if metrics_at else None
    stale = age_metrics is None or age_metrics > config.METRICS_STALE_HOURS
    missing = 1 if stale else 0
    snap = snapshot if snapshot is not None else snapshot_at_target(
        con, post["tweet_id"], published_at_utc=post["published_at_utc"])
    if snap is None:
        likes6 = replies6 = None
        missing = 1
        exact = False
    else:
        likes6, replies6, _age, exact = snap
    sig, engagement, velocity, spread = significance_values(
        likes6=likes6, replies6=replies6, xconf=xconf_value, hours_since=hours,
        metrics_missing=bool(missing), spread_graph=spread_graph)
    lead = (story or {}).get("lead_time_min")
    first = scoring.score_first(lead)
    engage = (scoring.score_engage(velocity, replies6)
              if velocity is not None else 0.0)
    return {
        "tweet_id": str(post["tweet_id"]),
        "handle": db.post_author(post),
        "story_id": (story or {}).get("story_id"),
        "significance": sig,
        "branch": "graph" if missing else "popularity",
        "metrics_missing": int(missing),
        "metrics_at": metrics_at,
        "metrics_age_hours": None if age_metrics is None else round(age_metrics, 4),
        "likes_at_6h": likes6, "replies_at_6h": replies6,
        "engagement": engagement, "velocity": velocity,
        "xconf": xconf_value, "spread": spread,
        "spread_graph": spread_graph,
        "score_engage": engage, "score_spread": float(spread),
        "score_first": first,
        "velocity_exact": exact,
        "suspect": int((story or {}).get("suspect") or 0),
    }


RETWEET_EXCLUDE_SQL = ("COALESCE(p.is_retweet,0)=0 AND COALESCE(p.pinned,0)=0"
                       " AND p.deleted_at IS NULL AND COALESCE(p.metrics_src,'') != 'cdn_rt'")


def resolve_original_tweet_id(post):
    """tweet_id оригинала для ретвита/цитаты — из ссылок поста, если есть."""
    links = post["links"]
    if isinstance(links, str):
        try:
            links = json.loads(links)
        except ValueError:
            links = [links]
    import re
    for l in links or []:
        m = re.search(r"/status/(\d+)", str(l))
        if m:
            return m.group(1)
    return None


def _suspect_story_ids(con):
    return {r["id"] for r in con.execute("SELECT id FROM stories WHERE suspect=1")}


def _purge_stale(con, *, now, since_days):
    """Задача 5 ТЗ виральности: убрать оценки, выпавшие из текущей выборки.

    `scores.py` уже удаляет suspect-сюжеты, но строки, чей пост стал ретвитом
    или получил `is_ai=0`, остаются лежать вечно. Удаляем оценки ВНУТРИ окна
    пересчёта, чей tweet_id больше не проходит фильтр
    `RETWEET_EXCLUDE_SQL`/`ai_join`. Строки за пределами окна не трогаем:
    они относятся к истории, а не к текущей выборке.
    """
    if not since_days:
        return 0
    cutoff = db.iso(now - timedelta(days=since_days))
    before = con.total_changes
    con.execute(
        "DELETE FROM scores WHERE tweet_id IN"
        " (SELECT tweet_id FROM posts WHERE published_at_utc >= ?)"
        " AND tweet_id NOT IN"
        " (SELECT p.tweet_id FROM posts p " + ai_filter.ai_join()
        + " WHERE " + RETWEET_EXCLUDE_SQL + " AND p.published_at_utc >= ?)",
        (cutoff, cutoff))
    con.commit()
    # rowcount у представления всегда 0 — берём фактическое число удалённых
    # строк из total_changes (TECH-DEBT D-23).
    return db.changes_since(con, before)


def compute(con, *, now=None, persist=True, limit=None, since_days=30, stats=None):
    """Пересчитать значимость. Ретвиты и suspect-сюжеты в рейтинг не подаются."""
    now = now or datetime.now(timezone.utc)
    idx = _story_index(con)
    suspect = _suspect_story_ids(con)
    # ТЗ-8 задача 1: значимость считаем только по постам с приговором is_ai=1.
    q = ("SELECT p.* FROM posts p " + ai_filter.ai_join()
         + " WHERE " + RETWEET_EXCLUDE_SQL)
    params = []
    if since_days:
        q += " AND p.published_at_utc >= ?"
        params.append(db.iso(now - timedelta(days=since_days)))
    q += " ORDER BY p.published_at_utc DESC"
    if limit:
        q += " LIMIT ?"
        params.append(int(limit))
    rows = list(con.execute(q, params))
    out = []
    for r in rows:
        story = idx.get(str(r["tweet_id"]))
        if story and story.get("story_id") in suspect:
            continue
        rec = score_post(con, r, story=story, now=now)
        out.append(rec)
    out.sort(key=lambda d: (-d["significance"], d["tweet_id"]))
    if persist:
        removed_stale = _purge_stale(con, now=now, since_days=since_days)
        if stats is not None:
            stats["removed_stale"] = removed_stale
        if suspect:
            con.execute(
                "DELETE FROM scores WHERE story_id IN (%s)"
                % ",".join("?" * len(suspect)), tuple(suspect))
            con.commit()
        _persist(con, out, now)
    return out


def _persist(con, records, now):
    ts = db.iso(now)
    for r in records:
        con.execute(
            """INSERT INTO scores (tweet_id, story_id, computed_at, significance,
                 branch, metrics_missing, metrics_at, metrics_age_hours,
                 likes_at_6h, replies_at_6h, engagement, velocity, xconf, spread,
                 score_engage, score_spread, score_first)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["tweet_id"], r["story_id"], ts, r["significance"], r["branch"],
             r["metrics_missing"], r["metrics_at"], r["metrics_age_hours"],
             r["likes_at_6h"], r["replies_at_6h"], r["engagement"], r["velocity"],
             r["xconf"], r["spread"], r["score_engage"], r["score_spread"],
             r["score_first"]))
    con.commit()


def rank(con, limit=20, *, now=None):
    """Топ по значимости: читается из `scores`, ретвиты/закреплённые исключены.

    Дополнительно исключаются посты с ИЗМЕРЕННЫМ нулём лайков: их метрики
    принадлежат не им (ретвиты) либо ещё не набрались, и «нулевой» пост не
    должен попадать в топ (П11). Посты без метрик (metrics_missing=1) остаются:
    их оценка честно помечена как временная (Р3.1-бис).
    """
    rows = con.execute(
        """SELECT s.*, COALESCE(p.author_handle, p.owner_handle) AS owner_handle,
                  p.published_at_utc, p.likes, p.replies,
                  p.is_retweet, p.metrics_at AS p_metrics_at
           FROM scores s JOIN posts p ON p.tweet_id=s.tweet_id
           JOIN classified c_ai ON c_ai.text_hash=p.text_hash AND c_ai.is_ai=1
           WHERE p.deleted_at IS NULL AND COALESCE(p.is_retweet,0)=0
             AND COALESCE(p.pinned,0)=0
             AND NOT (COALESCE(s.metrics_missing,0)=0
                      AND COALESCE(s.likes_at_6h,0) <= 0)
           ORDER BY s.significance DESC, s.tweet_id LIMIT ?""", (int(limit),)).fetchall()
    return rows


# ------------------------------------------------------- first_mover_score (Р3.2)
def first_mover_score(con, *, now=None, persist=True, half_life_days=None):
    """Доля «первых» постов автора среди сюжетов, где у него был КОНКУРЕНТ.

    ТЗ п.3.1: сюжеты с одним независимым автором (`xconf < 2`) в ось не входят —
    ни в числитель, ни в знаменатель. Иначе у аккаунта с одними одиночками
    всегда выходило ровно 1.000, и ось мерила одиночество, а не первенство.
    Автор без единого сюжета с конкурентом получает NULL (не 0 и не 1).
    """
    now = now or datetime.now(timezone.utc)
    hl = float(half_life_days or config.FIRST_MOVER_HALFLIFE_DAYS)
    acc = {}
    for r in con.execute(
            "SELECT sp.handle, sp.role, s.published_at FROM story_posts sp"
            " JOIN stories s ON s.id=sp.story_id"
            " WHERE sp.handle IS NOT NULL AND s.suspect=0"
            " AND COALESCE(s.xconf, 1) >= 2"):
        h = r["handle"].lower()
        t = db.parse_iso(r["published_at"])
        if not t:
            continue
        age_days = max(0.0, (now - t).total_seconds() / 86400.0)
        w = 0.5 ** (age_days / hl) if hl > 0 else 1.0
        d = acc.setdefault(h, {"part": 0.0, "prim": 0.0, "stories": 0, "primary": 0})
        d["part"] += w
        d["stories"] += 1
        if r["role"] == config.STORY_ROLE_PRIMARY:
            d["prim"] += w
            d["primary"] += 1
    out = {}
    for h, d in acc.items():
        score = (d["prim"] / d["part"]) if d["part"] else None
        out[h] = {"handle": h, "score": None if score is None else round(score, 6),
                  "stories": d["stories"], "primary": d["primary"]}
    if persist:
        # Полный пересчёт: у кого нет сюжета с конкурентом — NULL, а не старое
        # значение и не 1.000.
        con.execute("UPDATE accounts SET first_mover_score=NULL")
        for h, d in out.items():
            if d["score"] is None:
                continue
            con.execute("UPDATE accounts SET first_mover_score=? WHERE lower(handle)=?",
                        (d["score"], h))
        con.commit()
    return out


# ------------------------------------------------------------ тёмные лошадки (Р3.3)
def darks(con, *, now=None, persist=True, window_days=None, min_stories=None,
          top_n=None):
    """Аккаунты с ростом уникальных сюжетов за неделю, ни разу не в топ-N."""
    now = now or datetime.now(timezone.utc)
    days = int(window_days or config.DARKS_WINDOW_DAYS)
    min_stories = int(min_stories or config.DARKS_MIN_STORIES)
    top_n = int(top_n or config.DARKS_TOP_N)
    cur_lo = db.iso(now - timedelta(days=days))
    prev_lo = db.iso(now - timedelta(days=2 * days))

    def _unique(since, until):
        d = {}
        for r in con.execute(
                "SELECT sp.handle, COUNT(DISTINCT sp.story_id) n FROM story_posts sp"
                " JOIN stories s ON s.id=sp.story_id"
                " WHERE s.published_at >= ? AND s.published_at < ? AND s.suspect=0"
                " AND sp.handle IS NOT NULL GROUP BY lower(sp.handle)",
                (since, until)):
            d[r["handle"].lower()] = r["n"]
        return d

    cur = _unique(cur_lo, db.iso(now))
    prev = _unique(prev_lo, cur_lo)
    top_handles = {(r["owner_handle"] or "").lower() for r in con.execute(
        "SELECT COALESCE(p.author_handle, p.owner_handle) AS owner_handle"
        " FROM scores s JOIN posts p ON p.tweet_id=s.tweet_id"
        " JOIN classified c_ai ON c_ai.text_hash=p.text_hash AND c_ai.is_ai=1"
        " WHERE s.significance IS NOT NULL ORDER BY s.significance DESC LIMIT ?",
        (top_n,))}
    out = []
    for h, n in cur.items():
        if n < min_stories:
            continue
        if h in top_handles:
            continue
        p = prev.get(h, 0)
        growth = (n / p) if p else float(n)
        out.append({"handle": h, "stories_cur": n, "stories_prev": p,
                    "growth": round(growth, 4)})
    out.sort(key=lambda d: (-d["growth"], -d["stories_cur"], d["handle"]))
    if persist:
        ts = db.iso(now)
        con.execute("DELETE FROM darks")
        for d in out:
            con.execute("INSERT OR REPLACE INTO darks (handle, computed_at,"
                        " stories_cur, stories_prev, growth, in_top)"
                        " VALUES (?,?,?,?,?,0)",
                        (d["handle"], ts, d["stories_cur"], d["stories_prev"],
                         d["growth"]))
        con.commit()
    return out


# ------------------------------------------------------------- suspect (Р3.4)
def suspect_stories(con, *, min_xconf=None):
    min_xconf = int(min_xconf if min_xconf is not None else config.SUSPECT_MIN_XCONF)
    return list(con.execute("SELECT * FROM stories WHERE suspect=1 AND xconf>=?"
                            " ORDER BY xconf DESC", (min_xconf,)))


def run(con, *, now=None, persist=True, limit=None):
    """Полный пересчёт осей: значимость -> first_mover_score -> darks."""
    now = now or datetime.now(timezone.utc)
    stats = {}
    recs = compute(con, now=now, persist=persist, limit=limit, stats=stats)
    fm = first_mover_score(con, now=now, persist=persist)
    dk = darks(con, now=now, persist=persist)
    return {"scored": len(recs), "first_movers": fm, "darks": dk,
            "removed_stale": stats.get("removed_stale", 0),
            "top": recs[:config.DARKS_TOP_N]}
