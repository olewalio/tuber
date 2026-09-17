"""Единственный владелец квоты Nitter (Р3).

Через этот модуль ходят в Nitter ВСЕ: и Tuber-x, и (после перевода)
CryptoGraph. Прямые `urlopen` вне этого файла запрещены — проверяется
тестом `test_no_direct_network`.

Что внутри:
  * пул инстансов из config.INSTANCES, health «отдал >= 15 item»;
  * лимитер на инстанс (по умолчанию 6 зап/60 с, >= 2 с между запросами);
  * суточный потолок расхода (18 000 запросов = 80% ёмкости) с исключением
    инстанса до конца суток и записью WARN в run_log;
  * cooldown при 429 / блокировка на сутки при 403/451;
  * кеш ответа 10 минут на URL;
  * приоритетная очередь: critical > collect > discover > backfill;
  * разбор RSS (Р3.9) — `parse_rss`.
"""
from __future__ import annotations

import heapq
import html
import gzip
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import config, store as db

# --------------------------------------------------------------------- ошибки
class NitterError(RuntimeError):
    """Общая ошибка брокера."""


class NoLiveInstance(NitterError):
    """Живых инстансов нет: не долбим сеть, возвращаем ошибку (Р3.5)."""


class NitterTransportError(NitterError):
    """Фактический транспортный отказ Nitter (ТЗ-10 2.1).

    Соединение отклонено/таймаут/DNS/5xx/обрыв чтения. Именно эти отказы
    увеличивают счётчик отказов сбора и дают право на аварийный резерв x_ssr.
    """


class NitterNotFound(NitterError):
    """404 «пост/лента не найдены» — это НЕ отказ Nitter (ТЗ-10 2.2/П4)."""


class EmptyFeedError(NitterError):
    """Лента пуста (0 item) — это НЕ отказ Nitter (ТЗ-10 2.2/П4)."""


# ------------------------------------------------------------------ snowflake
SNOWFLAKE_EPOCH_MS = 1288834974657  # 2010-11-04T01:42:54.657Z


def snowflake_to_datetime(tweet_id):
    """Восстановление времени публикации из snowflake-id (Р3.9)."""
    try:
        sid = int(tweet_id)
    except (TypeError, ValueError):
        return None
    if sid <= 0:
        return None
    ms = (sid >> 22) + SNOWFLAKE_EPOCH_MS
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt


def _parse_pubdate(raw):
    if not raw:
        return None
    txt = html.unescape(raw).strip()
    if not txt:
        return None
    try:
        dt = parsedate_to_datetime(txt)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_valid_published(dt, now=None):
    """Инвариант Р2: не пусто, не 1970-*, не будущее более чем на 2 часа."""
    if dt is None:
        return False
    now = now or datetime.now(timezone.utc)
    if dt.year < config.MIN_VALID_YEAR:
        return False
    if dt.year <= 1971:
        return False
    if dt > now + timedelta(seconds=config.FUTURE_TOLERANCE_SEC):
        return False
    return True


def normalize_published(tweet_id, raw_pubdate, now=None):
    """Вернуть (iso_utc|None, source) по правилу Р3.9 + инварианту Р2.

    source: 'rss' | 'snowflake' | 'unknown'.
    """
    dt = _parse_pubdate(raw_pubdate)
    if dt is not None and is_valid_published(dt, now=now):
        return dt.strftime("%Y-%m-%dT%H:%M:%S"), "rss"
    dt2 = snowflake_to_datetime(tweet_id)
    if is_valid_published(dt2, now=now):
        return dt2.strftime("%Y-%m-%dT%H:%M:%S"), "snowflake"
    return None, "unknown"


# --------------------------------------------------------------------- лимитер
class SharedSemaphore:
    """Вариант (б) ТЗ-4 5-БИС: общий файловый семафор расхода на инстанс.

    Записывает времена запросов в файл под flock, поэтому его может читать и
    боевой демон CryptoGraph: суммарный расход двух процессов не превысит
    замеренный предел инстанса. По умолчанию выключен
    (`config.SHARED_SEMAPHORE_PATH is None`); вариант (а) — закрепление
    инстанса за проектом — принят как основной.
    """

    def __init__(self, path=None, max_requests=None, window=None,
                 clock=time.time, sleeper=time.sleep):
        self.path = path or config.SHARED_SEMAPHORE_PATH
        self.max_requests = int(max_requests if max_requests is not None
                                else config.SHARED_SEMAPHORE_MAX)
        self.window = float(window if window is not None
                            else config.SHARED_SEMAPHORE_WINDOW)
        self._clock = clock
        self._sleeper = sleeper

    def _read(self, fh):
        try:
            data = fh.read() or "[]"
            return [float(x) for x in json.loads(data)]
        except (ValueError, TypeError):
            return []

    def next_delay(self):
        """Сколько ждать, пока в окне освободится место (0 — можно сейчас)."""
        if not self.path:
            return 0.0
        import fcntl
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        now = self._clock()
        with open(self.path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                times = [t for t in self._read(fh) if t + self.window > now]
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        if len(times) >= self.max_requests:
            return max(0.0, times[len(times) - self.max_requests] + self.window - now)
        return 0.0

    def record(self):
        if not self.path:
            return
        import fcntl
        now = self._clock()
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                times = [t for t in self._read(fh) if t + self.window > now]
                times.append(now)
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(times))
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def acquire(self):
        """Дождаться места и отметить запрос (для общего расхода двух процессов)."""
        while True:
            d = self.next_delay()
            if d <= 0:
                break
            self._sleeper(min(d, 30.0))
        self.record()


