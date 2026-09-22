"""Сбор видео и каналов (этап 4, часть 2).

Экономия квоты:
- search.list стоит 100 units, поэтому дешёвый предфильтр идёт по данным
  выдачи поиска до дорогого videos.list (1 unit за 50 id);
- уже известные видео не перезапрашиваем, только обновляем last_seen;
- заведомо шумные категории отсекаются после videos.list, но до записи в БД.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import config, store as db, api as yt

log = logging.getLogger(__name__)

# Путь к курсору ротации по умолчанию, если конфиг его не задал.
_ROTATION_FILE_DEFAULT = "data/query_rotation.json"
# W3-1: путь к курсору ротации обхода uploads по умолчанию.
_UPLOADS_ROTATION_FILE_DEFAULT = "data/uploads_rotation.json"

# Шумовые категории YouTube (мемы, игры, музыка, развлечения и т.п.).
NOISE_CATEGORY_IDS = frozenset({1, 10, 17, 20, 23, 24})

# Размер страницы плейлиста YouTube (элементов на страницу).
PLAYLIST_PAGE_SIZE = 50

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def make_client(conn: Any) -> yt.YouTubeClient:
    """Создать клиент YouTube, пишущий квоту в это соединение."""
    return yt.YouTubeClient(conn=conn)


# --- ротация порций поисковых запросов ------------------------------------


def rotation_slice(queries: Sequence[str], size: int, cursor: int = 0) -> list[str]:
    """Окно из `size` запросов по кругу пула, начиная с позиции `cursor`.

    Если порция длиннее пула — возвращается весь пул целиком (без повторов
    внутри одной порции). Некорректный размер или пустой пул дают [].
    """
    pool = list(queries)
    if not pool:
        return []
    try:
        count = int(size)
    except (TypeError, ValueError):
        return []
    if count <= 0:
        return []
    if count >= len(pool):
        return pool
    try:
        start = int(cursor)
    except (TypeError, ValueError):
        start = 0
    start %= len(pool)
    return [pool[(start + i) % len(pool)] for i in range(count)]


def load_cursor(path: Any, full_len: int) -> int:
    """Прочитать курсор ротации.

    Мусор не ломает прогон: отсутствующий/битый файл, не-число, отрицательный
    курсор или курсор вне диапазона [0, full_len) дают 0.
    """
    try:
        length = int(full_len)
    except (TypeError, ValueError):
        return 0
    if length <= 0:
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    raw = data.get("cursor")
    if isinstance(raw, bool):
        return 0
    try:
        cursor = int(raw)
    except (TypeError, ValueError):
        return 0
    if cursor < 0 or cursor >= length:
        return 0
    return cursor


def _read_rotation_state(path: Any) -> dict[str, Any]:
    """Прочитать JSON-состояние ротации; битый/отсутствующий файл → {}."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_rotation_state(path: Any, state: Mapping[str, Any]) -> None:
    """Атомарно записать JSON-состояние ротации (временный файл + os.replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(state), fh, ensure_ascii=False)
    os.replace(tmp, target)


def save_cursor(path: Any, cursor: int, full_len: int) -> None:
    """Атомарно записать курсор ротации.

    W3-2: неизвестные ключи файла (счётчики отдачи запросов) сохраняются —
    курсор и служебное состояние живут в одном файле и не затирают друг друга.
    """
    try:
        length = int(full_len)
    except (TypeError, ValueError):
        length = 0
    try:
        value = int(cursor)
    except (TypeError, ValueError):
        value = 0
    if length > 0:
        value %= length
    state = _read_rotation_state(path)
    state["cursor"] = value
    state["updated_at"] = config.now_ts()
    state["full_len"] = length
    _write_rotation_state(path, state)


def _rotation_path(cfg: Any) -> Path:
    """Абсолютный путь к файлу курсора ротации."""
    raw = getattr(cfg, "QUERY_ROTATION_FILE", _ROTATION_FILE_DEFAULT)
    path = Path(str(raw))
    if not path.is_absolute():
        base = getattr(cfg, "TUBER_DIR", None) or config.TUBER_DIR
        path = Path(base) / path
    return path


def _uploads_rotation_path(cfg: Any) -> Path:
    """Абсолютный путь к файлу курсора ротации обхода uploads (W3-1)."""
    raw = getattr(cfg, "UPLOADS_ROTATION_FILE", _UPLOADS_ROTATION_FILE_DEFAULT)
    path = Path(str(raw))
    if not path.is_absolute():
        base = getattr(cfg, "TUBER_DIR", None) or config.TUBER_DIR
        path = Path(base) / path
    return path


# --- W3-2: учёт отдачи поисковых запросов (рядом с курсором) ---------------


def load_query_stats(path: Any) -> dict[str, Any]:
    """Прочитать счётчики отдачи запросов из файла состояния ротации."""
    raw = _read_rotation_state(path).get("queries")
    return dict(raw) if isinstance(raw, dict) else {}


def save_query_stats(path: Any, stats: Mapping[str, Any]) -> None:
    """Сохранить счётчики отдачи запросов, не теряя курсор (W3-2)."""
    state = _read_rotation_state(path)
    state["queries"] = dict(stats)
    state["updated_at"] = config.now_ts()
    _write_rotation_state(path, state)


def query_is_quarantined(entry: Any, now: int, zero_limit: int,
                         cooldown_sec: int) -> bool:
    """Выпал ли запрос из ротации по малой отдаче (W3-2).

    Запрос на карантине, если он `zero_limit` прогонов подряд дал found=0 и
    срок карантина (``cooldown_sec``) ещё не истёк.
    """
    if not isinstance(entry, dict):
        return False
    until = entry.get("quarantine_until")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return False
    return int(now) < int(until)


def active_search_queries(pool: Sequence[str], stats: Mapping[str, Any], now: int,
                          zero_limit: int, cooldown_sec: int) -> list[str]:
    """Пул ротации без запросов на карантине (W3-2)."""
    return [
        q for q in pool
        if not query_is_quarantined(stats.get(q), now, zero_limit, cooldown_sec)
    ]


def record_query_result(stats: dict[str, Any], query: str, found: int, now: int,
                        zero_limit: int, cooldown_sec: int) -> None:
    """Обновить счётчик отдачи запроса после прогона (W3-2).

    Ненулевая выдача сбрасывает серию и снимает карантин; ``zero_limit`` пустых
    прогонов подряд отправляют запрос в карантин на ``cooldown_sec``.
    """
    entry = stats.get(query)
    if not isinstance(entry, dict):
        entry = {}
    if int(found) > 0:
        entry["zero_runs"] = 0
        entry["last_found_at"] = int(now)
        entry.pop("quarantine_until", None)
    else:
        entry["zero_runs"] = int(entry.get("zero_runs", 0)) + 1
        entry["last_zero_at"] = int(now)
        if entry["zero_runs"] >= int(zero_limit):
            entry["quarantine_until"] = int(now) + int(cooldown_sec)
    stats[query] = entry


# --- разбор значений -------------------------------------------------------


def parse_iso_utc(value: Any) -> int | None:
    """ISO-8601 -> unixtime. Поддержаны 'Z', смещения и дробные секунды."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    # fromisoformat в 3.11 понимает 'Z', но нормализуем для совместимости.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse_duration(value: Any) -> int | None:
    """ISO-8601 duration (PT1H2M3S) -> секунды. Без данных — None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _DURATION_RE.match(text)
    if not m:
        return None
    parts = {k: int(v) if v is not None else 0 for k, v in m.groupdict().items()}
    seconds = (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )
    return int(seconds)


def _to_int(value: Any) -> int | None:
    """Привести строковое число API к int; пустое/None -> None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pick_thumbnail(thumbnails: Mapping[str, Any] | None) -> tuple[str | None, int | None, int | None]:
    """Выбрать обложку: maxres, иначе high, иначе остальные по убыванию."""
    if not thumbnails:
        return None, None, None
    for name in ("maxres", "high", "standard", "medium", "default"):
        thumb = thumbnails.get(name)
        if isinstance(thumb, dict) and thumb.get("url"):
            return thumb.get("url"), _to_int(thumb.get("width")), _to_int(thumb.get("height"))
    return None, None, None


