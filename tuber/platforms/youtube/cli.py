"""Консольный вход Tuber_OS (этап 7).

Команды:
- collect   — прогон сбора и разбор собранного;
- snapshots — снять замеры по плану;
- classify  — разобрать неразобранные видео;
- report    — текстовый (или JSON) отчёт по базе;
- migrate-shorts — пересчитать признак шортса у всего пула по длительности;
- migrate-speed — этап 10: качество интервалов и обнуление коротких скоростей;
- daily     — полный суточный цикл: сбор → разбор → замеры → отчёт.

Особенности:
- при старте подхватывается /root/.hermes/.env (запуск из крона работает без source);
- защита от параллельного запуска через fcntl.flock;
- логи в data/tuber.log; никакой публикации и сообщений в Telegram.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import (
    candidates,
    classify,
    collect,
    comments,
    config,
    store as db,
    expand,
    report,
    schedule,
    seo,
    thumbs,
    viral,
)

log = logging.getLogger(__name__)

# --- пути (выводятся из config, чтобы тесты могли подменить) ---------------

DATA_DIR = config.TUBER_DIR / "data"
DB_PATH = config.DB_PATH
LOCK_PATH = DATA_DIR / ".lock"
LOG_PATH = DATA_DIR / "tuber.log"

# Единственный файловый обработчик лога этого процесса.
_log_handler: logging.FileHandler | None = None


# --- инфраструктура --------------------------------------------------------


def _setup_logging() -> None:
    """Настроить дописывающий лог data/tuber.log (идемпотентно)."""
    global _log_handler
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    target = str(Path(LOG_PATH).resolve())
    if _log_handler is not None and getattr(_log_handler, "baseFilename", None) == target:
        return
    if _log_handler is not None:
        root.removeHandler(_log_handler)
        _log_handler.close()
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    _log_handler = handler
    root.addHandler(handler)


class _Lock:
    """Неблокирующий файловый замок (fcntl.flock)."""

    def __init__(self, path: str | Path) -> None:
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True

    def release(self) -> None:
        if self.fh is not None:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None


def _connect(init: bool = True) -> Any:
    """Открыть БД и (по умолчанию) гарантировать схему.

    init=False нужен переразбору: он сам синхронизирует справочник тем и по
    разнице честно показывает topics_added (при init_db темы уже добавлены).
    """
    conn = db.connect(DB_PATH)
    if init:
        db.init_db(conn)
    return conn


def _emit(payload: dict[str, Any], text: str, as_json: bool) -> None:
    """Напечатать результат: ровно один JSON-объект либо человеческий текст."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(text)


def _log_summary(command: str, summary: Any) -> None:
    """Записать итог прогона в лог одной строкой."""
    log.info(
        "ИТОГ %s: %s",
        command,
        json.dumps(summary, ensure_ascii=False, default=str),
    )


# --- команды ---------------------------------------------------------------


def cmd_collect(args: argparse.Namespace) -> int:
    """Сбор (поиск + разбор новых + обход ИИ-каналов)."""
    queries = None
    if getattr(args, "all_queries", False):
        # Весь пул сразу, ротацию не двигаем (внутри run_collect это явный
        # список запросов — курсор остаётся на месте).
        queries = list(config.SEARCH_QUERIES)
    elif args.queries is not None:
        queries = list(config.SEARCH_QUERIES[: max(0, args.queries)])
    if getattr(args, "reset_rotation", False):
        # Сброс курсора в 0 до прогона (ротация — поведение по умолчанию).
        collect.save_cursor(
            collect._rotation_path(config), 0, len(config.SEARCH_QUERIES)
        )
    conn = _connect()
    try:
        summary = collect.run_collect(
            conn,
            config,
            queries=queries,
            classify=not args.no_classify,
            time_budget_seconds=args.budget,
        )
    finally:
        conn.close()
    _log_summary("collect", summary)

    text = (
        f"Сбор завершён: запросов {summary['queries']} "
        f"(выполнено {summary['queries_done']}), найдено видео {summary['found']}, "
        f"новых {summary['new']}, каналов обойдено {summary['channels_scanned']}, "
        f"расход квоты {summary['units']} units."
    )
    if summary.get("stopped_by_budget"):
        text += " Остановлен по лимиту времени."
    elif summary.get("stopped_by_quota"):
        text += " Остановлен по исчерпанию квоты."
    elif summary.get("stop_reason"):
        text += f" Остановлен: {summary['stop_reason']}."
    if summary.get("search_guard"):
        text += f" Поиск пропущен: {summary['search_guard']}."
    if summary.get("errors"):
        text += f" Ошибок: {len(summary['errors'])}."
    _emit({"command": "collect", **summary}, text, args.json)
    return 0