class RateLimiter:
    """Не более N запросов в окне и не чаще одного запроса в min_interval.

    Часы инъектируются: тест проверяет лимит на виртуальном времени, не спя.
    """

    def __init__(self, max_requests=None, window=None, min_interval=None, clock=time.monotonic):
        self.max_requests = int(max_requests if max_requests is not None else config.RATE_MAX_REQUESTS)
        self.window = float(window if window is not None else config.RATE_WINDOW_SEC)
        self.min_interval = float(min_interval if min_interval is not None else config.MIN_REQUEST_INTERVAL_SEC)
        self._clock = clock
        self._times = deque()

    def _prune(self, now):
        while self._times and self._times[0] + self.window <= now:
            self._times.popleft()

    def next_delay(self, extra=0):
        """Сколько секунд ждать до (extra+1)-го запроса. 0 — можно сейчас."""
        now = self._clock()
        times = [t for t in self._times if t + self.window > now]
        delay = 0.0
        for _ in range(int(extra) + 1):
            cand = now + delay
            if times:
                cand = max(cand, times[-1] + self.min_interval)
            if len(times) >= self.max_requests:
                cand = max(cand, times[len(times) - self.max_requests] + self.window)
            delay = max(0.0, cand - now)
            times.append(cand)
        return delay

    def record(self, when=None):
        """Отметить фактически сделанный запрос."""
        t = self._clock() if when is None else when
        self._times.append(t)

    def count_in_window(self, now=None):
        now = self._clock() if now is None else now
        return sum(1 for t in self._times if t + self.window > now)

    def all_times(self):
        return list(self._times)


# ------------------------------------------------------------------ разбор RSS (Р3.9)
_ITEM_RE = re.compile(r"<item\b.*?>(.*?)</item>", re.S | re.I)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_ANCHOR_RE = re.compile(r"""<a\s[^>]*href=["']([^"']*)["'][^>]*>(.*?)</a>""", re.S | re.I)
_STATUS_RE = re.compile(r"/status/(\d+)")
_RT_TITLE_RE = re.compile(r"^RT by @([A-Za-z0-9_]{1,15}):\s*", re.I)
_RT_SHORT_RE = re.compile(r"^RT @([A-Za-z0-9_]{1,15}):\s*", re.I)
_REPLY_TITLE_RE = re.compile(r"^R to @([A-Za-z0-9_]{1,15}):\s*", re.I)
_PINNED_RE = re.compile(r"^Pinned:\s*", re.I)
_MENTION_TEXT_RE = re.compile(r"(?<![\w/])@([A-Za-z0-9_]{1,15})")
_HASHTAG_TEXT_RE = re.compile(r"[#$]([A-Za-z0-9_]{1,60})")
_URL_TEXT_RE = re.compile(r"https?://[^\s<>\"']+")
_QUOTE_AUTHOR_RE = re.compile(r"<b>\s*([^<]*?)\s*\(@([A-Za-z0-9_]{1,15})\)\s*</b>", re.I)


def count_items(body):
    """Число <item> в теле ответа — единственный честный признак данных (Р3.4)."""
    if not body:
        return 0
    return len(_ITEM_RE.findall(body))


def _inner(item, tag):
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", item, re.S | re.I)
    return m.group(1) if m else None


def _strip_html(text):
    text = _BR_RE.sub("\n", text or "")
    text = _TAG_RE.sub("", text)
    return html.unescape(text)


def _clean_ws(text):
    text = re.sub(r"[ \t\u00a0]+", " ", str(text or ""))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _unwrap_cdata(text):
    if text is None:
        return ""
    m = _CDATA_RE.search(text)
    return m.group(1) if m else text


def _norm_handle(value):
    if not value:
        return None
    h = str(value).strip().lstrip("@").strip()
    m = re.match(r"^([A-Za-z0-9_]{1,15})$", h)
    return m.group(1).lower() if m else None


def _dedup(seq):
    out, seen = [], set()
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _extract_links(html_fragment, host=None):
    """Внешние ссылки: не наш инстанс, не поиск по хештегу, не /status/."""
    links = []
    host_key = (urllib.parse.urlparse(host).netloc.lower() if host else None)
    for href, _inner_html in _ANCHOR_RE.findall(html_fragment or ""):
        h = html.unescape(href).strip()
        if not h.lower().startswith(("http://", "https://")):
            continue
        p = urllib.parse.urlparse(h)
        if host_key and p.netloc.lower() == host_key:
            continue
        if h.lower().startswith("https://x.com/") or h.lower().startswith("https://twitter.com/"):
            links.append(h)
            continue
        links.append(h)
    for u in _URL_TEXT_RE.findall(_strip_html(html_fragment or "")):
        links.append(u.rstrip(".,;)"))
    return _dedup(links)


