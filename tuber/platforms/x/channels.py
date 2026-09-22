"""Единый роутер каналов сбора (ТЗ-4 Р2.1).

Обязан быть единственным владельцем квоты ВСЕХ каналов (расширение роли
`broker.py` из ТЗ-1). Никакой другой модуль проекта не ходит в сеть
напрямую: коллектор, обогащение, скоринг и CLI вызывают эти объекты.

Каналы (матрица Р1):

| канал            | что делает                                   | темп                    |
|------------------|----------------------------------------------|-------------------------|
| `nitter_rss`     | основной сбор (ТЗ-1, брокер Nitter)          | свой лимитер ТЗ-1       |
| `nitter_search`  | дискавери (ТЗ-2)                             | свой лимитер ТЗ-1       |
| `cdn_tweet`      | ОБОГАЩЕНИЕ метриками (лайки, ответы, время)  | 2 зап/с, пауза 0,35 с   |
| `synd_timeline`  | премиальная лента, РАЗОВАЯ (репосты)         | бюджет 5/сутки, 5/окно  |
| `x_ssr`          | аварийный дублёр при деградации Nitter       | 1 зап/с                 |

Изоляция транспорта: настоящий сетевой вызов живёт только в `broker.py`
(`raw_http_get`). Здесь — только разбор ответов, лимитеры и запись в `requests`.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

from . import config, store as db
from .broker import NitterBroker, RateLimiter, raw_http_get, raw_http_post

CHANNELS = ("nitter_rss", "nitter_search", "cdn_tweet", "synd_timeline", "x_ssr",
            "deepseek")

# Соответствие имён каналов и значения requests.kind. Nitter-каналы сохраняют
# исторические имена ТЗ-1/ТЗ-2: на `search` завязан суточный бюджет дискавери
# (`discover.search_requests_today`), переименование сломало бы обратную
# совместимость (ТЗ-4 условие 7). Новые каналы пишутся под своим именем.
CHANNEL_KIND = {
    "nitter_rss": ("feed", "backfill"),
    "nitter_search": ("search",),
    "cdn_tweet": ("cdn_tweet",),
    "synd_timeline": ("synd_timeline",),
    "x_ssr": ("x_ssr",),
    "deepseek": ("deepseek",),
}


# --------------------------------------------------------------------- ошибки
class ChannelError(RuntimeError):
    """Общая ошибка канала."""


class CdnError(ChannelError):
    """Канал cdn_tweet (метрики)."""


class SyndError(ChannelError):
    """Канал synd_timeline (премиальная лента)."""


class XssrError(ChannelError):
    """Канал x_ssr (аварийный дублёр)."""


class QuotaExceeded(ChannelError):
    """Канал упёрся в бюджет окна/суток: не повторять в этом окне."""


# ------------------------------------------------------------------- хелперы
def _iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _record_request(con, host, kind, url, status, items, latency_ms, run_id=None):
    """Все запросы (успех и отказ) — в таблицу `requests` (ТЗ-4 2.1)."""
    try:
        con.execute(
            "INSERT INTO requests (host, ts, kind, url, status, items, latency_ms, run_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (host, _iso_now(), kind, url, status, items, latency_ms, run_id))
        con.commit()
    except Exception:
        pass


# ============================================================ канал cdn_tweet
def classify_cdn_response(status, body):
    """Строгое различение кодов `tweet-result` (ТЗ-4 питфолл 7.9).

    Возвращает один из кодов:
      ok          — 200 с полями поста;
      deleted     — 200 + `__typename=Tombstone` (пост удалён/недоступен);
      not_found   — 404 (ID не найден);
      invalid     — 400 (ID вне диапазона/битой длины);
      rate_limited— 429;
      network     — 0 (сбой транспорта);
      error       — прочий код.
    Ни `invalid`, ни `not_found`, ни `deleted` повторять нельзя.
    """
    if status == 0:
        return "network"
    if status == 400:
        return "invalid"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status != 200:
        return "error"
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "error"
    if not isinstance(data, dict):
        return "error"
    if str(data.get("__typename") or "").lower() == "tombstone":
        return "deleted"
    if not data.get("created_at"):
        return "deleted"
    return "ok"


def parse_tweet_result(payload, tweet_id=None):
    """Разбор JSON `tweet-result` в поля для БД (ТЗ-4 2.2).

    Метрики принадлежат оригиналу: ретвиты приходят с favorite_count=0
    (питфолл 7.4), поэтому `is_retweet` определяется вызывающим кодом по
    `posts.is_retweet`, а не здесь.
    """
    if isinstance(payload, str):
        payload = json.loads(payload)
    user = payload.get("user") or {}
    text = payload.get("text")
    note = payload.get("note_tweet")
    if not text and isinstance(note, dict):
        text = (note.get("text") or note.get("full_text"))
    quoted = payload.get("quoted_tweet") or None
    media_kind = None
    if payload.get("video") or payload.get("videos"):
        media_kind = "video"
    elif payload.get("photos") or payload.get("mediaDetails"):
        media_kind = "photo"
    verified = bool(user.get("verified") or user.get("is_blue_verified")
                    or user.get("verified_type"))
    return {
        "tweet_id": str(payload.get("id_str") or tweet_id or ""),
        "likes": payload.get("favorite_count"),
        "replies": payload.get("conversation_count"),
        "created_at": _iso(_parse_iso(payload.get("created_at"))),
        "lang": payload.get("lang"),
        "text": text,
        "screen_name": user.get("screen_name"),
        "is_long": 1 if note else 0,
        "has_quote": 1 if quoted else 0,
        "quoted_author": ((quoted.get("user") or {}).get("screen_name")
                          if isinstance(quoted, dict) else None),
        "quoted_likes": (quoted.get("favorite_count") if isinstance(quoted, dict) else None),
        "author_verified": 1 if verified else 0,
        "is_edited": bool(payload.get("isEdited")),
        "media_kind": media_kind,
    }


class CdnTweetBroker:
    """Канал `cdn_tweet`: безлимитный по факту, но со своим темпом и backoff.

    Отдельный от `broker.py`: другой хост, другие лимиты, другие ошибки.
    """

    KIND = "cdn_tweet"
    HOST = config.CDN_HOST

    def __init__(self, *, db_path=None, transport=None, clock=time.monotonic,
                 sleeper=time.sleep, run_id=None, lang="en"):
        self._con = db.connect(db_path or config.DB_PATH, check_same_thread=False)
        self._transport = transport or self._default_transport
        self._clock = clock
        self._sleeper = sleeper
        self.run_id = run_id
        self.lang = lang
        self._rl = RateLimiter(config.CDN_RATE_MAX, config.CDN_RATE_WINDOW_SEC,
                               config.CDN_MIN_INTERVAL_SEC, clock)
        self.requests_429 = 0

    @staticmethod
    def _default_transport(url, headers):
        return raw_http_get(url, None, headers=headers)

    def build_url(self, tweet_id, lang=None):
        q = f"?id={tweet_id}&lang={lang or self.lang}&token=x"
        return f"https://{self.HOST}{config.CDN_PATH}{q}"

    def fetch(self, tweet_id):
        """Один пост: (status_code, fields|None). Backoff на 429 — 60/120/240 с."""
        tid = str(tweet_id)
        if not re.match(r"^\d+$", tid):
            return "invalid", None, None
        url = self.build_url(tid)
        headers = {
            "User-Agent": config.CDN_USER_AGENT,
            "Accept": "application/json",
        }
        pause = config.CDN_429_PAUSE_SEC
        for attempt in range(config.CDN_MAX_RETRIES + 1):
            d = self._rl.next_delay()
            if d > 0:
                self._sleeper(d)
            t0 = self._clock()
            status, _hdrs, body = self._transport(url, headers)
            latency_ms = int(max(0.0, self._clock() - t0) * 1000)
            self._rl.record()
            kind = classify_cdn_response(status, body)
            items = 1 if kind == "ok" else 0
            if status == 429:
                self.requests_429 += 1
            _record_request(self._con, self.HOST, self.KIND, url, status, items,
                            latency_ms, self.run_id)
            if status == 429 and attempt < config.CDN_MAX_RETRIES:
                self._sleeper(pause)
                pause *= 2
                continue
            if kind == "ok":
                return "ok", parse_tweet_result(body, tid), status
            return kind, None, status
        return "rate_limited", None, 429

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass


# ======================================================== канал synd_timeline
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I)
_RT_RE = re.compile(r"^RT @([A-Za-z0-9_]{1,15}):", re.I)


def parse_timeline_html(html_text):
    """Разбор `__NEXT_DATA__` ленты (ТЗ-4 2.3).

    Путь: `props.pageProps.timeline.entries[]` -> `entry['content']['tweet']`.
    Первая запись — закреплённый пост (`pinned=1`), он может быть старым.
    Ретвиты (`full_text` начинается с `RT @`) помечаются `is_retweet=1`.
    """
    m = _NEXT_DATA_RE.search(html_text or "")
    if not m:
        raise SyndError("__NEXT_DATA__ не найден в ответе ленты")
    try:
        data = json.loads(m.group(1))
    except ValueError as e:
        raise SyndError(f"__NEXT_DATA__ не разобрался: {e}")
    try:
        entries = data["props"]["pageProps"]["timeline"]["entries"]
    except (KeyError, TypeError):
        raise SyndError("нет props.pageProps.timeline.entries")
    out = []
    for idx, entry in enumerate(entries or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("type") and entry.get("type") != "tweet":
            continue
        content = entry.get("content") or {}
        tw = content.get("tweet") if isinstance(content, dict) else None
        if not isinstance(tw, dict):
            continue
        full = tw.get("full_text") or tw.get("text") or ""
        user = tw.get("user") or {}
        out.append({
            "tweet_id": str(tw.get("id_str") or tw.get("id") or ""),
            "text": full,
            "published_at_utc": _iso(_parse_iso(tw.get("created_at"))),
            "likes": tw.get("favorite_count"),
            "retweet_count": tw.get("retweet_count"),
            "replies": tw.get("reply_count"),
            "lang": tw.get("lang"),
            "owner_handle": user.get("screen_name"),
            "is_retweet": 1 if _RT_RE.match(full or "") else 0,
            "pinned": 1 if idx == 0 else 0,
        })
    return out


class SyndTimelineBroker:
    """Канал `synd_timeline`: РАЗОВЫЙ, не регулярный (ТЗ-4 2.3).

    Бюджет окна (5 запросов, затем пауза 20 мин) и суточный бюджет (5). При 429
    в этом окне не повторяем: пишем событие в `run_log` и уходим на Nitter.
    """

    KIND = "synd_timeline"
    HOST = config.SYND_HOST

    def __init__(self, *, db_path=None, transport=None, clock=time.monotonic,
                 sleeper=time.sleep, run_id=None, daily_budget=None,
                 window_budget=None, window_pause=None):
        self._con = db.connect(db_path or config.DB_PATH, check_same_thread=False)
        self._transport = transport or self._default_transport
        self._clock = clock
        self._sleeper = sleeper
        self.run_id = run_id
        self.daily_budget = (config.SYND_DAILY_BUDGET if daily_budget is None
                             else int(daily_budget))
        self.window_budget = (config.SYND_WINDOW_BUDGET if window_budget is None
                              else int(window_budget))
        self.window_pause = (config.SYND_WINDOW_PAUSE_SEC if window_pause is None
                             else float(window_pause))
        self._window_start = None
        self._window_count = 0
        self.requests_429 = 0
        self._load_state()

    @staticmethod
    def _default_transport(url, headers):
        return raw_http_get(url, None, headers=headers)

    def _today(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_state(self):
        row = self._con.execute("SELECT * FROM instances WHERE host=?",
                                (self.HOST,)).fetchone()
        if row is None:
            self._con.execute("INSERT OR IGNORE INTO instances (host, healthy)"
                              " VALUES (?, NULL)", (self.HOST,))
            self._con.commit()

    # --- бюджет -------------------------------------------------------------
    def _used_today(self):
        row = self._con.execute("SELECT requests_today, day FROM instances WHERE host=?",
                                (self.HOST,)).fetchone()
        if not row or row["day"] != self._today():
            return 0
        return row["requests_today"] or 0

    def _bump_used(self):
        day = self._today()
        row = self._con.execute("SELECT requests_today, day FROM instances WHERE host=?",
                                (self.HOST,)).fetchone()
        used = (row["requests_today"] or 0) if (row and row["day"] == day) else 0
        self._con.execute("UPDATE instances SET requests_today=?, day=? WHERE host=?",
                          (used + 1, day, self.HOST))
        self._con.commit()
        return used + 1

    def _set_cooldown(self, seconds):
        until = datetime.now(timezone.utc).timestamp() + max(0.0, seconds)
        iso = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self._con.execute("UPDATE instances SET cooldown_until=? WHERE host=?",
                          (iso, self.HOST))
        self._con.commit()

    def budget_ok(self):
        """Проверка бюджета без запроса. (ok, reason)."""
        if self._used_today() >= self.daily_budget:
            return False, f"суточный бюджет {self.daily_budget} исчерпан"
        if (self._window_count >= self.window_budget and self._window_start is not None
                and (self._clock() - self._window_start) < self.window_pause):
            left = int(self.window_pause - (self._clock() - self._window_start))
            return False, f"бюджет окна исчерпан, пауза ещё {left} с"
        return True, None

    def snapshot(self, handle):
        """Один запрос ленты. Возвращает список постов (2.3)."""
        handle = str(handle).lstrip("@")
        ok, reason = self.budget_ok()
        if not ok:
            self._log("WARN", f"synd_timeline: пропуск @{handle} — {reason}")
            raise QuotaExceeded(reason)
        url = f"https://{self.HOST}{config.SYND_PATH}{handle}"
        headers = {
            "User-Agent": config.CDN_USER_AGENT,
            "Referer": config.SYND_REFERER,   # обязателен (питфолл 7.1)
            "Accept": "text/html,application/xhtml+xml",
        }
        if self._window_start is None or (self._clock() - self._window_start) >= self.window_pause:
            self._window_start = self._clock()
            self._window_count = 0
        t0 = self._clock()
        status, _hdrs, body = self._transport(url, headers)
        latency_ms = int(max(0.0, self._clock() - t0) * 1000)
        self._window_count += 1
        self._bump_used()
        items = 0
        if status == 429:
            self.requests_429 += 1
            self._set_cooldown(self.window_pause)
            self._window_start = self._clock()   # окно сброшено на паузу
            self._window_count = self.window_budget
            self._log("WARN", f"synd_timeline 429 на @{handle}: бюджет окна закрыт"
                              f" на {int(self.window_pause)} с, переходим на Nitter")
        elif status == 200:
            try:
                posts = parse_timeline_html(body)
                items = len(posts)
            except SyndError as e:
                self._log("WARN", f"synd_timeline разбор @{handle}: {e}")
                posts = []
        else:
            posts = []
            self._log("WARN", f"synd_timeline HTTP {status} на @{handle}")
        _record_request(self._con, self.HOST, self.KIND, url, status, items,
                        latency_ms, self.run_id)
        if status == 429:
            raise QuotaExceeded(f"429 на synd_timeline (@{handle})")
        if status != 200:
            raise SyndError(f"HTTP {status} на synd_timeline (@{handle})")
        return posts

    def _log(self, level, msg):
        try:
            db.log_run(self._con, level, msg, run_id=self.run_id)
            self._con.commit()
        except Exception:
            pass

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass


def timeline_snapshot(handle, **kwargs):
    """`channels.py::timeline_snapshot(handle)` из ТЗ-4 2.3.

    Разовая уточняющая задача: не более 5 аккаунтов в сутки. Никогда не
    вызывать из регулярного расписания.
    """
    b = SyndTimelineBroker(**kwargs)
    try:
        return b.snapshot(handle)
    finally:
        b.close()


# ============================================================== канал x_ssr
_STATUS_ID_RE = re.compile(r"/status/(\d{5,})")


def parse_ssr_profile(html_text, handle=None):
    """5–10 ID постов из SSR-страницы профиля (ТЗ-4 Р1).

    Канал отстаёт до 45 ч (питфолл 7.6) и потому годится только как дублёр:
    дата берётся из snowflake, текст не гарантирован.
    """
    from .broker import snowflake_to_datetime
    seen, out = set(), []
    for tid in _STATUS_ID_RE.findall(html_text or ""):
        if tid in seen:
            continue
        seen.add(tid)
        dt = snowflake_to_datetime(tid)
        out.append({
            "tweet_id": tid,
            "owner_handle": (handle or "").lstrip("@") or None,
            "published_at_utc": _iso(dt),
            "published_src": "snowflake",
            "text": None,
            "links": [], "mentions": [], "hashtags": [],
            "is_retweet": 0, "is_quote": 0, "is_reply": 0,
            "media_kind": None, "cursor_next": None,
        })
    return out


class XssrBroker:
    """Канал `x_ssr`: аварийный дублёр, только при деградации Nitter."""

    KIND = "x_ssr"
    HOST = config.XSSR_HOST

    def __init__(self, *, db_path=None, transport=None, clock=time.monotonic,
                 sleeper=time.sleep, run_id=None):
        self._con = db.connect(db_path or config.DB_PATH, check_same_thread=False)
        self._transport = transport or self._default_transport
        self._clock = clock
        self._sleeper = sleeper
        self.run_id = run_id
        self._rl = RateLimiter(config.XSSR_RATE_MAX, config.XSSR_RATE_WINDOW_SEC,
                               config.XSSR_MIN_INTERVAL_SEC, clock)
        self.requests_429 = 0

    @staticmethod
    def _default_transport(url, headers):
        return raw_http_get(url, None, headers=headers)

    def fetch_profile(self, handle):
        handle = str(handle).lstrip("@")
        url = f"https://{self.HOST}/{handle}"
        headers = {"User-Agent": config.CDN_USER_AGENT, "Accept": "text/html"}
        d = self._rl.next_delay()
        if d > 0:
            self._sleeper(d)
        # ТЗ-10 2.3: первое обращение к резерву фиксируем — сторож считает,
        # сколько времени Nitter лежит (резерв «активен»).
        try:
            db.mark_reserve_active(self._con)
        except Exception:
            pass
        t0 = self._clock()
        status, _hdrs, body = self._transport(url, headers)
        latency_ms = int(max(0.0, self._clock() - t0) * 1000)
        self._rl.record()
        if status == 429:
            self.requests_429 += 1
        posts = parse_ssr_profile(body, handle) if status == 200 else []
        _record_request(self._con, self.HOST, self.KIND, url, status, len(posts),
                        latency_ms, self.run_id)
        if status != 200:
            raise XssrError(f"HTTP {status} на x_ssr (@{handle})")
        return posts

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass


# ======================================================= канал DeepSeek (ТЗ-3 Р1)
class DeepSeekError(ChannelError):
    """Ошибка вызова модели (сеть, авторизация, невалидный ответ)."""


def deepseek_budget_status(script=None, halt_flag=None):
    """Проверка бюджета DeepSeek перед прогоном классификации (ТЗ-3 Р1.5).

    Возвращает (ok, reason, details). Читает только внешний бюджет-скрипт и
    флаг останова; никакой сети. При отсутствии скрипта бюджет считается
    неопределённым, но прогон НЕ блокируется (иначе окружение без Hermes
    было бы навсегда заперто).
    """
    script = script or config.BUDGET_SCRIPT
    halt_flag = halt_flag or config.BUDGET_HALT_FLAG
    import os as _os
    import re as _re
    import subprocess
    details = {"script": script, "today_usd": None, "daily_limit_usd": None,
               "halted": False}
    if halt_flag and _os.path.exists(halt_flag):
        details["halted"] = True
        try:
            with open(halt_flag, encoding="utf-8") as fh:
                details["halt_reason"] = fh.read().strip()[:200]
        except OSError:
            details["halt_reason"] = None
        return False, "установлен флаг останова DeepSeek " + str(details.get("halt_reason") or ""), details
    if not _os.path.exists(script):
        return True, "бюджет-скрипт не найден: лимит проверить нельзя", details
    try:
        proc = subprocess.run(["python3", script, "--report"], capture_output=True,
                              text=True, timeout=60)
    except Exception as e:  # noqa: BLE001 — прогон не должен падать из-за сторожа бюджета
        return True, f"бюджет-скрипт не запустился ({type(e).__name__})", details
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = _re.search(r"Сегодня:\s*\$([0-9.]+)", out)
    if m:
        details["today_usd"] = float(m.group(1))
    m = _re.search(r"дневной\s+([0-9.]+)\s*USD", out)
    if m:
        details["daily_limit_usd"] = float(m.group(1))
    if details["today_usd"] is not None and details["daily_limit_usd"] is not None:
        if details["today_usd"] >= details["daily_limit_usd"]:
            return False, (f"дневной бюджет DeepSeek исчерпан: "
                           f"{details['today_usd']:.2f} >= "
                           f"{details['daily_limit_usd']:.2f} USD"), details
    return True, None, details


def log_deepseek_usage(script, model, prompt_tokens=0, completion_tokens=0,
                       cache_read_tokens=0, cost_usd=None, path=None):
    """Запись расхода в общий журнал DeepSeek (P0-6). Никогда не бросает."""
    import json as _json
    import os as _os
    path = path or "/root/.hermes/logs/deepseek_usage.jsonl"
    try:
        if cost_usd is None:
            cost_usd = (prompt_tokens * config.DEEPSEEK_PRICE_IN
                        + completion_tokens * config.DEEPSEEK_PRICE_OUT
                        + cache_read_tokens * config.DEEPSEEK_PRICE_CACHE)
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "script": script, "model": model, "prompt_tokens": int(prompt_tokens),
               "completion_tokens": int(completion_tokens),
               "cache_read_tokens": int(cache_read_tokens),
               "cost_usd": round(float(cost_usd), 6)}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception:
        return None


def estimate_cost(prompt_tokens=0, completion_tokens=0, cache_read_tokens=0):
    return (prompt_tokens * config.DEEPSEEK_PRICE_IN
            + completion_tokens * config.DEEPSEEK_PRICE_OUT
            + cache_read_tokens * config.DEEPSEEK_PRICE_CACHE)


class DeepSeekBroker:
    """Канал `deepseek`: единственная точка вызова модели (ТЗ-3 Р1).

    Ключ берётся ТОЛЬКО из окружения (`DEEPSEEK_API_KEY`); в коде ключей нет.
    Модель — `deepseek-v4-flash`, `thinking:disabled`. Транспорт — общий
    `raw_http_post` брокера, поэтому инвариант «сеть только в транспорте»
    сохраняется.
    """

    KIND = "deepseek"
    HOST = "api.deepseek.com"

    def __init__(self, *, db_path=None, transport=None, clock=time.monotonic,
                 sleeper=time.sleep, run_id=None, api_key=None, model=None,
                 url=None):
        self._con = db.connect(db_path or config.DB_PATH, check_same_thread=False)
        self._transport = transport or self._default_transport
        self._clock = clock
        self._sleeper = sleeper
        self.run_id = run_id
        self.api_key = (api_key if api_key is not None
                        else os.environ.get(config.DEEPSEEK_API_KEY_ENV))
        self.model = model or config.DEEPSEEK_MODEL
        self.url = url or config.DEEPSEEK_URL
        self._last_call_at = None
        self.calls = 0
        self.failures = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_read_tokens = 0
        self.cost_usd = 0.0
        self.last_error = None

    @staticmethod
    def _default_transport(url, body, headers):
        status, _hdrs, text = raw_http_post(
            url, body, timeout=config.DEEPSEEK_TIMEOUT_SEC, headers=headers)
        return status, text

    def available(self):
        return bool(self.api_key)

    def budget_status(self):
        return deepseek_budget_status()

    def _throttle(self):
        if self._last_call_at is None:
            return
        wait = config.DEEPSEEK_MIN_INTERVAL_SEC - (self._clock() - self._last_call_at)
        if wait > 0:
            self._sleeper(wait)

    def classify(self, system, user, *, max_tokens=None, json_mode=True,
                 temperature=None):
        """Один вызов модели. Возвращает dict с content и usage.

        Бросает `DeepSeekError` при отсутствии ключа, ошибке канала или
        исчерпании повторов. Ретраи только на 429/5xx/сетевой сбой.
        `temperature` переопределяет базовую (нужно для повтора при невалидном
        ответе: при нулевой температуре повтор бессмыслен).
        """
        if not self.api_key:
            self.last_error = "нет ключа"
            raise DeepSeekError(
                f"нет ключа DeepSeek в окружении ({config.DEEPSEEK_API_KEY_ENV})")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": (config.DEEPSEEK_TEMPERATURE if temperature is None
                            else float(temperature)),
            "max_tokens": int(max_tokens or config.DEEPSEEK_MAX_TOKENS),
            # ТЗ-3: thinking:disabled — без длинных цепочек рассуждений.
            "thinking": config.DEEPSEEK_THINKING,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        payload = json.dumps(body, ensure_ascii=False)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last = None
        for attempt in range(config.CDN_MAX_RETRIES + 1):
            self._throttle()
            t0 = self._clock()
            try:
                status, text = self._transport(self.url, payload, headers)
            except Exception as e:  # noqa: BLE001
                status, text = 0, f"__transport_error__:{type(e).__name__}:{e}"
            latency_ms = int(max(0.0, self._clock() - t0) * 1000)
            self._last_call_at = self._clock()
            self.calls += 1
            _record_request(self._con, self.HOST, self.KIND, self.url, status,
                            1 if status == 200 else 0, latency_ms, self.run_id)
            if status == 200:
                try:
                    data = json.loads(text)
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage") or {}
                except (ValueError, KeyError, IndexError, TypeError) as e:
                    self.failures += 1
                    self.last_error = f"неразобранный ответ: {e}"
                    raise DeepSeekError(self.last_error)
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
                cr = int(usage.get("prompt_cache_hit_tokens") or 0)
                cost = estimate_cost(pt, ct, cr)
                self.prompt_tokens += pt
                self.completion_tokens += ct
                self.cache_read_tokens += cr
                self.cost_usd += cost
                log_deepseek_usage("tuber_x_classify", self.model, pt, ct, cr, cost)
                return {"content": content,
                        "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                                  "cache_read_tokens": cr, "cost_usd": cost}}
            last = f"HTTP {status}: {str(text)[:200]}"
            self.failures += 1
            if status in (429, 500, 502, 503, 504, 0) and attempt < config.CDN_MAX_RETRIES:
                self._sleeper(config.DEEPSEEK_RETRY_PAUSE_SEC * (2 ** attempt))
                continue
            break
        self.last_error = last
        raise DeepSeekError(last or "неизвестная ошибка канала DeepSeek")

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass


# ============================================================ роутер каналов
class ChannelRouter:
    """Единственный владелец квоты всех каналов (ТЗ-4 2.1).

    Коллектор, обогащение, скоринг и CLI получают каналы только отсюда.
    """

    def __init__(self, *, db_path=None, instances=None, clock=time.monotonic,
                 sleeper=time.sleep, run_id=None, lang="en",
                 nitter_transport=None, cdn_transport=None, synd_transport=None,
                 ssr_transport=None, deepseek_transport=None, deepseek_api_key=None):
        self._db_path = db_path or config.DB_PATH
        self._clock = clock
        self._sleeper = sleeper
        self.lang = lang
        self.nitter = NitterBroker(instances=instances, db_path=self._db_path,
                                   transport=nitter_transport, clock=clock,
                                   sleeper=sleeper, run_id=run_id)
        self.cdn = CdnTweetBroker(db_path=self._db_path, transport=cdn_transport,
                                  clock=clock, sleeper=sleeper, run_id=run_id,
                                  lang=lang)
        self.synd = SyndTimelineBroker(db_path=self._db_path, transport=synd_transport,
                                       clock=clock, sleeper=sleeper, run_id=run_id)
        self.ssr = XssrBroker(db_path=self._db_path, transport=ssr_transport,
                              clock=clock, sleeper=sleeper, run_id=run_id)
        self.deepseek = DeepSeekBroker(db_path=self._db_path,
                                       transport=deepseek_transport, clock=clock,
                                       sleeper=sleeper, run_id=run_id,
                                       api_key=deepseek_api_key)
        self.ssr_used = 0
        self.run_id = run_id

    # --- Nitter (основной сбор и дискавери) --------------------------------
    def set_run_id(self, run_id):
        """Прописать run_id во все каналы (для журнала `requests`/`run_log`)."""
        self.run_id = run_id
        for obj in (self.nitter, self.cdn, self.synd, self.ssr, self.deepseek):
            obj.run_id = run_id
        return run_id

    def fetch_feed(self, handle, cursor=None, priority="collect", force=False):
        return self.nitter.fetch_feed(handle, cursor=cursor, priority=priority,
                                      force=force)

    def fetch_search(self, query, cursor=None, priority="discover", force=False):
        return self.nitter.fetch_search(query, cursor=cursor, priority=priority,
                                        force=force)

    def nitter_degraded(self):
        """fail_streak >= 3 по всем инстансам Nitter -> включаем x_ssr (2.1)."""
        try:
            return self.nitter.all_degraded()
        except Exception:
            return False

    # --- CDN (обогащение) ---------------------------------------------------
    def enrich_tweet(self, tweet_id):
        return self.cdn.fetch(tweet_id)

    def cdn_429_count(self):
        return self.cdn.requests_429

    # --- syndication (разовое уточнение) -----------------------------------
    def timeline_snapshot(self, handle):
        return self.synd.snapshot(handle)

    def synd_429_count(self):
        return self.synd.requests_429

    # --- x.com SSR (аварийный дублёр) --------------------------------------
    def fetch_ssr(self, handle):
        posts = self.ssr.fetch_profile(handle)
        self.ssr_used += 1
        return posts

    # --- DeepSeek (классификация, ТЗ-3 Р1) ---------------------------------
    def classify_batch(self, system, user, **kwargs):
        return self.deepseek.classify(system, user, **kwargs)

    def deepseek_available(self):
        return self.deepseek.available()

    def close(self):
        for obj in (self.nitter, self.cdn, self.synd, self.ssr, self.deepseek):
            try:
                obj.close()
            except Exception:
                pass
