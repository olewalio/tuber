"""Внешний контур новинок (ТЗ-48, контур 5 плана «Сливки»).

Зачем
-----
Сюжеты ловят только то, что уже обсуждают у нас. Кейсы вида «вышел After
Effects MCP — монтаж автоматом» рождаются СНАРУЖИ и приходят к нам позже. Этот
модуль забирает свежие новинки из четырёх публичных источников **без ключей и
без авторизации** и отдаёт их единым списком:

* **Hacker News** — Algolia API (``search_by_date``), очки и число комментариев;
* **GitHub** — поиск репозиториев по звёздам (публичный API, без токена);
* **Product Hunt** — публичная Atom-лента ``/feed`` (названия продуктов);
* **arXiv** — Atom API (``export.arxiv.org``), свежие статьи cs.AI/cs.CL/cs.LG;
* **Hugging Face** — два публичных JSON-эндпоинта: трендовые модели
  (``/api/models``) и статьи дня (``/api/daily_papers``).

Платформ пять (``PLATFORMS``), а фитчеров шесть: у HF их два — отказ одного
виден в честном отчёте отдельной строкой. Все запросы — через stdlib ``urllib``
(как ``thumbs.py``/``broker.py``), без третьих зависимостей. Каждый источник
изолирован: отказ сети/HTTP не роняет прогон, а честно попадает в
``sources[name]`` словарь отчёта — заглушек нет.

Свежесть: у каждого источника есть свой серверный фильтр по окну (HN —
``created_at_i``, GitHub — ``created:>``, PH/arXiv/HF — разбор даты публикации:
``published``/``createdAt``/``paper.publishedAt``).
Предметный слой (сопоставление с нашей базой) живёт в
:mod:`tuber.analysis.novelties`.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

#: Платформы внешнего контура (код → человекочитаемое имя).
PLATFORMS: dict[str, str] = {
    "hackernews": "Hacker News",
    "github": "GitHub",
    "producthunt": "Product Hunt",
    "arxiv": "arXiv",
    "huggingface": "Hugging Face",
}

#: Источник ответа (для честного отчёта о недоступности).
SOURCE_OK = "ok"
SOURCE_HTTP_ERROR = "http_error"
SOURCE_NETWORK_ERROR = "network_error"
SOURCE_PARSE_ERROR = "parse_error"

#: User-Agent: публичные API просят идентифицировать клиента.
USER_AGENT = "tuber-novelty/1.0 (public contour; +https://github.com/1jehuang/jcode)"
#: Таймаут одного запроса, с.
HTTP_TIMEOUT_SEC = 25.0

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
PRODUCT_HUNT_FEED_URL = "https://www.producthunt.com/feed"
ARXIV_API_URL = "https://export.arxiv.org/api/query"
#: Трендовые модели HF — сортировка по ``trendingScore`` (публичный JSON).
HF_MODELS_URL = "https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=100"
#: Статьи дня HF (Daily Papers) — публичный JSON.
HF_PAPERS_URL = "https://huggingface.co/api/daily_papers?limit=100"

#: По умолчанию у HN берём истории с очками не ниже этого (иначе шум Show HN).
HN_MIN_POINTS = 5
#: У GitHub — репозитории не ниже этих звёзд (трендинг, а не пустышки).
GITHUB_MIN_STARS = 10
#: Сколько записей максимум брать у каждого источника.
HN_MAX_HITS = 200
GITHUB_MAX_ITEMS = 50
PH_MAX_ENTRIES = 60
ARXIV_MAX_RESULTS = 100
HF_MODELS_MAX_ITEMS = 40
HF_PAPERS_MAX_ITEMS = 40

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
#: Заголовок HN: ``Show HN: Название — теглайн``.
_SHOW_HN_RE = re.compile(r"^\s*show\s+hn\s*:\s*(.*)$", re.IGNORECASE)
#: Разделитель названия и теглайна в заголовке HN.
_TITLE_SPLIT_RE = re.compile(r"\s+[–—-]\s+|[,:(]")


def _hn_name_text(title: str) -> str:
    """Название новинки из ``Show HN`` (до теглайна), иначе весь заголовок.

    ``Show HN: Mini-AGI – Dynamic continual learning …`` → ``Mini-AGI``. Без
    отсечения теглайна ``Dynamic``/``VRAM``/``Write`` становились бы именами.
    """
    m = _SHOW_HN_RE.match(title or "")
    if not m:
        return title or ""
    return _TITLE_SPLIT_RE.split(m.group(1), maxsplit=1)[0].strip() or (title or "")


def _now(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    from tuber.core import timeutil

    iso = timeutil.parse_any(now)
    if iso is None:
        raise ValueError(f"не разобрана дата: {now!r}")
    return datetime.strptime(iso, timeutil.ISO_FMT).replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso_utc(value) -> str | None:
    """ISO-8601 (в т.ч. с ``T``/``Z``/смещением) → UTC ``YYYY-MM-DD HH:MM:SS``."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _iso(dt)


