"""ТЗ B: веб-страница HypeParser — поиск постов X по коэффициенту
«просмотры / подписчики автора».

Модуль сознательно живёт рядом с транспортом X, а не в ``cli.py``: он напрямую
опирается на :mod:`tuber.platforms.x.session` (живой ``SearchTimeline`` на сессии
аккаунта), :mod:`tuber.platforms.x.store` (журнал бюджета + необязательная запись)
и :mod:`tuber.platforms.x.config`. Отдельный пакет ``tuber/web`` завёл бы
платформенно-нейтральный слой, которого в проекте пока нет: страница по существу
одна и она про X.

Зависимостей нет — только стандартная библиотека (``http.server``, ``html``,
``urllib.parse``, ``threading``, ``json``, ``argparse``). Проектный venv не
используется и не трогается.

Запуск по умолчанию слушает ТОЛЬКО петлю::

    python3 -m tuber.platforms.x.hype_web --host 127.0.0.1 --port 8730

Наружу (не петлевой адрес) сервер поднимется лишь при явном ``--allow-public``.

Маршруты:

* ``GET /``            — форма (запрос, период, режим, страницы, только с просмотрами);
* ``GET /search``      — живой поиск и таблица по коэффициенту (кэш 10 минут);
* ``GET /healthz``     — 200 + короткий JSON (версия, доступность сессии, бюджет);
* ``GET /robots.txt``  — ``User-agent: *`` + ``Disallow: /``.

Предохранители (см. :class:`HypeApp`): один поиск за раз; пауза ≥ 10 с между
стартами; максимум 5 страниц и 200 постов; суточный лимит поиска берётся из
штатного бюджета сессии (``X_SESSION_SEARCH_DAILY_CAP``); один поиск ограничен
180 с и отдаёт то, что успел. Любая ошибка транспорта — понятная русская
HTML-страница без трассировки; трассировка уходит в журнал.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tuber import __version__ as TUBER_VERSION
from tuber.platforms.x import __version__ as X_PLATFORM_VERSION
from tuber.platforms.x import config
from tuber.platforms.x import session as xs
from tuber.platforms.x import store as db

# --------------------------------------------------------------------------- #
# Константы предохранителей. Источник части лимитов — сам бюджет сессии X,
# здесь только то, что специфично для страницы.
# --------------------------------------------------------------------------- #
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8730
MAX_PAGES = 5               # требование ТЗ: не больше 5 страниц за запрос
MAX_POSTS = 200             # требование ТЗ: не больше 200 строк в таблице
MIN_INTERVAL_SEC = 10.0     # требование ТЗ: пауза между стартами поисков
SEARCH_TIMEOUT_SEC = 180.0  # требование ТЗ: потолок одного поиска
CACHE_TTL_SEC = 600.0       # требование ТЗ: кэш запроса 10 минут
TEXT_SNIPPET = 220          # сколько символов текста поста показывать
APP_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Ошибки: каждая знает свой HTTP-код и человеческий русский текст.
# --------------------------------------------------------------------------- #
class HypeError(Exception):
    """Базовая ошибка страницы: ``status`` + сообщение для человека."""

    status = 500

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class BadRequest(HypeError):
    status = 400


class SearchBusy(HypeError):
    status = 429


class SearchTooSoon(HypeError):
    status = 429


class BudgetExhausted(HypeError):
    status = 429


class SessionUnavailable(HypeError):
    status = 503


class TransportFailure(HypeError):
    status = 502


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #
def _num(value):
    """Число или ``None`` (без падения на строках и bool)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    n = _num(value)
    return None if n is None else int(n)


