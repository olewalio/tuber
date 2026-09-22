"""Новинки: сущности, впервые встреченные за 48 ч, с внешним подтверждением.

ТЗ-48, контур 5 плана «Сливки». Определение (термины плана):

* **новинка** — именованная сущность (название инструмента/модели/стартапа),
  впервые встреченная за 48 ч, у ≥ 3 независимых источников на ≥ 2 платформах;
  для инструментов — подтверждение из внешнего контура (HN / GitHub / Product
  Hunt / arXiv). Совпадение внешнего заголовка с сущностью из нашей базы —
  подтверждение.
* **свежесть («впервые за 48 ч»)** — либо сущности не было в нашей базе до
  окна (``new_internal``), либо её внешнее подтверждение — это ВЫПУСК в окне
  (новый репозиторий GitHub, запуск на Product Hunt, статья arXiv, Show HN).
  Иначе известная сущность («google» в обычной новости) новинкой не считается.

Два уровня выдачи (оба с числами, ничего не смешивается):

* **строгий** (``tier='strict'``) — точная формула плана только на ВНУТРЕННИХ
  источниках: ``new_internal`` И ≥ 3 независимых внутренних источника И ≥ 2
  внутренние платформы И внешнее подтверждение. Такой разрез честно может дать
  ноль на нашем корпусе: новые имена редко появляются сразу на двух платформах.
* **внешний** (``tier='external'``) — итог объединённого счёта: ≥ 3 независимых
  источника (внутренние + внешние) И ≥ 2 платформы (внутренние + внешние) И
  внешнее подтверждение. Именно внешний контур ловит кейсы «вышел After Effects
  MCP».

Отказ внешнего источника не маскируется: статус каждого (``ok`` /
``http_error`` / ``network_error`` / ``parse_error``) печатается в шапке отчёта.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.core import timeutil
from tuber.platforms.x import stories as xstories

from . import external as external_mod
from . import names

#: Окно «первого появления», ч.
DEFAULT_WINDOW_HOURS = 48
#: Минимум независимых источников (внутренние + внешние).
MIN_SOURCES = 3
#: Минимум платформ (внутренние + внешние).
MIN_PLATFORMS = 2
#: Минимум внешних источников (внешнее подтверждение).
MIN_EXTERNAL_SOURCES = 1
#: Сколько примеров внешних ссылок хранить на новинку.
MAX_EXTERNAL_EVIDENCE = 3
#: Заголовки внешних лент, означающие ВЫПУСК (для «свежести» известных сущностей).
RELEASE_PREFIXES = ("show hn", "introducing", "launch", "released", "release",
                    "announcing", "новый", "вышел")


def _now(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    iso = timeutil.parse_any(now)
    if iso is None:
        raise ValueError(f"не разобрана дата: {now!r}")
    return datetime.strptime(iso, timeutil.ISO_FMT).replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(timeutil.ISO_FMT)


def _author_key(row) -> tuple:
    sid = row["source_id"]
    if sid is not None:
        return ("s", sid)
    handle = (row["author_handle"] or "").strip().lower()
    if handle:
        return ("h", row["platform"], handle)
    return ("p", row["platform"], row["external_id"])


def _entity_names(text, mentions=None, links=None) -> set[str]:
    """Канонические имена поста: извлечение сюжетов + именные токены текста."""
    out: set[str] = set()
    for raw in xstories.extract_entities(text, mentions, links):
        name = names.normalize_entity(raw)
        if name:
            out.add(name)
    for token in names.title_tokens(text):
        out.add(token)
    return out


def internal_stats(con, *, since: str, until: str) -> dict:
    """Внутренние источники/платформы сущности в окне ``[since, until)``."""
    stats: dict[str, dict] = {}
    total = 0
    for row in con.execute(
            "SELECT id, platform, source_id, external_id, author_handle, url, text,"
            "       mentions, links, published_at"
            "  FROM content"
            " WHERE published_at >= ? AND published_at < ?"
            " ORDER BY published_at ASC, id ASC",
            (since, until)):
        total += 1
        author = _author_key(row)
        for name in _entity_names(row["text"], row["mentions"], row["links"]):
            entry = stats.setdefault(
                name, {"posts": 0, "authors": set(), "platforms": set(), "example": None})
            entry["posts"] += 1
            entry["authors"].add(author)
            entry["platforms"].add(row["platform"])
            if entry["example"] is None:
                entry["example"] = {
                    "content_id": row["id"],
                    "platform": row["platform"],
                    "url": row["url"],
                    "published_at": row["published_at"],
                }
    return {"total": total, "entities": stats}


def internal_before(con, *, cutoff: str, lookback_days: int = 0) -> set[str]:
    """Сущности, встречавшиеся в нашей базе ДО окна (``published_at < cutoff``).

    ``lookback_days > 0`` ограничивает скан снизу (быстрее, но менее строго:
    «новинка» тогда значит «не встречалась столько дней»). Ноль — вся история.
    """
    seen: set[str] = set()
    if lookback_days and lookback_days > 0:
        floor = _iso(datetime.strptime(cutoff, timeutil.ISO_FMT).replace(
            tzinfo=timezone.utc) - timedelta(days=int(lookback_days)))
        sql = ("SELECT text, mentions, links FROM content"
               " WHERE published_at < ? AND published_at >= ?")
        params = (cutoff, floor)
    else:
        sql = "SELECT text, mentions, links FROM content WHERE published_at < ?"
        params = (cutoff,)
    for row in con.execute(sql, params):
        for raw in xstories.extract_entities(row["text"], row["mentions"], row["links"]):
            name = names.normalize_entity(raw)
            if name:
                seen.add(name)
        seen |= names.title_tokens(row["text"])
    return seen


def is_release_item(item: dict) -> bool:
    """Является ли внешняя запись ВЫПУСКОМ (а не новостью про старое)."""
    platform = item.get("platform")
    if platform in ("github", "producthunt", "arxiv"):
        return True
    title = (item.get("title") or "").strip().lower()
    return any(title.startswith(prefix) for prefix in RELEASE_PREFIXES)


def _capitalized_mode(item: dict) -> str:
    """Где брать заглавную букву как имя: Product Hunt и HN ``Show HN`` — первый токен.

    Для HN ``name_text`` — уже отсечённое название (см. ``_hn_name_text``), поэтому
    заглавная допустима только у первого токена; остальные заголовки — без неё.
    """
    platform = item.get("platform")
    if platform == "producthunt":
        return "first"
    if platform == "hackernews":
        return "first" if (item.get("title") or "").strip().lower().startswith("show hn") else "none"
    return "none"


def external_index(items) -> dict[str, dict]:
    """``{имя: {platforms:set, items:[…]}}`` по внешним записям.

    Имена берутся из НАЗВАНИЯ записи (``name_text``), а не из описания: описание
    несёт обычную лексику (``Powerful``, ``fast``), и она не должна становиться
    именем новинки. Правило отбора — :func:`tuber.analysis.names.distinctive_names`.
    """
    index: dict[str, dict] = {}
    for item in items:
        title = item.get("name_text") or item.get("title") or ""
        platform = item.get("platform")
        found = names.distinctive_names(
            title, capitalized=_capitalized_mode(item))
        for name in found:
            entry = index.setdefault(name, {"platforms": set(), "items": []})
            entry["platforms"].add(platform)
            entry["items"].append(item)
    return index


def _score_of(item) -> float:
    value = item.get("score")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build(con, *, now=None, window_hours: int = DEFAULT_WINDOW_HOURS,
          external_result=None, client=None, min_sources: int = MIN_SOURCES,
          min_platforms: int = MIN_PLATFORMS,
          min_external_sources: int = MIN_EXTERNAL_SOURCES,
          lookback_days: int = 0, limit: int | None = None) -> dict:
    """Посчитать новинки. Внешний контур зовётся, если ``external_result`` не задан."""
    window_hours = int(window_hours)
    now_dt = _now(now)
    until = _iso(now_dt)
    since = _iso(now_dt - timedelta(hours=window_hours))

    if external_result is None:
        external_result = external_mod.collect(now=now_dt, window_hours=window_hours,
                                               client=client)

    internal = internal_stats(con, since=since, until=until)
    n_window = internal["total"]
    before = internal_before(con, cutoff=since, lookback_days=lookback_days)
    ext = external_index(external_result.get("items", []))

    strict: list[dict] = []
    external_tier: list[dict] = []
    excluded = set(names.PLATFORM_WORDS) | set(xstories._ORG_WORDS)
    for name, entry in internal["entities"].items():
        ext_entry = ext.get(name)
        if ext_entry is None or not names.is_product_like(name) or name in excluded:
            continue
        internal_sources = len(entry["authors"])
        internal_platforms = set(entry["platforms"])
        ext_items = ext_entry["items"]
        external_sources = len(ext_items)
        external_platforms = set(ext_entry["platforms"])
        platforms = internal_platforms | external_platforms
        total_sources = internal_sources + external_sources
        new_internal = name not in before
        release = any(is_release_item(it) for it in ext_items)
        fresh = new_internal or release
        if not fresh:
            continue
        record = {
            "entity": name,
            "posts": entry["posts"],
            "internal_sources": internal_sources,
            "internal_platforms": sorted(internal_platforms),
            "external_sources": external_sources,
            "external_platforms": sorted(external_platforms),
            "platforms": sorted(platforms),
            "total_sources": total_sources,
            "new_internal": new_internal,
            "release_external": release,
            "internal_url": (entry["example"] or {}).get("url"),
            "external_items": sorted(
                ext_items, key=_score_of, reverse=True)[:MAX_EXTERNAL_EVIDENCE],
            "external_score": sum(_score_of(it) for it in ext_items),
        }
        if (new_internal and internal_sources >= min_sources
                and len(internal_platforms) >= min_platforms
                and external_sources >= min_external_sources):
            # TODO(debt-D-63): на нашем корпусе строгий разрез даёт 0 (новое имя
            # редко появляется сразу на двух платформах) — см. TECH-DEBT.md.
            record["tier"] = "strict"
            strict.append(record)
        elif (total_sources >= min_sources and len(platforms) >= min_platforms
              and external_sources >= min_external_sources):
            record["tier"] = "external"
            external_tier.append(record)

    def _key(r):
        return (r["external_score"], r["total_sources"], r["internal_sources"])

    strict.sort(key=_key, reverse=True)
    external_tier.sort(key=_key, reverse=True)
    if limit:
        strict = strict[: int(limit)]
        external_tier = external_tier[: int(limit)]

    return {
        "now": until,
        "window_hours": window_hours,
        "window_start": since,
        "posts_window": n_window,
        "sources": external_result.get("sources", {}),
        "external_items": len(external_result.get("items", [])),
        "strict": strict,
        "external": external_tier,
        "min_sources": min_sources,
        "min_platforms": min_platforms,
    }


def _link(item) -> str:
    return (item or {}).get("url") or "нет ссылки"


def _ext_line(item: dict) -> str:
    platform = item.get("platform")
    score = item.get("score")
    comments = item.get("comments")
    bits = [f"{platform}: {item.get('title', '')[:90]}"]
    if score is not None:
        bits.append(f"очки {score}")
    if platform == "producthunt" and comments:
        bits.append(str(comments)[:70])
    bits.append(_link(item))
    return " | ".join(bits)


def format_report(data: dict, *, limit: int = 5) -> str:
    """Человекочитаемая выдача: числа по источникам, примеры, ссылки."""
    lines = ["=== Новинки: сущности за 48 ч с внешним подтверждением (ТЗ-48) ==="]
    lines.append(f"окно {data['window_hours']}ч: {data['window_start']} … {data['now']} "
                 f"| постов в базе {data['posts_window']}")
    lines.append("-- внешний контур --")
    for name, rep in data["sources"].items():
        if rep.get("status") == external_mod.SOURCE_OK:
            lines.append(f"  {rep.get('title', name)}: ok, записей {rep.get('count', 0)}")
        else:
            lines.append(f"  {rep.get('title', name)}: НЕ ОТВЕТИЛ "
                         f"({rep.get('status')}: {rep.get('error') or '—'})")
    lines.append("")
    lines.append(f"-- строгий разрез (новинка внутри базы + ≥{data['min_sources']} "
                 f"внутренних источников + ≥{data['min_platforms']} платформ + внешнее "
                 f"подтверждение): {len(data['strict'])} --")
    if not data["strict"]:
        lines.append("  (нет: новые имена редко появляются сразу на двух платформах)")
    for i, r in enumerate(data["strict"][:limit], 1):
        lines.append(_format_novelty(i, r))
    lines.append("")
    lines.append(f"-- внешний разрез (объединённый счёт: ≥{data['min_sources']} "
                 f"источников + ≥{data['min_platforms']} платформ + внешнее "
                 f"подтверждение): {len(data['external'])} --")
    for i, r in enumerate(data["external"][:limit], 1):
        lines.append(_format_novelty(i, r))
    return "\n".join(lines)


def _format_novelty(i: int, r: dict) -> str:
    head = (f"  {i}. {r['entity']}: источников {r['total_sources']} "
            f"(внутр. {r['internal_sources']} [{','.join(r['internal_platforms'])}] + "
            f"внешн. {r['external_sources']} [{','.join(r['external_platforms'])}]), "
            f"новинка в базе: {'да' if r['new_internal'] else 'нет (внешний выпуск)'}")
    out = [head]
    if r["internal_url"]:
        out.append(f"     наша база: {r['internal_url']}")
    for item in r["external_items"]:
        out.append(f"     внешний: {_ext_line(item)}")
    return "\n".join(out)
