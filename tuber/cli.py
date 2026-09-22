"""CLI монорепозитория.

Поддерживаемые команды::

    python3 -m tuber migrate --target ... --os ... --x ... --tg ...
    python3 -m tuber tools migrate --target ...        # эквивалент (ТЗ-1 §5)
    python3 -m tuber parity  --target ... --os ... --x ... --tg ...
    python3 -m tuber tools parity  --target ...
    python3 -m tuber schema --target ...               # печать/проверка схемы
    python3 -m tuber report [--db PATH] [--days 10] [--compact]
                            # объединённая выдача (ТЗ-5 §2); --compact — сводка владельцу
    python3 -m tuber db backup [--db PATH] [--dir DIR] # суточный бэкап + integrity (ТЗ-5 §1.4)
    python3 -m tuber db integrity [--db PATH]          # только PRAGMA integrity_check
    python3 -m tuber yt <подкоманда> ...               # YouTube (ТЗ-2)
    python3 -m tuber x  <подкоманда> ...               # X / Twitter (ТЗ-3)
    python3 -m tuber tg <подкоманда> ...               # Telegram (ТЗ-4)

Подкоманды X (`x`) — прежние команды проекта tuber-x:
  init, instances, registry, discover, blocklist, collect, import_candidates,
  export_candidates, backfill, budget, enrich, synd-snapshot, fulltext, health,
  scores, classify, stories, report.
Путь к базе — `--db PATH` (глобальный флаг, работает в любой позиции) или
переменная `TUBER_DB`; для X дополнительно поддерживается историческое
`TUBER_X_DB`, как в tuber-x.

Подкоманды Telegram (`tg`) — прежние команды проекта tuber-telegram:
  init-db, collect, scoring, discover, prelim, `bridge export` (= feed-export),
  `bridge import` (= feed-import). Все пишут в ЕДИНУЮ базу `data/tuber.db`;
  путь переопределяется `--db PATH` (глобальный флаг), `TUBER_TELEGRAM_DB`
  (историческое имя) или `TUBER_DB`.

Подкоманды YouTube (`yt`) — прежние команды проекта tuber-os:
  report, collect, snapshots, classify, comments, viral-refresh, migrate-shorts,
  migrate-speed, expand-migrate, expand, daily, seo, candidates-export,
  candidates-import. Путь к базе — `--db PATH` или переменная `TUBER_DB`
  (по умолчанию data/tuber.db монорепозитория).

Диагностика:
  doctor    интерпретатор, версия SQLite, путь к базе, версия схемы ядра
"""


from __future__ import annotations

import argparse
import sys

USAGE = """\
tuber — единая база трёх платформ (YouTube / X / Telegram)

Команды:
  migrate   перенести данные из legacy-баз в единое ядро (read-only по legacy)
  parity    сверить числа legacy ↔ ядро
  schema    вывести список объектов схемы ядра
  doctor    диагностика окружения: интерпретатор, SQLite, база, схема
  report    объединённая выдача по трём платформам из единой базы (ТЗ-5)
  db        операции с единой базой: `db backup` (копия + integrity), `db integrity`
  graph     граф источников (ТЗ-8): `graph backfill`, `graph consume`, `graph report`,
            `graph verify-feeds`, `graph prune`, `graph first-movers` (ТЗ-47)
  rating    рейтинг растущих авторов «сливки» (ТЗ-45):
            `rating slivki` (выдача), `rating slivki capture` (суточный снимок)
  queue     разбор очереди кандидатов (ТЗ-46): `queue review` (вердикты),
            `queue promote` (candidate -> активный сбор), `queue report`
  trends    контур 5 (ТЗ-48): `trends inside` (подтемы сюжетов),
            `trends novelties` (новинки HN/GitHub/Product Hunt/arXiv)
  comments  контур 6 (ТЗ-49): `comments youtube` (расширенный сбор),
            `comments telegram` (ответы через Telethon), `comments x`
            (сигнал «горячий спор» по счётчику), `comments top` (выдача)
  digest    ежедневная выдача «Сливки» одной командой (ТЗ-50):
            `digest slivki` (10 авторов + 5 подтем + 5 новинок + 3 обсуждения
            + перцентиль «где я»), `digest slivki --send` (доставка в Telegram)
  metrics   очередь замеров 1/6/24/72 ч для X и Telegram (ТЗ-43, контур 1
            «Скорость»): `metrics plan|sweep|backfill|early|run`

Примеры:
  python3 -m tuber migrate --target data/tuber.db --os /root/tuber-os/data/tuber.db \\
      --x /root/tuber-x/data/tuber_x.db --tg /root/tuber-telegram/data/tuber_telegram.db
  python3 -m tuber parity  --target data/tuber.db --os ... --x ... --tg ...
  python3 -m tuber doctor [--db data/tuber.db]
"""


