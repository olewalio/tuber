"""Повышение кандидата Telegram в активный источник (последняя миля).

Зачем
-----
Сбор контента ИДЁТ с каналов в статусе ``candidate``, но «источником» в реестре
канал не становится: переход ``candidate → active`` у Telegram не выполнялся по
измеримому правилу. Кандидат собирается, но навсегда остаётся кандидатом.

Правило (ТЗ-51, пороги откалиброваны — см. ``config.PROMOTE_*``)
----------------------------------------------------------------
Канал становится ``active``, когда:
1. по нему уже собран материал: число строк ``content`` канала
   ≥ :data:`tuber.platforms.telegram.config.PROMOTE_MIN_POSTS`;
2. он не отбракован по антифроду: ``antifraud_flag`` ==
   :data:`~config.PROMOTE_MAX_ANTIFRAUD`;
3. виральность ``vr`` посчитана ПО ФАКТИЧЕСКИМ ПОСТАМ на момент промоушена
   (медиана просмотров / подписчики × 100) и не ниже
   :data:`~config.PROMOTE_MIN_VR` (если :data:`~config.PROMOTE_REQUIRE_VR`).

``vr`` больше НЕ берётся из колонки ``source.vr``: она заполнялась только при
импорте ``UNIVERSE.csv`` и была посчитана у 5 из 766 кандидатов (D-54). Если на
``vr`` не хватает данных (нет подписчиков или нет постов с просмотрами) — это
ЯВНОЕ состояние «vr неизвестен» с отдельной строкой в отчёте, а не тихий отказ.

Связь очередь ↔ реестр (ТЗ-51, D-55)
------------------------------------
Повышение реестра закрывает строку очереди ``candidate`` по КАНОНИЧЕСКОМУ
хендлу (``@X``/``t.me/x``/``X``/``x/s`` → ``x``) и/или ``tg_id``
(:mod:`tuber.platforms.telegram.linking`): ``status='promoted'``,
``promoted_at``, ``promoted_by``. Если строки очереди нет, она заводится
(``found_via='registry_promote'``), чтобы маркер повышения не терялся.

Гарантии
--------
* Функция трогает ТОЛЬКО строки ``source`` со статусом ``candidate``. Каналы
  ``private``/``dead``/``rejected`` не читаются и не изменяются, ни один статус
  не понижается.
* Идемпотентна: повторный прогон без новых данных не меняет ни одной строки и
  не плодит дубликатов в очереди.
* Проверка вырождения: если на входе ≥ ``PROMOTE_DEGENERATE_MIN_CHECKED``
  кандидатов и повышаются ВСЕ подряд, боевой прогон отменяется (``rc=3``).
* Поддерживает dry-run и печатает таблицу «проверено / повышено / отклонено и
  почему» с отдельной строкой «vr неизвестен».

CLI::

    python3 -m tuber tg promote [--db PATH] [--dry] [--min-posts N]
                                [--min-vr X] [--require-vr/--no-require-vr]
                                [--allow-production] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys

from tuber.core import timeutil

from . import config
from . import linking
from . import store as db

# TODO(debt-D-55): закрыт в ТЗ-51 — связь очереди и реестра идёт по
#   каноническому хендлу и `tg_id` через `linking`; повышение закрывает или
#   заводит строку очереди, `promoted_at` перестал быть вечно пустым.
#
# TODO(debt-D-68): ОСТАТОК — у 159 из 743 кандидатов Telegram (21.09.2026) нет
#   подписчиков, поэтому `vr` не вычислить и канал не проходит порог; нужен
#   источник `subs` (см. D-62: JS-превью t.me не отдаёт подписчиков) либо
#   честный альтернативный признак охвата. См. TECH-DEBT.md.

# Источник промоушена — для candidate.promoted_by и журнала прогона.
PROMOTE_SOURCE = "tg_promote"

#: Причины отказа кандидата (для сводки и журнала).
REASON_TOO_FEW_POSTS = "мало постов"
REASON_ANTIFRAUD = "антифрод"
REASON_VR_UNKNOWN = "vr неизвестен"
REASON_VR_LOW = "vr ниже порога"

#: Сколько постов с просмотрами нужно, чтобы честно посчитать медиану.
VR_MIN_VIEW_SAMPLES = 3

#: Статусы, которые функция НЕ трогает ни при каких условиях.
PROTECTED_STATUSES = ("private", "dead", "rejected")

#: Статус, в который закрывается строка очереди при повышении.
PROMOTED_STATUS = "promoted"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Повышение кандидата Telegram в активный по измерениям")
    ap.add_argument("--db", default=None, help="путь к базе (по умолчанию — из config)")
    ap.add_argument("--dry", action="store_true", help="сводка без записи в БД")
    ap.add_argument("--min-posts", type=int, default=None,
                    help="минимум собранных постов (по умолчанию PROMOTE_MIN_POSTS)")
    ap.add_argument("--min-vr", type=float, default=None,
                    help="минимум vr, %% (по умолчанию PROMOTE_MIN_VR)")
    ap.add_argument("--require-vr", dest="require_vr", action="store_true", default=None,
                    help="требовать посчитанный vr (по умолчанию из конфига)")
    ap.add_argument("--no-require-vr", dest="require_vr", action="store_false",
                    help="не требовать vr (для калибровки порога)")
    ap.add_argument("--allow-production", action="store_true",
                    help="разрешить запись в боевую единую базу")
    ap.add_argument("--json", action="store_true", help="машинная сводка")
    return ap.parse_args(argv)


def _thresholds(*, min_posts=None, min_vr=None, require_vr=None):
    return (
        config.PROMOTE_MIN_POSTS if min_posts is None else int(min_posts),
        config.PROMOTE_REQUIRE_VR if require_vr is None else bool(require_vr),
        int(config.PROMOTE_MAX_ANTIFRAUD),
        config.PROMOTE_MIN_VR if min_vr is None else float(min_vr),
    )


def _candidate_rows(con) -> list[sqlite3.Row]:
    """Кандидаты Telegram с числом реально собранных постов (content)."""
    return con.execute(
        """
        SELECT s.id                         AS id,
               s.handle                     AS handle,
               s.external_id                AS external_id,
               s.meta_json                  AS meta_json,
               s.subs                       AS subs,
               COALESCE(s.antifraud_flag, 0) AS antifraud_flag,
               (SELECT COUNT(*) FROM content c
                 WHERE c.source_id = s.id AND c.platform = 'telegram') AS posts
        FROM source s
        WHERE s.platform = 'telegram' AND s.status = 'candidate'
        ORDER BY s.handle
        """
    ).fetchall()


def _post_views(con, source_id) -> list[int]:
    """Просмотры постов канала: ``content.meta_json.views`` + запасные источники."""
    out: list[int] = []
    rows = con.execute(
        """
        SELECT c.meta_json AS meta_json,
               (SELECT MAX(m.views) FROM metric_snapshot m
                 WHERE m.content_id = c.id AND m.views IS NOT NULL) AS snap_views,
               (SELECT cl.views FROM content_latest cl
                 WHERE cl.content_id = c.id AND cl.views IS NOT NULL) AS latest_views
        FROM content c
        WHERE c.source_id = ? AND c.platform = 'telegram'
        """,
        (source_id,),
    ).fetchall()
    for row in rows:
        views = None
        if row["meta_json"]:
            try:
                meta = json.loads(row["meta_json"])
            except (TypeError, ValueError):
                meta = None
            if isinstance(meta, dict):
                views = meta.get("views")
        if views is None:
            views = row["snap_views"]
        if views is None:
            views = row["latest_views"]
        if views is None:
            continue
        try:
            value = int(views)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out.append(value)
    return out


def compute_vr(con, source_id, subs) -> dict:
    """Виральность по фактическим постам: медиана просмотров / подписчики × 100.

    Возвращает ``{"state", "vr", "views_median", "samples"}``, где ``state`` —
    ``"ok"`` / ``"no_subs"`` / ``"no_views"``. «Нет данных» — такое же
    наблюдаемое состояние, как и число: его печатает отчёт.
    """
    result = {"state": "ok", "vr": None, "views_median": None, "samples": 0}
    if subs is None or int(subs or 0) <= 0:
        result["state"] = "no_subs"
        return result
    views = _post_views(con, source_id)
    result["samples"] = len(views)
    if len(views) < VR_MIN_VIEW_SAMPLES:
        result["state"] = "no_views"
        return result
    median = statistics.median(views)
    result["views_median"] = float(median)
    result["vr"] = round(100.0 * median / float(subs), 1)
    return result


def _evaluate(row, *, min_posts, require_vr, max_antifraud, min_vr, vr):
    """Причина отказа либо None, если кандидат подлежит повышению."""
    if int(row["antifraud_flag"] or 0) != max_antifraud:
        return REASON_ANTIFRAUD
    if int(row["posts"] or 0) < min_posts:
        return REASON_TOO_FEW_POSTS
    if require_vr:
        if vr["state"] != "ok":
            return REASON_VR_UNKNOWN
        if vr["vr"] is None or vr["vr"] < min_vr:
            return REASON_VR_LOW
    return None


def promote_candidates(con, *, dry_run=False, min_posts=None, min_vr=None,
                       require_vr=None, run_id=None, now=None,
                       create_missing_queue_row=True) -> dict:
    """Повышает подходящих кандидатов Telegram. Возвращает сводку.

    ``con`` — соединение-адаптер :mod:`tuber.platforms.telegram.store`.
    """
    min_posts, require_vr, max_antifraud, min_vr = _thresholds(
        min_posts=min_posts, min_vr=min_vr, require_vr=require_vr)
    stamp = timeutil.iso_now() if now is None else (timeutil.parse_any(now) or timeutil.iso_now())
    rows = _candidate_rows(con)
    summary = {
        "checked": len(rows),
        "promoted": 0,
        "reasons": {},
        "promoted_handles": [],
        "dry_run": bool(dry_run),
        "min_posts": min_posts,
        "require_vr": require_vr,
        "min_vr": min_vr,
        "vr_known": 0,
        "vr_unknown": 0,
        "vr_unknown_handles": [],
        "queue_closed": 0,
        "queue_created": 0,
        "degenerate": False,
        "degenerate_threshold": int(config.PROMOTE_DEGENERATE_MIN_CHECKED),
    }

    # Первый проход — измерения и решение (без записи): вырождение определяется
    # до изменения базы.
    decisions = []
    for row in rows:
        vr = compute_vr(con, row["id"], row["subs"])
        if vr["state"] == "ok":
            summary["vr_known"] += 1
        else:
            summary["vr_unknown"] += 1
            if len(summary["vr_unknown_handles"]) < 50:
                summary["vr_unknown_handles"].append(row["handle"])
        reason = _evaluate(row, min_posts=min_posts, require_vr=require_vr,
                           max_antifraud=max_antifraud, min_vr=min_vr, vr=vr)
        if reason is not None:
            summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
            continue
        decisions.append((row, vr))

    # Проверка на вырождение: правило, повышающее ВСЕХ подряд на заметной
    # выборке, перестало различать. В dry-run только помечаем.
    if (len(rows) >= config.PROMOTE_DEGENERATE_MIN_CHECKED
            and len(decisions) == len(rows) and rows):
        summary["degenerate"] = True
        if not dry_run:
            summary["aborted"] = True
            return summary

    queue = linking.queue_index(con) if not dry_run else None
    for row, vr in decisions:
        if not dry_run:
            cur = con.execute(
                "UPDATE source SET status='active', vr=?, avg_views=? "
                "WHERE id=? AND platform='telegram' AND status='candidate'",
                (vr["vr"], int(vr["views_median"]) if vr["views_median"] else None,
                 row["id"]),
            )
            if cur.rowcount == 0:
                continue
            outcome = _close_queue_row(
                con, row, queue, stamp, create_missing_queue_row)
            if outcome == "closed":
                summary["queue_closed"] += 1
            elif outcome == "created":
                summary["queue_created"] += 1
            db.log_run(
                con, "INFO",
                f"промоушен candidate->active by={PROMOTE_SOURCE} "
                f"posts={row['posts']} vr={vr['vr']}",
                handle=row["handle"], run_id=run_id)
        summary["promoted"] += 1
        summary["promoted_handles"].append(row["handle"])
    if not dry_run:
        con.commit()
    return summary


def _close_queue_row(con, source_row, queue: linking.Index, stamp: str,
                     create_missing: bool) -> str:
    """Закрыть (или завести) строку очереди для повышенного реестра.

    Возвращает ``"closed"`` (строка была), ``"created"`` (строка заведена),
    ``"none"`` (ничего не сделано).
    """
    tg_id = linking.tg_id_of(source_row)
    match = queue.match(handle=source_row["handle"], tg_id=tg_id)
    if match is not None:
        con.execute(
            "UPDATE candidate SET status=?, promoted_by=?, promoted_at=?, "
            "validated=COALESCE(validated,'ok') WHERE id=?",
            (PROMOTED_STATUS, PROMOTE_SOURCE, stamp, match))
        return "closed"
    canonical = linking.normalize_handle(source_row["handle"])
    if not canonical:
        return "none"
    if not create_missing:
        return "none"
    # Строки очереди для канала не было: заводим её, чтобы маркер повышения не
    # терялся (D-55). Повторный прогон найдёт её и не продублирует.
    cur = con.execute(
        """
        INSERT INTO candidate(platform, kind, handle, external_id, display_handle,
                              found_via, status, promoted_by, promoted_at,
                              first_seen_at, last_seen_at)
        VALUES('telegram', 'channel', ?, ?, ?, 'registry_promote', ?, ?, ?, ?, ?)
        ON CONFLICT(platform, handle) DO UPDATE SET
            status=excluded.status, promoted_by=excluded.promoted_by,
            promoted_at=excluded.promoted_at, last_seen_at=excluded.last_seen_at
        """,
        (canonical, source_row["external_id"], source_row["handle"],
         PROMOTED_STATUS, PROMOTE_SOURCE, stamp, stamp, stamp),
    )
    if cur.rowcount:
        queue.add(cur.lastrowid, handle=canonical, tg_id=tg_id)
    return "created"


def format_report(summary: dict) -> str:
    """Человекочитаемая таблица «проверено / повышено / отклонено и почему»."""
    lines = [
        "=== повышение кандидатов Telegram (candidate -> active)",
        f"проверено кандидатов: {summary['checked']}",
        f"повышено: {summary['promoted']}"
        + (" (dry-run, без записи)" if summary["dry_run"] else ""),
    ]
    if summary.get("degenerate"):
        state = "прогон ОТМЕНЁН" if summary.get("aborted") else "(dry-run)"
        lines.append(
            f"вырождение: повышены все {summary['checked']} при пороге "
            f"{summary['degenerate_threshold']} — {state}")
    lines.append(f"vr посчитан: {summary.get('vr_known', 0)}; "
                 f"vr неизвестен: {summary.get('vr_unknown', 0)}")
    reasons = summary.get("reasons") or {}
    if reasons:
        lines.append("отклонено:")
        for reason, count in sorted(reasons.items()):
            lines.append(f"  {reason}: {count}")
    else:
        lines.append("отклонено: 0")
    if not summary["dry_run"]:
        lines.append(f"строк очереди закрыто: {summary.get('queue_closed', 0)}; "
                     f"заведено: {summary.get('queue_created', 0)}")
    lines.append(
        f"пороги: min_posts={summary['min_posts']} require_vr={summary['require_vr']} "
        f"min_vr={summary.get('min_vr', config.PROMOTE_MIN_VR)}")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    path = config.resolve_db(args.db)
    if not args.dry and db.is_production_db(path) and not args.allow_production:
        print("отказ: запись в боевую базу без --allow-production "
              "(приёмка идёт на копии: TUBER_DB=/tmp/tuber-copy.db)", file=sys.stderr)
        return 2
    con = db.connect(path)
    try:
        run_id = None
        if not args.dry:
            run_id = db.start_run(con, "promote")
        summary = promote_candidates(
            con, dry_run=args.dry, min_posts=args.min_posts, min_vr=args.min_vr,
            require_vr=args.require_vr, run_id=run_id)
        if run_id is not None:
            db.finish_run(con, run_id, channels_ok=summary["promoted"],
                          errors=sum(summary["reasons"].values()))
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            print(format_report(summary))
        if summary.get("aborted"):
            return 3
        return 0
    finally:
        con.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
