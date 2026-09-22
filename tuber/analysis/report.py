"""Объединённая выдача по трём платформам из ОДНОЙ базы (ТЗ-5 §2).

    python3 -m tuber report [--db data/tuber.db] [--days 10] [--json]
                            [--compact] [--compact-limit N] [--save] [--out DIR]
                            [--readable PATH.md] [--docx PATH.docx]

Отчёт читает единое ядро (``content`` / ``metric_snapshot`` / ``content_latest``
/ ``score`` / ``story_member`` / ``source``) и печатает пять секций:

1. **YouTube** — топ по просмотрам в сутки за окно (по умолчанию 10 дней) с
   отсечкой по порогу показов: 50 000 для не-русских видео и 10 000 для русских
   (``content.lang = 'ru'``). Порог и окно — не «улучшение» формул, а отсечка
   микровыборки: так делал прежний предиктор поиска tuber-os
   (``discover_videos.py``: шортсы 50 000 / обычные 10 000 просмотров, см.
   ``docs/os/AUDIT-OLD-CONTOUR.md``), поэтому значения взяты оттуда, а не
   выдуманы.
2. **X** — без ретвитов, по лайкам и лайкам/час.
3. **Telegram** — свежие посты окна. Значимость (``significance``) НЕсравнима
   между каналами (у каждого своя база нормировки), поэтому ранжирование
   внутриканальное: каналы идут по алфавиту, внутри канала — по значимости,
   вместе с нормированной на канал осью ``eng_channel``.
4. **Сквозной сюжет** — материалы ОДНОГО сюжета с РАЗНЫХ платформ (через
   ``story_member`` + ``content.platform``). Это то, чего не могло быть при
   трёх раздельных базах.
5. **Виральные — охват против своей аудитории** (ТЗ-35, ТЗ-36) — материалы,
   которые собрали много просмотров относительно СВОЕЙ аудитории (охват =
   просмотры ÷ подписчики канала), а не абсолютный топ. В блок 1 («полезное»)
   попадают только продуктовые темы (:data:`PRODUCT_TOPICS`), в блок 2 — всё
   остальное: фермы, нарезки, «прочее»; у всех блоков проверяется свежесть
   публикации и печатается возраст материала. Запуски/релизы/стартапы, X и
   Telegram разведены по отдельным блокам.
6. **Новое за сутки — вышедшее за 24 ч** (ТЗ-Tuber ч.2) — материалы, у которых
   ``content.published_at`` не старше 24 ч; YouTube — по просмотрам/сутки, X —
   по лайкам/час, без дублей с разделами 1–3. В короткую сводку идёт тот же
   раздел (с ограничением до 10 позиций); в полном отчёте он печатается целиком,
   чтобы полный список оставался надмножеством сводки.

Ссылка берётся из ``content.url`` (заполняется при сборе/переносе, см. долг
D-45). Если колонка пуста, честно печатается «нет ссылки (content.url пуст)» —
выдача больше НЕ собирает ссылку на месте и не выдумывает handle. Идентификаторы
не выдумываются.

Честные оговорки о неполноте данных печатаются в подвале и считаются по факту
(пустой ``viral_index``, мёртвая ось ``spread`` в X и т.п.).
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sqlite3
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tuber import config
from tuber.analysis import digest_memory
from tuber.core import db

# --- Отсечки YouTube (взяты из прежнего предиктора поиска tuber-os) ----------
#: Порог показов для не-русских видео.
YOUTUBE_MIN_VIEWS = 50_000
#: Порог показов для русскоязычных видео (content.lang='ru').
YOUTUBE_MIN_VIEWS_RU = 10_000
#: Сколько позиций показывать в секции YouTube.
YOUTUBE_LIMIT = 15
#: Сколько позиций показывать в секции X.
X_LIMIT = 15
#: Сколько позиций X тянуть из базы ПЕРЕД фильтром памяти и кэпом на автора
#: (ТЗ §3.3, ТЗ №2 §1.1): кэп и память применяются ПОСЛЕ выбора, поэтому пул
#: обязан быть заметно шире итоговой выдачи. Оставлено для совместимости;
#: рабочий размер пула считает :func:`candidate_pool`.
X_CANDIDATE_LIMIT = X_LIMIT * 10
#: Кратность широкого пула кандидатов раздела (ТЗ №2 §1.1): перед исключением
#: показанных тянем ``размер_раздела * K`` позиций, иначе после фильтра памяти
#: добирать нечем и раздел схлопывается. ``TUBER_CANDIDATE_FACTOR``, по умолчанию
#: 8. Некорректное/неположительное значение переменной игнорируется.
CANDIDATE_FACTOR = 8
#: Нижняя граница широкого пула, строк (ТЗ №2 §1.1): у X/YouTube фактора 8
#: достаточно, но на малых размерах раздела пол страхует добор.
CANDIDATE_MIN_ROWS = 200
#: Сколько постов показывать на канал в секции Telegram.
TELEGRAM_PER_CHANNEL = 3
#: Сколько каналов показывать в секции Telegram (иначе секция необъятна).
TELEGRAM_CHANNEL_LIMIT = 12
#: Длина «короткого описания» в символах.
DESC_LIMIT = 200

#: Кэп на автора/канал в пределах раздела (ТЗ-Tuber §3): не больше стольких
#: позиций одного автора (канала). Настраивается ``TUBER_MAX_PER_AUTHOR``
#: (по умолчанию 2). Кэп применяется ПОСЛЕ дедупа; при нехватке позиций раздел
#: добирается следующими по ранжированию из того же пула (см. ``_cap_per_key``).
MAX_PER_AUTHOR = 2


def max_per_author() -> int:
    """Кэп на автора в разделе. ``TUBER_MAX_PER_AUTHOR`` (по умолчанию 2).

    Некорректное/неположительное значение переменной игнорируется — откат
    нельзя сломать опечаткой.
    """
    raw = os.environ.get("TUBER_MAX_PER_AUTHOR")
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return MAX_PER_AUTHOR


def candidate_factor() -> int:
    """Кратность широкого пула кандидатов (ТЗ №2 §1.1).

    ``TUBER_CANDIDATE_FACTOR`` (по умолчанию :data:`CANDIDATE_FACTOR`).
    Некорректное/неположительное значение игнорируется: откат нельзя сломать
    опечаткой.
    """
    raw = os.environ.get("TUBER_CANDIDATE_FACTOR")
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return CANDIDATE_FACTOR


def candidate_pool(section_size: int,
                   minimum: int = CANDIDATE_MIN_ROWS) -> int:
    """Размер широкого пула кандидатов раздела (ТЗ №2 §1.1).

    ``размер_раздела * candidate_factor()``, но не меньше ``minimum`` (по
    умолчанию :data:`CANDIDATE_MIN_ROWS`): у X/YouTube фактора 8 достаточно,
    а пол страхует добор на малых разделах.
    """
    return max(int(section_size) * candidate_factor(), int(minimum))


def ru_desc_enabled() -> bool:
    """Русские описания X в сводке (ТЗ-Tuber §1.4). ``TUBER_RU_DESC=0`` — выкл.

    По умолчанию ВКЛючено. При выключении описание X — прежний ``_short(text)``.
    """
    raw = (os.environ.get("TUBER_RU_DESC") or "").strip().lower()
    return raw not in ("0", "false", "no", "off", "нет")


# --- Компактная сводка владельцу (ТЗ-5-доп-2, ТЗ-5-доп-3) --------------------
#: Бюджет сводки по умолчанию, байт UTF-8. История: 3500 → 3900 (ТЗ-34 §3.1) →
#: 4000 (ТЗ-35 §2.7, запас до лимита Telegram 4096) → 12000 (ТЗ-Tuber ч.1 §1):
#: при бюджете 4000 сводка 20.09.2026 вышла «тонкой» — 13 позиций показано,
#: 22 скрыто; разделы «Новое за сутки» и топы не влезали. 12000 байт UTF-8 —
#: осознанный выход за лимит Telegram 4096 байт: сводку доставляет не Telegram,
#: а stdout-обёртка задания Hermes, поэтому длину держим по факту полноты.
#: Значение переопределяется переменной окружения ``TUBER_COMPACT_LIMIT``
#: (откат без правки кода) и флагом CLI ``--compact-limit N``.
DEFAULT_COMPACT_LIMIT = 12000


def compact_limit(override: int | None = None) -> int:
    """Бюджет сводки в байтах UTF-8 (ТЗ-Tuber ч.1 §1).

    Порядок разрешения: явный ``override`` (флаг CLI) → переменная окружения
    ``TUBER_COMPACT_LIMIT`` → :data:`DEFAULT_COMPACT_LIMIT`. Некорректное или
    неположительное значение переменной игнорируется (берётся значение по
    умолчанию), чтобы откат нельзя было сломать опечаткой.
    """
    if override is not None:
        return int(override)
    raw = os.environ.get("TUBER_COMPACT_LIMIT")
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return DEFAULT_COMPACT_LIMIT


#: Бюджет по умолчанию для обратной совместимости вызовов и тестов.
COMPACT_LIMIT = DEFAULT_COMPACT_LIMIT


def telegram_section_enabled() -> bool:
    """Раздел Telegram в компактной сводке (ТЗ-A).

    Управляется переменной окружения ``TUBER_TG_SECTION``. По умолчанию **OFF**:
    раздел не печатается (поведение ТЗ-38), поэтому merge безопасен, а откат —
    одна переменная (или её снятие). Включающие значения: ``1``, ``on``, ``true``
    (регистр не важен). Всё прочее — OFF, чтобы откат нельзя было сломать
    опечаткой. Сама сборка раздела Telegram из сводки не удалена: предохранитель
    :func:`_compact_section_is_telegram` разрешает раздел только при включённом
    флаге.
    """
    raw = (os.environ.get("TUBER_TG_SECTION") or "").strip().lower()
    return raw in ("1", "on", "true")


#: Метка Telegram-ссылки. Предохранитель сводки (ТЗ-38 §2.1): раздел, в пунктах
#: которого есть такая метка, в сводку не печатается. Telegram-раздел
#: печатается только при ``TUBER_TG_SECTION`` (ТЗ-A) и с фильтром профиля по
#: статусу реестра (``s.status='active'``); предохранитель по ссылке продолжает
#: защищать прочие разделы от протечки Telegram-строки.
TELEGRAM_LINK_MARK = "t.me/"

# --- Виральные: охват против СВОЕЙ аудитории (ТЗ-35, ТЗ-36) ------------------
#: Полы и лимиты — из замера боевой базы 18.09.2026
#: (docs/TZ-35-viral-by-audience.md §1–§2), не выдуманы.
#: Главная метрика блока 1–2: ``охват = просмотры ÷ подписчики канала``, а не
#: кратность к медиане канала (та давала артефакты вроде кратности 2197 у
#: канала с медианой 43 просмотра/сутки). Подписчиков нет — материал выпадает
#: (никаких подстановок и оценок).
VIRAL_YT_MIN_SUBS = 1_000
VIRAL_YT_MIN_VIEWS = 5_000
VIRAL_YT_MIN_COVERAGE = 3.0
#: Не более стольких видео с одного канала в блоке (иначе канал займёт весь топ).
VIRAL_YT_PER_CHANNEL = 2
#: Блок 1 — полезное (главный список), блок 2 — фермы и развлекательное.
VIRAL_YT_LIMIT = 15
VIRAL_YT_FUN_LIMIT = 5
#: Темы ПОЛЕЗНОГО YouTube (блок 1). Оба варианта ярлыков старой и новой
#: таксономии: в базе есть дубли («модели и релизы» против «релизы моделей» и
#: т.п.). Материал попадает в блок 1 ТОЛЬКО если его тема входит сюда; любая
#: другая тема, NULL, «прочее» и «ИИ-инструменты для обычных людей» идут в
#: блок 2. Так от полезного списка отсекаются контент-фермы (ТЗ-36 §3.1):
#: замер §2 — без сужения в блок 1 проходило 807 материалов, с сужением 108.
PRODUCT_TOPICS = frozenset({
    "модели и релизы", "релизы моделей", "агенты и автоматизация",
    "кодинг и разработка", "чипы и железо", "инфраструктура и железо",
    "роботы и физический ИИ", "запуски и анонсы", "стартапы и бизнес",
    "инвестиции и раунды", "инструменты разработчика", "наука и медицина",
    "кейсы внедрения", "дата-центры и энергия",
})
#: Темы новых продуктов (блок 3) — подмножество полезных тем. Оба варианта
#: ярлыков старой и новой таксономии (ТЗ-35 §2).
VIRAL_PRODUCT_TOPICS = frozenset({
    "запуски и анонсы", "стартапы и бизнес", "модели и релизы", "релизы моделей",
    "инструменты разработчика", "инвестиции и раунды",
})
#: Блок 3: три подсписка по 5 позиций, не больше 2 на канал.
VIRAL_PRODUCT_YT_MIN_VIEWS = 50_000
VIRAL_PRODUCT_X_MIN_LIKES = 500
VIRAL_PRODUCT_TG_MIN_VIEWS = 10_000
VIRAL_PRODUCT_LIMIT = 5
VIRAL_PRODUCT_PER_CHANNEL = 2
#: Блок 4: X меряется не охватом (подписчики X не собираются вовсе), а
#: кратностью лайков к МЕДИАНЕ автора за 30 дней (пол — не меньше 5 постов).
VIRAL_X_MIN_LIKES = 200
VIRAL_X_MIN_AUTHOR_POSTS = 5
VIRAL_X_AUTHOR_WINDOW_DAYS = 30
#: Нижняя граница «нормальности» автора X (ТЗ-36 §3.4): медиана лайков автора
#: в окне. Без неё мусорный всплеск вида «19 038 лайков при медиане 1» проходил
#: пол кратности; замер §2 — с полом из 201 материала остаётся 177.
VIRAL_X_MIN_AUTHOR_MEDIAN = 20.0
VIRAL_X_MIN_RATIO = 3.0
VIRAL_X_PER_AUTHOR = 2
VIRAL_X_LIMIT = 10
#: «Норма канала» в строке печатается только при таком числе материалов канала
#: в окне (ТЗ-36 §3.5): по 1–2 материалам норма равна самому видео и кратность
#: всегда 1.0 — это шум, а не измерение.
VIRAL_YT_MIN_NORM_SAMPLE = 3
#: Блок 5: Telegram — просмотры против базы канала (source_baseline.median_views,
#: окно 90 дней). Базы нет — материал выпадает (норму по 1–2 постам не выдумываем).
VIRAL_TG_MIN_VIEWS = 1_000
VIRAL_TG_MIN_RATIO = 3.0
VIRAL_TG_PER_CHANNEL = 2
VIRAL_TG_LIMIT = 10


# --------------------------------------------------------------------------- #
# Ссылки
# --------------------------------------------------------------------------- #
#: Честная оговорка вместо ссылки, когда ``content.url`` пуст (D-45).
NO_URL = "нет ссылки (content.url пуст)"


def _link(url: str | None) -> str:
    """Ссылка из ``content.url`` или честная оговорка (без синтеза на месте)."""
    return url or NO_URL


def _telegram_message_id(external_id: str | None) -> int | None:
    if not external_id:
        return None
    _, _, mid = str(external_id).partition("/")
    try:
        return int(mid)
    except (TypeError, ValueError):
        return None


def _short(text: str | None, limit: int = DESC_LIMIT) -> str:
    t = " ".join((text or "").split())
    if not t:
        return "(без текста)"
    return t[:limit] + ("…" if len(t) > limit else "")


# --------------------------------------------------------------------------- #
# Русские описания X (ТЗ-Tuber §1)
# --------------------------------------------------------------------------- #
#: Системный промпт перевода — тот же, что у переводчика отчёта X
#: (``tuber/platforms/x/report.py::ReportTranslator.SYSTEM``), чтобы кэш
#: ``report_text`` наполнялся согласованно.
_RU_SYSTEM_FALLBACK = (
    "Переведи пост на русский язык одной короткой фразой (до 30 слов). "
    "Сохрани имена, названия моделей и чисел. Верни строго JSON "
    "{\"ru\": \"...\"} без пояснений."
)


def _ru_system_prompt() -> str:
    try:
        from tuber.platforms.x.report import ReportTranslator
        return ReportTranslator.SYSTEM
    except Exception:  # noqa: BLE001 — переводчик недоступен, хватит промпта
        return _RU_SYSTEM_FALLBACK


def _live_translation_allowed(conn) -> bool:
    """Сеть разрешена только в боевом режиме, не в pytest (ТЗ §1.2).

    В тестах сетевых вызовов быть не должно: заглушка/кэш ``report_text``.
    На read-only соединении переводим без кэширования (запись в кэш — best
    effort, ошибка записи глушится): иначе CLI-отчёт, открывающий боевую базу
    только на чтение, не смог бы перевести попадающие в вывод позиции.
    """
    if not ru_desc_enabled():
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if "pytest" in sys.modules:
        return False
    return True


def _translate_and_cache(conn, text_hash: str, text: str) -> str | None:
    """Перевести текст через канал DeepSeek и положить в кэш ``report_text``."""
    budget = getattr(config, "REPORT_TRANSLATE_BUDGET", 25)
    try:
        max_chars = int(getattr(config, "CLASSIFY_MAX_TEXT_CHARS", 2000))
    except (TypeError, ValueError):
        max_chars = 2000
    try:
        from tuber.platforms.x import channels
        broker = channels.DeepSeekBroker()
        if not broker.available():
            return None
        res = broker.classify(_ru_system_prompt(), text[:max_chars], max_tokens=400)
        ru = (json.loads(res["content"]).get("ru") or "").strip()
        model = getattr(broker, "model", None)
        broker.close()
    except Exception:  # noqa: BLE001 — сеть/промпт могут отказать
        return None
    if not ru or budget <= 0:
        return None
    try:
        conn.execute(
            "INSERT OR REPLACE INTO report_text(text_hash, ru, model, created_at,"
            " src) VALUES (?,?,?,?,'model')",
            (text_hash, ru, model,
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except sqlite3.Error:
        pass  # кэш не сохранился — перевод всё равно отдаём
    return ru


def _ru_description(conn, text_hash: str | None, text: str | None) -> str | None:
    """Русское описание X по приоритету кэша переводчика (ТЗ §1.1, §1.2).

    Сначала читается кэш ``report_text`` (быстро, без сети). Если текста в кэше
    нет и режим боевой — переводим и кэшируем. Тесты сети не трогают: ``pytest``
    в ``sys.modules`` или read-only база отключают живой перевод.
    """
    if not ru_desc_enabled() or not text:
        return None
    if text_hash:
        try:
            row = conn.execute("SELECT ru FROM report_text WHERE text_hash=?",
                               (text_hash,)).fetchone()
        except sqlite3.Error:
            row = None
        if row and (row["ru"] if isinstance(row, sqlite3.Row) else row[0]):
            return row["ru"] if isinstance(row, sqlite3.Row) else row[0]
    if not text_hash or not _live_translation_allowed(conn):
        return None
    return _translate_and_cache(conn, text_hash, text)


def backfill_ru(conn, limit: int, days: int = 10) -> int:
    """Ручной прогрев кэша переводов X (ТЗ-Tuber §1.5).

    Переводит не больше ``limit`` постов окна, которых нет в ``report_text``.
    Полный бэкфилл архива НЕ делается (деньги ограничены) — это необязательный
    ручной флаг ``--ru-backfill-limit N``. Возвращает число новых переводов.
    """
    if limit <= 0:
        return 0
    cutoff = _now_iso(days)[0]
    rows = conn.execute(
        "SELECT c.text_hash, c.text FROM content c"
        " WHERE c.platform='x' AND c.deleted_at IS NULL"
        " AND COALESCE(c.is_repost,0)=0 AND c.published_at >= ?"
        " AND c.text_hash IS NOT NULL AND length(c.text) > 0"
        " AND NOT EXISTS (SELECT 1 FROM report_text rt"
        "                 WHERE rt.text_hash=c.text_hash)"
        " ORDER BY c.published_at DESC LIMIT ?", (cutoff, int(limit))).fetchall()
    done = 0
    for r in rows:
        if not ru_desc_enabled():
            break
        if _translate_and_cache(conn, r["text_hash"], r["text"]):
            done += 1
    return done



def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value, nd: int = 1) -> str:
    try:
        return f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _shorten_desc(desc: str, limit: int) -> str:
    """Укоротить описание до ``limit`` знаков с многоточием (ТЗ-5-доп-3)."""
    if limit <= 0:
        return ""
    if len(desc) <= limit:
        return desc
    return desc[:limit].rstrip() + "…"


class CompactItem:
    """Пункт сводки с отделённым описанием (ТЗ-5-доп-3).

    Пункт делится на три части: ``head`` (маркер/канал), ``desc`` (собственно
    описание — единственная часть, которую МОЖНО укоротить) и ``tail`` (цифры и
    ПОЛНАЯ ссылка — неприкосновенны). Так при нехватке места под резерв
    укорачивается только описание, а числа и ссылка не меняются.
    """

    __slots__ = ("head", "desc", "tail", "content_id", "author_key",
                 "x_text_hash", "x_text")

    def __init__(self, head: str, desc: str, tail: str,
                 content_id: int | None = None,
                 author_key: str | None = None) -> None:
        self.head = head
        self.desc = desc
        self.tail = tail
        #: id материала в ядре (ТЗ-34 §3.5). Необязателен: по умолчанию None,
        #: поэтому существующие вызовы не меняются. Нужен для дедупликации
        #: сводки — один и тот же content_id не должен печататься дважды
        #: (в разделе 1–3 и в разделе «Виральные»).
        self.content_id = content_id
        #: Ключ автора/канала для кэпа «не больше N на автора» (ТЗ-Tuber §3).
        #: Необязателен: None — позиция под кэп не попадает.
        self.author_key = author_key
        #: Отложенный русский перевод X (ТЗ-Tuber §1.2): текст и его хэш
        #: заполняются только для позиций-кандидатов без готового ``title_ru``/
        #: ``summary_ru``. Перевод выполняется ПОСЛЕ отбора, только для тех
        #: позиций, что реально попали в вывод (иначе платили бы за весь пул).
        self.x_text_hash = None
        self.x_text = None

    @property
    def text(self) -> str:
        return f"{self.head}{self.desc}{self.tail}"

    def render(self, desc_limit: int | None = None) -> str:
        if desc_limit is None:
            return self.text
        return f"{self.head}{_shorten_desc(self.desc, desc_limit)}{self.tail}"


def _now_iso(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def _in_window(published_at, cutoff: str) -> bool:
    """Опубликовано ли ВНУТРИ окна отчёта (ТЗ-36 §3.1–§3.4).

    Даты ядра — ISO UTC ``%Y-%m-%d %H:%M:%S``, поэтому сравнение строк
    лексикографически корректно. Пустая/неизвестная дата окном не
    подтверждается: материал выпадает (свежесть не выдумывается).
    """
    return bool(published_at) and str(published_at)[:19] >= cutoff


def _parse_published(published_at) -> datetime | None:
    """Разобрать дату публикации ядра; неразобранная/пустая → ``None``."""
    if not published_at:
        return None
    try:
        return datetime.strptime(str(published_at)[:19],
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_days(published_at) -> int | None:
    """Сколько полных дней назад материал опубликован; ``None`` — дата неизвестна."""
    t = _parse_published(published_at)
    if t is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds() // 86400))


def _age_note(published_at) -> str:
    """Возраст материала для полного отчёта (ТЗ-36 §3.6).

    Дата неизвестна — печатается честное «дата неизвестна», число не выдумывается.
    """
    days = _age_days(published_at)
    return "дата неизвестна" if days is None else f"опубликовано {days} дн. назад"


def _age_note_short(published_at) -> str:
    """Короткий возраст для сводки (ТЗ-36 §3.6): «N дн» или «дата неизвестна»."""
    days = _age_days(published_at)
    return "дата неизвестна" if days is None else f"{days} дн"


# --------------------------------------------------------------------------- #
# Секция 1. YouTube
# --------------------------------------------------------------------------- #
def _row_get(row, key):
    """Значение колонки ``key`` или ``None`` (работает и с dict, и с Row)."""
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(key) if isinstance(row, dict) else None
    return row[key] if key in keys else None


def _x_description_from(conn, summary_ru, title_ru, text_hash, text) -> str:
    """ЕДИНАЯ логика описания X (ТЗ-Tuber §1.1, ТЗ №2 §2.1).

    ``summary_ru`` → ``title_ru`` → кэш переводчика ``report_text`` → живой
    перевод (в боевом режиме) → сырой текст ``_short(text)``. Сырой английский
    текст — только последний фолбэк, чтобы ничего не потерять. Помощник общий
    для разделов 2 (X) и «Новое за сутки», чтобы логика не дублировалась.
    """
    for value in (summary_ru, title_ru):
        if value and str(value).strip():
            return _short(value)
    ru = _ru_description(conn, text_hash, text)
    if ru:
        return _short(ru)
    return _short(text)


def _x_description(conn, row) -> str:
    """Описание X по общему помощнику :func:`_x_description_from` (ТЗ №2 §2.1)."""
    return _x_description_from(conn, _row_get(row, "summary_ru"),
                               _row_get(row, "title_ru"),
                               _row_get(row, "text_hash"), _row_get(row, "text"))


def _x_desc_fields(row) -> tuple[str, str | None, str | None]:
    """Описание X и материал для ОТЛОЖЕННОГО перевода (ТЗ №2 §1.2, §2.2).

    Если есть готовое русское поле (``summary_ru``/``title_ru``) — возвращаем
    описание и ``None``-перевод (платить не за что). Иначе — сырой текст и
    ``(text_hash, text)``: перевод выполнит
    :func:`_apply_deferred_x_descriptions` только для позиций, реально попавших
    в вывод.
    """
    for value in (_row_get(row, "summary_ru"), _row_get(row, "title_ru")):
        if value and str(value).strip():
            return _short(value), None, None
    return _short(_row_get(row, "text")), _row_get(row, "text_hash"), _row_get(row, "text")


def _apply_deferred_x_descriptions(conn, items) -> None:
    """Применить русское описание к отложенным X-позициям (ТЗ №2 §2.1–§2.2).

    Вызывается ПОСЛЕ отбора раздела, поэтому живой перевод платится только за
    позиции, реально попадающие в вывод. Логика — тот же общий помощник
    :func:`_x_description_from`, без дублирования.
    """
    for it in items:
        if it.x_text is None:
            continue
        it.desc = _x_description_from(conn, None, None, it.x_text_hash, it.x_text)
        it.x_text_hash = None
        it.x_text = None


#: Строка шумной деградации (ТЗ-E §F): идёт в stderr, в доставку владельцу не
#: попадает, но остаётся в журнале обёртки (``--- stderr ---``).
RU_DEGRADED_FMT = "RU-DEGRADED: {n} описаний без перевода (нет ключа/кэша)"

#: Уникальные ``content_id`` X-строк, оставшихся без русского описания. Ledger
#: на процесс: сводка и полный отчёт в одном прогоне ``tuber report``
#: пересекаются по строкам, поэтому без дедупликации счётчик удвоился бы, а
#: строка RU-DEGRADED напечаталась бы дважды.
_RU_DEGRADED_IDS: set = set()
_RU_DEGRADED_FLUSHED = False


def _apply_ru_to_viral_x(conn, items) -> int:
    """Русское описание для ВЫБРАННЫХ X-строк «Виральных» (ТЗ-E §D–§F).

    Применяется ПОСЛЕ отбора/кэпа: платим переводом ровно за печатаемые строки.
    Приоритет (через общий :func:`_x_description_from`): ``summary_ru`` →
    ``title_ru`` → кэш/живой перевод ``report_text`` → сырой текст. Результат
    пишется прямо в ``title`` (печатается и сводкой, и полным отчётом).

    Возвращает число строк, у которых русского источника НЕ нашлось и остался
    сырой текст (счётчик деградации). Строки без сырого текста не считаются:
    переводить нечего, «(без текста)» — не английское описание.
    """
    degraded = 0
    for it in items:
        if getattr(it, "platform", None) != "x":
            continue
        has_ru = bool((it.summary_ru or "").strip()
                      or (it.title_ru or "").strip())
        if not has_ru and it.x_text_hash:
            # Кэш/живой перевод отдельным вызовом — нужен только счётчик. При
            # первом попадании запись кэшируется, поэтому повторный вызов
            # внутри ``_x_description_from`` не тянет сеть второй раз.
            has_ru = bool(_ru_description(conn, it.x_text_hash, it.x_text))
        if has_ru or (it.x_text or "").strip():
            # Не затираем title, если материала нет вовсе (нет ни русского
            # поля, ни сырого текста): иначе «(без текста)» - потеря позиции.
            it.title = _x_description_from(conn, it.summary_ru, it.title_ru,
                                           it.x_text_hash, it.x_text)
        if not has_ru and (it.x_text or "").strip():
            degraded += 1
            _RU_DEGRADED_IDS.add(it.content_id)
        it.x_text_hash = None
        it.x_text = None
    return degraded


def _flush_ru_degraded() -> None:
    """Напечатать в stderr РОВНО одну строку деградации за процесс (ТЗ-E §F).

    Печать отложена до завершения интерпретатора, чтобы сводка и полный отчёт
    одного прогона дали одну строку с числом УНИКАЛЬНЫХ нетронутых строк.
    """
    global _RU_DEGRADED_FLUSHED
    if _RU_DEGRADED_FLUSHED or not _RU_DEGRADED_IDS:
        return
    _RU_DEGRADED_FLUSHED = True
    print(RU_DEGRADED_FMT.format(n=len(_RU_DEGRADED_IDS)), file=sys.stderr)


atexit.register(_flush_ru_degraded)


def _x_author_head(row) -> tuple[str, str, bool]:
    """Голова строки X: ``@автор`` и, при расхождении, «в ленте @лента» (ТЗ §5).

    Возвращает ``(head, author_key, mismatch)``. ``author_key`` — ключ кэпа по
    автору. Расхождение — реальный автор (``author_handle``) не совпадает с
    владельцем ленты (``source_handle``), чья ссылка печатается.
    """
    author = (_row_get(row, "author_handle") or "").strip()
    source = (_row_get(row, "source_handle") or "").strip()
    handle = author or source or "i"
    mismatch = bool(author and source and author.lower() != source.lower())
    if mismatch:
        head = f"   @{author} (в ленте @{source}): "
    else:
        head = f"   @{handle}: "
    return head, handle.lower(), mismatch


_YOUTUBE_SQL = """
    SELECT c.id AS content_id, c.external_id, c.title, c.lang, c.published_at,
           c.url, s.id AS source_id, s.handle AS channel_handle,
           s.title AS channel_title,
           m.views, m.views_per_day, m.likes, m.comments, m.captured_at,
           cl.title_ru
    FROM metric_snapshot m
    JOIN content c ON c.id = m.content_id
    JOIN (
        SELECT content_id, MAX(captured_at) AS mc
        FROM metric_snapshot
        WHERE captured_at >= ? AND interval_quality = 'ok'
              AND views_per_day IS NOT NULL
        GROUP BY content_id
    ) t ON t.content_id = m.content_id AND t.mc = m.captured_at
    LEFT JOIN source s ON s.id = c.source_id
    LEFT JOIN classification cl ON cl.content_id = c.id
    WHERE c.platform = 'youtube' AND c.deleted_at IS NULL
      AND c.published_at >= ?
      AND m.interval_quality = 'ok' AND m.views_per_day IS NOT NULL