def _parse_days(raw):
    """Период со страницы: пусто → ``None``; иначе целое 1..3650."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        raise BadRequest("Период (days) должен быть числом: 1, 7 или 30") from None
    if days < 1 or days > 3650:
        raise BadRequest("Период (days) вне допустимого диапазона: 1..3650")
    return days


def _parse_bool(raw):
    return str(raw or "").strip().lower() in ("1", "true", "on", "yes", "да")


def _parse_product(raw):
    product = (raw or "").strip()
    if product not in ("Latest", "Top"):
        if product:
            raise BadRequest("Режим (product) должен быть Latest или Top")
        return "Latest"
    return product


def _keep_days(post, cutoff):
    """Пост новее ``cutoff`` (нет даты — не выбрасываем, как в CLI ``x search``)."""
    if cutoff is None:
        return True
    published = post.get("published_at") or post.get("published_at_utc")
    if not published:
        return True
    dt = db.parse_iso(published)
    if dt is None:
        return True
    return dt >= cutoff


def sort_posts(posts):
    """Сортировка по коэффициенту по убыванию; строки без коэффициента — вниз.

    Тот же порядок, что у ``cli._top_posts``: сначала ``ratio`` (убывание), затем
    ``views`` (убывание); ``ratio is None`` уходит вниз, но не выбрасывается.
    """
    def key(p):
        ratio = _num(p.get("ratio"))
        views = _num(p.get("views"))
        return (0 if ratio is not None else 1,
                -(ratio if ratio is not None else 0.0),
                -(views if views is not None else 0.0))

    return sorted(posts, key=key)


def _snippet(text):
    text = " ".join(str(text or "").split())
    if len(text) > TEXT_SNIPPET:
        text = text[:TEXT_SNIPPET].rstrip() + "…"
    return text


def normalize_post(post):
    """Пост транспорта → строка таблицы для шаблона."""
    return {
        "ratio": _num(post.get("ratio")),
        "views": _int(post.get("views")),
        "subs": _int(post.get("author_subs")),
        "author": post.get("author_handle") or post.get("author") or "?",
        "date": post.get("published_at") or post.get("published_at_utc"),
        "text": _snippet(post.get("text")),
        "url": post.get("url"),
    }


# --------------------------------------------------------------------------- #
# Ядро: поиск с предохранителями и кэшем.
# --------------------------------------------------------------------------- #
def _default_session_factory():
    return xs.get_session_transport(db_path=config.DB_PATH, run_id=None)


class HypeApp:
    """Состояние приложения в процессе: кэш, замок поиска, пауза, бюджет.

    Кэш и замок живут в памяти ОДНОГО процесса (по ТЗ). Между процессами бюджет
    делят уже существующие механизмы транспорта (журнал ``transport_request``).
    """

    def __init__(self, *, session_factory=None, db_path=None,
                 clock=time.monotonic, min_interval=MIN_INTERVAL_SEC,
                 cache_ttl=CACHE_TTL_SEC, write_posts=False):
        self.db_path = db_path or config.DB_PATH
        self.session_factory = session_factory or _default_session_factory
        self._clock = clock
        self.min_interval = float(min_interval)
        self.cache_ttl = float(cache_ttl)
        self.write_posts = bool(write_posts)
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache = {}          # key -> (expires_at, result_dict)
        self._last_start = 0.0

    # --------------------------------------------------------------- health
    def health(self):
        session_available = False
        session_error = None
        try:
            session_available = self.session_factory() is not None
        except Exception as e:  # noqa: BLE001 — health обязан не падать
            session_error = f"{type(e).__name__}: {e}"
        if not session_available and session_error is None:
            session_error = xs.session_last_error()
        daily = {}
        try:
            daily = self._daily_budget()
        except Exception as e:  # noqa: BLE001
            daily = {"error": f"{type(e).__name__}: {e}"}
        return {
            "status": "ok" if session_available else "degraded",
            "version": APP_VERSION,
            "tuber_version": TUBER_VERSION,
            "x_platform_version": X_PLATFORM_VERSION,
            "session_available": session_available,
            "session_error": session_error,
            "search_daily": daily,
        }

    # --------------------------------------------------------------- бюджет
    def _daily_budget(self):
        con = db.connect(self.db_path)
        try:
            b = db.session_budget(con)
        finally:
            con.close()
        return {
            "count": b.get("search_daily_count"),
            "cap": b.get("search_daily_cap"),
            "exhausted": bool(b.get("search_daily_exhausted")),
            "free_at": b.get("search_daily_free_at"),
        }

    def _check_daily_budget(self):
        b = self._daily_budget()
        if b.get("exhausted"):
            raise BudgetExhausted(
                "Суточный лимит поиска X исчерпан: сделано "
                f"{b.get('count')} из {b.get('cap')} за 24 ч. "
                f"Лимит откроется в {b.get('free_at')}. "
                "Попробуйте позже — бюджет X общий для всех задач сессии.")

    # ----------------------------------------------------------------- кэш
    def _cache_get(self, key):
        now = self._clock()
        with self._cache_lock:
            item = self._cache.get(key)
            if item is None:
                return None
            expires, result = item
            if now >= expires:
                self._cache.pop(key, None)
                return None
            return result

    def _cache_put(self, key, result):
        with self._cache_lock:
            # Чистим протухшее, чтобы словарь не рос на ровном месте.
            now = self._clock()
            for k in [k for k, (exp, _r) in self._cache.items() if now >= exp]:
                self._cache.pop(k, None)
            self._cache[key] = (now + self.cache_ttl, result)

    def cache_size(self):
        with self._cache_lock:
            return len(self._cache)

    # -------------------------------------------------------------- поиск
    def search(self, query, *, pages=1, product="Latest", days=None,
               only_views=False):
        """Выполнить (или отдать из кэша) поиск и вернуть результат-словарь.

        Возвращает ``{"query", "product", "pages", "days", "only_views",
        "posts", "stats", "cached", "warning"}``.
        """
        query = (query or "").strip()
        if not query:
            raise BadRequest("Введите поисковый запрос — пустой запрос не отправляем")
        try:
            pages = int(pages)
        except (TypeError, ValueError):
            raise BadRequest("Число страниц должно быть целым: 1..5") from None
        pages = max(1, min(pages, MAX_PAGES))
        product = _parse_product(product)
        days = None if days in (None, "") else _parse_days(str(days))
        only_views = bool(only_views)

        key = json.dumps([query, pages, product, days, only_views],
                         ensure_ascii=False)
        hit = self._cache_get(key)
        if hit is not None:
            return {**hit, "cached": True}

        if not self._lock.acquire(blocking=False):
            raise SearchBusy(
                "Другой поиск уже выполняется. Подождите, пока он закончится, "
                "и повторите — одновременно выполняется не больше одного поиска.")
        try:
            now = self._clock()
            wait = self.min_interval - (now - self._last_start)
            if self._last_start and wait > 0:
                raise SearchTooSoon(
                    "Слишком часто: между поисками должна быть пауза. "
                    f"Повторите через {math.ceil(wait)} с.")
            self._check_daily_budget()
            self._last_start = now
            result = self._do_search(query, pages=pages, product=product,
                                     days=days, only_views=only_views)
            self._cache_put(key, result)
            return result
        finally:
            self._lock.release()

    def _do_search(self, query, *, pages, product, days, only_views):
        session = None
        try:
            session = self.session_factory()
        except Exception as e:  # noqa: BLE001 — конструктор транспорта
            raise TransportFailure(
                f"Не удалось подготовить сессию X ({type(e).__name__}). "
                "Попробуйте позже.") from e
        if session is None:
            raise SessionUnavailable(
                "Сессия X недоступна: нет файла x_session.json с правами 600 "
                "или он нечитаем. Поиск не отправлен.")

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)
                  if days else None)
        collected = []
        pages_seen = 0
        warning = None
        written_run = None
        con = None

        def on_page(page_no, page_posts, _cursor):
            nonlocal pages_seen
            pages_seen = page_no
            collected.extend(page_posts)
            if self.write_posts and page_posts:
                good = [p for p in page_posts if _keep_days(p, cutoff)]
                if good:
                    db.save_search_posts(con, good, run_id=written_run, query=query)

        started = time.monotonic()
        try:
            if self.write_posts:
                con = db.connect(self.db_path)
                written_run = db.start_search_run(con, query, product=product)
            try:
                posts = session.search(query, product=product, max_pages=pages,
                                       max_posts=MAX_POSTS,
                                       max_seconds=SEARCH_TIMEOUT_SEC,
                                       on_page=on_page)
            except (xs.XSessionDailyCap, xs.XSessionWindowCap) as e:
                posts = collected
                warning = ("Бюджет X исчерпан: " + str(e)
                           + ". Показано то, что успели получить.")
            except (xs.SessionBlocked, xs.XSessionRateLimited) as e:
                if collected:
                    posts = collected
                    warning = (f"X отклонил запрос ({type(e).__name__}). "
                               "Показана часть результатов.")
                else:
                    raise TransportFailure(
                        "X отклонил запрос: " + str(e)
                        + ". Попробуйте позже.") from e
            except (xs.SessionUnavailable, xs.XSessionError) as e:
                if collected:
                    posts = collected
                    warning = (f"Сбой транспорта X ({type(e).__name__}). "
                               "Показана часть результатов.")
                else:
                    raise TransportFailure(
                        "Отказ транспорта X: " + str(e) + ". Попробуйте позже."
                    ) from e
            elapsed = time.monotonic() - started
            if self.write_posts and con is not None:
                status = "partial" if warning else "ok"
                note = (f"web: pages={pages_seen} elapsed={elapsed:.0f}s"
                        + (f" days<={days}" if days else ""))[:200]
                db.finish_search_run(con, written_run, pages=pages_seen,
                                     posts=len(collected), status=status,
                                     note=note)
        finally:
            if con is not None:
                con.close()

        # Фильтры периода и «только с просмотрами» — поверх уже собранного.
        if cutoff is not None:
            posts = [p for p in posts if _keep_days(p, cutoff)]
        if only_views:
            posts = [p for p in posts if _num(p.get("views")) is not None]

        posts = sort_posts(posts)[:MAX_POSTS]
        rows = [normalize_post(p) for p in posts]
        stats = {
            "pages": pages_seen,
            "posts": len(rows),
            "with_views": sum(1 for r in rows if r["views"] is not None),
            "with_ratio": sum(1 for r in rows if r["ratio"] is not None),
            "elapsed_sec": round(elapsed, 1),
        }
        return {
            "query": query,
            "product": product,
            "pages": pages,
            "days": days,
            "only_views": only_views,
            "posts": rows,
            "stats": stats,
            "cached": False,
            "warning": warning,
        }


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;
     color:#111;background:#fafafa}
h1{font-size:20px;margin:0 0 16px}
form{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;
     max-width:760px;margin-bottom:20px}
label{display:block;margin:10px 0 4px;font-size:13px;color:#333}
input[type=text],select,input[type=number]{padding:6px 8px;border:1px solid #bbb;
     border-radius:5px;font-size:14px;min-width:220px}
button{margin-top:14px;padding:8px 18px;border:0;border-radius:6px;
     background:#1d9bf0;color:#fff;font-size:14px;cursor:pointer}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #e1e1e1;padding:6px 8px;text-align:left;
     vertical-align:top}
th{background:#f2f2f2;position:sticky;top:0}
td.ratio{font-weight:600;white-space:nowrap}
tr.nodata td.ratio{color:#999;font-weight:400}
.meta{color:#555;font-size:13px;margin:8px 0}
.warn{background:#fff4e5;border:1px solid #ffcf8a;border-radius:6px;
      padding:10px 12px;margin:10px 0;color:#7a4b00}
.err{background:#fdecec;border:1px solid #f2b8b8;border-radius:6px;
     padding:14px;color:#8a1f1f;max-width:760px}
a{color:#1d9bf0}
.snip{max-width:520px;white-space:pre-wrap}
"""


