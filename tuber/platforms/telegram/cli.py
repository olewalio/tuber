"""CLI Telegram-платформы монорепозитория (ТЗ-4).

    python3 -m tuber tg init-db [--csv PATH]
    python3 -m tuber tg collect [--mode web|mtproto|resolve|all] [--handle H] …
    python3 -m tuber tg scoring [forwards|baselines|authors|scores|report|all] …
    python3 -m tuber tg discover [--limit N] [--dry]
    python3 -m tuber tg promote [--dry] [--min-posts N]
    python3 -m tuber tg bridge export [--out FILE] [--dry]
    python3 -m tuber tg bridge import --feed FILE [--dry]
    python3 -m tuber tg prelim [--limit N] [--pages N]

Прежние имена-скрипты сохранены псевдонимами: ``feed-export`` = ``bridge export``,
``feed-import`` = ``bridge import``, ``init`` = ``init-db``.

Путь к базе — ЕДИНАЯ база ``data/tuber.db``; переопределение ``--db PATH``
(глобальный флаг, работает в любой позиции), ``TUBER_TELEGRAM_DB`` (историческое
имя) или ``TUBER_DB``.
"""
from __future__ import annotations

import os
import sys

from . import config


def _apply_db_override(argv: list[str]) -> list[str]:
    """Вынуть глобальный ``--db PATH`` из argv и переопределить путь к базе.

    Приоритет (ТЗ-4 §1): ``--db`` → ``TUBER_TELEGRAM_DB`` → ``TUBER_DB`` →
    боевая единая база ``data/tuber.db``. Флаг работает в любой позиции:
    ``python3 -m tuber tg --db copy.db scoring all``.
    """
    out, i, value = [], 0, None
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
        config.DB_PATH = value
        os.environ["TUBER_TELEGRAM_DB"] = value
        # Модули платформы держат путь и модульными константами (их читают
        # argparse-дефолты). Если они уже импортированы — обновляем и их.
        prefix = __name__.rsplit(".", 1)[0] + "."
        for name, module in list(sys.modules.items()):
            if not name.startswith(prefix) or module is None:
                continue
            for attr in ("DB_PATH", "DEFAULT_DB"):
                if hasattr(module, attr):
                    setattr(module, attr, value)
    return out


#: Подкоманды: имя (и синонимы) → ленивый импорт модуля и его ``main``.
COMMANDS: dict[str, str] = {
    "init-db": "init_db",
    "init": "init_db",
    "collect": "collect",
    "scoring": "scoring",
    "discover": "discover",
    "promote": "promote",
    "prelim": "prelim",
    "classify": "classify",
    "followers": "followers",
    "bridge-export": "feeds_export",
    "feed-export": "feeds_export",
    "export-candidates": "feeds_export",
    "bridge-import": "feeds_import",
    "feed-import": "feeds_import",
    "import-candidates": "feeds_import",
}

USAGE = """\
tuber tg — Telegram-платформа единой базы (перенос tuber-telegram, ТЗ-4)

Подкоманды (флаги каждой — как у прежнего скрипта проекта; полный список:
`python3 -m tuber tg <подкоманда> --help`):
  init-db                 схема ядра + реестр каналов из UNIVERSE.csv
  collect                 сбор постов (--mode web|mtproto|resolve|all)
  scoring                 значимость: forwards|baselines|authors|scores|report|all
  discover                дискавери t.me-кандидатов из своих постов
  promote                 повышение кандидата в active по измерениям
  prelim                  разовое предварительное наполнение t.me/S
  classify                смысловая классификация постов ИИ-каналов (DeepSeek, ТЗ-G)
  followers               подписчики с публичной превью-страницы t.me/<handle> (ТЗ-45)
  bridge export           экспорт X/YouTube-кандидатов (JSONL + таблица candidate)
  bridge import           импорт telegram-кандидатов из JSONL в candidate

Синонимы прежних имён скриптов: feed-export = bridge export,
feed-import = bridge import, init = init-db.

База — ЕДИНАЯ (data/tuber.db). Переопределение: --db PATH (в любой позиции),
TUBER_TELEGRAM_DB (историческое имя) или TUBER_DB.
"""


def _expand_bridge_alias(argv: list[str]) -> list[str]:
    """``bridge export`` → ``bridge-export`` (сохраняем прежний двухсловный вид)."""
    if len(argv) >= 2 and argv[0] == "bridge":
        tail = argv[1]
        if tail in ("export", "feed-export"):
            return ["bridge-export"] + argv[2:]
        if tail in ("import", "feed-import"):
            return ["bridge-import"] + argv[2:]
    return argv


def main(argv=None) -> int:
    """Делегирование в ``main`` соответствующего модуля с ЕГО argparse.

    Разбор подкоманды сделан вручную, а не вложенными ``subparsers``: с
    ``nargs=argparse.REMAINDER`` флаг, стоящий первым после подкоманды
    (``tg collect --mode web``), верхний парсер объявляет «unrecognized»
    и до обработчика не доходит.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        argv = _apply_db_override(argv)
    except SystemExit as exc:
        print(exc.code or "неверные аргументы", file=sys.stderr)
        return 2
    argv = _expand_bridge_alias(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"неизвестная подкоманда: {command}\n")
        print(USAGE)
        return 2
    try:
        module = __import__(f"{__package__}.{module_name}", fromlist=["main"])
        # Красивый `--help` подкоманды (argparse берёт prog из argv[0]).
        sys.argv[0] = f"tuber tg {command}"
        return module.main(rest)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