# --- сбор ------------------------------------------------------------------


def _known_video_ids(conn: Any, ids: Sequence[str]) -> set[str]:
    """Какие id уже есть в таблице videos."""
    known: set[str] = set()
    chunk = list(ids)
    for i in range(0, len(chunk), 500):
        part = chunk[i:i + 500]
        marks = ",".join("?" for _ in part)
        if not marks:
            continue
        rows = conn.execute(
            f"SELECT video_id FROM videos WHERE video_id IN ({marks})", part
        ).fetchall()
        known.update(r["video_id"] for r in rows)
    return known


def _stage_video(item: Mapping[str, Any], seen_at: int,
                 cfg: Any = config) -> dict[str, Any] | None:
    """Превратить элемент videos.list в строку для upsert_video."""
    video_id = item.get("id")
    if not video_id:
        return None
    snippet = item.get("snippet") or {}
    content = item.get("contentDetails") or {}
    duration = parse_duration(content.get("duration"))
    tags = snippet.get("tags")
    thumb_url, thumb_w, thumb_h = pick_thumbnail(snippet.get("thumbnails"))
    caption = content.get("caption")
    return {
        "video_id": video_id,
        "channel_id": snippet.get("channelId"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        # Один формат для тегов: ровно JSON-массив.
        "tags": json.dumps(tags if isinstance(tags, list) else [], ensure_ascii=False),
        "category_id": _to_int(snippet.get("categoryId")),
        # Язык пишем только если он реально пришёл в ответе.
        "default_language": snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage"),
        "duration_seconds": duration,
        # Вероятный шортс по длительности: порог из конфига (по умолчанию 180 с).
        "is_shorts": config.is_probable_shorts(duration, cfg),
        "published_at": parse_iso_utc(snippet.get("publishedAt")),
        "thumbnail_url": thumb_url,
        "thumbnail_width": thumb_w,
        "thumbnail_height": thumb_h,
        "live_broadcast": snippet.get("liveBroadcastContent"),
        "caption_available": None if caption is None else int(str(caption).lower() == "true"),
        "last_seen": seen_at,
    }


def _ensure_channel(conn: Any, channel_id: Any) -> None:
    """Гарантировать строку канала, чтобы FK из videos не падал.

    Полные данные канала пишет store_channel; здесь только заглушка по id.
    """
    if not channel_id:
        return
    conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, first_seen) VALUES (?, ?)",
        (channel_id, config.now_ts()),
    )


