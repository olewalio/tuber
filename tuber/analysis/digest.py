"""Ежедневная выдача «Сливки» одной командой (ТЗ-50, строка 123 плана и §4).

Зачем
-----
Контуры посчитаны, но владельцу нужна ОДНА утренняя выдача, а не пять команд.
Этот модуль собирает за сутки четыре блока ИЗ ГОТОВЫХ КОНТУРОВ и печатает
короткий текст, читаемый с телефона:

* **10 авторов** — рейтинг «сливки» (ТЗ-45, :mod:`tuber.analysis.slivki`);
* **5 подтем** — «тренд внутри тренда» (ТЗ-48, :mod:`tuber.analysis.subtopics`);
* **5 новинок** — с внешним подтверждением HN/GitHub/Product Hunt/arXiv
  (ТЗ-48, :mod:`tuber.analysis.novelties`);
* **3 обсуждения** — с числами по комментариям (ТЗ-49,
  :mod:`tuber.analysis.comments`).

Плюс ответ на прямой запрос владельца «в каком проценте населения я нахожусь»:
перцентиль по осям **охват на подписчика**, **реакции на 1 000 просмотров**,
**рост за 7 дней** внутри НАШЕЙ базы (YouTube + Telegram + X). Перцентиль
считает код, а не LLM; база каждой оси называет своё число источников.

Ссылки на первоисточники
------------------------
Ссылка каждой позиции берётся из данных (:mod:`tuber.core.urls`, колонка
``content.url``), а НЕ собирается из внутренних id, и **проверяется** HTTP-запросом:
позиция с неоткрывающейся ссылкой в выдачу не попадает (в подвале печатается,
сколько ссылок проверено и сколько отброшено). Проверку можно выключить
``--no-check-links`` (тогда честность ссылок — на ответственности вызывающего).

Тихий режим
-----------
Если все четыре блока пусты (или все ссылки отброшены), команда НЕ печатает
отчёт (stdout пуст) — пустую выдачу владельцу не отправляем. Обёртка
``scripts/common/tuber_digest_slivki.sh`` в этом случае молчит.

Доставка
--------
``--send`` отправляет текст прямым Telegram Bot API в явные ``chat_id`` и
``thread_id`` (топик не угадывается: нет настройки — параметр не передаётся) и
пишет в журнал строку ``delivered chat_id=… thread_id=… parts=… message_id=…``.
Отправка гейтуется боевой базой (``--allow-production``), как снимки ТЗ-45/48:
урок ТЗ-45F — иначе боевой ночной прогон молча отказывает.

Адрес доставки задаётся ТОЛЬКО окружением (ТЗ-54): чат — ``--chat-id`` либо
``TUBER_DIGEST_CHAT_ID`` (обёртка читает его из файла
``/root/.hermes/tuber_owner.env``), субъект «где я» — ``--me`` либо
``TUBER_SLIVKI_ME``. Личных значений (ID чата, логинов) в репозитории нет; если
чат не задан, ``--send`` честно отказывает (rc=2).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tuber import config
from tuber.core import db as _db
from tuber.core import timeutil

from . import comments as comments_mod
from . import novelties as novelties_mod
from . import slivki as slivki_mod
from . import subtopics as subtopics_mod

#: Сколько позиций в каждом блоке (ТЗ-50 п.1).
DEFAULT_LIMITS: dict[str, int] = {
    "authors": 10,
    "subtopics": 5,
    "novelties": 5,
    "discussions": 3,
}

#: Окно свежих обсуждений, дней.
DISCUSSION_DAYS = 14

#: Окно для распределений «охват на подписчика» и «реакции на 1 000».
RATE_WINDOW_DAYS = 30

#: Сколько постов источника нужно в окне, чтобы его медиана попала в базу.
MIN_POSTS_PER_SOURCE = 3

#: Метка платформы/режима для журналов ``run``/``run_log``.
RUN_PLATFORM = "digest"
RUN_MODE = "slivki"

# Личные адреса доставки задаются ТОЛЬКО окружением (ТЗ-54): репозиторий
# публикуется на GitHub, поэтому личных значений в коде нет. Чат доставки:
# `--chat-id` либо `TUBER_DIGEST_CHAT_ID` (обёртка читает его из файла
# `/root/.hermes/tuber_owner.env`); субъект «где я»: `--me` либо
# `TUBER_SLIVKI_ME`. Констант по умолчанию с личными значениями здесь больше нет.
# TODO(debt-D-65): закрыт в ТЗ-52 — субъект читается из окружения обёрткой и
# CLI; без переменных блок честно печатает «субъект не задан (--me или
# TUBER_SLIVKI_ME)». См. TECH-DEBT.md.
# TODO(debt-D-69): автопостановка канала владельца в реестр по-прежнему не
# сделана (канал владельца в базу не собирается) — остаток D-65, см. TECH-DEBT.md.

#: Журнал доставки: строки ``delivered``/``skipped`` (ТЗ-50 п.4). Отдельно от
#: журнала прогона обёртки (``tuber_digest_slivki.log``), чтобы факт доставки
#: можно было проверить одной строкой.
DEFAULT_JOURNAL = "/root/.hermes/logs/tuber_digest_slivki.journal"

#: Предел длины одного сообщения Telegram (с запасом под 4096).
MESSAGE_LIMIT = 3900

#: Пользовательский агент проверки ссылок.
LINK_UA = "Mozilla/5.0 (compatible; tuber-digest/1.0)"

#: Реакция поста по платформе: лайки (X/YouTube) либо реакции (Telegram).
PLATFORM_REACTION = {"youtube": "likes", "x": "likes", "telegram": "reactions"}


# ---------------------------------------------------------------------------
# Время и мелкие помощники
# ---------------------------------------------------------------------------

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


def _day(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _thousands(value) -> str:
    """Число с пробелами-разрядами (1 190). ``None`` → «—»."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _plural(n, one: str, few: str, many: str) -> str:
    """Русская форма множественного числа: 1 источник / 2 источника / 5 источников."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


# ---------------------------------------------------------------------------
# Сбор четырёх блоков
# ---------------------------------------------------------------------------

def _author_items(con, *, now, limit: int) -> list[dict]:
    """10 авторов из рейтинга «сливки» (рост подтверждён + рост не измерим).

    Сортировка — по breakout (как в выдаче ТЗ-45). Автор с непустой ссылкой на
    лучший виральный пост; без ссылки позиция честно помечается, но блок
    авторов её сохраняет (это не первоисточник, а профиль рейтинга).
    """
    data = slivki_mod.build(con, now=now, limit=limit)
    merged = list(data["candidates_verified"]) + list(data["candidates_growth_unknown"])
    merged.sort(key=lambda r: (r["breakout"] if r["breakout"] is not None else -1e9,
                               r["subs"] or 0), reverse=True)
    out: list[dict] = []
    for r in merged[:limit]:
        viral = r.get("viral_post") or {}
        out.append({
            "kind": "author",
            "handle": r.get("handle"),
            "platform": r.get("platform"),
            "subs": r.get("subs"),
            "g7": r.get("g7"),
            "breakout": r.get("breakout"),
            "outlier": viral.get("outlier"),
            "growth": r.get("growth_status"),
            "url": viral.get("url"),
            "urls": [u for u in (viral.get("url"), r.get("url")) if u],
        })
    return out


def _subtopic_items(con, *, now, limit: int) -> tuple[list[dict], dict]:
    data = subtopics_mod.build(con, now=now)
    out: list[dict] = []
    for r in data["trending"][:limit]:
        example = r.get("example") or {}
        stories = r.get("stories") or []
        out.append({
            "kind": "subtopic",
            "entity": r.get("entity"),
            "accel_sub": r.get("accel_sub"),
            "share_t": r.get("share_t"),
            "authors": r.get("authors"),
            "posts": r.get("posts"),
            "platforms": r.get("platforms") or [],
            "story_title": (stories[0].get("title") if stories else None),
            "url": example.get("url"),
            "urls": [example.get("url")] if example.get("url") else [],
        })
    meta = {
        "window_hours": data.get("window_hours"),
        "posts_now": data.get("posts_now"),
        "anchor_shifted": data.get("anchor_shifted"),
        "total": len(data["trending"]),
    }
    return out, meta


def _novelty_items(con, *, now, limit: int, external_result) -> tuple[list[dict], dict]:
    data = novelties_mod.build(con, now=now, external_result=external_result)
    merged = list(data["strict"]) + list(data["external"])
    # Дедуп по имени: строгий разрез приоритетнее внешнего.
    seen: set[str] = set()
    ordered: list[dict] = []
    for r in merged:
        name = r.get("entity")
        if name in seen:
            continue
        seen.add(name)
        ordered.append(r)
    out: list[dict] = []
    for r in ordered[:limit]:
        ext_items = r.get("external_items") or []
        ext_url = ext_items[0].get("url") if ext_items else None
        urls = [u for u in (ext_url, r.get("internal_url")) if u]
        out.append({
            "kind": "novelty",
            "entity": r.get("entity"),
            "tier": r.get("tier"),
            "total_sources": r.get("total_sources"),
            "internal_sources": r.get("internal_sources"),
            "external_sources": r.get("external_sources"),
            "platforms": r.get("platforms") or [],
            "url": urls[0] if urls else None,
            "internal_url": r.get("internal_url"),
            "external_url": ext_url,
            "external_title": (ext_items[0].get("title") if ext_items else None),
            "urls": urls,
        })
    return out, data.get("sources", {})


def _discussion_items(con, *, now, limit: int) -> list[dict]:
    data = comments_mod.report(con, top=max(limit, 5), questions=1, days=DISCUSSION_DAYS)
    out: list[dict] = []
    for r in data["discussions"][:limit]:
        out.append({
            "kind": "discussion",
            "platform": r.get("platform"),
            "title": r.get("title"),
            "source": r.get("source"),
            "comments": r.get("comments"),
            "authors": r.get("authors"),
            "question_share": r.get("question_share"),
            "url": r.get("url"),
            "urls": [r.get("url")] if r.get("url") else [],
        })
    return out


def collect_blocks(con, *, now=None, external_result=None, limits=None) -> dict:
    """Собрать четыре блока выдачи (только чтение)."""
    now_dt = _now(now)
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    if external_result is None and limits.get("novelties", 0) <= 0:
        external_result = {"items": [], "sources": {}}

    authors = _author_items(con, now=now_dt, limit=limits["authors"])
    subtopics, subtopic_meta = _subtopic_items(con, now=now_dt, limit=limits["subtopics"])
    novelties, novelty_sources = _novelty_items(
        con, now=now_dt, limit=limits["novelties"], external_result=external_result)
    discussions = _discussion_items(con, now=now_dt, limit=limits["discussions"])
    return {
        "generated_at": _iso(now_dt),
        "day": _day(now_dt),
        "limits": limits,
        "authors": authors,
        "subtopics": subtopics,
        "novelties": novelties,
        "discussions": discussions,
        "subtopic_meta": subtopic_meta,
        "novelty_sources": novelty_sources,
    }


def is_empty(data: dict) -> bool:
    """Пустой день: ни одной позиции ни в одном блоке."""
    return not (data.get("authors") or data.get("subtopics")
                or data.get("novelties") or data.get("discussions"))


# ---------------------------------------------------------------------------
# Проверка ссылок
# ---------------------------------------------------------------------------

def http_opens(url: str, *, timeout: float = 10.0) -> bool:
    """Открывается ли ссылка: GET, редиректы разрешены, 2xx/3xx — да.

    Ошибки сети/HTTP — «не открывается» (ссылка выдумана либо умерла). Чтение
    ограничено первым байтом: нам нужен только статус.
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    req = urllib.request.Request(url, headers={"User-Agent": LINK_UA},
                                 method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
            return 200 <= getattr(resp, "status", 200) < 400
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def filter_by_links(items: list[dict], *, checker=http_opens,
                    timeout: float = 10.0, cache: dict | None = None) -> dict:
    """Оставить позиции с открывающейся ссылкой (первой рабочей из ``urls``).

    Возвращает ``{"items": [...], "checked": N, "dropped": M}``. Кэш — на один
    прогон: одинаковые ссылки не проверяются дважды.
    """
    cache = {} if cache is None else cache
    kept: list[dict] = []
    checked = 0
    dropped = 0
    for item in items:
        urls = item.get("urls") or ([item["url"]] if item.get("url") else [])
        chosen = None
        for url in urls:
            if url in cache:
                ok = cache[url]
            else:
                ok = bool(checker(url, timeout=timeout))
                cache[url] = ok
            checked += 1
            if ok:
                chosen = url
                break
        if chosen is None:
            dropped += 1
            continue
        item = dict(item)
        item["url"] = chosen
        kept.append(item)
    return {"items": kept, "checked": checked, "dropped": dropped}


def verify_all_links(data: dict, *, checker=http_opens, timeout: float = 10.0) -> dict:
    """Проверить ссылки всех блоков; вернуть данные и статистику проверки."""
    cache: dict = {}
    stats: dict[str, dict] = {}
    out = dict(data)
    for key in ("authors", "subtopics", "novelties", "discussions"):
        res = filter_by_links(data.get(key) or [], checker=checker,
                              timeout=timeout, cache=cache)
        out[key] = res["items"]
        stats[key] = {"checked": res["checked"], "dropped": res["dropped"]}
    out["links"] = {"checked": sum(s["checked"] for s in stats.values()),
                    "dropped": sum(s["dropped"] for s in stats.values()),
                    "by_block": stats}
    return out


# ---------------------------------------------------------------------------
# Перцентиль «где я» внутри нашей базы
# ---------------------------------------------------------------------------

def percentile_rank(values, x) -> float | None:
    """Доля значений ``values`` не больше ``x``, в процентах (0…100)."""
    vals = [v for v in values if v is not None]
    if not vals or x is None:
        return None
    le = sum(1 for v in vals if v <= x)
    return 100.0 * le / len(vals)


def _median_or_none(vals: list[float], minimum: int) -> float | None:
    return statistics.median(vals) if len(vals) >= minimum else None


def base_axis_distributions(con, *, now, window_days: int = RATE_WINDOW_DAYS,
                            min_posts: int = MIN_POSTS_PER_SOURCE) -> dict:
    """Распределения осей по источникам НАШЕЙ базы.

    * ``reach``    — охват на подписчика: медиана ``просмотры / подписчики``
      постов источника;
    * ``reactions``— реакции на 1 000 просмотров: медиана
      ``реакции / просмотры × 1000`` (реакция — лайки X/YouTube, реакции Telegram);
    * ``g7``       — рост подписчиков за 7 дней из ``source_metric_history``.

    Медиана источника берётся только при ``min_posts`` постах в окне: медиана по
    одному посту — сам пост. Каждая ось хранит разбивку по платформам и общий
    список; в выдаче печатается, из какой базы взят перцентиль.
    """
    now_dt = _now(now)
    cutoff = _iso(now_dt - timedelta(days=int(window_days)))
    groups: dict[int, dict] = {}
    for row in con.execute(
            "SELECT c.source_id AS sid, c.platform AS platform, s.subs AS subs, "
            "       cl.views AS views, cl.likes AS likes, cl.reactions AS reactions "
            "  FROM content c "
            "  JOIN content_latest cl ON cl.content_id = c.id "
            "  JOIN source s ON s.id = c.source_id "
            " WHERE s.subs IS NOT NULL AND s.subs > 0 AND c.published_at >= ?",
            (cutoff,)):
        views = row["views"]
        if not views or views <= 0:
            continue
        entry = groups.setdefault(row["sid"], {
            "platform": row["platform"], "subs": row["subs"],
            "reach": [], "reactions": []})
        entry["reach"].append(float(views) / float(row["subs"]))
        # TODO(debt-D-66): закрыт в ТЗ-52 — X-путь заполняет `content_latest.views`
        # из снимка (`storage.recompute_content_latest`), поэтому X-источники с
        # просмотрами попадают в обе оси, а без просмотров честно не учитываются
        # (их нет в числе базы). См. TECH-DEBT.md.
        reaction = row["reactions"] if row["platform"] == "telegram" else row["likes"]
        if reaction is not None:
            entry["reactions"].append(float(reaction) / float(views) * 1000.0)

    per_platform: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"reach": [], "reactions": []})
    all_values: dict[str, list[float]] = {"reach": [], "reactions": []}
    subject_cache: dict[tuple[str, int], dict] = {}
    for sid, entry in groups.items():
        reach = _median_or_none(entry["reach"], min_posts)
        reactions = _median_or_none(entry["reactions"], min_posts)
        platform = entry["platform"]
        if reach is not None:
            per_platform[platform]["reach"].append(reach)
            all_values["reach"].append(reach)
        if reactions is not None:
            per_platform[platform]["reactions"].append(reactions)
            all_values["reactions"].append(reactions)
        subject_cache[(platform, sid)] = {
            "reach": reach, "reactions": reactions, "subs": entry["subs"],
            "platform": platform, "posts": len(entry["reach"])}

    return {
        "window_days": int(window_days),
        "min_posts": int(min_posts),
        "by_platform": {p: {k: sorted(v) for k, v in axes.items()}
                        for p, axes in per_platform.items()},
        "all": {k: sorted(v) for k, v in all_values.items()},
        "per_source": subject_cache,
    }