def _print_doctor(argv: list[str]) -> int:
    """Диагностика окружения (ТЗ-3c, задача 2): интерпретатор, SQLite, схема."""
    import platform

    from tuber.core import sqlcompat

    parser = argparse.ArgumentParser(prog="tuber doctor", add_help=False)
    parser.add_argument("--db", dest="db_path")
    args, _ = parser.parse_known_args(argv)

    info = sqlcompat.sqlite_info()
    print("tuber doctor")
    print("  Python:      %s" % info["python_version"])
    print("  исполняемый: %s" % info["executable"])
    print("  SQLite:      %s (минимум для адаптеров: %s)"
          % (info["sqlite_version"],
             ".".join(str(p) for p in sqlcompat.MIN_SQLITE_VERSION)))
    print("  платформа:   %s" % platform.platform())

    db_path = args.db_path or _env_db_path()
    if not db_path:
        print("  база:        (не задана; укажите --db PATH или TUBER_DB)")
        return 0
    print("  база:        %s" % db_path)

    from pathlib import Path
    if not Path(db_path).exists():
        print("  схема:       (файла нет — база будет создана при первом запуске)")
        return 0
    try:
        from tuber.core import db
        conn = db.connect(db_path, readonly=True)
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'").fetchone()
            print("  схема ядра:  %s" % (row[0] if row else "(нет schema_meta)"))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — диагностика не должна падать
        print("  схема:       не прочитана (%s)" % exc)
    return 0


def _env_db_path() -> str | None:
    import os

    return os.environ.get("TUBER_DB") or os.environ.get("TUBER_X_DB")


def _print_schema(argv: list[str]) -> int:
    from tuber.core import db
    from tuber.core import schema

    if not argv:
        print("нужен --target <путь к базе>")
        return 2
    target = argv[0] if argv[0] != "--target" else (argv[1] if len(argv) > 1 else None)
    if not target:
        print("нужен --target <путь к базе>")
        return 2
    conn = db.connect(target)
    try:
        print("Таблицы:", ", ".join(schema.REQUIRED_TABLES))
        print("Индексы:", ", ".join(schema.REQUIRED_INDEXES))
        print("Представления:", ", ".join(schema.REQUIRED_VIEWS))
        row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        print("Версия схемы:", row[0] if row else "(нет)")
        from tuber.core import sqlcompat
        print("SQLite:", sqlcompat.format_sqlite_info())
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 2

    # Обёртка «tools <command>» из ТЗ-1 §5.
    if argv[0] == "tools":
        argv = argv[1:]
    if not argv:
        print(USAGE)
        return 2

    command, rest = argv[0], argv[1:]
    if command == "migrate":
        from tuber.tools import migrate_legacy
        return migrate_legacy.main(rest)
    if command == "parity":
        from tuber.tools import parity_report
        return parity_report.main(rest)
    if command == "schema":
        return _print_schema(rest)
    if command == "doctor":
        return _print_doctor(rest)
    if command == "db":
        # Операции с единой базой: `tuber db backup`, `tuber db integrity`.
        from tuber.tools import db_backup
        return db_backup.main(rest)
    if command == "report":
        # Объединённая выдача по трём платформам из единой базы (ТЗ-5 §2).
        from tuber.analysis import report
        return report.main(rest)
    if command == "graph":
        # Граф источников (ТЗ-8): рёбра, кандидаты, веб-фиды.
        from tuber.tools import graph_cmd
        return graph_cmd.main(rest)
    if command == "rating":
        # Рейтинг «сливки» (ТЗ-45): `rating slivki [capture|report]`.
        from tuber.analysis import slivki
        return slivki.main(rest)
    if command == "queue":
        # Разбор очереди кандидатов и миля повышения (ТЗ-46):
        # `queue review`, `queue promote`, `queue report`.
        from tuber.analysis import queue as queue_cmd
        return queue_cmd.main(rest)
    if command == "trends":
        # Контур 5 (ТЗ-48): `trends inside` (подтемы), `trends novelties`.
        from tuber.analysis import trends as trends_cmd
        return trends_cmd.main(rest)
    if command == "comments":
        # Контур 6 (ТЗ-49): `comments youtube|telegram|x` (сбор) и `comments top`
        # (обсуждения с числами и вопросы аудитории).
        from tuber.analysis import comments as comments_cmd
        return comments_cmd.main(rest)
    if command == "digest":
        # Ежедневная выдача «Сливки» одной командой (ТЗ-50):
        # `digest slivki` собирает 10 авторов + 5 подтем + 5 новинок +
        # 3 обсуждения и перцентиль «где я»; `--send` доставляет в Telegram.
        from tuber.analysis import digest as digest_cmd
        return digest_cmd.main(rest)
    if command == "metrics":
        # Очередь замеров 1/6/24/72 ч для X и Telegram (ТЗ-43, контур 1):
        # `metrics plan|sweep|backfill|early|run`.
        from tuber.analysis import metric_queue as metrics_cmd
        return metrics_cmd.main(rest)
    if command == "x":
        # Подкоманды X: `python3 -m tuber x report`, `... x collect` и т.д. —
        # прежние имена и флаги проекта tuber-x.
        from tuber.platforms.x import cli as x_cli
        return x_cli.main(rest)
    if command in ("tg", "telegram"):
        # Подкоманды Telegram: `python3 -m tuber tg collect`, `... tg scoring all`
        # и т.д. — прежние имена скриптов проекта tuber-telegram.
        from tuber.platforms.telegram import cli as tg_cli
        return tg_cli.main(rest)
    if command in ("yt", "youtube"):
        # Подкоманды YouTube: `python3 -m tuber yt report`, `... yt collect` и
        # т.д. — прежние имена и флаги проекта tuber-os.
        from tuber.platforms.youtube import cli as yt_cli
        return yt_cli.main(rest)

    print(f"неизвестная команда: {command}\n")
    print(USAGE)
    return 2