def _page(title, body):
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


_INDEX_BODY = """<h1>HypeParser — поиск постов X по коэффициенту</h1>
<p class="meta">Коэффициент = просмотры / подписчики автора. Данные — из живого
поиска X; кэш запроса 10 минут, между поисками пауза ≥ 10 с.</p>
<form method="get" action="/search">
  <label for="q">Запрос</label>
  <input type="text" id="q" name="q" required autofocus
         placeholder="например: AI lang:en" size="40">
  <label for="days">Период</label>
  <select id="days" name="days">
    <option value="1">сутки</option>
    <option value="7">неделя</option>
    <option value="30">месяц</option>
  </select>
  <label for="product">Режим</label>
  <select id="product" name="product">
    <option value="Latest">Latest</option>
    <option value="Top">Top</option>
  </select>
  <label for="pages">Страниц (1–5)</label>
  <input type="number" id="pages" name="pages" min="1" max="5" value="1">
  <label><input type="checkbox" name="only_views" value="1"> только посты с просмотрами</label>
  <button type="submit">Найти</button>
</form>
"""


def render_index():
    return _page("HypeParser", _INDEX_BODY)


def _ratio_cell(row):
    if row["ratio"] is None:
        return '<td class="ratio">нет данных</td>'
    return f'<td class="ratio">{row["ratio"]:.4f}</td>'