def base_g7_distribution(con, *, now) -> dict:
    """Распределение роста за 7 дней по источникам (``source_metric_history``)."""
    now_dt = _now(now)
    today = _day(now_dt)
    series = slivki_mod.history_series(con)
    platforms = {r["id"]: r["platform"]
                 for r in con.execute("SELECT id, platform FROM source")}
    per_platform: dict[str, list[float]] = defaultdict(list)
    by_source: dict[int, float] = {}
    for sid, rows in series.items():
        g7 = slivki_mod.g7_from_series(rows, today)
        if g7 is None:
            continue
        by_source[sid] = g7
        per_platform[platforms.get(sid, "?")].append(g7)
    return {
        "day": today,
        "by_platform": {p: sorted(v) for p, v in per_platform.items()},
        "all": sorted(by_source.values()),
        "by_source": by_source,
    }


def _subject_from_options(con, subject, explicit) -> dict:
    """Разобрать субъект «где я»: tracked-источник и/или явные значения осей."""
    out = {"platform": None, "handle": None, "subs": None, "found": False,
           "explicit": {}, "source_id": None}
    if subject:
        platform, _, handle = str(subject).partition(":")
        platform, handle = platform.strip().lower(), handle.strip().lstrip("@")
        out["platform"], out["handle"] = platform, handle
        row = con.execute(
            "SELECT id, subs FROM source WHERE platform = ? AND lower(handle) = lower(?)",
            (platform, handle)).fetchone()
        if row is not None:
            out["found"] = True
            out["source_id"] = row["id"]
            out["subs"] = row["subs"]
    for key, value in (explicit or {}).items():
        if value is not None:
            out["explicit"][key] = float(value)
    return out