def cmd_snapshots(args: argparse.Namespace) -> int:
    """Снять замеры по плану; ``--fill-gap N`` добавляет добор архивных (D-47)."""
    conn = _connect()
    try:
        summary = schedule.run_snapshots(
            conn, config, fill_gap=int(getattr(args, "fill_gap", 0) or 0))
    finally:
        conn.close()
    _log_summary("snapshots", summary)

    quota = summary.get("quota_remaining")
    quota_text = "n/a" if quota is None else str(quota)
    text = (
        f"Замеры: по плану {summary['planned']}, добор архивных "
        f"{summary.get('fill_gap', 0)} "
        f"(запрошено {summary.get('fill_gap_requested', 0)}), "
        f"снято {summary['captured']}, батчей {summary['batches']}, "
        f"сбойных батчей {summary['failed_batches']}, "
        f"запросов videos.list {summary.get('requests', summary['batches'])} "
        f"(остаток квоты {quota_text})."
    )
    _emit({"command": "snapshots", **summary}, text, args.json)
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    """Разобрать неразобранные видео или переразобрать размеченные."""
    reclassify_mode = getattr(args, "reclassify", False)
    # Переразбор сам синхронизирует темы: без init_db topics_added осмыслен.
    conn = _connect(init=not reclassify_mode)
    try:
        if reclassify_mode:
            summary = classify.reclassify(
                conn,
                limit=args.limit,
                dry_run=getattr(args, "dry_run", False),
                only_ai=not getattr(args, "all_ai", False),
            )
        else:
            summary = classify.classify_videos(conn, config, limit=args.limit)
    finally:
        conn.close()
    _log_summary("classify", summary)

    if reclassify_mode:
        added = ", ".join(summary.get("topics_added") or []) or "нет"
        text = (
            f"переразбор: обработано {summary['done']} из {summary['selected']}, "
            f"ошибок {summary['errors']}, новые темы: {added}, "
            f"${summary['cost_usd']}"
        )
    else:
        text = (
            f"Разбор: к разбору {summary['requested']}, разобрано {summary['classified']}, "
            f"ИИ {summary['ai']}, не ИИ {summary['not_ai']}, "
            f"сбоев {summary['failed']}, стоимость ${summary['cost_usd']}."
        )
    _emit({"command": "classify", **summary}, text, args.json)
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    """Собрать верхние комментарии по отобранным видео."""
    conn = _connect()
    try:
        summary = comments.run(conn, config, limit=args.limit)
    finally:
        conn.close()
    _log_summary("comments", summary)
    _emit(
        {"command": "comments", **summary},
        comments.format_summary(summary),
        args.json,
    )
    return 0


def _comment_leaders_json(conn: Any, fmt: str, limit: int = 20) -> list[dict]:
    """Лидеры по комментариям + лучший комментарий к каждому (как в тексте).

    Текстовый отчёт печатает под числами самый залайканный комментарий
    (report.top_comment); чтобы потребитель JSON видел то же самое, добавляем
    к каждой позиции поле best_comment: текст, автор, лайки и ссылка на видео.
    """
    items = report.comment_leaders(conn, limit=limit, fmt=fmt)
    for it in items:
        best = report.top_comment(conn, it["video_id"])
        if best and best.get("text"):
            it["best_comment"] = {
                "text": best.get("text"),
                "author": best.get("author"),
                "likes": best.get("likes"),
                "video_url": it["url"],
            }
        else:
            it["best_comment"] = None
    return items


def _stream_json(conn: Any, days: int, fmt: str,
                 topic: str | None = None) -> dict[str, Any]:
    """Данные одного формата для машинного отчёта."""
    return {
        "rising": report.rising(conn, days=days, limit=50, lang=None, fmt=fmt),
        "viral_top": report.viral_top(conn, days=days, limit=50, fmt=fmt, topic=topic),
        "viral_small_sample": report.viral_small_sample(
            conn, days=days, limit=20, fmt=fmt, topic=topic
        ),
        "viral_likes_zero": report.viral_likes_zero(
            conn, days=days, limit=10, fmt=fmt, topic=topic
        ),
        "viral_stats": report.viral_window_stats(conn, days=days, fmt=fmt, topic=topic),
        "topics": report.by_topic(conn, fmt=fmt),
        "comments": _comment_leaders_json(conn, fmt, limit=20),
        "ru_slice": report.ru_slice(conn, limit=20, fmt=fmt),
        "dark_horses": report.dark_horses(conn, limit=20, fmt=fmt),
        "seo": report.seo_pack(conn, limit=30, fmt=fmt),
    }


def _report_json(conn: Any, days: int, text: str, fmt: str = "all",
                 topic: str | None = None) -> dict[str, Any]:
    """Машинное представление отчёта (те же данные, что в тексте)."""
    stats = report._snapshot_stats(conn)
    streams = ("short", "long") if fmt == "all" else (fmt,)
    return {
        "command": "report",
        "generated_at": config.now_ts(),
        "days": days,
        "format": fmt,
        "topic": topic,
        "viral_half_life_days": config.viral_half_life_days(),
        "viral": report.viral_window_stats(conn, days=days, fmt=None, topic=topic),
        "snapshots": {
            "total": stats["total"],
            "videos": stats["videos"],
            "with_speed": stats["with_speed"],
        },
        "streams": {
            name: _stream_json(conn, days, name, topic=topic) for name in streams
        },
        "report_text": text,
    }


def cmd_report(args: argparse.Namespace) -> int:
    """Отчёт по базе (только чтение, без сети).

    Если индекс виральности не посчитан ни у одного видео на окне, отчёт прямо
    об этом предупреждает и возвращает код 1: пустой топ не должен выглядеть как
    «видео нет» (требование п.2.4).
    """
    fmt = getattr(args, "format", "all") or "all"
    topic = getattr(args, "topic", None)
    conn = _connect()
    try:
        text = report.build_report(conn, config, days=args.days, fmt=fmt, topic=topic)
        payload = _report_json(conn, args.days, text, fmt, topic=topic)
        viral_stats = payload["viral"]
    finally:
        conn.close()
    _log_summary(
        "report",
        {"days": args.days, "format": fmt, "topic": topic,
         "snapshots": payload["snapshots"], "viral": viral_stats},
    )
    _emit(payload, text, args.json)
    if viral_stats["candidates"] and viral_stats["indexed"] == 0:
        log.warning(
            "отчёт: индекс виральности не посчитан ни у одного видео на окне"
        )
        if not args.json:
            print(
                "ВНИМАНИЕ: индекс виральности не посчитан ни у одного видео "
                "на окне — запустите `tuber viral-refresh`."
            )
        return 1
    return 0


