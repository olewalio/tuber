"""Реестр аккаунтов и валидатор кандидатов (Р4).

Р4.1 валидация хендла, Р4.2 добавление без сетевой проверки,
Р4.3 шесть порогов приёма, Р4.4 дневной потолок прироста, Р4.5 тиры.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import datetime, timezone

from . import config, store as db
from .broker import NitterError

_HANDLE_RE = re.compile(config.HANDLE_RE)


# --------------------------------------------------------------------- Р4.1
def validate_handle(raw):
    """Вернуть (handle, None) либо (None, 'bad_handle'). Не бросает исключений."""
    if raw is None:
        return None, "bad_handle"
    h = str(raw).strip().lstrip("@").strip()
    if not h or not _HANDLE_RE.match(h):
        return None, "bad_handle"
    return h.lower(), None


def normalize_handle(raw):
    h, err = validate_handle(raw)
    if err:
        raise ValueError(f"bad_handle: {raw!r}")
    return h


# --------------------------------------------------------------------- Р4.2
def add_account(con, handle, tier=None, source=None, *, status="candidate",
                source_type=None, notes=None, added_by=None, topic_guess=None,
                lang=None):
    """Добавить в реестр. Идемпотентно: повторный вызов не ломает запись."""
    h, err = validate_handle(handle)
    if err:
        return {"ok": False, "handle": None, "reason": "bad_handle"}
    tier = (tier or config.DEFAULT_TIER).upper()
    if tier not in config.TIERS:
        return {"ok": False, "handle": h, "reason": f"bad_tier:{tier}"}
    added_by = added_by or config.ADDED_BY
    row = con.execute("SELECT id, tier FROM accounts WHERE handle=?", (h,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO accounts (handle, tier, status, added_by, source_type, notes,"
            " topic_guess, lang, last_attempt_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (h, tier, status, added_by, source_type or source, notes, topic_guess, lang,
             db.utcnow_iso()))
        con.commit()
        return {"ok": True, "handle": h, "created": True, "tier": tier}
    # обновляем только то, что не задано
    con.execute(
        "UPDATE accounts SET tier=COALESCE(?, tier), source_type=COALESCE(?, source_type),"
        " notes=COALESCE(?, notes) WHERE id=?",
        (tier, source_type or source, notes, row["id"]))
    con.commit()
    return {"ok": True, "handle": h, "created": False, "tier": tier}


def add_file(con, path, *, default_tier=None, added_by=None):
    """Р1: registry add-file <путь.csv>. Колонки: handle[, tier, source, notes]."""
    results = {"added": 0, "updated": 0, "rejected": 0, "errors": []}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        has_header = "handle" in sample.splitlines()[0].lower() if sample.strip() else False
        reader = csv.DictReader(fh) if has_header else csv.reader(fh)
        for i, row in enumerate(reader):
            if isinstance(row, dict):
                handle = row.get("handle")
                tier = row.get("tier") or default_tier
                source = row.get("source") or row.get("found_via")
                notes = row.get("notes")
            else:
                handle = row[0] if row else None
                tier = (row[1] if len(row) > 1 else None) or default_tier
                source = row[2] if len(row) > 2 else None
                notes = row[3] if len(row) > 3 else None
            if not handle and not tier and not source:
                continue
            res = add_account(con, handle, tier, source, notes=notes, added_by=added_by)
            if not res["ok"]:
                results["rejected"] += 1
                results["errors"].append(f"row {i + 1}: {handle!r} -> {res['reason']}")
            elif res["created"]:
                results["added"] += 1
            else:
                results["updated"] += 1
    return results


def get_account(con, handle):
    h, err = validate_handle(handle)
    if err:
        return None
    return con.execute("SELECT * FROM accounts WHERE handle=?", (h,)).fetchone()


def list_accounts(con, status=None, tier=None, limit=None):
    q = "SELECT * FROM accounts WHERE 1=1"
    args = []
    if status:
        q += " AND status=?"
        args.append(status)
    if tier:
        q += " AND tier=?"
        args.append(tier.upper())
    q += " ORDER BY tier, handle"
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    return con.execute(q, args).fetchall()


def tier_accounts(con, tier, limit=None):
    """Аккаунты тира для обхода: только active (candidate/dead/blocked не собираем)."""
    q = ("SELECT * FROM accounts WHERE tier=? AND status='active'"
         " ORDER BY (last_success_at IS NULL) DESC, last_success_at ASC")
    args = [tier.upper()]
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    return con.execute(q, args).fetchall()


# ------------------------------------------------------------------ признаки
def text_hash(text):
    if not text:
        return None
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


def compute_intervals_stats(timestamps):
    """CV интервалов между постами (σ/μ) и постов/сутки по списку ISO-времён."""
    times = sorted(db.parse_iso(t) for t in timestamps if t)
    times = [t for t in times if t]
    if len(times) < 2:
        return None, None
    span_sec = (times[-1] - times[0]).total_seconds()
    if span_sec <= 0:
        return 0.0, None
    deltas = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    span_days = max(span_sec / 86400.0, 1.0 / 24.0)  # не меньше часа
    posts_per_day = len(times) / span_days
    mean = sum(deltas) / len(deltas)
    if mean <= 0:
        return 0.0, posts_per_day
    var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    cv = math.sqrt(var) / mean
    return cv, posts_per_day


def compute_features(post_dicts, min_posts_for_cv=None):
    """Признаки поведения по списку постов (словари с Р3.9 или строки БД)."""
    min_posts_for_cv = min_posts_for_cv or config.VERIFY_CV_MIN_POSTS
    n = len(post_dicts)
    out = {"n": n, "posts_per_day": None, "cv_interval": None, "link_ratio": None,
           "rt_ratio": None, "dup_ratio": None}
    if n == 0:
        return out

    def g(p, key):
        try:
            return p[key]
        except (KeyError, IndexError, TypeError):
            return None

    times = [g(p, "published_at_utc") for p in post_dicts]
    cv, ppd = compute_intervals_stats(times)
    if n >= min_posts_for_cv:
        out["cv_interval"] = cv
    out["posts_per_day"] = ppd

    links = 0
    rts = 0
    hashes = set()
    for p in post_dicts:
        lk = g(p, "links")
        if lk is None:
            links += 0
        elif isinstance(lk, str):
            try:
                links += 1 if json.loads(lk) else 0
            except (ValueError, TypeError):
                links += 1 if lk.strip() else 0
        else:
            links += 1 if len(lk) > 0 else 0
        rts += 1 if g(p, "is_retweet") else 0
        h = g(p, "text_hash") or text_hash(g(p, "text"))
        if h:
            hashes.add(h)
    out["link_ratio"] = links / n
    out["rt_ratio"] = rts / n
    out["dup_ratio"] = 1.0 - (len(hashes) / n)
    return out


# ------------------------------------------------------------- Р4.3 валидатор
# ---------------------------------------------------------------- Р3 спам-шаблон
def is_spam_handle(handle) -> bool:
    """Р3: шаблон спама — handle из цифр, либо длина >= 13 с длинной цифровой частью."""
    if not handle:
        return False
    h = str(handle).strip().lstrip("@").lower()
    if not h:
        return False
    if h.isdigit():
        return True
    digits = sum(1 for c in h if c.isdigit())
    if len(h) >= 13 and digits >= 8:
        return True
    return False


def candidate_spam(handle) -> int:
    return 1 if is_spam_handle(handle) else 0


# ------------------------------------------------- Р2-БИС.1 граф упоминаний
def _eligible_mentioner_statuses():
    """Аккаунты, чьё упоминание засчитывается (Р2-БИС.1)."""
    return ("active", "provisional")


def eligible_mentioners(con, handle):
    """Список аккаунтов, чьи упоминания засчитываются хендлу (Р2-БИС.1).

    Засчитывается упоминание от `active` (любой тир) И от `provisional`, если у
    того уже >= 10 собранных постов и ai_density >= 0.5. Самоупоминания не
    считаются, повторные упоминания от одного автора — один раз, спам-хендлы и
    всё, что в стоп-листе, в граф не попадают (Р2-БИС.3).
    """
    from . import blocklist
    h, err = validate_handle(handle)
    if err:
        return []
    like = f'%"{h}"%'
    rows = con.execute(
        """SELECT DISTINCT a.handle, a.status, a.posts_collected, a.ai_density
           FROM posts p JOIN accounts a ON a.id = p.account_id
           WHERE a.status IN ('active','provisional') AND a.handle <> ?
             AND (p.orig_handle = ? OR p.mentions LIKE ?)""",
        (h, h, like)).fetchall()
    out = []
    for r in rows:
        mh = r["handle"]
        if is_spam_handle(mh) or blocklist.is_blocked(con, mh):
            continue
        if r["status"] == "provisional":
            if (r["posts_collected"] or 0) < config.PROVISIONAL_MENTION_MIN_POSTS:
                continue
            if (r["ai_density"] or 0.0) < config.PROVISIONAL_MENTION_MIN_AI_DENSITY:
                continue
        out.append(mh)
    return out


def _trusted_core_mentioners(con, handle):
    """Совместимость ТЗ-1: число разных авторов, чьи упоминания засчитаны."""
    return len(eligible_mentioners(con, handle))


def _trusted_core_origins(con, handle):
    """Записи «кто сослался» — для отчёта о причине решения."""
    return eligible_mentioners(con, handle)


REASONS = {
    "items_lt_15": "инстанс отдал < 15 item: аккаунт не читается",
    "cooccurrence_lt_2": "меньше 2 разных авторов доверенного ядра упоминали хендл",
    "posts_per_day_out_of_range": "постов/сутки вне рабочего диапазона",
    "cv_too_low": "CV интервалов ниже порога (автомат)",
    "link_ratio_too_high": "доля ссылок выше порога",
    "dup_ratio_too_high": "доля дублей выше порога",
    "rt_ratio_too_high": "доля ретвитов выше порога",
    "daily_new_active_cap": "дневной потолок прироста active исчерпан",
    "bad_handle": "хендл не проходит ^[A-Za-z0-9_]{1,15}$",
    # ТЗ-2 / Р2-БИС
    "not_found": "инстанс не отдал ни одного item: аккаунта нет",
    "bot_pattern": "хендл похож на бота (спам-шаблон)",
    "duplicate_content": "контент дублируется сверх порога",
    "provisional_cap": "потолок provisional (300) исчерпан",
    "no_core_backing": "нет 2 упоминаний ядра — отправлен в provisional (Р2-БИС)",
}


def evaluate_candidate(features, items, mentioners):
    """Вернуть (ok, reason_code|None, checks). Порядок проверок — как в Р4.3."""
    checks = {
        "items_ge_15": items >= config.VERIFY_MIN_ITEMS,
        "cooccurrence_ge_2": mentioners >= config.VERIFY_MIN_MENTIONS,
        "posts_per_day_in_range": (
            features["posts_per_day"] is not None
            and config.VERIFY_POSTS_PER_DAY_MIN <= features["posts_per_day"] <= config.VERIFY_POSTS_PER_DAY_MAX),
        "cv_ok": True,
        "link_ratio_ok": (features["link_ratio"] is not None
                          and features["link_ratio"] <= config.VERIFY_MAX_LINK_RATIO),
        "dup_ratio_ok": (features["dup_ratio"] is not None
                         and features["dup_ratio"] <= config.VERIFY_MAX_DUP_RATIO),
        "rt_ratio_ok": (features["rt_ratio"] is not None
                        and features["rt_ratio"] <= config.VERIFY_MAX_RT_RATIO),
    }
    if features["cv_interval"] is not None:
        checks["cv_ok"] = features["cv_interval"] >= config.VERIFY_MIN_CV

    order = (("items_ge_15", "items_lt_15"),
             ("cooccurrence_ge_2", "cooccurrence_lt_2"),
             ("posts_per_day_in_range", "posts_per_day_out_of_range"),
             ("cv_ok", "cv_too_low"),
             ("link_ratio_ok", "link_ratio_too_high"),
             ("dup_ratio_ok", "dup_ratio_too_high"),
             ("rt_ratio_ok", "rt_ratio_too_high"))
    for ok_key, reason_key in order:
        if not checks.get(ok_key, True):
            return False, reason_key, checks
    return True, None, checks


def evaluate_provisional(features, items, mentioners=0):
    """Р2-БИС: те же пороги Р4.3, КРОМЕ cooccurrence_ge_2.

    Возвращает (ok, reason_code|None, checks). `ok=True` означает «годен в
    provisional»: кандидат читается и ведёт себя не как автомат. Причиной отказа
    cooccurrence_lt_2 этот путь НЕ отказывает — иначе bootstrap-ядро не вырастет.
    """
    ok, reason, checks = evaluate_candidate(features, items, mentioners)
    if not ok and reason == "cooccurrence_lt_2":
        # переоценить остальные пороги без cooccurrence
        saved = checks["cooccurrence_ge_2"]
        checks["cooccurrence_ge_2"] = True
        for ok_key, reason_key in (("items_ge_15", "items_lt_15"),
                                   ("posts_per_day_in_range", "posts_per_day_out_of_range"),
                                   ("cv_ok", "cv_too_low"),
                                   ("link_ratio_ok", "link_ratio_too_high"),
                                   ("dup_ratio_ok", "dup_ratio_too_high"),
                                   ("rt_ratio_ok", "rt_ratio_too_high")):
            if not checks.get(ok_key, True):
                checks["cooccurrence_ge_2"] = saved
                return False, reason_key, checks
        checks["cooccurrence_ge_2"] = saved
        return True, None, checks
    return ok, reason, checks


def reason_text(code):
    return REASONS.get(code, code)


# ------------------------------------------------------- Р4 тематическая плотность
def compute_ai_density(posts):
    """Р4.2: доля постов с ИИ-признаком (эвристика, НЕ классификатор ТЗ-3).

    По одному баллу за наличие ключевого слова в тексте ИЛИ ссылки на
    профильный домен. Возвращает долю 0..1 по последним `AI_DENSITY_POSTS` постам.
    """
    rows = list(posts)[:config.AI_DENSITY_POSTS]
    if not rows:
        return 0.0

    def get(p, key):
        try:
            return p[key]
        except (KeyError, IndexError, TypeError):
            return None

    hits = 0
    for p in rows:
        text = (get(p, "text") or "").lower()
        links = get(p, "links")
        if isinstance(links, str):
            links_txt = links.lower()
        elif isinstance(links, (list, tuple)):
            links_txt = " ".join(str(x) for x in links).lower()
        else:
            links_txt = ""
        if any(ind in text for ind in config.AI_TEXT_INDICATORS):
            hits += 1
        elif any(ind in links_txt for ind in config.AI_LINK_INDICATORS):
            hits += 1
    return round(hits / len(rows), 6)


# ------------------------------------------------------------------ Р3.4 тиры
def assign_tier(con, account_id, ai_density, posts_per_day):
    """Р3.4: A (плотность >=0.7 и >=1 пост/сутки, до 200) / B (>=0.5, до 1000) / C."""
    ai = ai_density or 0.0
    ppd = posts_per_day or 0.0
    n_a = con.execute("SELECT COUNT(*) FROM accounts WHERE tier='A' AND status='active'"
                      " AND id<>?", (account_id,)).fetchone()[0]
    n_b = con.execute("SELECT COUNT(*) FROM accounts WHERE tier='B' AND status='active'"
                      " AND id<>?", (account_id,)).fetchone()[0]
    if ai >= config.TIER_A_MIN_AI_DENSITY and ppd >= config.TIER_A_MIN_POSTS_PER_DAY \
            and n_a < config.TIER_A_MAX:
        tier = "A"
    elif ai >= config.TIER_B_MIN_AI_DENSITY and n_b < config.TIER_B_MAX:
        tier = "B"
    else:
        tier = "C"
    con.execute("UPDATE accounts SET tier=? WHERE id=?", (tier, account_id))
    return tier


# ------------------------------------------------------- Р5/Р2-БИС счётчики
def count_by_status(con, status):
    return con.execute("SELECT COUNT(*) FROM accounts WHERE status=?",
                       (status,)).fetchone()[0]


def count_active_today(con):
    """Сколько аккаунтов стали active сегодня (для Р4.4)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return con.execute(
        "SELECT COUNT(*) FROM accounts WHERE status='active' AND verified_at LIKE ?",
        (today + "%",)).fetchone()[0]