"""


def _youtube_channel_key(r) -> str:
    """Ключ канала YouTube для кэпа на автора (ТЗ-Tuber §3)."""
    return str(r["source_id"] if r["source_id"] is not None
               else (r["channel_handle"] or r["channel_title"] or "?"))


def _youtube_compact_item(r) -> CompactItem:
    """Пункт сводки YouTube из строки пула кандидатов."""
    title = r["title_ru"] or r["title"] or "(без заголовка)"
    return CompactItem(
        head="   ",
        desc=_short(title),
        tail=(f" — канал {r['channel_title'] or r['channel_handle'] or '?'}"
              f"; просмотры/сутки {_fmt_int(r['views_per_day'])}"
              f", просмотры {_fmt_int(r['views'])}"
              f", лайки {_fmt_int(r['likes'])}"
              f"; {_link(r['url']) or 'нет ссылки'}"),
        content_id=r["content_id"],
        author_key=_youtube_channel_key(r),
    )


def youtube_above_rows(conn, cutoff: str):
    """ВИДЕО окна ВЫШЕ порога показов, отсортированные (ТЗ-Tuber §4).

    Возвращает ``(above, stats)``; ``above`` НЕ обрезан лимитом раздела — это
    широкий пул кандидатов (ТЗ №2 §1.1). Окно — по ДАТЕ ПУБЛИКАЦИИ:
    ``published_at >= cutoff``, чтобы старые мега-ролики не висели в дайджесте.
    """
    rows = conn.execute(_YOUTUBE_SQL, (cutoff, cutoff)).fetchall()

    def is_ru(r) -> bool:
        return (r["lang"] or "").lower().startswith("ru")

    above = [r for r in rows if (r["views"] or 0) >= (
        YOUTUBE_MIN_VIEWS_RU if is_ru(r) else YOUTUBE_MIN_VIEWS)]
    above.sort(key=lambda r: -(r["views_per_day"] or 0))
    return above, {"considered": len(rows), "above": len(above)}


def youtube_items(conn, cutoff: str) -> tuple[list[str], dict]:
    """Пункты секции YouTube (топ по просмотрам/сутки с отсечкой порога).

    Кэп на канал (ТЗ §3.1) применяется после сортировки; при переполнении
    раздел добирается следующими по ``views_per_day``.
    """
    above, stats = youtube_above_rows(conn, cutoff)
    shown = _cap_per_key(above, _youtube_channel_key, max_per_author(),
                         YOUTUBE_LIMIT)
    items = [_youtube_compact_item(r) for r in shown]
    return items, {**stats, "shown": len(shown)}


def youtube_section(conn, cutoff: str) -> tuple[list[str], dict]:
    """Топ по просмотрам/сутки с отсечкой порога показов (50k / 10k ru)."""
    items, stats = youtube_items(conn, cutoff)
    lines = [
        "1. YouTube — топ по просмотрам в сутки за окно.",
        f"   Правило: окно {stats['considered']} видео с замерами; "
        f"выше порога показов {stats.get('above', 0)}; отсечка "
        f"{_fmt_int(YOUTUBE_MIN_VIEWS)} (не-ru) / "
        f"{_fmt_int(YOUTUBE_MIN_VIEWS_RU)} (ru, lang='ru'); "
        "ранжирование по views_per_day убыв.",
    ]
    if not items:
        lines.append("   нет видео, прошедших порог показов за окно "
                     f"(всего с замерами {stats['considered']}, из них выше порога "
                     f"{stats.get('above', 0)}).")
        return lines, stats
    lines.extend(i.text for i in items)
    return lines, stats


# --------------------------------------------------------------------------- #
# Секция 2. X
# --------------------------------------------------------------------------- #
_X_SQL = """
    SELECT c.id, c.external_id, c.published_at, c.text, c.text_hash, c.url,
           c.lang, c.author_handle, s.handle AS source_handle,
           latest.likes, cl.title_ru, cl.summary_ru
    FROM content c
    LEFT JOIN content_latest latest ON latest.content_id = c.id
    LEFT JOIN classification cl ON cl.content_id = c.id
    LEFT JOIN source s ON s.id = c.source_id
    WHERE c.platform = 'x' AND c.deleted_at IS NULL
      AND c.published_at >= ?
      AND COALESCE(c.is_repost, 0) = 0
    ORDER BY latest.likes DESC NULLS LAST
    LIMIT ?
