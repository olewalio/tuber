#!/root/tuber/.venv-hype/bin/python3
# -*- coding: utf-8 -*-
"""Spike: live X (Twitter) search via twscrape. HypeParser Phase 0.

Autonomous, read-only spike. Verifies that live X search works from our
account and whether it returns view counts and author follower counts.

Hard guarantees:
  - never reads or writes /root/tuber/data/tuber.db (production DB);
  - never writes to any project table;
  - never prints cookie/token values;
  - the only network traffic is twscrape search;
  - no retries / auto-reruns of our own.

Run:
    /root/tuber/.venv-hype/bin/python3 /root/tuber/scripts/x/spike_twscrape.py
"""

import asyncio
import json
import os
import sys
import time
from datetime import timezone

# twscrape sends anonymous events to PostHog by default. The spike must only
# produce search traffic, so opt out before twscrape is imported/used.
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("TWS_TELEMETRY", "0")

from twscrape import API
from twscrape.accounts_pool import AccountsPool
from twscrape.models import parse_tweets

# --- configuration ---------------------------------------------------------

SESSION_PATH = "/root/tuber/config/x_session.json"
ACCOUNTS_DB = "/tmp/twscrape_spike_accounts.db"  # outside the repository
RESULT_PATH = "/tmp/spike_twscrape_result.json"
SPIKE_ACCOUNT = "hype_spike"  # local label only, never a cookie value

QUERIES = [
    ('(AI OR "artificial intelligence") lang:en', "en"),
    ('(AI OR "artificial intelligence") lang:ru', "ru"),
]
LIMIT = 40
TOTAL_TIMEOUT = 150.0
BETWEEN_QUERIES_SLEEP = 20.0
PAGE_SLEEP = 5.0
TEXT_LEN = 60
TABLE_ROWS = 20


# --- helpers ---------------------------------------------------------------


def load_session_cookies() -> str:
    """Read the session file (read-only) and return a cookie JSON string.

    Only auth_token, ct0, guest_id and twid are used. Values are never
    printed nor written anywhere.
    """
    with open(SESSION_PATH, "r", encoding="utf-8") as fh:
        session = json.load(fh)
    if not isinstance(session, dict):
        raise ValueError("session file is not a JSON object")

    picked = {}
    for key in ("auth_token", "ct0", "guest_id", "twid"):
        value = session.get(key)
        if isinstance(value, str) and value:
            picked[key] = value

    missing = [k for k in ("auth_token", "ct0") if k not in picked]
    if missing:
        raise ValueError("session file misses required keys: " + ", ".join(missing))
    return json.dumps(picked)


def _iso_date(tweet) -> str:
    dt = getattr(tweet, "date", None)
    if dt is None:
        return ""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except Exception:
        return str(dt)


def make_record(tweet, lang: str) -> dict:
    """Build one flat record with all requested post fields."""
    user = getattr(tweet, "user", None)
    username = getattr(user, "username", None) if user is not None else None
    followers = getattr(user, "followersCount", None) if user is not None else None
    views = getattr(tweet, "viewCount", None)
    likes = getattr(tweet, "likeCount", None)
    replies = getattr(tweet, "replyCount", None)

    ratio = None
    if isinstance(views, int) and isinstance(followers, int) and followers > 0:
        ratio = round(views / followers, 4)

    no_followers = (not isinstance(followers, int)) or followers <= 0

    return {
        "id": getattr(tweet, "id", None),
        "username": username,
        "viewCount": views,
        "followersCount": followers if isinstance(followers, int) else None,
        "likeCount": likes if isinstance(likes, int) else None,
        "replyCount": replies if isinstance(replies, int) else None,
        "date": _iso_date(tweet),
        "text": (getattr(tweet, "rawContent", "") or "")[:TEXT_LEN],
        "view_per_follower": ratio,
        "no_followers": no_followers,
        "lang": lang,
    }


def http_status_of(exc: Exception):
    """Best-effort HTTP status code extraction from an exception."""
    for attr in ("status_code",):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


# --- collection ------------------------------------------------------------