def bootstrap_phase(con):
    """Р2-БИС.2: фаза bootstrap, пока count(active) < 20."""
    return count_by_status(con, "active") < config.BOOTSTRAP_MIN_ACTIVE


# --------------------------------------------------------- Р2-БИС промоушен
def _promote(con, account_id, handle, *, path, run_id=None, detail=""):
    """Перевод в active с записью пути промоушена в run_log (Р2-БИС.4)."""
    now = db.utcnow_iso()
    con.execute(
        "UPDATE accounts SET status='active', promo_path=?, verified_at=?,"
        " reject_reason=NULL, provisional_since=COALESCE(provisional_since, ?)"
        " WHERE id=?", (path, now, now, account_id))
    db.log_run(con, "INFO", f"промоушен provisional->active by={path} {detail}".strip(),
               handle=handle, run_id=run_id)
    con.commit()


def tenure_days(con, row, *, now=None, simulate_days=None):
    """Дней непрерывного сбора под статусом provisional (Р2-БИС.2б)."""
    now = now or db.utcnow()
    start = db.parse_iso(row["provisional_since"]) or db.parse_iso(row["added_at"])
    if start is None:
        return 0.0
    days = (now - start).total_seconds() / 86400.0
    if simulate_days is not None:
        days = max(days, float(simulate_days))
    return days