"""


def _x_author_key(r) -> str:
    return _x_author_head(r)[1]


def x_candidate_rows(conn, cutoff: str, limit: int):
    """Широкий пул X-кандидатов (ТЗ №2 §1.1) и общее число постов окна.

    Возвращает ``(rows, total)``; ``rows`` отсортированы по лайкам убыв. и
    обрезаны ``limit``, но НЕ кэпом на автора — кэп применяется после выбора.
    """
    rows = conn.execute(_X_SQL, (cutoff, int(limit))).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM content WHERE platform='x' AND deleted_at IS NULL"
        " AND published_at >= ? AND COALESCE(is_repost,0)=0", (cutoff,)).fetchone()[0]
    return rows, total


def _x_compact_item(r, now: datetime) -> CompactItem:
    """Пункт сводки X из строки пула; перевод откладывается (:func:`_x_desc_fields`)."""
    likes = r["likes"]
    head, key, _mismatch = _x_author_head(r)
    lph = "—"
    pub = r["published_at"]
    if likes is not None and pub:
        try:
            t = datetime.strptime(pub[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            hours = max((now - t).total_seconds() / 3600.0, 1.0)
            lph = _fmt_num(likes / hours)
        except ValueError:
            lph = "—"
    desc, text_hash, text = _x_desc_fields(r)
    item = CompactItem(
        head=head,
        desc=desc,
        tail=(f" — лайки {_fmt_int(likes)}"
              f", лайки/час {lph}; {_link(r['url']) or 'нет ссылки'}"),
        content_id=r["id"],
        author_key=key,
    )
    item.x_text_hash = text_hash
    item.x_text = text
    return item


def _x_items_from_rows(conn, rows, *, apply_ru: bool) -> list[CompactItem]:
    """Построить пункты X из строк пула; при ``apply_ru`` — перевести их (ТЗ §2.2)."""
    now = datetime.now(timezone.utc)
    items = [_x_compact_item(r, now) for r in rows]
    if apply_ru:
        _apply_deferred_x_descriptions(conn, items)
    return items


def x_items(conn, cutoff: str) -> tuple[list[str], dict]:
    """Пункты секции X: без ретвитов, по лайкам и лайкам/час.

    Русское описание — по приоритету (ТЗ §1); автор печатается честно, при
    расхождении с лентой ссылки — с пометкой «в ленте» (ТЗ §5). Кэп на автора
    (ТЗ §3) применяется после ранжирования, добор — следующими по лайкам.
    Перевод выполняется только для позиций раздела (ТЗ №2 §2.2).
    """
    rows, total = x_candidate_rows(conn, cutoff, candidate_pool(X_LIMIT))
    shown = _cap_per_key(rows, _x_author_key, max_per_author(), X_LIMIT)
    items = _x_items_from_rows(conn, shown, apply_ru=True)
    mismatches = sum(1 for r in shown if _x_author_head(r)[2])
    return items, {"considered": total, "above": total, "shown": len(items),
                   "author_mismatch": mismatches}


def x_section(conn, cutoff: str) -> tuple[list[str], dict]:
    """X: без ретвитов, по лайкам и лайкам/час."""
    items, stats = x_items(conn, cutoff)
    lines = [
        "2. X — посты за окно без ретвитов.",
        "   Правило: ретвиты исключены (is_repost=0); ранжирование по лайкам "
        "убыв., в строке — лайки/час от момента публикации.",
    ]
    if not items:
        lines.append("   нет постов за окно.")
        return lines, stats
    lines.extend(i.text for i in items)
    return lines, stats


# --------------------------------------------------------------------------- #
# Секция 3. Telegram
# --------------------------------------------------------------------------- #
def telegram_items(conn, cutoff: str) -> tuple[list[str], dict]:
    """Пункты секции Telegram; ранжирование ВНУТРИ канала (значимость несравнима)."""
    sql = """
        SELECT c.id, c.external_id, c.published_at, c.text, c.url,
               s.handle AS channel, s.title AS channel_title,
               cl.views, cl.forwards, sc.significance, sc.axes_json
        FROM content c
        LEFT JOIN source s ON s.id = c.source_id
        LEFT JOIN content_latest cl ON cl.content_id = c.id
        LEFT JOIN score sc ON sc.content_id = c.id
             AND sc.computed_at = (
                 SELECT MAX(computed_at) FROM score WHERE content_id = c.id)
        WHERE c.platform = 'telegram' AND c.deleted_at IS NULL
          AND c.published_at >= ?
          AND s.status = 'active'
        ORDER BY s.handle ASC, sc.significance DESC NULLS LAST
    """
    rows = conn.execute(sql, (cutoff,)).fetchall()

    # Сначала выбираем КАНАЛЫ (по лучшей внутриканальной значимости), затем
    # внутри выбранных каналов берём верхние посты. Так секция остаётся
    # обозримой, а ранжирование не выходит за пределы канала.
    best: dict[str, float] = {}
    for r in rows:
        ch = r["channel"] or "(без канала)"
        sig = r["significance"]
        key = float(sig) if isinstance(sig, (int, float)) else float("-inf")
        if ch not in best or key > best[ch]:
            best[ch] = key
    selected = [
        ch for ch, _ in sorted(
            best.items(), key=lambda kv: (-(kv[1] if kv[1] != float("-inf") else -1e18), kv[0]))
    ][:TELEGRAM_CHANNEL_LIMIT]

    channels: dict[str, list] = {}
    per_channel = min(TELEGRAM_PER_CHANNEL, max_per_author())
    for r in rows:
        ch = r["channel"] or "(без канала)"
        if ch not in selected:
            continue
        bucket = channels.setdefault(ch, [])
        if len(bucket) < per_channel:
            bucket.append(r)

    items = []
    for ch in sorted(channels):
        for r in channels[ch]:
            link = _link(r["url"])
            eng_channel = None
            try:
                eng_channel = json.loads(r["axes_json"] or "{}").get("eng_channel")
            except (ValueError, TypeError):
                eng_channel = None
            items.append(CompactItem(
                head=f"   [{r['channel_title'] or ch}] ",
                desc=_short(r["text"]),
                tail=(f" — просмотры {_fmt_int(r['views'])}"
                      f", forwards {_fmt_int(r['forwards'])}"
                      f", significance {_fmt_num(r['significance'], 3)}"
                      f", eng_channel {_fmt_num(eng_channel, 3)}; {link or 'нет ссылки'}"),
                content_id=r["id"],
                author_key=ch,
            ))
    stats = {"considered": len(rows), "channels": len(channels),
             "selected": len(selected), "have": len(best)}
    return items, stats


def telegram_section(conn, cutoff: str) -> tuple[list[str], dict]:
    """Telegram: свежие посты; ранжирование ВНУТРИ канала (значимость несравнима)."""
    items, stats = telegram_items(conn, cutoff)
    lines = [
        "3. Telegram — свежие посты за окно.",
        "   Правило: significance НЕсравнима между каналами (у каждого своя база "
        "нормировки), поэтому сортировка ВНУТРИ канала по significance; каналы "
        "по алфавиту; eng_channel — нормированная на канал вовлечённость.",
    ]
    if not items:
        lines.append("   нет постов за окно.")
        return lines, {"considered": 0, "channels": 0}
    if stats["have"] > stats["selected"]:
        lines.append(
            f"   показаны {stats['selected']} каналов из {stats['have']} "
            "(отобраны по лучшей внутриканальной значимости).")
    lines.extend(i.text for i in items)
    return lines, {"considered": stats["considered"], "channels": stats["channels"]}


# --------------------------------------------------------------------------- #
# Секция 4. Сквозной сюжет
# --------------------------------------------------------------------------- #
def cross_story_section(conn) -> tuple[list[str], dict]:
    """Сюжеты, у которых материалы есть на РАЗНЫХ платформах."""
    sql = """
        SELECT st.id AS story_id, st.topic, st.title,
               c.platform, c.external_id, c.url, c.published_at,
               c.author_handle, s.handle AS source_handle
        FROM story_member sm
        JOIN content c ON c.id = sm.content_id
        JOIN story st ON st.id = sm.story_id
        LEFT JOIN source s ON s.id = c.source_id
        WHERE sm.story_id IN (
            SELECT sm2.story_id
            FROM story_member sm2
            JOIN content c2 ON c2.id = sm2.content_id
            GROUP BY sm2.story_id
            HAVING COUNT(DISTINCT c2.platform) >= 2)
        ORDER BY st.id, c.platform
    """
    rows = conn.execute(sql).fetchall()
    stories: dict[int, dict] = {}
    for r in rows:
        entry = stories.setdefault(r["story_id"], {"topic": r["topic"],
                                                    "title": r["title"],
                                                    "platforms": {}})
        entry["platforms"].setdefault(r["platform"], []).append(r)

    lines = [
        "4. Сквозной сюжет — материалы одного сюжета с РАЗНЫХ платформ.",
        "   Правило: сюжет попадает сюда, если в story_member есть контент "
        "минимум двух разных platform; платформы идут по алфавиту.",
    ]
    if not stories:
        lines.append("   нет сюжетов, объединяющих материалы разных платформ.")
        return lines, {"stories": 0, "members": len(rows)}

    for sid in sorted(stories):
        entry = stories[sid]
        label = entry["title"] or entry["topic"] or f"сюжет {sid}"
        platforms = ", ".join(sorted(entry["platforms"]))
        lines.append(f"   Сюжет {sid} [{label}] — платформы: {platforms}")
        for platform in sorted(entry["platforms"]):
            for r in entry["platforms"][platform]:
                link = _link(r["url"])
                lines.append(f"     {platform}: {link}")
    return lines, {"stories": len(stories), "members": len(rows)}


# --------------------------------------------------------------------------- #
# Секция 5. Виральные: охват против своей аудитории (ТЗ-35)
# --------------------------------------------------------------------------- #
def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ViralRow:
    """Материал-кандидат «Виральных» с числами для строки полного отчёта.

    Поля заполняются ровно тем, что есть в базе: нет подписчиков — ``coverage``
    остаётся ``None`` (охват не выдумывается), нет базы канала — ``ratio``
    остаётся ``None``. ``channel_key`` — технический ключ канала/автора для
    ограничения «не больше N на канал» (id источника либо handle).
    """

    content_id: int
    title: str
    channel: str
    channel_key: str
    platform: str
    url: str | None = None
    views: int | None = None
    views_per_day: int | None = None
    subs: int | None = None
    coverage: float | None = None
    ratio: float | None = None
    norm: float | None = None
    likes: int | None = None
    replies: int | None = None
    #: Медиана лайков автора (блок 4) — печатается рядом с кратностью.
    likes_median: float | None = None
    #: Сколько материалов канала/автора легло в норму (ТЗ-36 §3.5): «норма
    #: канала» печатается только при достаточном числе, иначе «мало данных».
    norm_n: int | None = None
    topic: str | None = None
    lang: str | None = None
    #: Дата публикации материала — из неё считается возраст (ТЗ-36 §3.6).
    published_at: str | None = None
    #: Русский заголовок классификации X (ТЗ-E): приоритетный источник описания.
    title_ru: str | None = None
    #: Русская сводка классификации X (ТЗ-E): высший приоритет описания.
    summary_ru: str | None = None
    #: Хэш сырого текста X — ключ кэша ``report_text`` для отложенного перевода
    #: (ТЗ-E). У не-X строк остаётся ``None``, чтобы не платить за перевод впустую.
    x_text_hash: str | None = None
    #: Сырой текст X для отложенного перевода (ТЗ-E); ``None`` у не-X строк.
    x_text: str | None = None


def _norm_map(rows, key_fn, value_fn) -> dict:
    """Медиана ``value_fn`` и число материалов по группам ``key_fn``.

    Возвращает ``{ключ: (медиана, сколько значений)}``. Число нужно, чтобы не
    печатать «норму канала» по 1–2 материалам (ТЗ-36 §3.5): там норма равна
    самому видео, а кратность всегда 1.0.
    """
    groups: dict = {}
    for r in rows:
        value = _as_float(value_fn(r))
        if value is None:
            continue
        groups.setdefault(key_fn(r), []).append(value)
    return {key: (statistics.median(vals), len(vals))
            for key, vals in groups.items() if vals}


def _cap_per_key(rows: list, key_fn, per_key: int, limit: int) -> list:
    """Не больше ``per_key`` позиций на ключ и не больше ``limit`` всего."""
    counts: dict = {}
    out: list = []
    for r in rows:
        key = key_fn(r)
        if counts.get(key, 0) >= per_key:
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _cap_items_per_key(items: list, per_key: int, limit: int) -> list:
    """Кэп на автора (``CompactItem.author_key``) + обрезка до ``limit`` (ТЗ №2 §1.2).

    Применяется к пунктам ПОСЛЕ исключения показанных: раздел добирается
    следующими по ранжированию из широкого пула, а не остаётся полупустым.
    """
    counts: dict = {}
    out: list = []
    for it in items:
        key = it.author_key
        if key is not None:
            if counts.get(key, 0) >= per_key:
                continue
            counts[key] = counts.get(key, 0) + 1
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _viral_key(row) -> object:
    return row["source_id"] if row["source_id"] is not None else (
        row["channel_handle"] or row["channel_title"] or "(без канала)")


def _viral_youtube_pool(conn, cutoff: str) -> list:
    """YouTube-видео окна с последним добротным замером (общий пул блоков 1–3).

    Окно — как в секции 1: последний добротный замер (``interval_quality='ok'``,
    есть ``views_per_day``) снят внутри окна. Фильтр по ``published_at`` здесь
    НЕ применяется намеренно: пул — знаменатель статистики «рассмотрено», а
    свежесть (ТЗ-36 §3.1–§3.4) накладывается при отборе блоков. Подписчики
    берутся из ``source.subs`` как есть; пусто/NULL остаётся NULL — отбор по
    охвату такие материалы честно отбрасывает.
    """
    sql = """
        SELECT c.id AS content_id, c.title, c.lang, c.published_at, c.url,
               s.id AS source_id, s.handle AS channel_handle,
               s.title AS channel_title, s.subs AS subs,
               m.views, m.views_per_day, cl.topic AS topic, cl.title_ru
        FROM metric_snapshot m
        JOIN content c ON c.id = m.content_id
        JOIN (
            SELECT content_id, MAX(captured_at) AS mc
            FROM metric_snapshot
            WHERE captured_at >= ? AND interval_quality = 'ok'
                  AND views_per_day IS NOT NULL
            GROUP BY content_id
        ) t ON t.content_id = m.content_id AND t.mc = m.captured_at
        LEFT JOIN source s ON s.id = c.source_id
        LEFT JOIN classification cl ON cl.content_id = c.id
        WHERE c.platform = 'youtube' AND c.deleted_at IS NULL
          AND m.interval_quality = 'ok' AND m.views_per_day IS NOT NULL
    """
    return conn.execute(sql, (cutoff,)).fetchall()


def select_viral_youtube(pool, *, fun: bool, norm: dict,
                         cutoff: str) -> tuple[list[ViralRow], dict]:
    """Блоки 1–2: охват = просмотры ÷ подписчики СВОЕГО канала.

    ``fun=False`` — полезное (тема из :data:`PRODUCT_TOPICS`), ``fun=True`` —
    всё остальное: фермы, нарезки, «прочее», пустая тема (блок 2). Свежесть
    (ТЗ-36 §3.1): ``published_at`` материала — внутри окна отчёта. Полы:
    подписчиков ≥ :data:`VIRAL_YT_MIN_SUBS`, просмотров ≥
    :data:`VIRAL_YT_MIN_VIEWS`, охват ≥ :data:`VIRAL_YT_MIN_COVERAGE`.
    Подписчиков нет/0 — материал выпадает. ``norm`` — ``{source_id: (медиана,
    сколько материалов)}``; норма канала печатается только при
    :data:`VIRAL_YT_MIN_NORM_SAMPLE` материалах (ТЗ-36 §3.5).
    """
    def is_product(row) -> bool:
        return (row["topic"] or "") in PRODUCT_TOPICS

    passed: list[ViralRow] = []
    for r in pool:
        if not _in_window(r["published_at"], cutoff):
            continue  # свежесть: материал вне окна в блок не берём
        if fun == is_product(r):
            continue  # блок 1 — только полезные темы, блок 2 — всё остальное
        subs = r["subs"]
        views = int(r["views"] or 0)
        if not subs or subs <= 0:
            continue  # охват без подписчиков не считается (никаких оценок)
        if subs < VIRAL_YT_MIN_SUBS or views < VIRAL_YT_MIN_VIEWS:
            continue
        coverage = views / subs
        if coverage < VIRAL_YT_MIN_COVERAGE:
            continue
        key = _viral_key(r)
        pair = norm.get(r["source_id"]) if r["source_id"] is not None else None
        base, norm_n = pair if pair else (None, 0)
        passed.append(ViralRow(
            content_id=r["content_id"],
            title=r["title_ru"] or r["title"] or "(без заголовка)",
            channel=r["channel_title"] or r["channel_handle"] or "?",
            channel_key=str(key),
            platform="youtube",
            url=r["url"],
            views=views,
            views_per_day=int(r["views_per_day"] or 0),
            subs=int(subs),
            coverage=coverage,
            ratio=(views / base) if base else None,
            norm=base,
            norm_n=norm_n,
            topic=r["topic"],
            lang=r["lang"],
            published_at=r["published_at"],
        ))
    passed.sort(key=lambda it: (-(it.coverage or 0), -(it.views or 0)))
    limit = VIRAL_YT_FUN_LIMIT if fun else VIRAL_YT_LIMIT
    shown = _cap_per_key(passed, lambda it: it.channel_key, VIRAL_YT_PER_CHANNEL,
                         limit)
    stats = {"considered": len(pool), "passed": len(passed), "shown": len(shown)}
    return shown, stats


def _viral_x_pool(conn, cutoff: str) -> list:
    """X-посты окна без ретвитов с лайками/ответами и темой классификации."""
    sql = """
        SELECT c.id AS content_id, c.text AS title, c.url, c.author_handle,
               c.published_at, s.handle AS source_handle,
               cl.topic AS topic, c.lang AS lang,
               cl.title_ru AS title_ru, cl.summary_ru AS summary_ru,
               c.text_hash AS text_hash, c.text AS raw_text,
               l.likes, l.replies
        FROM content c
        LEFT JOIN content_latest l ON l.content_id = c.id
        LEFT JOIN source s ON s.id = c.source_id
        LEFT JOIN classification cl ON cl.content_id = c.id
        WHERE c.platform = 'x' AND c.deleted_at IS NULL
          AND c.published_at >= ?
          AND COALESCE(c.is_repost, 0) = 0
    """
    return conn.execute(sql, (cutoff,)).fetchall()


def _viral_tg_pool(conn, cutoff: str) -> list:
    """Telegram-посты окна с просмотрами, forwards и базой нормы канала."""
    sql = """
        SELECT c.id AS content_id, c.text AS title, c.url, c.published_at,
               s.id AS source_id, s.handle AS channel_handle,
               s.title AS channel_title, cl.topic AS topic, c.lang AS lang,
               l.views, l.forwards, b.median_views
        FROM content c
        LEFT JOIN source s ON s.id = c.source_id
        LEFT JOIN content_latest l ON l.content_id = c.id
        LEFT JOIN classification cl ON cl.content_id = c.id
        LEFT JOIN source_baseline b ON b.source_id = c.source_id
        WHERE c.platform = 'telegram' AND c.deleted_at IS NULL
          AND c.published_at >= ?
    """
    return conn.execute(sql, (cutoff,)).fetchall()


def viral_product_items(yt_pool, x_pool, tg_pool, cutoff: str,
                        yt_min_coverage: float | None = None) -> dict:
    """Блок 3: фичи, релизы и стартапы — три подсписка с РАЗНЫМИ полами.

    YouTube — просмотров ≥ :data:`VIRAL_PRODUCT_YT_MIN_VIEWS` по просмотрам;
    X — лайков ≥ :data:`VIRAL_PRODUCT_X_MIN_LIKES` по лайкам; Telegram —
    просмотров ≥ :data:`VIRAL_PRODUCT_TG_MIN_VIEWS` по просмотрам. Тема — из
    :data:`VIRAL_PRODUCT_TOPICS` (оба варианта ярлыков), не больше 2 на канал.
    Все три подсписка фильтруются по свежести внутри окна отчёта (ТЗ-36 §3.3).

    ``yt_min_coverage`` — необязательный ДОПОЛНИТЕЛЬНЫЙ пол по охвату только для
    подсписка 3.1 (ТЗ-37 §2.1): сводке нужен двойной пол «просмотров ≥ 50 000 И
    охват ≥ 3.0». По умолчанию ``None`` — поведение полного отчёта не меняется.
    """
    def product(row) -> bool:
        return (row["topic"] or "") in VIRAL_PRODUCT_TOPICS

    def fresh(row) -> bool:
        return _in_window(row["published_at"], cutoff)

    def yt_key(row):
        return str(row["source_id"] if row["source_id"] is not None
                   else (row["channel_handle"] or "(без канала)"))

    out: dict = {}
    # 3.1. YouTube
    cand = []
    for r in yt_pool:
        if not fresh(r) or not product(r):
            continue
        views = int(r["views"] or 0)
        if views < VIRAL_PRODUCT_YT_MIN_VIEWS:
            continue
        subs = r["subs"]
        coverage = (views / subs) if subs and subs > 0 else None
        if yt_min_coverage is not None and (coverage is None
                                            or coverage < yt_min_coverage):
            # Двойной пол сводки (ТЗ-37 §2.1): просмотров ≥ 50 000 И охват ≥ 3.0.
            continue
        cand.append(ViralRow(
            content_id=r["content_id"],
            title=r["title_ru"] or r["title"] or "(без заголовка)",
            channel=r["channel_title"] or r["channel_handle"] or "?",
            channel_key=yt_key(r), platform="youtube", url=r["url"],
            views=views, views_per_day=int(r["views_per_day"] or 0),
            subs=int(subs) if subs else None, coverage=coverage,
            topic=r["topic"], lang=r["lang"],
            published_at=r["published_at"]))
    cand.sort(key=lambda it: -(it.views or 0))
    key_fn = lambda it: it.channel_key  # noqa: E731 — короткий ключ канала
    shown = _cap_per_key(cand, key_fn, VIRAL_PRODUCT_PER_CHANNEL,
                         VIRAL_PRODUCT_LIMIT)
    if yt_min_coverage is None:
        # Полный отчёт обязан быть НАДмножеством сводки (ссылки сводки сверяются
        # с полным отчётом). Сводка с ТЗ-Tuber §7 считает блок 3.1 своим двойным
        # полом (просмотров ≥ 50 000 И охват ≥ 3.0), поэтому полный отчёт
        # показывает сначала двойной пол, затем остальной одиночный — так
        # позиция сводки всегда находится и в полном отчёте.
        double = [it for it in cand if it.coverage is not None
                  and it.coverage >= VIRAL_YT_MIN_COVERAGE]
        double_shown = _cap_per_key(double, key_fn, VIRAL_PRODUCT_PER_CHANNEL,
                                    VIRAL_PRODUCT_LIMIT)
        seen_ids = {it.content_id for it in double_shown}
        rest = [it for it in cand if it.content_id not in seen_ids]
        shown = _cap_per_key(double_shown + rest, key_fn,
                             VIRAL_PRODUCT_PER_CHANNEL, VIRAL_PRODUCT_LIMIT)
    out["youtube"] = (shown, {"considered": len(yt_pool), "passed": len(cand),
                              "shown": len(shown)})

    # 3.2. X
    cand = []
    for r in x_pool:
        if not fresh(r) or not product(r):
            continue
        likes = int(r["likes"] or 0)
        if likes < VIRAL_PRODUCT_X_MIN_LIKES:
            continue
        key = r["author_handle"] or r["source_handle"] or "(без автора)"
        cand.append(ViralRow(
            content_id=r["content_id"], title=r["title"] or "(без текста)",
            channel=key, channel_key=key, platform="x", url=r["url"],
            likes=likes, replies=r["replies"], topic=r["topic"], lang=r["lang"],
            published_at=r["published_at"],
            title_ru=r["title_ru"], summary_ru=r["summary_ru"],
            x_text_hash=r["text_hash"], x_text=r["raw_text"] or r["title"]))
    cand.sort(key=lambda it: -(it.likes or 0))
    shown = _cap_per_key(cand, lambda it: it.channel_key,
                         VIRAL_PRODUCT_PER_CHANNEL, VIRAL_PRODUCT_LIMIT)
    out["x"] = (shown, {"considered": len(x_pool), "passed": len(cand),
                        "shown": len(shown)})

    # 3.3. Telegram
    cand = []
    for r in tg_pool:
        if not fresh(r) or not product(r):
            continue
        views = int(r["views"] or 0)
        if views < VIRAL_PRODUCT_TG_MIN_VIEWS:
            continue
        key = r["channel_handle"] or r["channel_title"] or "(без канала)"
        cand.append(ViralRow(
            content_id=r["content_id"], title=r["title"] or "(без текста)",
            channel=r["channel_title"] or key, channel_key=str(key),
            platform="telegram", url=r["url"], views=views,
            topic=r["topic"], lang=r["lang"], published_at=r["published_at"]))
    cand.sort(key=lambda it: -(it.views or 0))
    shown = _cap_per_key(cand, lambda it: it.channel_key,
                         VIRAL_PRODUCT_PER_CHANNEL, VIRAL_PRODUCT_LIMIT)
    out["telegram"] = (shown, {"considered": len(tg_pool), "passed": len(cand),
                               "shown": len(shown)})
    return out


def viral_x_items(x_pool, cutoff: str) -> tuple[list[ViralRow], dict]:
    """Блок 4: X — кратность лайков к МЕДИАНЕ СВОЕГО автора (подписчиков нет).

    Полы: лайков ≥ :data:`VIRAL_X_MIN_LIKES`, у автора ≥
    :data:`VIRAL_X_MIN_AUTHOR_POSTS` постов за :data:`VIRAL_X_AUTHOR_WINDOW_DAYS`
    дней, кратность ≥ :data:`VIRAL_X_MIN_RATIO`. Сам материал — из окна отчёта.
    """
    by_author: dict = {}
    for r in x_pool:
        key = r["author_handle"] or r["source_handle"] or "(без автора)"
        by_author.setdefault(key, []).append(r)

    passed: list[ViralRow] = []
    for key, group in by_author.items():
        if len(group) < VIRAL_X_MIN_AUTHOR_POSTS:
            continue
        likes_all = [_as_float(r["likes"]) for r in group]
        likes_all = [v for v in likes_all if v is not None]
        if not likes_all:
            continue
        median = statistics.median(likes_all)
        if median < VIRAL_X_MIN_AUTHOR_MEDIAN:
            # Нижняя граница «нормальности» автора (ТЗ-36 §3.4): у автора без
            # реакций медиана близка к 0, и мусорный всплеск даёт кратность
            # в тысячи раз. Такой всплеск остаётся только в статистике блока.
            continue
        if median <= 0:
            continue  # кратность к нулевой базе не определена
        for r in group:
            likes = _as_float(r["likes"])
            if likes is None or likes < VIRAL_X_MIN_LIKES:
                continue
            if not _in_window(r["published_at"], cutoff):
                continue  # норма автора — 30 дней, но сам материал — окно отчёта
            ratio = likes / median
            if ratio < VIRAL_X_MIN_RATIO:
                continue
            passed.append(ViralRow(
                content_id=r["content_id"], title=r["title"] or "(без текста)",
                channel=key, channel_key=key, platform="x", url=r["url"],
                likes=int(likes), replies=r["replies"], ratio=ratio, norm=median,
                likes_median=median, topic=r["topic"], lang=r["lang"],
                published_at=r["published_at"],
                title_ru=r["title_ru"], summary_ru=r["summary_ru"],
                x_text_hash=r["text_hash"], x_text=r["raw_text"] or r["title"]))
    passed.sort(key=lambda it: (-(it.ratio or 0), -(it.likes or 0)))
    shown = _cap_per_key(passed, lambda it: it.channel_key, VIRAL_X_PER_AUTHOR,
                         VIRAL_X_LIMIT)
    stats = {"considered": len(x_pool), "passed": len(passed), "shown": len(shown)}
    return shown, stats


def viral_telegram_items(tg_pool, cutoff: str) -> tuple[list[ViralRow], dict]:
    """Блок 5: Telegram — просмотры против базы канала (``source_baseline``).

    Полы: просмотров ≥ :data:`VIRAL_TG_MIN_VIEWS`, свежесть (публикация внутри
    окна отчёта), база канала есть, просмотры ≥ :data:`VIRAL_TG_MIN_RATIO` ×
    базы. Нет базы — материал выпадает (норму по 1–2 постам не выдумываем).
    """
    passed: list[ViralRow] = []
    for r in tg_pool:
        if not _in_window(r["published_at"], cutoff):
            continue
        views = _as_float(r["views"])
        base = _as_float(r["median_views"])
        if views is None or base is None or base <= 0:
            continue
        if views < VIRAL_TG_MIN_VIEWS:
            continue
        ratio = views / base
        if ratio < VIRAL_TG_MIN_RATIO:
            continue
        key = r["channel_handle"] or r["channel_title"] or "(без канала)"
        passed.append(ViralRow(
            content_id=r["content_id"], title=r["title"] or "(без текста)",
            channel=r["channel_title"] or key, channel_key=str(key),
            platform="telegram", url=r["url"], views=int(views),
            ratio=ratio, norm=base, topic=r["topic"], lang=r["lang"],
            published_at=r["published_at"]))
    passed.sort(key=lambda it: (-(it.ratio or 0), -(it.views or 0)))
    shown = _cap_per_key(passed, lambda it: it.channel_key, VIRAL_TG_PER_CHANNEL,
                         VIRAL_TG_LIMIT)
    stats = {"considered": len(tg_pool), "passed": len(passed), "shown": len(shown)}
    return shown, stats


def _stats_line(label: str, st: dict) -> str:
    return (f"   {label}: рассмотрено {st['considered']}, прошло полы "
            f"{st['passed']}, показано {st['shown']}.")


def _viral_yt_line(it: ViralRow) -> str:
    # Норма канала печатается только при ≥ VIRAL_YT_MIN_NORM_SAMPLE материалах
    # (ТЗ-36 §3.5): по 1–2 материалам «норма» равна самому видео, кратность
    # всегда 1.0 — это шум.
    if it.norm is not None and (it.norm_n or 0) >= VIRAL_YT_MIN_NORM_SAMPLE:
        norm = (f"кратность к норме канала {_fmt_num(it.ratio, 1)} "
                f"(норма {_fmt_int(it.norm)})")
    else:
        norm = "норма: мало данных"
    return (f"   {_short(it.title)} — канал {it.channel}; подписчиков "
            f"{_fmt_int(it.subs)}, просмотров {_fmt_int(it.views)}, в сутки "
            f"{_fmt_int(it.views_per_day)}, охват на подписчика "
            f"{_fmt_num(it.coverage, 2)}, {norm}; {_age_note(it.published_at)}; "
            f"тема: {it.topic or 'без темы'}, язык {it.lang or '?'}; "
            f"{_link(it.url)}")


def _viral_product_yt_line(it: ViralRow) -> str:
    coverage = (f", охват на подписчика {_fmt_num(it.coverage, 2)}"
                if it.coverage is not None else "")
    return (f"   {_short(it.title)} — канал {it.channel}; просмотров "
            f"{_fmt_int(it.views)}, в сутки {_fmt_int(it.views_per_day)}"
            f"{coverage}; {_age_note(it.published_at)}; тема: "
            f"{it.topic or 'без темы'}, язык {it.lang or '?'}; {_link(it.url)}")


def _viral_product_x_line(it: ViralRow) -> str:
    return (f"   @{it.channel}: {_short(it.title)} — лайки {_fmt_int(it.likes)}"
            f", ответы {_fmt_int(it.replies)}; {_age_note(it.published_at)}; "
            f"тема: {it.topic or 'без темы'}, язык {it.lang or '?'}; "
            f"{_link(it.url)}")


def _viral_product_tg_line(it: ViralRow) -> str:
    return (f"   [{it.channel}] {_short(it.title)} — просмотры "
            f"{_fmt_int(it.views)}; {_age_note(it.published_at)}; тема: "
            f"{it.topic or 'без темы'}, язык {it.lang or '?'}; {_link(it.url)}")


def _viral_x_line(it: ViralRow) -> str:
    return (f"   @{it.channel}: {_short(it.title)} — лайки {_fmt_int(it.likes)}"
            f", медиана автора {_fmt_int(it.likes_median)}, кратность "
            f"{_fmt_num(it.ratio, 1)}, ответы {_fmt_int(it.replies)}; "
            f"{_age_note(it.published_at)}; {_link(it.url)}")


def _viral_tg_line(it: ViralRow) -> str:
    return (f"   [{it.channel}] {_short(it.title)} — просмотры "
            f"{_fmt_int(it.views)}, база канала {_fmt_int(it.norm)}, кратность "
            f"{_fmt_num(it.ratio, 1)}; {_age_note(it.published_at)}; "
            f"{_link(it.url)}")


def _blk(lines: list[str], rows: list[ViralRow], render, empty: str) -> None:
    if not rows:
        lines.append(empty)
        return
    lines.extend(render(it) for it in rows)


def viral_section(conn, cutoff: str, days: int = 10) -> tuple[list[str], dict]:
    """Секция 5 полного отчёта: виральные по охвату против своей аудитории."""
    yt_pool = _viral_youtube_pool(conn, cutoff)
    # Норма канала считается по СВЕЖИМ материалам окна (ТЗ-36 §3.1, §3.5):
    # старые видео канала к норме окна не относятся.
    fresh_yt = [r for r in yt_pool if _in_window(r["published_at"], cutoff)]
    norm = _norm_map(fresh_yt, lambda r: r["source_id"], lambda r: r["views"])
    b1, b1_stats = select_viral_youtube(yt_pool, fun=False, norm=norm,
                                        cutoff=cutoff)
    b2, b2_stats = select_viral_youtube(yt_pool, fun=True, norm=norm,
                                        cutoff=cutoff)
    x_cutoff, _ = _now_iso(max(days, VIRAL_X_AUTHOR_WINDOW_DAYS))
    x_pool = _viral_x_pool(conn, x_cutoff)
    tg_pool = _viral_tg_pool(conn, cutoff)
    product = viral_product_items(yt_pool, x_pool, tg_pool, cutoff)
    b4, b4_stats = viral_x_items(x_pool, cutoff)
    b5, b5_stats = viral_telegram_items(tg_pool, cutoff)

    # Русское описание X (ТЗ-E §E–§F): только для ВЫБРАННЫХ, печатаемых строк
    # (после полов и кэпа). Сводка и полный отчёт в одном прогоне дедуплицируют
    # счётчик по content_id (ledger в _apply_ru_to_viral_x).
    _apply_ru_to_viral_x(conn, product["x"][0])
    _apply_ru_to_viral_x(conn, b4)

    stats = {"block1": b1_stats, "block2": b2_stats,
             "block3": {k: v[1] for k, v in product.items()},
             "block4": b4_stats, "block5": b5_stats}

    lines = [
        "5. Виральные — охват против своей аудитории.",
        "   Метрика: охват = просмотры ÷ подписчики СВОЕГО канала (не абсолютные "
        "просмотры: пост малого канала с большим охватом не проигрывает "
        "топ-аккаунтам). Кратность к норме канала — просмотры ÷ медиана "
        "просмотров канала за окно; норма печатается только при ≥ "
        f"{VIRAL_YT_MIN_NORM_SAMPLE} материалах канала в окне.",
        "   Свежесть: во ВСЕХ блоках материал должен быть опубликован внутри "
        "окна отчёта; в строке печатается возраст материала.",
        "   Блок 1. YouTube — полезное (подписчиков ≥ "
        f"{_fmt_int(VIRAL_YT_MIN_SUBS)}, просмотров ≥ "
        f"{_fmt_int(VIRAL_YT_MIN_VIEWS)}, охват ≥ "
        f"{_fmt_num(VIRAL_YT_MIN_COVERAGE, 1)}; тема только из "
        f"PRODUCT_TOPICS ({len(PRODUCT_TOPICS)} полезных ярлыков); "
        f"не более {VIRAL_YT_PER_CHANNEL} на канал).",
        _stats_line("Блок 1", b1_stats),
    ]
    _blk(lines, b1, _viral_yt_line,
         "      нет полезных видео, прошедших полы (или у каналов нет данных "
         "о подписчиках).")
    lines.append(
        "   Блок 2. YouTube — развлекательное и фермы (те же полы; темы ВНЕ "
        "PRODUCT_TOPICS: «прочее», пустая тема, обучение, ИИ-инструменты для "
        f"обычных людей и прочие фермы; не более {VIRAL_YT_PER_CHANNEL} "
        "на канал).")
    lines.append(_stats_line("Блок 2", b2_stats))
    _blk(lines, b2, _viral_yt_line,
         "      нет развлекательных видео, прошедших полы.")
    lines.append(
        "   Блок 3. Новые продукты: фичи, релизы, стартапы (темы: запуски и "
        "анонсы, стартапы и бизнес, модели и релизы / релизы моделей, "
        "инструменты разработчика, инвестиции и раунды; все подсписки — "
        f"только свежее внутри окна; не более "
        f"{VIRAL_PRODUCT_PER_CHANNEL} на канал).")
    p_yt, p_yt_st = product["youtube"]
    lines.append(f"      3.1. YouTube: просмотров ≥ "
                 f"{_fmt_int(VIRAL_PRODUCT_YT_MIN_VIEWS)}, по просмотрам убыв.")
    lines.append("   " + _stats_line("3.1", p_yt_st).strip())
    _blk(lines, p_yt, _viral_product_yt_line,
         "         нет видео по темам продуктов, прошедших пол.")
    p_x, p_x_st = product["x"]
    lines.append(f"      3.2. X: лайков ≥ {_fmt_int(VIRAL_PRODUCT_X_MIN_LIKES)}, "
                 "по лайкам убыв.")
    lines.append("   " + _stats_line("3.2", p_x_st).strip())
    _blk(lines, p_x, _viral_product_x_line,
         "         нет постов по темам продуктов, прошедших пол.")
    p_tg, p_tg_st = product["telegram"]
    lines.append(f"      3.3. Telegram: просмотров ≥ "
                 f"{_fmt_int(VIRAL_PRODUCT_TG_MIN_VIEWS)}, по просмотрам убыв.")
    lines.append("   " + _stats_line("3.3", p_tg_st).strip())
    _blk(lines, p_tg, _viral_product_tg_line,
         "         нет постов по темам продуктов, прошедших пол "
         "(классификация Telegram может быть не наполнена).")
    lines.append(
        "   Блок 4. X — истории с реакциями (лайков ≥ "
        f"{_fmt_int(VIRAL_X_MIN_LIKES)}, у автора ≥ {VIRAL_X_MIN_AUTHOR_POSTS} "
        f"постов за {VIRAL_X_AUTHOR_WINDOW_DAYS} дней, медиана лайков автора ≥ "
        f"{_fmt_int(VIRAL_X_MIN_AUTHOR_MEDIAN)}, кратность к медиане "
        f"автора ≥ {_fmt_num(VIRAL_X_MIN_RATIO, 1)}; не более "
        f"{VIRAL_X_PER_AUTHOR} на автора).")
    lines.append(
        "      Оговорка: подписчики X в базе НЕ собираются (замер — в подвале), "
        "поэтому охват на подписчика для X невозможен: виральность X измеряется "
        "только относительно нормы автора.")
    lines.append(_stats_line("Блок 4", b4_stats))
    _blk(lines, b4, _viral_x_line,
         "      нет постов X с кратностью к норме автора.")
    lines.append(
        "   Блок 5. Telegram — выше своей нормы (просмотров ≥ "
        f"{_fmt_int(VIRAL_TG_MIN_VIEWS)}, база канала есть, просмотры ≥ "
        f"{_fmt_num(VIRAL_TG_MIN_RATIO, 1)} × source_baseline.median_views, "
        "свежее внутри окна; "
        f"не более {VIRAL_TG_PER_CHANNEL} на канал).")
    lines.append(_stats_line("Блок 5", b5_stats))
    _blk(lines, b5, _viral_tg_line,
         "      нет постов выше нормы своего канала (или нет базы у канала).")
    return lines, stats


def _viral_compact_item(it: ViralRow, marker: str | None = None) -> CompactItem:
    """Короткая строка «Виральных» для сводки (без подписчиков и кратности блока 1).

    ``marker`` — пометка источника (ТЗ-37 §2.1): ``продукт`` у позиций блока 3.1,
    ``X`` у позиций блока 4; у позиций блока 1 пометки нет. Пометка печатается в
    НЕукорачиваемой части строки (``tail``), чтобы не пропасть при усечении
    описания по бюджету.

    Возраст печатается коротко: ``, N дн`` либо ``, дата неизвестна`` (ТЗ-36 §3.6).
    У позиций блока 3.1 печатаются охват на подписчика и возраст (ТЗ-37 §2.1).
    """
    age = _age_note_short(it.published_at)
    mark = f" ({marker})" if marker else ""
    if it.platform == "youtube":
        tail = (f"{mark} — канал {it.channel}; охват {_fmt_num(it.coverage, 2)}; "
                f"просмотры {_fmt_int(it.views)}, {age}; {_link(it.url)}")
        return CompactItem("", _short(it.title), tail, content_id=it.content_id)
    if it.platform == "x":
        if it.ratio is not None:  # блок 4: реакции относительно нормы автора
            tail = (f"{mark} — лайки {_fmt_int(it.likes)}, норма автора "
                    f"{_fmt_int(it.likes_median)}, ×{_fmt_num(it.ratio, 1)}, "
                    f"{age}; {_link(it.url)}")
        else:  # блок 3.2: запуски/релизы по лайкам
            tail = (f"{mark} — лайки {_fmt_int(it.likes)}, {age}; {_link(it.url)}")
        return CompactItem(f"@{it.channel}: ", _short(it.title), tail,
                           content_id=it.content_id)
    # Блок 5 / 3.3: Telegram. В сводку сейчас НЕ попадает (ТЗ-37 §2.2, D-52);
    # ветка оставлена на возврат позиции Telegram после тематической маркировки.
    if it.ratio is not None:  # блок 5: Telegram выше своей нормы
        tail = (f"{mark} — просмотры {_fmt_int(it.views)}, норма канала "
                f"{_fmt_int(it.norm)}, ×{_fmt_num(it.ratio, 1)}, {age}; "
                f"{_link(it.url)}")
    else:  # блок 3.3: продукты Telegram
        tail = f"{mark} — просмотры {_fmt_int(it.views)}, {age}; {_link(it.url)}"
    return CompactItem(f"[{it.channel}] ", _short(it.title), tail,
                       content_id=it.content_id)


def viral_compact_items(conn, cutoff: str, days: int = 10) -> list[CompactItem]:
    """Раздел «Виральные» сводки: блок 1 ≤3, блок 3.1 ≤2, блок 4 ≤1 (ТЗ-37).

    Инвариант сводки (ТЗ-37 §2.1): сюда попадают ТОЛЬКО YouTube-позиции с
    охватом на подписчика ≥ :data:`VIRAL_YT_MIN_COVERAGE` (3.0):

    * **блок 1** — полезное (``PRODUCT_TOPICS``), без пометки, до 3 позиций;
    * **блок 3.1** — новые продукты с ДВОЙНЫМ полом (просмотров ≥
      :data:`VIRAL_PRODUCT_YT_MIN_VIEWS` И охват ≥ :data:`VIRAL_YT_MIN_COVERAGE`),
      пометка ``(продукт)``, до 2 позиций;
    * **блок 4** — X; подписчиков X в базе нет, поэтому виральность меряется
      кратностью к норме автора, пометка ``(X)``, до 1 позиции.

    Блок 2 (фермы и развлекательное) в сводку не попадает никогда (ТЗ-36 §3.7);
    Telegram (блок 5 и продукты 3.3) не попадает вообще, пока у Telegram-материалов
    пуст ``classification`` (ТЗ-37 §2.2, см. D-52). Добора позициями с охватом
    < 3.0 нет (ТЗ-37 §2.3): лучше меньше строк, чем строка с охватом 0.35.
    Порядок — по приоритету смысла: полезное → новые продукты → X. Дубли по
    ``content_id`` внутри раздела убираются (позиция блока 1 не повторяется как
    ``(продукт)``).
    """
    yt_pool = _viral_youtube_pool(conn, cutoff)
    fresh_yt = [r for r in yt_pool if _in_window(r["published_at"], cutoff)]
    norm = _norm_map(fresh_yt, lambda r: r["source_id"], lambda r: r["views"])
    b1, _ = select_viral_youtube(yt_pool, fun=False, norm=norm, cutoff=cutoff)
    x_cutoff, _ = _now_iso(max(days, VIRAL_X_AUTHOR_WINDOW_DAYS))
    x_pool = _viral_x_pool(conn, x_cutoff)
    # Блок 3.1 сводки пересчитывается СВОИМ полом (ТЗ-Tuber §7): двойной пол
    # «просмотров ≥ 50 000 И охват ≥ 3.0» применяется ДО лимита, поэтому раздел
    # остаётся осмысленным, а не фильтруется чужим топ-5 полного отчёта (замер
    # 20.09.2026: b1 ∩ product = ∅ — продуктов не было ни одного). Порядок —
    # верхние по просмотрам.
    product_yt, _ = viral_product_items(
        yt_pool, x_pool, [], cutoff,
        yt_min_coverage=VIRAL_YT_MIN_COVERAGE)["youtube"]
    b4, _ = viral_x_items(x_pool, cutoff)
    # Telegram-позиции (блок 5) в сводку не пускаем: у Telegram-материалов
    # classification пуст, поэтому ничто не отличает ИИ-материал от не-ИИ.
    # TODO(debt-D-52): Telegram-блок без тематической маркировки — в сводку не пускать (см. TECH-DEBT.md)

    ordered: list[tuple[ViralRow, str | None]] = []
    ordered.extend((it, None) for it in b1[:3])
    ordered.extend((it, "продукт") for it in product_yt[:2])
    ordered.extend((it, "X") for it in b4[:1])

    # Сначала отбор и дедупликация по content_id, потом — перевод ТОЛЬКО
    # выбранных строк (ТЗ-E §E): не платим за позиции, не попавшие в сводку.
    selected: list[tuple[ViralRow, str | None]] = []
    seen: set = set()
    for it, marker in ordered:
        if it.content_id in seen:
            continue
        seen.add(it.content_id)
        selected.append((it, marker))
    _apply_ru_to_viral_x(conn, [it for it, _ in selected])
    return [_viral_compact_item(it, marker) for it, marker in selected]


# --------------------------------------------------------------------------- #
# Подвал: честные оговорки
# --------------------------------------------------------------------------- #
def caveats(conn, days: int = 10) -> list[str]:
    """Оговорки о неполноте данных, посчитанные по факту (не выдуманные)."""
    lines = ["ПОДВАЛ: честные оговорки (что в данных неполно)"]
    cutoff = _now_iso(days)[0]

    yt_null = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='youtube'"
        " AND json_extract(sc.axes_json, '$.viral_index') IS NULL").fetchone()[0]
    yt_all = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='youtube'").fetchone()[0]
    lines.append(
        f"   YouTube: viral_index пуст у {yt_null} из {yt_all} строк score — "
        "композитный индекс на этих видео не построен.")

    x_spread_zero = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='x' AND (sc.spread IS NULL OR sc.spread=0)").fetchone()[0]
    x_all = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='x'").fetchone()[0]
    lines.append(
        f"   X: score.spread = 0/NULL у {x_spread_zero} из {x_all} строк score "
        "(ось наполнена, D-48 закрыт: ноль — честное «тему не подхватил второй "
        "автор реестра», а не отсутствие расчёта).")

    url_null = conn.execute(
        "SELECT COUNT(*) FROM content WHERE url IS NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM content").fetchone()[0]
    lines.append(
        f"   Ссылки: content.url пуст у {url_null} из {total} строк — у этих "
        "материалов ссылка честно не показана (не выдумывается).")

    tg_sig = conn.execute(
        "SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id"
        " WHERE c.platform='telegram' AND sc.significance IS NOT NULL").fetchone()[0]
    lines.append(
        f"   Telegram: significance есть у {tg_sig} строк — значение сравнимо "
        "только внутри одного канала, межканальные сравнения не делаются.")

    cross = conn.execute(
        "SELECT COUNT(*) FROM (SELECT sm.story_id FROM story_member sm"
        " JOIN content c ON c.id=sm.content_id GROUP BY sm.story_id"
        " HAVING COUNT(DISTINCT c.platform)>=2)").fetchone()[0]
    lines.append(
        f"   Сквозных сюжетов в story_member: {cross} — если 0, связывание "
        "сюжетов между платформами ещё не наполнено.")

    # Покрытие данных, от которых зависит секция «Виральные» (ТЗ-35 §2.6).
    # Числа — ЗАПРОСОМ из базы, не хардкод: без подписчиков охват не считается,
    # без базы нормы Telegram материал в блок 5 не попадает.
    for platform, title in (("youtube", "YouTube"), ("x", "X"),
                            ("telegram", "Telegram")):
        total_src = conn.execute(
            "SELECT COUNT(*) FROM source WHERE platform=?", (platform,)).fetchone()[0]
        subs_have = conn.execute(
            "SELECT COUNT(*) FROM source WHERE platform=? AND subs IS NOT NULL"
            " AND subs>0", (platform,)).fetchone()[0]
        lines.append(
            f"   Виральные: подписчики {title} есть у {subs_have} из {total_src} "
            "источников — где их нет, охват на подписчика не считается, материал "
            "в блоки охвата не попадает.")
    tg_total = conn.execute(
        "SELECT COUNT(*) FROM source WHERE platform='telegram'").fetchone()[0]
    tg_base = conn.execute(
        "SELECT COUNT(*) FROM source_baseline b JOIN source s ON s.id=b.source_id"
        " WHERE s.platform='telegram' AND b.median_views IS NOT NULL"
        " AND b.median_views>0").fetchone()[0]
    lines.append(
        f"   Виральные: база нормы Telegram (source_baseline.median_views) есть у "
        f"{tg_base} из {tg_total} источников — без базы материал в блок 5 не "
        "попадает (норму по 1–2 постам не выдумываем).")
    lines.append(
        "   Виральные: подписчики X не собираются вовсе — охват на подписчика "
        "для X невозможен, блок 4 меряется только кратностью к норме автора.")

    # Честная маркировка автора X (ТЗ-Tuber §5): ссылка ведёт на владельца ленты
    # (``source_handle``), а печатается реальный автор (``author_handle``). Там,
    # где они расходятся, в строке печатается «в ленте @…». Счётчик — по факту.
    x_mismatch = conn.execute(
        "SELECT COUNT(*) FROM content c JOIN source s ON s.id=c.source_id"
        " WHERE c.platform='x' AND c.deleted_at IS NULL"
        " AND COALESCE(c.is_repost,0)=0 AND c.published_at >= ?"
        " AND c.author_handle IS NOT NULL AND s.handle IS NOT NULL"
        " AND lower(c.author_handle) <> lower(s.handle)", (cutoff,)).fetchone()[0]
    x_window = conn.execute(
        "SELECT COUNT(*) FROM content c WHERE c.platform='x'"
        " AND c.deleted_at IS NULL AND COALESCE(c.is_repost,0)=0"
        " AND c.published_at >= ?", (cutoff,)).fetchone()[0]
    lines.append(
        f"   X: автор не совпадает с лентой ссылки у {x_mismatch} из {x_window} "
        "постов окна — в строке печатается реальный автор и «в ленте @лента», "
        "чтобы маркировка не противоречила ссылке.")
    return lines


# --------------------------------------------------------------------------- #
# Сборка отчёта
# --------------------------------------------------------------------------- #
def build(conn, *, days: int = 10, db_path: str | None = None) -> str:
    cutoff, now = _now_iso(days)
    parts = [
        f"tuber report — объединённая выдача (единая база: {db_path or '(ядро)'})",
        f"окно: последние {days} дней (с {cutoff} UTC), сформировано {now} UTC",
        "=" * 72,
    ]
    yt_lines, yt_stats = youtube_section(conn, cutoff)
    x_lines, x_stats = x_section(conn, cutoff)
    tg_lines, tg_stats = telegram_section(conn, cutoff)
    st_lines, st_stats = cross_story_section(conn)
    viral_lines, _ = viral_section(conn, cutoff, days)
    # «Новое за сутки» (ТЗ-Tuber ч.2) печатается и в полном отчёте: полный список
    # обязан оставаться НАДмножеством сводки (ссылки сводки сверяются с полным
    # отчётом). Отбор — тем же помощником и с той же дедупликацией, что в сводке.
    fresh_cutoff = (datetime.now(timezone.utc)
                    - timedelta(hours=FRESH_WINDOW_HOURS)
                    ).strftime("%Y-%m-%d %H:%M:%S")
    exclude_ids, _ = _compact_excluded_ids(
        youtube_items(conn, cutoff)[0], x_items(conn, cutoff)[0],
        telegram_items(conn, cutoff)[0], viral_compact_items(conn, cutoff, days))
    fresh_items = new_today_items(conn, fresh_cutoff, exclude_ids)
    fresh_lines = ["6. Новое за сутки — вышедшее за 24 ч:"]
    if fresh_items:
        fresh_lines.extend(it.text for it in fresh_items)
    else:
        fresh_lines.append("   нет новых материалов за сутки.")
    for block in (yt_lines, x_lines, tg_lines, st_lines, viral_lines,
                  fresh_lines):
        parts.append("")
        parts.extend(block)
    parts.append("")
    parts.extend(["=" * 72])
    parts.extend(caveats(conn, days))
    return "\n".join(parts)


def build_json(conn, *, days: int = 10, db_path: str | None = None) -> str:
    """Машинный вывод: те же секции, но текстом (используется приёмкой)."""
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    payload = {"db": db_path, "days": days}
    for name, fn in (("youtube", youtube_section), ("x", x_section),
                     ("telegram", telegram_section)):
        _, stats = fn(conn, cut.strftime("%Y-%m-%d %H:%M:%S"))
        payload[name] = stats
    _, stats = cross_story_section(conn)
    payload["cross_story"] = stats
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --- Раздел «Новое за сутки» (ТЗ-Tuber ч.2) ---------------------------------
#: Глубина «нового»: материал считается свежим, если опубликован не позже
#: стольких часов назад от момента сборки.
FRESH_WINDOW_HOURS = 24
#: Верхний предел позиций раздела «Новое за сутки» (обе платформы вместе).
FRESH_LIMIT = 10


def _fresh_youtube_items(conn, cutoff: str, limit: int) -> list[CompactItem]:
    """Свежие YouTube-видео (за :data:`FRESH_WINDOW_HOURS`) по views/сутки убыв.

    Ранжирование — как в разделе 1 (``views_per_day`` из последнего замера с
    ``interval_quality='ok'``). В отличие от раздела 1 порог показов НЕ
    применяется: цель раздела — показать то, что вышло за сутки и не пробилось
    в накопленный 10-дневный топ, а не отсечь «мелкое» порогом 50 000.
    """
    sql = """
        SELECT c.id AS content_id, c.title, c.lang, c.published_at, c.url,
               s.id AS source_id, s.handle AS channel_handle,
               s.title AS channel_title,
               m.views, m.views_per_day, m.likes, cl.title_ru
        FROM metric_snapshot m
        JOIN content c ON c.id = m.content_id
        JOIN (
            SELECT content_id, MAX(captured_at) AS mc
            FROM metric_snapshot
            WHERE captured_at >= ? AND interval_quality = 'ok'
                  AND views_per_day IS NOT NULL
            GROUP BY content_id
        ) t ON t.content_id = m.content_id AND t.mc = m.captured_at
        LEFT JOIN source s ON s.id = c.source_id
        LEFT JOIN classification cl ON cl.content_id = c.id
        WHERE c.platform = 'youtube' AND c.deleted_at IS NULL
          AND c.published_at >= ?
          AND m.interval_quality = 'ok' AND m.views_per_day IS NOT NULL
        ORDER BY m.views_per_day DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (cutoff, cutoff, limit)).fetchall()
    items = []
    for r in rows:
        title = r["title_ru"] or r["title"] or "(без заголовка)"
        key = str(r["source_id"] if r["source_id"] is not None
                  else (r["channel_handle"] or r["channel_title"] or "?"))
        items.append(CompactItem(
            head="   ",
            desc=_short(title),
            tail=(f" — канал {r['channel_title'] or r['channel_handle'] or '?'}"
                  f"; просмотры/сутки {_fmt_int(r['views_per_day'])}"
                  f", просмотры {_fmt_int(r['views'])}"
                  f", лайки {_fmt_int(r['likes'])}"
                  f"; {_link(r['url']) or 'нет ссылки'}"),
            content_id=r["content_id"],
            author_key=key,
        ))
    return items