async def collect(api: API, result: dict) -> None:
    """Run the two search queries strictly sequentially, filling result."""
    for index, (query, lang) in enumerate(QUERIES):
        if index > 0:
            await asyncio.sleep(BETWEEN_QUERIES_SLEEP)

        query_info = {
            "index": index,
            "query": query,
            "lang": lang,
            "pages": 0,
            "posts": 0,
            "completed": False,
            "error": None,
            "error_page": None,
            "http_status": None,
        }
        result["query_stats"].append(query_info)

        page = 0
        try:
            async for rep in api.search_raw(query, limit=LIMIT):
                page += 1
                query_info["pages"] = page

                try:
                    tweets = list(parse_tweets(rep, LIMIT))
                except Exception as exc:  # parsing one page must not kill the run
                    result["events"].append(
                        {
                            "query_index": index,
                            "lang": lang,
                            "page": page,
                            "error": type(exc).__name__,
                            "http_status": http_status_of(exc),
                            "message": str(exc)[:300],
                        }
                    )
                    tweets = []

                for tweet in tweets:
                    result["posts"].append(make_record(tweet, lang))
                    query_info["posts"] += 1

                result["pages"].append(
                    {
                        "query_index": index,
                        "lang": lang,
                        "page": page,
                        "posts_on_page": len(tweets),
                    }
                )

                # pacing: never faster than one page per 5 seconds
                await asyncio.sleep(PAGE_SLEEP)

            query_info["completed"] = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = http_status_of(exc)
            query_info["error"] = type(exc).__name__
            query_info["error_page"] = page + 1 if page == 0 else page
            query_info["http_status"] = status
            result["events"].append(
                {
                    "query_index": index,
                    "lang": lang,
                    "page": query_info["error_page"],
                    "error": type(exc).__name__,
                    "http_status": status,
                    "message": str(exc)[:300],
                }
            )


# --- summary / verdict -----------------------------------------------------


def summarize(result: dict) -> None:
    posts = result["posts"]
    per_lang = {}
    with_view = 0
    with_followers = 0
    with_ratio = 0
    for post in posts:
        per_lang[post["lang"]] = per_lang.get(post["lang"], 0) + 1
        if post["viewCount"] is not None:
            with_view += 1
        if post["followersCount"] is not None:
            with_followers += 1
        if post["view_per_follower"] is not None:
            with_ratio += 1

    result["per_lang"] = per_lang
    result["total_posts"] = len(posts)
    result["with_view"] = with_view
    result["with_followers"] = with_followers
    result["with_ratio"] = with_ratio

    first_event = result["events"][0] if result["events"] else None
    result["first_event"] = first_event

    pages_done = len(result["pages"])

    if result.get("error"):
        result["verdict"] = {
            "level": "нельзя",
            "pages_limit": 0,
            "reason": "фатальная ошибка: " + str(result["error"]),
        }
    elif len(posts) == 0:
        if first_event is not None:
            reason = (
                "отказ на странице {p} (запрос {q}/{lang}, {err}, HTTP {st})".format(
                    p=first_event["page"],
                    q=first_event["query_index"] + 1,
                    lang=first_event["lang"],
                    err=first_event["error"],
                    st=first_event["http_status"],
                )
            )
        else:
            reason = "поиск не вернул ни одного поста (вероятно, сессия/аккаунт недействительны)"
        result["verdict"] = {"level": "нельзя", "pages_limit": 0, "reason": reason}
    elif result.get("timeout") or first_event is not None:
        if first_event is not None:
            safe_pages = max(first_event["page"] - 1, 0)
            reason = (
                "частичный результат: отказ на странице {p} (запрос {q}/{lang}, "
                "{err}, HTTP {st})".format(
                    p=first_event["page"],
                    q=first_event["query_index"] + 1,
                    lang=first_event["lang"],
                    err=first_event["error"],
                    st=first_event["http_status"],
                )
            )
        else:
            safe_pages = pages_done
            reason = "частичный результат: сработал общий таймаут {} с".format(TOTAL_TIMEOUT)
        if safe_pages < 1:
            result["verdict"] = {"level": "нельзя", "pages_limit": 0, "reason": reason}
        else:
            result["verdict"] = {
                "level": "можно с ограничением {} страниц".format(safe_pages),
                "pages_limit": safe_pages,
                "reason": reason,
            }
    elif len(posts) >= len(QUERIES) * LIMIT - 10 and with_view > 0:
        result["verdict"] = {
            "level": "может",
            "pages_limit": None,
            "reason": "оба запроса вернули полный объём, просмотры присутствуют",
        }
    else:
        result["verdict"] = {
            "level": "можно с ограничением {} страниц".format(max(pages_done, 1)),
            "pages_limit": max(pages_done, 1),
            "reason": "поиск работает, но объём/поля неполные",
        }


# --- reporting -------------------------------------------------------------


def _cell(value, width: int) -> str:
    text = "-" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


