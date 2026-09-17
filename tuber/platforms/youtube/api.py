"""Транспорт YouTube Data API v3 без SDK (requests).

Гарантии:
- битые ключи исключаются из работы до конца процесса;
- исчерпавший суточную квоту ключ исключается только до конца текущих суток
  КВОТЫ по Pacific Time (в эту полночь Google сбрасывает суточную квоту):
  при смене суток он снова доступен (исключение хранится вместе с сутками);
- при quotaExceeded — переход на следующий ключ, при 429 — пауза и повтор;
- любая другая HTTP-ошибка поднимает YouTubeError (не возвращает None);
- каждый вызов пишет строку в quota_log;
- пауза между вызовами не меньше min_interval (по умолчанию 0.2 с).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # requests установлен в системе
    import requests
except ImportError:  # pragma: no cover - запасной путь
    requests = None  # type: ignore

from . import config, store as db

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3/"

# Известное публичное видео для проверки годности ключа.
KNOWN_VIDEO_ID = "dQw4w9WgXcQ"

# Причины ошибок, означающие непригодность ключа.
KEY_DEAD_REASONS = frozenset({
    "keyInvalid",
    "badRequest",
    "ipRefererBlocked",
    "forbidden",
})
# Причины, означающие исчерпание квоты ключа.
QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
# Причины 403, относящиеся к самому видео, а не к ключу (например, у видео
# отключены комментарии). Такой 403 не делает ключ битым и не должен
# зацикливать ротацию: поднимаем ошибку сразу, ключ остаётся рабочим.
VIDEO_LEVEL_REASONS = frozenset({"commentsDisabled"})

# Причины 403 у commentThreads.list по документации YouTube Data API v3
# (https://developers.google.com/youtube/v3/docs/commentThreads/list, раздел
# Errors): commentsDisabled — у видео отключены комментарии; forbidden —
# недостаточно прав / запрос не авторизован для этого видео. Оба 403 здесь —
# свойство видео/доступа, а не смерть ключа.
COMMENT_403_REASONS = frozenset({"commentsDisabled", "forbidden"})


class YouTubeError(Exception):
    """Ошибка транспорта YouTube. Не глотается и не превращается в None."""

    def __init__(self, code: int, body: Any, endpoint: str = ""):
        self.code = code
        self.body = body
        self.endpoint = endpoint
        super().__init__(f"YouTube API error {code} at {endpoint}: {body!r}")


class SearchQuotaGuard(YouTubeError):
    """Предохранитель поисковой квоты: вызов search.list НЕ отправлен.

    Отдельный лимит Google — 100 вызовов search.list в сутки на проект.
    Когда порог (лимит минус резерв) достигнут, вызов не уходит в сеть и units
    не тратятся. Прогон из-за этого не падает: вызывающий код ловит это
    исключение и продолжает работу бесплатными механизмами (разбор имён,
    упоминания, обход плейлистов).

    Унаследован от YouTubeError, чтобы уже существующие обработчики ошибок не
    пропустили аварийный случай; отдельный тип нужен, чтобы отличить остановку
    поиска от прочих сбоев и записать понятную причину в итог прогона.
    """

    def __init__(self, message: str, project: str | None = None,
                 calls: int = 0, threshold: int = 0):
        self.message = message
        self.project = project
        self.calls = int(calls)
        self.threshold = int(threshold)
        super().__init__(
            403,
            {"error": {
                "message": message,
                "errors": [{"reason": "searchQuotaGuard"}],
            }},
            "search",
        )

    def __str__(self) -> str:
        """Человеческий текст события, без обёртки YouTubeError."""
        return self.message


def _chunks(items: Sequence[Any], size: int) -> Iterable[list[Any]]:
    """Разбить список на пачки не длиннее size."""
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def _error_reason(body: Any) -> str | None:
    """Достать reason из тела ошибки YouTube."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0].get("reason")
    return None


