"""Конфигурация Telegram-платформы монорепозитория: пути, лимиты, защита баз.

Проект переехал из ``/root/tuber-telegram`` в ``tuber/platforms/telegram``
монорепозитория (ТЗ-4), поэтому:

* ``ROOT`` — корень МОНОрепозитория (``/root/tuber``), а не каталог проекта:
  ``data/``, ``config/``, ``logs/`` и ``reports/`` теперь общие;
* ``DB_PATH`` — ЕДИНАЯ база ``data/tuber.db``. Прежние способы выбора базы
  сохранены: ``TUBER_TELEGRAM_DB`` (историческое имя приёмки) и ``TUBER_DB``
  (короткое имя ТЗ «починка виральности»), плюс ``--db`` у CLI.

``DEFAULT_DB`` — боевая единая база. Она же используется гейтом
:func:`tuber.platforms.telegram.scoring.assert_can_write`: изменяющие прогоны
делаются на КОПИИ, а запись в боевую требует явного ``--allow-production``.
"""

from __future__ import annotations

import os

# Файл лежит в tuber/platforms/telegram/config.py — корень репозитория на
# четыре уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
# Отчёты прогонов — выдача, а не код. Каталог переопределяется переменной
# TUBER_TG_REPORTS_DIR: приёмка уводит их в свой временный каталог, чтобы не
# сорить в репозитории (в ТЗ-4 reports/ намеренно не переносится).
REPORTS_DIR = os.environ.get("TUBER_TG_REPORTS_DIR") or os.path.join(ROOT, "reports")
DOCS_DIR = os.path.join(ROOT, "docs")
EXCHANGE_DIR = os.path.join(DATA_DIR, "exchange")
CONFIG_DIR = os.path.join(ROOT, "config", "telegram")

SCORING_CONFIG = os.path.join(CONFIG_DIR, "scoring.json")
BRIDGE_CONFIG = os.path.join(CONFIG_DIR, "bridge_sources.json")

# Боевая единая база (без учёта переменных окружения): нужна гейту записи и
# приёмке, которая доказывает, что боевая база не тронута.
DEFAULT_DB = os.path.join(DATA_DIR, "tuber.db")
PRODUCTION_DB = DEFAULT_DB

#: Псевдоним для совместимости с прежним API ``scripts/scoring.py``.
DB_PATH = os.environ.get("TUBER_TELEGRAM_DB") or os.environ.get("TUBER_DB") or DEFAULT_DB

# Таймаут ожидания блокировки записи, с (как в legacy db.connect и адаптере X):
# в единой базе пишут несколько платформ.
BUSY_TIMEOUT_SEC = 30.0

# Лимиты сбора (перенесены из scripts/collect.py без изменений).
HTTP_TIMEOUT = 25.0
WEB_MAX_PAGES = 5
RESOLVE_MAX_PER_RUN = 20
RESOLVE_MAX_PER_DAY = 20

# Опознание бота-сборщика в transport_account_state и логах.
ACCOUNT_NAME = "tg_collector"


def resolve_db(cli_db: str | None = None) -> str:
    """Путь к единой базе: ``--db`` → ``TUBER_TELEGRAM_DB`` → ``TUBER_DB`` → боевая."""
    return str(cli_db or DB_PATH)


def ensure_dirs() -> None:
    """Создать рабочие каталоги, если их нет (идемпотентно)."""
    for path in (DATA_DIR, LOG_DIR, REPORTS_DIR, EXCHANGE_DIR):
        os.makedirs(path, exist_ok=True)