def render_table(posts: list) -> str:
    header = (
        _cell("text", 26)
        + " "
        + _cell("author", 18)
        + " "
        + _cell("date", 20)
        + " "
        + _cell("views", 10)
        + " "
        + _cell("follows", 10)
        + " "
        + _cell("likes", 8)
        + " "
        + _cell("repl", 7)
        + " "
        + _cell("v/f", 9)
    )
    lines = [header, "-" * len(header)]
    for post in posts[:TABLE_ROWS]:
        lines.append(
            _cell(post["text"], 26)
            + " "
            + _cell(post["username"], 18)
            + " "
            + _cell(post["date"], 20)
            + " "
            + _cell(post["viewCount"], 10)
            + " "
            + _cell(post["followersCount"], 10)
            + " "
            + _cell(post["likeCount"], 8)
            + " "
            + _cell(post["replyCount"], 7)
            + " "
            + _cell(post["view_per_follower"], 9)
        )
    return "\n".join(lines)


def print_report(result: dict) -> None:
    print("=" * 72)
    print("twscrape live-search spike for X")
    print("=" * 72)

    if result.get("error"):
        print("ОШИБКА: " + str(result["error"]))

    print("")
    print("Первые {} постов:".format(min(TABLE_ROWS, len(result["posts"]))))
    print(render_table(result["posts"]))
    print("")

    print("Сводка:")
    print("  всего постов: {}".format(result["total_posts"]))
    for lang, count in sorted(result["per_lang"].items()):
        print("    {}: {}".format(lang, count))
    print("  с viewCount: {}".format(result["with_view"]))
    print("  с followersCount: {}".format(result["with_followers"]))
    print("  коэффициент viewCount/followersCount считается: {}".format(result["with_ratio"]))
    print("  страниц получено: {}".format(len(result["pages"])))
    for qs in result["query_stats"]:
        print(
            "  запрос {q} ({lang}): страниц={pages}, постов={posts}, завершён={done}".format(
                q=qs["index"] + 1,
                lang=qs["lang"],
                pages=qs["pages"],
                posts=qs["posts"],
                done=qs["completed"],
            )
        )
    print("  время работы: {} с".format(result["runtime_sec"]))
    if result.get("timeout"):
        print("  ТАЙМАУТ: общий лимит {} с превышен".format(TOTAL_TIMEOUT))

    if result["events"]:
        for event in result["events"]:
            print(
                "  отказ: запрос {q} ({lang}), страница {p}, {err}, HTTP {st}".format(
                    q=event["query_index"] + 1,
                    lang=event["lang"],
                    p=event["page"],
                    err=event["error"],
                    st=event["http_status"],
                )
            )
    else:
        print("  отказов не было")

    print("")
    print("ВЕРДИКТ: {} ({})".format(result["verdict"]["level"], result["verdict"]["reason"]))
    print("=" * 72)


def write_result(result: dict) -> None:
    payload = {k: v for k, v in result.items() if k != "first_event"}
    with open(RESULT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    os.chmod(RESULT_PATH, 0o600)


# --- entry point -----------------------------------------------------------


async def run() -> int:
    result = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runtime_sec": 0.0,
        "queries": [q for q, _ in QUERIES],
        "limit": LIMIT,
        "total_timeout_sec": TOTAL_TIMEOUT,
        "posts": [],
        "pages": [],
        "events": [],
        "query_stats": [],
        "timeout": False,
        "error": None,
    }

    started = time.monotonic()
    try:
        cookies = load_session_cookies()
        pool = AccountsPool(ACCOUNTS_DB)
        await pool.add_account_cookies(SPIKE_ACCOUNT, cookies)
        api = API(pool)
        try:
            await asyncio.wait_for(collect(api, result), TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            result["timeout"] = True
    except Exception as exc:  # noqa: BLE001 - spike must never traceback
        result["error"] = "{}: {}".format(type(exc).__name__, str(exc)[:300])

    result["runtime_sec"] = round(time.monotonic() - started, 2)
    summarize(result)

    try:
        print_report(result)
    except Exception as exc:  # noqa: BLE001
        print("Не удалось напечатать отчёт: {}: {}".format(type(exc).__name__, exc))

    try:
        write_result(result)
        print("Результат записан: {} (права 600)".format(RESULT_PATH))
    except Exception as exc:  # noqa: BLE001
        print("Не удалось записать {}: {}: {}".format(RESULT_PATH, type(exc).__name__, exc))

    return 1 if result.get("error") else 0


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - final safety net
        print("Непредвиденная ошибка: {}: {}".format(type(exc).__name__, exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
