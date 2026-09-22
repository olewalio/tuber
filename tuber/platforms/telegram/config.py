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

# ---------------------------------------------------------------------------
# Повышение кандидата в активные (последняя миля «кандидат → источник»).
# TODO(debt-D-54): закрыт в ТЗ-51 — пороги откалиброваны по боевой базе
#   21.09.2026; `vr` больше не берётся из импорта UNIVERSE.csv, а считается при
#   промоушене по фактическим постам (см. `promote.compute_vr`). Числа — ниже.
# Правило ИЗМЕРИМОЕ, а не по календарю: канал становится ``active``, когда по
# нему уже собран материал и он не отбракован. Считается не колонка
# ``posts_collected`` (её никто не ведёт — 0 у всех), а реальное число строк
# ``content`` канала. Пороги откалиброваны по боевой базе 21.09.2026 (ТЗ-51).
#
# Калибровка (боевая база, только чтение; 743 кандидата Telegram в ``source``):
#   * ``posts >= 20``   — 584 кандидата (собрано достаточно; коллектор кладёт
#     ~20 постов на канал за прогон, поэтому 20 — нижняя граница «канал живой»,
#     а не «случайно один пост»);
#   * ``antifraud = 0`` — 743 (ни один кандидат не отбракован антифродом);
#   * ``vr`` посчитан по фактическим постам (медиана просмотров / подписчики)
#     у 425 из этих 584; у 159 данных на ``vr`` не хватает (нет подписчиков или
#     просмотров) — это ЯВНОЕ состояние «vr неизвестен», а не тихий отказ.
#   * ``PROMOTE_MIN_VR = 15.0`` — нижний квартиль распределения ``vr`` (Q1 ≈ 14)
#     по 425 кандидатам с посчитанным ``vr`` (медиана ≈ 27): порог отсекает
#     слабейшую четверть по охвату и НЕ пропускает всех подряд. Пропускает
#     307 каналов (41 % очереди Telegram, 53 % пригодных), из них 16 попадают в
#     выдачу «сливки» — оба числа с запасом перекрывают критерий плана
#     «≥ 50 активных, ≥ 10 в сливках».
# ---------------------------------------------------------------------------
PROMOTE_MIN_POSTS = 20      # минимум собранных постов (content) по каналу
PROMOTE_MAX_ANTIFRAUD = 0   # допускается только antifraud_flag = 0
PROMOTE_REQUIRE_VR = True   # требуется посчитанный vr (не NULL)
PROMOTE_MIN_VR = 15.0       # минимум виральности, % (Q1 распределения vr, см. выше)

#: Порог вырождения: если на входе не меньше стольких кандидатов и повышены
#: ВСЕ до одного, правило перестало различать и выродилось («повышает всех
#: подряд»). Такой прогон в боевом режиме отменяется, а не выполняется молча.
PROMOTE_DEGENERATE_MIN_CHECKED = 50


def resolve_db(cli_db: str | None = None) -> str:
    """Путь к единой базе: ``--db`` → ``TUBER_TELEGRAM_DB`` → ``TUBER_DB`` → боевая."""
    return str(cli_db or DB_PATH)


def ensure_dirs() -> None:
    """Создать рабочие каталоги, если их нет (идемпотентно)."""
    for path in (DATA_DIR, LOG_DIR, REPORTS_DIR, EXCHANGE_DIR):
        os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# Закрытый список тем (SCHEMA.md §2.8). Telegram-классификация (ТЗ-G) пишет
# тему только из этого списка, иначе NULL. Список ТОТ ЖЕ, что у YouTube-ветки:
# объединённый отчёт и Datapine Radar должны видеть сравнимые ярлыки тем, а не
# три разные таксономии. Берётся из одного источника, чтобы не разъехаться.
# ---------------------------------------------------------------------------
from tuber.platforms.youtube.config import (  # noqa: E402,F401
    TOPICS,
    TOPIC_SET,
    is_valid_topic,
)