def _record_description_edges(conn: Any, row: Mapping[str, Any]) -> None:
    """ТЗ-8 Р1.5: рёбра из описания видео. Ошибка не роняет сбор."""
    try:
        from tuber.core import graph as core_graph
        vid = row.get("video_id")
        if not vid:
            return
        cid = conn.execute(
            "SELECT id FROM content WHERE platform='youtube' AND external_id=?",
            (vid,)).fetchone()
        if cid is None:
            return
        src = conn.execute(
            "SELECT id FROM source WHERE platform='youtube' AND external_id=?",
            (row.get("channel_id"),)).fetchone()
        core_graph.record_description_edges(
            conn, content_id=cid[0], source_id=(src[0] if src else None),
            from_handle=row.get("channel_id"), text=row.get("description"),
            platform="youtube", origin="collect")
    except Exception:  # noqa: BLE001 — граф не имеет права ломать сбор
        pass


def store_videos(conn: Any, items: Sequence[Mapping[str, Any]], query: str,
                 cfg: Any = config) -> dict[str, Any]:
    """Записать метаданные видео. Возвращает сводку по записи.

    query сохраняется как контекст вызова (в схеме отдельного поля нет),
    все данные пишутся через db.upsert_video: first_seen не перезаписывается.
    """
    now = config.now_ts()
    written = 0
    new_ids: list[str] = []
    skipped = 0
    for item in items:
        row = _stage_video(item, now, cfg)
        if row is None or not row.get("thumbnail_url"):
            # Обложка — обязательное поле входа (SCHEMA.md, инвариант 5).
            skipped += 1
            continue
        _ensure_channel(conn, row.get("channel_id"))
        existed = conn.execute(
            "SELECT 1 FROM videos WHERE video_id=?", (row["video_id"],)
        ).fetchone()
        prev_seen = None
        if existed is not None:
            prev = conn.execute(
                "SELECT first_seen FROM videos WHERE video_id=?", (row["video_id"],)
            ).fetchone()
            prev_seen = prev["first_seen"] if prev else None
        row["first_seen"] = prev_seen if prev_seen is not None else now
        db.upsert_video(conn, row)
        written += 1
        # ТЗ-8 Р1.5: рёбра из описания видео (тот же контур, что data/exchange/).
        _record_description_edges(conn, row)
        if existed is None:
            new_ids.append(row["video_id"])
    return {"query": query, "written": written, "new": len(new_ids),
            "skipped": skipped, "video_ids": new_ids}


def store_channel(conn: Any, item: Mapping[str, Any]) -> str | None:
    """Записать канал. is_russian: 1/0 при данных, NULL при их отсутствии."""
    channel_id = item.get("id")
    if not channel_id:
        return None
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    content = item.get("contentDetails") or {}
    topics = (item.get("topicDetails") or {}).get("topicCategories")

    language = snippet.get("defaultLanguage")
    country = snippet.get("country")
    if language is None and country is None:
        # Данных нет: «не знаем» это NULL, а не 0.
        is_russian: int | None = None
    else:
        is_russian = int(language == "ru" or country == "RU")

    subscribers = _to_int(stats.get("subscriberCount"))
    if stats.get("hiddenSubscriberCount"):
        subscribers = None

    db.upsert_channel(
        conn,
        {
            "channel_id": channel_id,
            "title": snippet.get("title"),
            "handle": snippet.get("customUrl"),
            "subscriber_count": subscribers,
            "video_count": _to_int(stats.get("videoCount")),
            "view_count": _to_int(stats.get("viewCount")),
            "country": country,
            "default_language": language,
            "topic_categories": json.dumps(
                topics if isinstance(topics, list) else [], ensure_ascii=False
            ),
            "uploads_playlist_id": (content.get("relatedPlaylists") or {}).get("uploads"),
            "is_russian": is_russian,
            "last_synced_at": config.now_ts(),
        },
    )
    return channel_id