def percentile_block(con, *, now, subject=None, explicit=None,
                     distributions=None, g7=None) -> dict:
    """Таблица «где я»: перцентиль субъекта внутри нашей базы по трём осям.

    Субъект задаётся ``--me platform:handle`` (значения осей берутся из базы)
    либо явными значениями ``--me-reach/--me-reactions/--me-g7``. База каждой
    оси указывается числом источников; % считается кодом (не LLM).
    """
    distributions = distributions or base_axis_distributions(con, now=now)
    g7 = g7 or base_g7_distribution(con, now=now)
    subj = _subject_from_options(con, subject, explicit)
    per_source = distributions["per_source"]
    cached = per_source.get((subj["platform"], subj["source_id"])) if subj["source_id"] else None

    def axis_value(axis):
        if axis in subj["explicit"]:
            return subj["explicit"][axis], "явное значение"
        if axis == "g7":
            if subj["source_id"] is not None:
                return g7["by_source"].get(subj["source_id"]), "база"
            return None, "нет данных"
        if cached:
            return cached.get(axis), "база"
        return None, "нет данных"

    def pack(key, title, value, unit):
        if key in ("reach", "reactions"):
            plat_values = distributions["by_platform"].get(subj["platform"], {}).get(key, [])
            all_values = distributions["all"].get(key, [])
        else:
            plat_values = g7["by_platform"].get(subj["platform"], [])
            all_values = g7["all"]
        # Платформа субъекта неизвестна (субъект не задан) либо её база пуста —
        # перцентиль считаем по объединённой базе и честно это называем.
        if key in ("reach", "reactions"):
            platforms_with = sorted(p for p, axes in distributions["by_platform"].items()
                                   if axes.get(key))
            base_by_platform = {p: len(distributions["by_platform"].get(p, {}).get(key, []))
                                for p in ("youtube", "telegram", "x")}
        else:
            platforms_with = sorted(p for p, vals in g7["by_platform"].items() if vals)
            base_by_platform = {p: len(g7["by_platform"].get(p, []))
                                for p in ("youtube", "telegram", "x")}
        if subj["platform"] and plat_values:
            base_values, base_label = plat_values, subj["platform"]
        else:
            base_values, base_label = all_values, "все платформы"
        med_p = _median_or_none(base_values, 1)
        # TODO(debt-D-66): закрыт в ТЗ-52 — ось «реакции на 1 000 просмотров»
        # считается и для X, где есть просмотры в `content_latest`; посты без
        # просмотров в базу оси не входят (см. число базы по платформам ниже).
        if value is None:
            return {"key": key, "title": title, "value": None, "unit": unit,
                    "platform": base_label, "platform_n": len(base_values),
                    "base_platforms": platforms_with, "all_n": len(all_values),
                    "base_by_platform": base_by_platform,
                    "platform_median": med_p,
                    "all_median": _median_or_none(all_values, 1)}
        return {
            "key": key, "title": title, "value": value, "unit": unit,
            "platform": base_label,
            "platform_n": len(base_values),
            "base_platforms": platforms_with,
            "base_by_platform": base_by_platform,
            "platform_percentile": percentile_rank(base_values, value),
            "all_n": len(all_values),
            "all_percentile": percentile_rank(all_values, value),
            "platform_median": med_p, "all_median": _median_or_none(all_values, 1),
        }

    axes = [
        pack("reach", "охват на подписчика", axis_value("reach")[0], "просмотров на подписчика"),
        pack("reactions", "реакции на 1 000 просмотров", axis_value("reactions")[0], "на 1 000"),
        pack("g7", "рост за 7 дней", axis_value("g7")[0], "доля"),
    ]
    return {
        "subject": subj,
        "axes": axes,
        "base_sizes": {
            "youtube": len(distributions["by_platform"].get("youtube", {}).get("reach", [])),
            "telegram": len(distributions["by_platform"].get("telegram", {}).get("reach", [])),
            "x": len(distributions["by_platform"].get("x", {}).get("reach", [])),
        },
        # Поимённая база КАЖДОЙ оси (D-66): сколько YouTube/Telegram/X-источников
        # реально входят в ось. X-источник без просмотров в оси не учитывается и
        # в числе базы не появляется.
        "axis_bases": {ax["key"]: ax["base_by_platform"] for ax in axes},
        "g7_base_total": len(g7["all"]),
        "window_days": distributions["window_days"],
    }