def _num_cell(value):
    return "—" if value is None else f"{value:,}".replace(",", " ")


def render_results(result):
    stats = result["stats"]
    rows = []
    for r in result["posts"]:
        cls = ' class="nodata"' if r["ratio"] is None else ""
        if r["url"]:
            author = (f'<a href="{html.escape(r["url"], quote=True)}" '
                      f'target="_blank" rel="noopener noreferrer">'
                      f'@{html.escape(r["author"])}</a>')
        else:
            author = "@" + html.escape(r["author"])
        rows.append(
            f'<tr{cls}>{_ratio_cell(r)}'
            f'<td>{_num_cell(r["views"])}</td>'
            f'<td>{_num_cell(r["subs"])}</td>'
            f'<td>{author}</td>'
            f'<td>{html.escape(str(r["date"] or "—"))}</td>'
            f'<td class="snip">{html.escape(r["text"])}</td></tr>')
    if not rows:
        table = ('<p class="meta">Постов не найдено (или все отфильтрованы '
                 'периодом / «только с просмотрами»).</p>')
    else:
        table = ("<table><thead><tr>"
                 "<th>коэффициент</th><th>просмотры</th><th>подписчиков</th>"
                 "<th>автор</th><th>дата</th><th>фрагмент текста</th>"
                 "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    warn = (f'<div class="warn">{html.escape(result["warning"])}</div>'
            if result.get("warning") else "")
    cached = ('<span class="meta"> · из кэша (без обращения к X)</span>'
              if result.get("cached") else "")
    days = f" · период {result['days']} сут" if result.get("days") else ""
    body = (
        f'<h1>Результаты поиска</h1>'
        f'<p class="meta">запрос <b>{html.escape(result["query"])}</b> · '
        f'режим {html.escape(result["product"])}{days} · '
        f'коэффициент: есть {stats["with_ratio"]} из {stats["posts"]} · '
        f'просмотры: есть {stats["with_views"]} · '
        f'страниц {stats["pages"]} · {stats["elapsed_sec"]} с{cached}</p>'
        + warn + table
        + '<p class="meta"><a href="/">← новый поиск</a></p>')
    return _page("Результаты: " + result["query"], body)


