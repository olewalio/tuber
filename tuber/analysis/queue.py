"""Разбор очереди кандидатов и миля повышения (ТЗ-46).

Зачем
-----
Очередь новых аккаунтов (``candidate``) не разбиралась: 17 770 записей в
статусе ``new`` ждали вердикта, ``promoted_at`` был пуст у всех 21 250 строк.
Новые авторы не попадали в активный сбор. Этот модуль закрывает оба шага:

* **``queue review``** — у КАЖДОГО кандидата вердикт с причиной: ``promote``
  (в активный сбор), ``hold`` (не хватает данных), ``reject`` (мусор/дубль/
  нерелевантно). Вердикт, причина и дата разбора пишутся в ``candidate``.
* **``queue promote``** — переводит вердикт ``promote`` в активный сбор:
  заводит/активирует строку ``source`` (``candidate``/``new`` →
  ``active``/``provisional`` по измеримым правилам), закрывает строку очереди
  (``promoted_at``/``promoted_by``). Идемпотентно: повторный прогон не
  дублирует ни источников, ни отметок.

Критерии вердикта (числами, не «на глаз»)
-----------------------------------------
Источники данных — эвристики и БЕСПЛАТНЫЕ страницы (``t.me``, YouTube Data API,
сессия X); DeepSeek по умолчанию не зовётся вовсе (см. ``--llm-budget-usd``).

* мёртвый/пустой источник (нет постов за 30 дней, канал удалён) → ``reject``;
* дубль уже известного источника (каноническая ссылка/``handle``) → ``reject``;
* **Telegram**: подписчики с превью ``t.me/<handle>`` + ≥ 3 поста за 30 дней →
  ``promote``; 1–2 поста → ``hold``; 0 постов/канал удалён → ``reject``;
* **X**: подписчики сессии X (профиль) + ≥ 3 поста за 30 дней и медиана лайков
  ≥ порога → ``promote``, иначе ``hold``; аккаунт не найден/закрыт → ``reject``;
  бюджет сессии X не позволил проверить → ``hold``;
* **YouTube**: подписчики из API + ≥ 3 видео за 30 дней → ``promote``;
  1–2 видео → ``hold``; 0 видео/канал удалён → ``reject``;
* **web**: сборщика веб-фидов нет (D-49), посты получить нечем → ``reject``.

Правило ``active`` против ``provisional`` (как у существующих контуров):
``active`` — если подписчиков ≥ :data:`ACTIVE_SUBS_MIN` (1 000) и данных
достаточно; иначе ``provisional``.

CLI::

    python3 -m tuber queue review  [--db PATH] [--platform P] [--limit N]
                                   [--offline] [--dry-run] [--allow-production]
                                   [--time-cap-sec S] [--workers N]
                                   [--x-budget N] [--llm-budget-usd X]
    python3 -m tuber queue promote [--db PATH] [--dry-run] [--allow-production]
                                   [--collect/--no-collect] [--limit N]
    python3 -m tuber queue report  [--db PATH] [--json] [--top N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from tuber import config
from tuber.core import db as core_db
from tuber.core import storage

# ---------------------------------------------------------------------------
# Константы вердиктов и порогов
# ---------------------------------------------------------------------------

#: Вердикты (термины ТЗ).
PROMOTE = "promote"
HOLD = "hold"
REJECT = "reject"

#: Вердикт → статус строки очереди ``candidate``.
STATUS_OF = {PROMOTE: "promoted", HOLD: "hold", REJECT: "rejected"}
#: Вердикт → ``candidate.validated`` (терминология проекта X/YouTube).
VALIDATED_OF = {PROMOTE: "ok", HOLD: "pending", REJECT: "reject"}

#: Источник отметок в очереди.
REVIEW_SOURCE = "queue_review"
PROMOTE_SOURCE = "queue_promote"

#: Окно «живости» источника, дней.
POSTS_WINDOW_DAYS = 30
#: Минимум постов/видео за окно для вердикта ``promote``.
MIN_POSTS_30D = 3
#: Медиана лайков X, ниже которой аккаунт уходит в ``hold`` (Контур 2 плана).
X_MEDIAN_LIKES_MIN = 20.0
#: С какой численности подписчиков источник сразу ``active`` (иначе ``provisional``).
ACTIVE_SUBS_MIN = 1_000

#: Пауза между HTTP-запросами Telegram, с.
TG_PAUSE_SEC = 1.2
#: Потолок времени прогона review по умолчанию, с (обёртка даёт свой).
DEFAULT_TIME_CAP_SEC = 3600.0
#: Потолок LLM-расхода на весь разбор, USD (ТЗ-46 п.4).
DEFAULT_LLM_BUDGET_USD = 1.0

#: Платформы в порядке разбора (web — мгновенный reject, без сети).
PLATFORMS = ("web", "youtube", "telegram", "x")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def norm_handle(platform: str, handle: str | None) -> str:
    """Каноническая форма ``handle`` для сравнения на дубль."""
    h = (handle or "").strip()
    if h.startswith("@"):
        h = h[1:]
    if platform == "web":
        # Кандидат web хранит URL-кодированный домен — раскодируем.
        try:
            from urllib.parse import unquote

            h = unquote(h)
        except Exception:  # noqa: BLE001
            pass
    return h.lower()


# ---------------------------------------------------------------------------
# Сеть: сборщики свидетельств
# ---------------------------------------------------------------------------

class NetworkFetchers:
    """Бесплатные страницы: Telegram preview, YouTube Data API, сессия X."""

    def __init__(self, *, con=None, db_path=None, x_budget=None, time_cap_sec=None,
                 tg_pause=None, workers=4, log=None):
        self.con = con
        self.db_path = db_path
        self.x_budget = x_budget
        self.time_cap_sec = time_cap_sec or DEFAULT_TIME_CAP_SEC
        self.tg_pause = TG_PAUSE_SEC if tg_pause is None else tg_pause
        self.workers = max(1, int(workers))
        self.log = log or (lambda *a, **k: None)
        self._started = time.monotonic()
        self._tg_client = None
        self._tg_shell = 0
        self._yt = None
        self._x = None

    def time_exceeded(self) -> bool:
        return (time.monotonic() - self._started) >= self.time_cap_sec

    # -- Telegram ----------------------------------------------------------
    def _tg_http(self):
        if self._tg_client is None:
            from tuber.platforms.telegram import collect as tg_collect

            self._tg_client = tg_collect.make_client()
        return self._tg_client

    def telegram(self, handle: str) -> dict:
        """Посты с ``t.me/s/<handle>`` и подписчики (точные либо округлённые).

        Превью ``t.me/<handle>`` отдаёт ТОЧНОЕ число, но умеет отвечать
        JS-оболочкой (антибот) — тогда берём округлённое число из ленты ``/s/``
        (``tgme_channel_info_counter``, напр. ``10.7K``). Для вердикта округления
        достаточно; поля ``outcome``/``subs_source`` честно это показывают.
        """
        from tuber.platforms.telegram import collect as tg_collect
        from tuber.platforms.telegram import followers as tg_followers

        client = self._tg_http()
        out = {"platform": "telegram", "handle": handle, "subs": None,
               "subs_source": None, "posts30": 0, "posts": [],
               "outcome": "ok", "note": None}
        code, html = tg_collect.http_get(client, f"https://t.me/s/{handle}")
        if code is None or code >= 400 or html is None:
            out["outcome"] = "fetch_error"
            out["note"] = "лента /s/ недоступна"
            return out
        if tg_collect.page_is_unreadable(html):
            out["outcome"] = "no_feed"
            out["note"] = "нет публичной ленты (канал удалён/закрыт или не канал)"
            return out
        posts = tg_collect.parse_page(html, handle)
        out["posts"] = posts
        out["posts30"] = _count_recent(posts, "date_utc", POSTS_WINDOW_DAYS)
        out["subs"] = _parse_rounded_subs(html)
        if out["subs"] is not None:
            out["subs_source"] = "rounded"
        # Точное число с превью — если антибот отдаёт настоящую страницу.
        # Если превью дважды подряд отдало JS-оболочку — больше не пробуем.
        if self.tg_pause:
            time.sleep(self.tg_pause)
        if self._tg_shell < 2:
            code2, html2 = tg_collect.http_get(
                client, tg_followers.preview_url(handle))
            if code2 == 200 and html2 and not _is_js_shell(html2):
                parsed = tg_followers.parse_preview(html2)
                if parsed.outcome == tg_followers.OUTCOME_VALUE:
                    out["subs"] = parsed.value
                    out["subs_source"] = "exact"
            else:
                self._tg_shell += 1
        return out

    # -- YouTube -----------------------------------------------------------
    def _yt_client(self):
        if self._yt is None:
            from tuber.platforms.youtube import api as yt_api
            from tuber.platforms.youtube import store as yt_store

            try:
                yt_store.install_compat(self.con)
            except Exception:  # noqa: BLE001 — квота не должна ронять разбор
                pass
            self._yt = yt_api.YouTubeClient(conn=self.con, min_interval=0.1)
        return self._yt

    def youtube(self, ids: list[str]) -> dict:
        """Подписчики и свежие видео каналов пачками (YouTube Data API)."""
        client = self._yt_client()
        out: dict[str, dict] = {}
        items = client.channels_by_ids(ids)
        by_id = {it.get("id"): it for it in items}
        cutoff = _now() - timedelta(days=POSTS_WINDOW_DAYS)
        for cid in ids:
            it = by_id.get(cid)
            if it is None:
                out[cid] = {"exists": False, "subs": None, "videos30": 0,
                            "video_ids": [], "title": None, "uploads": None}
                continue
            stats = it.get("statistics") or {}
            subs = None
            if str(stats.get("hiddenSubscriberCount", "false")).lower() != "true":
                try:
                    subs = int(stats.get("subscriberCount"))
                except (TypeError, ValueError):
                    subs = None
            uploads = ((it.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
            vids = []
            if uploads:
                try:
                    pl = client.playlist_items(uploads, max_results=10)
                    vids = [x for x in pl if _published_after(x, cutoff)]
                except Exception as exc:  # noqa: BLE001 — квота/сеть не роняют разбор
                    self.log("youtube", cid, f"playlist недоступен: {exc}")
            out[cid] = {
                "exists": True,
                "subs": subs,
                "videos30": len(vids),
                "video_ids": [(((v.get("contentDetails") or {}).get("videoId"))
                               or ((v.get("snippet") or {}).get("resourceId") or {}).get("videoId"))
                              for v in vids],
                "title": (it.get("snippet") or {}).get("title"),
                "uploads": uploads,
                "custom_url": (it.get("snippet") or {}).get("customUrl"),
            }
        return out

    # -- X -----------------------------------------------------------------
    def _x_client(self):
        if self._x is None:
            from tuber.platforms.x import session as x_session

            self._x = x_session.XSessionTransport(db_path=self.db_path)
        return self._x

    def x(self, handle: str) -> dict:
        """Профиль (подписчики) и лента X через сессию."""
        t = self._x_client()
        out = {"platform": "x", "handle": handle, "subs": None, "posts30": 0,
               "median_likes": None, "tweets": [], "outcome": "ok", "note": None}
        try:
            prof = t.profile(handle)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "не найден" in msg or "404" in msg:
                out["outcome"] = "gone"
                out["note"] = "аккаунт не найден"
            elif "cooldown" in msg or "бюджет" in msg or "RateLimited" in type(exc).__name__:
                out["outcome"] = "budget"
                out["note"] = "сессия X недоступна (бюджет/cooldown)"
            else:
                out["outcome"] = "fetch_error"
                out["note"] = msg[:200]
            return out
        if not prof:
            out["outcome"] = "gone"
            out["note"] = "аккаунт не найден"
            return out
        out["subs"] = prof.get("followers")
        if prof.get("protected"):
            out["outcome"] = "protected"
            out["note"] = "закрытый аккаунт"
            return out
        try:
            tweets, _cursor = t.timeline(prof.get("id"), count=20)
        except Exception as exc:  # noqa: BLE001
            out["outcome"] = "fetch_error"
            out["note"] = str(exc)[:200]
            return out
        recent = [tw for tw in tweets if _tweet_recent(tw)]
        out["tweets"] = tweets
        out["posts30"] = len(recent)
        likes = [tw.get("likes") for tw in recent if isinstance(tw.get("likes"), int)]
        out["median_likes"] = _median(likes) if likes else None
        return out


def _count_recent(rows: list[dict], key: str, days: int) -> int:
    cutoff = _now() - timedelta(days=days)
    n = 0
    for r in rows:
        dt = _parse_dt(r.get(key))
        if dt is not None and dt >= cutoff:
            n += 1
    return n


_SUBS_RE = re.compile(
    r'counter_value">\s*([0-9][0-9.,]*\s*[KkMm]?)\s*</span>\s*'
    r'<span class="counter_type">subscribers', re.I)


def _is_js_shell(html: str) -> bool:
    """Превью t.me отдало JS-оболочку (антибот), а не страницу канала."""
    return "location.hash" in html and "tgme_page_extra" not in html


def _parse_rounded_subs(html: str) -> int | None:
    """Округлённые подписчики из ленты ``/s/`` (``10.7K`` → 10 700).

    TODO(debt-D-62): при JS-оболочке превью ``t.me/<handle>`` точное число
    недоступно, и для вердикта берётся округлённое — см. TECH-DEBT.md.
    """
    m = _SUBS_RE.search(html)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    mult = 1
    if raw[-1:].lower() == "k":
        mult, raw = 1_000, raw[:-1]
    elif raw[-1:].lower() == "m":
        mult, raw = 1_000_000, raw[:-1]
    try:
        return int(float(raw) * mult)
    except (TypeError, ValueError):
        return None


def _published_after(item: dict, cutoff: datetime) -> bool:
    pub = (item.get("contentDetails") or {}).get("videoPublishedAt") \
        or (item.get("snippet") or {}).get("publishedAt")
    dt = _parse_dt(pub)
    return dt is not None and dt >= cutoff


def _tweet_recent(tw: dict) -> bool:
    dt = _parse_dt(tw.get("published_at_utc") or tw.get("created_at"))
    return dt is not None and dt >= (_now() - timedelta(days=POSTS_WINDOW_DAYS))


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in (None, "%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S +0000 %Y"):
        try:
            dt = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _median(values: list) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


# ---------------------------------------------------------------------------
# Разбор очереди
# ---------------------------------------------------------------------------

class QueueReviewer:
    """Разбор очереди ``candidate``: вердикт + причина + дата по каждому."""

    def __init__(self, con: sqlite3.Connection, *, now: datetime | None = None,
                 fetchers=None, offline: bool = False, limit: int | None = None,
                 platforms=None, run_id: int | None = None, llm_budget_usd: float = 0.0):
        self.con = con
        self.now = now or _now()
        self.fetchers = fetchers
        self.offline = offline
        self.limit = limit
        self.platforms = tuple(platforms or PLATFORMS)
        self.run_id = run_id
        self.llm_budget_usd = float(llm_budget_usd or 0.0)
        self.llm_calls = 0
        self.known = self._load_known()
        self.summary: dict[str, dict] = {}

    # -- подготовка --------------------------------------------------------
    def _load_known(self) -> dict[str, set[str]]:
        """Множества известных источников по платформам (для дублей)."""
        out: dict[str, set[str]] = {p: set() for p in PLATFORMS}
        for r in self.con.execute(
                "SELECT platform, handle, external_id FROM source"):
            p = r["platform"]
            if p not in out:
                continue
            if r["handle"]:
                out[p].add(norm_handle(p, r["handle"]))
            if r["external_id"]:
                out[p].add(norm_handle(p, r["external_id"]))
        return out

    def _blocklist(self) -> set[tuple[str, str]]:
        return {(r["platform"], norm_handle(r["platform"], r["handle"]))
                for r in self.con.execute("SELECT platform, handle FROM blocklist")}

    def _candidates(self, platform: str) -> list[sqlite3.Row]:
        sql = ("SELECT id, platform, handle, external_id, kind, meta_json, found_via, "
               "rubric FROM candidate WHERE platform=? AND status='new' ORDER BY id")
        rows = list(self.con.execute(sql, (platform,)))
        if self.limit is not None:
            rows = rows[: self.limit]
        return rows

    # -- запись вердикта ---------------------------------------------------
    def _apply(self, cand_id: int, verdict: str, reason: str, evidence: dict) -> None:
        row = self.con.execute("SELECT meta_json FROM candidate WHERE id=?",
                               (cand_id,)).fetchone()
        meta: dict = {}
        if row and row[0]:
            try:
                meta = json.loads(row[0])
            except (TypeError, ValueError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["review"] = {
            "verdict": verdict,
            "reason": reason,
            "reviewed_at": _iso(self.now),
            "by": REVIEW_SOURCE,
            "evidence": evidence,
        }
        self.con.execute(
            "UPDATE candidate SET status=?, validated=?, reject_reason=?, meta_json=? "
            "WHERE id=?",
            (STATUS_OF[verdict], VALIDATED_OF[verdict], reason,
             storage.jdump(meta), cand_id),
        )

    # -- платформенные разборы --------------------------------------------
    def review_web(self, rows: list[sqlite3.Row]) -> None:
        reason = ("веб-фид: нет сборщика постов (D-49) — посты получить нечем")
        for r in rows:
            self._apply(r["id"], REJECT, reason, {"collector": None})
            _bump(self.summary, "web", REJECT)

    def review_youtube(self, rows: list[sqlite3.Row]) -> None:
        if not rows:
            return
        if self.offline:
            self._offline_hold(rows, "youtube", "офлайн-режим: YouTube API не зван")
            return
        # Отбор дублей и невалидных id до сети.
        todo: list[sqlite3.Row] = []
        for r in rows:
            h = norm_handle("youtube", r["handle"])
            eid = norm_handle("youtube", r["external_id"] or r["handle"])
            if h in self.known["youtube"] or eid in self.known["youtube"]:
                self._apply(r["id"], REJECT, "дубль известного источника",
                            {"handle": r["handle"]})
                _bump(self.summary, "youtube", REJECT)
                continue
            if not (r["handle"] or "").startswith("UC"):
                # TODO(debt-D-60): очередь YouTube, кроме каналов (kind='channel'),
                # содержит поисковые запросы и id видео (ссылки из Telegram) —
                # канал по ним не разрешается, см. TECH-DEBT.md.
                kind = (r["kind"] or "").lower() if "kind" in r.keys() else ""
                if kind == "query":
                    reason = "не аккаунт YouTube (поисковый запрос)"
                elif kind in ("youtube", "video"):
                    reason = "не аккаунт YouTube (id видео, канал не разрешён)"
                else:
                    reason = "handle не разрешён в id канала"
                self._apply(r["id"], REJECT, reason, {"handle": r["handle"]})
                _bump(self.summary, "youtube", REJECT)
                continue
            todo.append(r)
        ids = [r["handle"] for r in todo]
        data: dict[str, dict] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            try:
                data.update(self.fetchers.youtube(chunk))
            except Exception as exc:  # noqa: BLE001 — сеть не роняет разбор
                for cid in chunk:
                    data[cid] = {"exists": None, "note": str(exc)[:200]}
        for r in todo:
            info = data.get(r["handle"], {})
            if info.get("exists") is False:
                self._apply(r["id"], REJECT, "канал удалён",
                            {"handle": r["handle"]})
                _bump(self.summary, "youtube", REJECT)
                continue
            if info.get("exists") is None:
                self._apply(r["id"], HOLD, "YouTube API не ответил",
                            {"note": info.get("note")})
                _bump(self.summary, "youtube", HOLD)
                continue
            v30 = int(info.get("videos30") or 0)
            subs = info.get("subs")
            ev = {"subs": subs, "videos30": v30, "title": info.get("title"),
                  "video_ids": info.get("video_ids") or []}
            if v30 == 0:
                self._apply(r["id"], REJECT, "нет видео за 30 дней", ev)
                _bump(self.summary, "youtube", REJECT)
            elif v30 < MIN_POSTS_30D:
                self._apply(r["id"], HOLD, f"мало видео за 30 дней ({v30} < "
                                           f"{MIN_POSTS_30D})", ev)
                _bump(self.summary, "youtube", HOLD)
            elif subs is None:
                self._apply(r["id"], HOLD, "подписчики скрыты (API не отдал)",
                            ev)
                _bump(self.summary, "youtube", HOLD)
            else:
                self._apply(r["id"], PROMOTE,
                            f"подписчиков {subs}, видео за 30 дней {v30}", ev)
                _bump(self.summary, "youtube", PROMOTE)

    def review_telegram(self, rows: list[sqlite3.Row]) -> None:
        if not rows:
            return
        if self.offline:
            self._offline_hold(rows, "telegram", "офлайн-режим: t.me не зван")
            return
        block = self._blocklist()
        todo: list[sqlite3.Row] = []
        for r in rows:
            h = norm_handle("telegram", r["handle"])
            if h in self.known["telegram"]:
                self._apply(r["id"], REJECT, "дубль известного источника",
                            {"handle": r["handle"]})
                _bump(self.summary, "telegram", REJECT)
            elif ("telegram", h) in block:
                self._apply(r["id"], REJECT, "в блоклисте", {"handle": r["handle"]})
                _bump(self.summary, "telegram", REJECT)
            else:
                todo.append(r)

        workers = getattr(self.fetchers, "workers", 4)
        results: dict[str, dict] = {}
        if self.offline or not todo:
            pass
        else:
            def work(r):
                try:
                    res = self.fetchers.telegram(r["handle"])
                    return r["handle"], res
                except Exception as exc:  # noqa: BLE001
                    return r["handle"], {"outcome": "fetch_error",
                                         "note": str(exc)[:200], "posts30": 0}
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for handle, res in ex.map(work, todo):
                    results[handle] = res

        for r in todo:
            res = results.get(r["handle"], {"outcome": "fetch_error", "posts30": 0})
            outcome = res.get("outcome")
            posts30 = int(res.get("posts30") or 0)
            subs = res.get("subs")
            ev = {"subs": subs, "posts30": posts30, "note": res.get("note")}
            if outcome == "gone":
                self._apply(r["id"], REJECT, "канал удалён/переименован", ev)
                _bump(self.summary, "telegram", REJECT)
            elif outcome in ("no_feed", "unreadable"):
                self._apply(r["id"], REJECT,
                            "нет публичной ленты (канал удалён/закрыт или не канал)",
                            ev)
                _bump(self.summary, "telegram", REJECT)
            elif outcome in ("fetch_error", None):
                self._apply(r["id"], HOLD, res.get("note") or "t.me не ответил", ev)
                _bump(self.summary, "telegram", HOLD)
            elif posts30 == 0:
                self._apply(r["id"], REJECT, "нет постов за 30 дней", ev)
                _bump(self.summary, "telegram", REJECT)
            elif posts30 < MIN_POSTS_30D:
                self._apply(r["id"], HOLD,
                            f"мало постов за 30 дней ({posts30} < {MIN_POSTS_30D})", ev)
                _bump(self.summary, "telegram", HOLD)
            elif subs is None:
                self._apply(r["id"], HOLD, "нет счётчика подписчиков", ev)
                _bump(self.summary, "telegram", HOLD)
            else:
                self._apply(r["id"], PROMOTE,
                            f"подписчиков {subs}, постов за 30 дней {posts30}", ev)
                _bump(self.summary, "telegram", PROMOTE)

    def review_x(self, rows: list[sqlite3.Row]) -> None:
        if not rows:
            return
        block = self._blocklist()
        budget = self._x_budget()
        checked = 0
        for r in rows:
            h = norm_handle("x", r["handle"])
            if h in self.known["x"]:
                self._apply(r["id"], REJECT, "дубль известного источника",
                            {"handle": r["handle"]})
                _bump(self.summary, "x", REJECT)
                continue
            if ("x", h) in block:
                self._apply(r["id"], REJECT, "в блоклисте", {"handle": r["handle"]})
                _bump(self.summary, "x", REJECT)
                continue
            if self.offline or budget is None or checked >= budget:
                # TODO(debt-D-61): бюджет сессии X (2 запроса на аккаунт,
                # пауза 10 с, 800 запросов/сутки) не покрывает всю очередь —
                # непроверенные честно уходят в hold, см. TECH-DEBT.md.
                self._apply(r["id"], HOLD,
                            "X: подписчики/посты не проверены (API закрыт, "
                            "бюджет сессии X исчерпан)", {"handle": r["handle"]})
                _bump(self.summary, "x", HOLD)
                continue
            try:
                res = self.fetchers.x(r["handle"])
            except Exception as exc:  # noqa: BLE001
                res = {"outcome": "fetch_error", "note": str(exc)[:200]}
            checked += 1
            outcome = res.get("outcome")
            posts30 = int(res.get("posts30") or 0)
            med = res.get("median_likes")
            ev = {"subs": res.get("subs"), "posts30": posts30,
                  "median_likes": med, "note": res.get("note")}
            if outcome == "gone":
                self._apply(r["id"], REJECT, "аккаунт не найден", ev)
                _bump(self.summary, "x", REJECT)
            elif outcome == "protected":
                self._apply(r["id"], REJECT, "закрытый аккаунт", ev)
                _bump(self.summary, "x", REJECT)
            elif outcome in ("budget", "fetch_error", None):
                self._apply(r["id"], HOLD,
                            res.get("note") or "X: данные не получены", ev)
                _bump(self.summary, "x", HOLD)
            elif posts30 == 0:
                self._apply(r["id"], REJECT, "нет постов за 30 дней", ev)
                _bump(self.summary, "x", REJECT)
            elif posts30 < MIN_POSTS_30D:
                self._apply(r["id"], HOLD,
                            f"мало постов за 30 дней ({posts30} < {MIN_POSTS_30D})", ev)
                _bump(self.summary, "x", HOLD)
            elif med is None or med < X_MEDIAN_LIKES_MIN:
                self._apply(r["id"], HOLD,
                            f"медиана лайков ниже порога ({med} < {X_MEDIAN_LIKES_MIN:g})",
                            ev)
                _bump(self.summary, "x", HOLD)
            else:
                self._apply(r["id"], PROMOTE,
                            f"постов за 30 дней {posts30}, медиана лайков {med:g}", ev)
                _bump(self.summary, "x", PROMOTE)

    def _x_budget(self):
        if self.fetchers is None:
            return None
        return getattr(self.fetchers, "x_budget", None)

    def _offline_hold(self, rows, platform, reason):
        for r in rows:
            self._apply(r["id"], HOLD, reason, {})
            _bump(self.summary, platform, HOLD)

    # -- оркестрация -------------------------------------------------------
    def run(self) -> dict:
        for platform in self.platforms:
            rows = self._candidates(platform)
            self.summary.setdefault(platform, {})
            if platform == "web":
                self.review_web(rows)
            elif platform == "youtube":
                self.review_youtube(rows)
            elif platform == "telegram":
                self.review_telegram(rows)
            elif platform == "x":
                self.review_x(rows)
        self.con.commit()
        return {"by_platform": self.summary, "llm_calls": self.llm_calls,
                "offline": self.offline, "limit": self.limit}


def _bump(summary: dict, platform: str, verdict: str) -> None:
    bucket = summary.setdefault(platform, {})
    bucket[verdict] = bucket.get(verdict, 0) + 1


# ---------------------------------------------------------------------------
# Миля повышения
# ---------------------------------------------------------------------------

def promote(con: sqlite3.Connection, *, dry_run: bool = False, limit: int | None = None,
            collect_first: bool = False, db_path: str | None = None,
            run_id: int | None = None, now: datetime | None = None) -> dict:
    """Перевести вердикт ``promote`` в активный сбор (идемпотентно)."""
    now = now or _now()
    rows = list(con.execute(
        "SELECT id, platform, handle, external_id, meta_json FROM candidate "
        "WHERE status='promoted' AND promoted_at IS NULL ORDER BY platform, id"))
    if limit is not None:
        rows = rows[: limit]
    summary = {"checked": len(rows), "promoted": 0, "active": 0, "provisional": 0,
               "by_platform": {}, "dry_run": bool(dry_run),
               "promoted_handles": [], "seeded": 0}
    # В машинном выводе не печатаем сотни хэндлов: первые 50 + общий счёт.
    HANDLES_IN_SUMMARY = 50
    for r in rows:
        platform = r["platform"]
        if platform == "web":
            continue  # web не повышается (нет сборщика)
        evidence = _review_evidence(r["meta_json"])
        if evidence is None:
            continue  # без свидетельства повышать нечем (страховка)
        status = "active" if _is_active(platform, evidence) else "provisional"
        summary["by_platform"][platform] = summary["by_platform"].get(platform, 0) + 1
        summary["promoted"] += 1
        summary[status] += 1
        if len(summary["promoted_handles"]) < HANDLES_IN_SUMMARY:
            summary["promoted_handles"].append((platform, r["handle"]))
        if dry_run:
            continue
        handle, external_id = _source_identity(platform, r["handle"], r["external_id"],
                                               evidence)
        with core_db.write_tx(con):
            # ТЗ-51 (D-55): очередь и реестр могут хранить один канал в разной
            # форме (`@X` / `t.me/x` / `X`). Перед upsert ищем существующую
            # строку реестра по каноническому хендлу/tg_id и переиспользуем ЕЁ
            # хендл, иначе UNIQUE(platform, handle) завёл бы дубликат.
            handle, external_id = _reuse_registry_identity(
                con, platform, handle, external_id, r)
            storage.upsert_source(
                con, platform, handle,
                external_id=external_id,
                subs=evidence.get("subs"),
                title=evidence.get("title"),
                status=status,
                source_kind="queue_promote",
                added_at=_iso(now),
                added_by=PROMOTE_SOURCE,
            )
            storage.promote_candidate(
                con, platform, r["handle"], status="promoted",
                promoted_by=PROMOTE_SOURCE, promoted_at=_iso(now))
        if run_id is not None:
            storage.log_run(
                con, run_id, now, "INFO", None,
                f"queue promote {platform}/{handle} -> {status} "
                f"subs={evidence.get('subs')} posts30={evidence.get('posts30', evidence.get('videos30'))}",
                platform="cross")
    if not dry_run:
        con.commit()
    if collect_first and not dry_run:
        summary["seeded"] = _seed_promoted(con, db_path, rows, summary, now)
        con.commit()
    return summary


def _review_evidence(meta_json) -> dict | None:
    if not meta_json:
        return None
    try:
        meta = json.loads(meta_json)
    except (TypeError, ValueError):
        return None
    review = (meta or {}).get("review") or {}
    if review.get("verdict") != PROMOTE:
        return None
    return review.get("evidence") or {}


def _is_active(platform: str, evidence: dict) -> bool:
    subs = evidence.get("subs")
    if subs is None or subs < ACTIVE_SUBS_MIN:
        return False
    if platform == "x" and evidence.get("median_likes") is None:
        return False
    return True


def _source_identity(platform: str, handle: str, external_id, evidence: dict):
    if platform == "youtube":
        cid = external_id or handle
        custom = evidence.get("custom_url")
        nice = (custom or "").lstrip("@").strip()
        return (nice or handle or cid), cid
    if platform == "x":
        return handle, external_id
    return handle, external_id


def _reuse_registry_identity(con, platform, handle, external_id, candidate_row):
    """Хендл/``external_id`` существующей строки реестра (ТЗ-51, D-55).

    Очередь и реестр могут хранить один канал по-разному (``@X`` / ``t.me/x`` /
    ``X``). Ищем существующую строку реестра по каноническому хендлу и ``tg_id``
    и, если нашли, возвращаем её идентичность: upsert обновит ту же строку, а не
    создаст дубликат под другим регистром/формой.
    """
    if platform != "telegram":
        return handle, external_id
    from tuber.platforms.telegram import linking as tg_linking

    tg_id = tg_linking.tg_id_of(candidate_row)
    match = tg_linking.find_source(con, handle=handle or candidate_row["handle"],
                                   tg_id=tg_id, platform=platform)
    if match is None:
        return handle, external_id
    row = con.execute(
        "SELECT handle, external_id FROM source WHERE id=?", (match,)).fetchone()
    if row is None:
        return handle, external_id
    return (row["handle"] or handle,
            external_id if external_id is not None else row["external_id"])


# ---------------------------------------------------------------------------
# Первый сбор для повышенных (чтобы «сливки» сразу видели автора)
# ---------------------------------------------------------------------------

def _seed_promoted(con, db_path, rows, summary, now) -> int:
    """Первый батч постов повышенных источников (бесплатные страницы)."""
    from collections import defaultdict

    by_platform: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["platform"] in ("telegram", "youtube"):
            by_platform[r["platform"]].append(r["handle"])
    seeded = 0
    if by_platform.get("telegram"):
        seeded += _seed_telegram(con, db_path, by_platform["telegram"])
    if by_platform.get("youtube"):
        seeded += _seed_youtube(con, by_platform["youtube"])
    return seeded


def _seed_telegram(con, db_path, handles) -> int:
    """Первый сбор Telegram для повышенных каналов (переиспользуем Collector)."""
    if db_path is None:
        return 0
    from tuber.platforms.telegram import collect as tg_collect

    col = tg_collect.Collector(db_path=db_path, mode="web", handle=None)
    col.connect()
    col.client = tg_collect.make_client()
    n = 0
    try:
        for handle in handles:
            row = col.con.execute("SELECT * FROM channels WHERE handle=?",
                                  (handle,)).fetchone()
            if row is None:
                continue
            try:
                col.collect_channel_web(row)
                n += 1
            except Exception:  # noqa: BLE001 — посев не роняет повышение
                continue
    finally:
        for closer in (getattr(col, "client", None), col.con):
            close = getattr(closer, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
    return n


def _seed_youtube(con, handles) -> int:
    """Первый сбор YouTube: свежие видео + снимки метрик."""
    from tuber.platforms.youtube import api as yt_api
    from tuber.platforms.youtube import store as yt_store

    try:
        yt_store.install_compat(con)
    except Exception:  # noqa: BLE001
        pass
    client = yt_api.YouTubeClient(conn=con, min_interval=0.1)
    ids = [h for h in handles if h and h.startswith("UC")]
    if not ids:
        return 0
    items = client.channels_by_ids(ids)
    by_id = {it.get("id"): it for it in items}
    seeded = 0
    captured = int(time.time())
    for cid in ids:
        it = by_id.get(cid)
        if it is None:
            continue
        sn = it.get("snippet") or {}
        st = it.get("statistics") or {}
        cd = it.get("contentDetails") or {}
        try:
            subs = None if str(st.get("hiddenSubscriberCount", "false")).lower() == "true" \
                else int(st.get("subscriberCount"))
        except (TypeError, ValueError):
            subs = None
        yt_store.upsert_channel(con, {
            "channel_id": cid, "title": sn.get("title"),
            "handle": sn.get("customUrl"), "subscriber_count": subs,
            "video_count": _to_int(st.get("videoCount")),
            "view_count": _to_int(st.get("viewCount")),
            "country": sn.get("country"),
            "default_language": sn.get("defaultLanguage"),
            "uploads_playlist_id": (cd.get("relatedPlaylists") or {}).get("uploads"),
        })
        uploads = (cd.get("relatedPlaylists") or {}).get("uploads")
        if not uploads:
            continue
        try:
            pl = client.playlist_items(uploads, max_results=15)
        except Exception:  # noqa: BLE001
            continue
        vids = [(((v.get("contentDetails") or {}).get("videoId"))
                 or ((v.get("snippet") or {}).get("resourceId") or {}).get("videoId"))
                for v in pl]
        vids = [v for v in vids if v]
        if not vids:
            continue
        try:
            details = client.videos_by_ids(vids)
        except Exception:  # noqa: BLE001
            details = []
        for d in details:
            sn2 = d.get("snippet") or {}
            st2 = d.get("statistics") or {}
            try:
                yt_store.upsert_video(con, {
                    "video_id": d.get("id"), "channel_id": cid,
                    "title": sn2.get("title"), "description": sn2.get("description"),
                    "published_at": sn2.get("publishedAt"),
                    "category_id": sn2.get("categoryId"),
                })
            except Exception:  # noqa: BLE001
                continue
            cid_row = con.execute(
                "SELECT id FROM content WHERE platform='youtube' AND external_id=?",
                (d.get("id"),)).fetchone()
            if cid_row is None:
                continue
            storage.add_snapshot(
                con, cid_row[0], _iso(datetime.fromtimestamp(captured, timezone.utc)),
                bucket="queue-seed", views=_to_int(st2.get("viewCount")),
                likes=_to_int(st2.get("likeCount")),
                comments=_to_int(st2.get("commentCount")),
                source=PROMOTE_SOURCE)
        seeded += 1
    return seeded


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Отчёт о разборе
# ---------------------------------------------------------------------------

def report(con: sqlite3.Connection, *, top: int = 10) -> dict:
    """Таблица вердиктов по платформам + примеры повышенных с числами.

    Считаются ТОЛЬКО вердикты нашего разбора (``meta_json.review``), чтобы
    предсуществующие терминальные статусы YouTube (``accepted``/``dropped``/
    ``unresolved``/``rejected``) не смешивались с очередью ``new``.
    """
    rows = list(con.execute(
        "SELECT platform, json_extract(meta_json,'$.review.verdict') AS v, "
        "COUNT(*) AS n FROM candidate "
        "WHERE json_extract(meta_json,'$.review.by')='queue_review' "
        "GROUP BY 1,2 ORDER BY 1,2"))
    by_platform: dict[str, dict[str, int]] = {}
    for r in rows:
        by_platform.setdefault(r["platform"], {})[r["v"]] = r["n"]
    # Остаток очереди без вердикта.
    remaining = list(con.execute(
        "SELECT platform, COUNT(*) AS n FROM candidate WHERE status='new' GROUP BY 1"))
    promoted = _promoted_examples(con, top=top)
    return {"generated_at": _iso(_now()), "by_platform": by_platform,
            "status_by_platform": rows, "remaining_new": {r["platform"]: r["n"]
                                                          for r in remaining},
            "promoted_examples": promoted}


def _promoted_examples(con, *, top: int) -> list[dict]:
    from tuber.analysis import slivki

    data = slivki.build(con, limit=100_000)
    promoted_ids = {r[0] for r in con.execute(
        "SELECT id FROM source WHERE added_by=?", (PROMOTE_SOURCE,))}
    out = []
    for rec in data["candidates_verified"] + data["candidates_growth_unknown"]:
        if rec["source_id"] in promoted_ids:
            row = con.execute(
                "SELECT COUNT(*) FROM content WHERE source_id=? "
                "AND published_at >= datetime('now','-14 day')",
                (rec["source_id"],)).fetchone()
            rec = dict(rec)
            rec["posts_14d"] = row[0] if row else 0
            out.append(rec)
    return out[:top]


def format_report(data: dict, *, top: int = 10) -> str:
    lines = ["=== Разбор очереди кандидатов (ТЗ-46) ===",
             f"на: {data['generated_at']}", "", "вердикты по платформам:"]
    lines.append(f"  {'платформа':<10} {'promote':>8} {'hold':>8} {'reject':>8} "
                 f"{'всего':>8} {'new!':>8}")
    total = {PROMOTE: 0, HOLD: 0, REJECT: 0}
    for p in sorted(data["by_platform"]):
        b = data["by_platform"][p]
        for k in total:
            total[k] += b.get(k, 0)
        lines.append(f"  {p:<10} {b.get(PROMOTE, 0):>8} {b.get(HOLD, 0):>8} "
                     f"{b.get(REJECT, 0):>8} {sum(b.values()):>8} "
                     f"{data['remaining_new'].get(p, 0):>8}")
    lines.append(f"  {'ИТОГО':<10} {total[PROMOTE]:>8} {total[HOLD]:>8} "
                 f"{total[REJECT]:>8} {sum(total.values()):>8} "
                 f"{sum(data['remaining_new'].values()):>8}")
    lines.append("")
    lines.append(f"-- примеры повышенных авторов в «сливках» (топ {top}) --")
    if not data["promoted_examples"]:
        lines.append("  (нет)")
    for r in data["promoted_examples"]:
        v = r.get("viral_post") or {}
        lines.append(
            f"  {r['handle']} ({r['platform']}, {r['subs']} подписчиков, "
            f"постов/14д {r.get('posts_14d', '?')}): "
            f"выброс ×{v.get('outlier')}, {v.get('url') or 'нет ссылки'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _db_path(args) -> str:
    return args.db or os.environ.get("TUBER_DB") or config.db_path()


def _is_production(path: str) -> bool:
    try:
        return os.path.realpath(str(path)) == os.path.realpath(str(config.DEFAULT_DB_PATH))
    except OSError:
        return False


def _connect(path: str):
    from tuber.core import schema

    con = core_db.connect(path)
    schema.migrate_schema(con)
    return con


def _gate(args, path: str) -> bool:
    """True — писать нельзя (отказ)."""
    return (not getattr(args, "dry_run", False) and _is_production(path)
            and not args.allow_production)


def cmd_review(args) -> int:
    path = _db_path(args)
    if _gate(args, path):
        print("отказ: запись в боевую базу без --allow-production "
              "(приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        return 2
    con = _connect(path)
    try:
        fetchers = None
        if not args.offline:
            fetchers = NetworkFetchers(
                con=con, db_path=path, x_budget=args.x_budget,
                time_cap_sec=args.time_cap_sec, workers=args.workers)
        else:
            fetchers = NetworkFetchers(con=con, db_path=path, x_budget=0,
                                       time_cap_sec=args.time_cap_sec,
                                       workers=args.workers)
        run_id = None
        if not args.dry_run:
            run_id = _start_run(con)
        rev = QueueReviewer(
            con, fetchers=fetchers, offline=args.offline, limit=args.limit,
            platforms=args.platform or None, run_id=run_id,
            llm_budget_usd=args.llm_budget_usd)
        if args.dry_run:
            # Сухой прогон: считаем вердикты без записи.
            summary = _dry_review(rev)
        else:
            summary = rev.run()
        if run_id is not None:
            _finish_run(con, run_id, summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        con.close()


def _dry_review(rev: QueueReviewer) -> dict:
    """Сухой прогон: решения принимаются, в базу не пишется."""
    counts: dict[str, dict[str, int]] = {}
    original = rev._apply

    def spy(cand_id, verdict, reason, evidence):
        platform = None
        row = rev.con.execute("SELECT platform FROM candidate WHERE id=?",
                              (cand_id,)).fetchone()
        platform = row["platform"] if row else "?"
        _bump(counts, platform, verdict)

    rev._apply = spy  # type: ignore[assignment]
    try:
        result = rev.run()
    finally:
        rev._apply = original  # type: ignore[assignment]
        rev.con.rollback()
    result["by_platform"] = counts
    result["dry_run"] = True
    return result


def _start_run(con) -> int:
    return storage.add_run(con, "cross", mode="queue-review",
                           started_at=_iso(_now()))


def _finish_run(con, run_id: int, summary: dict) -> None:
    total_promote = sum(b.get(PROMOTE, 0) for b in summary["by_platform"].values())
    total_reject = sum(b.get(REJECT, 0) for b in summary["by_platform"].values())
    total_hold = sum(b.get(HOLD, 0) for b in summary["by_platform"].values())
    con.execute(
        "UPDATE run SET finished_at=?, ok_count=?, fail_count=?, note=? WHERE id=?",
        (_iso(_now()), total_promote, total_reject + total_hold,
         f"queue review: promote={total_promote} hold={total_hold} "
         f"reject={total_reject} llm={summary.get('llm_calls', 0)}", run_id))
    storage.log_run(con, run_id, _iso(_now()), "INFO", None,
                    f"queue review: promote={total_promote} hold={total_hold} "
                    f"reject={total_reject}", platform="cross")
    con.commit()


def cmd_promote(args) -> int:
    path = _db_path(args)
    if _gate(args, path):
        print("отказ: запись в боевую базу без --allow-production "
              "(приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        return 2
    con = _connect(path)
    try:
        run_id = None if args.dry_run else storage.add_run(
            con, "cross", mode="queue-promote", started_at=_iso(_now()))
        summary = promote(con, dry_run=args.dry_run, limit=args.limit,
                          collect_first=args.collect_first, db_path=path, run_id=run_id)
        if run_id is not None:
            con.execute(
                "UPDATE run SET finished_at=?, ok_count=?, fail_count=0, note=? "
                "WHERE id=?",
                (_iso(_now()), summary["promoted"], "queue promote", run_id))
            con.commit()
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        con.close()


def cmd_report(args) -> int:
    con = _connect(_db_path(args))
    try:
        data = report(con, top=args.top or 10)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        else:
            print(format_report(data, top=args.top or 10))
        return 0
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tuber queue",
                                 description="Разбор очереди кандидатов (ТЗ-46)")
    ap.add_argument("--db", default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")

    gate = argparse.ArgumentParser(add_help=False)
    gate.add_argument("--dry-run", action="store_true", help="без записи в БД")
    gate.add_argument("--allow-production", action="store_true",
                      help="разрешить запись в боевую базу")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="путь к базе (иначе TUBER_DB)")

    sp = sub.add_parser("review", parents=[common, gate],
                        help="разобрать очередь: вердикт+причина каждому кандидату")
    sp.add_argument("--platform", action="append", default=None,
                    help="только эта платформа (можно несколько раз)")
    sp.add_argument("--limit", type=int, default=None,
                    help="не больше N кандидатов на платформу (отладка)")
    sp.add_argument("--offline", action="store_true",
                    help="без сети: неразобранное уходит в hold")
    sp.add_argument("--time-cap-sec", type=float, default=DEFAULT_TIME_CAP_SEC,
                    help="потолок времени прогона, с")
    sp.add_argument("--workers", type=int, default=4, help="параллельных запросов")
    sp.add_argument("--x-budget", type=int, default=None,
                    help="сколько X-аккаунтов проверить сетью за прогон")
    sp.add_argument("--llm-budget-usd", type=float, default=DEFAULT_LLM_BUDGET_USD,
                    help="потолок LLM-расхода на разбор, USD")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("promote", parents=[common, gate],
                        help="перевести вердикт promote в активный сбор")
    sp.add_argument("--limit", type=int, default=None, help="не больше N повышений")
    sp.add_argument("--collect", dest="collect_first", action="store_true",
                    default=True, help="первый сбор постов для повышенных")
    sp.add_argument("--no-collect", dest="collect_first", action="store_false",
                    help="не собирать посты, только активировать")
    sp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("report", parents=[common],
                        help="таблица вердиктов и примеры повышенных")
    sp.add_argument("--json", action="store_true", help="машинный вывод")
    sp.add_argument("--top", type=int, default=10, help="сколько примеров")
    sp.set_defaults(func=cmd_report)

    ap.set_defaults(func=None)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "queue":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
