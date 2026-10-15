"""Единый конфиг проекта.

Волна ТЗ-1 намеренно минимальна: путь к единой базе и настройки соединения.
Платформенные настройки (лимиты, ключи, расписания) добавляются в следующих
волнах и читаются из ``config/``.

Приоритет значения пути к БД:
1. явный аргумент ``--target`` (передаётся в tools);
2. переменная окружения ``TUBER_DB``;
3. ``db.path`` из ``default.ini``;
4. значение по умолчанию ``data/tuber.db`` относительно корня репозитория.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

# Корень репозитория: .../tuber/  (файл лежит в tuber/tuber/config.py)
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DB_PATH = ROOT / "data" / "tuber.db"

# Настройки соединения из §3 ТЗ-1.
BUSY_TIMEOUT_MS = 15000
JOURNAL_MODE = "WAL"
SYNCHRONOUS = "NORMAL"


def _read_default_ini() -> dict[str, str]:
    parser = configparser.ConfigParser()
    path = ROOT / "default.ini"
    if path.exists():
        parser.read(path, encoding="utf-8")
    out: dict[str, str] = {}
    if parser.has_section("db"):
        for key, value in parser.items("db"):
            out[key] = value
    return out


def db_path(explicit: str | os.PathLike[str] | None = None) -> str:
    """Разрешить путь к единой базе по правилам приоритета выше."""
    if explicit:
        return str(explicit)
    env = os.environ.get("TUBER_DB")
    if env:
        return env
    cfg = _read_default_ini()
    configured = cfg.get("path")
    if configured:
        p = Path(configured)
        return str(p if p.is_absolute() else ROOT / p)
    return str(DEFAULT_DB_PATH)


def platforms() -> tuple[str, ...]:
    """Коды платформ ядра."""
    return ("youtube", "x", "telegram")
