"""Тренд внутри тренда — подтемы сюжетов (ТЗ-48, контур 5 плана «Сливки»).

Зачем
-----
Сюжеты (кластеры) работают, но «второй производной» нет: не видно, какая
ПОДТЕМА внутри сюжета разгоняется сегодня. Подтема — именованная сущность
внутри сюжета (существующее извлечение
:func:`tuber.platforms.x.stories.extract_entities`, ничего нового не
изобретается).

Числа (термины плана)
---------------------
* **``share_t``** — доля постов подтемы от ВСЕХ постов 6-часового окна:
  ``постов_с_сущностью / постов_в_окне``;
* **``accel_sub = (share_t − share_{t−1}) / share_{t−1}``** — ускорение доли
  относительно предыдущего 6-часового окна;
* **тренд внутри тренда** — ``accel_sub ≥ 0,5`` И ``≥ 5`` независимых авторов.

Честность
---------
* Сравниваются ТОЛЬКО окна одинаковой длины; ``share`` нормируется на число
  постов окна (иначе поток сбора искажал бы ускорение).
* Если ``share_{t−1} = 0``, ускорение не определено: сущность попадает в блок
  «новые» (без деления на ноль и без выдуманного роста).
* Независимый автор — источник (``source_id``) либо, если его нет, автор поста;
  один и тот же автор в окне считается один раз.
* Подтема обязана быть сущностью СЮЖЕТА: перебираются только сущности,
  встречающиеся в ``story.entities``.
* Если в свежем окне нет постов (сбор встал), окно честно сдвигается к
  последнему посту и это печатается, а не подменяется нулём.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tuber.core import timeutil
from tuber.platforms.x import stories as xstories

from . import names

#: Длина окна сравнения по умолчанию, ч (термин плана — 6-часовое окно).
DEFAULT_WINDOW_HOURS = 6
#: Порог ускорения доли подтемы.
ACCEL_MIN = 0.5
#: Минимум независимых авторов подтемы.
MIN_AUTHORS = 5
#: Минимум постов в окне, ниже которого окно считается «пустым» (сдвиг якоря).
MIN_WINDOW_POSTS = 20


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
    """Независимый автор поста: источник, иначе автор, иначе сам пост."""
    sid = row["source_id"]
    if sid is not None:
        return ("s", sid)
    handle = (row["author_handle"] or "").strip().lower()
    if handle:
        return ("h", row["platform"], handle)
    return ("p", row["platform"], row["external_id"])


def story_vocabulary(con) -> tuple[set[str], dict[str, list[dict]]]:
    """Сущности сюжетов: ``(множество имён, {имя: [сюжет, …]})``.

    «Сюжет» здесь — строка ``story`` с непустым ``entities`` (JSON-массив).
    Имя нормализуется :func:`tuber.analysis.names.normalize_entity`; одно имя
    может принадлежать нескольким сюжетам — храним все для ссылки в выдаче.
    """
    vocab: set[str] = set()
    by_name: dict[str, list[dict]] = {}
    for row in con.execute(
            "SELECT id, platform, title, entities FROM story"
            " WHERE entities IS NOT NULL AND entities != ''"):
        try:
            raw = json.loads(row["entities"])
        except (ValueError, TypeError):
            continue
        if not isinstance(raw, list):
            continue
        for item in raw:
            name = names.normalize_entity(item)
            if not name:
                continue
            vocab.add(name)
            stories = by_name.setdefault(name, [])
            if len(stories) < 5:
                stories.append({"story_id": row["id"], "platform": row["platform"],
                                "title": row["title"]})
    return vocab, by_name


def content_entity_stats(con, since: str, until: str, vocabulary: set[str]) -> dict:
    """Статистика сущностей в окне ``[since, until)`` по постам ``content``.

    Возвращает ``{имя: {posts, authors:set, platforms:set, example:{…}}}``.
    Пример (ссылка на первоисточник) — самый ранний пост окна с сущностью.
    """
    rows = con.execute(
        "SELECT id, platform, source_id, external_id, author_handle, url, text,"
        "       mentions, links, published_at"
        "  FROM content"
        " WHERE published_at >= ? AND published_at < ?"
        " ORDER BY published_at ASC, id ASC",
        (since, until)).fetchall()
    stats: dict[str, dict] = {}
    total = 0
    for row in rows:
        total += 1
        ents = xstories.extract_entities(row["text"], row["mentions"], row["links"])
        author = _author_key(row)
        for raw in ents:
            name = names.normalize_entity(raw)
            if not name or name not in vocabulary:
                continue
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


def resolve_anchor(con, now=None, *, window_hours: int = DEFAULT_WINDOW_HOURS,
                   min_posts: int = MIN_WINDOW_POSTS) -> dict:
    """Якорь окна: конец свежего 6-часового окна.

    По умолчанию — ``now``. Если в окне ``[now − 6ч, now)`` меньше ``min_posts``
    постов (сбор встал или окно ещё не наполнилось), якорь сдвигается на
    ``max(published_at)`` — конец последнего окна, где данные есть. Сдвиг
    честно помечается ``shifted=True`` и печатается в отчёте.
    """
    now_dt = _now(now)
    start = _iso(now_dt - timedelta(hours=window_hours))
    end = _iso(now_dt)
    count = con.execute(
        "SELECT COUNT(*) FROM content WHERE published_at >= ? AND published_at < ?",
        (start, end)).fetchone()[0]
    if count >= min_posts:
        return {"anchor": end, "shifted": False, "posts_in_window": int(count),
                "requested_anchor": end}
    latest = con.execute("SELECT MAX(published_at) FROM content").fetchone()[0]
    if not latest:
        return {"anchor": end, "shifted": False, "posts_in_window": int(count),
                "requested_anchor": end}
    latest_dt = datetime.strptime(latest, timeutil.ISO_FMT).replace(tzinfo=timezone.utc)
    if latest_dt >= now_dt:
        return {"anchor": end, "shifted": False, "posts_in_window": int(count),
                "requested_anchor": end}
    shifted_end = _iso(latest_dt)
    start2 = _iso(latest_dt - timedelta(hours=window_hours))
    count2 = con.execute(
        "SELECT COUNT(*) FROM content WHERE published_at >= ? AND published_at < ?",
        (start2, shifted_end)).fetchone()[0]
    return {"anchor": shifted_end, "shifted": True, "posts_in_window": int(count2),
            "requested_anchor": end}


def build(con, *, now=None, window_hours: int = DEFAULT_WINDOW_HOURS,
          accel_min: float = ACCEL_MIN, min_authors: int = MIN_AUTHORS,
          limit: int | None = None) -> dict:
    """Посчитать подтемы-«тренды внутри тренда» (только чтение)."""
    window_hours = int(window_hours)
    resolved = resolve_anchor(con, now, window_hours=window_hours)
    anchor = datetime.strptime(resolved["anchor"], timeutil.ISO_FMT).replace(tzinfo=timezone.utc)
    vocab, by_name = story_vocabulary(con)

    now_start = _iso(anchor - timedelta(hours=window_hours))
    now_end = _iso(anchor)
    prev_start = _iso(anchor - timedelta(hours=2 * window_hours))
    prev_end = now_start

    cur = content_entity_stats(con, now_start, now_end, vocab)
    prev = content_entity_stats(con, prev_start, prev_end, vocab)
    n_now, n_prev = cur["total"], prev["total"]

    trending: list[dict] = []
    new_entities: list[dict] = []
    for name, entry in cur["entities"].items():
        authors = len(entry["authors"])
        share_t = (entry["posts"] / n_now) if n_now else None
        prev_posts = prev["entities"].get(name, {}).get("posts", 0)
        share_prev = (prev_posts / n_prev) if n_prev else None
        record = {
            "entity": name,
            "posts": entry["posts"],
            "authors": authors,
            "platforms": sorted(entry["platforms"]),
            "share_t": share_t,
            "share_prev": share_prev,
            "accel_sub": None,
            "prev_posts": prev_posts,
            "example": entry["example"],
            "stories": by_name.get(name, []),
        }
        if not share_prev:
            if authors >= min_authors:
                new_entities.append(record)
            continue
        accel = (share_t - share_prev) / share_prev
        record["accel_sub"] = accel
        if accel >= accel_min and authors >= min_authors:
            trending.append(record)

    trending.sort(key=lambda r: (-r["accel_sub"], -r["authors"]))
    new_entities.sort(key=lambda r: -r["authors"])
    if limit:
        trending = trending[: int(limit)]
        new_entities = new_entities[: int(limit)]
    return {
        "anchor": resolved["anchor"],
        "requested_anchor": resolved["requested_anchor"],
        "anchor_shifted": resolved["shifted"],
        "window_hours": window_hours,
        "window_start": now_start,
        "window_end": now_end,
        "prev_start": prev_start,
        "prev_end": prev_end,
        "posts_now": n_now,
        "posts_prev": n_prev,
        "vocabulary": len(vocab),
        "accel_min": accel_min,
        "min_authors": min_authors,
        "trending": trending,
        "new_entities": new_entities,
    }


def _fmt_share(value) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _fmt_accel(value) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _link(example) -> str:
    if not example:
        return "нет ссылки"
    return example.get("url") or "нет ссылки"


def format_report(data: dict, *, limit: int = 5) -> str:
    """Человекочитаемая выдача подтем: числа, авторы, платформы, ссылка."""
    lines = ["=== Тренд внутри тренда: подтемы сюжетов (ТЗ-48) ==="]
    if data["anchor_shifted"]:
        lines.append(
            f"! свежих постов в окне к {data['requested_anchor']} мало: якорь "
            f"сдвинут к последнему посту {data['anchor']}")
    lines.append(
        f"окно {data['window_hours']}ч: {data['window_start']} … {data['window_end']} "
        f"| постов {data['posts_now']} (предыдущее окно {data['posts_prev']})")
    lines.append(
        f"порог: accel_sub ≥ {data['accel_min']:g} и ≥ {data['min_authors']} "
        f"независимых авторов | сущностей сюжетов {data['vocabulary']}")
    lines.append("")
    lines.append(f"-- подтемы-тренды: {len(data['trending'])} --")
    if not data["trending"]:
        lines.append("  (нет)")
    for i, r in enumerate(data["trending"][:limit], 1):
        stories = r["stories"][0] if r["stories"] else {}
        title = (stories.get("title") or "").strip()
        lines.append(
            f"  {i}. {r['entity']}: accel_sub {_fmt_accel(r['accel_sub'])}, "
            f"share {_fmt_share(r['share_t'])} (было {_fmt_share(r['share_prev'])}), "
            f"авторов {r['authors']}, постов {r['posts']}, "
            f"платформы {','.join(r['platforms'])}")
        lines.append(
            f"     сюжет: {title or '—'} | первоисточник: {_link(r['example'])}")
    lines.append("")
    lines.append(f"-- новые подтемы без базы сравнения (share_{{t-1}} = 0), "
                 f"≥ {data['min_authors']} авторов: {len(data['new_entities'])} --")
    for r in data["new_entities"][:limit]:
        lines.append(f"  {r['entity']}: авторов {r['authors']}, постов {r['posts']}, "
                     f"платформы {','.join(r['platforms'])}, "
                     f"первоисточник: {_link(r['example'])}")
    return "\n".join(lines)
