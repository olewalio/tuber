"""Комментарии и обсуждения: сбор (YouTube/Telegram/X) и выдача (ТЗ-49).

Контур 6 плана: комментарии YouTube расширяются (топ виральных видео и топ
каналов), тексты ответов Telegram добираются через Telethon, а X отдаёт только
счётчик ``conversation_count`` как сигнал «горячий спор» (текст недостижим без
платного API — не имитируем). Наружу контур выдаёт ``comments top``: обсуждения
с числами (комментариев, разных авторов, доля вопросов) и список вопросов
аудитории как темы для контента.

CLI::

    python3 -m tuber comments youtube  [--limit N] [--dry-run] [--allow-production]
    python3 -m tuber comments telegram [--channels N] [--max-replies N]
                                       [--dry-run] [--allow-production]
    python3 -m tuber comments x        [--dry-run] [--allow-production]
    python3 -m tuber comments top      [--json] [--top N] [--questions N]

Запись в боевую базу гейтится ``--allow-production`` (ТЗ-45F): обёртка-планировщик
объявляет флаг сама, ручные прогоны и приёмка идут на копии. ``--dry-run`` не
трогает ни сеть, ни базу и разрешён даже на боевой.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from tuber import config as core_config
from tuber.core import db as core_db
from tuber.core import schema as core_schema
from tuber.core import storage

PLATFORMS = ("youtube", "telegram", "x")

#: Метка строки ``run`` для прогонов контура.
RUN_PLATFORM = "cross"


# ---------------------------------------------------------------------------
# Общие помощники (как в queue.py)
# ---------------------------------------------------------------------------

def _db_path(args) -> str:
    return args.db or os.environ.get("TUBER_DB") or core_config.db_path()


def _is_production(path: str) -> bool:
    try:
        return (os.path.realpath(str(path))
                == os.path.realpath(str(core_config.DEFAULT_DB_PATH)))
    except OSError:
        return False


def _connect(path: str):
    con = core_db.connect(path)
    core_schema.migrate_schema(con)
    return con


def _gate(args, path: str) -> bool:
    """True — писать нельзя (отказ)."""
    return (not getattr(args, "dry_run", False) and _is_production(path)
            and not args.allow_production)


def _start_run(con, mode: str) -> int:
    return storage.add_run(con, RUN_PLATFORM, mode=mode,
                           started_at=_iso_now())


def _finish_run(con, run_id: int, note: str, ok: int, fail: int) -> None:
    con.execute(
        "UPDATE run SET finished_at=?, ok_count=?, fail_count=?, note=? WHERE id=?",
        (_iso_now(), int(ok), int(fail), note, run_id))
    storage.log_run(con, run_id, _iso_now(), "INFO", None, note,
                    platform=RUN_PLATFORM)
    con.commit()


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _emit(payload: dict, text: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(text)


# ---------------------------------------------------------------------------
# Подкоманды сбора
# ---------------------------------------------------------------------------

def cmd_youtube(args) -> int:
    path = _db_path(args)
    if _gate(args, path):
        print("отказ: запись в боевую базу без --allow-production "
              "(приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        return 2
    from tuber.platforms.youtube import comments as yt_comments

    con = _connect(path)
    try:
        run_id = None if args.dry_run else _start_run(con, "comments-youtube")
        summary = yt_comments.run(con, limit=args.limit, dry_run=args.dry_run)
        if run_id is not None:
            h = summary.get("harvested") or {}
            _finish_run(con, run_id,
                        f"comments yt: videos={summary['videos']} "
                        f"comments={summary['comments']} units={summary['units']} "
                        f"harvested={h.get('added', 0)}",
                        ok=summary["videos"], fail=summary["errors"] + summary["disabled"])
        _emit({"command": "comments youtube", **summary},
              yt_comments.format_summary(summary), args.json)
        return 0
    finally:
        con.close()


def cmd_telegram(args) -> int:
    path = _db_path(args)
    if _gate(args, path):
        print("отказ: запись в боевую базу без --allow-production "
              "(приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        return 2
    from tuber.platforms.telegram import comments as tg_comments

    con = _connect(path)
    try:
        run_id = None if args.dry_run else _start_run(con, "comments-telegram")
        summary = tg_comments.collect(
            con, dry_run=args.dry_run, n_channels=args.channels,
            max_replies=args.max_replies, messages_per_channel=args.messages)
        if run_id is not None:
            _finish_run(con, run_id,
                        f"comments tg: channels={summary.get('channels_ok', 0)} "
                        f"replies={summary.get('replies', 0)} "
                        f"flood={summary.get('flood_wait', 0)}",
                        ok=summary.get("channels_ok", 0),
                        fail=summary.get("errors", 0))
        _emit({"command": "comments telegram", **summary},
              tg_comments.format_summary(summary), args.json)
        return 0
    finally:
        con.close()


def cmd_x(args) -> int:
    """X: снять сигнал «горячий спор» по conversation_count (без текста)."""
    path = _db_path(args)
    if _gate(args, path):
        print("отказ: запись в боевую базу без --allow-production "
              "(приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        return 2
    con = _connect(path)
    try:
        run_id = None if args.dry_run else _start_run(con, "comments-x")
        rows = hot_discussions_x(con, limit=args.top)
        summary = {"hot": len(rows), "signal_only": True, "dry_run": bool(args.dry_run),
                   "top": rows[:10]}
        if run_id is not None:
            _finish_run(con, run_id,
                        f"comments x: hot={len(rows)} (только conversation_count)",
                        ok=len(rows), fail=0)
        text = (f"X: горячих споров по счётчику ответов {len(rows)}. "
                f"Текст reply-цепочек недоступен без платного API — сигнал без текста.")
        _emit({"command": "comments x", **summary}, text, args.json)
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Выдача
# ---------------------------------------------------------------------------

def _discussions(con, *, days: int = 90, top: int = 5) -> list[dict]:
    """Обсуждения с числами: комментарии, разные авторы, доля вопросов."""
    rows = con.execute(
        """
        SELECT c.platform                                   AS platform,
               c.id                                         AS content_id,
               c.external_id                                AS external_id,
               c.title                                      AS title,
               c.url                                        AS url,
               s.handle                                     AS source_handle,
               s.title                                      AS source_title,
               COUNT(cc.comment_id)                         AS comments,
               COUNT(DISTINCT cc.author)                    AS authors,
               SUM(CASE WHEN cc.text LIKE '%?%' THEN 1 ELSE 0 END) AS questions,
               MAX(cc.captured_at)                          AS last_comment_at
        FROM content_comment cc
        JOIN content c ON c.id = cc.content_id
        LEFT JOIN source s ON s.id = c.source_id
        WHERE c.platform IN ('youtube', 'telegram')
          AND (c.published_at IS NULL
               OR c.published_at >= datetime('now', ?))
        GROUP BY c.id
        ORDER BY comments DESC, authors DESC
        LIMIT ?
        """,
        (f"-{int(days)} days", int(top)),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        comments = int(r["comments"] or 0)
        q = int(r["questions"] or 0)
        out.append({
            "platform": r["platform"],
            "title": r["title"] or r["external_id"],
            "source": r["source_title"] or r["source_handle"],
            "external_id": r["external_id"],
            "url": r["url"],
            "comments": comments,
            "authors": int(r["authors"] or 0),
            "questions": q,
            "question_share": round(q / comments, 3) if comments else 0.0,
            "last_comment_at": r["last_comment_at"],
        })
    return out


def _questions(con, *, days: int = 90, limit: int = 5) -> list[dict]:
    """Вопросы аудитории дословно (текст с «?»), лучшие по лайкам."""
    rows = con.execute(
        """
        SELECT cc.platform      AS platform,
               cc.author        AS author,
               cc.text          AS text,
               cc.likes         AS likes,
               cc.comment_id    AS comment_id,
               c.title          AS title,
               c.external_id    AS external_id,
               c.platform       AS cplatform
        FROM content_comment cc
        JOIN content c ON c.id = cc.content_id
        WHERE cc.text LIKE '%?%'
          AND cc.text IS NOT NULL
          AND LENGTH(TRIM(cc.text)) BETWEEN 8 AND 400
          AND (c.published_at IS NULL
               OR c.published_at >= datetime('now', ?))
        ORDER BY (cc.likes IS NULL), cc.likes DESC, cc.comment_id ASC
        LIMIT ?
        """,
        (f"-{int(days)} days", int(limit)),
    ).fetchall()
    return [{
        "platform": r["platform"],
        "author": r["author"],
        "text": r["text"],
        "likes": r["likes"],
        "title": r["title"] or r["external_id"],
    } for r in rows]


def hot_discussions_x(con, *, days: int = 30, limit: int = 5) -> list[dict]:
    """X: посты с наибольшим ``conversation_count`` — сигнал «горячий спор».

    Текст reply-цепочек недоступен без платного API (ТЗ-49 п.4): возвращаем
    только числа и ссылку, без выдуманных цитат.
    """
    rows = con.execute(
        """
        SELECT p.tweet_id           AS tweet_id,
               p.owner_handle       AS owner_handle,
               p.text               AS text,
               p.likes              AS likes,
               p.replies            AS replies,
               p.published_at_utc   AS published_at
        FROM v_x_posts p
        WHERE p.replies IS NOT NULL AND p.replies > 0
          AND (p.published_at_utc IS NULL
               OR p.published_at_utc >= datetime('now', ?))
        ORDER BY p.replies DESC, p.likes DESC
        LIMIT ?
        """,
        (f"-{int(days)} days", int(limit)),
    ).fetchall()
    return [{
        "tweet_id": r["tweet_id"],
        "handle": r["owner_handle"],
        "replies": int(r["replies"] or 0),
        "likes": r["likes"],
        "published_at": r["published_at"],
        "url": f"https://x.com/{r['owner_handle']}/status/{r['tweet_id']}"
               if r["owner_handle"] else None,
        "text_available": False,
    } for r in rows]


def _platform_totals(con) -> list[dict]:
    rows = con.execute(
        """
        SELECT platform                         AS platform,
               COUNT(*)                         AS comments,
               COUNT(DISTINCT content_id)       AS discussions,
               COUNT(DISTINCT author)           AS authors
        FROM content_comment
        GROUP BY platform
        ORDER BY comments DESC
        """
    ).fetchall()
    return [{"platform": r["platform"], "comments": int(r["comments"] or 0),
             "discussions": int(r["discussions"] or 0),
             "authors": int(r["authors"] or 0)} for r in rows]


def _telegram_stats(con) -> dict:
    row = con.execute(
        """
        SELECT COUNT(*)                       AS comments,
               COUNT(DISTINCT content_id)     AS discussions,
               COUNT(DISTINCT external_id)    AS posts,
               COUNT(DISTINCT substr(external_id, 1, instr(external_id, '/') - 1))
                                              AS channels
        FROM content_comment
        WHERE platform='telegram' AND external_id IS NOT NULL
        """
    ).fetchone()
    return {
        "comments": int(row["comments"] or 0),
        "discussions": int(row["discussions"] or 0),
        "posts": int(row["posts"] or 0),
        "channels": int(row["channels"] or 0),
    }


def report(con, *, top: int = 5, questions: int = 5, days: int = 90) -> dict:
    """Собрать выдачу ``comments top`` целиком (только чтение)."""
    from tuber.platforms.youtube import comments as yt_comments

    return {
        "command": "comments top",
        "days": days,
        "totals": _platform_totals(con),
        "youtube_units_today": yt_comments.units_today(con),
        "telegram": _telegram_stats(con),
        "discussions": _discussions(con, days=days, top=top),
        "questions": _questions(con, days=days, limit=questions),
        "x_hot": hot_discussions_x(con, limit=top),
    }


def format_report(data: dict) -> str:
    """Человеческая выдача: числа обсуждений и вопросы аудитории дословно."""
    lines: list[str] = []
    totals = {t["platform"]: t for t in data.get("totals", [])}
    total_comments = sum(t["comments"] for t in totals.values())
    lines.append(f"КОММЕНТАРИИ И ОБСУЖДЕНИЯ (окно {data.get('days', 90)} дней)")
    lines.append(f"Всего комментариев: {total_comments}")
    for t in data.get("totals", []):
        lines.append(
            f"  {t['platform']}: {t['comments']} комм., "
            f"{t['discussions']} обсуждений, {t['authors']} авторов")
    tg = data.get("telegram") or {}
    lines.append(
        f"Telegram: комментариев {tg.get('comments', 0)}, "
        f"каналов {tg.get('channels', 0)}, сообщений {tg.get('posts', 0)}")
    units = data.get("youtube_units_today")
    if units is not None:
        lines.append(f"Расход квоты YouTube (commentThreads) за сутки: {units} units")
    else:
        lines.append("Расход квоты YouTube: нет данных")

    lines.append("")
    lines.append(f"ОБСУЖДЕНИЯ (топ-{len(data.get('discussions', []))}):")
    for d in data.get("discussions", []):
        lines.append(
            f"  [{d['platform']}] {d['title']} — {d['source']}")
        lines.append(
            f"      комментариев {d['comments']}, разных авторов {d['authors']}, "
            f"доля вопросов {d['question_share']:.0%}"
            + (f", {d['url']}" if d.get("url") else ""))

    lines.append("")
    lines.append("ВОПРОСЫ АУДИТОРИИ (темы для контента, дословно):")
    for i, q in enumerate(data.get("questions", []), 1):
        likes = q.get("likes")
        likes_phrase = f", лайков {likes}" if likes is not None else ""
        text = " ".join((q.get("text") or "").split())
        lines.append(f"  {i}. «{text}»")
        lines.append(
            f"     [{q['platform']}] {q.get('author') or '—'}{likes_phrase} "
            f"к «{q.get('title')}»")

    lines.append("")
    lines.append("X — ГОРЯЧИЕ СПОРЫ (только счётчик ответов, текста нет):")
    xh = data.get("x_hot") or []
    if not xh:
        lines.append("  (нет данных)")
    for h in xh:
        lines.append(
            f"  @{h.get('handle')}: ответов {h['replies']}, лайков {h.get('likes')}"
            + (f", {h['url']}" if h.get("url") else ""))
    return "\n".join(lines)


def cmd_top(args) -> int:
    con = _connect(_db_path(args))
    try:
        data = report(con, top=args.top, questions=args.questions, days=args.days)
    finally:
        con.close()
    _emit(data, format_report(data), args.json)
    return 0


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tuber comments",
                                 description="Комментарии и обсуждения (ТЗ-49)")
    ap.add_argument("--db", default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")

    gate = argparse.ArgumentParser(add_help=False)
    gate.add_argument("--dry-run", action="store_true", help="без записи в БД")
    gate.add_argument("--allow-production", action="store_true",
                      help="разрешить запись в боевую базу")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="путь к базе (иначе TUBER_DB)")

    p = sub.add_parser("youtube", parents=[common, gate],
                       help="собрать комментарии YouTube (топ виральных + топ каналов)")
    p.add_argument("--limit", type=int, default=None, help="максимум видео за прогон")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.set_defaults(func=cmd_youtube)

    p = sub.add_parser("telegram", parents=[common, gate],
                       help="собрать тексты ответов Telegram через Telethon")
    p.add_argument("--channels", type=int, default=None, help="сколько каналов взять")
    p.add_argument("--max-replies", type=int, default=None,
                   help="потолок ответов за прогон")
    p.add_argument("--messages", type=int, default=None,
                   help="сколько сообщений канала просматривать")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.set_defaults(func=cmd_telegram)

    p = sub.add_parser("x", parents=[common, gate],
                       help="снять сигнал «горячий спор» по счётчику ответов X")
    p.add_argument("--top", type=int, default=10, help="сколько постов показать")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.set_defaults(func=cmd_x)

    p = sub.add_parser("top", parents=[common],
                       help="обсуждения с числами и вопросы аудитории")
    p.add_argument("--top", type=int, default=5, help="сколько примеров обсуждений")
    p.add_argument("--questions", type=int, default=5, help="сколько вопросов")
    p.add_argument("--days", type=int, default=90, help="окно обсуждений, дней")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.set_defaults(func=cmd_top)

    ap.set_defaults(func=None)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "comments":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