def tenure_eligible(con, row, *, now=None, simulate_days=None):
    """Проверка пути (б): >=14 дней, ai_density>=0.5, чистота качества (Р2-БИС.2)."""
    days = tenure_days(con, row, now=now, simulate_days=simulate_days)
    ai = row["ai_density"] or 0.0
    if days < config.TENURE_DAYS:
        return False, {"days": round(days, 2), "need_days": config.TENURE_DAYS,
                       "ai_density": ai, "reason": "tenure_short"}
    if ai < config.PROMOTE_MIN_AI_DENSITY:
        return False, {"days": round(days, 2), "ai_density": ai,
                       "reason": "ai_density_low"}
    # После bootstrap путь (б) требует отсутствия reject_reason за 30 дней.
    if not bootstrap_phase(con):
        last = db.parse_iso(row["last_reject_at"])
        now = now or db.utcnow()
        if last is not None:
            age = (now - last).total_seconds() / 86400.0
            if age < config.TENURE_REJECT_FREE_DAYS:
                return False, {"days": round(days, 2), "ai_density": ai,
                               "reject_age_days": round(age, 2),
                               "reason": "recent_reject"}
    return True, {"days": round(days, 2), "ai_density": ai}


def promote_provisional(con, *, simulate_days=None, run_id=None, now=None):
    """Промоушен всех provisional в active (Р2-БИС.2, Р2-БИС.4).

    Путь (а) core_mentions доступен, когда фаза bootstrap завершена (>=20 active);
    путь (б) by_tenure — 14 дней непрерывного сбора. Дневной потолок (Р4.4) общий.
    """
    now = now or db.utcnow()
    boot = bootstrap_phase(con)
    rows = con.execute("SELECT * FROM accounts WHERE status='provisional'").fetchall()
    promoted = []
    for row in rows:
        if count_active_today(con) >= config.DAILY_NEW_ACTIVE_CAP:
            db.log_run(con, "WARN", "дневной потолок прироста active исчерпан,"
                                    " provisional остаются в очереди", run_id=run_id)
            break
        # Предохранитель (ТЗ-2-ФИКС): posts_collected = 0 -> отказ.
        # Ядро (active) должно опираться на реальную историю сбора, иначе
        # фиктивные записи начинают считаться доверенными и искажают граф.
        if (row["posts_collected"] or 0) <= 0:
            db.log_run(con, "INFO", "отказ промоушена: нет собранных постов",
                       handle=row["handle"], run_id=run_id)
            continue
        mentioners = eligible_mentioners(con, row["handle"])
        # путь (а): только после bootstrap (Р2-БИС.2)
        if not boot and len(mentioners) >= config.VERIFY_MIN_MENTIONS:
            _promote(con, row["id"], row["handle"], path="core_mentions", run_id=run_id,
                     detail=f"core_mentions={len(mentioners)} "
                            f"({','.join(mentioners[:5])})")
            assign_tier(con, row["id"], row["ai_density"], row["posts_per_day"])
            promoted.append({"handle": row["handle"], "by": "core_mentions",
                             "mentioners": len(mentioners)})
            continue
        ok, info = tenure_eligible(con, row, now=now, simulate_days=simulate_days)
        if ok:
            _promote(con, row["id"], row["handle"], path="tenure", run_id=run_id,
                     detail=f"days={info['days']} ai_density={info['ai_density']}")
            assign_tier(con, row["id"], row["ai_density"], row["posts_per_day"])
            promoted.append({"handle": row["handle"], "by": "tenure", **info})
    con.commit()
    return promoted


