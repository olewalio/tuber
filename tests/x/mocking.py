"""Подменяемый транспорт Nitter и виртуальные часы для тестов.

Реальные запросы в тестах не выполняются (Р7).
"""
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

SNOWFLAKE_EPOCH_MS = 1288834974657


class VClock:
    """Виртуальное время: тест не спит, но лимитер видит честные интервалы."""

    def __init__(self, start=1_000_000.0):
        self.t = float(start)
        self.sleeps = []

    def __call__(self):
        return self.t

    def advance(self, seconds):
        if seconds and seconds > 0:
            self.sleeps.append(seconds)
            self.t += float(seconds)

    def sleep(self, seconds):
        self.advance(seconds)


def snowflake_id(dt, seq=0):
    ms = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return str(((ms - SNOWFLAKE_EPOCH_MS) << 22) | seq)


def make_item(tweet_id, handle, text="post", published=None, extra=""):
    pub = ""
    if published is not None:
        pub = f"<pubDate>{published}</pubDate>"
    return f"""    <item>
      <title>{text}</title>
      <dc:creator>@{handle}</dc:creator>
      <description><![CDATA[<p>{text}</p>]]></description>
      {pub}
      <guid isPermaLink="false">{tweet_id}</guid>
      <link>https://one.test/{handle}/status/{tweet_id}#m</link>
    </item>
"""


def make_feed(n=20, handle="nasa", start=None, spacing_minutes=None, jitter=0.0,
              seed=7, body_extra=None):
    """Сгенерировать RSS с n постами и валидными pubDate.

    jitter > 0 даёт неравномерные интервалы (нужно, чтобы CV проходил порог).
    """
    import random
    rng = random.Random(seed)
    start = start or datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    if spacing_minutes is None:
        spacing_minutes = 6000.0 / max(n, 1)  # n постов внутри ~4 суток
    gaps = [spacing_minutes]
    for _ in range(n - 1):
        mult = 1.0 + jitter * rng.uniform(-0.85, 0.85) if jitter else 1.0
        gaps.append(max(1.0, spacing_minutes * mult))
    items = []
    cursor_dt = start
    times = []
    for i in range(n):
        times.append(cursor_dt)
        items.append(make_item(snowflake_id(cursor_dt, seq=i), handle,
                               text=f"{handle} post {i}",
                               published=cursor_dt.strftime("%a, %d %b %Y %H:%M:%S GMT")))
        cursor_dt = cursor_dt - timedelta(minutes=gaps[min(i + 1, len(gaps) - 1)])
    body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<rss xmlns:atom=\"http://www.w3.org/2005/Atom\""
            " xmlns:dc=\"http://purl.org/dc/elements/1.1/\" version=\"2.0\">\n"
            "  <channel>\n" + (body_extra or "") + "".join(items) + "  </channel>\n</rss>\n")
    return body


def error_feed(body="<rss><channel></channel></rss>"):
    return body


class FakeNitter:
    """Транспорт-заглушка: (url, timeout) -> (status, headers, body)."""

    def __init__(self, clock=None, health_body=None, health_items=20):
        self.clock = clock or VClock()
        self.calls = []          # (url, timeout, virtual_time)
        self.feed_routes = {}    # handle -> (status, headers, body)
        self.search_routes = {}  # query -> (status, headers, body)
        self.health_status = 200
        self.health_headers = {}
        self.health_body = health_body if health_body is not None else make_feed(health_items)
        self.default_feed = make_feed(20)
        self.default_status = 200
        self.counts = {}
        self.cursor_routes = {}  # (handle, cursor) -> (status, headers, body)

    # --- настройка
    def set_feed(self, handle, body=None, status=200, headers=None, items=None):
        if body is None:
            body = make_feed(items if items is not None else 20, handle=handle)
        self.feed_routes[handle.lower()] = (status, headers or {}, body)

    def set_search(self, query, body=None, status=200, headers=None, items=20):
        if body is None:
            body = make_feed(items, handle="searcher")
        self.search_routes[query] = (status, headers or {}, body)

    def set_health(self, status=200, body=None, headers=None):
        self.health_status = status
        self.health_headers = headers or {}
        self.health_body = body if body is not None else make_feed(20)

    # --- транспорт
    def __call__(self, url, timeout):
        self.calls.append((url, timeout, self.clock()))
        p = urllib.parse.urlparse(url)
        path = p.path
        if path.endswith("/nasa/rss"):
            return self.health_status, dict(self.health_headers), self.health_body
        if "/search/rss" in path:
            q = urllib.parse.parse_qs(p.query).get("q", [""])[0]
            if q in self.search_routes:
                return self.search_routes[q]
            return 200, {}, make_feed(20, handle="searcher")
        m = re.match(r"^/([A-Za-z0-9_]+)/rss$", path)
        if m:
            handle = m.group(1).lower()
            self.counts[handle] = self.counts.get(handle, 0) + 1
            cursor = urllib.parse.parse_qs(p.query).get("cursor", [None])[0]
            key = (handle, cursor)
            if key in self.cursor_routes:
                return self.cursor_routes[key]
            if cursor is not None:
                return 200, {"min-id": f"cur-{cursor}-next"}, make_feed(0)
            if handle in self.feed_routes:
                return self.feed_routes[handle]
            return self.default_status, {}, self.default_feed
        return 404, {}, "not found"

    # --- удобства
    def feed_call_times(self, health_path="/nasa/rss"):
        """Виртуальные времена запросов лент (без health и поиска)."""
        out = []
        for url, _t, ts in self.calls:
            if url.endswith(health_path) or "/search/rss" in url:
                continue
            out.append(ts)
        return out


