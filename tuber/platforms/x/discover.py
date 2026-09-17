"""Р2/Р2-БИС/Р3/Р4/Р5 — цикл дискавери Tuber-x.

Сеть — ТОЛЬКО через брокер (инвариант П9 ТЗ-1): этот модуль вызывает
`broker.fetch_search(...)` и не имеет права ходить в Nitter сам.

Что делает прогон:
  1. проверяет суточный бюджет (Р1.3) — при исчерпании пропускает прогон;
  2. идёт по рубрикам в порядке приоритета (Р2.1), по одному запросу за раз;
  3. из постов извлекает кандидатов четырёх видов (Р2.3) и дедуплицирует (Р2.4/2.6);
  4. скорит очередь (Р3) и проверяет верхних кандидатов (Р3.1/Р3.2) через
     `registry.verify_account` — там трёхступенчатый статус (Р2-БИС);
  5. пишет отчёт о росте (Р5) командой `discover --report`.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timezone

from . import blocklist, config, store as db, registry, seeds
from .broker import NitterError, NoLiveInstance

_HANDLE_RE = re.compile(config.HANDLE_RE)
_LINK_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com",
               "mobile.twitter.com", "m.twitter.com"}
_LINK_SKIP = {"i", "search", "home", "explore", "intent", "share", "settings",
              "notifications", "messages", "compose", "tos", "privacy"}
_KNOWN_KINDS = ("author", "orig", "mention", "link")


# ------------------------------------------------------------------- бюджет (Р1.3)
def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def search_requests_today(con, day=None):
    """Сколько поисковых запросов дискавери ушло сегодня (расход бюджета)."""
    return con.execute(
        "SELECT COUNT(*) FROM requests WHERE kind='search' AND ts LIKE ?",
        ((day or _today()) + "%",)).fetchone()[0]


def budget_remaining(con, budget):
    """Сколько запросов можно сделать в прогоне.

    `budget` — потолок прогона; одновременно он не может превысить остаток
    суточного бюджета дискавери (Р1.3).
    """
    used_today = search_requests_today(con)
    daily_left = max(0, config.DISCOVERY_DAILY_BUDGET - used_today)
    return min(int(budget), daily_left)


# --------------------------------------------------------- извлечение (Р2.3)
def link_handles(links):
    """Хендлы из ссылок вида https://x.com/<handle> (Р2.3, вид `link`)."""
    out = []
    for u in links or []:
        try:
            p = urllib.parse.urlparse(str(u))
        except ValueError:
            continue
        if p.netloc.lower() not in _LINK_HOSTS:
            continue
        parts = [x for x in p.path.split("/") if x]
        if not parts:
            continue
        cand = parts[0].lstrip("@")
        if cand.lower() in _LINK_SKIP:
            continue
        if _HANDLE_RE.match(cand):
            out.append(cand.lower())
    seen, res = set(), []
    for h in out:
        if h not in seen:
            seen.add(h)
            res.append(h)
    return res


def extract_candidates(post, rubric):
    """Из поста — кандидаты с пометкой вида находки (Р2.3).

    Возвращает список (handle, kind, found_via), kind in author|orig|mention|link.
    """
    out = []
    owner = (db.post_author(post) or "").strip().lower()
    orig = (post.get("orig_handle") or "").strip().lstrip("@").lower()
    if owner:
        out.append((owner, "author", f"author:{rubric}"))
    if orig:
        out.append((orig, "orig", f"orig:{rubric}"))
    for m in post.get("mentions") or []:
        h = str(m).strip().lstrip("@").lower()
        if h:
            out.append((h, "mention", f"mention:{rubric}"))
    for h in link_handles(post.get("links")):
        out.append((h, "link", f"link:{rubric}"))
    return out


def _observer(post, kind, query):
    """Аккаунт-наблюдатель: владелец поста (кроме author, где это сам кандидат)."""
    if kind == "author":
        return f"q:{query}"
    owner = (db.post_author(post) or "").strip().lower()
    return owner or f"q:{query}"