# ---------------------------------------------------------------------------
# Формат сообщения (читаемый с телефона)
# ---------------------------------------------------------------------------

def _author_line(i: int, item: dict) -> list[str]:
    head = f"{i}. @{item['handle']} · {item['platform']} · {_thousands(item['subs'])} подписчиков"
    nums = []
    if item.get("outlier") is not None:
        nums.append(f"выброс ×{item['outlier']:g}")
    if item.get("breakout") is not None:
        nums.append(f"breakout {item['breakout']:.0f}")
    tail = " · ".join(nums) if nums else "нет чисел"
    out = [head, "   " + tail]
    if item.get("url"):
        out.append("   " + item["url"])
    return out


def _subtopic_line(i: int, item: dict) -> list[str]:
    accel = "—" if item.get("accel_sub") is None else f"{item['accel_sub']:+.1f}"
    head = (f"{i}. {item['entity']} · ускорение {accel} · "
            f"{item['authors']} авторов")
    out = [head]
    if item.get("share_t") is not None:
        out.append(f"   доля {item['share_t'] * 100:.1f}% · платформы "
                   f"{', '.join(item['platforms']) or '—'}")
    if item.get("url"):
        out.append("   " + item["url"])
    return out


def _novelty_line(i: int, item: dict) -> list[str]:
    head = (f"{i}. {item['entity']} · {item['total_sources']} "
            f"{_plural(item['total_sources'], 'источник', 'источника', 'источников')} "
            f"({item['internal_sources']} своих + {item['external_sources']} внешних)")
    out = [head]
    if item.get("url"):
        out.append("   " + item["url"])
    if item.get("internal_url") and item["internal_url"] != item.get("url"):
        out.append("   " + item["internal_url"])
    return out


