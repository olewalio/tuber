"""Сбор верхних комментариев видео и харвест комментаторов (ТЗ-49).

Комментарии нужны отчёту («ЧТО ОБСУЖДАЮТ») и очереди кандидатов: авторы с
достаточным числом лайков — это готовый пул людей, которые пишут по делу.

Правила честности и экономии:
- отбор видео идёт по чужим, уже посчитанным данным: сначала топ виральных
  видео (``report.viral_top``, ТЗ-45), затем лидеры роста комментариев
  (``report.comment_leaders``), затем видео топ-каналов (для них нужен
  API-клиент), и лишь при недоборе — добор по абсолютному числу комментариев
  среди видео с ИИ-разметкой;
- каждый видео-таргет — несколько страниц ``commentThreads`` (пагинация через
  ``nextPageToken``), по 1 unit за страницу до 100 комментариев; расход units
  показывается числом и за прогон, и за сутки;
- свежесть: видео пропускается, если по нему уже есть свежая запись в
  comment_checks (результат любого обращения, включая пустые и отключённые
  комментарии) или свежие строки в video_comments — не платим дважды;
- харвест комментаторов: авторы комментариев с likes ≥ порога (по умолчанию
  50) попадают в очередь кандидатов (таблица ``candidate``, как в ТЗ-46) с
  пометкой происхождения «комментатор вирального видео»; дедупликация по
  UNIQUE(platform, handle) и отсечение уже известных источников;
- ничего не выдумываем: нет ответа API — пишем в errors, а не подставляем
  примеры;
- сбой по одному видео (сеть, доступ, отключённые комментарии) не валит
  прогон целиком: считаем в errors/skipped/disabled и идём дальше.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from tuber.core import storage, timeutil

from . import config, report, api as yt
from . import store as db

log = logging.getLogger(__name__)

# Во сколько раз больше кандидатов брать, чем реально нужно обращений: часть
# отобранных видео окажется свежей и будет пропущена без вызова API.
_CANDIDATE_POOL_FACTOR = 3

#: Метка происхождения комментатора в очереди кандидатов (ТЗ-49 п.2).
COMMENT_ORIGIN = "комментатор вирального видео"
#: Код ``found_via`` для комментаторов (короткий машинный идентификатор).
COMMENT_FOUND_VIA = "comment_harvest"


def _now(cfg: Any) -> int:
    """Текущее время в unixtime (через cfg, если он это умеет)."""
    now = getattr(cfg, "now_ts", None)
    return int(now()) if callable(now) else int(config.now_ts())


def _cfg_int(cfg: Any, name: str) -> int:
    return int(getattr(cfg, name, getattr(config, name)))


def select_videos(conn: Any, cfg: Any = config, limit: int | None = None) -> list[str]:
    """Отобрать video_id для сбора комментариев (база, без сети).

    Сначала — лидеры скорости роста комментариев. Если их меньше limit,
    добираем по абсолютному числу комментариев из последних замеров среди
    видео с ИИ-разметкой (is_ai = 1), без повторов.
    """
    if limit is None:
        limit = _cfg_int(cfg, "COMMENT_MAX_VIDEOS_PER_RUN")
    limit = max(0, int(limit))
    if limit == 0:
        return []

    leaders = report.comment_leaders(conn, limit=limit)
    ids: list[str] = []
    seen: set[str] = set()
    for it in leaders:
        vid = it.get("video_id")
        if vid and vid not in seen:
            seen.add(vid)
            ids.append(vid)

    if len(ids) < limit:
        rows = conn.execute(
            """
            SELECT s.video_id AS video_id
            FROM snapshots s
            JOIN (
                SELECT video_id, MAX(id) AS mid FROM snapshots GROUP BY video_id
            ) t ON t.mid = s.id
            JOIN video_classification vc ON vc.video_id = s.video_id
            WHERE vc.is_ai = 1 AND s.comments IS NOT NULL
            ORDER BY s.comments DESC, s.video_id ASC
            """
        ).fetchall()
        for r in rows:
            if len(ids) >= limit:
                break
            vid = r["video_id"]
            if vid and vid not in seen:
                seen.add(vid)
                ids.append(vid)

    return ids[:limit]


def select_targets(conn: Any, cfg: Any = config, limit: int | None = None) -> list[str]:
    """Полный упорядоченный список видео для сбора за прогон.

    Порядок приоритетов (ТЗ-49 п.1): топ виральных видео → лидеры роста
    комментариев и добор по абсолюту (``select_videos``). Видео топ-каналов
    добавляются в :func:`run`, потому что требуют API-клиента. ``limit`` —
    число ВИДЕО; список берётся с запасом (свежие видео выпадают без вызова).
    """
    if limit is None:
        limit = _cfg_int(cfg, "COMMENT_MAX_VIDEOS_PER_RUN")
    limit = max(0, int(limit))
    if limit == 0:
        return []

    pool = limit * _CANDIDATE_POOL_FACTOR
    ids: list[str] = []
    seen: set[str] = set()

    def add(vid: Any) -> None:
        if vid and vid not in seen:
            seen.add(vid)
            ids.append(str(vid))

    viral_n = _cfg_int(cfg, "COMMENT_VIRAL_TOP_N")
    try:
        for it in report.viral_top(conn, days=_viral_window_days(cfg), limit=viral_n):
            add(it.get("video_id"))
    except Exception as exc:  # noqa: BLE001 — отбор не должен ронять прогон
        log.warning("comments: не удалось взять виральный топ (%s)", exc)

    for vid in select_videos(conn, cfg=cfg, limit=pool):
        add(vid)

    return ids[: max(limit, pool)]


def _viral_window_days(cfg: Any) -> int:
    return _cfg_int(cfg, "COMMENT_VIRAL_WINDOW_DAYS")


def select_top_channels(conn: Any, cfg: Any = config, n: int | None = None) -> list[dict]:
    """Топ каналов для сбора комментариев по свежим видео (ТЗ-49 п.1).

    Канал ранжируется по числу его видео в виральном топе, тай-брейк — сумма
    индексов виральности. Возвращает записи с ``channel_id`` и
    ``uploads_playlist_id`` (может быть пустым). Только БД, без сети.
    """
    if n is None:
        n = _cfg_int(cfg, "COMMENT_CHANNEL_TOP_N")
    n = max(0, int(n))
    if n == 0:
        return []
    try:
        top = report.viral_top(conn, days=_viral_window_days(cfg),
                               limit=_cfg_int(cfg, "COMMENT_VIRAL_TOP_N") * 4)
    except Exception as exc:  # noqa: BLE001
        log.warning("comments: каналы не ранжированы (%s)", exc)
        return []

    tally: dict[str, dict] = {}
    for it in top:
        row = conn.execute(
            "SELECT channel_id FROM videos WHERE video_id=?", (it.get("video_id"),)
        ).fetchone() if it.get("video_id") else None
        cid = row["channel_id"] if row is not None else None
        if not cid:
            continue
        rec = tally.setdefault(cid, {"channel_id": cid, "videos": 0, "index_sum": 0.0})
        rec["videos"] += 1
        rec["index_sum"] += float(it.get("viral_index") or 0.0)

    ranked = sorted(tally.values(), key=lambda r: (-r["videos"], -r["index_sum"]))
    out: list[dict] = []
    for rec in ranked[:n]:
        row = conn.execute(
            "SELECT title, uploads_playlist_id FROM channels WHERE channel_id=?",
            (rec["channel_id"],),
        ).fetchone()
        out.append({
            "channel_id": rec["channel_id"],
            "title": (row["title"] if row is not None else None),
            "uploads_playlist_id": (row["uploads_playlist_id"] if row is not None else None),
            "videos_in_top": rec["videos"],
        })
    return out


def _is_fresh(conn: Any, video_id: str, fresh_before: int) -> bool:
    """Уже проверяли видео недавно?

    Признак — свежая запись в comment_checks (любой статус: даже пустой или
    отключённый ответ стоил 1 unit) либо свежие строки в video_comments
    (совместимость с базой, где comment_checks ещё не заполнена).
    """
    row = conn.execute(
        "SELECT checked_at FROM comment_checks WHERE video_id=?",
        (video_id,),
    ).fetchone()
    if row is not None and row["checked_at"] is not None:
        if int(row["checked_at"]) >= fresh_before:
            return True
    row = conn.execute(
        "SELECT MAX(captured_at) AS last FROM video_comments WHERE video_id=?",
        (video_id,),
    ).fetchone()
    last = row["last"] if row is not None else None
    return last is not None and int(last) >= fresh_before


def save_check(
    conn: Any,
    video_id: str,
    status: str,
    error: str | None,
    checked_at: int,
) -> None:
    """Записать результат последнего обращения к commentThreads по видео.

    status: ok (комментарии записаны), empty (пустой ответ), disabled
    (403 commentsDisabled/forbidden), error (сеть/иное). Одна строка на
    видео — повторное обращение обновляет её.
    """
    db.upsert_comment_check(conn, video_id, checked_at=int(checked_at),
                            status=status, error=error)


def _rank(comments: Iterable[dict]) -> list[dict]:
    """Отсортировать по лайкам (убывание), отбросив записи без comment_id."""
    clean = [c for c in comments if c.get("comment_id")]
    return sorted(
        clean,
        key=lambda c: c["likes"] if isinstance(c.get("likes"), int) else -1,
        reverse=True,
    )


def save_comments(
    conn: Any,
    video_id: str,
    comments: Iterable[dict],
    max_per_video: int,
    captured_at: int,
) -> int:
    """Записать верхние комментарии видео. Возвращает число новых строк.

    INSERT OR IGNORE: повтор уже пойманного comment_id не создаёт дубль и не
    учитывается в счётчике.
    """
    rows = [
        {
            "comment_id": str(c["comment_id"]),
            "author": c.get("author"),
            "text": c.get("text"),
            "likes": c.get("likes"),
            "published_at": c.get("published_at"),
            "captured_at": captured_at,
        }
        for c in _rank(comments)[: max(0, int(max_per_video))]
    ]
    return db.add_comments(conn, video_id, rows)


def harvest_commenters(
    conn: Any,
    comments: Iterable[dict],
    cfg: Any = config,
    now: int | None = None,
    video_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Положить авторов «залайканных» комментариев в очередь кандидатов.

    Берутся только комментарии с likes ≥ COMMENT_HARVEST_MIN_LIKES и известным
    ``author_channel_id``. Дедупликация: в пределах прогона — по каналу автора,
    в базе — UNIQUE(platform, handle) плюс отсечение уже известных источников
    (``source``). Повторный прогон инкрементит ``seen_count``, а не плодит
    строки. Возвращает сводку ``{"added", "bumped", "threshold", "found"}``.
    """
    threshold = _cfg_int(cfg, "COMMENT_HARVEST_MIN_LIKES")
    now = _now(cfg) if now is None else int(now)
    iso = timeutil.epoch_to_iso(now)

    best: dict[str, dict] = {}
    for c in comments:
        cid = c.get("author_channel_id")
        likes = c.get("likes")
        if not cid or not isinstance(likes, int) or likes < threshold:
            continue
        rec = best.get(cid)
        if rec is None or likes > rec["likes"]:
            best[cid] = {"likes": likes, "author": c.get("author")}

    summary = {"added": 0, "bumped": 0, "threshold": threshold, "found": len(best)}
    for cid, rec in best.items():
        if conn.execute(
            "SELECT 1 FROM source WHERE platform='youtube' AND "
            "(external_id=? OR handle=?) LIMIT 1",
            (cid, cid),
        ).fetchone():
            continue
        row = conn.execute(
            "SELECT id, seen_count FROM candidate WHERE platform='youtube' AND handle=?",
            (cid,),
        ).fetchone()
        if row is not None:
            summary["bumped"] += 1
            if not dry_run:
                conn.execute(
                    "UPDATE candidate SET seen_count=COALESCE(seen_count,1)+1, "
                    "last_seen_at=?, meta_json=? WHERE id=?",
                    (iso,
                     storage.jdump({
                         "origin": COMMENT_ORIGIN,
                         "found_via": COMMENT_FOUND_VIA,
                         "best_likes": rec["likes"],
                     }),
                     row["id"]),
                )
        else:
            summary["added"] += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO candidate(platform, kind, handle, found_via, "
                    "display_handle, meta_json, first_seen_at, last_seen_at, "
                    "status, validated) "
                    "VALUES('youtube','channel',?,'" + COMMENT_FOUND_VIA + "',?,?,?,?,"
                    "'new','pending')",
                    (cid, rec["author"],
                     storage.jdump({
                         "origin": COMMENT_ORIGIN,
                         "found_via": COMMENT_FOUND_VIA,
                         "found_in_video": video_id,
                         "best_likes": rec["likes"],
                         "harvested_at": iso,
                     }),
                     iso, iso),
                )
    if not dry_run:
        conn.commit()
    return summary