def cmd_viral_refresh(args: argparse.Namespace) -> int:
    """Пересчитать композитный индекс виральности (TL виральности, ч.1).

    Пишет ``viral_index`` и разбивку по осям в последнюю строку ``video_scores``.
    Рабочая БД защищена: без явного ``--allow-production`` команда отказывается
    работать (образец — scripts/backfill_authors.py), все проверки делаются на
    копии.
    """
    working = os.path.realpath(config.TUBER_DIR / "data" / "tuber.db")
    if os.path.realpath(DB_PATH) == working and not args.allow_production:
        print(
            f"отказ: {DB_PATH} — рабочая БД. Все проверки делаются на копии; "
            "для записи в боевую добавь --allow-production"
        )
        return 2
    conn = _connect()
    try:
        summary = viral.refresh(conn, cfg=config)
    finally:
        conn.close()
    _log_summary("viral-refresh", summary)
    histogram = ", ".join(
        f"{n} осей: {c}" for n, c in summary["axes_histogram"].items()
    )
    text = (
        f"Индекс виральности (полураспад "
        f"{summary['half_life_days']} дней): видео {summary['total']}, "
        f"индекс посчитан у {summary['indexed']}, NULL у {summary['null_index']} "
        f"(меньше двух осей); лайки не собраны у {summary['likes_null']}, "
        f"реакций нет (лайки = 0) у {summary['likes_zero']}; "
        f"ось лайков/комментариев обрезана потолком ×{summary['axis_cap']:g} "
        f"у {summary['axis_capped']}; "
        f"разбивка по числу осей: {histogram or 'нет'}; "
        f"обновлено строк: {summary['rows_written']}."
    )
    _emit({"command": "viral-refresh", **summary}, text, args.json)
    return 0


def cmd_migrate_shorts(args: argparse.Namespace) -> int:
    """Пересчитать признак шортса у всего пула по длительности."""
    conn = _connect()
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE is_shorts = 1"
        ).fetchone()["n"]
        changed = db.recompute_shorts(conn, config)
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE is_shorts = 1"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
    finally:
        conn.close()
    threshold = config.shorts_max_seconds()
    summary = {
        "threshold_seconds": threshold,
        "total": total,
        "is_shorts_before": before,
        "is_shorts_after": after,
        "changed": changed,
    }
    _log_summary("migrate-shorts", summary)
    text = (
        f"Пересчёт признака шортса (порог {threshold} с): изменено {changed} строк; "
        f"шортсов было {before}, стало {after} из {total} видео."
    )
    _emit({"command": "migrate-shorts", **summary}, text, args.json)
    return 0


def cmd_migrate_speed(args: argparse.Namespace) -> int:
    """Миграция этапа 10: качество интервалов и обнуление коротких скоростей.

    Открывает базу без init_db, чтобы показать состояние ДО миграции, затем
    прогоняет её (идемпотентно) и показывает состояние ПОСЛЕ.
    """
    threshold = config.min_interval_for_speed_seconds(config)
    conn = db.connect(DB_PATH)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
        if not columns:
            total = speed_before = short_before = 0
            changed = 0
            ok_after = short_after = first_after = speed_after = 0
        else:
            total = int(
                conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]
            )
            speed_before = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM snapshots "
                    "WHERE views_per_day IS NOT NULL"
                ).fetchone()["n"]
            )
            if "interval_quality" in columns:
                short_before = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM snapshots "
                        "WHERE interval_quality='short'"
                    ).fetchone()["n"]
                )
            else:
                short_before = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM snapshots "
                        "WHERE interval_seconds IS NOT NULL AND interval_seconds < ?",
                        (threshold,),
                    ).fetchone()["n"]
                )
            changed = db.migrate_interval_quality(conn, config)
            short_after = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM snapshots "
                    "WHERE interval_quality='short'"
                ).fetchone()["n"]
            )
            ok_after = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM snapshots WHERE interval_quality='ok'"
                ).fetchone()["n"]
            )
            first_after = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM snapshots WHERE interval_quality='first'"
                ).fetchone()["n"]
            )
            speed_after = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM snapshots "
                    "WHERE views_per_day IS NOT NULL"
                ).fetchone()["n"]
            )
    finally:
        conn.close()

    summary = {
        "threshold_seconds": threshold,
        "total": total,
        "changed": changed,
        "short_before": short_before,
        "short_after": short_after,
        "ok_after": ok_after,
        "first_after": first_after,
        "speeds_before": speed_before,
        "speeds_after": speed_after,
        "speeds_zeroed": max(0, speed_before - speed_after),
    }
    _log_summary("migrate-speed", summary)
    text = (
        f"Миграция интервалов (порог {threshold} с): изменено строк {changed}; "
        f"коротких замеров было {short_before}, стало {short_after}; "
        f"обнулено скоростей {summary['speeds_zeroed']}; "
        f"пригодны для скорости: {ok_after}, без пары: {first_after}, "
        f"всего замеров: {total}."
    )
    _emit({"command": "migrate-speed", **summary}, text, args.json)
    return 0


def cmd_expand_migrate(args: argparse.Namespace) -> int:
    """Миграция этапа 11: чистка запросов, чарта и потерянных handle."""
    conn = _connect()
    try:
        summary = expand.migrate_expand_filters(conn, config)
    finally:
        conn.close()
    _log_summary("expand-migrate", summary)
    text = (
        f"Миграция расширения: запросов всего {summary['queries_total']}, "
        f"в работу {summary['queries_accepted']}, "
        f"мусора отклонено {summary['queries_rejected']}; "
        f"handle возвращено в unresolved {summary['handles_requeued']}; "
        f"чарт-кандидатов {summary['charts_total']}, "
        f"отсеяно без ИИ-признака {summary['charts_rejected']}, "
        f"оставлено {summary['charts_kept']}."
    )
    _emit({"command": "expand-migrate", **summary}, text, args.json)
    return 0


def cmd_expand(args: argparse.Namespace) -> int:
    """Расширение поиска: бесплатные механизмы, затем платные в бюджете."""
    conn = _connect()
    try:
        dry_run = bool(getattr(args, "dry_run", False))
        client = None if dry_run else collect.make_client(conn)
        summary = expand.run_expand(
            conn,
            config,
            client=client,
            budget=args.budget,
            max_probes=args.max_probes,
            max_new_channels=getattr(args, "max_new_channels", None),
            dry_run=dry_run,
        )
    finally:
        conn.close()
    _log_summary("expand", summary)
    _emit({"command": "expand", **summary}, expand.format_summary(summary), args.json)
    return 0