def _extract_mentions(html_fragment, text, host=None):
    mentions = []
    host_key = urllib.parse.urlparse(host).netloc.lower() if host else None
    # ссылки-упоминания вида https://<instance>/<user> (только свой инстанс)
    for href in re.findall(r"""href=["']([^"']+)["']""", html_fragment or ""):
        p = urllib.parse.urlparse(html.unescape(href))
        if host_key and p.netloc.lower() != host_key:
            continue
        parts = [x for x in p.path.split("/") if x]
        if len(parts) == 1 and not p.query and re.match(r"^[A-Za-z0-9_]{1,15}$", parts[0]):
            mentions.append(parts[0].lower())
    for m in _MENTION_TEXT_RE.findall(text or ""):
        mentions.append(m.lower())
    return _dedup(mentions)


def _extract_hashtags(html_fragment, text):
    tags = []
    for href in re.findall(r"""href=["']([^"']+)["']""", html_fragment or ""):
        p = urllib.parse.urlparse(html.unescape(href))
        if "search" not in p.path:
            continue
        qs = urllib.parse.parse_qs(p.query)
        for q in qs.get("q", []):
            if q.startswith("#") and re.match(r"^#[A-Za-z0-9_]{1,60}$", q):
                tags.append(q[1:])
    for t in _HASHTAG_TEXT_RE.findall(text or ""):
        tags.append(t)
    return _dedup(tags)


def _detect_media(description):
    """media_kind: photo | video | gif | None (берём первый по появлению)."""
    if not description:
        return None
    low = description.lower()
    pos = []
    i = low.find("<video")
    if i >= 0:
        pos.append((i, "gif"))
    for needle, kind in ((">video<", "video"), ("/amplify_video_thumb", "video")):
        i = low.find(needle)
        if i >= 0:
            pos.append((i, kind))
    i = low.find("/pic/media")
    if i >= 0:
        pos.append((i, "photo"))
    if not pos:
        return None
    return min(pos, key=lambda x: x[0])[1]


def parse_rss(body, host=None, now=None):
    """Разбор RSS Nitter в список постов (Р3.9).

    Возвращает список словарей с ключами: tweet_id, owner_handle, orig_handle,
    published_at_utc, published_src, text, links, mentions, hashtags,
    is_retweet, is_quote, is_reply, media_kind, cursor_next.
    """
    now = now or datetime.now(timezone.utc)
    posts = []
    for raw in _ITEM_RE.findall(body or ""):
        title_raw = _unwrap_cdata(_inner(raw, "title") or "")
        title = _clean_ws(_strip_html(title_raw))

        is_retweet = is_reply = 0
        owner_handle = None
        m = _RT_TITLE_RE.match(title) or _RT_SHORT_RE.match(title)
        if m:
            is_retweet = 1
            owner_handle = m.group(1).lower()
            title = title[m.end():]
        else:
            m = _REPLY_TITLE_RE.match(title)
            if m:
                is_reply = 1
                title = title[m.end():]
        title = _PINNED_RE.sub("", title).strip()

        creator = _norm_handle(_strip_html(_inner(raw, "dc:creator") or ""))

        description = _unwrap_cdata(_inner(raw, "description") or "")
        is_quote = 1 if "<blockquote" in (description or "").lower() else 0

        # текст самого поста: до вложенной цитаты / карточки / обёртки медиа / hr
        own_html = description or ""
        cut = len(own_html)
        for marker in ("<hr/>", "<hr />", "<hr>", "<blockquote", "<video", "<img "):
            i = own_html.lower().find(marker.lower())
            if 0 <= i < cut:
                cut = i
        # обёртка видео: <a href=".../status/<id>"> — собственный текст кончился раньше
        ma = re.search(r"""<a\s[^>]*href=["'][^"']*/status/[^"']*["']""", own_html)
        if ma and ma.start() < cut:
            cut = ma.start()
        own_html = own_html[:cut]
        text = _clean_ws(_strip_html(own_html)) if _strip_html(own_html).strip() else None
        if not text and title:
            text = title

        # guid: числовой snowflake, иначе ссылка
        guid_raw = _clean_ws(_strip_html(_inner(raw, "guid") or ""))
        link = _clean_ws(_strip_html(_inner(raw, "link") or ""))
        tweet_id = None
        if guid_raw and re.match(r"^\d{5,}$", guid_raw):
            tweet_id = guid_raw
        else:
            m2 = _STATUS_RE.search(guid_raw) or _STATUS_RE.search(link)
            if m2:
                tweet_id = m2.group(1)
        if not tweet_id:
            continue  # пост без id бесполезен: нет дедупликации и даты

        orig_handle = None
        if is_retweet:
            orig_handle = creator
        elif is_quote:
            mq = _QUOTE_AUTHOR_RE.search(description or "")
            if mq:
                orig_handle = mq.group(2).lower()

        published_at, published_src = normalize_published(
            tweet_id, _inner(raw, "pubDate"), now=now)
        if published_at is None:
            published_src = "unknown"

        mentions = [x for x in _extract_mentions(own_html, text or "", host) if x != owner_handle]
        posts.append({
            "tweet_id": tweet_id,
            "owner_handle": owner_handle or creator,
            "orig_handle": orig_handle,
            "published_at_utc": published_at,
            "published_src": published_src,
            "text": text,
            "links": _extract_links(own_html, host),
            "mentions": mentions,
            "hashtags": _extract_hashtags(own_html, text or ""),
            "is_retweet": is_retweet,
            "is_quote": is_quote,
            "is_reply": is_reply,
            "media_kind": _detect_media(description),
            "cursor_next": None,
        })
    return posts


