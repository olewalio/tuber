"""CLI очереди замеров 1/6/24/72 ч для X и Telegram (ТЗ-43, контур 1 «Скорость»).

Подкоманды::

    python3 -m tuber metrics plan     [--db PATH] [--now ISO] [--lookback-hours 96]
    python3 -m tuber metrics backfill [--db PATH] [--limit N] [--dry-run] [--allow-production]
    python3 -m tuber metrics sweep    [--db PATH] [--now ISO] [--limit N] [--no-network]
                                      [--dry-run] [--allow-production]
    python3 -m tuber metrics early    [--db PATH] [--now ISO] [--window-days 7] [--limit 5]
    python3 -m tuber metrics run      [--db PATH] [--now ISO] [--limit N] [--no-network]
                                      [--dry-run] [--allow-production]

``plan`` ставит стадии ``1h/6h/24h/72h`` от ``published_at`` (идемпотентно),
``sweep`` добирает просроченные (``due_at < now``, ``done_at IS NULL``),
``backfill`` заполняет ``bucket``/дельты у уже накопленных снимков X/Telegram,
``early`` печатает блок «Раннее» (только чтение). ``run`` — всё вместе по
расписанию. Запись в боевую базу гейтится ``--allow-production`` (ТЗ-45F).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from tuber import config
from tuber.core import db as _db
from tuber.core import metrics, timeutil

RUN_PLATFORM = "metrics"
RUN_MODE = "queue"

DEFAULT_EARLY_LIMIT = 5


def _db_path(args) -> str:
    if getattr(args, "db", None):
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


def _iso_now(now=None) -> str:
    if now is None:
        return timeutil.iso_now()
    iso = timeutil.parse_any(now)
    if iso is None:
        raise ValueError(f"не разобрана дата: {now!r}")
    return iso


# ---------------------------------------------------------------------------
# Сеть: свежие метрики для добора просроченных
# ---------------------------------------------------------------------------

class NetworkFetcher:
    """Тонкая обёртка над транспортами X и Telegram (только чтение метрик).

    Telegram: одна страница ``t.me/s/<handle>`` даёт ~20 последних постов с
    просмотрами (кэшируется на прогон — замеры по одному каналу не дублируют
    запрос). X: метрики одного поста через CDN ``tweet-result`` (лайки/ответы).
    Любой отказ возвращает ``None`` — добор не имеет права ронять расписание.

    Разделение ответственности за ``views_checked_at`` (ТЗ-53): метка — только
    сборщика (``platforms/telegram/collect.py``), она управляет ЕГО суточным
    лимитом обновления просмотров. Очередь метку не читает и не пишет: свежие
    просмотры она кладёт снимком напрямую в ``metric_snapshot`` (через
    :func:`tuber.core.metrics._write_snapshot`), поэтому суточный лимит её не
    тормозит. Обратная сторона — конфликт значений: оба писателя не понижают
    уже сохранённый максимум просмотров материала (``metrics.monotonic_views``,
    см. ``collect.py`` и триггеры ``telegram/store.py``), так что откат назад
    невозможен независимо от порядка записи.
    """

    def __init__(self, *, deadline_sec: float | None = None):
        self._deadline = deadline_sec
        self._tg_cache: dict[str, dict] = {}
        self._x_broker = None

    # -- Telegram -----------------------------------------------------------
    def _tg_page(self, handle: str) -> dict:
        if handle in self._tg_cache:
            return self._tg_cache[handle]
        out: dict = {}
        try:
            from tuber.platforms.telegram import collect as tg_collect
            client = tg_collect.make_client()
            try:
                code, text = tg_collect.http_get(client, f"https://t.me/s/{handle}")
            finally:
                client.close()
            if code == 200 and text:
                for p in tg_collect.parse_page(text, handle):
                    out[int(p["message_id"])] = p
        except Exception:  # noqa: BLE001 — сеть не должна ронять добор
            out = {}
        self._tg_cache[handle] = out
        return out

    def _tg(self, external_id, handle):
        if not handle:
            return None
        msg_id = None
        try:
            msg_id = int(str(external_id).rsplit("/", 1)[-1])
        except (TypeError, ValueError):
            return None
        post = self._tg_page(handle).get(msg_id)
        if not post:
            return None
        return {"views": post.get("views"), "forwards": post.get("forwards"),
                "reactions": post.get("reactions")}

    # -- X ------------------------------------------------------------------
    def _x(self, external_id, _handle):
        if not external_id:
            return None
        try:
            if self._x_broker is None:
                from tuber.platforms.x import channels as x_channels
                self._x_broker = x_channels.CdnTweetBroker()
            status, fields, _ = self._x_broker.fetch(str(external_id))
            if status != "ok" or not fields:
                return None
            return {"likes": fields.get("likes"), "replies": fields.get("replies")}
        except Exception:  # noqa: BLE001 — сеть не должна ронять добор
            return None

    def __call__(self, platform, external_id, handle):
        if platform == "telegram":
            return self._tg(external_id, handle)
        if platform == "x":
            return self._x(external_id, handle)
        return None

    def close(self):
        if self._x_broker is not None:
            try:
                self._x_broker.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Подкоманды
# ---------------------------------------------------------------------------

def cmd_plan(args) -> int:
    path = _db_path(args)
    if not _write_gate(args, path):
        print(json.dumps({"dry_run": True, "action": "plan"},
                         ensure_ascii=False, sort_keys=True))
        return 0
    con = _connect(path)
    try:
        stats = metrics.plan_stages(
            con, now=args.now, lookback_hours=args.lookback_hours, limit=args.limit)
        print(f"план стадий: поставлено {stats['inserted']}, уже было"
              f" {stats['existing']}, постов {stats['planned_posts']},"
              f" к добору {stats['due_now']}")
        return 0
    finally:
        con.close()


def _write_gate(args, path) -> bool:
    """Проверить гейт записи; вернуть True, если писать можно."""
    if getattr(args, "dry_run", False):
        return False
    if _is_production(path) and not args.allow_production:
        print("отказ: запись в боевую базу без --allow-production"
              " (приёмка идёт на копии через `tuber db backup`)", file=sys.stderr)
        raise SystemExit(2)
    return True


def cmd_backfill(args) -> int:
    path = _db_path(args)
    writing = _write_gate(args, path)
    if not writing:
        print(json.dumps({"dry_run": True, "action": "backfill"},
                         ensure_ascii=False, sort_keys=True))
        return 0
    con = _connect(path)
    try:
        stats = metrics.backfill_snapshots(con, limit=args.limit)
        print(f"бэкфилл: снимков заполнено {stats['snapshots']},"
              f" материалов {stats['contents']},"
              f" content_latest обновлено {stats.get('content_latest', 0)}")
        return 0
    finally:
        con.close()


def cmd_sweep(args) -> int:
    path = _db_path(args)
    writing = _write_gate(args, path)
    if not writing:
        print(json.dumps({"dry_run": True, "action": "sweep"},
                         ensure_ascii=False, sort_keys=True))
        return 0
    con = _connect(path)
    fetcher = None
    if not args.no_network:
        fetcher = NetworkFetcher()
    try:
        stats = metrics.sweep_overdue(con, now=args.now, limit=args.limit, fetch=fetcher)
        print(_sweep_line(stats))
        return 0
    finally:
        if fetcher is not None:
            fetcher.close()
        con.close()


def _sweep_line(stats) -> str:
    return (f"добор: привязано {stats['linked']}, замерено {stats['recorded']},"
            f" отложено {stats['deferred']}, закрыто без данных {stats['missed']},"
            f" всего просрочено {stats['processed']}")


def cmd_early(args) -> int:
    con = _connect(_db_path(args))
    try:
        limit = args.limit or DEFAULT_EARLY_LIMIT
        data = metrics.build_early(
            con, now=args.now, window_days=args.window_days, limit=limit)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
            return 0
        text = metrics.format_early(data)
        if text:
            print(text)
        return 0
    finally:
        con.close()


def cmd_run(args) -> int:
    """По расписанию: планирование → бэкфилл → добор → блок «Раннее»."""
    path = _db_path(args)
    writing = _write_gate(args, path)
    if not writing:
        print(json.dumps({"dry_run": True, "action": "run"},
                         ensure_ascii=False, sort_keys=True))
        return 0
    con = _connect(path)
    fetcher = None
    if not args.no_network:
        fetcher = NetworkFetcher()
    try:
        plan_stats = metrics.plan_stages(
            con, now=args.now, lookback_hours=args.lookback_hours, limit=args.limit)
        back_stats = metrics.backfill_snapshots(con, limit=0)
        sweep_stats = metrics.sweep_overdue(
            con, now=args.now, limit=args.limit, fetch=fetcher)
        if args.json:
            print(json.dumps({"plan": plan_stats, "backfill": back_stats,
                              "sweep": sweep_stats},
                             ensure_ascii=False, sort_keys=True))
        else:
            print(f"план стадий: поставлено {plan_stats['inserted']}, уже было"
                  f" {plan_stats['existing']}, к добору {plan_stats['due_now']}")
            print(f"бэкфилл: снимков заполнено {back_stats['snapshots']},"
                  f" материалов {back_stats['contents']},"
                  f" content_latest обновлено {back_stats.get('content_latest', 0)}")
            print(_sweep_line(sweep_stats))
        data = metrics.build_early(con, now=args.now, limit=DEFAULT_EARLY_LIMIT)
        if not args.json:
            # ТЗ-53 (D-67): статусы стадий — «закрыто без данных» отдельно от
            # «ещё не наступило». Строка печатается всегда, чтобы «закрыто 0 из N»
            # больше не читалось как закрытая без данных стадия.
            print(metrics.format_schedule(data["schedule"]))
        text = metrics.format_early(data)
        if text:
            print(text)
        return 0
    finally:
        if fetcher is not None:
            fetcher.close()
        con.close()


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tuber metrics",
                                 description="Очередь замеров метрик (ТЗ-43)")
    sub = ap.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="путь к базе (иначе TUBER_DB)")
    common.add_argument("--now", default=None, help="якорь времени (ISO UTC)")
    common.add_argument("--limit", type=int, default=0, help="потолок записей за прогон")
    common.add_argument("--json", action="store_true", help="машинный вывод")

    gate = argparse.ArgumentParser(add_help=False)
    gate.add_argument("--dry-run", action="store_true", help="ничего не писать")
    gate.add_argument("--allow-production", action="store_true",
                      help="разрешить запись в боевую базу (иначе отказ)")

    sp = sub.add_parser("plan", parents=[common, gate],
                        help="поставить стадии от published_at")
    sp.add_argument("--lookback-hours", type=int,
                    default=metrics.PLAN_LOOKBACK_HOURS,
                    help="глубина планирования, ч (по умолчанию 96)")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("backfill", parents=[common, gate],
                        help="заполнить bucket/дельты у накопленных снимков")
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser("sweep", parents=[common, gate],
                        help="добрать просроченные стадии очереди")
    sp.add_argument("--no-network", action="store_true",
                    help="не ходить в сеть: только привязка существующих снимков")
    sp.add_argument("--lookback-hours", type=int,
                    default=metrics.PLAN_LOOKBACK_HOURS, help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_sweep)

    sp = sub.add_parser("early", parents=[common], help="блок «Раннее» (только чтение)")
    sp.add_argument("--window-days", type=int, default=7,
                    help="окно постов, дней (по умолчанию 7)")
    sp.set_defaults(func=cmd_early)

    sp = sub.add_parser("run", parents=[common, gate],
                        help="планирование + бэкфилл + добор + «Раннее»")
    sp.add_argument("--no-network", action="store_true",
                    help="не ходить в сеть (только привязка существующих снимков)")
    sp.add_argument("--lookback-hours", type=int,
                    default=metrics.PLAN_LOOKBACK_HOURS, help="глубина планирования, ч")
    sp.set_defaults(func=cmd_run)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "metrics":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if not hasattr(args, "limit"):
        args.limit = 0
    if not hasattr(args, "json"):
        args.json = False
    if not hasattr(args, "dry_run"):
        args.dry_run = False
    if not hasattr(args, "allow_production"):
        args.allow_production = False
    if not hasattr(args, "now"):
        args.now = None
    return args.func(args)