def _fresh_x_items(conn, cutoff: str, limit: int) -> list[CompactItem]:
    """Свежие X-посты (за :data:`FRESH_WINDOW_HOURS`) по лайкам/час убыв.

    Сортировка именно по лайкам/час (а не по абсолютным лайкам, как раздел 2):
    свежий пост с меньшим абсолютом, но высокой скоростью реакции должен
    обгонять старый с большим накопленным счётом. Кандидаты берутся из БД все,
    сортировка — в памяти (окно 24 ч невелико).
    """
    sql = """
        SELECT c.id, c.published_at, c.text, c.text_hash, c.url, c.author_handle,
               s.handle AS source_handle, latest.likes,
               cl.title_ru, cl.summary_ru
        FROM content c
        LEFT JOIN content_latest latest ON latest.content_id = c.id
        LEFT JOIN classification cl ON cl.content_id = c.id
        LEFT JOIN source s ON s.id = c.source_id
        WHERE c.platform = 'x' AND c.deleted_at IS NULL
          AND c.published_at >= ?
          AND COALESCE(c.is_repost, 0) = 0
    """
    rows = conn.execute(sql, (cutoff,)).fetchall()
    now = datetime.now(timezone.utc)
    ranked: list[tuple[float, object, str]] = []
    for r in rows:
        likes = r["likes"]
        pub = _parse_published(r["published_at"])
        if likes is None or pub is None:
            continue
        hours = max((now - pub).total_seconds() / 3600.0, 1.0)
        ranked.append((likes / hours, r, _fmt_num(likes / hours)))
    ranked.sort(key=lambda t: -t[0])
    items = []
    for _lph, r, lph in ranked[:limit]:
        head, key, _mismatch = _x_author_head(r)
        # Общая логика описания X (ТЗ №2 §2.1): готовое русское поле — сразу,
        # иначе отложенный перевод того же помощника (:func:`_x_desc_fields`).
        desc, text_hash, text = _x_desc_fields(r)
        item = CompactItem(
            head=head,
            desc=desc,
            tail=(f" — лайки {_fmt_int(r['likes'])}"
                  f", лайки/час {lph}; {_link(r['url']) or 'нет ссылки'}"),
            content_id=r["id"],
            author_key=key,
        )
        item.x_text_hash = text_hash
        item.x_text = text
        items.append(item)
    return items