# ------------------------------------------------------------- ТЗ-4: каналы
def make_cdn_payload(tweet_id="1234567890123456789", likes=10, replies=2, text="post",
                     lang="en", handle="author", created="2026-09-14T10:00:00.000Z",
                     note=None, quoted=None, verified=False, edited=False,
                     tombstone=False):
    """JSON-ответ канала cdn_tweet (tweet-result)."""
    if tombstone:
        return json.dumps({"__typename": "Tombstone", "id_str": str(tweet_id)})
    data = {
        "id_str": str(tweet_id),
        "favorite_count": likes,
        "conversation_count": replies,
        "created_at": created,
        "text": text,
        "lang": lang,
        "user": {"screen_name": handle, "verified": verified},
        "isEdited": edited,
    }
    if note:
        data["note_tweet"] = {"text": note}
    if quoted:
        data["quoted_tweet"] = {"favorite_count": 5,
                                "user": {"screen_name": quoted}}
    return json.dumps(data)


import json  # noqa: E402


class FakeCdnTransport:
    """Подмена транспорта канала cdn_tweet: (url, headers) -> (status, hdrs, body)."""

    def __init__(self, clock=None):
        self.clock = clock or VClock()
        self.calls = []                 # (url, headers, virtual_time)
        self.routes = {}                # tweet_id -> (status, body)
        self.default = (404, "{}")
        self.gzip = False

    def set(self, tweet_id, data=None, status=200, body=None):
        if body is None:
            body = data if isinstance(data, str) else make_cdn_payload(tweet_id)
        self.routes[str(tweet_id)] = (status, body)

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers), self.clock()))
        tid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("id", [""])[0]
        status, body = self.routes.get(tid, self.default)
        return status, {"content-encoding": "gzip"} if self.gzip else {}, body

    def ids(self):
        out = []
        for url, _h, _t in self.calls:
            out.append(urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("id", [""])[0])
        return out


class FakeSyndTransport:
    """Подмена транспорта ленты syndication."""

    def __init__(self, clock=None):
        self.clock = clock or VClock()
        self.calls = []
        self.routes = {}     # handle -> (status, body)
        self.default = (429, "")

    def set(self, handle, posts=None, status=200, body=None):
        if body is None:
            body = make_synd_html(posts if posts is not None else make_synd_posts())
        self.routes[handle.lower()] = (status, body)

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers), self.clock()))
        handle = url.rsplit("/", 1)[-1].lower()
        status, body = self.routes.get(handle, self.default)
        return status, {}, body


class FakeSsrTransport:
    """Подмена транспорта x.com SSR."""

    def __init__(self, clock=None):
        self.clock = clock or VClock()
        self.calls = []
        self.routes = {}     # handle -> (status, body)
        self.default = (200, "")

    def set(self, handle, html=None, status=200):
        if html is None:
            html = make_ssr_html()
        self.routes[handle.lower()] = (status, html)

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers), self.clock()))
        handle = url.rstrip("/").rsplit("/", 1)[-1].lower()
        status, body = self.routes.get(handle, self.default)
        return status, {}, body


def make_synd_posts(n=3, handle="author", start=None):
    start = start or datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        dt = start - timedelta(hours=i)
        out.append({
            "id_str": snowflake_id(dt, seq=i),
            "full_text": f"{handle} synd post {i}",
            "created_at": dt.strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "favorite_count": 100 + i,
            "retweet_count": 10 + i,
            "reply_count": 3 + i,
            "lang": "en",
            "user": {"screen_name": handle},
        })
    return out


def make_synd_html(posts):
    """HTML с встроенным __NEXT_DATA__ (питфолл 2.3)."""
    entries = []
    for p in posts:
        tw = dict(p)
        text = tw.get("full_text", "")
        if text.startswith("RT @"):
            tw["is_retweet_expected"] = True
        entries.append({"type": "tweet", "content": {"tweet": tw}})
    data = {"props": {"pageProps": {"timeline": {"entries": entries}}}}
    return ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data) + "</script></body></html>")


def make_ssr_html(ids=None, handle="author"):
    ids = ids or [snowflake_id(datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc), seq=i)
                  for i in range(6)]
    body = "".join(f'<a href="/{handle}/status/{i}">x</a>' for i in ids)
    return f"<html><body>{body}</body></html>"


def mark_ai(con, is_ai=1, *, topic="релизы моделей", claim_type="news"):
    """Пометить посты приговором модели (ТЗ-8 задача 1).

    Сюжеты/оценки/отчёт теперь видны только для постов с приговором is_ai=1,
    поэтому тесты, строящие сюжеты из «сырых» постов, обязаны их
    классифицировать. Приговор пишется в `classified` по text_hash.
    """
    for r in con.execute("SELECT text_hash FROM posts WHERE text_hash IS NOT NULL"):
        con.execute(
            "INSERT OR REPLACE INTO classified (text_hash, is_ai, topic, claim_type,"
            " status, method) VALUES (?,?,?,?, 'classified', 'model')",
            (r["text_hash"], is_ai, topic if is_ai else None,
             claim_type if is_ai else None))
    con.commit()