def _clean_text(value) -> str:
    """Убрать HTML-теги и схлопнуть пробелы (для заголовков внешних лент)."""
    text = _TAG_RE.sub(" ", str(value or ""))
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&#39;", "'").replace("&quot;", '"')
    return _WS_RE.sub(" ", text).strip()


def _item(platform: str, title: str, url: str | None, *, score=None,
          comments=None, published_at=None, name_text: str | None = None) -> dict:
    return {
        "platform": platform,
        "title": title,
        "url": url,
        "score": score,
        "comments": comments,
        "published_at": published_at,
        "name_text": name_text if name_text is not None else title,
    }


class HttpClient:
    """Минимальный HTTP-клиент поверх stdlib (легко подменить в тестах)."""

    def __init__(self, *, timeout: float = HTTP_TIMEOUT_SEC, user_agent: str = USER_AGENT):
        self.timeout = float(timeout)
        self.user_agent = user_agent

    def get(self, url: str, headers: dict | None = None) -> str:
        req_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            req_headers.update(headers)
        request = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")


def _record_source(sources: dict, name: str, status: str, count: int = 0,
                   error: str | None = None) -> None:
    sources[name] = {
        "platform": name,
        "title": PLATFORMS.get(name, name),
        "status": status,
        "count": int(count),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Источники
# ---------------------------------------------------------------------------

def fetch_hackernews(client, *, now, window_hours: int = 48) -> list[dict]:
    """Свежие истории HN за окно (Algolia ``search_by_date``), по очкам."""
    cutoff = int((_now(now) - timedelta(hours=window_hours)).timestamp())
    query = urllib.parse.urlencode({
        "tags": "story",
        "numericFilters": f"created_at_i>{cutoff},points>={HN_MIN_POINTS}",
        "hitsPerPage": HN_MAX_HITS,
    })
    body = client.get(f"{HN_SEARCH_URL}?{query}")
    data = json.loads(body)
    items = []
    for hit in data.get("hits", []):
        if hit.get("title") is None:
            continue
        object_id = hit.get("objectID")
        title = _clean_text(hit.get("title"))
        items.append(_item(
            "hackernews", title,
            f"https://news.ycombinator.com/item?id={object_id}" if object_id else hit.get("url"),
            score=hit.get("points"), comments=hit.get("num_comments"),
            published_at=_parse_iso_utc(hit.get("created_at")),
            name_text=_hn_name_text(title)))
    return items


def fetch_github(client, *, now, window_hours: int = 48) -> list[dict]:
    """Репозитории, созданные в окне, по звёздам (публичный search API)."""
    since = (_now(now) - timedelta(hours=window_hours)).strftime("%Y-%m-%d")
    query = urllib.parse.urlencode({
        "q": f"created:>{since} stars:>={GITHUB_MIN_STARS}",
        "sort": "stars",
        "order": "desc",
        "per_page": GITHUB_MAX_ITEMS,
    })
    body = client.get(f"{GITHUB_SEARCH_URL}?{query}",
                      headers={"Accept": "application/vnd.github+json"})
    data = json.loads(body)
    items = []
    for repo in data.get("items", []):
        title = repo.get("full_name") or repo.get("name") or ""
        description = _clean_text(repo.get("description"))
        items.append(_item(
            "github", (f"{title}: {description}" if description else title),
            repo.get("html_url"), score=repo.get("stargazers_count"),
            published_at=_parse_iso_utc(repo.get("created_at")), name_text=title))
    return items


def fetch_producthunt(client, *, now, window_hours: int = 48) -> list[dict]:
    """Продукты из публичной Atom-ленты Product Hunt за окно."""
    body = client.get(PRODUCT_HUNT_FEED_URL)
    cutoff = _now(now) - timedelta(hours=window_hours)
    items = []
    for entry in re.findall(r"<entry>(.*?)</entry>", body, re.DOTALL):
        title = _clean_text(_first(r"<title[^>]*>(.*?)</title>", entry))
        if not title:
            continue
        url = _first(r'<link[^>]*href="([^"]+)"', entry)
        published = _parse_iso_utc(_first(r"<published>(.*?)</published>", entry))
        if published is not None:
            dt = datetime.strptime(published, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
        body_text = _clean_text(_first(r"<content[^>]*>(.*?)</content>", entry))
        items.append(_item("producthunt", title, url,
                           published_at=published, comments=body_text or None,
                           name_text=title))
    return items[:PH_MAX_ENTRIES]


def fetch_arxiv(client, *, now, window_hours: int = 48) -> list[dict]:
    """Свежие статьи arXiv (cs.AI / cs.CL / cs.LG) за окно."""
    query = urllib.parse.urlencode({
        "search_query": "cat:cs.AI OR cat:cs.CL OR cat:cs.LG",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": ARXIV_MAX_RESULTS,
    })
    body = client.get(f"{ARXIV_API_URL}?{query}")
    cutoff = _now(now) - timedelta(hours=window_hours)
    items = []
    for entry in re.findall(r"<entry>(.*?)</entry>", body, re.DOTALL):
        title = _clean_text(_first(r"<title>(.*?)</title>", entry))
        if not title:
            continue
        url = _first(r"<id>(.*?)</id>", entry)
        published = _parse_iso_utc(_first(r"<published>(.*?)</published>", entry))
        if published is not None:
            dt = datetime.strptime(published, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
        items.append(_item("arxiv", title, url, published_at=published, name_text=title))
    return items


def _first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1) if m else None


def _hf_model_name(model_id: str) -> str:
    """Имя модели без автора: часть ``id`` после первого ``/``, дефисы и
    подчёркивания — на пробелы (для сопоставления имён во внешнем индексе)."""
    name = str(model_id or "").split("/", 1)[-1]
    return name.replace("-", " ").replace("_", " ").strip()


def fetch_huggingface_models(client, *, now, window_hours: int = 48) -> list[dict]:
    """Свежие трендовые модели Hugging Face за окно (по ``createdAt``)."""
    body = client.get(HF_MODELS_URL)
    data = json.loads(body)
    cutoff = _now(now) - timedelta(hours=window_hours)
    items = []
    for model in data if isinstance(data, list) else []:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "")
        if not model_id:
            continue
        published = _parse_iso_utc(model.get("createdAt"))
        if published is not None:
            dt = datetime.strptime(published, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
        name = _hf_model_name(model_id)
        items.append(_item(
            "huggingface", name, f"https://huggingface.co/{model_id}",
            score=model.get("trendingScore"), published_at=published, name_text=name))
    return items[:HF_MODELS_MAX_ITEMS]


def fetch_huggingface_papers(client, *, now, window_hours: int = 48) -> list[dict]:
    """Свежие статьи дня Hugging Face за окно (по ``paper.publishedAt``)."""
    body = client.get(HF_PAPERS_URL)
    data = json.loads(body)
    cutoff = _now(now) - timedelta(hours=window_hours)
    items = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        paper = entry.get("paper") if isinstance(entry.get("paper"), dict) else {}
        title = _clean_text(paper.get("title") or entry.get("title"))
        if not title:
            continue
        published = _parse_iso_utc(
            paper.get("publishedAt") or entry.get("publishedAt"))
        if published is not None:
            dt = datetime.strptime(published, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
        paper_id = paper.get("id")
        url = f"https://huggingface.co/papers/{paper_id}" if paper_id else None
        items.append(_item(
            "huggingface", title, url, score=paper.get("upvotes"),
            published_at=published, name_text=title))
    return items[:HF_PAPERS_MAX_ITEMS]


#: Реестр источников: имя → функция. Порядок определяет порядок в отчёте.
FETCHERS: dict[str, object] = {
    "hackernews": fetch_hackernews,
    "github": fetch_github,
    "producthunt": fetch_producthunt,
    "arxiv": fetch_arxiv,
    "huggingface_models": fetch_huggingface_models,
    "huggingface_papers": fetch_huggingface_papers,
}


def collect(*, now=None, window_hours: int = 48, client=None,
            sources: tuple[str, ...] | None = None) -> dict:
    """Собрать свежие новинки из внешнего контура.

    Возвращает::

        {"now": ISO, "window_hours": 48,
         "items": [{"platform","title","url","score","comments","published_at"}, …],
         "sources": {"hackernews": {"status": "ok", "count": 73, "error": None}, …}}

    Отказ одного источника не мешает остальным; заглушек не подставляется.
    """
    now_dt = _now(now)
    client = client or HttpClient()
    names = sources or tuple(FETCHERS)
    items: list[dict] = []
    report: dict[str, dict] = {}
    for name in names:
        fn = FETCHERS.get(name)
        if fn is None:
            _record_source(report, name, SOURCE_PARSE_ERROR, 0, "неизвестный источник")
            continue
        try:
            got = fn(client, now=now_dt, window_hours=window_hours)
            items.extend(got)
            _record_source(report, name, SOURCE_OK, len(got))
        except urllib.error.HTTPError as exc:
            _record_source(report, name, SOURCE_HTTP_ERROR, 0, f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            _record_source(report, name, SOURCE_NETWORK_ERROR, 0, str(exc)[:200])
        except (ValueError, KeyError, TypeError) as exc:
            _record_source(report, name, SOURCE_PARSE_ERROR, 0, str(exc)[:200])
    return {"now": _iso(now_dt), "window_hours": int(window_hours),
            "items": items, "sources": report}