def cmd_candidates_export(args: argparse.Namespace) -> int:
    """Экспорт внешних кандидатов (Telegram/X) из описаний видео.

    Только чтение рабочей базы (URI mode=ro): командой можно смотреть боевую
    базу, не трогая её. stdout — ровно один JSON-объект. Если база не читается
    (нет файла, схема чужая, слой совместимости не встал), печатаем причину в
    stderr и возвращаем ненулевой код: обёртка крона не должна показывать «ok».
    """
    out = getattr(args, "out", None) or candidates.DEFAULT_OUT
    conn = None
    try:
        conn = candidates.open_readonly(DB_PATH)
        summary = candidates.export_external(
            conn,
            out=out,
            min_mentions=getattr(args, "min_mentions", 1),
            limit=getattr(args, "limit", 3000),
            dry=bool(getattr(args, "dry", False)),
        )
    except sqlite3.Error as exc:
        # Чтение невозможно (no such table, locked, повреждённый файл) — это
        # ошибка, а не пустой фид: код возврата обязан быть ненулевым.
        print(f"ошибка: {exc}", file=sys.stderr)
        log.exception("candidates-export: чтение базы не удалось")
        return 1
    finally:
        if conn is not None:
            conn.close()
    payload = {"command": "candidates-export", **summary}
    _log_summary("candidates-export", {k: v for k, v in summary.items()
                                       if k != "top"})
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0