def _rfc3339(value: int | str | None) -> str | None:
    """Перевести unixtime в RFC3339; строку вернуть как есть."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _day_start(ts: int) -> int:
    """Начало UTC-суток для unixtime (целое).

    Используется как колонка `date` в quota_log — это формат суточных сводок и
    отчётов (collect.py, comments.py); его менять нельзя.
    """
    return (int(ts) // 86400) * 86400


# Пояс, в полночь которого Google сбрасывает суточную квоту проекта.
# Смещение НЕ хардкодится: DST переключает его (PDT −7 летом / PST −8 зимой),
# поэтому берём пояс системой через zoneinfo.
QUOTA_TZ_NAME = "America/Los_Angeles"
# Запасное фиксированное смещение PST (−8), если в системе нет базы поясов.
_FALLBACK_QUOTA_OFFSET = timedelta(hours=-8)
_quota_tz_warned = False


def _quota_tzinfo() -> Any:
    """tzinfo America/Los_Angeles; при отсутствии данных — фикс. PST (−8).

    Если базы поясов (пакета tzdata) в системе нет, zoneinfo бросает
    ZoneInfoNotFoundError. Падать из-за этого предохранитель не должен, поэтому
    один раз пишем предупреждение в лог и работаем на фиксированном смещении
    −8 (PST). В сентябре реальное смещение −7 (PDT), но запасной путь нужен
    лишь чтобы не уронить прогон при отсутствии tzdata.
    """
    global _quota_tz_warned
    try:
        return ZoneInfo(QUOTA_TZ_NAME)
    except ZoneInfoNotFoundError:
        if not _quota_tz_warned:
            log.warning(
                "zoneinfo: нет данных о поясе %s — беру фиксированное "
                "смещение −8 (PST) для границ суточной квоты",
                QUOTA_TZ_NAME,
            )
            _quota_tz_warned = True
        return timezone(_FALLBACK_QUOTA_OFFSET)


def _quota_day_start(ts: int) -> int:
    """Начало текущих суток квоты по Pacific Time для unixtime ts.

    Google сбрасывает суточную квоту проекта в полночь America/Los_Angeles.
    В сентябре это 07:00 UTC: до 07:00 UTC расход прошлых UTC-суток всё ещё
    входит в текущие сутки квоты.
    """
    local = datetime.fromtimestamp(int(ts), tz=_quota_tzinfo())
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _normalize_key_items(
    items: Iterable[Any],
) -> list[tuple[str, str | None]]:
    """Привести элементы ключей к парам (ключ, проект|None).

    Принимает как строки (старый формат — проект неизвестен), так и пары
    «ключ + проект». Разбор JSON-файла живёт в config.load_key_projects();
    здесь — только нормализация того, что передал вызывающий код.
    """
    out: list[tuple[str, str | None]] = []
    for item in items:
        if isinstance(item, str):
            key, project = item, None
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            key, project = item[0], item[1]
        else:
            continue
        key = str(key).strip() if key is not None else ""
        if not key:
            continue
        if project is None or project == "":
            project = None
        else:
            project = str(project).strip() or None
        out.append((key, project))
    return out


class YouTubeClient:
    """Клиент YouTube API с ротацией ключей и учётом квоты."""

    def __init__(
        self,
        keys: Sequence[Any] | None = None,
        conn: Any | None = None,
        session: Any | None = None,
        min_interval: float = 0.2,
        timeout: float = 20.0,
        sleep: Any = time.sleep,
        retry_delay: float = 5.0,
    ):
        raw_keys = keys if keys is not None else config.load_key_projects()
        pairs = _normalize_key_items(raw_keys)
        self._keys = [key for key, _project in pairs]
        # ключ -> проект (None, если привязки нет). Ключи одного проекта делят
        # суточную квоту проекта (QUOTA_LIMIT_PER_PROJECT).
        self._projects: dict[str, str | None] = {key: project for key, project in pairs}
        self.conn = conn
        self.min_interval = min_interval
        self.timeout = timeout
        self.retry_delay = retry_delay
        self._sleep = sleep
        if session is not None:
            self.session = session
        elif requests is not None:
            self.session = requests.Session()
        else:  # pragma: no cover
            raise RuntimeError("requests не установлен и сессия не передана")

        self._bad: set[str] = set()
        # Ключи, исчерпавшие СУТОЧНУЮ квоту: key -> начало суток исключения.
        # В отличие от _bad (битые ключи), исключение снимается при смене суток.
        self._quota_blocked: dict[str, int] = {}
        self._valid: set[str] = set()
        self._cursor = 0
        self._last_call_ts = 0.0
        self._lock = threading.Lock()
        self._max_attempts = max(6, len(self._keys) * 3)

    # --- ключи ------------------------------------------------------------

    @staticmethod
    def key_id(key: str) -> str:
        """Несекретный идентификатор ключа для логов."""
        return "k" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]

    @property
    def bad_keys_count(self) -> int:
        """Сколько ключей недоступно: битые плюс исчерпавшие сутки."""
        self._purge_stale_quota_blocks()
        return len(self._bad) + len(self._quota_blocked)

    def usable_keys_count(self) -> int:
        self._purge_stale_quota_blocks()
        return len(self._keys) - len(self._bad) - len(self._quota_blocked)

    # --- ключи, исключённые по суточной квоте -----------------------------

    def _purge_stale_quota_blocks(self) -> None:
        """Снять исключения по квоте, сделанные в прошлые сутки квоты (PT)."""
        today = _quota_day_start(config.now_ts())
        for key in [k for k, day in self._quota_blocked.items() if day != today]:
            del self._quota_blocked[key]

    def _is_quota_blocked(self, key: str) -> bool:
        """Исключён ли ключ по суточной квоте в ТЕКУЩИЕ сутки квоты (PT).

        Ключ не выпадает навсегда: вместе с ключом хранятся сутки квоты
        исключения, и при смене суток (полночь Pacific Time, как у Google)
        исключение снимается — ключ снова доступен.
        """
        day = self._quota_blocked.get(key)
        if day is None:
            return False
        if day != _quota_day_start(config.now_ts()):
            del self._quota_blocked[key]
            return False
        return True

    def _block_quota(self, key: str) -> None:
        """Исключить ключ по суточной квоте до конца текущих суток квоты (PT)."""
        self._quota_blocked[key] = _quota_day_start(config.now_ts())

    # --- предохранитель суточной квоты -----------------------------------

    def quota_limit(self, key: str) -> int:
        """Суточный лимит для ключа: проектный, если проект известен."""
        if self._projects.get(key):
            return config.QUOTA_LIMIT_PER_PROJECT
        return config.QUOTA_LIMIT_PER_KEY

    def quota_stop_threshold(self, key: str) -> int:
        """Порог остановки по квоте: лимит минус неприкосновенный запас.

        Формула запаса считается только здесь, чтобы не дублировать её:
        10 000 - QUOTA_SAFETY_RESERVE (2 000) = 8 000 units на проект (или на
        ключ без проекта). Запас оставляем на повторы после 429/сетевого
        сбоя, одну дорогую операцию (поиск до 100 units) и хвост прогона.
        """
        return self.quota_limit(key) - config.QUOTA_SAFETY_RESERVE

    def spent_today(self, key: str) -> int:
        """Израсходовано за текущие сутки квоты (units) по quota_log.

        Сутки квоты считаются по Pacific Time (America/Los_Angeles): именно в
        эту полночь Google сбрасывает суточную квоту проекта. Суммируются
        строки с `ts >= начало_суток_по_PT`, ПЛЮС старые строки с `ts IS NULL`,
        у которых `date >= начало UTC-суток, в которые попадает начало суток
        квоты` (то есть `date >= _day_start(quota_day)`): они записаны прежним
        кодом, который хранил только UTC-дату.

        Точное время у legacy-строк неизвестно, поэтому приближение неизбежно.
        Выбранное приближение может ЗАВЫСИТЬ расход на часть суток (записи
        00:00–07:00 UTC летом и 00:00–08:00 UTC зимой, относящиеся к прошлым
        суткам квоты — граница суток квоты в UTC смещается вместе с переходом
        Калифорнии на зимнее время), но предохранитель
        должен останавливаться раньше, а не позже: недооценка приводит к отказу
        Google `403 quotaExceeded`, а преувеличение — лишь к более ранней
        остановке. Эффект одноразовый: как только весь `quota_log` заполнен
        строками с `ts`, эта ветка перестаёт срабатывать.

        Если проект ключа известен — суммируется расход ВСЕХ ключей этого
        проекта (квота Google выдаётся проекту, а не ключу). Если нет — только
        этого ключа. Суммируем по key_id, а не по колонке project: строки,
        записанные до появления колонки, тоже должны учитываться.
        """
        if self.conn is None:
            return 0
        project = self._projects.get(key)
        if project:
            key_ids = [
                self.key_id(k) for k, p in self._projects.items() if p == project
            ]
        else:
            key_ids = [self.key_id(key)]
        if not key_ids:
            return 0
        placeholders = ",".join("?" * len(key_ids))
        now_ts = config.now_ts()
        quota_day = _quota_day_start(now_ts)
        # Legacy-строки (ts IS NULL) хранят только UTC-дату. Берём все, чья
        # UTC-дата не старше UTC-суток начала суток квоты: ошибка идёт в
        # сторону преувеличения расхода (см. докстринг).
        legacy_day = _day_start(quota_day)
        try:
            row = self.conn.execute(
                f"SELECT COALESCE(SUM(units),0) AS u FROM quota_log "
                f"WHERE key_id IN ({placeholders}) "
                f"AND (ts >= ? OR (ts IS NULL AND date >= ?))",
                (*key_ids, quota_day, legacy_day),
            ).fetchone()
            # Значение читается ПОЗИЦИОННО (row[0]), а не по имени колонки:
            # обычный sqlite3.connect() без row_factory даёт tuple, и row["u"]
            # падал бы TypeError'ом. Возврат тоже внутри try — любой сбой
            # чтения (в т.ч. нечисловое значение) обязан дать warning + 0, а
            # не уронить сбор.
            # TODO(debt-D-11): закрыт 15.09.2026 (docs/TECH-DEBT.md).
            return int(row[0]) if row else 0
        except Exception as exc:
            # Нет таблицы/колонок или иной сбой чтения: предохранитель не
            # может посчитать расход. Молча выключать его нельзя — пишем
            # предупреждение с текстом исключения и возвращаем 0. Поведение
            # прежнее: при недоступном учёте работа не останавливается.
            log.warning(
                "quota_log: не удалось прочитать расход за сутки (%s) — "
                "считаю 0",
                exc,
            )
            return 0

    def quota_exceeded(self, key: str) -> bool:
        """Достигнут ли порог остановки (лимит минус запас)."""
        return self.spent_today(key) >= self.quota_stop_threshold(key)

    def quota_remaining(self) -> int:
        """Суммарный остаток суточной квоты (units) по проектам.

        Квота Google выдаётся ПРОЕКТУ, поэтому ключи одного проекта считаются
        один раз. Берётся максимум по ключам проекта: клиент ротирует ключи и
        остановится, когда исчерпаются ВСЕ, а не первый. Остаток не может быть
        отрицательным.
        """
        self._purge_stale_quota_blocks()
        best: dict[str, int] = {}
        for key in self._keys:
            ref = self._projects.get(key) or self.key_id(key)
            remaining = max(0, self.quota_stop_threshold(key) - self.spent_today(key))
            if ref not in best or remaining > best[ref]:
                best[ref] = remaining
        return int(sum(best.values()))

    # --- предохранитель поисковой квоты (search.list) ---------------------

    def _key_ids_for(self, key: str) -> list[str]:
        """key_id ключа или всех ключей его проекта (если проект известен).

        Квота Google считается на проект, поэтому для ключа с проектом
        берём все ключи этого проекта; для ключа без проекта — только его.
        """
        project = self._projects.get(key)
        if project:
            return [self.key_id(k) for k, p in self._projects.items() if p == project]
        return [self.key_id(key)]

    def search_stop_threshold(self) -> int:
        """Порог остановки поиска: лимит вызовов минус резерв.

        Отдельный жёсткий лимит Google — 100 вызовов search.list в сутки на
        проект. Порог считается в одном месте (рядом с
        ``quota_stop_threshold``): 100 - QUOTA_SEARCH_RESERVE (15) = 85.
        """
        return (config.QUOTA_SEARCH_LIMIT_PER_PROJECT
                - config.QUOTA_SEARCH_RESERVE)

    def search_calls_today(self, key: str) -> int:
        """Число попыток search.list за текущие сутки квоты (Pacific Time).

        Для ключа с известным проектом — сумма по всем ключам проекта, для
        ключа без проекта — по ключу (прежнее поведение). Считаются строки
        quota_log с endpoint='search'. Legacy-строки (ts IS NULL) учитываются
        по UTC-дате так же, как в ``spent_today``.
        """
        if self.conn is None:
            return 0
        key_ids = self._key_ids_for(key)
        if not key_ids:
            return 0
        placeholders = ",".join("?" * len(key_ids))
        now_ts = config.now_ts()
        quota_day = _quota_day_start(now_ts)
        legacy_day = _day_start(quota_day)
        try:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM quota_log "
                f"WHERE key_id IN ({placeholders}) AND endpoint='search' "
                f"AND (ts >= ? OR (ts IS NULL AND date >= ?))",
                (*key_ids, quota_day, legacy_day),
            ).fetchone()
            # Позиционное чтение row[0] и возврат внутри try: подключение без
            # row_factory (обычный sqlite3.connect()) давало tuple, и row["n"]
            # падал TypeError'ом, роняя сбор. Теперь сбой чтения — warning + 0.
            # TODO(debt-D-11): закрыт 15.09.2026 (docs/TECH-DEBT.md).
            return int(row[0]) if row else 0
        except Exception as exc:
            # Нет таблицы/колонок или иной сбой чтения: счётчик поиска
            # недоступен. Молча выключать предохранитель нельзя — пишем
            # предупреждение и возвращаем 0 (прежнее поведение: недоступный
            # учёт не останавливает работу).
            log.warning(
                "quota_log: не удалось прочитать число поисков за сутки (%s) — "
                "считаю 0",
                exc,
            )
            return 0

    def search_guard_exceeded(self, key: str) -> bool:
        """Достигнут ли порог по числу вызовов search.list."""
        return self.search_calls_today(key) >= self.search_stop_threshold()

    def search_guard_event(self, key: str) -> SearchQuotaGuard:
        """Собрать событие-остановку поиска с понятным текстом."""
        project = self._projects.get(key)
        calls = self.search_calls_today(key)
        limit = config.QUOTA_SEARCH_LIMIT_PER_PROJECT
        reserve = config.QUOTA_SEARCH_RESERVE
        if project:
            where = f"проект {project}"
        else:
            where = f"ключ {self.key_id(key)}"
        message = (
            f"search quota guard: {where}: {calls}/{limit} "
            f"(лимит {limit}, резерв {reserve})"
        )
        return SearchQuotaGuard(message, project=project, calls=calls,
                                threshold=self.search_stop_threshold())

    def _select_key(self, search: bool = False) -> str:
        """Выбрать следующий годный ключ (с ленивой проверкой).

        search=True — вызов поиска: ключи, у которых достигнут лимит вызовов
        search.list (по проекту или по ключу), пропускаются. Если поиск
        заблокирован у всех годных ключей, поднимается SearchQuotaGuard —
        вызывающий код не теряет прогон, а продолжает бесплатными шагами.
        """
        n = len(self._keys)
        if n == 0:
            raise YouTubeError(0, {"error": {"message": "no api keys"}}, "")
        quota_blocked: list[str] = []
        search_blocked: list[str] = []
        for _ in range(n):
            key = self._keys[self._cursor % n]
            self._cursor += 1
            if key in self._bad:
                continue
            if self._is_quota_blocked(key):
                quota_blocked.append(key)
                continue
            if search and self.search_guard_exceeded(key):
                # Порог по вызовам поиска: не отправляем и не тратим квоту.
                # Сначала проверяем поисковый лимит: он и есть цель вызова,
                # и его сообщение понятнее владельцу.
                search_blocked.append(key)
                continue
            if self.quota_exceeded(key):
                # Достигнут порог (лимит минус запас) по проекту или по ключу:
                # исключаем ключ только до конца текущих суток, при смене суток
                # он снова доступен (см. _is_quota_blocked), пробуем следующий.
                self._block_quota(key)
                quota_blocked.append(key)
                continue
            if key not in self._valid and not self._probe(key):
                continue
            return key
        if search_blocked:
            raise self.search_guard_event(search_blocked[0])
        if quota_blocked:
            # Перечисляем ВСЕ заблокированные ключи (в порядке исходного
            # списка ключей), а не только первый: пороги и лимиты у ключей
            # разных проектов (или без проекта) различаются. Одна строка на
            # ключ, без словарей и JSON.
            blocked_set = set(quota_blocked)
            lines = [
                f"{self.key_id(k)}: threshold={self.quota_stop_threshold(k)} "
                f"units of limit {self.quota_limit(k)}, "
                f"reserve={config.QUOTA_SAFETY_RESERVE}"
                for k in self._keys
                if k in blocked_set
            ]
            raise YouTubeError(
                403,
                {"error": {
                    "message": (
                        "daily quota safeguard reached:\n" + "\n".join(lines)
                    ),
                    "errors": [{"reason": "quotaExceeded"}],
                }},
                "",
            )
        raise YouTubeError(
            403,
            {"error": {"message": "no usable api keys",
                       "errors": [{"reason": "keyInvalid"}]}},
            "",
        )

    def _probe(self, key: str) -> bool:
        """Проверить ключ через videos.list по одному известному видео."""
        status, body = self._raw_get(
            key, "videos", {"part": "statistics", "id": KNOWN_VIDEO_ID}
        )
        self._log_quota(key, config.COST_VIDEOS, "videos/probe")
        if status == 200:
            self._valid.add(key)
            return True
        reason = _error_reason(body)
        if status in (400, 403) and reason in RATE_REASONS:
            # Лимит частоты не делает ключ битым.
            return True
        if status in (400, 403) and reason in QUOTA_REASONS:
            # Квоту исчерпал ключ/проект: исключаем до конца текущих суток.
            self._block_quota(key)
            return False
        if status in (400, 403):
            self._bad.add(key)
            return False
        if status == 429:
            return True
        raise YouTubeError(status, body, "videos/probe")

    # --- HTTP -------------------------------------------------------------

    def _raw_get(self, key: str, endpoint: str, params: dict) -> tuple[int, Any]:
        """GET-запрос с паузой. Возвращает (status, body)."""
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call_ts)
            if wait > 0:
                self._sleep(wait)
            url = API_BASE + endpoint.lstrip("/")
            query = dict(params)
            query["key"] = key
            try:
                resp = self.session.get(url, params=query, timeout=self.timeout)
            except Exception as exc:  # сетевая ошибка — тоже исключение, не None
                self._last_call_ts = time.monotonic()
                raise YouTubeError(0, f"network error: {exc}", endpoint) from exc
            self._last_call_ts = time.monotonic()
            status = int(getattr(resp, "status_code", 0) or 0)
            try:
                body = resp.json()
            except Exception:
                body = {"raw": getattr(resp, "text", "")}
            return status, body

    def _log_quota(self, key: str, units: int, endpoint: str) -> None:
        if self.conn is None:
            return
        now_ts = config.now_ts()
        # date — UTC-начало суток (формат сводок), ts — точный момент записи
        # (для границ суток квоты по Pacific Time).
        db.log_quota(
            self.conn,
            _day_start(now_ts),
            self.key_id(key),
            1,
            units,
            endpoint,
            self._projects.get(key),
            ts=now_ts,
        )

    # --- ядро -------------------------------------------------------------

    def api_call(
        self, endpoint: str, params: dict, parts_cost: int,
        video_level_403: bool = False,
    ) -> dict:
        """Вызов API. Стоимость в units задаётся явно.

        video_level_403=True — 403 на этом эндпоинте означает свойство видео
        (например, отключённые комментарии), а не смерть ключа: такой ответ
        сразу поднимается исключением, ключ остаётся рабочим. Исключение —
        403 с признаком квоты (quotaExceeded/dailyLimitExceeded): это
        по-прежнему ошибка уровня ключа.
        """
        last: tuple[int, Any] | None = None
        for _ in range(self._max_attempts):
            # Для search.list включается отдельный предохранитель по числу
            # вызовов на проект: если порог достигнут, ключ не выбран, вызов не
            # уходит и units не тратятся (поднимается SearchQuotaGuard).
            key = self._select_key(search=(endpoint == "search"))
            status, body = self._raw_get(key, endpoint, params)
            self._log_quota(key, parts_cost, endpoint)
            if status == 200:
                self._valid.add(key)
                return body
            reason = _error_reason(body)
            last = (status, body)
            if status == 429 or reason in RATE_REASONS:
                # Пауза и повтор тем же кругом.
                self._sleep(self.retry_delay)
                continue
            if status in (400, 403) and reason in QUOTA_REASONS:
                # Ключ/проект исчерпал квоту: до конца текущих суток не берём,
                # при смене суток исключение снимается.
                self._block_quota(key)
                continue
            if video_level_403 and status == 403:
                # 403 уровня видео (commentThreads): ключ годен, ротацию не
                # запускаем, отдаём ошибку наверх сразу.
                raise YouTubeError(status, body, endpoint)
            if status in (400, 403) and reason in KEY_DEAD_REASONS:
                self._bad.add(key)
                continue
            if status == 403 and reason in VIDEO_LEVEL_REASONS:
                # Ошибка уровня видео (например, commentsDisabled): ключ
                # годен, ротация не нужна — отдаём ошибку наверх сразу.
                raise YouTubeError(status, body, endpoint)
            if status == 403:
                # Неизвестный 403 — исключаем ключ и пробуем следующий.
                self._bad.add(key)
                continue
            raise YouTubeError(status, body, endpoint)

        code = last[0] if last else 0
        body = last[1] if last else {"error": {"message": "call failed"}}
        raise YouTubeError(code, body, endpoint)

    # --- высокоуровневые функции ------------------------------------------

    def videos_by_ids(
        self, ids: Sequence[str], parts: str = "snippet,statistics,contentDetails"
    ) -> list[dict]:
        """Метаданные видео пачками по 50 (1 unit за вызов)."""
        out: list[dict] = []
        for batch in _chunks(list(ids), 50):
            body = self.api_call(
                "videos", {"part": parts, "id": ",".join(batch)},
                config.COST_VIDEOS,
            )
            out.extend(body.get("items", []))
        return out

    def channels_by_ids(
        self, ids: Sequence[str], parts: str = "snippet,statistics,contentDetails"
    ) -> list[dict]:
        """Метаданные каналов пачками по 50 (1 unit за вызов)."""
        out: list[dict] = []
        for batch in _chunks(list(ids), 50):
            body = self.api_call(
                "channels", {"part": parts, "id": ",".join(batch)},
                config.COST_CHANNELS,
            )
            out.extend(body.get("items", []))
        return out

    def search_videos(
        self,
        query: str,
        published_after: int | str | None = None,
        order: str = "viewCount",
        max_pages: int = 1,
        extra: dict | None = None,
    ) -> list[dict]:
        """Поиск видео (100 units за страницу)."""
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "order": order,
            "maxResults": 50,
        }
        if query:
            params["q"] = query
        after = _rfc3339(published_after)
        if after is not None:
            params["publishedAfter"] = after
        params.update(config.search_params(query))
        if extra:
            params.update(extra)

        out: list[dict] = []
        page_token = None
        for _ in range(max(1, int(max_pages))):
            page = dict(params)
            if page_token:
                page["pageToken"] = page_token
            body = self.api_call("search", page, config.COST_SEARCH)
            out.extend(body.get("items", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return out

    def playlist_items(self, playlist_id: str, max_results: int = 50) -> list[dict]:
        """Элементы плейлиста (1 unit за вызов), с пейджингом до max_results."""
        out: list[dict] = []
        page_token = None
        remaining = max(1, int(max_results))
        while remaining > 0:
            params: dict[str, Any] = {
                "part": "contentDetails,snippet",
                "playlistId": playlist_id,
                "maxResults": min(50, remaining),
            }
            if page_token:
                params["pageToken"] = page_token
            body = self.api_call("playlistItems", params, config.COST_PLAYLIST_ITEMS)
            items = body.get("items", [])
            out.extend(items)
            remaining -= len(items)
            page_token = body.get("nextPageToken")
            if not page_token or not items:
                break
        return out

    def search_channels(
        self,
        query: str,
        max_results: int = 50,
        language: str | None = None,
        region: str | None = None,
    ) -> list[dict]:
        """Поиск каналов напрямую (search?type=channel, 100 units за страницу)."""
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": "channel",
            "order": "relevance",
            "maxResults": min(50, max(1, int(max_results))),
        }
        if language:
            params["relevanceLanguage"] = language
        if region:
            params["regionCode"] = region

        out: list[dict] = []
        page_token = None
        remaining = max(1, int(max_results))
        while remaining > 0:
            page = dict(params)
            page["maxResults"] = min(50, remaining)
            if page_token:
                page["pageToken"] = page_token
            body = self.api_call("search", page, config.COST_SEARCH)
            items = body.get("items", [])
            out.extend(items)
            remaining -= len(items)
            page_token = body.get("nextPageToken")
            if not page_token or not items:
                break
        return out

    def chart_videos(
        self, region: str, category_id: str | int | None = None,
        max_results: int = 50,
    ) -> list[dict]:
        """Чарт популярного в регионе/категории (videos?chart=mostPopular, 1 unit)."""
        params: dict[str, Any] = {
            "part": "snippet,statistics,contentDetails",
            "chart": "mostPopular",
            "regionCode": region,
            "maxResults": min(50, max(1, int(max_results))),
        }
        if category_id is not None and str(category_id) != "":
            params["videoCategoryId"] = str(category_id)
        body = self.api_call("videos", params, config.COST_VIDEOS)
        return body.get("items", [])

    def playlists_by_channel(
        self, channel_id: str, max_results: int = 50
    ) -> list[dict]:
        """Плейлисты канала (playlists.list, 1 unit за вызов), с пейджингом."""
        out: list[dict] = []
        page_token = None
        remaining = max(1, int(max_results))
        while remaining > 0:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "channelId": channel_id,
                "maxResults": min(50, remaining),
            }
            if page_token:
                params["pageToken"] = page_token
            body = self.api_call("playlists", params, config.COST_PLAYLIST_ITEMS)
            items = body.get("items", [])
            out.extend(items)
            remaining -= len(items)
            page_token = body.get("nextPageToken")
            if not page_token or not items:
                break
        return out

    def channels_by_handle(
        self, handles: Sequence[str],
        parts: str = "snippet,statistics,contentDetails",
    ) -> list[dict]:
        """Резолв handle в канал (channels?forHandle).

        Живой вызов показал: API принимает ровно одно значение forHandle за
        запрос (несколько значений возвращают только последнее), поэтому
        каждый handle — отдельный вызов по 1 unit. Параметр parts_cost=1.
        """
        out: list[dict] = []
        clean = [str(h).strip() for h in handles if str(h).strip()]
        for handle in clean:
            body = self.api_call(
                "channels",
                {"part": parts, "forHandle": handle},
                config.COST_CHANNELS,
            )
            out.extend(body.get("items", []))
        return out

    def comment_threads(self, video_id: str, max_results: int = 20) -> list[dict]:
        """Верхние комментарии видео (commentThreads.list, 1 unit за вызов).

        Возвращает плоский список словарей: comment_id, author, text, likes,
        published_at (unixtime, из topLevelComment.snippet.publishedAt).

        Любой 403 на commentThreads.list — ошибка уровня видео, а не ключа
        (см. COMMENT_403_REASONS): поднимаем YouTubeError с кодом 403, ключ
        остаётся рабочим и ротация не запускается. Решение о пропуске видео
        и записи статуса принимает вызывающий код (tuber.comments).
        """
        params: dict[str, Any] = {
            "part": "snippet",
            "videoId": video_id,
            "order": "relevance",
            "textFormat": "plainText",
            "maxResults": min(100, max(1, int(max_results))),
        }
        body = self.api_call(
            "commentThreads", params, config.COST_COMMENT_THREADS,
            video_level_403=True,
        )

        # Хелпер разбора RFC3339 живёт в tuber.collect; импорт локальный,
        # чтобы не создавать циклическую зависимость (collect импортирует yt).
        from .collect import parse_iso_utc

        out: list[dict] = []
        for item in body.get("items", []):
            top = (item.get("snippet") or {}).get("topLevelComment") or {}
            sn = top.get("snippet") or {}
            out.append({
                "comment_id": top.get("id") or item.get("id"),
                "author": sn.get("authorDisplayName"),
                "text": sn.get("textDisplay") or sn.get("textOriginal"),
                "likes": sn.get("likeCount"),
                "published_at": parse_iso_utc(sn.get("publishedAt")),
            })
        return out