def _discussion_line(i: int, item: dict) -> list[str]:
    title = (item.get("title") or "").strip()
    if len(title) > 60:
        title = title[:57].rstrip() + "…"
    head = f"{i}. {title} · {item['comments']} комм. · {item['authors']} авторов"
    out = [head]
    if item.get("url"):
        out.append("   " + item["url"])
    return out


def _percentile_lines(pct: dict) -> list[str]:
    if not pct:
        return []
    subj = pct.get("subject") or {}
    lines = ["ГДЕ Я (перцентиль в нашей базе)"]
    if subj.get("handle"):
        mark = "" if subj.get("found") else " — НЕ найден в базе"
        subs = f" · {_thousands(subj['subs'])} подписчиков" if subj.get("subs") else ""
        lines.append(f"субъект: @{subj['handle']} ({subj.get('platform')}){subs}{mark}")
    else:
        lines.append("субъект не задан (--me или TUBER_SLIVKI_ME)")
    for ax in pct.get("axes", []):
        value = ax.get("value")
        if value is None:
            base_ref = ax.get("platform_median") or ax.get("all_median")
            if base_ref is not None:
                shown = (f"{base_ref:.2f}" if ax["key"] != "g7"
                         else f"{base_ref * 100:+.1f}%")
                lines.append(f"{ax['title']}: нет данных о субъекте "
                             f"(типичное в базе: {shown})")
            else:
                lines.append(f"{ax['title']}: нет данных")
            lines.append(_axis_base_line(ax))
            continue
        shown = f"{value:.2f}" if ax["key"] != "g7" else f"{value * 100:+.1f}%"
        pp = ax.get("platform_percentile")
        pn = ax.get("platform_n")
        if pp is None or not pn:
            lines.append(f"{ax['title']}: {shown} — база {ax.get('platform') or '—'} пуста")
            lines.append(_axis_base_line(ax))
            continue
        base_desc = ax["platform"]
        if ax.get("base_platforms") and base_desc == "все платформы":
            base_desc += ": " + "+".join(ax["base_platforms"])
        lines.append(
            f"{ax['title']}: {shown} — перцентиль {round(pp)} "
            f"из 100 (база {base_desc}, {_thousands(pn)} "
            f"{_plural(pn, 'источник', 'источника', 'источников')})")
        lines.append(_axis_base_line(ax))
    return lines