def _compact_excluded_ids(yt_items, x_items, tg_items, viral_items,
                          per_platform: int | None = None):
    """ids материалов, «занятых» разделами 1–3 сводки, + отфильтрованные «Виральные».

    Общий помощник сводки и полного отчёта: и «Виральные», и «Новое за сутки»
    дедуплицируются по ``content_id`` с разделами 1–3 (приоритет у них).
    Возвращает ``(excluded_ids, viral_items_after_dedup)``.
    """
    def capped(items):
        return list(items[:per_platform] if per_platform is not None else items)

    shown = {it.content_id
             for group in (capped(yt_items), capped(x_items), capped(tg_items))
             for it in group if it.content_id is not None}
    viral = [it for it in capped(viral_items) if it.content_id not in shown]
    excluded = shown | {it.content_id for it in viral if it.content_id is not None}
    return excluded, viral


def new_today_items(conn, cutoff: str, exclude_ids: set,
                    memory_ids: set | None = None,
                    per_author: int | None = None) -> list[CompactItem]:
    """Пункты раздела «Новое за сутки — вышедшее за 24 ч:» (ТЗ-Tuber ч.2).

    Берутся материалы, у которых ``content.published_at`` не старше
    :data:`FRESH_WINDOW_HOURS` часов от ``cutoff``. YouTube ранжируется по
    просмотрам/сутки, X — по лайкам/час (оба — убыв.). Материал, уже показанный
    (или заявленный) в разделах 1–3, в новый раздел не попадает: приоритет у
    существующих разделов (дедупликация по ``content_id``). Итог ограничен
    :data:`FRESH_LIMIT` позициями; платформы перемежаются по кругу, чтобы свежий
    X не был вытеснен более «шумным» числом свежих видео.

    Кэп на автора (ТЗ-Tuber §3.1) применяется и здесь: не больше
    ``per_author`` (по умолчанию :func:`max_per_author`) позиций одного автора;
    при переполнении раздел добирается следующими по ранжированию. Позиции из
    ``memory_ids`` (память выдачи, ТЗ §2.2) НЕ исключаются жёстко, но помечаются
    ``↻`` и печатаются ниже свежих.
    """
    memory_ids = set(memory_ids or ())
    cap = max_per_author() if per_author is None else int(per_author)
    # Пул шире выдачи: кэп на автора требует добора из следующих по ранжированию.
    pool = FRESH_LIMIT * 5
    yt = _fresh_youtube_items(conn, cutoff, pool)
    xs = _fresh_x_items(conn, cutoff, pool)
    # Круговая раздача: YouTube, X, YouTube, X, …
    stream: list[CompactItem] = []
    idx = 0
    pools = (yt, xs)
    while any(idx < len(p) for p in pools):
        for p in pools:
            if idx < len(p):
                stream.append(p[idx])
        idx += 1

    seen = set(exclude_ids)
    counts: dict = {}
    fresh: list[CompactItem] = []
    repeats: list[CompactItem] = []
    for item in stream:
        if item.content_id in seen:
            continue
        key = item.author_key
        if key is not None and counts.get(key, 0) >= cap:
            continue  # кэп: добираем дальше по ранжированию
        seen.add(item.content_id)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
        if item.content_id in memory_ids:
            item.head = "   ↻ " + item.head.lstrip()
            repeats.append(item)
        else:
            fresh.append(item)
    # Русские описания X для «Нового за сутки» (ТЗ №2 §2.1–§2.2): переводим
    # только позиции, реально попавшие в вывод, тем же общим помощником, что и
    # раздел 2. При TUBER_RU_DESC=0 помощник вернёт сырой текст — как раньше.
    selected = (fresh + repeats)[:FRESH_LIMIT]
    _apply_deferred_x_descriptions(conn, selected)
    # Помеченные «↻» идут ниже свежих, но в пределах общего лимита.
    return selected