def render_error(status, message):
    body = (f'<h1>Ошибка {status}</h1>'
            f'<div class="err">{html.escape(message)}</div>'
            f'<p class="meta"><a href="/">← вернуться к форме</a></p>')
    return _page(f"Ошибка {status}", body)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_handler(app, logger=None):
    logger = logger or logging.getLogger("hype_web")

    class HypeHandler(BaseHTTPRequestHandler):
        server_version = f"HypeParser/{APP_VERSION}"
        protocol_version = "HTTP/1.1"

        # ---- вспомогательное
        def _send(self, status, body, content_type):
            payload = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _html(self, status, body):
            self._send(status, body, "text/html")

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False), "application/json")

        def _text(self, status, body):
            self._send(status, body, "text/plain")

        def log_message(self, fmt, *args):
            logger.info("%s - %s", self.address_string(), fmt % args)

        # ---- маршруты
        def do_GET(self):  # noqa: N802 — имя задано BaseHTTPRequestHandler
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path
            try:
                if path in ("/", ""):
                    return self._html(200, render_index())
                if path == "/robots.txt":
                    return self._text(200, "User-agent: *\nDisallow: /\n")
                if path == "/healthz":
                    return self._json(200, app.health())
                if path == "/search":
                    return self._do_search(parts.query)
                return self._html(404, render_error(
                    404, "Страница не найдена. Доступны: /, /search, /healthz, "
                         "/robots.txt."))
            except HypeError as e:
                return self._html(e.status, render_error(e.status, e.message))
            except Exception:  # noqa: BLE001 — в браузер без трассировки
                logger.error("необработанная ошибка:\n%s", traceback.format_exc())
                return self._html(500, render_error(
                    500, "Внутренняя ошибка сервера. Подробности — в журнале "
                         "systemd, в браузер трассировка не выводится."))

        def do_HEAD(self):  # noqa: N802
            return self.do_GET()

        def _do_search(self, raw_query):
            params = urllib.parse.parse_qs(raw_query, keep_blank_values=True)

            def one(name, default=""):
                return (params.get(name) or [default])[0]

            query = one("q")
            pages = one("pages", "1")
            try:
                pages_int = int(pages)
            except ValueError:
                raise BadRequest("Число страниц должно быть целым: 1..5") from None
            result = app.search(
                query, pages=pages_int, product=one("product", "Latest"),
                days=one("days", ""), only_views=_parse_bool(one("only_views")))
            return self._html(200, render_results(result))

    return HypeHandler