def _axis_base_line(ax: dict) -> str:
    """Поимённая база оси: сколько источников каждой платформы её образуют (D-66).

    X-строки называются отдельно: если у X-постов нет просмотров, их нет в
    ``base_by_platform``, и ось не выдаёт X за полноценную базу.
    """
    by = ax.get("base_by_platform") or {}
    parts = " + ".join(
        f"{_thousands(by.get(p, 0))} {label}"
        for p, label in (("youtube", "YouTube"), ("telegram", "Telegram"),
                         ("x", "X")))
    return f"  база оси «{ax['title']}»: {parts}"


def format_message(data: dict, *, percentile: dict | None = None) -> str:
    """Короткий текст для телефона: заголовки блоков, 2–3 числа на позицию."""
    lines: list[str] = [f"СЛИВКИ · {data.get('day', '')}", ""]
    blocks = [
        ("АВТОРЫ — растут", data.get("authors") or [], _author_line),
        ("ПОДТЕМЫ — тренд внутри тренда", data.get("subtopics") or [], _subtopic_line),
        ("НОВИНКИ — 48 ч", data.get("novelties") or [], _novelty_line),
        ("ОБСУЖДЕНИЯ", data.get("discussions") or [], _discussion_line),
    ]
    for title, items, fmt in blocks:
        if not items:
            continue
        lines.append(title)
        for i, item in enumerate(items, 1):
            lines.extend(fmt(i, item))
        if title.startswith("АВТОРЫ"):
            unknown = sum(1 for it in items if it.get("growth") == "unknown")
            if unknown:
                lines.append(f"   (рост 7д не измерим у {unknown} "
                             f"{_plural(unknown, 'автора', 'авторов', 'авторов')}: "
                             f"нет ряда истории)")
        lines.append("")
    pct_lines = _percentile_lines(percentile) if percentile else []
    if pct_lines:
        lines.extend(pct_lines)
        lines.append("")
    links = (data.get("links") or {})
    if links:
        lines.append(f"ссылок проверено {links.get('checked', 0)}, "
                     f"отброшено {links.get('dropped', 0)}")
    return "\n".join(lines).rstrip()