def simulate_tenure(con, days=config.TENURE_DAYS, run_id=None):
    """П9: сдвинуть provisional_since назад на `days` (эмуляция непрерывного сбора)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    back = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    con.execute("UPDATE accounts SET provisional_since=?, last_success_at=?,"
                " fail_streak=0, reject_reason=NULL WHERE status='provisional'",
                (back, db.utcnow_iso()))
    con.commit()
    return con.execute("SELECT COUNT(*) FROM accounts WHERE status='provisional'"
                       ).fetchone()[0]


# -------------------------------------------------------------- Р3 скоринг очереди
def tier_a_sources(con, handle, sources=None):
    """Сколько РАЗНЫХ аккаунтов TIER-A связано с хендлом (Р3)."""
    h, err = validate_handle(handle)
    if err:
        return 0
    seen = set()
    for s in (sources or []):
        # ключ источника вида "mention:<рубрика>|<observer>"
        obs = str(s).split("|")[-1].strip().lower()
        if obs:
            row = con.execute("SELECT tier FROM accounts WHERE handle=? AND tier='A'",
                              (obs,)).fetchone()
            if row:
                seen.add(obs)
    like = f'%"{h}"%'
    rows = con.execute(
        """SELECT DISTINCT a.handle FROM posts p JOIN accounts a ON a.id=p.account_id
           WHERE a.tier='A' AND a.status IN ('active','provisional') AND a.handle<>?
             AND (p.orig_handle=? OR p.mentions LIKE ?)""", (h, h, like)).fetchall()
    seen.update(r[0] for r in rows)
    return len(seen)


def candidate_priority(con, handle, *, distinct_sources=None, found_via=None,
                       rubric=None, sources=None):
    """Р3: приоритет очереди кандидатов.

    приоритет = distinct_sources
               + 3 * (встречался у аккаунтов TIER-A)
               + 2 * (рубрика приоритета 1)
               + 1 * (найден среди авторов, а не только в упоминаниях)
               - 2 * (совпадение с шаблоном спама)
    """
    from . import seeds
    h = str(handle).strip().lstrip("@").lower()
    if distinct_sources is None or found_via is None or sources is None:
        row = con.execute("SELECT * FROM candidates WHERE handle=?", (h,)).fetchone()
        if row is None:
            return 0.0
        distinct_sources = row["distinct_sources"] if distinct_sources is None else distinct_sources
        found_via = row["found_via"] if found_via is None else found_via
        rubric = row["rubric"] if rubric is None else rubric
        try:
            sources = json.loads(row["sources"]) if (sources is None and row["sources"]) else (sources or [])
        except (ValueError, TypeError):
            sources = []
    score = float(distinct_sources or 1)
    score += 3.0 * tier_a_sources(con, h, sources)
    rubric_priority = None
    for r in seeds.RUBRICS:
        if r["рубрика"] == rubric or r["slug"] == rubric:
            rubric_priority = r["priority"]
            break
    if rubric_priority == 1:
        score += 2.0
    fv = str(found_via or "")
    if fv.startswith("author") or fv.startswith("orig"):
        score += 1.0
    if is_spam_handle(h):
        score -= 2.0
    return round(score, 4)


def refresh_candidate_priority(con, handle):
    score = candidate_priority(con, handle)
    con.execute("UPDATE candidates SET priority=? WHERE handle=?", (score, handle))
    return score


# ------------------------------------------------------------- verify (Р4.3 + Р2-БИС)
def _posts_for_features(con, account_id, fetched):
    """Посты из БД (последние N) или, если их мало, свежая лента."""
    rows = con.execute(
        "SELECT * FROM posts WHERE account_id=? ORDER BY published_at_utc DESC LIMIT ?",
        (account_id, config.RECENT_POSTS_WINDOW)).fetchall()
    if len(rows) >= config.VERIFY_CV_MIN_POSTS:
        return rows, "db"
    return fetched, "feed"


def verify_account(con, broker, handle, *, run_id=None, apply_status=True):
    """Р4.3 + Р2-БИС: проверить кандидата.

    Решение:
      * не проходит 5 порогов (кроме cooccurrence) -> rejected с причиной;
      * проходит 5 порогов и есть >=2 упоминания ядра -> active (by=core_mentions);
      * проходит 5 порогов без ядра -> provisional (разрыв замкнутого круга).

    cooccurrence_ge_2 НЕ является причиной отказа при пустом ядре: иначе реестр
    не вырастет (Р2-БИС). Возвращает dict с решением и измеренными признаками.
    """
    h, err = validate_handle(handle)
    if err:
        return {"ok": False, "handle": None, "reason": "bad_handle", "status": None}
    row = con.execute("SELECT * FROM accounts WHERE handle=?", (h,)).fetchone()
    account_id = row["id"] if row else None
    if row is None:
        add_account(con, h)
        row = con.execute("SELECT * FROM accounts WHERE handle=?", (h,)).fetchone()
        account_id = row["id"]

    # Р4.3.1 — сколько item отдал инстанс по этому аккаунту
    items = 0
    fetched = []
    fetch_error = None
    try:
        fetched = broker.fetch_feed(h, priority="discover", force=True) or []
        items = len(fetched)
    except NitterError as e:
        fetch_error = str(e)

    mentioners = eligible_mentioners(con, h)
    source_posts, feat_src = _posts_for_features(con, account_id, fetched)
    features = compute_features(source_posts)
    ai_density = compute_ai_density(source_posts)

    prov_ok, prov_reason, checks = evaluate_provisional(features, items, len(mentioners))

    # --- решение по трём ступеням (Р2-БИС)
    promo_path = None
    if not prov_ok:
        new_status, reason = "rejected", prov_reason
    elif len(mentioners) >= config.VERIFY_MIN_MENTIONS:
        new_status, reason, promo_path = "active", None, "core_mentions"
    else:
        new_status, reason = "provisional", None

    # бот-паттерн / не найден -> сразу отказ в стоп-лист-причины
    if is_spam_handle(h) and new_status != "rejected":
        new_status, reason = "rejected", "bot_pattern"
    if not fetch_error and items == 0 and new_status != "rejected":
        new_status, reason = "rejected", "not_found"

    daily_cap_hit = False
    provisional_cap_hit = False
    if new_status == "active" and apply_status \
            and count_active_today(con) >= config.DAILY_NEW_ACTIVE_CAP:
        # Р4.4: при исчерпанном потолке кандидат остаётся в очереди
        new_status, reason, daily_cap_hit = "provisional", "daily_new_active_cap", True
    if new_status == "provisional" and apply_status \
            and count_by_status(con, "provisional") >= config.PROVISIONAL_MAX:
        new_status, reason, provisional_cap_hit = "candidate", "provisional_cap", True

    if not apply_status:
        new_status = None

    now = db.utcnow_iso()
    fresh_success = "last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END"
    with con:
        con.execute(
            f"""UPDATE accounts SET
                 cv_interval=?, posts_per_day=?, link_ratio=?, rt_ratio=?, dup_ratio=?,
                 ai_density=?, ai_density_src=?,
                 verified_at=?, last_attempt_at=?,
                 {fresh_success},
                 status=COALESCE(?, status),
                 provisional_since=CASE WHEN ?='provisional' THEN COALESCE(provisional_since, ?)
                                        ELSE provisional_since END,
                 promo_path=CASE WHEN ?='active' THEN ? ELSE promo_path END,
                 reject_reason=?, last_reject_at=CASE WHEN ? THEN ? ELSE last_reject_at END
               WHERE id=?""",
            (features["cv_interval"], features["posts_per_day"], features["link_ratio"],
             features["rt_ratio"], features["dup_ratio"], ai_density,
             config.AI_DENSITY_SRC_HEURISTIC, now, now, items > 0, now, new_status,
             new_status, now, new_status, promo_path, reason,
             1 if (reason and new_status == "rejected") else 0, now, account_id))
        if daily_cap_hit or provisional_cap_hit:
            validated, reject_reason = "pending", None
        elif new_status == "rejected":
            validated, reject_reason = "reject", reason
        elif new_status == "provisional":
            validated, reject_reason = "provisional", None
        else:
            validated, reject_reason = "ok", None
        con.execute(
            # UPSERT делает триггер представления candidates (SQLite не умеет
            # UPSERT по представлению): validated/reject_reason/last_seen_at/
            # promoted_by/verified_at обновляются той же семантикой.
            "INSERT INTO candidates (handle, validated, reject_reason, first_seen_at,"
            " last_seen_at, priority, promoted_by, verified_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (h, validated, reject_reason, now, now,
             candidate_priority(con, h), promo_path, now))
    if new_status == "active":
        assign_tier(con, account_id, ai_density, features["posts_per_day"])
        db.log_run(con, "INFO",
                   f"промоушен candidate->active by=core_mentions core_mentions={len(mentioners)}",
                   handle=h, run_id=run_id)
    elif daily_cap_hit:
        db.log_run(con, "WARN", f"дневной потолок прироста active"
                                f" ({config.DAILY_NEW_ACTIVE_CAP}) достигнут,"
                                f" кандидат получает provisional", handle=h, run_id=run_id)
    elif provisional_cap_hit:
        db.log_run(con, "WARN", f"потолок provisional ({config.PROVISIONAL_MAX})"
                                f" достигнут, кандидат остаётся в очереди",
                   handle=h, run_id=run_id)
    elif new_status == "provisional":
        db.log_run(con, "INFO",
                   "промоушен candidate->provisional (ядро пусто: cooccurrence НЕ отказ)"
                   f" items={items} ppd={features['posts_per_day']}",
                   handle=h, run_id=run_id)
    elif reason:
        db.log_run(con, "INFO", f"кандидат отклонён: {reason} ({reason_text(reason)})",
                   handle=h, run_id=run_id)
    con.commit()

    return {"ok": new_status == "active" or new_status == "provisional",
            "handle": h, "reason": reason, "status": new_status,
            "items": items, "mentioners": len(mentioners),
            "mentioners_list": mentioners, "checks": checks, "features": features,
            "ai_density": ai_density, "feature_source": feat_src,
            "fetch_error": fetch_error, "daily_cap_hit": daily_cap_hit,
            "provisional_cap_hit": provisional_cap_hit, "promo_path": promo_path}