def build_server(host=DEFAULT_HOST, port=DEFAULT_PORT, *, app=None, logger=None):
    """Собрать (но не запускать) ``ThreadingHTTPServer`` — удобно в тестах."""
    app = app or HypeApp()
    handler = make_handler(app, logger=logger)
    httpd = ThreadingHTTPServer((host, int(port)), handler)
    httpd.daemon_threads = True
    return httpd


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _is_loopback(host):
    return host in ("127.0.0.1", "::1", "localhost")


def build_parser():
    p = argparse.ArgumentParser(
        prog="python3 -m tuber.platforms.x.hype_web",
        description="Веб-страница HypeParser: поиск постов X по коэффициенту "
                    "просмотры/подписчики (только стандартная библиотека).")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"адрес прослушивания (по умолчанию {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"порт (по умолчанию {DEFAULT_PORT})")
    p.add_argument("--allow-public", action="store_true",
                   help="разрешить слушать не-петлевой адрес (осознанное действие)")
    p.add_argument("--write", action="store_true",
                   help="писать найденные посты в x_search_post (по умолчанию "
                        "сухой путь, в БД не пишем)")
    p.add_argument("--cache-ttl", type=float, default=CACHE_TTL_SEC,
                   help=f"TTL кэша запроса, с (по умолчанию {CACHE_TTL_SEC:.0f})")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr)
    if not _is_loopback(args.host) and not args.allow_public:
        print(f"отказ: {args.host} — не петлевой адрес; для прослушивания наружу "
              "запустите с явным --allow-public (или оставьте 127.0.0.1 и "
              "проксируйте через nginx)", file=sys.stderr)
        return 2
    app = HypeApp(cache_ttl=args.cache_ttl, write_posts=args.write)
    try:
        httpd = build_server(args.host, args.port, app=app)
    except OSError as e:
        print(f"не удалось занять {args.host}:{args.port} ({e}). "
              "Порт уже занят? Остановите второй экземпляр или задайте "
              "другой --port.", file=sys.stderr)
        return 2
    logging.getLogger("hype_web").info(
        "HypeParser слушает http://%s:%d/ (кэш %.0f с, запись в БД: %s)",
        args.host, args.port, args.cache_ttl, "да" if args.write else "нет")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