def split_message(text: str, *, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Разбить текст по строкам на части не длиннее ``limit`` (без разрыва строк)."""
    if not text:
        return []
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur.rstrip())
    return parts


# ---------------------------------------------------------------------------
# Доставка (прямой Telegram Bot API)
# ---------------------------------------------------------------------------

def load_bot_token(*, env_file: str = "/root/.hermes/.env") -> str | None:
    """Токен бота: переменная окружения, затем ``.env`` (значение не печатается)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip()
    for path in (env_file, "/root/tuber-os/.env"):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().startswith("TELEGRAM_BOT_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def _post_message(token: str, chat_id, thread_id, text: str, *,
                  timeout: float = 30.0) -> dict:
    """Один вызов ``sendMessage``; возвращает распарсенный JSON ответа."""
    data = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": "true",
    }
    if thread_id not in (None, "", "null"):
        data["message_thread_id"] = str(thread_id)
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"ok": False, "description": f"HTTP {exc.code}: {body[:200]}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "description": f"сеть: {exc}"}


def deliver(token: str, chat_id, thread_id, text: str, *,
            poster=_post_message, limit: int = MESSAGE_LIMIT) -> dict:
    """Отправить текст частями; вернуть ``{ok, parts, message_ids, error}``."""
    parts = split_message(text, limit=limit)
    ids: list = []
    for part in parts:
        result = poster(token, chat_id, thread_id, part)
        if not result.get("ok"):
            return {"ok": False, "parts": len(parts), "message_ids": ids,
                    "error": result.get("description") or "неизвестная ошибка"}
        msg = (result.get("result") or {}).get("message_id")
        if msg is not None:
            ids.append(msg)
    return {"ok": True, "parts": len(parts), "message_ids": ids, "error": None}


def journal_line(outcome: dict, *, chat_id, thread_id, text: str, when=None) -> str:
    """Строка журнала доставки (ТЗ-50 п.4): факт, а не «отправлено» на слово."""
    ts = _iso(_now(when))
    thread = "null" if thread_id in (None, "", "null") else str(thread_id)
    if outcome.get("ok"):
        return (f"delivered chat_id={chat_id} thread_id={thread} "
                f"parts={outcome['parts']} message_id="
                f"{outcome['message_ids'][-1] if outcome['message_ids'] else '—'} "
                f"bytes={len(text)} at={ts}")
    return (f"failed chat_id={chat_id} thread_id={thread} parts={outcome.get('parts', 0)} "
            f"error={outcome.get('error')} at={ts}")