def _quota_total(conn: Any) -> int:
    """Суммарный расход квоты по quota_log (для сводок)."""
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(units),0) AS u FROM quota_log"
        ).fetchone()
    except Exception:  # нет таблицы/соединения — считаем нулём
        return 0
    return int(row["u"]) if row else 0


def _quota_spent_today(conn: Any) -> int:
    """Расход units за текущие сутки квоты (Pacific Time), суммарно по проекту.

    Повторяет семантику ``YouTubeClient.spent_today`` (там — по ключам проекта),
    но суммирует ВСЕ строки quota_log: для предохранителя прогона нужен общий
    расход проекта. Legacy-строки без ``ts`` учитываются по UTC-дате, ошибка идёт
    в сторону преувеличения расхода (как в api.py). Любой сбой чтения — 0:
    недоступный учёт не должен ронять сбор.
    """
    try:
        quota_day = yt._quota_day_start(config.now_ts())
        legacy_day = yt._day_start(quota_day)
        row = conn.execute(
            "SELECT COALESCE(SUM(units),0) AS u FROM quota_log "
            "WHERE ts >= ? OR (ts IS NULL AND date >= ?)",
            (quota_day, legacy_day),
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # нет таблицы/колонок — предохранитель выключен
        log.warning("quota_log: не удалось прочитать расход за сутки (%s)", exc)
        return 0


def _quota_remaining_units(conn: Any, cfg: Any = config) -> int:
    """Остаток units до рабочего порога проекта (ТЗ-12, предохранитель квоты).

    Порог = ``QUOTA_LIMIT_PER_PROJECT - QUOTA_SAFETY_RESERVE`` (10 000 - 2 000 =
    8 000). Из него вычитается расход за ТЕКУЩИЕ сутки квоты, а не сумма за всё
    время: quota_log хранит историю, и all-time ``_quota_total`` за дни накопил бы
    больше порога, навсегда остановив обход (``_quota_total`` остаётся для
    измерения расхода конкретного прогона). Результат неотрицателен; 0 означает
    «остатка нет, обход надо штатно останавливать».
    """
    limit = int(getattr(cfg, "QUOTA_LIMIT_PER_PROJECT",
                        config.QUOTA_LIMIT_PER_PROJECT))
    reserve = int(getattr(cfg, "QUOTA_SAFETY_RESERVE",
                         config.QUOTA_SAFETY_RESERVE))
    return max(0, limit - reserve - _quota_spent_today(conn))


def _prefilter(results: Sequence[Mapping[str, Any]], now: int, window_days: int) -> list[dict[str, Any]]:
    """Дешёвый предфильтр по данным выдачи search.list."""
    window = window_days * 86400
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        ident = item.get("id") or {}
        if ident.get("kind") not in (None, "youtube#video"):
            continue
        video_id = ident.get("videoId")
        if not video_id or video_id in seen:
            continue
        snippet = item.get("snippet") or {}
        published = parse_iso_utc(snippet.get("publishedAt"))
        if published is None or not snippet.get("title"):
            continue
        age = now - published
        if age < 0 or age > window:
            continue
        if not snippet.get("channelId"):
            continue
        seen.add(video_id)
        out.append({
            "video_id": video_id,
            "channel_id": snippet.get("channelId"),
            "published_at": published,
        })
    return out


def search_topic(conn: Any, query: str, cfg: Any = config, max_pages: int = 2,
                 client: Any | None = None) -> dict[str, Any]:
    """Поиск видео по теме с предфильтром и записью метаданных."""
    now = cfg.now_ts()
    window_days = getattr(cfg, "MAX_VIDEO_AGE_DAYS", config.MAX_VIDEO_AGE_DAYS)
    published_after = now - window_days * 86400
    if client is None:
        client = make_client(conn)

    units_before = _quota_total(conn)
    results = client.search_videos(
        query, published_after=published_after, order="viewCount", max_pages=max_pages
    )
    candidates = _prefilter(results, now, window_days)

    found = len(results)

    # Уже известные видео: не тратим videos.list, только освежаем last_seen.
    known = _known_video_ids(conn, [c["video_id"] for c in candidates])
    if known:
        conn.executemany(
            "UPDATE videos SET last_seen=? WHERE video_id=?",
            [(now, vid) for vid in known],
        )
        conn.commit()
    fresh = [c for c in candidates if c["video_id"] not in known]

    stored = {"written": 0, "new": 0, "skipped": 0, "video_ids": []}
    channel_ids: list[str] = []
    if fresh:
        meta = client.videos_by_ids([c["video_id"] for c in fresh])
        # Проверка категории идёт после videos.list, но до записи в БД.
        usable = [m for m in meta if _is_usable(m)]
        stored = store_videos(conn, usable, query, cfg)
        channel_ids = sorted({
            (m.get("snippet") or {}).get("channelId")
            for m in usable
            if (m.get("snippet") or {}).get("channelId")
        })

    if channel_ids:
        channels = client.channels_by_ids(channel_ids)
        for ch in channels:
            store_channel(conn, ch)

    return {
        "query": query,
        "found": found,
        "candidates": len(candidates),
        "known": len(known),
        "written": stored["written"],
        "new": stored["new"],
        "skipped_noise": stored["skipped"],
        "video_ids": stored["video_ids"],
        "channel_ids": channel_ids,
        "units": _quota_total(conn) - units_before,
    }


def _is_usable(item: Mapping[str, Any]) -> bool:
    """Годно ли видео по метаданным: длительность > 0 и не шумовая категория."""
    content = item.get("contentDetails") or {}
    duration = parse_duration(content.get("duration"))
    if duration is None or duration <= 0:
        return False
    category = _to_int((item.get("snippet") or {}).get("categoryId"))
    if category is not None and category in NOISE_CATEGORY_IDS:
        return False
    return True


def _is_ai_channel(conn: Any, channel_id: str, min_videos: int = 2) -> bool:
    """Подтверждён ли канал как ИИ-канал: не менее min_videos видео is_ai=1.

    Каналы без разбора (нет строк в video_classification) не проходят:
    сначала смысловой разбор, потом обход.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM video_classification vc
        JOIN videos v ON v.video_id = vc.video_id
        WHERE v.channel_id = ? AND vc.is_ai = 1
        """,
        (channel_id,),
    ).fetchone()
    return bool(row and int(row["n"]) >= int(min_videos))