def cmd_candidates_import(args: argparse.Namespace) -> int:
    """Импорт YouTube-каналов из фида (kind='youtube').

    Единственная команда части C, которая пишет в базу — и только в таблицу
    channel_candidates. stdout — ровно один JSON-объект; битый/отсутствующий
    фид даёт понятную ошибку JSON и ненулевой код без трейсбека.
    """
    limit = getattr(args, "limit", 50)
    dry = bool(getattr(args, "dry", False))
    if dry:
        try:
            summary = candidates.import_youtube_feed(
                None, args.feed, None, limit=limit, dry=True
            )
        except candidates.CandidatesError as exc:
            print(json.dumps({"command": "candidates-import", "feed": str(args.feed),
                              "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"command": "candidates-import", **summary},
                         ensure_ascii=False, default=str))
        return 0

    # Проверяем фид до открытия базы: битый файл не должен ничего писать.
    try:
        candidates.read_youtube_feed(args.feed)
    except candidates.CandidatesError as exc:
        print(json.dumps({"command": "candidates-import", "feed": str(args.feed),
                          "error": str(exc)}, ensure_ascii=False))
        return 1

    conn = _connect()
    try:
        client = collect.make_client(conn)
        summary = candidates.import_youtube_feed(
            conn, args.feed, client, limit=limit, dry=False
        )
    finally:
        conn.close()
    _log_summary("candidates-import", summary)
    print(json.dumps({"command": "candidates-import", **summary},
                     ensure_ascii=False, default=str))
    return 0


def cmd_seo(args: argparse.Namespace) -> int:
    """SEO: разбор полей, сравнение выбросов, оценка упаковки.

    Только чтение YouTube-метаданных из базы и запись в seo_fields: сеть
    не используется. Метрики, которых нет в данных (CTR, удержание),
    не выводятся ни в каком виде.
    """
    fmt = getattr(args, "format", None)
    if fmt == "all":
        fmt = None
    conn = db.connect(DB_PATH)
    try:
        db.init_db(conn)
        if getattr(args, "thumbs", False):
            if thumbs.get_api_key() is None:
                print(
                    "обложки: нет ключа KIMI_API_KEY — разбор обложек невозможен",
                    file=sys.stderr,
                )
                return 1
            res = thumbs.run(
                conn,
                limit=getattr(args, "limit", None),
                min_mult=args.min_mult,
                budget_usd=getattr(args, "budget", None),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
            text = thumbs.format_run(res)
            payload: dict[str, Any] = {"command": "seo", "mode": "thumbs", **res}
        elif getattr(args, "score_refresh", False):
            summary = seo.refresh_scores(
                conn,
                limit=getattr(args, "limit", None),
                force=bool(getattr(args, "force", False)),
                fmt=fmt,
            )
            text = seo.format_score_refresh(summary)
            payload = {"command": "seo", "mode": "score-refresh", **summary}
        elif getattr(args, "score", None):
            result = seo.score_video(conn, args.score, fmt=fmt)
            text = seo.format_score(result)
            payload: dict[str, Any] = {"command": "seo", "mode": "score", **result}
        elif getattr(args, "brief", False):
            result = seo.brief(
                conn,
                topic=getattr(args, "topic", None),
                keyword=getattr(args, "keyword", None),
                channel_id=getattr(args, "channel", None),
                fmt=fmt,
                lang=getattr(args, "lang", None),
                min_outlier=args.min_outlier,
            )
            text = seo.format_brief(result)
            payload = {"command": "seo", "mode": "brief", **result}
        elif getattr(args, "audit", None) or getattr(args, "audit_top", None):
            min_out = args.min_outlier
            audit_top = getattr(args, "audit_top", None)
            if audit_top:
                channel_ids = seo.top_channels_by_outliers(
                    conn, audit_top, min_outlier=min_out
                )
            else:
                channel_ids = [
                    part.strip()
                    for part in str(args.audit).split(",")
                    if part.strip()
                ]
            if len(channel_ids) == 1 and not audit_top:
                # Один канал — прежний формат вывода и один файл отчёта.
                result = seo.audit_channel(
                    conn, channel_ids[0], min_outlier=min_out
                )
                seo.save_audit_report(result)
                text = seo.format_audit(result)
                payload = {"command": "seo", "mode": "audit", **result}
            else:
                batch = seo.audit_channels(
                    conn, channel_ids, min_outlier=min_out
                )
                text = seo.format_audit_batch(batch)
                payload = {"command": "seo", "mode": "audit-batch", **batch}
        elif getattr(args, "patterns", False):
            res = seo.patterns(
                conn,
                min_outlier=args.min_outlier,
                fmt=fmt,
                lang=getattr(args, "lang", None),
                days=getattr(args, "days", None),
                channel=getattr(args, "channel", None),
            )
            text = seo.format_patterns(res)
            payload = {"command": "seo", "mode": "patterns", **res}
        else:
            summary = seo.analyze(
                conn, force=bool(getattr(args, "force", False)),
                limit=getattr(args, "limit", None),
            )
            text = seo.format_analyze(summary)
            payload = {"command": "seo", "mode": "analyze", **summary}
    finally:
        conn.close()
    _log_summary("seo", {k: v for k, v in payload.items() if k != "sample"})
    _emit(payload, text, args.json)
    return 0


def save_run_report(text: str, when: datetime.datetime | None = None) -> str:
    """Сохранить текст суточного отчёта в data/reports/report-YYYY-MM-DD_HHMM.txt.

    Имя содержит дату И время по МСК: сутки базы считаются по МСК, поэтому имя
    файла совпадает с её сутками. Два прогона в разные минуты дают два разных
    файла. Повтор в ту же минуту не затирает предыдущий выпуск молча — к имени
    добавляется суффикс ``-2``, ``-3``, … Каталог берётся от расположения
    боевой базы (DB_PATH), а не хардкодится. Кодировка UTF-8. Возвращает путь
    сохранённого файла.
    """
    moment = when if when is not None else config.now_msk()
    target_dir = Path(DB_PATH).parent / config.REPORT_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = moment.strftime("%Y-%m-%d_%H%M")
    path = target_dir / f"report-{stamp}.txt"
    if path.exists():
        n = 2
        while True:
            candidate = target_dir / f"report-{stamp}-{n}.txt"
            if not candidate.exists():
                path = candidate
                break
            n += 1
    path.write_text(text, encoding="utf-8")
    return str(path)


def _archive_path(archive_dir: Path, name: str) -> Path:
    """Свободный путь в архиве: при совпадении имени добавляет -2, -3, …"""
    path = archive_dir / name
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = archive_dir / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _move_to_archive(path: Path, archive_dir: Path) -> Path:
    """Перенести файл в архив, не затирая одноимённый. Возвращает новый путь."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = _archive_path(archive_dir, path.name)
    os.replace(path, target)
    return target


# D-06: маска суточного отчёта — дата обязательна (report-ГГГГ-ММ-ДД*.txt).
# Под неё НЕ попадают ручные отчёты вида report-all.txt, report-long.txt,
# report-short.txt, лежащие прямо в data/ (ТЗ-33): их ротация не трогает.
_REPORT_DAY_GLOB = "report-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*.txt"


def rotate_reports(target_dir: str | Path, keep_days: int,
                   now: float | None = None) -> int:
    """Унести старые отчёты в ``archive/``, ничего не удаляя (D-06).

    Файлы суточного отчёта (``report-ГГГГ-ММ-ДД*.txt``, дата обязательна) в
    ``target_dir`` старше ``keep_days`` целиком переносятся в
    ``target_dir/archive/``. Заодно туда уходят остатки старой схемы — такие же
    датированные ``report-ГГГГ-ММ-ДД*.txt``, лежащие прямо в каталоге ``data/``
    рядом с базой; после переноса их там нет, поэтому повторная ротация
    безопасна. Ручные отчёты без даты (``report-all.txt`` и подобные) остаются
    на месте. Возвращает число перенесённых файлов.
    """
    reports_dir = Path(target_dir)
    archive_dir = reports_dir / "archive"
    now_value = time.time() if now is None else float(now)
    cutoff = now_value - int(keep_days) * 86400
    moved = 0
    if reports_dir.is_dir():
        for path in sorted(reports_dir.glob(_REPORT_DAY_GLOB)):
            if not path.is_file():
                continue
            try:
                is_old = path.stat().st_mtime < cutoff
            except OSError:
                continue
            if is_old:
                _move_to_archive(path, archive_dir)
                moved += 1
    # Остатки старой схемы рядом с базой переносятся без проверки возраста:
    # это уже не действующие выпуски (новая схема пишет в отдельный каталог).
    legacy_dir = reports_dir.parent
    if legacy_dir != reports_dir and legacy_dir.is_dir():
        for path in sorted(legacy_dir.glob(_REPORT_DAY_GLOB)):
            if path.is_file():
                _move_to_archive(path, archive_dir)
                moved += 1
    if moved:
        log.info("отчёты: перенесено в архив %d файлов → %s", moved, archive_dir)
    return moved


def build_daily_summary(
    days: int,
    *,
    collect: dict[str, Any],
    classify: dict[str, Any],
    expand: dict[str, Any],
    snapshots: dict[str, Any],
    seo: dict[str, Any],
    seo_scores: dict[str, Any],
    viral: dict[str, Any] | None = None,
    thumbs: dict[str, Any],
    comments: dict[str, Any],
    report_file: str | None,
    report_rotated: int,
    report_file_error: str | None = None,
) -> dict[str, Any]:
    """Единственная точка сборки сводки суточного цикла.

    Один и тот же словарь уходит и в лог (_log_summary), и в stdout: раньше
    сводка собиралась отдельно от того, что печаталось, и путь к файлу отчёта,
    блок комментариев и счётчик ротации могли не дойти до stdout. Ключи
    report_file и report_file_error присутствуют всегда, заполнен ровно один
    из них; report_rotated и comments — как есть.
    """
    return {
        "command": "daily",
        "days": days,
        "collect": collect,
        "classify": classify,
        "expand": expand,
        "snapshots": snapshots,
        "seo": seo,
        "seo_scores": seo_scores,
        "viral": viral if viral is not None else {},
        "thumbs": thumbs,
        "comments": comments,
        "report_rotated": int(report_rotated),
        "report_file": None if report_file_error is not None else report_file,
        "report_file_error": report_file_error,
    }


def cmd_daily(args: argparse.Namespace) -> int:
    """Полный суточный цикл: сбор → разбор → замеры → отчёт."""
    conn = _connect()
    try:
        collect_summary = collect.run_collect(
            conn, config, classify=False, time_budget_seconds=args.budget
        )
        classify_summary = classify.classify_videos(conn, config)
        expand_summary = expand.run_expand(conn, config)
        snap_summary = schedule.run_snapshots(conn, config)
        # SEO — вторичный блок: его падение не должно ронять суточный цикл.
        try:
            seo_summary: dict[str, Any] = seo.analyze(conn, force=False)
        except Exception as exc:  # noqa: BLE001
            seo_summary = {"error": str(exc)}
        # Копим историю скоров упаковки: тоже вторичный шаг, сбой не валит цикл.
        try:
            scores_summary: dict[str, Any] = seo.refresh_scores(conn, force=False)
        except Exception as exc:  # noqa: BLE001
            scores_summary = {"error": str(exc)}
            log.warning("SEO-скоры: сбой (%s), цикл продолжен", exc)
        # Композитный индекс виральности — поверх тех же скоров, чтобы отчёт
        # ранжировал по нему. Тоже вторичный шаг: сбой не валит цикл.
        try:
            viral_summary: dict[str, Any] = viral.refresh(conn, cfg=config)
        except Exception as exc:  # noqa: BLE001
            viral_summary = {"error": str(exc)}
            log.warning("виральность: сбой (%s), цикл продолжен", exc)
        # Разбор обложек — тоже вторичный шаг: сбой не валит цикл.
        try:
            if thumbs.get_api_key() is None:
                thumbs_summary: dict[str, Any] = {"skipped": True}
                log.info("thumbs: пропуск (нет KIMI_API_KEY)")
            else:
                thumbs_summary = thumbs.run(conn, limit=config.THUMB_MAX_PER_RUN)
                log.info(
                    "thumbs: разобрано %s, ошибок %s, $%s",
                    thumbs_summary["done"],
                    thumbs_summary["errors"],
                    thumbs_summary["cost_usd"],
                )
        except Exception as exc:  # noqa: BLE001
            thumbs_summary = {"error": str(exc)}
            log.warning("thumbs: сбой разбора обложек (%s)", exc)
        # Сбор комментариев — тоже вторичный шаг: сбой не валит цикл.
        try:
            comments_summary: dict[str, Any] = comments.run(conn, config)
            log.info(
                "comments: разобрано видео %s, записано %s, errors %s, units %s",
                comments_summary["videos"],
                comments_summary["comments"],
                comments_summary["errors"],
                comments_summary["units"],
            )
        except Exception as exc:  # noqa: BLE001
            comments_summary = {"error": str(exc)}
            log.warning("comments: сбой сбора комментариев (%s), цикл продолжен", exc)
        text = report.build_report(conn, config, days=args.days)
    finally:
        conn.close()

    # Файл отчёта — вторичный артефакт: сбой записи не должен ронять цикл.
    try:
        report_file: str | None = save_run_report(text)
        report_file_error: str | None = None
    except Exception as exc:  # noqa: BLE001
        report_file = None
        report_file_error = str(exc)
        log.warning("отчёт: сбой сохранения файла (%s), цикл продолжен", exc)

    # Ротация отчётов — тоже вторичный шаг: сбой не роняет цикл.
    report_rotated = 0
    try:
        report_rotated = rotate_reports(
            Path(DB_PATH).parent / config.REPORT_SUBDIR,
            config.REPORT_KEEP_DAYS,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("отчёты: сбой ротации (%s), цикл продолжен", exc)

    summary = build_daily_summary(
        args.days,
        collect=collect_summary,
        classify=classify_summary,
        expand=expand_summary,
        snapshots=snap_summary,
        seo=seo_summary,
        seo_scores=scores_summary,
        viral=viral_summary,
        thumbs=thumbs_summary,
        comments=comments_summary,
        report_file=report_file,
        report_rotated=report_rotated,
        report_file_error=report_file_error,
    )
    payload = {**summary, "report_text": text}
    _log_summary("daily", summary)

    if "error" in seo_summary:
        seo_phrase = f"SEO-разбор: ошибка ({seo_summary['error']})"
    else:
        seo_phrase = f"SEO-разбор: заполнено полей {seo_summary['written']}"

    if "error" in scores_summary:
        scores_phrase = (
            f"SEO-скоры: сбой ({scores_summary['error']}), цикл продолжен"
        )
    else:
        scores_phrase = (
            f"SEO-скоры: записано {scores_summary['scored']}, "
            f"пропущено (уже есть) {scores_summary['skipped']}, "
            f"всего кандидатов {scores_summary['total']}"
        )

    if "error" in viral_summary:
        viral_phrase = (
            f"виральность: сбой ({viral_summary['error']}), цикл продолжен"
        )
    else:
        viral_phrase = (
            f"виральность: индекс посчитан у {viral_summary['indexed']}, "
            f"NULL у {viral_summary['null_index']}"
        )

    if thumbs_summary.get("skipped"):
        thumbs_phrase = "обложки: пропуск (нет KIMI_API_KEY)"
    elif "error" in thumbs_summary:
        thumbs_phrase = f"обложки: ошибка ({thumbs_summary['error']})"
    else:
        thumbs_phrase = (
            f"обложки: разобрано {thumbs_summary['done']}, "
            f"ошибок {thumbs_summary['errors']}"
        )

    if "error" in comments_summary:
        comments_phrase = f"комментарии: ошибка ({comments_summary['error']})"
    else:
        comments_phrase = (
            f"комментарии: разобрано видео {comments_summary['videos']}, "
            f"записано {comments_summary['comments']}"
        )

    header = (
        f"Суточный цикл: собрано новых {collect_summary['new']}, "
        f"разобрано {classify_summary['classified']} (ИИ {classify_summary['ai']}), "
        f"расширение: принято каналов {expand_summary['accepted']} "
        f"(фраз в пуле {expand_summary.get('phrases_pool', 0)}, "
        f"использовано {expand_summary.get('phrases_used', 0)}, "
        f"отсеяно по урожаю {expand_summary.get('phrases_dropped', 0)}), "
        f"снято замеров {snap_summary['captured']}, "
        f"{seo_phrase}; {scores_phrase}; {viral_phrase}; {thumbs_phrase}; "
        f"{comments_phrase}."
    )
    if args.json:
        _emit(payload, header, True)
    else:
        print(header)
        print()
        print(text)
        if summary["report_file"] is not None:
            print(f"Отчёт: {summary['report_file']}")
    return 0


# --- разбор аргументов -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Собрать парсер команд."""
    parser = argparse.ArgumentParser(
        prog="tuber", description="Tuber_OS — консольный вход"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="сбор и разбор собранного")
    p_collect.add_argument("--queries", type=int, default=None,
                           help="сколько первых поисковых запросов взять (без ротации)")
    p_collect.add_argument("--all-queries", action="store_true",
                           help="весь пул запросов сразу, ротацию не двигать")
    p_collect.add_argument("--reset-rotation", action="store_true",
                           help="сбросить курсор ротации в 0 перед прогоном")
    p_collect.add_argument("--budget", type=int, default=None,
                           help="лимит времени прогона, секунды")
    p_collect.add_argument("--no-classify", action="store_true",
                           help="не разбирать собранное")
    p_collect.add_argument("--json", action="store_true", help="машинный вывод")
    p_collect.set_defaults(handler=cmd_collect)

    p_snap = sub.add_parser("snapshots", help="снять замеры по плану")
    p_snap.add_argument("--json", action="store_true", help="машинный вывод")
    p_snap.add_argument(
        "--fill-gap", dest="fill_gap", type=int, default=0,
        help="добор метрик для архивных видео с менее чем 2 осями "
             "виральности (D-47): максимум видео за прогон; 0 — выключено",
    )
    p_snap.set_defaults(handler=cmd_snapshots)

    p_cls = sub.add_parser("classify", help="разобрать неразобранные видео")
    p_cls.add_argument("--limit", type=int, default=None, help="максимум видео")
    p_cls.add_argument("--reclassify", action="store_true",
                       help="переразобрать уже размеченные видео")
    p_cls.add_argument("--dry-run", action="store_true",
                       help="с --reclassify: только показать объём, без модели")
    p_cls.add_argument("--all-ai", action="store_true",
                       help="с --reclassify: включить и видео с is_ai = 0")
    p_cls.add_argument("--json", action="store_true", help="машинный вывод")
    p_cls.set_defaults(handler=cmd_classify)

    p_cmt = sub.add_parser(
        "comments", help="собрать верхние комментарии отобранных видео"
    )
    p_cmt.add_argument(
        "--limit", type=int, default=config.COMMENT_MAX_VIDEOS_PER_RUN,
        help="максимум видео за прогон",
    )
    p_cmt.add_argument("--json", action="store_true", help="машинный вывод")
    p_cmt.set_defaults(handler=cmd_comments)

    p_rep = sub.add_parser("report", help="отчёт по базе")
    p_rep.add_argument("--days", type=int, default=10, help="окно роста, дней")
    p_rep.add_argument(
        "--format", choices=("short", "long", "all"), default="all",
        help="какой поток показать: шортсы, полные или оба",
    )
    p_rep.add_argument(
        "--topic", default=None,
        help="фильтр топа виральности по теме (video_classification.topic), "
             "например «агенты и автоматизация»",
    )
    p_rep.add_argument("--json", action="store_true", help="машинный вывод")
    p_rep.set_defaults(handler=cmd_report)

    p_viral = sub.add_parser(
        "viral-refresh",
        help="пересчитать композитный индекс виральности в video_scores",
    )
    p_viral.add_argument("--json", action="store_true", help="машинный вывод")
    p_viral.add_argument(
        "--allow-production", action="store_true",
        help="разрешить запись в рабочую БД (по умолчанию запрещена)",
    )
    p_viral.set_defaults(handler=cmd_viral_refresh)

    p_mig = sub.add_parser(
        "migrate-shorts", help="пересчитать признак шортса у всего пула"
    )
    p_mig.add_argument("--json", action="store_true", help="машинный вывод")
    p_mig.set_defaults(handler=cmd_migrate_shorts)

    p_mig_speed = sub.add_parser(
        "migrate-speed",
        help="этап 10: качество интервалов и обнуление коротких скоростей",
    )
    p_mig_speed.add_argument("--json", action="store_true", help="машинный вывод")
    p_mig_speed.set_defaults(handler=cmd_migrate_speed)

    p_expm = sub.add_parser(
        "expand-migrate",
        help="миграция этапа 11: фильтр запросов, чарта и handle",
    )
    p_expm.add_argument("--json", action="store_true", help="машинный вывод")
    p_expm.set_defaults(handler=cmd_expand_migrate)

    p_daily = sub.add_parser("daily", help="полный суточный цикл")
    p_daily.add_argument("--days", type=int, default=10, help="окно роста, дней")
    p_daily.add_argument("--budget", type=int, default=None,
                         help="лимит времени сбора, секунды")
    p_daily.add_argument("--json", action="store_true", help="машинный вывод")
    p_daily.set_defaults(handler=cmd_daily)

    p_exp = sub.add_parser(
        "expand", help="расширение поиска видео и каналов"
    )
    p_exp.add_argument("--budget", type=int, default=None,
                       help="лимит квоты прогона, units")
    p_exp.add_argument("--max-probes", type=int, default=None,
                       help="сколько кандидатов проверять за прогон")
    p_exp.add_argument("--max-new-channels", type=int, default=None,
                       help="сколько каналов принять за прогон")
    p_exp.add_argument("--dry-run", action="store_true",
                       help="только бесплатные шаги, без обращений к API")
    p_exp.add_argument("--json", action="store_true", help="машинный вывод")
    p_exp.set_defaults(handler=cmd_expand)

    p_seo = sub.add_parser("seo", help="SEO-разбор упаковки видео")
    seo_mode = p_seo.add_mutually_exclusive_group()
    seo_mode.add_argument("--analyze", action="store_true",
                          help="разобрать поля и записать в seo_fields")
    seo_mode.add_argument("--thumbs", action="store_true",
                          help="разобрать обложки топ-видео через vision")
    seo_mode.add_argument("--patterns", action="store_true",
                          help="сравнить оформление выбросов с фоном")
    seo_mode.add_argument("--score", metavar="VIDEO_ID", default=None,
                          help="оценить упаковку одного видео")
    seo_mode.add_argument("--score-refresh", action="store_true",
                          help="посчитать скор упаковки и записать в video_scores")
    seo_mode.add_argument("--brief", action="store_true",
                          help="SEO-бриф на новое видео по залетевшим")
    seo_mode.add_argument("--audit", metavar="CHANNEL_ID[,CHANNEL_ID...]",
                          default=None,
                          help="SEO-аудит канала (несколько через запятую)")
    seo_mode.add_argument("--audit-top", metavar="N", type=int, default=None,
                          help="SEO-аудит N каналов с наибольшим числом выбросов")
    p_seo.add_argument("--topic", default=None,
                       help="с --brief: смысловая тема (например, "
                            "«агенты и автоматизация»)")
    p_seo.add_argument("--keyword", default=None,
                       help="с --brief: основной ключ для опечаток")
    p_seo.add_argument("--force", action="store_true",
                       help="с --analyze/--score-refresh: пересчитать заново")
    p_seo.add_argument("--limit", type=int, default=None,
                       help="с --analyze/--thumbs/--score-refresh: максимум видео")
    p_seo.add_argument("--min-mult", type=float, default=config.THUMB_MIN_MULT,
                       help="с --thumbs: порог выброса (по умолчанию 3.0)")
    p_seo.add_argument("--budget", type=float, default=None,
                       help="с --thumbs: денежный лимит прогона, $")
    p_seo.add_argument("--dry-run", action="store_true",
                       help="с --thumbs: только отбор, без сети и записи")
    p_seo.add_argument("--min-outlier", type=float, default=3.0,
                       help="порог выброса для --patterns/--audit/--audit-top "
                            "(по умолчанию 3.0)")
    p_seo.add_argument("--channel", default=None,
                       help="срез по каналу (CHANNEL_ID): для --patterns и --brief")
    p_seo.add_argument("--format", choices=("short", "long", "all"),
                       default="all", help="формат выборки")
    p_seo.add_argument("--lang", choices=("ru", "world"), default=None,
                       help="языковой срез")
    p_seo.add_argument("--days", type=int, default=None,
                       help="окно публикации, дней")
    p_seo.add_argument("--json", action="store_true", help="машинный вывод")
    p_seo.set_defaults(handler=cmd_seo)

    p_cand_out = sub.add_parser(
        "candidates-export",
        help="экспорт внешних кандидатов (Telegram/X) из описаний",
    )
    p_cand_out.add_argument(
        "--out", default=None,
        help="файл JSONL (по умолчанию data/exchange/external_candidates.jsonl)",
    )
    p_cand_out.add_argument("--min-mentions", type=int, default=1,
                            help="минимальное число упоминаний")
    p_cand_out.add_argument("--limit", type=int, default=3000,
                            help="максимум записей в файле")
    p_cand_out.add_argument("--dry", action="store_true",
                            help="только сводка, файл не писать")
    p_cand_out.set_defaults(handler=cmd_candidates_export)

    p_cand_in = sub.add_parser(
        "candidates-import",
        help="импорт YouTube-каналов из фида (kind='youtube')",
    )
    p_cand_in.add_argument("--feed", required=True, help="файл фида (JSONL)")
    p_cand_in.add_argument("--limit", type=int, default=50,
                           help="максимум сетевых вызовов за прогон")
    p_cand_in.add_argument("--dry", action="store_true",
                           help="только разбор фида, без сети и записи")
    p_cand_in.set_defaults(handler=cmd_candidates_import)

    return parser


def _apply_db_override(argv: list[str]) -> list[str]:
    """Вынуть глобальный ``--db PATH`` из argv и переставить пути.

    Приоритет пути: ``--db`` → ``TUBER_DB`` (среда, читается в config) →
    ``data/tuber.db`` относительно корня репозитория. Каталог для замка и лога
    берётся рядом с базой, чтобы прогон на копии не трогал рабочий ``data/``.
    """
    global DB_PATH, DATA_DIR, LOCK_PATH, LOG_PATH
    out: list[str] = []
    i = 0
    value: str | None = None
    while i < len(argv):
        a = argv[i]
        if a == "--db":
            if i + 1 >= len(argv):
                raise SystemExit("--db требует путь")
            value = argv[i + 1]
            i += 2
            continue
        if a.startswith("--db="):
            value = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    if value:
        DB_PATH = Path(value)
        DATA_DIR = DB_PATH.parent
        LOCK_PATH = DATA_DIR / ".lock"
        LOG_PATH = DATA_DIR / "tuber.log"
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. Код возврата: 0 — успех, 1 — ошибка."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        argv = _apply_db_override(argv)
    except SystemExit as exc:
        print(exc.code or "неверные аргументы", file=sys.stderr)
        return 1
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1

    config.load_env()
    _setup_logging()

    lock = _Lock(LOCK_PATH)
    if not lock.acquire():
        # Замок занят: крон не должен сыпать ошибками.
        print("прогон уже идёт")
        log.info("прогон уже идёт, команда %s пропущена", args.command)
        return 0

    try:
        return int(args.handler(args))
    except BrokenPipeError:
        # Читатель закрыл трубу (например, `| head`) — это не ошибка.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    except Exception as exc:  # короткая причина по-русски в stderr
        print(f"ошибка: {exc}", file=sys.stderr)
        log.exception("команда %s упала", args.command)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