# --------------------------------------------------------- единый транспорт
def raw_http_get(url, timeout=None, headers=None, data=None, method=None):
    """ЕДИНСТВЕННЫЙ выход в сеть проекта (ТЗ-1 Р7.9, ТЗ-4 Р2.1).

    Все каналы (Nitter RSS, CDN tweet-result, syndication timeline, x.com SSR,
    DeepSeek-классификатор ТЗ-3) ходят сюда. Прямой `urlopen` вне этого файла
    запрещён и проверяется тестом `test_no_direct_network`. Возвращает
    (status, headers_lower, body_str); при сетевой ошибке —
    (0, {}, "__transport_error__:...").

    Питфолл 7.7: ответы сжаты gzip — декомпрессия по Content-Encoding.
    `data` (bytes) переводит запрос в POST; `method` задаёт метод явно.
    """
    hdrs = {
        "User-Agent": config.USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "*/*",
    }
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    timeout = config.HTTP_TIMEOUT_SEC if timeout is None else timeout
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            head = {k.lower(): v for k, v in resp.headers.items()}
            body = _decode_body(raw, head)
            return resp.status, head, body
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        head = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return e.code, head, _decode_body(raw, head)
    except Exception as e:  # сеть, DNS, таймаут
        return 0, {}, f"__transport_error__:{type(e).__name__}:{e}"


def raw_http_post(url, body, timeout=None, headers=None):
    """POST JSON-тела через тот же единственный выход в сеть.

    Используется клиентом DeepSeek (ТЗ-3 Р1). Сам вызов `urlopen` остаётся
    ровно один — в `raw_http_get`, поэтому инвариант `test_no_direct_network`
    сохраняется.
    """
    payload = body.encode("utf-8") if isinstance(body, str) else (body or b"")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    return raw_http_get(url, timeout=timeout, headers=hdrs, data=payload,
                        method="POST")



def _decode_body(raw, headers):
    body = raw or b""
    if (headers or {}).get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            pass
    return body.decode("utf-8", "replace")


# ------------------------------------------------------------------- job / очередь
@dataclass(order=True)
class _Job:
    priority: int
    not_before: float
    seq: int
    kind: str = field(compare=False)
    key: str = field(compare=False)
    cursor: str = field(compare=False, default=None)
    force: bool = field(compare=False, default=False)
    run_id: int = field(compare=False, default=None)
    attempts: int = field(compare=False, default=0)
    done: bool = field(compare=False, default=False)
    result: object = field(compare=False, default=None)
    error: object = field(compare=False, default=None)
    last_failure: str = field(compare=False, default=None)