def _confirmed_ai_channels(conn: Any, min_videos: int) -> list[str]:
    """Все подтверждённые ИИ-каналы реестра, детерминированно по channel_id.

    W3-1: обходим реестр целиком (а не только каналы, найденные этим прогоном),
    поэтому список устойчив между прогонами и годится для ротации по курсору.
    """
    rows = conn.execute(
        """
        SELECT v.channel_id AS cid, COUNT(*) AS n
        FROM video_classification vc
        JOIN videos v ON v.video_id = vc.video_id
        WHERE vc.is_ai = 1 AND v.channel_id IS NOT NULL
        GROUP BY v.channel_id
        HAVING COUNT(*) >= ?
        ORDER BY v.channel_id
        """,
        (int(min_videos),),
    ).fetchall()
    return [r["cid"] for r in rows]


def _scan_uploads_batch(conn: Any, channel_ids: Sequence[str], cfg: Any, client: Any,
                        should_stop: Any, errors: list[str], min_ai: int,
                        already_scanned: set[str]) -> dict[str, Any]:
    """Обойти пачку каналов через uploads (W3-1), учитывая бюджет и повторы.

    ``should_stop`` — вызываемый без аргументов предикат остановки: время
    прогона или квота (ТЗ-12). Возвращает причину остановки строкой или None;
    проверяется перед КАЖДЫМ каналом, поэтому пачка обрывается штатно, а не
    падает при исчерпанной квоте.

    Возвращает ``{"new", "scanned", "requests", "attempted", "stopped",
    "stop_reason"}``. ``attempted`` — сколько каналов реально пытались обойти
    (даже со сбоем): по нему сдвигается курсор ротации, чтобы упавший канал не
    голодил очередь.
    """
    out = {"new": 0, "scanned": 0, "requests": 0, "attempted": 0,
           "stopped": False, "stop_reason": None}
    for channel_id in channel_ids:
        reason = should_stop()
        if reason:
            out["stopped"] = True
            out["stop_reason"] = reason
            break
        if channel_id in already_scanned:
            continue
        if not _is_ai_channel(conn, channel_id, min_ai):
            continue
        out["attempted"] += 1
        try:
            res = collect_uploads(conn, channel_id, cfg, client=client)
        except yt.YouTubeError as exc:
            log.warning("обход канала не удался (%s): %s", channel_id, exc)
            errors.append(f"uploads:{channel_id}: {exc}")
            already_scanned.add(channel_id)
            continue
        already_scanned.add(channel_id)
        out["requests"] += 1
        out["scanned"] += 1
        out["new"] += res.get("new", 0)
    return out