def append_journal(path: str, line: str) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _write_text(path: str, text: str) -> None:
    """Записать точный текст отправки (аудит); ошибка записи не роняет прогон."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _db_path(args) -> str:
    if args.db:
        return args.db
    return os.environ.get("TUBER_DB") or config.db_path()


def _is_production(path: str) -> bool:
    try:
        return os.path.realpath(str(path)) == os.path.realpath(str(config.DEFAULT_DB_PATH))
    except OSError:
        return False


def _connect(path: str):
    from tuber.core import schema

    con = _db.connect(path)
    schema.migrate_schema(con)
    return con


def _chromeless(msg: str) -> None:
    print(msg, file=sys.stderr)


def cmd_digest(args) -> int:
    path = _db_path(args)
    now = _now(args.now)

    # Сеть для новинок: --no-network честно отдаёт пустой внешний контур.
    external_result = None
    if args.no_network:
        external_result = {
            "items": [], "sources": {
                name: {"platform": name, "title": name, "status": "skipped",
                       "count": 0, "error": "--no-network"}
                for name in ("hackernews", "github", "producthunt", "arxiv")}}

    con = _connect(path)
    try:
        limits = {
            "authors": args.limit_authors,
            "subtopics": args.limit_subtopics,
            "novelties": args.limit_novelties,
            "discussions": args.limit_discussions,
        }
        data = collect_blocks(con, now=now, external_result=external_result, limits=limits)

        if args.check_links:
            data = verify_all_links(data, timeout=args.link_timeout)

        percentile = percentile_block(
            con, now=now, subject=args.me,
            explicit={"reach": args.me_reach, "reactions": args.me_reactions,
                      "g7": args.me_g7})

        empty = is_empty(data)
        if empty:
            _chromeless("нет данных за сутки — отчёт не отправляется")
        text = "" if empty else format_message(data, percentile=percentile)

        # Точный текст отправки — в файл (аудит/приёмка: «тот самый текст»).
        message_file = args.save_message or os.environ.get("TUBER_DIGEST_MESSAGE_FILE")
        if text and message_file:
            _write_text(message_file, text)

        if args.json:
            payload = dict(data)
            payload["percentile"] = percentile
            payload["empty"] = empty
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0

        if args.dry_run and args.send:
            chat_id = args.chat_id or os.environ.get("TUBER_DIGEST_CHAT_ID")
            thread_id = args.thread_id or os.environ.get("TUBER_DIGEST_THREAD_ID")
            print("DRY-RUN: отправки нет.")
            print(f"  чат: chat_id={'не задан' if not chat_id else chat_id} thread_id="
                  f"{'null' if thread_id in (None, '', 'null') else thread_id}")
            print(f"  частей: {len(split_message(text))}, знаков: {len(text)}")
            print("  python3 -m tuber digest slivki --send --allow-production")
            return 0

        if args.send:
            if _is_production(path) and not args.allow_production:
                _chromeless("отказ: отправка из боевой базы без --allow-production")
                return 2
            if empty:
                append_journal(args.journal,
                               f"skipped нет данных at={_iso(now)}")
                return 0
            chat_id = args.chat_id or os.environ.get("TUBER_DIGEST_CHAT_ID")
            thread_id = args.thread_id or os.environ.get("TUBER_DIGEST_THREAD_ID")
            if not chat_id:
                _chromeless("отказ: нет chat_id (--chat-id или TUBER_DIGEST_CHAT_ID)")
                return 2
            token = load_bot_token()
            if not token:
                _chromeless("нет TELEGRAM_BOT_TOKEN — доставка невозможна")
                append_journal(args.journal, journal_line(
                    {"ok": False, "parts": 0, "message_ids": [],
                     "error": "нет TELEGRAM_BOT_TOKEN"},
                    chat_id=chat_id, thread_id=thread_id, text=text, when=now))
                return 0
            outcome = deliver(token, chat_id, thread_id, text)
            append_journal(args.journal, journal_line(
                outcome, chat_id=chat_id, thread_id=thread_id, text=text, when=now))
            if not outcome.get("ok"):
                _chromeless(f"ALERT: доставка не удалась ({outcome.get('error')})")
            return 0

        if text:
            print(text)
        return 0
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tuber digest",
        description="Ежедневная выдача «Сливки»: авторы, подтемы, новинки, "
                    "обсуждения + перцентиль «где я» (ТЗ-50)")
    ap.add_argument("--db", default=None, help="путь к базе (иначе TUBER_DB)")
    ap.add_argument("--now", default=None, help="якорь времени (ISO UTC)")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--no-network", action="store_true",
                    help="не ходить во внешний контур новинок")
    ap.add_argument("--no-check-links", dest="check_links", action="store_false",
                    help="не проверять ссылки HTTP (по умолчанию проверяются)")
    ap.set_defaults(check_links=True)
    ap.add_argument("--link-timeout", type=float, default=10.0,
                    help="таймаут проверки одной ссылки, с")
    ap.add_argument("--limit-authors", type=int, default=DEFAULT_LIMITS["authors"])
    ap.add_argument("--limit-subtopics", type=int, default=DEFAULT_LIMITS["subtopics"])
    ap.add_argument("--limit-novelties", type=int, default=DEFAULT_LIMITS["novelties"])
    ap.add_argument("--limit-discussions", type=int, default=DEFAULT_LIMITS["discussions"])
    ap.add_argument("--me", default=None,
                    help="субъект «где я»: platform:handle (иначе TUBER_SLIVKI_ME)")
    ap.add_argument("--me-reach", type=float, default=None,
                    help="охват на подписчика субъекта (явное значение)")
    ap.add_argument("--me-reactions", type=float, default=None,
                    help="реакции на 1 000 просмотров субъекта (явное значение)")
    ap.add_argument("--me-g7", type=float, default=None,
                    help="рост за 7 дней субъекта (доля, явное значение)")
    ap.add_argument("--send", action="store_true",
                    help="отправить в Telegram прямым Bot API (гейт боевой базы)")
    ap.add_argument("--chat-id", default=None, help="chat_id получателя (явный)")
    ap.add_argument("--thread-id", default=None,
                    help="message_thread_id топика (не угадывается; нет — не передаётся)")
    ap.add_argument("--journal", default=DEFAULT_JOURNAL,
                    help="журнал доставки (строки delivered/skipped/failed)")
    ap.add_argument("--save-message", default=None,
                    help="записать точный текст отправки в файл (аудит)")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать план отправки, ничего не отправлять")
    ap.add_argument("--allow-production", action="store_true",
                    help="разрешить отправку из боевой базы (иначе отказ)")
    return ap


def _env_float(name: str) -> float | None:
    """Число из переменной окружения; пусто/мусор → ``None``."""
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "slivki":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.me:
        args.me = os.environ.get("TUBER_SLIVKI_ME")
    if args.me_reach is None:
        args.me_reach = _env_float("TUBER_SLIVKI_REACH")
    if args.me_reactions is None:
        args.me_reactions = _env_float("TUBER_SLIVKI_REACTIONS")
    if args.me_g7 is None:
        args.me_g7 = _env_float("TUBER_SLIVKI_G7")
    return cmd_digest(args)