class NitterBroker:
    """Единственная точка выхода в Nitter."""

    def __init__(self, instances=None, *, db_path=None, transport=None,
                 clock=time.monotonic, sleeper=time.sleep, async_mode=None,
                 run_id=None, cache_ttl=None, daily_cap=None):
        self.instances = [i.rstrip("/") for i in (instances or config.nitter_instances())]
        self._db_path = db_path or config.DB_PATH
        self._transport = transport or self._default_transport
        self._clock = clock
        self._sleeper = sleeper
        self._async = config.BROKER_ASYNC if async_mode is None else async_mode
        self._cache_ttl = config.CACHE_TTL_SEC if cache_ttl is None else cache_ttl
        self._daily_cap = config.DAILY_REQUEST_CAP if daily_cap is None else daily_cap
        self.run_id = run_id

        self._lock = threading.RLock()
        self._con = db.connect(self._db_path, check_same_thread=False)
        self._pending = []
        self._seq = 0
        self._cache = {}          # key -> (ts, result)
        self._rl = {h: RateLimiter(clock=clock) for h in self.instances}
        self._cooldown_until = {h: 0.0 for h in self.instances}
        self._blocked_until = {h: 0.0 for h in self.instances}
        self._fail_streak = {h: 0 for h in self.instances}
        # ТЗ-10 2.1: счётчик ФАКТИЧЕСКИХ отказов сбора (не проб). Живёт в БД.
        self._collect_fail = {h: 0 for h in self.instances}
        # Монотонный счётчик событий отказов за жизнь процесса — для итоговой
        # строки прогона («сколько отказов Nitter»).
        self._collect_fail_events = 0
        self._consecutive_429 = {h: 0 for h in self.instances}
        self._healthy = {h: None for h in self.instances}
        self._health_at = {h: None for h in self.instances}
        self._requests_today = {h: 0 for h in self.instances}
        self._day = {h: None for h in self.instances}
        self._budget_warned = set()
        self._sem = (SharedSemaphore() if config.SHARED_SEMAPHORE_PATH else None)
        self._load_state()

    # ----------------------------------------------------------- транспорт
    def _default_transport(self, url, timeout):
        """Обёртка над единым транспортом (Р3, изоляция транспорта)."""
        return raw_http_get(url, timeout, headers={
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        })

    # ------------------------------------------------------------ состояние
    def _today(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_state(self):
        for host in self.instances:
            row = self._con.execute(
                "SELECT * FROM instances WHERE host=?", (host,)).fetchone()
            if row is None:
                self._con.execute("INSERT OR IGNORE INTO instances (host, healthy) VALUES (?, NULL)", (host,))
                continue
            self._requests_today[host] = row["requests_today"] or 0
            self._day[host] = row["day"]
            self._fail_streak[host] = row["fail_streak"] or 0
            try:
                self._collect_fail[host] = row["collect_fail_streak"] or 0
            except (IndexError, KeyError):
                self._collect_fail[host] = 0
            if row["day"] != self._today():
                self._requests_today[host] = 0
            if row["cooldown_until"]:
                cd = db.parse_iso(row["cooldown_until"])
                if cd:
                    left = (cd - datetime.now(timezone.utc)).total_seconds()
                    if left > 0:
                        self._cooldown_until[host] = self._clock() + left
        self._con.commit()

    def _save_instance(self, host, **kw):
        healthy = self._healthy.get(host)
        fields = {
            "fail_streak": self._fail_streak[host],
            "collect_fail_streak": self._collect_fail.get(host, 0),
            "requests_today": self._requests_today[host],
            "day": self._day[host],
            "rate_limited_429": self._consecutive_429[host],
        }
        # NULL-здоровье не затирает уже записанное состояние
        if healthy is not None:
            fields["healthy"] = 1 if healthy else 0
            fields["rss_ok"] = 1 if healthy else 0
        fields.update(kw)
        cd = self._cooldown_until[host]
        fields["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, cd - self._clock()))).strftime("%Y-%m-%dT%H:%M:%S") if cd > self._clock() else None
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._con.execute(f"UPDATE instances SET {cols} WHERE host=?",
                              (*fields.values(), host))
            self._con.commit()

    # --------------------------------------------------------------- budget
    def _bump_budget(self, host):
        day = self._today()
        if self._day[host] != day:
            self._day[host] = day
            self._requests_today[host] = 0
            self._budget_warned.discard(host)
        self._requests_today[host] += 1
        if (self._requests_today[host] >= self._daily_cap
                and host not in self._budget_warned):
            self._budget_warned.add(host)
            self._warn(host, f"WARN: суточный потолок {self._daily_cap} достигнут, "
                             f"инстанс исключён до конца суток")
        self._save_instance(host)

    def _over_budget(self, host):
        return self._requests_today.get(host, 0) >= self._daily_cap

    def _streak(self, host):
        """Тяжесть инстанса: max(пробы, фактические отказы сбора)."""
        return max(self._fail_streak.get(host, 0), self._collect_fail.get(host, 0))

    def all_degraded(self, threshold=None):
        """ТЗ-10 2.1/2.2: Nitter деградировал — суммарные ФАКТИЧЕСКИЕ отказы.

        Считаются оба счётчика (health-проба `fail_streak` и отказы сбора
        `collect_fail_streak`) по всем инстансам, берётся максимум из памяти и
        БД (состояние переживает перезапуск). Деградация — когда суммарная
        тяжесть достигла порога: один прогон, в котором аккаунт исчерпал свои
        повторные попытки (<= `MAX_JOB_ATTEMPTS`), уже включает резерв в ТОМ ЖЕ
        прогоне. Раньше требовалось `>= N` по КАЖДОМУ инстансу, и резерв
        включался только через ~3 цикла проб (до получаса простоя).
        """
        thr = config.XSSR_DEGRADED_STREAK if threshold is None else int(threshold)
        total = 0
        for h in self.instances:
            mem = self._streak(h)
            row = self._con.execute(
                "SELECT fail_streak, collect_fail_streak FROM instances WHERE host=?",
                (h,)).fetchone()
            persisted = 0
            if row:
                try:
                    persisted = max(row["fail_streak"] or 0,
                                    row["collect_fail_streak"] or 0)
                except (IndexError, KeyError):
                    persisted = row["fail_streak"] or 0
            total += max(mem, persisted)
        return bool(self.instances) and total >= thr

    def collect_fail_events(self):
        """Монотонный счётчик событий фактических отказов сбора (ТЗ-10 2.3)."""
        return self._collect_fail_events

    def _warn(self, handle, msg, level="WARN"):
        try:
            db.log_run(self._con, level, msg, handle=handle, run_id=self.run_id)
            self._con.commit()
        except Exception:
            pass

    # ------------------------------------------------------- выбор инстанса
    def _health_fresh(self, host):
        """Есть ли свежий вердикт health-пробы (моложе HEALTH_TTL_SEC)."""
        at = self._health_at.get(host)
        if at is None:
            return False
        return (self._clock() - at) < config.HEALTH_TTL_SEC

    def _is_healthy(self, host):
        """Свежий вердикт «жив»: не чаще одной пробы в HEALTH_TTL_SEC (Р3.4)."""
        if self._healthy.get(host) is None or not self._health_fresh(host):
            return False
        return bool(self._healthy[host])

    def _pick_instance(self, now):
        """(host, wait_sec). host=None, если живых нет.

        ТЗ-10 2.1 (исправление дефекта): свежая отметка «нездоров» больше НЕ
        исключает инстанс. Иначе повторных фактических попыток не было, отказы
        не копились и резерв включался с запаздыванием. Health-проба остаётся
        ограниченной TTL (`_ensure_healthy`), а пропуск инстанса — только по
        cooldown/блокировке/суточному потолку.
        """
        best = None
        for host in self.instances:
            if self._cooldown_until[host] > now or self._blocked_until[host] > now:
                continue
            if self._over_budget(host):
                continue
            wait = self._rl[host].next_delay()
            if best is None or wait < best[1]:
                best = (host, wait)
        return best if best else (None, 0.0)

    # ------------------------------------------------------------ health (Р3.4)
    def check_instance(self, host):
        """GET /nasa/rss: живой только при HTTP 200 и items >= 15."""
        host = host.rstrip("/")
        url = host + config.HEALTH_PATH
        status, headers, body, latency_ms = self._http_get(host, url, "health", None, wait=True)
        items = count_items(body)
        healthy = bool(status == 200 and items >= config.HEALTH_MIN_ITEMS)
        err = None
        if status == 0:
            err = str(body)[:300]
        elif status != 200:
            err = f"HTTP {status}"
        elif items < config.HEALTH_MIN_ITEMS:
            err = f"HTTP 200, но items={items} < {config.HEALTH_MIN_ITEMS} (заглушка?)"
        # Р3.5 применяется и к health-запросам: 429 -> cooldown, 403/451 -> сутки
        if status == 429:
            self._handle_429(host)
        elif status in (403, 451):
            self._block_instance(host, status)
        with self._lock:
            self._healthy[host] = healthy
            self._health_at[host] = self._clock()
            if healthy:
                self._fail_streak[host] = 0
                self._consecutive_429[host] = 0
            else:
                self._fail_streak[host] = self._fail_streak.get(host, 0) + 1
        self._save_instance(
            host,
            items_last_test=items,
            last_check_at=db.utcnow_iso(),
            version=headers.get("x-nitter-backend"),
            last_error=err,
            rss_ok=1 if healthy else 0,
            healthy=1 if healthy else 0,
        )
        return {"host": host, "healthy": healthy, "rss_ok": healthy, "items": items,
                "status": status, "latency_ms": latency_ms, "error": err}

    def _ensure_healthy(self, host, priority="collect"):
        """Health-проба не чаще одного раза в HEALTH_TTL_SEC (Р3.4, ТЗ-10 2.1).

        Свежий вердикт (и «жив», и «нездоров») используется без сети: на подсчёт
        ФАКТИЧЕСКИХ отказов сбора это ограничение не распространяется — его
        ведёт `_run_job`.
        """
        if self._health_fresh(host):
            return bool(self._healthy.get(host))
        res = self.check_instance(host)
        if not res["healthy"]:
            self._warn(host, f"инстанс не прошёл health-проверку: {res['error']}")
        return res["healthy"]

    # ---------------------------------------------------------- HTTP + учёт (Р3.3)
    def _http_get(self, host, url, kind, run_id, wait=True):
        rl = self._rl[host]
        if getattr(self, "_sem", None) is not None:
            self._sem.acquire()
        if wait:
            d = rl.next_delay()
            if d > 0:
                self._sleeper(d)
        t0 = self._clock()
        status, headers, body = self._transport(url, config.HTTP_TIMEOUT_SEC)
        latency_ms = int(max(0.0, self._clock() - t0) * 1000)
        rl.record()
        items = count_items(body) if status == 200 else 0
        self._bump_budget(host)
        with self._lock:
            self._con.execute(
                "INSERT INTO requests (host, ts, kind, url, status, items, latency_ms, run_id)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (host, db.utcnow_iso(), kind, url, status, items, latency_ms, run_id))
            self._con.commit()
        return status, headers, body, latency_ms

    # --------------------------------------------------------------- кеш (Р3.6)
    @staticmethod
    def _cache_key(kind, key, cursor):
        return f"{kind}|{key}|{cursor or ''}"

    def _cache_get(self, key, force):
        if force:
            return None
        hit = self._cache.get(key)
        if hit and (self._clock() - hit[0]) < self._cache_ttl:
            return json.loads(hit[1])
        return None

    def _cache_put(self, key, value):
        self._cache[key] = (self._clock(), json.dumps(value, ensure_ascii=False))

    # ------------------------------------------------------- публичный интерфейс
    def fetch_feed(self, handle, cursor=None, priority="collect", force=False):
        return self._fetch("feed", handle.lstrip("@"), cursor, priority, force)

    def fetch_search(self, query, cursor=None, priority="discover", force=False):
        return self._fetch("search", query, cursor, priority, force)

    def _fetch(self, kind, key, cursor, priority, force):
        if priority not in config.PRIORITIES:
            raise NitterError(f"неизвестный приоритет: {priority}")
        ckey = self._cache_key(kind, key, cursor)
        cached = self._cache_get(ckey, force)
        if cached is not None:
            return cached
        job = _Job(priority=config.PRIORITIES[priority],
                   not_before=self._clock(), seq=self._next_seq(), kind=kind, key=key,
                   cursor=cursor, force=force, run_id=self.run_id)
        with self._lock:
            heapq.heappush(self._pending, job)
        self._drain(stop_at=job)
        if job.error is not None:
            raise job.error
        return job.result

    def _next_seq(self):
        with self._lock:
            self._seq += 1
            return self._seq

    def _requeue(self, job):
        with self._lock:
            heapq.heappush(self._pending, job)

    def _select(self, now):
        with self._lock:
            if not self._pending:
                return None, 0.0
            ready = [j for j in self._pending if j.not_before <= now and not j.done]
            if not ready:
                # не готов никто: сон до ближайшего
                alive = [j for j in self._pending if not j.done]
                if not alive:
                    return None, 0.0
                j = min(alive, key=lambda x: (x.not_before, x.priority, x.seq))
                return j, max(0.001, j.not_before - now)
            job = min(ready, key=lambda x: (x.priority, x.not_before, x.seq))
            return job, 0.0

    def _drain(self, stop_at=None):
        while True:
            if stop_at is not None and stop_at.done:
                return
            job, wait = self._select(self._clock())
            if job is None:
                return
            if wait > 0:
                self._sleeper(wait)
                continue
            self._run_job(job)

    def _build_url(self, host, kind, key, cursor):
        if kind == "feed":
            url = f"{host}/{urllib.parse.quote(key)}/rss"
        elif kind == "search":
            url = f"{host}/search/rss?f=tweets&q={urllib.parse.quote(key)}"
        elif kind == "backfill":
            url = f"{host}/{urllib.parse.quote(key)}/rss"
        else:
            raise NitterError(f"неизвестный вид запроса: {kind}")
        if cursor:
            url += ("&" if "?" in url else "?") + "cursor=" + urllib.parse.quote(cursor)
        return url

    @staticmethod
    def _is_transport_failure(status):
        """ТЗ-10 2.1: что считать фактическим отказом Nitter.

        status==0 — соединение/DNS/таймаут/обрыв чтения; >=500 — ошибка сервера.
        404/400 и прочие 4xx — это НЕ отказ (П4), пустая лента тоже (П4).
        """
        return status == 0 or status >= 500

    def _note_collect_failure(self, host, status, body):
        """Фактический отказ сбора: растёт счётчик инстанса (ТЗ-10 2.1)."""
        with self._lock:
            self._collect_fail[host] = self._collect_fail.get(host, 0) + 1
            self._collect_fail_events += 1
        self._save_instance(host, last_error=str(body)[:300])

    def _note_collect_success(self, host):
        """Успешный фид обнуляет счётчик отказов инстанса (ТЗ-10 2.1/П5)."""
        with self._lock:
            self._consecutive_429[host] = 0
            self._fail_streak[host] = 0
            self._collect_fail[host] = 0
            self._healthy[host] = True
            if self._health_at[host] is None:
                self._health_at[host] = self._clock()
        self._save_instance(host, last_error=None)
        # Nitter снова отдаёт фиды — признак «резерв активен» снимаем.
        if not self.all_degraded():
            try:
                db.clear_reserve_active(self._con)
            except Exception:
                pass

    def _note_no_live_instance(self):
        """Нет живых инстансов — тоже фактический отказ Nitter (ТЗ-10 2.1)."""
        with self._lock:
            for h in self.instances:
                self._collect_fail[h] = self._collect_fail.get(h, 0) + 1
            self._collect_fail_events += 1
        for h in self.instances:
            self._save_instance(h, last_error="нет живых инстансов Nitter")

    def _exhausted_error(self, job):
        """Ошибка после исчерпания попыток — с сохранением ПРИЧИНЫ (ТЗ-10 2.2).

        Транспортный отказ даёт право на резерв, 404 — нет (П4).
        """
        n = config.MAX_JOB_ATTEMPTS
        if job.last_failure == "transport":
            return NitterTransportError(
                f"исчерпаны попытки ({n}): транспортный отказ Nitter:"
                f" {job.kind} {job.key}")
        if job.last_failure == "not_found":
            return NitterNotFound(
                f"исчерпаны попытки ({n}): Nitter отдал 404 (не найдено):"
                f" {job.kind} {job.key}")
        return NitterError(
            f"исчерпаны попытки ({n}): {job.kind} {job.key}")

    def _run_job(self, job):
        now = self._clock()
        with self._lock:
            self._pending = [j for j in self._pending if not (j is job or j.done)]
        host, wait = self._pick_instance(now)
        if host is None:
            job.error = NoLiveInstance("нет живых инстансов Nitter (cooldown/блок/потолок)")
            job.done = True
            self._note_no_live_instance()
            self._warn(None, "NoLiveInstance: все инстансы в cooldown/заблокированы/"
                             "исчерпали суточный потолок")
            return
        if wait > 0:
            # ожидание лимитера — это НЕ попытка запроса
            job.not_before = now + wait
            self._requeue(job)
            return

        # Health-проба — не чаще раза в HEALTH_TTL_SEC (Р3.4). Вердикт лишь
        # подсказка: фактическую попытку ленты делаем всегда (ТЗ-10 2.1), иначе
        # повторных проверок нет и резерв включается с запаздыванием. Если проба
        # пометила инстанс заблокированным/в cooldown — уходим на другой.
        if not self._health_fresh(host):
            self._ensure_healthy(host)
            if (self._blocked_until[host] > self._clock()
                    or self._cooldown_until[host] > self._clock()):
                job.not_before = self._clock() + 0.01
                self._requeue(job)
                return

        # Попыткой считается только фактический запрос ленты (ТЗ-10 2.1):
        # ожидания лимитера и health-пробы её не расходуют.
        job.attempts += 1
        if job.attempts > config.MAX_JOB_ATTEMPTS:
            job.error = self._exhausted_error(job)
            job.done = True
            return

        url = self._build_url(host, job.kind, job.key, job.cursor)
        status, headers, body, latency_ms = self._http_get(host, url, job.kind, job.run_id)

        if status == 429:
            self._handle_429(host)
            job.not_before = self._clock() + 0.01
            self._requeue(job)
            return
        if status in (403, 451):
            self._block_instance(host, status)
            job.not_before = self._clock() + 0.01
            self._requeue(job)
            return
        if self._is_transport_failure(status):
            # ТЗ-10 2.1: фактический отказ сбора, без ограничения по TTL.
            self._note_collect_failure(host, status, body)
            job.last_failure = "transport"
            job.not_before = self._clock() + 0.01
            self._requeue(job)
            return
        if status == 404:
            # ТЗ-10 2.2/П4: «не найдено» — не отказ Nitter, счётчик не растёт.
            job.last_failure = "not_found"
            job.not_before = self._clock() + 0.01
            self._requeue(job)
            return
        if status != 200:
            job.last_failure = "http"
            job.not_before = self._clock() + 0.01
            self._requeue(job)
            return

        # успех
        self._note_collect_success(host)
        cursor_next = headers.get("min-id") or headers.get("Min-Id")
        posts = parse_rss(body, host=host)
        for p in posts:
            p["cursor_next"] = cursor_next
        job.result = posts
        job.error = None
        job.done = True
        self._cache_put(self._cache_key(job.kind, job.key, job.cursor), posts)

    def _handle_429(self, host):
        with self._lock:
            self._consecutive_429[host] += 1
            n = self._consecutive_429[host]
            self._fail_streak[host] += 1
        secs = config.COOLDOWN_429_HARD_SEC if n >= 2 else config.COOLDOWN_429_SEC
        self._cooldown_until[host] = max(self._cooldown_until[host], self._clock() + secs)
        self._save_instance(host, last_error="429 Too Many Requests")
        self._warn(host, f"429 на инстансе, cooldown {secs} с (подряд: {n})")

    def _block_instance(self, host, status):
        secs = config.COOLDOWN_403_SEC
        with self._lock:
            self._blocked_until[host] = self._clock() + secs
            self._healthy[host] = False
            self._health_at[host] = self._clock()
        self._save_instance(host, last_error=f"HTTP {status}: инстанс недоступен",
                            healthy=0, blocked=1)
        self._warn(host, f"HTTP {status}: инстанс помечен недоступным на сутки")

    def stats(self):
        """Р3.8: по инстансам — запросы за сутки, cooldown, живой/нет."""
        now_utc = datetime.now(timezone.utc)
        out = {}
        for host in self.instances:
            cd = self._cooldown_until[host]
            bd = self._blocked_until[host]
            deadline = max(cd, bd)
            out[host] = {
                "requests_today": self._requests_today.get(host, 0),
                "daily_cap": self._daily_cap,
                "used_pct": round(100.0 * self._requests_today.get(host, 0) / self._daily_cap, 2),
                "cooldown_sec": max(0, int(deadline - self._clock())),
                "blocked": bd > self._clock(),
                "healthy": self._healthy.get(host),
                "collect_fail_streak": self._collect_fail.get(host, 0),
                "over_budget": self._over_budget(host),
                "rate_limited_429": self._consecutive_429.get(host, 0),
                "day": self._day.get(host),
            }
        # дополнить данными из БД (мог быть другой процесс)
        for host in self.instances:
            row = self._con.execute(
                "SELECT requests_today, day, cooldown_until, healthy, items_last_test,"
                " last_check_at, last_error FROM instances WHERE host=?", (host,)).fetchone()
            if row:
                out[host]["db_requests_today"] = row["requests_today"] if row["day"] == now_utc.strftime("%Y-%m-%d") else 0
                out[host]["db_healthy"] = row["healthy"]
                out[host]["last_check_at"] = row["last_check_at"]
                out[host]["items_last_test"] = row["items_last_test"]
                out[host]["last_error"] = row["last_error"]
        return out

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass


# ------------------------------------------------------------------ singleton
_BROKER = None
_BROKER_LOCK = threading.Lock()


def get_broker(**kwargs):
    """Процессный синглтон брокера: одна очередь и одна квота на процесс."""
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None or kwargs:
            _BROKER = NitterBroker(**kwargs)
        return _BROKER


def reset_broker():
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is not None:
            _BROKER.close()
        _BROKER = None