def _comment_units(conn: Any) -> int | None:
    """Сумма units по endpoint commentThreads из quota_log (если доступна)."""
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS u FROM quota_log "
            "WHERE endpoint = 'commentThreads'"
        ).fetchone()
    except Exception:  # noqa: BLE001 - нет таблицы/другой драйвер
        return None
    if row is None:
        return None
    try:
        return int(row["u"])
    except (TypeError, ValueError):
        return None


def units_today(conn: Any, day: str | None = None) -> int | None:
    """Расход units YouTube по commentThreads за сутки (quota_usage)."""
    day = day or timeutil.epoch_to_iso(_now(config))[:10]
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS u FROM quota_usage "
            "WHERE platform='youtube' AND endpoint='commentThreads' AND day=?",
            (day,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    try:
        return int(row["u"])
    except (TypeError, ValueError):
        return None


def _channel_videos(client: Any, chan: dict, cfg: Any) -> list[dict]:
    """Свежие видео канала по uploads-плейлисту (1 unit за вызов).

    Возвращает словари ``{"video_id", "title", "published_at"}``: их нужно
    завести в ``content``, иначе комментарии не к чему привязать.
    """
    uploads = chan.get("uploads_playlist_id")
    if not uploads or not hasattr(client, "playlist_items"):
        return []
    try:
        items = client.playlist_items(
            uploads, max_results=_cfg_int(cfg, "COMMENT_CHANNEL_VIDEOS"))
    except Exception as exc:  # noqa: BLE001 — один канал не валит прогон
        log.warning("comments: видео канала %s не получены (%s)",
                    chan.get("channel_id"), exc)
        return []
    from .collect import parse_iso_utc

    out: list[dict] = []
    for it in items:
        cd = it.get("contentDetails") or {}
        sn = it.get("snippet") or {}
        vid = cd.get("videoId") or (sn.get("resourceId") or {}).get("videoId")
        if not vid:
            continue
        out.append({
            "video_id": vid,
            "title": sn.get("title"),
            "published_at": parse_iso_utc(
                cd.get("videoPublishedAt") or sn.get("publishedAt")),
        })
    return out[:_cfg_int(cfg, "COMMENT_CHANNEL_VIDEOS")]


def run(
    conn: Any,
    cfg: Any = config,
    client: Any = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Собрать комментарии по отобранным видео и харвестить комментаторов.

    Сводка: ``{"videos", "comments", "skipped", "disabled", "errors", "units",
    "pages", "harvested", "units_today", "dry_run"}``. ``limit`` — число ВИДЕО
    за прогон; на каждое тратится до COMMENT_PAGES_PER_VIDEO страниц (1 unit за
    страницу). Свежие видео пропускаются без вызова и не съедают лимит.
    """
    if limit is None:
        limit = _cfg_int(cfg, "COMMENT_MAX_VIDEOS_PER_RUN")
    limit = max(0, int(limit))

    # Отбор идёт по чужим таблицам YouTube (snapshots/videos/video_scores).
    # На соединении без слоя совместимости (например, ядровое соединение CLI
    # `tuber comments youtube`) их нет: ставим слой идемпотентно.
    try:
        db.install_compat(conn)
    except Exception as exc:  # noqa: BLE001 — не ронять отбор из-за вида
        log.debug("comments: install_compat пропущен (%s)", exc)

    pages_limit = max(1, _cfg_int(cfg, "COMMENT_PAGES_PER_VIDEO"))
    fetch_max = _cfg_int(cfg, "COMMENT_FETCH_MAX")
    max_per_video = _cfg_int(cfg, "COMMENT_MAX_PER_VIDEO")
    cost = _cfg_int(cfg, "COST_COMMENT_THREADS")
    # Окно свежести — короткое из двух: суточный виральный обход (ТЗ-49)
    # должен возвращаться к тому же топу раз в сутки, а семисуточное окно
    # COMMENT_REFRESH_DAYS остаётся страховкой от слишком частых обращений.
    fresh_before = _now(cfg) - min(
        _cfg_int(cfg, "COMMENT_SWEEP_FRESH_HOURS"),
        _cfg_int(cfg, "COMMENT_REFRESH_DAYS") * 24,
    ) * 3600
    captured_at = _now(cfg)

    ids = select_targets(conn, cfg=cfg, limit=limit)

    summary = {
        "videos": 0, "comments": 0, "skipped": 0, "disabled": 0,
        "errors": 0, "units": 0, "pages": 0,
        "harvested": {"added": 0, "bumped": 0},
        "units_today": None, "dry_run": bool(dry_run),
    }

    if dry_run:
        summary["planned"] = len(ids)
        summary["units"] = 0
        summary["units_today"] = units_today(conn)
        return summary

    if client is None:
        client = yt.YouTubeClient(conn=conn)

    # Видео топ-каналов (нужен API-клиент): заводим их в content, чтобы
    # комментарии было к чему привязать, и добавляем в обход.
    for chan in select_top_channels(conn, cfg=cfg):
        for v in _channel_videos(client, chan, cfg):
            vid = v["video_id"]
            try:
                db.upsert_video(conn, {
                    "video_id": vid,
                    "channel_id": chan.get("channel_id"),
                    "title": v.get("title"),
                    "published_at": v.get("published_at"),
                    "first_seen": captured_at,
                })
            except Exception as exc:  # noqa: BLE001
                log.warning("comments: видео %s канала %s не заведено (%s)",
                            vid, chan.get("channel_id"), exc)
            if vid not in ids:
                ids.append(vid)

    before = _comment_units(conn)
    attempts = 0
    paginate = hasattr(client, "comment_threads_page")

    for video_id in ids:
        if attempts >= limit:
            break
        if _is_fresh(conn, video_id, fresh_before):
            summary["skipped"] += 1
            continue
        attempts += 1
        collected: list[dict] = []
        page_token = None
        pages = 0
        failed = False
        for _page in range(pages_limit):
            try:
                if paginate:
                    items, page_token = client.comment_threads_page(
                        video_id, max_results=fetch_max, page_token=page_token)
                else:
                    items = client.comment_threads(video_id, max_results=fetch_max)
                    page_token = None
            except yt.YouTubeError as exc:
                if exc.code == 403:
                    # 403 commentThreads — свойство видео (commentsDisabled или
                    # forbidden), ключ рабочий: видео пропускаем, пишем статус.
                    summary["disabled"] += 1
                    save_check(conn, video_id, "disabled", str(exc), captured_at)
                else:
                    summary["errors"] += 1
                    save_check(conn, video_id, "error", str(exc), captured_at)
                    log.warning("comments: сбой по видео %s (%s)", video_id, exc)
                failed = True
                break
            except Exception as exc:  # noqa: BLE001 - один сбой не валит прогон
                summary["errors"] += 1
                save_check(conn, video_id, "error", str(exc), captured_at)
                log.warning("comments: сбой по видео %s (%s)", video_id, exc)
                failed = True
                break
            pages += 1
            collected.extend(items)
            if not items or not page_token:
                break
        if failed:
            continue
        summary["videos"] += 1
        summary["pages"] += pages
        if collected:
            summary["comments"] += save_comments(
                conn, video_id, collected, max_per_video, captured_at
            )
            save_check(conn, video_id, "ok", None, captured_at)
            h = harvest_commenters(conn, collected, cfg=cfg, now=captured_at,
                                   video_id=video_id, dry_run=dry_run)
            summary["harvested"]["added"] += h["added"]
            summary["harvested"]["bumped"] += h["bumped"]
        else:
            # Пустой ответ: комментариев нет либо они закрыты (403 отсечён
            # выше) — фиксируем факт проверки, чтобы не платить снова.
            save_check(conn, video_id, "empty", None, captured_at)

    after = _comment_units(conn)
    if before is not None and after is not None and after - before > 0:
        summary["units"] = after - before
    else:
        # Нет строк в quota_log (фейковый клиент/ещё не сконфигурированный
        # quota_usage): честная нижняя оценка по числу сделанных обращений.
        summary["units"] = max(attempts, summary["pages"]) * cost
    summary["units_today"] = units_today(conn)
    return summary


def format_summary(summary: dict) -> str:
    """Короткая человеческая сводка по прогону."""
    h = summary.get("harvested") or {}
    today = summary.get("units_today")
    today_phrase = f", за сутки {today}" if today is not None else ""
    return (
        f"Комментарии: разобрано видео {summary.get('videos', 0)}, "
        f"страниц {summary.get('pages', 0)}, "
        f"записано строк {summary.get('comments', 0)}, "
        f"пропущено (свежие) {summary.get('skipped', 0)}, "
        f"отключено (403) {summary.get('disabled', 0)}, "
        f"ошибок {summary.get('errors', 0)}, "
        f"комментаторов в очередь +{h.get('added', 0)} "
        f"(в базе уже {h.get('bumped', 0)}), "
        f"расход {summary.get('units', 0)} units{today_phrase}."
    )
