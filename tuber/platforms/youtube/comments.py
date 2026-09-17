"""Сбор верхних комментариев видео (закрытие таблицы video_comments).

Комментарии нужны отчёту («ЧТО ОБСУЖДАЮТ»): там уже есть скорость роста
комментариев по замерам, а здесь мы складываем сами тексты верхних
комментариев, чтобы показать, что именно обсуждают.

Правила честности и экономии:
- отбор видео идёт по чужим, уже посчитанным данным: сначала лидеры роста
  комментариев (report.comment_leaders), при недоборе — добор по абсолютному
  числу комментариев из последних замеров среди видео с ИИ-разметкой;
- свежесть: видео пропускается, если по нему уже есть свежая запись в
  comment_checks (результат любого обращения, включая пустые и отключённые
  комментарии) или свежие строки в video_comments — не платим дважды;
- каждый реально сделанный вызов commentThreads фиксируется в comment_checks
  статусом ok/empty/disabled/error; 403 на этом эндпоинте — свойство видео
  (commentsDisabled/forbidden), ключ при этом не бракуется;
- ничего не выдумываем: нет ответа API — пишем в errors, а не подставляем
  примеры;
- сбой по одному видео (сеть, доступ, отключённые комментарии) не валит
  прогон целиком: считаем в errors/skipped/disabled и идём дальше.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from . import config, report, api as yt
from . import store as db

log = logging.getLogger(__name__)

# Во сколько раз больше кандидатов брать, чем реально нужно обращений: часть
# отобранных видео окажется свежей и будет пропущена без вызова API.
_CANDIDATE_POOL_FACTOR = 3


def _now(cfg: Any) -> int:
    """Текущее время в unixtime (через cfg, если он это умеет)."""
    now = getattr(cfg, "now_ts", None)
    return int(now()) if callable(now) else int(config.now_ts())


def _cfg_int(cfg: Any, name: str) -> int:
    return int(getattr(cfg, name, getattr(config, name)))


def select_videos(conn: Any, cfg: Any = config, limit: int | None = None) -> list[str]:
    """Отобрать video_id для сбора комментариев.

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


def run(
    conn: Any,
    cfg: Any = config,
    client: Any = None,
    limit: int | None = None,
) -> dict:
    """Собрать верхние комментарии по отобранным видео.

    Сводка: {"videos", "comments", "skipped", "disabled", "errors", "units"}.
    Лимит — это число реально сделанных обращений: уже проверенные (свежие)
    видео пропускаются и не съедают его, поэтому кандидатов берём с запасом.
    """
    if client is None:
        client = yt.YouTubeClient(conn=conn)
    if limit is None:
        limit = _cfg_int(cfg, "COMMENT_MAX_VIDEOS_PER_RUN")
    limit = max(0, int(limit))

    fetch_max = _cfg_int(cfg, "COMMENT_FETCH_MAX")
    max_per_video = _cfg_int(cfg, "COMMENT_MAX_PER_VIDEO")
    cost = _cfg_int(cfg, "COST_COMMENT_THREADS")
    fresh_before = _now(cfg) - _cfg_int(cfg, "COMMENT_REFRESH_DAYS") * 86400
    captured_at = _now(cfg)

    # Свежие видео не должны исчерпывать лимит: берём кандидатов с запасом,
    # чтобы после пропуска уже проверенных добрать проверяемые (иначе прогон
    # сдаётся нулём). Ограничение по-прежнему на число обращений к API.
    pool = limit * _CANDIDATE_POOL_FACTOR
    ids = select_videos(conn, cfg=cfg, limit=pool)

    before = _comment_units(conn)
    summary = {
        "videos": 0, "comments": 0, "skipped": 0, "disabled": 0,
        "errors": 0, "units": 0,
    }
    attempts = 0

    for video_id in ids:
        if attempts >= limit:
            break
        if _is_fresh(conn, video_id, fresh_before):
            summary["skipped"] += 1
            continue
        attempts += 1
        try:
            comments = client.comment_threads(video_id, max_results=fetch_max)
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
            continue
        except Exception as exc:  # noqa: BLE001 - один сбой не валит прогон
            summary["errors"] += 1
            save_check(conn, video_id, "error", str(exc), captured_at)
            log.warning("comments: сбой по видео %s (%s)", video_id, exc)
            continue
        summary["videos"] += 1
        if comments:
            summary["comments"] += save_comments(
                conn, video_id, comments, max_per_video, captured_at
            )
            save_check(conn, video_id, "ok", None, captured_at)
        else:
            # Пустой ответ: комментариев нет либо они закрыты (403 отсечён
            # выше) — фиксируем факт проверки, чтобы не платить снова.
            save_check(conn, video_id, "empty", None, captured_at)
    after = _comment_units(conn)
    if before is not None and after is not None and after - before > 0:
        summary["units"] = after - before
    else:
        summary["units"] = attempts * cost
    return summary


def format_summary(summary: dict) -> str:
    """Короткая человеческая сводка по прогону."""
    return (
        f"Комментарии: разобрано видео {summary.get('videos', 0)}, "
        f"записано строк {summary.get('comments', 0)}, "
        f"пропущено (свежие) {summary.get('skipped', 0)}, "
        f"отключено (403) {summary.get('disabled', 0)}, "
        f"ошибок {summary.get('errors', 0)}, "
        f"расход {summary.get('units', 0)} units."
    )
