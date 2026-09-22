"""Граф источников (ТЗ-8): рёбра «пост → внешняя цель» и их потребитель.

Здесь живёт ровно то, что раньше было размазано по платформам:

* **одна точка правды классификации цели** — :func:`classify_target` /
  :func:`normalize_edge_target`. X-дискавери больше не держит свой список
  ``x.com``: и он, и общий потребитель рёбер зовут эту функцию;
* **слой рёбер** ``edge`` (ТЗ-8 §1): ссылки, упоминания, цитаты и репосты
  пишутся при сборе (:func:`record_post_edges`), а не теряются в парсере;
* **потребитель** :func:`consume` — превращает рёбра в кандидатов четырёх
  типов (X-аккаунт, Telegram-канал, YouTube-канал, веб-фид) с порогами входа,
  анти-самопиаром и дедупом с уже подключёнными источниками;
* **верификация веб-фида** :func:`verify_feed` — единственное место графа,
  которое ходит в сеть (и только через переданный ``broker``).

Инварианты:

* извлечение рёбер — 0 сетевых запросов (всё сырьё уже в базе);
* подключение источника — решение владельца, кроме фида с вердиктом ``feed``,
  прошедшего порог :data:`EDGE_MIN_DISTINCT_SOURCES` (ТЗ-8 §7);
* бэкфилл — ЯВНАЯ команда, а не побочный эффект соединения.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

# ===========================================================================
# Константы (ТЗ-8 §1.3/§1.4/§3.2/§3.4/§5.2). Все пороги — здесь, а не числами
# по коду: правка поведения графа не должна требовать поиска литералов.
# ===========================================================================

#: Р1.3: сокращатели, трекеры, хранилища документов, внутренние адреса и уже
#: собираемые зеркала. Ребро не создаётся вообще; в отчёте видны причинами.
SKIP_HOSTS: frozenset[str] = frozenset({
    # сокращатели и трекеры
    "t.co", "bit.ly", "goo.gl", "ow.ly", "buff.ly", "lnkd.in", "onelink.me",
    "linktr.ee", "fb.me", "vk.cc",
    # хранилища документов
    "docs.google.com", "drive.google.com", "dropbox.com", "notion.site",
    # внутренние
    "telegram.org", "telesco.pe",
})
#: Хосты внутренних Telegram-ссылок, которые НЕ считаются каналом, если в пути
#: нет самого канала (``t.me`` без канала → skip, ``t.me/<h>`` → канал).
TELEGRAM_HOSTS: frozenset[str] = frozenset({
    "t.me", "telegram.me", "telegram.dog", "www.t.me",
})
#: Р1.3: зеркала, которые уже собираются/не нужны как отдельный источник.
SKIP_HOST_PREFIXES: tuple[str, ...] = ("nitter.",)

#: Р1.4: домены-платформы-конкуренты. Ребро СОЗДАЁТСЯ, но помечается
#: ``competitor=1`` — выдача «на подключение» может отфильтровать отдельно.
# TODO(debt-D-50): флаг пишется, но фильтр выдачи на подключение не вынесен CLI — см. TECH-DEBT.md
COMPETITOR_HOSTS: frozenset[str] = frozenset({
    "max.ru", "vkvideo.ru", "rutube.ru", "dzen.ru", "vk.com",
})

#: X-хосты и служебные пути (Р1.2). Ссылка на ``/status/<id>`` — ребро на пост,
#: не на аккаунт, поэтому тоже отбрасывается.
X_HOSTS: frozenset[str] = frozenset({
    "x.com", "twitter.com", "mobile.twitter.com", "m.twitter.com",
})
X_SERVICE_PATHS: frozenset[str] = frozenset({
    "i", "search", "home", "explore", "intent", "share", "settings",
    "notifications", "messages", "compose", "tos", "privacy",
})

#: YouTube-хосты (включая зеркало piped.video — цель всё равно YouTube).
YOUTUBE_HOSTS: frozenset[str] = frozenset({
    "youtube.com", "m.youtube.com", "youtu.be", "piped.video",
})
#: Служебные первые сегменты YouTube, которые НЕ являются handle канала.
#: Без этого ``youtube.com/playlist?list=…`` давал ложный канал ``playlist``
#: (найдено на живой базе: цель `youtube|playlist`).
YOUTUBE_SERVICE_PATHS: frozenset[str] = frozenset({
    "playlist", "results", "feed", "gaming", "premium", "account", "redirect",
    "attribution_link", "t", "about", "watch", "embed", "v", "shorts", "live",
    "channel", "c", "user",
})

#: Реферальные метки, которые срезаются из URL (Р1.2).
_TRACKING_EXACT: frozenset[str] = frozenset({
    "ref", "ref_src", "fbclid", "gclid", "igshid", "mc_cid", "yclid",
})
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_",)

_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_TG_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{3,}$")
_YT_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,}$")
_YT_CHANNEL_RE = re.compile(r"^UC[A-Za-z0-9_\-]{20,}$")
_YT_VIDEO_RE = re.compile(r"^[A-Za-z0-9_\-]{6,}$")

#: Цель ребра. ``skip_reason`` непустой ⇒ ребро не создаётся (Р1.3).
#: ``target_value`` — НОРМАЛИЗОВАННОЕ значение (handle нижним регистром, домен
#: без ``www.``, id видео). ``target_url`` — исходная (очищенная) ссылка.
Target = namedtuple(
    "Target",
    "target_type target_platform target_value target_url competitor skip_reason",
)
Target.__new__.__defaults__ = (None, 0, None)

#: Тип цели при упоминании (@handle в тексте) зависит от платформы-родителя:
#: у X это X-аккаунт, у Telegram — Telegram-канал, у YouTube описания содержат
#: X-хендлы (тот же контур, что кормит exchange-фид, ТЗ-18).
MENTION_TARGET: dict[str, tuple[str, str]] = {
    "x": ("account", "x"),
    "telegram": ("channel", "telegram"),
    "youtube": ("account", "x"),
}

#: Р3.2: базовый вес ребра по виду. Цитата важнее репоста, репост важнее
#: упоминания, упоминание важнее внешней ссылки.
EDGE_KIND_WEIGHT: dict[str, float] = {
    "quote": 1.0,
    "repost": 0.7,
    "mention": 0.5,
    "link_x": 0.4,
    "link_tg": 0.4,
    "link_yt": 0.4,
    "link_web": 0.3,
}
EDGE_DEFAULT_WEIGHT = 0.3
EDGE_POPULARITY_W = 0.5        # вклад популярности родителя (нормированной)
EDGE_INDEPENDENCE_W = 0.5      # вклад независимости (число distinct_sources)
EDGE_INDEPENDENCE_SATURATION = 5.0  # при скольких источниках вклад насыщается
#: Нормировка significance родителя в [0, 1] для веса ребра.
EDGE_POPULARITY_NORM = 10.0

#: Р2.4: порог входа в верификацию. «Топ-1% корпуса» — доля, не число.
EDGE_MIN_DISTINCT_SOURCES = 2
EDGE_TOP_FRACTION = 0.01
#: Тира A/B — для условия «quote от доверенного источника».
EDGE_TRUSTED_TIERS = ("A", "B")

#: Р2.5: анти-самопиар. ≥10 ссылок от ОДНОГО источника → spam.
EDGE_SPAM_MIN_LINKS = 10

#: Р3.4: дневной лимит новых верификаций фидов и вид учёта запросов.
EDGE_FEED_VERIFY_DAILY_LIMIT = 30
EDGE_FEED_VERIFY_KIND = "feed_verify"
#: Р1.1 (ТЗ-8.1): коды бот-фильтров/WAF — сайт жив, но отдаёт отказ роботу.
#: Вердикт ``blocked``: кандидат НЕ отбраковывается и НЕ становится ``dead``.
EDGE_FEED_BLOCKED_CODES = (401, 403, 405, 406, 429, 451, 503)
#: Р1.2 (ТЗ-8.1): редиректы. При наличии ``Location`` проходятся вручную.
EDGE_FEED_REDIRECT_CODES = (301, 302, 303, 307, 308)
#: Р1.2 (ТЗ-8.1): максимум хопов редиректа (не больше 3 доп. запросов на домен).
EDGE_FEED_REDIRECT_MAX_HOPS = 3
#: Р1.1/Р1.2 (ТЗ-8.1): повторная верификация отложенного кандидата — не раньше N дней.
EDGE_FEED_RETRY_DAYS = 14
#: Кандидаты в фид: пути фида, которые пробуются после главной страницы.
FEED_CANDIDATE_PATHS: tuple[str, ...] = (
    "/feed", "/rss", "/atom.xml", "/index.xml", "/rss.xml", "/feed.xml",
)
FEED_TIMEOUT_SEC = 20.0

#: Р5.2: авто-отсев по отдаче. За 14 дней меньше порога постов ИЛИ ни одного
#: поста выше порога score — ``status='rejected'``, ``reject_reason='no_yield'``.
EDGE_NO_YIELD_DAYS = 14
EDGE_NO_YIELD_MIN_POSTS = 3
EDGE_NO_YIELD_MIN_SCORE = 1.0

#: Виды рёбер, которые бэкфилл НЕ восстанавливает (нет данных).
BACKFILL_LOST_KINDS: tuple[str, ...] = ("quote", "repost")


# ===========================================================================
# Классификация цели (Р1.2/Р1.3/Р1.4)
# ===========================================================================

def _clean_url(raw: str) -> tuple[str, urllib.parse.ParseResult]:
    """Очистить URL от реферальных меток; вернуть (url, parsed).

    Битую ссылку (например, несбалансированная ``[`` из markdown-текста даёт
    ``ValueError: Invalid IPv6 URL``) не парсим: возвращаем пустую строку и
    пустой разбор. Наружу исключений не летит (ТЗ №2 ч.2), ссылка
    отбрасывается, а пост сохраняется.
    """
    try:
        parsed = urllib.parse.urlparse(raw.strip())
    except ValueError:
        return "", urllib.parse.urlparse("")
    q = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(k, v) for k, v in q if k.lower() not in _TRACKING_EXACT
            and not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)]
    cleaned = parsed._replace(query=urllib.parse.urlencode(kept))
    return urllib.parse.urlunparse(cleaned), cleaned


def _host_of(parsed: urllib.parse.ParseResult) -> str:
    return (parsed.netloc or "").lower().split("@")[-1].split(":")[0]


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _is_skippable(host: str) -> bool:
    if host in SKIP_HOSTS:
        return True
    return any(host.startswith(prefix) for prefix in SKIP_HOST_PREFIXES)


def classify_target(url: str) -> Target:
    """Классифицировать URL в цель ребра (единая точка правды, Р1.2).

    Возвращает :class:`Target`; при отбрасывании ``skip_reason`` непустой
    (``skip_host`` | ``service_path`` | ``invite`` | ``no_target`` | ``bad_url``).
    """
    if url is None:
        return Target(None, None, "", None, 0, "bad_url")
    raw = str(url).strip()
    if not raw:
        return Target(None, None, "", None, 0, "bad_url")
    if "://" not in raw:
        raw = "http://" + raw
    try:
        _, parsed = _clean_url(raw)
    except ValueError:
        return Target(None, None, "", None, 0, "bad_url")
    host = _host_of(parsed)
    if not host:
        return Target(None, None, "", None, 0, "bad_url")
    bare = _bare(host)

    if _is_skippable(bare):
        return Target(None, None, "", None, 0, "skip_host")

    # --- X ---------------------------------------------------------------
    if bare in X_HOSTS:
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return Target(None, None, "", None, 0, "no_target")
        first = parts[0].lstrip("@").lower()
        if first in X_SERVICE_PATHS:
            return Target(None, None, "", None, 0, "service_path")
        if len(parts) >= 2 and parts[1].lower() == "status":
            return Target(None, None, "", None, 0, "service_path")
        if not _X_HANDLE_RE.match(first):
            return Target(None, None, "", None, 0, "no_target")
        return Target("account", "x", first, None, 0, None)

    # --- Telegram --------------------------------------------------------
    if bare in TELEGRAM_HOSTS:
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0].lower() == "s":
            parts = parts[1:]
        if not parts:
            return Target(None, None, "", None, 0, "no_target")
        name = parts[0].lstrip("@")
        if name.startswith("+"):
            return Target(None, None, "", None, 0, "invite")
        name = name.lower()
        if not _TG_HANDLE_RE.match(name):
            return Target(None, None, "", None, 0, "no_target")
        return Target("channel", "telegram", name, None, 0, None)

    # --- YouTube (и зеркало piped.video) ---------------------------------
    if bare in YOUTUBE_HOSTS:
        if bare == "youtu.be":
            vid = parsed.path.strip("/").split("/")[0]
            if _YT_VIDEO_RE.match(vid):
                return Target("video", "youtube", vid, None, 0, None)
            return Target(None, None, "", None, 0, "no_target")
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return Target(None, None, "", None, 0, "no_target")
        head = parts[0].lower()
        if head in ("watch", "embed", "v", "shorts", "live"):
            vid = ""
            if head in ("shorts", "embed", "live", "v") and len(parts) >= 2:
                vid = parts[1]
            qs = urllib.parse.parse_qs(parsed.query)
            vid = vid or (qs.get("v", [""])[0])
            if _YT_VIDEO_RE.match(vid):
                return Target("video", "youtube", vid, None, 0, None)
            return Target(None, None, "", None, 0, "no_target")
        if head == "channel" and len(parts) >= 2 and _YT_CHANNEL_RE.match(parts[1]):
            return Target("channel", "youtube", parts[1], None, 0, None)
        if head in ("c", "user") and len(parts) >= 2 and _YT_HANDLE_RE.match(parts[1]):
            return Target("channel", "youtube", parts[1].lower(), None, 0, None)
        if head in YOUTUBE_SERVICE_PATHS:
            return Target(None, None, "", None, 0, "service_path")
        name = parts[0].lstrip("@").lower()
        if _YT_HANDLE_RE.match(name):
            return Target("channel", "youtube", name, None, 0, None)
        return Target(None, None, "", None, 0, "no_target")

    # --- всё остальное → домен (веб-фид) ---------------------------------
    competitor = 1 if bare in COMPETITOR_HOSTS else 0
    clean_url, _ = _clean_url(raw)
    return Target("domain", "web", bare, clean_url, competitor, None)


def normalize_edge_target(kind: str, raw) -> Target | None:
    """Нормализовать цель ребра (Р1.2). ``None`` — ребро создавать нельзя.

    Для ``kind='mention'`` ``raw`` — это @handle или голый хендл, а не URL:
    платформа цели берётся из самого вида ребра (см. :data:`MENTION_TARGET`
    в вызывающем коде). Здесь поддерживается только форма ``@handle``.
    """
    if kind == "mention":
        h = str(raw or "").strip().lstrip("@").lower()
        if not h:
            return None
        return Target("account", None, h, None, 0, None)
    target = classify_target(raw)
    if target.skip_reason:
        return None
    return target


def edge_kind_for(target: Target) -> str | None:
    """Вид ребра-ссылки по цели: ``link_x`` / ``link_tg`` / ``link_yt`` / ``link_web``."""
    if target is None or target.skip_reason:
        return None
    if target.target_type == "account" and target.target_platform == "x":
        return "link_x"
    if target.target_platform == "telegram":
        return "link_tg"
    if target.target_platform == "youtube":
        return "link_yt"
    if target.target_platform == "web":
        return "link_web"
    return None


# ===========================================================================
# Вес ребра (Р3.2)
# ===========================================================================

def edge_weight(kind: str, popularity: float = 0.0,
                distinct_sources: int = 1) -> float:
    """Вычислимый вес ребра: вид × популярность родителя × независимость.

    ``popularity`` — нормированная (0..1) значимость родительского поста,
    ``distinct_sources`` — число независимых источников цели. В порог входа
    (Р2.4) вес НЕ подменяет: он только для сортировки очереди.
    """
    base = EDGE_KIND_WEIGHT.get(kind, EDGE_DEFAULT_WEIGHT)
    pop = max(0.0, min(1.0, float(popularity or 0.0)))
    indep = max(0, int(distinct_sources or 1) - 1) / EDGE_INDEPENDENCE_SATURATION
    indep = min(1.0, indep)
    return round(base * (1.0 + EDGE_POPULARITY_W * pop)
                 * (1.0 + EDGE_INDEPENDENCE_W * indep), 4)


def parent_popularity(con, content_id) -> float:
    """Нормированная значимость родительского поста (0..1) из ``v_score_current``."""
    if content_id is None:
        return 0.0
    try:
        row = con.execute(
            "SELECT significance FROM v_score_current WHERE content_id=?",
            (content_id,)).fetchone()
    except Exception:  # noqa: BLE001 — недо-мигрированная база не должна ломать сбор
        return 0.0
    if not row or row[0] is None:
        return 0.0
    return max(0.0, min(1.0, float(row[0]) / EDGE_POPULARITY_NORM))


# ===========================================================================
# Запись рёбер
# ===========================================================================

def _now_iso(now=None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def record_edge(con, *, from_platform: str, kind: str, target: Target,
                from_source_id=None, from_content_id=None, from_handle=None,
                origin: str, evidence=None, weight=None, now=None) -> str | None:
    """Записать/обновить одно ребро. Возвращает ``new`` | ``upd`` | ``None``.

    Идемпотентно по ``UNIQUE(from_content_id, kind, target_value)``:
    повторный прогон увеличивает ``seen_count``/``last_seen_at``, но не число
    строк (Р1.6/А6.4).
    """
    if target is None or target.skip_reason or not target.target_value:
        return None
    ts = _now_iso(now)
    if weight is None:
        weight = edge_weight(kind)
    try:
        cur = con.execute(
            """INSERT INTO edge (from_platform, from_source_id, from_content_id,
                 from_handle, kind, target_type, target_platform, target_value,
                 target_url, weight, evidence, origin, competitor, first_seen_at,
                 last_seen_at, seen_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(from_content_id, kind, target_value) DO UPDATE SET
                 seen_count = edge.seen_count + 1,
                 last_seen_at = excluded.last_seen_at,
                 weight = MAX(edge.weight, excluded.weight),
                 target_url = COALESCE(excluded.target_url, edge.target_url),
                 from_source_id = COALESCE(excluded.from_source_id, edge.from_source_id),
                 competitor = MAX(edge.competitor, excluded.competitor)""",
            (from_platform, from_source_id, from_content_id, from_handle, kind,
             target.target_type, target.target_platform, target.target_value,
             target.target_url, weight, evidence, origin, target.competitor or 0,
             ts, ts))
    except Exception:  # noqa: BLE001
        return None
    # ``rowcount`` у прямого INSERT/ON CONFLICT: 1 при вставке строки; при
    # ON CONFLICT DO UPDATE в SQLite тоже 1, поэтому «новизну» определяем по
    # ``seen_count``: свежая строка имеет ровно 1.
    row = con.execute(
        "SELECT seen_count FROM edge WHERE from_content_id IS ? AND kind=? AND target_value=?",
        (from_content_id, kind, target.target_value)).fetchone()
    return "new" if (row and row[0] == 1) else "upd"


def _mention_target(platform: str, raw) -> Target | None:
    base = normalize_edge_target("mention", raw)
    if base is None:
        return None
    ttype, tplat = MENTION_TARGET.get(platform, ("account", "x"))
    return Target(ttype, tplat, base.target_value, None, 0, None)


def record_post_edges(con, *, platform: str, links=(), mentions=(),
                      orig_handle=None, is_quote=False, is_repost=False,
                      from_content_id=None, from_source_id=None,
                      from_handle=None, origin: str = "collect",
                      evidence=None, popularity: float | None = None,
                      now=None) -> dict:
    """Записать все рёбра одного поста (Р1.5). Ноль сетевых запросов.

    ``links``/``mentions`` — списки или JSON-строки (как лежат в ``content``).
    ``orig_handle`` при ``is_repost``/``is_quote`` — автор оригинала: именно
    это ребро сегодня теряется в парсере, поэтому пишется в момент сбора.
    """
    if popularity is None:
        popularity = parent_popularity(con, from_content_id)
    created = updated = skipped = 0
    skip_reasons: dict[str, int] = {}
    by_kind: dict[str, int] = {}

    def _skip(reason: str) -> None:
        nonlocal skipped
        skipped += 1
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    def _record(kind: str, target: Target) -> None:
        nonlocal created, updated
        w = edge_weight(kind, popularity)
        res = record_edge(con, from_platform=platform, kind=kind, target=target,
                          from_source_id=from_source_id, from_content_id=from_content_id,
                          from_handle=from_handle, origin=origin,
                          evidence=evidence, weight=w, now=now)
        if res == "new":
            created += 1
            by_kind[kind] = by_kind.get(kind, 0) + 1
        elif res == "upd":
            updated += 1

    for raw in _as_list(links):
        target = classify_target(raw)
        if target.skip_reason:
            _skip(target.skip_reason)
            continue
        kind = edge_kind_for(target)
        if not kind:
            _skip("no_target")
            continue
        _record(kind, target)

    for raw in _as_list(mentions):
        target = _mention_target(platform, raw)
        if target is None:
            _skip("bad_mention")
            continue
        _record("mention", target)

    if orig_handle:
        h = str(orig_handle).strip().lstrip("@").lower()
        if h:
            ttype = "channel" if platform == "telegram" else "account"
            tplat = platform if platform in ("x", "telegram", "youtube") else "x"
            # Цитаты/репосты известны только у X (у TG это forward без автора).
            for kind, flag in (("repost", is_repost), ("quote", is_quote)):
                if flag:
                    _record(kind, Target(ttype, tplat, h, None, 0, None))
    return {"created": created, "updated": updated, "skipped": skipped,
            "by_kind": by_kind, "skip_reasons": skip_reasons}


def record_description_edges(con, *, content_id, source_id=None, from_handle=None,
                            text, platform: str = "youtube",
                            origin: str = "collect", now=None) -> dict:
    """Рёбра из ОПИСАНИЯ видео (тот же контур, что кормит ``data/exchange/``).

    Разбор ссылок не дублируется: берётся ``candidates._iter_links`` —
    единственный разбор описаний YouTube. Отсутствие модуля/таблицы не роняет
    сбор: возвращается нулевая сводка.
    """
    zero = {"created": 0, "updated": 0, "skipped": 0, "by_kind": {}, "skip_reasons": {}}
    if content_id is None or not text:
        return zero
    try:
        from tuber.platforms.youtube import candidates as yc
        pairs = list(yc.extract_mentions(text))
    except Exception:  # noqa: BLE001
        return zero
    created = updated = skipped = 0
    by_kind: dict[str, int] = {}
    skip_reasons: dict[str, int] = {}
    seen: set[str] = set()
    pop = parent_popularity(con, content_id)
    for kind, handle in pairs:
        if not handle or handle in seen:
            continue
        seen.add(handle)
        url = ("https://t.me/" + handle if kind == "telegram"
               else "https://x.com/" + handle)
        target = classify_target(url)
        if target.skip_reason:
            skipped += 1
            skip_reasons[target.skip_reason] = skip_reasons.get(target.skip_reason, 0) + 1
            continue
        ek = edge_kind_for(target)
        if not ek:
            continue
        res = record_edge(con, from_platform=platform, kind=ek, target=target,
                          from_source_id=source_id, from_content_id=content_id,
                          from_handle=from_handle, origin=origin,
                          weight=edge_weight(ek, pop), now=now)
        if res == "new":
            created += 1
            by_kind[ek] = by_kind.get(ek, 0) + 1
        elif res == "upd":
            updated += 1
    return {"created": created, "updated": updated, "skipped": skipped,
            "by_kind": by_kind, "skip_reasons": skip_reasons}


def _as_list(value):
    """Список из значения ``content.links``/``mentions`` (JSON-строка или список)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return [s]
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
        return [str(parsed)]
    return []


# ===========================================================================
# Бэкфилл (Р1.6)
# ===========================================================================

def backfill(con, *, platforms=("x", "telegram", "youtube"), origin="backfill",
             limit=None, dry=False, now=None) -> dict:
    """Явный бэкфилл рёбер из уже собранного корпуса (Р1.6).

    Источник — ``content.links`` и ``content.mentions``. ``quote``/``repost``
    НЕ восстанавливаются: автора цитируемого/репостнутого поста в ``content``
    нет (потеря зафиксирована честно, см. ``lost_kinds``).
    """
    marks = ",".join("?" for _ in platforms)
    params = list(platforms)
    sql = (
        "SELECT c.id, c.platform, c.source_id, c.author_handle, c.links, c.mentions,"
        " c.meta_json, s.handle AS source_handle "
        "FROM content c LEFT JOIN source s ON s.id = c.source_id "
        "WHERE c.platform IN (%s) AND ("
        " (c.links IS NOT NULL AND c.links NOT IN ('', '[]')) OR"
        " (c.mentions IS NOT NULL AND c.mentions NOT IN ('', '[]')) ) "
        "ORDER BY c.platform, c.id" % marks)
    if limit:
        sql += " LIMIT %d" % int(limit)
    rows = con.execute(sql, params).fetchall()
    stats = {"posts": 0, "created": 0, "updated": 0, "skipped": 0,
             "by_kind": {}, "skip_reasons": {}, "by_platform": {},
             "lost_kinds": list(BACKFILL_LOST_KINDS)}
    for row in rows:
        stats["posts"] += 1
        plat = row["platform"]
        meta = {}
        if row["meta_json"]:
            try:
                meta = json.loads(row["meta_json"]) or {}
            except (ValueError, TypeError):
                meta = {}
        res = record_post_edges(
            con, platform=plat, links=row["links"], mentions=row["mentions"],
            orig_handle=None, is_quote=False, is_repost=False,
            from_content_id=row["id"], from_source_id=row["source_id"],
            from_handle=row["author_handle"] or row["source_handle"]
            or meta.get("owner_handle"),
            origin=origin, now=now)
        stats["created"] += res["created"]
        stats["updated"] += res["updated"]
        stats["skipped"] += res["skipped"]
        for k, v in res["by_kind"].items():
            stats["by_kind"][k] = stats["by_kind"].get(k, 0) + v
        for k, v in res["skip_reasons"].items():
            stats["skip_reasons"][k] = stats["skip_reasons"].get(k, 0) + v
        stats["by_platform"][plat] = stats["by_platform"].get(plat, 0) + 1
    # YouTube: у описаний нет колонки links (контур описаний отдельный), поэтому
    # рёбра берём тем же разбором, что кормит data/exchange/ (Р1.6).
    if "youtube" in platforms:
        yt = con.execute(
            "SELECT id, source_id, author_handle, text FROM content "
            "WHERE platform='youtube' AND text IS NOT NULL AND text <> ''").fetchall()
        for row in yt:
            stats["posts"] += 1
            res = record_description_edges(
                con, content_id=row["id"], source_id=row["source_id"],
                from_handle=row["author_handle"], text=row["text"],
                platform="youtube", origin=origin, now=now)
            stats["created"] += res["created"]
            stats["updated"] += res["updated"]
            stats["skipped"] += res["skipped"]
            for k, v in res["by_kind"].items():
                stats["by_kind"][k] = stats["by_kind"].get(k, 0) + v
            for k, v in res["skip_reasons"].items():
                stats["skip_reasons"][k] = stats["skip_reasons"].get(k, 0) + v
        stats["by_platform"]["youtube"] = stats["by_platform"].get("youtube", 0) + len(yt)
    if dry:
        con.rollback()
    else:
        con.commit()
    return stats


# ===========================================================================
# Потребитель рёбер → кандидаты (Р2.x)
# ===========================================================================

#: Целевые типы кандидатов: (target_type, target_platform) → (platform, kind).
CANDIDATE_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("account", "x"): ("x", "handle"),
    ("channel", "telegram"): ("telegram", "channel"),
    ("channel", "youtube"): ("youtube", "channel"),
    ("domain", "web"): ("web", "feed"),
}


def _source_token(platform: str, from_handle) -> str:
    if platform == "telegram":
        return f"tg_posts:{from_handle or '?'}"
    if platform == "youtube":
        return f"yt_desc:{from_handle or '?'}"
    if platform == "x":
        return "x_post"
    return f"{platform}_post"


def top_fraction_content_ids(con, fraction=EDGE_TOP_FRACTION, cache=None) -> set:
    """ID постов верхней ``fraction`` корпуса по значимости (для Р2.4)."""
    if cache is not None:
        return cache
    try:
        rows = con.execute(
            "SELECT content_id FROM v_score_current WHERE significance IS NOT NULL "
            "ORDER BY significance DESC").fetchall()
    except Exception:  # noqa: BLE001
        return set()
    if not rows:
        return set()
    k = max(1, int(len(rows) * fraction))
    return {r[0] for r in rows[:k]}


def _edge_groups(con, platforms=None):
    where = "target_platform IS NOT NULL AND target_value <> ''"
    params: list = []
    if platforms:
        marks = ",".join("?" for _ in platforms)
        where += f" AND from_platform IN ({marks})"
        params = list(platforms)
    return con.execute(
        f"""SELECT target_type, target_platform, target_value,
                  COUNT(*) AS n_edges,
                  COUNT(DISTINCT CASE WHEN from_source_id IS NOT NULL
                                      THEN 'src:' || from_source_id
                                      ELSE 'c:' || from_content_id END) AS ds,
                  MAX(weight) AS max_weight,
                  MAX(competitor) AS competitor
           FROM edge WHERE {where}
           GROUP BY target_type, target_platform, target_value""", params).fetchall()


def _group_sources(con, ttype, tplat, tvalue):
    rows = con.execute(
        """SELECT DISTINCT from_platform, kind, from_source_id, from_content_id
           FROM edge WHERE target_type=? AND target_platform=? AND target_value=?""",
        (ttype, tplat, tvalue)).fetchall()
    keys, kinds, platforms, content_ids = [], set(), set(), []
    for r in rows:
        k = (f"{r['kind']}:{r['from_platform']}:src:{r['from_source_id']}"
             if r["from_source_id"] is not None
             else f"{r['kind']}:{r['from_platform']}:content:{r['from_content_id']}")
        if k not in keys:
            keys.append(k)
        kinds.add(r["kind"])
        platforms.add(r["from_platform"])
        if r["from_content_id"] is not None:
            content_ids.append(r["from_content_id"])
    return keys, kinds, platforms, content_ids


def _source_exists(con, handle: str) -> bool:
    """Источник с таким handle уже подключён на ЛЮБОЙ платформе (Р4.3)."""
    row = con.execute(
        "SELECT 1 FROM source WHERE lower(handle)=lower(?) LIMIT 1", (handle,)).fetchone()
    return row is not None


def _quote_from_trusted(con, ttype, tplat, tvalue) -> bool:
    try:
        row = con.execute(
            """SELECT 1 FROM edge e JOIN source s ON s.id = e.from_source_id
               WHERE e.target_type=? AND e.target_platform=? AND e.target_value=?
                 AND e.kind='quote' AND s.tier IN (?,?) LIMIT 1""",
            (ttype, tplat, tvalue, *EDGE_TRUSTED_TIERS)).fetchone()
    except Exception:  # noqa: BLE001
        return False
    return row is not None


def consume(con, *, platforms=None, min_distinct_sources=EDGE_MIN_DISTINCT_SOURCES,
            spam_min_links=EDGE_SPAM_MIN_LINKS, limit=None, dry=False,
            now=None) -> dict:
    """Превратить рёбра в кандидатов четырёх типов (Р2.1-Р2.6).

    Кандидат создаётся для целей с ``CANDIDATE_MAP``; ``video`` пропускается
    (нужен resolve канала, которого у нас нет — Р2.1). Дедуп с уже
    подключёнными источниками — по всем платформам (Р4.3).
    """
    ts = _now_iso(now)
    stats = {"groups": 0, "created": 0, "merged": 0, "skipped_existing": 0,
             "skipped_type": 0, "spam": 0, "eligible": 0, "by_platform": {},
             "by_kind": {}, "not_eligible": 0}
    top_ids = top_fraction_content_ids(con)
    groups = _edge_groups(con, platforms)
    for g in groups:
        stats["groups"] += 1
        ttype, tplat, tvalue = g["target_type"], g["target_platform"], g["target_value"]
        mapped = CANDIDATE_MAP.get((ttype, tplat))
        if not mapped:
            stats["skipped_type"] += 1
            continue
        platform, kind = mapped
        if _source_exists(con, tvalue):
            stats["skipped_existing"] += 1
            continue
        keys, kinds, from_plats, content_ids = _group_sources(con, ttype, tplat, tvalue)
        ds = int(g["ds"] or 0)
        n_edges = int(g["n_edges"] or 0)
        is_spam = 1 if (n_edges >= spam_min_links and ds <= 1) else 0
        if is_spam:
            stats["spam"] += 1

        # --- порог входа (Р2.4): вес в порог НЕ подменяет -----------------
        eligible = ds >= min_distinct_sources
        if not eligible and any(cid in top_ids for cid in content_ids):
            eligible = True
        if not eligible and _quote_from_trusted(con, ttype, tplat, tvalue):
            eligible = True
        if eligible:
            stats["eligible"] += 1
        else:
            stats["not_eligible"] += 1

        # --- found_via (Р2.2): вид + происхождение ------------------------
        dominant = _dominant_kind(con, ttype, tplat, tvalue, kinds)
        origin_plat = sorted(from_plats)[0] if from_plats else "x"
        fh = con.execute(
            "SELECT from_handle FROM edge WHERE target_type=? AND target_platform=?"
            " AND target_value=? AND from_handle IS NOT NULL LIMIT 1",
            (ttype, tplat, tvalue)).fetchone()
        token = _source_token(origin_plat, fh[0] if fh else None)
        if dominant in ("quote", "repost"):
            found_via = f"{dominant}:{token}"
        else:
            found_via = f"{dominant}:{token}"

        weight = float(g["max_weight"] or 0.0)
        priority = round(weight * (1.0 + 0.1 * ds), 4)
        meta = {"verify_eligible": bool(eligible), "edges": n_edges,
                "target_type": ttype, "competitor": bool(g["competitor"])}
        first_seen = con.execute(
            "SELECT MIN(first_seen_at) m FROM edge WHERE target_type=? AND"
            " target_platform=? AND target_value=?",
            (ttype, tplat, tvalue)).fetchone()["m"] or ts
        row = con.execute(
            "SELECT id, seen_count, status, validated FROM candidate"
            " WHERE platform=? AND handle=?", (platform, tvalue)).fetchone()
        if row is None:
            con.execute(
                """INSERT INTO candidate (platform, kind, handle, external_id,
                     score_priority, found_via, found_in_handle, seen_count,
                     distinct_sources, sources_json, meta_json, first_seen_at,
                     last_seen_at, spam, status, rubric)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new', ?)""",
                (platform, kind, tvalue, tvalue, priority, found_via,
                 fh[0] if fh else None, n_edges, ds,
                 json.dumps(keys, ensure_ascii=False),
                 json.dumps(meta, ensure_ascii=False), first_seen, ts, is_spam,
                 "graph"))
            stats["created"] += 1
        else:
            # ТЗ-8.1: не затирать ключи верификации (blocked_at, feed_verdict),
            # иначе повторный consume сбрасывает окно повторной проверки (Р1.1/Р1.2).
            old_meta = {}
            prev = con.execute("SELECT meta_json FROM candidate WHERE id=?",
                               (row["id"],)).fetchone()
            if prev and prev["meta_json"]:
                try:
                    old_meta = json.loads(prev["meta_json"]) or {}
                except (ValueError, TypeError):
                    old_meta = {}
            old_meta.update(meta)
            con.execute(
                """UPDATE candidate SET seen_count=MAX(seen_count, ?),
                     distinct_sources=MAX(distinct_sources, ?),
                     sources_json=?, score_priority=MAX(score_priority, ?),
                     spam=MAX(spam, ?), last_seen_at=?,
                     meta_json=? WHERE id=?""",
                (n_edges, ds, json.dumps(keys, ensure_ascii=False), priority,
                 is_spam, ts, json.dumps(old_meta, ensure_ascii=False), row["id"]))
            stats["merged"] += 1
        stats["by_platform"][platform] = stats["by_platform"].get(platform, 0) + 1
        stats["by_kind"][dominant] = stats["by_kind"].get(dominant, 0) + 1
    if dry:
        con.rollback()
    else:
        con.commit()
    return stats


def _dominant_kind(con, ttype, tplat, tvalue, kinds) -> str:
    if len(kinds) == 1:
        return next(iter(kinds))
    row = con.execute(
        """SELECT kind, SUM(weight) w FROM edge
           WHERE target_type=? AND target_platform=? AND target_value=?
           GROUP BY kind ORDER BY w DESC LIMIT 1""",
        (ttype, tplat, tvalue)).fetchone()
    return row["kind"] if row else "link_web"


def eligible_candidates(con, *, limit=None, platforms=("web",), now=None):
    """Кандидаты, прошедшие порог входа (Р2.4), для верификации/подключения.

    Р1.1/Р1.2 (ТЗ-8.1): кандидат с ``meta_json.blocked_at`` не попадает в очередь,
    пока с момента блокировки не прошло :data:`EDGE_FEED_RETRY_DAYS` дней.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=EDGE_FEED_RETRY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    marks = ",".join("?" for _ in platforms)
    rows = con.execute(
        f"""SELECT * FROM candidate
            WHERE platform IN ({marks})
              AND COALESCE(validated, 'pending') NOT IN ('ok', 'reject')
              AND json_extract(COALESCE(meta_json, '{{}}'), '$.verify_eligible') = 1
              AND (json_extract(COALESCE(meta_json, '{{}}'), '$.blocked_at') IS NULL
                   OR json_extract(COALESCE(meta_json, '{{}}'), '$.blocked_at') <= ?)
            ORDER BY score_priority DESC, distinct_sources DESC, handle ASC""",
        list(platforms) + [cutoff]).fetchall()
    return rows[:limit] if limit else rows


# ===========================================================================
# Верификация веб-фида (Р3.1/Р3.4)
# ===========================================================================

_FEED_LINK_RE = re.compile(
    r"""<link[^>]+type=["']application/(?:rss|atom)\+xml["'][^>]*>""", re.I)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class RawHTTPBroker:
    """Адаптер сети для :func:`verify_feed`: единственный выход — брокер X."""

    def get(self, url, timeout=FEED_TIMEOUT_SEC):
        from tuber.platforms.x.broker import raw_http_get
        return raw_http_get(url, timeout=timeout)


def _fetch_ok(broker, url, timeout=FEED_TIMEOUT_SEC):
    """GET через брокер. Возвращает (status, body) или (0, '')."""
    try:
        status, _headers, body = broker.get(url, timeout=timeout)
    except TypeError:
        status, _headers, body = broker.get(url)
    except Exception:  # noqa: BLE001
        return 0, ""
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
    return int(status or 0), body or ""


def _fetch_response(broker, url, timeout=FEED_TIMEOUT_SEC):
    """GET через брокер с заголовками. Возвращает (status, headers, body).

    Нужен Р1.2: для ручного прохода редиректов требуется ``Location``.
    Заголовки нормализуются к нижнему регистру; транспортная ошибка → (0, {}, "").
    """
    try:
        res = broker.get(url, timeout=timeout)
    except TypeError:
        res = broker.get(url)
    except Exception:  # noqa: BLE001
        return 0, {}, ""
    try:
        status, headers, body = res
    except (TypeError, ValueError):
        return 0, {}, ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    norm = {str(k).lower(): v for k, v in (headers or {}).items()}
    return int(status or 0), norm, body or ""


def _follow_redirects(broker, status, headers, body, url,
                      max_hops=EDGE_FEED_REDIRECT_MAX_HOPS):
    """Р1.2: ручной проход редиректов (не больше ``max_hops`` доп. запросов).

    Принимает уже полученный первый ответ. Возвращает
    ``(status, body, final_url, unresolved)``: ``unresolved=True`` означает
    отсутствие ``Location``, зацикливание или исчерпание хопов — это вердикт
    ``redirect``, а НЕ ``dead``.
    """
    seen = {url}
    cur = url
    for _ in range(max_hops):
        if status not in EDGE_FEED_REDIRECT_CODES:
            return status, body, cur, False
        loc = headers.get("location")
        if not loc:
            return status, body, cur, True
        nxt = urllib.parse.urljoin(cur, loc)
        if nxt == cur or nxt in seen:
            return status, body, cur, True
        seen.add(nxt)
        cur = nxt
        status, headers, body = _fetch_response(broker, cur)
    if status in EDGE_FEED_REDIRECT_CODES:
        return status, body, cur, True
    return status, body, cur, False


def _feed_dead(status: int) -> bool:
    """Р1.3: смерть сайта — только транспортная ошибка или 4xx/5xx вне Р1.1."""
    if status == 0:
        return True
    if status in EDGE_FEED_BLOCKED_CODES:
        return False
    return status >= 400


def discover_feed_url(broker, base: str, home_html: str):
    """URL фида: из ``<link rel=alternate>`` главной, иначе по кандидат-путям."""
    m = _FEED_LINK_RE.search(home_html or "")
    if m:
        href = _HREF_RE.search(m.group(0))
        if href:
            return urllib.parse.urljoin(base, href.group(1))
    return None


def parse_feed(body: str) -> dict:
    """Разобрать RSS/Atom: title, язык, последняя запись, число, частота."""
    out = {"title": None, "lang": None, "last_at": None, "entries": 0,
           "per_day": 0.0, "ok": False}
    if not body:
        return out
    try:
        root = ElementTree.fromstring(body.strip())
    except ElementTree.ParseError:
        return out
    out["ok"] = True
    tag = root.tag.split("}")[-1]
    if tag == "rss":
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        if channel is not None:
            t = channel.findtext("title")
            out["title"] = (t or "").strip() or None
            out["lang"] = (channel.findtext("{http://www.w3.org/XML/1998/namespace}lang")
                           or channel.findtext("language") or None)
    else:  # atom
        items = root.findall("{http://www.w3.org/2005/Atom}entry") or root.findall("entry")
        t = root.findtext("{http://www.w3.org/2005/Atom}title") or root.findtext("title")
        out["title"] = (t or "").strip() or None
        out["lang"] = root.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
    out["entries"] = len(items)
    dates = []
    for it in items[:20]:
        raw = (it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated")
               or it.findtext("updated") or it.findtext("{http://www.w3.org/2005/Atom}published")
               or it.findtext("published"))
        dt = _parse_feed_date(raw)
        if dt:
            dates.append(dt)
    if dates:
        out["last_at"] = max(dates).strftime("%Y-%m-%d %H:%M:%S")
        span = (max(dates) - min(dates)).total_seconds() / 86400.0
        if span > 0:
            out["per_day"] = round(len(dates) / span, 3)
        else:
            out["per_day"] = float(len(dates))
    return out


def _parse_feed_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    fmts = ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
            "%a, %d %b %Y %H:%M %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S")
    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# TODO(debt-D-49): веб-источники регистрируются, но сбора фида в content нет — см. TECH-DEBT.md
def verify_feed(con, domain, url=None, *, broker, run_id=None, now=None) -> dict:
    """Верифицировать домен как веб-фид (Р3.1; вердикты Р1.1-Р1.3 ТЗ-8.1).

    Живость → поиск фида → разбор → вердикт ``feed`` | ``html_only`` |
    ``blocked`` | ``redirect`` | ``dead`` | ``no_content``. Коды бот-фильтров
    (Р1.1) и неразрешённые редиректы (Р1.2) НЕ отбраковывают кандидата:
    он остаётся ``new``, а в ``meta_json`` пишутся ``blocked=1``,
    ``blocked_at`` и ``blocked_http``. При вердикте ``feed`` и пороге
    :data:`EDGE_MIN_DISTINCT_SOURCES` заводится источник ``platform='web'``.
    """
    now = now or datetime.now(timezone.utc)
    base = f"https://{domain}/"
    result = {"domain": domain, "url": url or base, "verdict": None,
              "feed_url": None, "title": None, "lang": None, "last_at": None,
              "entries": 0, "per_day": 0.0, "http": 0, "reason": None,
              "source_created": False}
    status, headers, body = _fetch_response(broker, base)
    used = base
    status, body, used, unresolved = _follow_redirects(
        broker, status, headers, body, used)
    if _feed_dead(status):
        # Запасной http:// — только если https-попытка отдала «смерть».
        st2, h2, b2 = _fetch_response(broker, f"http://{domain}/")
        used2 = f"http://{domain}/"
        st2, b2, used2, unres2 = _follow_redirects(broker, st2, h2, b2, used2)
        if not _feed_dead(st2):
            status, body, used, unresolved = st2, b2, used2, unres2
    result["http"] = status
    _record_feed_request(con, domain, used, status, run_id, now)

    # Р1.1: бот-фильтр — кандидат не отбраковывается.
    if status in EDGE_FEED_BLOCKED_CODES:
        result["verdict"] = "blocked"
        result["reason"] = f"http {status}"
        _mark_candidate_verdict(con, domain, "blocked", now, http=status)
        return result
    # Р1.2: редирект не разрешён (нет Location / петля / хопы исчерпаны).
    if unresolved or status in EDGE_FEED_REDIRECT_CODES:
        result["verdict"] = "redirect"
        result["reason"] = f"http {status} loop"
        _mark_candidate_verdict(con, domain, "redirect", now, http=status)
        return result
    # Р1.3: только транспортная ошибка или 4xx/5xx вне Р1.1.
    if _feed_dead(status):
        result["verdict"] = "dead"
        result["reason"] = f"http {status or 'error'}"
        _mark_candidate_verdict(con, domain, "dead", now, http=status)
        return result

    feed_url = discover_feed_url(broker, used, body)
    candidates = [feed_url] if feed_url else []
    for path in FEED_CANDIDATE_PATHS:
        candidates.append(urllib.parse.urljoin(used, path))
    parsed = None
    # W3-3 (D-51): кандидат-URL фида проходит тот же ручной проход редиректов,
    # что и главная (Р1.2): иначе фид за 301/302 не находится, а тело ответа —
    # пустая страница редиректа. Финальный URL идёт в parse_feed и feed_url.
    # TODO(debt-D-51): закрыт в ТЗ-11 (W3-3) — см. TECH-DEBT.md.
    for cu in candidates:
        if not cu:
            continue
        st, ch, cb = _fetch_response(broker, cu)
        if st == 0:
            continue  # транспортная ошибка кандидата — пробуем следующий
        used_cu = cu
        st, cb, used_cu, unresolved_cu = _follow_redirects(broker, st, ch, cb, used_cu)
        # Неразрешённый редирект — не фид, переходим к следующему кандидату.
        if unresolved_cu or st in EDGE_FEED_REDIRECT_CODES:
            continue
        if st == 200 and cb:
            p = parse_feed(cb)
            if p.get("ok") and (p.get("entries") or 0) > 0:
                parsed, result["feed_url"] = p, used_cu
                break
    if parsed:
        result.update(verdict="feed", title=parsed["title"], lang=parsed["lang"],
                      last_at=parsed["last_at"], entries=parsed["entries"],
                      per_day=parsed["per_day"])
        _mark_candidate_verdict(con, domain, "feed", now, http=status)
        if _maybe_create_web_source(con, domain, result["feed_url"], now):
            result["source_created"] = True
        if queue_has_eligible(con, domain):
            result["threshold_met"] = True
        return result
    if len(body.strip()) < 200:
        result["verdict"] = "no_content"
        result["reason"] = "главная почти пуста, фида нет"
    else:
        result["verdict"] = "html_only"
        result["reason"] = "жив, но RSS/Atom не найден"
    _mark_candidate_verdict(con, domain, result["verdict"], now, http=status)
    return result


def _mark_candidate_verdict(con, domain, verdict, now, http=None) -> None:
    """Зафиксировать вердикт верификации в ``candidate.meta_json`` (ТЗ-8.1).

    Статус кандидата НЕ меняется. Для ``blocked``/``redirect`` (Р1.1/Р1.2)
    пишутся ``blocked=1``, ``blocked_at``, ``blocked_http`` — по ``blocked_at``
    очередь повторной верификации ждёт :data:`EDGE_FEED_RETRY_DAYS`. Для прочих
    вердиктов устаревшие блокировки снимаются. ``feed_verdict``/``feed_verdict_at``
    нужны отчёту (Р1.4), чтобы считать вердикты раздельно.
    """
    row = con.execute(
        "SELECT id, meta_json FROM candidate WHERE platform='web' AND handle=?",
        (domain,)).fetchone()
    if row is None:
        return
    try:
        meta = json.loads(row["meta_json"] or "{}") or {}
    except (ValueError, TypeError):
        meta = {}
    meta["feed_verdict"] = verdict
    meta["feed_verdict_at"] = _now_iso(now)
    if verdict in ("blocked", "redirect"):
        meta["blocked"] = 1
        meta["blocked_at"] = _now_iso(now)
        if http is not None:
            meta["blocked_http"] = http
    else:
        for key in ("blocked", "blocked_at", "blocked_http"):
            meta.pop(key, None)
    con.execute("UPDATE candidate SET meta_json=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), row["id"]))
    con.commit()


def queue_has_eligible(con, domain) -> bool:
    """Кандидат-домен прошёл порог входа (Р2.4) — условие автоподключения."""
    try:
        row = con.execute(
            "SELECT json_extract(COALESCE(meta_json,'{}'), '$.verify_eligible') e "
            "FROM candidate WHERE platform='web' AND handle=?", (domain,)).fetchone()
    except Exception:  # noqa: BLE001
        return False
    return bool(row and row[0] == 1)


def _maybe_create_web_source(con, domain, feed_url, now) -> bool:
    if not queue_has_eligible(con, domain):
        return False
    row = con.execute(
        "SELECT 1 FROM source WHERE platform='web' AND lower(handle)=lower(?)",
        (domain,)).fetchone()
    if row:
        return False
    con.execute(
        """INSERT INTO source (platform, handle, url, status, source_kind,
             added_at, added_by, first_seen_at, last_synced_at)
           VALUES ('web', ?, ?, 'active', 'backfill', ?, 'graph', ?, ?)""",
        (domain, feed_url, _now_iso(now), _now_iso(now), _now_iso(now)))
    con.commit()
    return True


def _record_feed_request(con, domain, url, status, run_id, now) -> None:
    """Учёт сетевого запроса верификации фида (Р3.4, kind='feed_verify').

    Ядро хранит запросы в ``transport_request``; legacy-представление ``requests``
    смотрит ровно на неё, поэтому учёт ведётся там.
    """
    try:
        con.execute(
            """INSERT INTO transport_request (platform, host, ts, kind, url, status,
                 items, run_id) VALUES ('web', ?, ?, ?, ?, ?, 1, ?)""",
            (domain, _now_iso(now), EDGE_FEED_VERIFY_KIND, url, status, run_id))
        con.commit()
    except Exception:  # noqa: BLE001
        pass


def feed_verifications_today(con, day=None) -> int:
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = con.execute(
        "SELECT COUNT(*) FROM transport_request WHERE kind=? AND ts LIKE ?",
        (EDGE_FEED_VERIFY_KIND, day + "%")).fetchone()
    return int(row[0] or 0)


def feed_verify_budget(con, limit=EDGE_FEED_VERIFY_DAILY_LIMIT, day=None) -> int:
    return max(0, int(limit) - feed_verifications_today(con, day))


# ===========================================================================
# Авто-отсев по отдаче (Р5.2)
# ===========================================================================

def prune_no_yield(con, *, days=EDGE_NO_YIELD_DAYS,
                   min_posts=EDGE_NO_YIELD_MIN_POSTS,
                   min_score=EDGE_NO_YIELD_MIN_SCORE, dry=False, now=None) -> dict:
    """Источники без отдачи за ``days`` → ``status='rejected'`` / ``no_yield``."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute(
        """SELECT s.id, s.platform, s.handle, s.status,
                  (SELECT COUNT(*) FROM content c
                    WHERE c.source_id=s.id AND c.published_at >= ?) AS posts,
                  (SELECT COUNT(*) FROM content c
                     JOIN v_score_current sc ON sc.content_id=c.id
                    WHERE c.source_id=s.id AND sc.significance >= ?) AS good
           FROM source s
           WHERE s.status IN ('active','provisional')""", (since, min_score)).fetchall()
    stats = {"checked": len(rows), "rejected": 0, "skipped_ok": 0, "details": []}
    for r in rows:
        if int(r["posts"] or 0) >= int(min_posts) and int(r["good"] or 0) > 0:
            stats["skipped_ok"] += 1
            continue
        stats["rejected"] += 1
        stats["details"].append({"id": r["id"], "platform": r["platform"],
                                 "handle": r["handle"], "posts": r["posts"],
                                 "good": r["good"]})
        if not dry:
            con.execute(
                "UPDATE source SET status='rejected', last_error='no_yield' WHERE id=?",
                (r["id"],))
    if dry:
        con.rollback()
    else:
        con.commit()
    return stats


# ===========================================================================
# Отчёт (Р5.1/Р5.3)
# ===========================================================================

def report(con, *, days=7, now=None) -> dict:
    """Сводка графа: рёбра, топы целей, кандидаты, подключения, отсев."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def _scalar(sql, params=()):
        return con.execute(sql, params).fetchone()[0] or 0

    total = _scalar("SELECT COUNT(*) FROM edge")
    by_kind = dict(con.execute(
        "SELECT kind, COUNT(*) FROM edge GROUP BY kind ORDER BY 2 DESC").fetchall())
    by_origin = dict(con.execute(
        "SELECT COALESCE(origin,'?'), COUNT(*) FROM edge GROUP BY 1 ORDER BY 2 DESC").fetchall())
    top_domains = con.execute(
        """SELECT target_value, COUNT(DISTINCT from_source_id) ds, COUNT(*) n
           FROM edge WHERE target_type='domain'
           GROUP BY target_value ORDER BY ds DESC, n DESC LIMIT 15""").fetchall()
    top_accounts = con.execute(
        """SELECT target_platform, target_value, COUNT(DISTINCT from_source_id) ds, COUNT(*) n
           FROM edge WHERE target_type IN ('account','channel')
           GROUP BY target_platform, target_value ORDER BY ds DESC, n DESC LIMIT 15""").fetchall()
    cand_by_type = con.execute(
        """SELECT platform, kind, COUNT(*) FROM candidate
           GROUP BY platform, kind ORDER BY 3 DESC""").fetchall()
    cand_by_status = con.execute(
        "SELECT platform, COALESCE(status,'?'), COUNT(*) FROM candidate GROUP BY 1,2"
    ).fetchall()
    eligible = _scalar(
        "SELECT COUNT(*) FROM candidate WHERE json_extract(COALESCE(meta_json,'{}'),"
        " '$.verify_eligible') = 1")
    web_connected = _scalar(
        "SELECT COUNT(*) FROM source WHERE platform='web' AND first_seen_at >= ?", (since,))
    newest = _scalar(
        "SELECT COUNT(*) FROM candidate WHERE first_seen_at >= ?", (since,))
    rejected = con.execute(
        """SELECT COALESCE(reject_reason,'?'), COUNT(*) FROM candidate
           WHERE reject_reason IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    feed_checks = _scalar(
        "SELECT COUNT(*) FROM transport_request WHERE kind=?",
        (EDGE_FEED_VERIFY_KIND,))
    # Р1.4: вердикты веб-фидов раздельно; blocked/redirect НЕ суммируются с dead.
    raw_verdicts = dict(con.execute(
        """SELECT COALESCE(json_extract(meta_json, '$.feed_verdict'), '?'), COUNT(*)
           FROM candidate WHERE platform='web' GROUP BY 1""").fetchall())
    feed_verdicts = {k: int(raw_verdicts.get(k, 0))
                     for k in ("feed", "html_only", "blocked", "redirect",
                               "no_content", "dead")}
    out = {
        "edges_total": total,
        "edges_by_kind": by_kind,
        "edges_by_origin": by_origin,
        "top_domains": [{"target_value": r[0], "distinct_sources": r[1], "edges": r[2]}
                        for r in top_domains],
        "top_accounts": [{"target_platform": r[0], "target_value": r[1],
                          "distinct_sources": r[2], "edges": r[3]} for r in top_accounts],
        "candidates_by_type": [{"platform": r[0], "kind": r[1], "n": r[2]}
                               for r in cand_by_type],
        "candidates_by_status": [{"platform": r[0], "status": r[1], "n": r[2]}
                                 for r in cand_by_status],
        "eligible": eligible,
        "web_connected_window": web_connected,
        "candidates_new_window": newest,
        "candidate_reject_reasons": [{"reason": r[0], "n": r[1]} for r in rejected],
        "feed_verifications": feed_checks,
        "feed_verdicts": feed_verdicts,
        "window_days": days,
    }
    return out


def format_report(con, *, days=7, now=None) -> str:
    """Человекочитаемая печать отчёта графа (числами, Р5.3)."""
    r = report(con, days=days, now=now)
    lines = ["=== Граф источников (рёбра -> кандидаты) ==="]
    lines.append(f"рёбер всего: {r['edges_total']}")
    lines.append("по видам:")
    for k, n in sorted(r["edges_by_kind"].items(), key=lambda x: -x[1]):
        lines.append(f"  {k:10s} {n}")
    lines.append("по происхождению:")
    for k, n in r["edges_by_origin"].items():
        lines.append(f"  {str(k):16s} {n}")
    lines.append("топ-15 доменов по числу независимых источников:")
    for d in r["top_domains"]:
        lines.append(f"  {d['target_value']:38s} src={d['distinct_sources']:4d} edges={d['edges']}")
    lines.append("топ-15 аккаунтов/каналов по числу независимых источников:")
    for d in r["top_accounts"]:
        lines.append(f"  {d['target_platform']:9s} {d['target_value']:28s}"
                     f" src={d['distinct_sources']:4d} edges={d['edges']}")
    lines.append("кандидатов по типам:")
    for c in r["candidates_by_type"]:
        lines.append(f"  {c['platform']:9s} {c['kind']:8s} {c['n']}")
    lines.append("кандидатов по статусам:")
    for c in r["candidates_by_status"]:
        lines.append(f"  {c['platform']:9s} {str(c['status']):10s} {c['n']}")
    lines.append(f"прошли порог входа (verify_eligible): {r['eligible']}")
    lines.append(f"новых кандидатов за {r['window_days']} дн: {r['candidates_new_window']}")
    lines.append(f"веб-источников подключено за {r['window_days']} дн: {r['web_connected_window']}")
    lines.append(f"верификаций фидов (feed_verify) всего: {r['feed_verifications']}")
    lines.append("вердикты веб-фидов (последняя проверка):")
    for k in ("feed", "html_only", "blocked", "redirect", "no_content", "dead"):
        lines.append(f"  {k:10s} {r['feed_verdicts'][k]}")
    lines.append("отсев кандидатов по причинам:")
    if r["candidate_reject_reasons"]:
        for x in r["candidate_reject_reasons"]:
            lines.append(f"  {x['reason']:16s} {x['n']}")
    else:
        lines.append("  (нет)")
    return "\n".join(lines)