def collect_uploads(conn: Any, channel_id: str, cfg: Any = config,
                    client: Any | None = None, max_pages: int | None = None) -> dict[str, Any]:
    """Обход uploads-плейлиста канала с остановкой по возрасту видео.

    Листается не более max_pages страниц (по 50 видео), по умолчанию
    cfg.MAX_UPLOAD_PAGES_PER_CHANNEL.
    """
    now = cfg.now_ts()
    window = getattr(cfg, "MAX_VIDEO_AGE_DAYS", config.MAX_VIDEO_AGE_DAYS) * 86400
    if max_pages is None:
        max_pages = getattr(cfg, "MAX_UPLOAD_PAGES_PER_CHANNEL",
                            config.MAX_UPLOAD_PAGES_PER_CHANNEL)
    max_items = max(1, int(max_pages)) * PLAYLIST_PAGE_SIZE
    if client is None:
        client = make_client(conn)

    row = conn.execute(
        "SELECT uploads_playlist_id FROM channels WHERE channel_id=?", (channel_id,)
    ).fetchone()
    playlist_id = row["uploads_playlist_id"] if row else None
    if not playlist_id:
        channels = client.channels_by_ids([channel_id])
        for ch in channels:
            store_channel(conn, ch)
        row = conn.execute(
            "SELECT uploads_playlist_id FROM channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        playlist_id = row["uploads_playlist_id"] if row else None
    if not playlist_id:
        return {"channel_id": channel_id, "scanned": 0, "selected": 0,
                "written": 0, "new": 0, "reason": "no uploads playlist"}

    items = client.playlist_items(playlist_id, max_results=max_items)
    selected: list[str] = []
    seen: set[str] = set()
    for item in items:
        content = item.get("contentDetails") or {}
        snippet = item.get("snippet") or {}
        video_id = content.get("videoId")
        published = parse_iso_utc(snippet.get("publishedAt"))
        if published is None:
            continue
        if now - published > window:
            # Плейлист упорядочен от новых к старым — дальше листать незачем.
            break
        if video_id and video_id not in seen:
            seen.add(video_id)
            selected.append(video_id)

    stored = {"written": 0, "new": 0, "skipped": 0, "video_ids": []}
    known: set[str] = set()
    if selected:
        # W3-1 (идемпотентность): уже известные видео не запрашиваем повторно
        # через videos.list — только освежаем last_seen. Повторный прогон
        # дешёвой фазы не тратит квоту на уже собранное.
        known = _known_video_ids(conn, selected)
        if known:
            conn.executemany(
                "UPDATE videos SET last_seen=? WHERE video_id=?",
                [(now, vid) for vid in known],
            )
            conn.commit()
        fresh = [vid for vid in selected if vid not in known]
        if fresh:
            meta = client.videos_by_ids(fresh)
            usable = [m for m in meta if _is_usable(m)]
            stored = store_videos(conn, usable, f"uploads:{channel_id}", cfg)

    return {
        "channel_id": channel_id,
        "scanned": len(items),
        "selected": len(selected),
        "known": len(known),
        "written": stored["written"],
        "new": stored["new"],
        "video_ids": stored["video_ids"],
    }


def run_collect(conn: Any, cfg: Any = config, queries: Sequence[str] | None = None,
                client: Any | None = None, classify: bool = True,
                time_budget_seconds: int | None = None) -> dict[str, Any]:
    """Прогон сбора: обход uploads реестра, поиск, разбор, обход найденных каналов.

    W3-1: порядок фаз изменён — сначала дешёвый обход uploads подтверждённых
    ИИ-каналов ВСЕГО реестра (~1 unit/канал), затем урезанный тематический
    поиск (только обнаружение новых каналов), затем разбор и обход каналов,
    найденных поиском. Поиск по умолчанию — 2 запроса, дедуп и карантин пула
    (W3-2).

    - classify=True — после поиска разобрать новые неразобранные видео;
    - обход канала идёт только если у него >= MIN_AI_VIDEOS_PER_CHANNEL
      видео с is_ai=1 (сначала разбор, потом обход);
    - time_budget_seconds — жёсткий лимит прогона; при исчерпании прогон
      аккуратно останавливается, собранное сохраняется.
    """
    # W3-2: дедуп близких формулировок до ротации — курсор и слайсы видят
    # уже очищенный пул.
    raw_pool = list(getattr(cfg, "SEARCH_QUERIES", []) or [])
    full_pool, dropped_queries = config.dedupe_search_queries(raw_pool)
    if dropped_queries:
        log.info(
            "W3-2: пул запросов сжат с %d до %d (отсечено дублей %d)",
            len(raw_pool), len(full_pool), len(dropped_queries),
        )

    # Явный список запросов — обратная совместимость: ротацию не трогаем.
    explicit_queries = queries is not None
    rotation_path: Path | None = None
    per_run = 0
    cursor_before = 0
    cursor_after = 0
    query_stats: dict[str, Any] = {}
    active_pool = full_pool
    zero_limit = int(getattr(cfg, "QUERY_ZERO_YIELD_LIMIT",
                             config.QUERY_ZERO_YIELD_LIMIT))
    quarantine_sec = int(getattr(cfg, "QUERY_QUARANTINE_DAYS",
                                 config.QUERY_QUARANTINE_DAYS)) * 86400
    if explicit_queries:
        query_list = list(queries)
    else:
        rotation_path = _rotation_path(cfg)
        per_run = config.collect_queries_per_run(cfg)
        query_stats = load_query_stats(rotation_path)
        # W3-2: запросы на карантине не участвуют в ротации.
        active_pool = active_search_queries(
            full_pool, query_stats, cfg.now_ts(), zero_limit, quarantine_sec)
        cursor_before = load_cursor(rotation_path, len(active_pool))
        query_list = rotation_slice(active_pool, per_run, cursor_before)
    if client is None:
        client = make_client(conn)
    if time_budget_seconds is None:
        time_budget_seconds = getattr(cfg, "COLLECT_TIME_BUDGET_SECONDS",
                                      config.COLLECT_TIME_BUDGET_SECONDS)
    budget = int(time_budget_seconds) if time_budget_seconds else 0
    started = cfg.now_ts()
    deadline = started + budget if budget > 0 else None

    def over_budget() -> bool:
        return deadline is not None and cfg.now_ts() >= deadline

    def over_quota() -> bool:
        """ТЗ-12: остатка units по проекту нет — обход надо штатно оборвать."""
        return _quota_remaining_units(conn, cfg) <= 0

    def stop_reason() -> str | None:
        """Причина остановки прогона: 'time_budget', 'quota' или None.

        Проверяется перед каждым каналом обхода uploads, каждым поисковым
        запросом и каждым каналом фазы 3, поэтому исчерпанная квота обрывает
        прогон штатно, а не роняет его.
        """
        if over_budget():
            return "time_budget"
        if over_quota():
            return "quota"
        return None

    units_before = _quota_total(conn)
    min_ai = getattr(cfg, "MIN_AI_VIDEOS_PER_CHANNEL",
                     config.MIN_AI_VIDEOS_PER_CHANNEL)

    found = 0
    new_videos = 0
    channels: list[str] = []
    errors: list[str] = []
    requests = 0
    queries_done = 0
    queries_attempted = 0
    stopped_by_budget = False
    run_stop_reason: str | None = None
    search_guard_reason: str | None = None
    scanned_channels: set[str] = set()
    scanned = 0

    # --- Фаза 1 (W3-1): обход uploads подтверждённых ИИ-каналов реестра -------
    registry_channels = _confirmed_ai_channels(conn, min_ai)
    uploads_limit = config.collect_uploads_channels_per_run(cfg)
    uploads_path = _uploads_rotation_path(cfg)
    uploads_cursor_before = load_cursor(uploads_path, len(registry_channels))
    registry_batch = rotation_slice(registry_channels, uploads_limit,
                                    uploads_cursor_before)
    units_uploads_before = _quota_total(conn)
    up = _scan_uploads_batch(
        conn, registry_batch, cfg, client, stop_reason, errors, min_ai,
        scanned_channels,
    )
    units_uploads = _quota_total(conn) - units_uploads_before
    new_videos += up["new"]
    requests += up["requests"]
    scanned += up["scanned"]
    if up["stopped"]:
        run_stop_reason = up["stop_reason"]
        if up["stop_reason"] == "time_budget":
            stopped_by_budget = True
    uploads_cursor_after = uploads_cursor_before
    if registry_channels:
        uploads_cursor_after = (
            uploads_cursor_before + up["attempted"]) % len(registry_channels)
        save_cursor(uploads_path, uploads_cursor_after, len(registry_channels))
    log.info(
        "W3-1: реестр uploads: каналов %d, порция %d, обойдено %d, курсор %d -> %d",
        len(registry_channels), len(registry_batch), up["scanned"],
        uploads_cursor_before, uploads_cursor_after,
    )

    # --- Фаза 2 (W3-1/W3-2): урезанный поиск новых каналов -------------------
    units_search_before = _quota_total(conn)
    executed_queries: list[tuple[str, int]] = []
    for query in query_list:
        reason = stop_reason()
        if reason:
            run_stop_reason = reason
            if reason == "time_budget":
                stopped_by_budget = True
            break
        # Слот ротации считается использованным даже при сбое поиска, иначе
        # упавший запрос навсегда застрянет в начале окна.
        queries_attempted += 1
        try:
            res = search_topic(conn, query, cfg, client=client)
        except yt.SearchQuotaGuard as exc:
            # Отдельный лимит вызовов search.list исчерпан: вызов не отправлен,
            # units не потрачены. Прогон не падает — дальше работают бесплатные
            # механизмы (разбор, обход уже найденных каналов).
            log.warning("поисковая квота: %s", exc)
            search_guard_reason = str(exc)
            queries_attempted -= 1  # запрос не отправляли — курсор не двигаем
            break
        except yt.YouTubeError as exc:
            log.warning("поиск не удался (%s): %s", query, exc)
            errors.append(f"search:{query}: {exc}")
            continue
        requests += 1
        queries_done += 1
        found += res["found"]
        new_videos += res["new"]
        executed_queries.append((query, res["found"]))
        for cid in res["channel_ids"]:
            if cid not in channels:
                channels.append(cid)
    units_search = _quota_total(conn) - units_search_before

    # Сдвигаем курсор на длину фактически выполненной порции (после прогона).
    if rotation_path is not None and active_pool:
        cursor_after = (cursor_before + queries_attempted) % len(active_pool)
        save_cursor(rotation_path, cursor_after, len(active_pool))
        log.info(
            "запросы: порция %d из %d, курсор %d -> %d",
            len(query_list), len(active_pool), cursor_before, cursor_after,
        )
    # W3-2: учесть отдачу выполненных запросов (пустые серии → карантин).
    if rotation_path is not None and executed_queries:
        for query, q_found in executed_queries:
            record_query_result(query_stats, query, q_found, cfg.now_ts(),
                                zero_limit, quarantine_sec)
        save_query_stats(rotation_path, query_stats)

    # Разбор новых видео до обхода: без is_ai=1 канал не подтверждён.
    if classify:
        pending = db.get_unclassified(conn, limit=1)
        if pending:
            from . import classify as classify_mod  # локально: нет цикла импорта
            try:
                classify_mod.classify_videos(conn, cfg)
            except Exception as exc:  # разбор упал — прогон продолжается
                log.warning("разбор не удался: %s", exc)
                errors.append(f"classify: {exc}")

    # --- Фаза 3 (W3-1): обход каналов, найденных поиском ---------------------
    # Подтверждённые реестровые каналы уже обойдены в фазе 1 (scanned_channels).
    for channel_id in channels:
        reason = stop_reason()
        if reason:
            run_stop_reason = reason
            if reason == "time_budget":
                stopped_by_budget = True
            break
        if channel_id in scanned_channels:
            continue
        if not _is_ai_channel(conn, channel_id, min_ai):
            continue  # не подтверждён как ИИ-канал — не обходим
        try:
            res = collect_uploads(conn, channel_id, cfg, client=client)
        except yt.YouTubeError as exc:
            log.warning("обход канала не удался (%s): %s", channel_id, exc)
            errors.append(f"uploads:{channel_id}: {exc}")
            scanned_channels.add(channel_id)
            continue
        scanned_channels.add(channel_id)
        requests += 1
        scanned += 1
        new_videos += res.get("new", 0)

    return {
        "queries": len(query_list),
        "queries_done": queries_done,
        "requests": requests,
        "found": found,
        "new": new_videos,
        "channels_scanned": scanned,
        "units": _quota_total(conn) - units_before,
        # W3-1: расход измеряется раздельно, чтобы эффект был виден в run_log.
        "units_uploads": units_uploads,
        "units_search": units_search,
        "errors": errors,
        "stopped_by_budget": stopped_by_budget,
        # ТЗ-12: отдельная причина остановки и остаток units по проекту, чтобы
        # исчерпанная квота была видна в отчёте, а не только в warning.
        "stopped_by_quota": run_stop_reason == "quota",
        "stop_reason": run_stop_reason,
        "units_remaining": _quota_remaining_units(conn, cfg),
        # ТЗ-12: полный размер реестра подтверждённых ИИ-каналов — по нему
        # считается охват круга обхода (registry_total / batch за прогон).
        "registry_total": len(registry_channels),
        "search_guard": search_guard_reason,
        "queries_deduped": len(dropped_queries),
        "queries_quarantined": len(full_pool) - len(active_pool),
        "rotation": {
            "enabled": rotation_path is not None,
            "per_run": per_run,
            "full_len": len(full_pool),
            "active_len": len(active_pool),
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
        },
        "uploads_rotation": {
            "channels_total": len(registry_channels),
            "batch": len(registry_batch),
            "scanned": up["scanned"],
            "cursor_before": uploads_cursor_before,
            "cursor_after": uploads_cursor_after,
        },
    }