def _compact_section_is_telegram(name: str, items) -> bool:
    """Предохранитель сводки (ТЗ-38 §2.1): Telegram-раздел не печатать.

    Раздел ``Telegram`` отбрасывается, пока не включён флаг ``TUBER_TG_SECTION``
    (:func:`telegram_section_enabled`, ТЗ-A); при включённом флаге он печатается
    с фильтром профиля по статусу реестра (``s.status='active'`` в
    :func:`telegram_items`). Независимо от имени раздела выбрасывается любой
    раздел, в пунктах которого есть Telegram-ссылка
    (:data:`TELEGRAM_LINK_MARK`) — предохранитель от протечки Telegram-строки
    через чужой раздел. Остаточный долг D-52 — пост-уровневая тематическая
    (семантическая) маркировка Telegram-постов (Слой B).
    """
    if name == "Telegram":
        return not telegram_section_enabled()
    return any(TELEGRAM_LINK_MARK in it.text for it in items)


def build_compact(conn, *, days: int = 10, db_path: str | None = None,
                  report_path: str | None = None,
                  per_platform: int | None = None,
                  limit: int | None = None,
                  record_sink: list | None = None) -> str:
    """Компактная сводка для доставки владельцу (ТЗ-5-доп-2, ТЗ-5-доп-3).

    Печатается в stdout обёрткой ``scripts/common/tuber_report.sh``: планировщик
    Hermes для заданий ``no_agent`` доставляет владельцу ровно stdout. Сводка:

    * заголовок: что это, окно (``--days``), откуда (путь к базе), дата;
    * по каждой платформе — пункты в том же виде, что и в полном отчёте (русское
      описание, цифры, ПОЛНАЯ ссылка); правило ранжирования НЕ печатается —
      вместо него одна строка в подвале. Telegram-раздел печатается только при
      включённом флаге ``TUBER_TG_SECTION`` (ТЗ-A, по умолчанию OFF) и с
      фильтром профиля по статусу реестра (``s.status='active'``): у
      Telegram-материалов пуст ``classification``, поэтому пост-уровневой
      тематической маркировки нет (остаток D-52, Слой B идёт пост-уровневой
      семантикой); Слой A (:func:`telegram_items`) отсекает каналы вне реестра.
      Независимо от флага предохранитель :func:`_compact_section_is_telegram`
      выбрасывает любой раздел (кроме ``Telegram``), содержащий строку с
      ``t.me/``; сырые Telegram-аномалии владелец видит в полном отчёте;
    * виральные (ТЗ-35, ТЗ-37) — раздел «Виральные — охват против своей
      аудитории:» (печатается, когда пул непуст — даже если весь пул показывался
      ранее: тогда раздел честно сообщает об исчерпании, ТЗ №2 §1.3) с короткими
      строками: до 3 позиций блока 1, до 2 позиций блока 3.1 (двойной пол:
      просмотров ≥ 50 000 И охват ≥ 3.0), до 1 позиции блока 4 (ТЗ-37 §2.1); у
      позиций блока 3.1 пометка ``(продукт)``, у блока 4 — ``(X)``. Блок 2
      (фермы) и Telegram (блок 5) в сводку не попадают (ТЗ-36 §3.7, ТЗ-37 §2.2,
      D-52); без дублей по ``content_id`` с разделами 1–3 (бюджет не тратится
      дважды на одну публикацию); подписчиков и кратность блока 1 в сводке не
      печатаем — экономим байты;
    * «Новое за сутки — вышедшее за 24 ч:» (ТЗ-Tuber ч.2) — печатается ВСЕГДА
      (пустое состояние честно сообщает «нет новых материалов за сутки»).
      Материалы, опубликованные не позже :data:`FRESH_WINDOW_HOURS` часов назад:
      YouTube — по просмотрам/сутки (как раздел 1, но без порога показов), X —
      по лайкам/час (убыв.), всего не более :data:`FRESH_LIMIT` позиций. Дубли
      по ``content_id`` с разделами 1–3 выброшены (приоритет у них); раздел
      идёт после «Виральных» и перед сквозным сюжетом;
    * сквозной сюжет — одной строкой (сколько сюжетов или «нет»), идёт последним;
    * подвал: сколько пунктов скрыто по каждой платформе + путь к полному файлу
      (или пометка, что файл не сохранялся).

    Номера разделов вычисляются по порядку фактического вывода (``enumerate``), а
    не хранятся в заголовках: при любом составе разделов (Telegram скрыт или
    возвращён после D-52, пустой X, «Виральные» или «Новое за сутки») номера идут
    сплошняком ``1, 2, …, N`` без дыр (ТЗ-39 §2.1). Сквозной сюжет получает номер
    ``N + 1``.

    Распределение мест (ТЗ-5-доп-3). Бюджет ``limit`` байт UTF-8 (по умолчанию
    :data:`DEFAULT_COMPACT_LIMIT`, переопределяется переменной окружения
    ``TUBER_COMPACT_LIMIT`` и флагом ``--compact-limit``) раздаётся СПРАВЕДЛИВО, а
    не «обрезкой с конца»:

    1. **резерв** — каждый раздел, у которого есть пункты за окно, получает
       минимум 1 пункт; если резерв не влезает целиком, укорачивается только
       ОПИСАНИЕ пункта (см. :class:`CompactItem`) — ссылка и цифры неизменны;
    2. **round-robin** — остаток бюджета раздаётся по кругу: на каждом шаге
       раздел получает следующий по порядку пункт, если тот влезает целиком;
       порядок внутри раздела сохраняется (сначала верхние).

    Строка «все пункты скрыты» не печатается никогда: раздел без данных честно
    сообщает «нет данных за окно» (пунктов нет в данных, а не в бюджете).
    Счётчики скрытых печатаются всегда, и «показано + скрыто» по каждому разделу
    сходится с полным отчётом. ``per_platform`` — необязательный верхний предел
    пунктов на раздел (``None`` — без предела, решает бюджет).
    """
    limit = compact_limit(limit)
    cutoff, now = _now_iso(days)
    fresh_cutoff = (datetime.now(timezone.utc)
                    - timedelta(hours=FRESH_WINDOW_HOURS)
                    ).strftime("%Y-%m-%d %H:%M:%S")
    # «Естественный» состав раздела (без памяти) — канонические функции разделов:
    # это знаменатель честного «скрыто» (ТЗ-Tuber §6) и материал предохранителя
    # (:func:`_compact_section_is_telegram`). Широкие пулы для добора берутся
    # отдельно (ТЗ №2 §1.1).
    yt_natural, yt_stats = youtube_items(conn, cutoff)
    yt_above, _ = youtube_above_rows(conn, cutoff)
    x_rows, x_total = x_candidate_rows(conn, cutoff, candidate_pool(X_LIMIT))
    x_stats = {"considered": x_total, "above": x_total,
               "author_mismatch": sum(1 for r in x_rows if _x_author_head(r)[2])}
    tg_items, tg_stats = telegram_items(conn, cutoff)
    viral_items = viral_compact_items(conn, cutoff, days)
    _, st_stats = cross_story_section(conn)
    now_dt = datetime.now(timezone.utc)

    # Память выдачи (ТЗ-Tuber §2): позиции, показывавшиеся за последние N суток,
    # из разделов 1–3 и 5 исключаются; «Новое за сутки» не исключается жёстко.
    mem_days = digest_memory.memory_days()
    memory_set: set = set()
    if digest_memory.enabled():
        memory_set = digest_memory.recent_ids(conn, mem_days)

    def exclude_memory(items: list) -> tuple[list, int]:
        kept, removed = [], 0
        for it in items:
            if it.content_id is not None and it.content_id in memory_set:
                removed += 1
            else:
                kept.append(it)
        return kept, removed

    def capped(items):
        return list(items[:per_platform] if per_platform is not None else items)

    per_author = max_per_author()
    yt_limit = (YOUTUBE_LIMIT if per_platform is None
                else min(YOUTUBE_LIMIT, per_platform))
    x_limit = X_LIMIT if per_platform is None else min(X_LIMIT, per_platform)

    # Порядок (ТЗ №2 §1.2): широкий пул → порог/дедуп (в SQL) → исключение
    # показанных за ``DIGEST_MEMORY_DAYS`` → кэп на автора → обрезка до размера
    # раздела. «natural» — сколько позиций раздел показал бы БЕЗ памяти: это
    # знаменатель честного «скрыто» и оно же остаётся целью добора из пула.
    yt_natural = capped(yt_natural)
    yt_pool = [_youtube_compact_item(r)
               for r in yt_above[:candidate_pool(YOUTUBE_LIMIT)]]
    x_pool = [_x_compact_item(r, now_dt) for r in x_rows]
    x_natural = _cap_items_per_key(x_pool, per_author, x_limit)

    yt_kept_pool, yt_mem = exclude_memory(yt_pool)
    x_kept_pool, x_mem = exclude_memory(x_pool)
    yt_kept = _cap_items_per_key(yt_kept_pool, per_author, yt_limit)
    x_kept = _cap_items_per_key(x_kept_pool, per_author, x_limit)
    # Перевод X — только для позиций, реально попавших в раздел (ТЗ №2 §2.2).
    _apply_deferred_x_descriptions(conn, x_kept)
    tg_kept, tg_mem = exclude_memory(capped(tg_items))
    viral_natural = capped(viral_items)
    viral_kept, viral_mem = exclude_memory(viral_natural)

    # Дедупликация (ТЗ-35 §2.7, ТЗ-Tuber ч.2): разделы 1–3 имеют приоритет, поэтому
    # материал, уже присутствующий там (любой из них), в «Виральные» и «Новое за
    # сутки» не попадает — иначе бюджет сводки тратится дважды на одну публикацию.
    # Сравнение по content_id (поле у элемента заполнено).
    exclude_ids, viral_kept = _compact_excluded_ids(
        yt_kept, x_kept, tg_kept, viral_kept)
    # Раздел «Новое за сутки» (ТЗ-Tuber ч.2): печатается ВСЕГДА, даже пустым.
    # Позиции из памяти НЕ исключаются, но помечаются ``↻`` и идут ниже свежих.
    fresh_items = new_today_items(conn, fresh_cutoff, exclude_ids,
                                  memory_ids=memory_set)

    raw: list[dict] = []
    raw.append({"name": "YouTube", "title": "YouTube — топ по просмотрам/сутки:",
                "items": yt_kept, "removed": yt_mem, "stats": yt_stats,
                "natural": len(yt_natural), "guard_items": yt_natural,
                "platform": "youtube"})
    raw.append({"name": "X", "title": "X — топ по лайкам:", "items": x_kept,
                "removed": x_mem, "stats": x_stats,
                "natural": len(x_natural), "guard_items": x_natural,
                "platform": "x"})
    # Telegram-раздел собирается всегда, но печатается только при включённом
    # флаге ``TUBER_TG_SECTION`` (ТЗ-A): при OFF предохранитель
    # (:func:`_compact_section_is_telegram`) выбрасывает раздел целиком.
    # Профиль отобран фильтром ``s.status='active'`` в :func:`telegram_items`.
    # TODO(debt-D-52): остаток долга — пост-уровневая (семантическая) тематическая
    # маркировка Telegram-постов (Слой B); раздел возвращён Слоем A (см. TECH-DEBT.md)
    raw.append({"name": "Telegram", "title": "Telegram — топ постов за окно:",
                "items": tg_kept, "removed": tg_mem, "stats": tg_stats,
                "natural": len(capped(tg_items)), "platform": "telegram"})
    # Раздел «Виральные» печатается, если пул был непуст — даже когда весь пул
    # показывался ранее: тогда раздел честно объясняет исчерпание, а не исчезает
    # без пояснения (ТЗ №2 §1.3, требование З2.4).
    if viral_natural:
        raw.append({"name": "Виральные",
                    "title": "Виральные — охват против своей аудитории:",
                    "items": viral_kept, "removed": viral_mem, "stats": None,
                    "natural": len(viral_natural), "platform": "viral"})
    raw.append({"name": "Новое за сутки",
                "title": "Новое за сутки — вышедшее за 24 ч:",
                "items": fresh_items, "removed": 0, "stats": None,
                "natural": len(fresh_items), "platform": "fresh"})

    for s in raw:
        if s["name"] == "Новое за сутки":
            s["empty"] = "нет новых материалов за сутки."
        elif s["items"]:
            s["empty"] = "нет данных за окно."
        elif s["removed"] > 0:
            # Честное пустое состояние после фильтра памяти (ТЗ №2 §1.3).
            s["empty"] = ("все позиции показывались в предыдущие "
                          f"{mem_days} суток (пул исчерпан).")
        else:
            s["empty"] = "нет данных за окно."
        s["shown"] = []

    # Предохранитель (ТЗ-38 §2.1, уточнён ТЗ-A): раздел ``Telegram`` печатается
    # только при включённом ``TUBER_TG_SECTION``; любой другой раздел со строкой
    # ``t.me/`` выбрасывается. Раздел выброшен здесь целиком, поэтому его нет ни
    # в теле, ни в счётчиках подвала (D-52 — остаётся пост-уровневая маркировка).
    sections: list[dict] = [
        s for s in raw
        if not _compact_section_is_telegram(
            s["name"],
            list(s["items"]) + list(s.get("guard_items") or ()))]
    # Сквозной сюжет идёт последним и получает следующий свободный номер после
    # фактически напечатанных разделов — тоже без дыр (ТЗ-39 §2.1).
    story_no = len(sections) + 1

    def hidden(s: dict) -> int:
        # Показано + скрыто = «natural» (кандидаты раздела без фильтра памяти):
        # скрытые — это и не влезшие в бюджет, и исключённые памятью выдачи.
        return max(0, s["natural"] - len(s["shown"]))

    def real_hidden(s: dict) -> int:
        """Честное «скрыто» от полной выборки раздела (ТЗ-Tuber §6).

        Для платформенных разделов знаменатель — «выше порога» из статистики
        (``above`` для YouTube, ``considered`` для X), а не обрезанный лимитом
        список. Именно это число владелец видит в подвале.
        """
        st = s.get("stats") or {}
        above = st.get("above")
        if above is None:
            return hidden(s)
        return max(0, int(above) - len(s["shown"]))

    header = [
        "Tuber — объединённая выдача: сводка владельцу",
        f"Окно: последние {days} дней (с {cutoff} UTC); база: {db_path or '(ядро)'}; "
        f"сформировано {now} UTC",
    ]
    if st_stats.get("stories"):
        story_line = (f"{story_no}. Сквозной сюжет: сюжетов с материалами разных "
                      f"платформ — {st_stats['stories']} (детали — в полном отчёте).")
    else:
        story_line = (f"{story_no}. Сквозной сюжет: нет сюжетов с материалами "
                      "разных платформ.")

    def stats_line(s: dict) -> str | None:
        st = s.get("stats") or {}
        if "considered" not in st:
            return None
        above = st.get("above", st["considered"])
        return (f"   {s['name']}: рассмотрено {_fmt_int(st['considered'])}"
                f", выше порога {_fmt_int(above)}"
                f", показано {len(s['shown'])}, скрыто {real_hidden(s)}.")

    def render() -> str:
        parts = list(header)
        for n, s in enumerate(sections, 1):
            parts.append("")
            parts.append(f"{n}. {s['title']}")
            vis = s["shown"]
            if vis:
                for i, (item, desc_limit) in enumerate(vis, 1):
                    parts.append(f"{i}. {item.render(desc_limit).strip()}")
                # Пул не дал добрать раздел до штатного размера (ТЗ №2 §1.3):
                # `items` — всё, что осталось в пуле после исключения показанных,
                # поэтому короткий `items` (а не бюджет) означает исчерпание пула.
                if s.get("removed", 0) > 0 and len(s["items"]) < s["natural"]:
                    parts.append(
                        f"   (часть позиций показывалась в предыдущие {mem_days} "
                        f"суток: пул исчерпан, в пуле осталось {len(s['items'])} "
                        f"из {s['natural']}.)")
            else:
                parts.append(f"   {s.get('empty', 'нет данных за окно.')}")
            parts.append(f"   скрыто пунктов: {hidden(s)}")
            counter = stats_line(s) if opts["counters"] else None
            if counter:
                parts.append(counter)
        parts.append("")
        if opts["story"]:
            parts.append(story_line)
            parts.append("")
        if opts["rules"]:
            parts.append("Правила ранжирования и полный список — в полном отчёте.")
        if report_path:
            parts.append(f"Полный отчёт: {report_path}")
        else:
            parts.append("Полный отчёт файлом не сохранялся (--save не запрошен).")
        parts.append("Скрыто по платформам: " + ", ".join(
            f"{s['name']} {real_hidden(s)}" for s in sections) + ".")
        return "\n".join(parts)

    #: Необязательные служебные строки: под узким бюджетом их можно убрать
    #: (счётчики «рассмотрено/…» → правило → сквозной сюжет), лишь бы сводка
    #: влезла в лимит. Обязательным остаётся тело разделов и счётчик скрытых.
    opts = {"counters": True, "rules": True, "story": True}

    def over_limit() -> bool:
        return len(render().encode("utf-8")) + 1 > limit

    # 1. Резерв: по одному пункту в каждый непустой раздел.
    for s in sections:
        if s["items"]:
            s["shown"].append((s["items"][0], None))

    # Резерв не влез — укорачиваем описания (никогда не ссылку и не цифры).
    while over_limit():
        best = None  # (len(rendered desc), section, index, item)
        for s in sections:
            for idx, (item, desc_limit) in enumerate(s["shown"]):
                cur = item.desc if desc_limit is None else _shorten_desc(
                    item.desc, desc_limit)
                if cur and (best is None or len(cur) > best[0]):
                    best = (len(cur), s, idx, item)
        if best is None:
            # Укорачивать больше нечего — убираем необязательные строки.
            if opts["counters"]:
                opts["counters"] = False
                continue
            if opts["rules"]:
                opts["rules"] = False
                continue
            if opts["story"]:
                opts["story"] = False
                continue
            break  # раздел всё равно показан: дальше сокращать нечего
        _, s, idx, item = best
        new_limit = max(0, best[0] - max(2, best[0] // 3))
        s["shown"][idx] = (item, new_limit)

    # 2. Round-robin: остаток бюджета — по одному следующему пункту за шаг.
    next_idx = {s["name"]: 1 for s in sections}
    while True:
        progressed = False
        for s in sections:
            i = next_idx[s["name"]]
            if i >= len(s["items"]):
                continue
            s["shown"].append((s["items"][i], None))
            if over_limit():
                s["shown"].pop()
            else:
                next_idx[s["name"]] = i + 1
                progressed = True
        if not progressed:
            break

    if record_sink is not None:
        for s in sections:
            for item, _desc_limit in s["shown"]:
                if item.content_id is not None:
                    record_sink.append((item.content_id, s["platform"], s["name"]))

    return render()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuber report",
                                     description="Объединённая выдача по трём платформам")
    parser.add_argument("--db", dest="db_path", default=None,
                        help="путь к единой базе (по умолчанию data/tuber.db)")
    parser.add_argument("--days", type=int, default=10, help="окно, дней")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--compact", action="store_true",
                        help="компактная сводка владельцу (бюджет по умолчанию "
                             f"{DEFAULT_COMPACT_LIMIT} байт, см. TUBER_COMPACT_LIMIT) "
                             "вместо полного текста")
    parser.add_argument("--compact-limit", dest="compact_limit", type=int,
                        default=None,
                        help="бюджет сводки в байтах UTF-8 (переопределяет "
                             "TUBER_COMPACT_LIMIT и значение по умолчанию)")
    parser.add_argument("--save", action="store_true",
                        help="дополнительно сохранить отчёт в файл")
    parser.add_argument("--out", default=None,
                        help="каталог/файл для --save (по умолчанию reports/)")
    parser.add_argument("--readable", dest="readable", default=None,
                        help="записать читаемый Markdown-мастер полного отчёта")
    parser.add_argument("--docx", dest="docx", default=None,
                        help="записать читаемый DOCX полного отчёта (ТЗ-41)")
    parser.add_argument("--no-ru-desc", dest="no_ru_desc", action="store_true",
                        help="отключить русские описания X (то же, что "
                             "TUBER_RU_DESC=0): описание снова _short(text)")
    parser.add_argument("--ru-backfill-limit", dest="ru_backfill_limit",
                        type=int, default=None,
                        help="необязательно: вручную прогреть кэш переводов X "
                             "(не более N постов окна; полного бэкфилла нет)")
    args = parser.parse_args(argv)
    if args.no_ru_desc:
        os.environ["TUBER_RU_DESC"] = "0"

    db_path = config.db_path(args.db_path)
    if not Path(db_path).exists():
        print(f"ошибка: базы нет: {db_path}", file=sys.stderr)
        return 2
    if args.ru_backfill_limit:
        wconn = db.connect(db_path)
        try:
            done = backfill_ru(wconn, args.ru_backfill_limit, args.days)
        finally:
            wconn.close()
        print(f"прогрев кэша переводов X: переведено {done} "
              f"(лимит {args.ru_backfill_limit})", file=sys.stderr)
        return 0
    conn = db.connect(db_path, readonly=True)
    try:
        if args.json:
            print(build_json(conn, days=args.days, db_path=db_path))
            return 0
        text = build(conn, days=args.days, db_path=db_path)
        saved_path = None
        if args.save:
            out = args.out
            if out and Path(out).suffix:
                path = Path(out)
            else:
                base = Path(out) if out else (config.ROOT / "reports")
                path = base / f"report-{datetime.now(timezone.utc):%Y-%m-%d}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
            saved_path = path

        # Читаемая выдача (ТЗ-41): Markdown-мастер и DOCX. Текст отчёта не
        # меняется; служебные строки идут в stderr, чтобы не задеть stdout
        # (в ветке --compact stdout — это ровно сводка владельцу).
        rc = 0
        if args.readable or args.docx:
            from tuber.analysis import readable as readable_mod
            tmp_master = None
            md_path = args.readable
            if args.docx and not md_path:
                # Мастер нужен только как промежуточный — во временный файл.
                fd, tmp_master = tempfile.mkstemp(suffix=".md", prefix="tuber-")
                os.close(fd)
                md_path = tmp_master
            try:
                _, messages = readable_mod.write_readable(
                    text, md_path=md_path, docx_path=args.docx)
                for message in messages:
                    print(message, file=sys.stderr)
            except ValueError as exc:
                print(f"ошибка разбора отчёта: {exc}", file=sys.stderr)
                rc = 2
            finally:
                if tmp_master:
                    try:
                        os.unlink(tmp_master)
                    except OSError:
                        pass

        if args.compact:
            # Владельцу уходит ровно сводка; полный отчёт — файлом (--save).
            recorded: list = []
            print(build_compact(conn, days=args.days, db_path=db_path,
                                report_path=str(saved_path) if saved_path else None,
                                limit=args.compact_limit,
                                record_sink=recorded))
            # Память выдачи (ТЗ-Tuber §2.3): записываем ТОЛЬКО в CLI-режиме с
            # --save (сухой прогон и тесты не пишут). Отдельное записываемое
            # соединение: основное открыто read-only.
            if args.save and digest_memory.enabled() and recorded:
                try:
                    wconn = db.connect(db_path)
                    try:
                        digest_memory.ensure_schema(wconn)
                        digest_memory.mark_sent(wconn, recorded)
                    finally:
                        wconn.close()
                except sqlite3.Error as exc:
                    print(f"предупреждение: память выдачи не записана: {exc}",
                          file=sys.stderr)
        else:
            print(text)
            if saved_path:
                print(f"\n(сохранено: {saved_path})")
        return rc
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
