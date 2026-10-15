"""D-72: точечный канал `fxtwitter` — полный текст поста X по id.

Зачем модуль
------------
Прежний добор полного текста (`collect.refetch_fulltext`) перечитывал ПОЛНУЮ
ленту каждого аккаунта очереди через Nitter RSS: латентность внешнего зеркала
(до 27 с на ленту) и уход в резерв `x_ssr` делали время прогона непредсказуемым
(23.09.2026 — до 95 мин при потолке планировщика 5400 с). При этом нужен текст
лишь нескольких постов на аккаунт.

Точечный канал — публичный JSON-эндпоинт `GET https://api.fxtwitter.com/status/<id>`:

* ключи/токены НЕ нужны (отдельная инфраструктура, не аккаунт X);
* отдаёт полный текст (`tweet.text`), `lang`, `is_note_tweet`, автора и метрики
  (`likes`, `replies`, `retweets`, `quotes`, `bookmarks`, `views`, `created_at`),
  которых нет в Nitter RSS;
* замер 24.09.2026: 12/12 запросов подряд успешно, 1,44 зап/с, ~0,7 с на пост;
* несуществующий id → HTTP 404 с чистым JSON `{"code":404,...}`.

Транспорт
---------
Единственный выход в сеть проекта — `broker.raw_http_get` (инвариант
`tests/x/test_no_direct_network.py`: прямой сетевой вызов допустим только в
`broker.py`). Поэтому транспорт по умолчанию идёт через него. Параметры
`opener`/`sleep` остаются точками подмены для тестов.
"""
from __future__ import annotations

import json
import re
import time

from . import config

API_HOST = config.FX_API_HOST
API_PATH = "/status/{id}"

_ID_RE = re.compile(r"^\d+$")
# Пауза между повторами при сетевой ошибке (не 429). 429 держит отдельную,
# более длинную паузу FX_429_SLEEP_SEC (защита от залипания лимита).
NET_RETRY_SLEEP_SEC = 1.0


def _default_opener(url, timeout, headers):
    """Транспорт по умолчанию: единственный сетевой выход проекта.

    Возвращает ``(status:int, headers:dict, body:str)`` как ``broker.raw_http_get``.
    Импорт ленивый, чтобы модуль не тянул брокер при импорте.
    """
    from .broker import raw_http_get
    return raw_http_get(url, timeout=timeout, headers=headers)


def _blank_result():
    """Пустой нормализованный ответ (все поля объявлены — потребителю проще)."""
    return {
        "ok": False,
        "http": 0,
        "not_found": False,
        "text": None,
        "lang": None,
        "is_note_tweet": None,
        "likes": None,
        "replies": None,
        "reposts": None,
        "quotes": None,
        "bookmarks": None,
        "views": None,
        "created_at": None,
        "author_handle": None,
        "author_verified": None,
        "error": None,
    }


def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_status(tweet_id, *, opener=None, timeout=None, ua=None, sleep=None):
    """Полный статус поста X по ``tweet_id`` через ``api.fxtwitter.com``.

    Возвращает нормализованный словарь (см. :func:`_blank_result`). Никаких
    исключений наружу: сетевые сбои/404/429 упакованы в ``ok``/``not_found``/
    ``error``, чтобы очередь добыра не падала на одном посте.
    """
    opener = opener or _default_opener
    sleep = sleep or time.sleep
    timeout = config.FX_TIMEOUT_SEC if timeout is None else timeout
    ua = ua or config.FX_USER_AGENT

    res = _blank_result()
    tid = str(tweet_id).strip()
    if not _ID_RE.match(tid):
        res["error"] = "invalid_id"
        return res

    url = f"https://{API_HOST}{API_PATH.format(id=tid)}"
    headers = {"User-Agent": ua, "Accept": "application/json"}
    attempts = config.FX_MAX_RETRIES + 1

    for attempt in range(attempts):
        try:
            status, _hdrs, body = opener(url, timeout, headers)
        except Exception as e:  # подменённый транспорт тоже не должен ронять прогон
            status, body = 0, f"__opener_error__:{type(e).__name__}:{e}"
        http = int(status or 0)
        res["http"] = http

        if http == 404:
            res["not_found"] = True
            res["error"] = "not_found"
            return res

        if http == 429:
            if attempt < attempts - 1:
                sleep(config.FX_429_SLEEP_SEC)
                continue
            res["error"] = "rate_limited"
            return res

        if http == 200:
            return _parse_200(res, body)

        # 0 (сеть/таймаут) и прочие коды — повторяем до исчерпания попыток.
        if attempt < attempts - 1:
            sleep(NET_RETRY_SLEEP_SEC)
            continue
        res["error"] = "network" if http == 0 else f"http_{http}"
        return res

    res["error"] = res["error"] or "network"
    return res


def _parse_200(res, body):
    """Разобрать успешный ответ fxtwitter (HTTP 200 + ``code`` 200)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        res["error"] = "bad_json"
        return res
    if not isinstance(data, dict):
        res["error"] = "bad_json"
        return res
    code = data.get("code")
    if code == 404:
        res["not_found"] = True
        res["error"] = "not_found"
        return res
    if code is not None and code != 200:
        res["error"] = f"api_code_{code}"
        return res
    tweet = data.get("tweet")
    if not isinstance(tweet, dict):
        res["error"] = "no_tweet"
        return res
    text = tweet.get("text")
    if text is None:
        res["error"] = "no_text"
        return res
    author = tweet.get("author") or {}
    if not isinstance(author, dict):
        author = {}
    handle = author.get("screen_name")
    res.update({
        "ok": True,
        "text": text,
        "lang": tweet.get("lang"),
        "is_note_tweet": tweet.get("is_note_tweet"),
        "likes": _int_or_none(tweet.get("likes")),
        "replies": _int_or_none(tweet.get("replies")),
        "reposts": _int_or_none(tweet.get("retweets")),
        "quotes": _int_or_none(tweet.get("quotes")),
        "bookmarks": _int_or_none(tweet.get("bookmarks")),
        "views": _int_or_none(tweet.get("views")),
        "created_at": tweet.get("created_at"),
        "author_handle": (str(handle).lstrip("@") if handle else None),
        "author_verified": (bool(author.get("verified"))
                            if author.get("verified") is not None else None),
    })
    return res