# --------------------------------------------------------- запись кандидатов (Р2.4-2.6)
def upsert_candidate(con, handle, kind, found_via, observer, rubric, *,
                     lang_guess=None, query=None):
    """Записать кандидата: дедуп по handle, рост distinct_sources (Р2.4/2.6)."""
    h, err = registry.validate_handle(handle)
    if err:
        return None
    if blocklist.is_blocked(con, h):
        return None
    if con.execute("SELECT 1 FROM accounts WHERE handle=?", (h,)).fetchone():
        return None  # Р2.5: уже в реестре
    now = db.utcnow_iso()
    source_key = f"{kind}:{rubric}|{observer or (query or '')}"
    row = con.execute("SELECT * FROM candidates WHERE handle=?", (h,)).fetchone()
    if row is None:
        sources = [source_key]
        con.execute(
            """INSERT INTO candidates (handle, found_via, found_in_account, seen_count,
                 distinct_sources, first_seen_at, last_seen_at, sources, priority, rubric,
                 lang_guess, spam)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (h, found_via, observer, 1, 1, now, now, json.dumps(sources, ensure_ascii=False),
             0.0, rubric, lang_guess, registry.candidate_spam(h)))
        con.commit()
        created = True
    else:
        sources = json.loads(row["sources"]) if row["sources"] else []
        if source_key not in sources:
            sources.append(source_key)
        found_via = row["found_via"] or found_via
        con.execute(
            """UPDATE candidates SET seen_count=seen_count+1, last_seen_at=?,
                 distinct_sources=?, sources=?, spam=?,
                 lang_guess=CASE WHEN ?='ru' THEN 'ru' ELSE COALESCE(lang_guess, ?) END
               WHERE handle=?""",
            (now, len(sources), json.dumps(sources, ensure_ascii=False),
             registry.candidate_spam(h), lang_guess, lang_guess, h))
        con.commit()
        created = False
    priority = registry.candidate_priority(
        con, h, distinct_sources=len(sources), found_via=found_via, rubric=rubric,
        sources=sources)
    con.execute("UPDATE candidates SET priority=? WHERE handle=?", (priority, h))
    con.commit()
    return {"handle": h, "kind": kind, "found_via": found_via, "priority": priority,
            "distinct_sources": len(sources), "created": created}


def ingest_posts(con, posts, rubric, *, lang_guess=None, query=None):
    """Разобрать страницу поиска: извлечь и записать всех кандидатов."""
    found = 0
    for p in posts or []:
        lang = _guess_lang(p, query) or lang_guess
        for handle, kind, found_via in extract_candidates(p, rubric):
            observer = _observer(p, kind, query)
            res = upsert_candidate(con, handle, kind, found_via, observer, rubric,
                                   lang_guess=lang, query=query)
            if res:
                found += 1
    return found


_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def _guess_lang(post, query):
    """Язык по тексту поста (Р1.4), с фолбэком на оператор lang запроса."""
    text = post.get("text") or ""
    if _CYRILLIC_RE.search(text):
        return "ru"
    if "lang:ru" in (query or ""):
        return "ru"
    if "lang:en" in (query or ""):
        return "en"
    return None


# ------------------------------------------------------------------ курсоры поиска
def _saved_cursor(con, query):
    row = con.execute("SELECT cursor FROM cursors WHERE kind='search' AND ref=?",
                      (query,)).fetchone()
    return row["cursor"] if row else None


def _save_cursor(con, query, cursor, items):
    con.execute(
        """INSERT INTO cursors (kind, ref, cursor, last_page_at, pages_total, items_total)
           VALUES ('search', ?, ?, ?, 1, ?)""",
        (query, cursor, db.utcnow_iso(), items))
    con.commit()


# ------------------------------------------------------------------ цикл (Р2)
def run_cycle(con, broker, *, budget=config.DISCOVERY_DAILY_BUDGET, lang="all",
              run_id=None, now=None):
    """Один цикл дискавери. Возвращает сводку прогона."""
    budget = int(budget)
    remaining = budget_remaining(con, budget)
    summary = {"budget": budget, "used_before": search_requests_today(con),
               "requests": 0, "pages": 0, "posts": 0, "candidates_found": 0,
               "queries": 0, "skipped": False, "reason": None,
               "rubrics": {}, "lang": lang}
    if remaining <= 0:
        summary["skipped"] = True
        summary["reason"] = "budget_exhausted"
        db.log_run(con, "WARN", f"бюджет дискавери исчерпан"
                                f" ({search_requests_today(con)}/{budget} запросов за сутки),"
                                f" прогон пропущен", run_id=run_id)
        con.commit()
        return summary

    plan = seeds.queries_for(lang=lang, now=now)
    rubric_used = {r["slug"]: 0 for r in seeds.RUBRICS}
    first_pass = []
    # проход 1: по одному запросу за раз, рубрики по приоритету
    for rubric, query in plan:
        if remaining <= 0:
            break
        slug = rubric["slug"]
        if rubric_used[slug] >= rubric["quota_per_day"]:
            continue
        cursor = _saved_cursor(con, query)
        lang_guess = "ru" if "lang:ru" in query else ("en" if "lang:en" in query else None)
        try:
            pages = _fetch_and_ingest(con, broker, rubric, query, cursor, lang_guess)
        except (NitterError, NoLiveInstance) as e:
            db.log_run(con, "WARN", f"поиск не удался ({e}): {query}",
                       run_id=run_id)
            con.commit()
            summary["rubrics"].setdefault(slug, 0)
            pages = None
        if pages is None:
            remaining -= 1
            summary["requests"] += 1
            rubric_used[slug] += 1
            continue
        remaining -= 1
        summary["requests"] += 1
        rubric_used[slug] += 1
        summary["pages"] += 1
        summary["posts"] += pages["posts"]
        summary["candidates_found"] += pages["found"]
        summary["queries"] += 1
        summary["rubrics"][slug] = summary["rubrics"].get(slug, 0) + pages["found"]
        if pages["cursor_next"] and remaining > 0 and rubric_used[slug] < rubric["quota_per_day"]:
            first_pass.append((rubric, query, pages["cursor_next"], lang_guess))

    # проход 2: вторая страница по курсору (Р2.2), не более 2 страниц на запрос
    for rubric, query, cursor, lang_guess in first_pass:
        if remaining <= 0:
            break
        slug = rubric["slug"]
        if rubric_used[slug] >= rubric["quota_per_day"]:
            continue
        try:
            pages = _fetch_and_ingest(con, broker, rubric, query, cursor, lang_guess)
        except (NitterError, NoLiveInstance):
            pages = None
        remaining -= 1
        summary["requests"] += 1
        rubric_used[slug] += 1
        if pages:
            summary["pages"] += 1
            summary["posts"] += pages["posts"]
            summary["candidates_found"] += pages["found"]
            summary["rubrics"][slug] = summary["rubrics"].get(slug, 0) + pages["found"]

    db.log_run(con, "INFO",
               f"дискавери: запросов={summary['requests']} страниц={summary['pages']}"
               f" постов={summary['posts']} кандидатов={summary['candidates_found']}"
               f" (бюджет {budget}, остаток до прогона {remaining + summary['requests']})",
               run_id=run_id)
    con.commit()
    return summary


def _fetch_and_ingest(con, broker, rubric, query, cursor, lang_guess):
    posts = broker.fetch_search(query, cursor=cursor, priority="discover") or []
    found = ingest_posts(con, posts, rubric["рубрика"], lang_guess=lang_guess, query=query)
    cursor_next = None
    for p in posts:
        if p.get("cursor_next"):
            cursor_next = p["cursor_next"]
            break
    _save_cursor(con, query, cursor_next, len(posts))
    return {"posts": len(posts), "found": found, "cursor_next": cursor_next}


# --------------------------------------------------- верификация кандидатов (Р3)
VERIFY_BLOCKLIST_REASONS = ("bad_handle", "not_found", "bot_pattern", "duplicate_content")


def pending_candidates(con, limit=None):
    """Очередь на верификацию: не проверенные, не в стоп-листе, не активные."""
    rows = con.execute(
        """SELECT c.* FROM candidates c
           WHERE (c.validated IS NULL OR c.validated='pending')
             AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.handle=c.handle
                             AND a.status IN ('active','provisional'))
           ORDER BY c.priority DESC, c.seen_count DESC, c.handle ASC""").fetchall()
    out = []
    for r in rows:
        if blocklist.is_blocked(con, r["handle"]):
            continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def verify_candidates(con, broker, *, max_verify=config.DISCOVERY_MAX_VERIFY_DEFAULT,
                      run_id=None):
    """Р3.1/Р3.2: проверить верхних кандидатов очереди (по приоритету)."""
    queue = pending_candidates(con, limit=max_verify)
    results = {"checked": 0, "active": 0, "provisional": 0, "rejected": 0,
               "blocked": 0, "reasons": {}, "details": []}
    for cand in queue:
        h = cand["handle"]
        res = registry.verify_account(con, broker, h, run_id=run_id)
        results["checked"] += 1
        st = res.get("status")
        if st == "active":
            results["active"] += 1
        elif st == "provisional":
            results["provisional"] += 1
        elif st == "rejected":
            results["rejected"] += 1
            reason = res.get("reason") or "unknown"
            results["reasons"][reason] = results["reasons"].get(reason, 0) + 1
            if reason in VERIFY_BLOCKLIST_REASONS:
                blocklist.add(con, h, reason)
                results["blocked"] += 1
        results["details"].append({"handle": h, "status": st, "reason": res.get("reason"),
                                   "items": res.get("items"),
                                   "ai_density": res.get("ai_density"),
                                   "mentioners": res.get("mentioners")})
    # после верификации можно попробовать промоушен provisional (путь б, Р2-БИС.2)
    promoted = registry.promote_provisional(con, run_id=run_id)
    results["promoted"] = promoted
    return results


# ------------------------------------------------------------------ отчёт (Р5)
def report(con):
    """Р5: отчёт о росте реестра простыми строками, без эмодзи."""
    day = _today()
    queue = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    pending = con.execute(
        "SELECT COUNT(*) FROM candidates WHERE validated IS NULL OR validated='pending'"
    ).fetchone()[0]
    verified = con.execute(
        "SELECT COUNT(*) FROM candidates WHERE verified_at IS NOT NULL").fetchone()[0]
    accepted = con.execute(
        "SELECT COUNT(*) FROM candidates WHERE validated IN ('ok','provisional')"
    ).fetchone()[0]
    rejected = con.execute(
        "SELECT COUNT(*) FROM candidates WHERE validated='reject'").fetchone()[0]
    reasons = con.execute(
        """SELECT reject_reason, COUNT(*) n FROM candidates
           WHERE validated='reject' AND reject_reason IS NOT NULL
           GROUP BY reject_reason ORDER BY n DESC LIMIT 5""").fetchall()

    print(f"=== Рост реестра на {day}")
    print(f"кандидатов в очереди: {queue} (не проверено: {pending})")
    print(f"проверено: {verified} | принято: {accepted} | отклонено: {rejected}")
    print("отклонено по причинам (топ-5):")
    if reasons:
        for r in reasons:
            print(f"  {r['reject_reason']:28s} {r['n']}")
    else:
        print("  (нет)")

    print("активных по тирам:")
    rows = con.execute(
        """SELECT tier, COUNT(*) n FROM accounts WHERE status='active'
           GROUP BY tier ORDER BY tier""").fetchall()
    if rows:
        for r in rows:
            print(f"  TIER-{r['tier']}: {r['n']}")
    else:
        print("  (нет активных)")
    prov = con.execute("SELECT COUNT(*) FROM accounts WHERE status='provisional'"
                       ).fetchone()[0]
    print(f"provisional: {prov} из {config.PROVISIONAL_MAX}"
          f" (phase={'bootstrap' if registry.bootstrap_phase(con) else 'growth'})")

    dead = con.execute(
        "SELECT COUNT(*) FROM accounts WHERE status='dead' AND last_attempt_at LIKE ?",
        (day + "%",)).fetchone()[0]
    print(f"dead за сутки: {dead}")

    reqs = search_requests_today(con, day)
    print(f"расход запросов дискавери за сутки: {reqs} из {config.DISCOVERY_DAILY_BUDGET}")

    print("топ-10 новых аккаунтов (откуда пришли):")
    rows = con.execute(
        """SELECT h.handle, h.status, h.tier, c.found_via, c.distinct_sources, c.priority,
                  h.promo_path
           FROM accounts h JOIN candidates c ON c.handle=h.handle
           WHERE h.added_at LIKE ? OR c.verified_at LIKE ?
           ORDER BY h.added_at DESC, c.priority DESC LIMIT 10""",
        (day + "%", day + "%")).fetchall()
    if rows:
        for r in rows:
            by = f" by={r['promo_path']}" if r["promo_path"] else ""
            print(f"  @{r['handle']:20s} {r['status']:11s} TIER-{r['tier']} "
                  f"источник={r['found_via'] or '-'} src={r['distinct_sources']} "
                  f"prio={r['priority']}{by}")
    else:
        print("  (нет новых за сутки)")
    return {"queue": queue, "pending": pending, "verified": verified,
            "accepted": accepted, "rejected": rejected, "provisional": prov,
            "dead_today": dead, "requests_today": reqs}
