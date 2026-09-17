"""Адаптер хранения X-кода к единому ядру монорепозитория (ТЗ-3).

Зачем он нужен
--------------
Код ``tuber-x`` (``collect``, ``channels``, ``scoring``, ``report``, …) писал
прямо в таблицы ``tuber_x.db`` (``accounts``, ``posts``, ``scores``, …) плоским
SQL. В монорепозитории таких таблиц нет: есть единое ядро ``source`` /
``content`` / ``metric_snapshot`` / ``score`` / ``candidate`` / … (см.
:mod:`tuber.core.schema`). Чтобы НЕ переписывать логику X, этот модуль играет
роль прежнего ``tuber_x/db.py``:

1. отдаёт те же функции (``connect``, ``init_db``, ``log_run``, ``start_run``,
   ``iso``, ``post_author``, …) и те же константы (``SCHEMA_VERSION``);
2. на каждом соединении заводит ВРЕМЕННЫЕ (``TEMP``) представления с именами и
   колонками legacy-таблиц (``accounts``, ``posts``, ``cursors``, …). Поэтому
   плоский SQL X-кода и перенесённых тестов продолжает работать без правок:
   он видит привычные таблицы, но данные приходят из ядра;
3. временные ``INSTEAD OF`` триггеры принимают legacy-запись
   (INSERT/UPDATE/DELETE) и раскладывают её по таблицам ядра.

Соответствие legacy → ядро (ТЗ-3 §1):

===========================  =====================================================
legacy                       ядро
===========================  =====================================================
``accounts``                 ``source`` (``platform='x'``, ``x_id``→``external_id``)
``posts``                    ``content`` (``tweet_id``→``external_id``, ``kind='post'``)
``post_metrics_history``     ``metric_snapshot``
``candidates``               ``candidate``
``classified``               ``classify_cache`` (+ проекция в ``classification``)
``stories`` / ``story_posts``  ``story`` / ``story_member``
``scores``                   ``score`` (оси → ``axes_json``)
``cursors``                  ``cursor``
``instances``                ``transport_instance``
``requests``                 ``transport_request``
``runs`` / ``run_log``       ``run`` / ``run_log``
``metrics_daily``            ``metrics_daily``
``classify_daily``           ``classify_daily``
``report_texts``             ``report_text``
``blocklist``                ``blocklist`` (``platform='x'``)
``darks``                    ``source.meta_json`` → ``$.darks``
===========================  =====================================================

Формат дат
----------
Единое ядро хранит даты как ``YYYY-MM-DD HH:MM:SS`` (пробел), legacy-код X — как
``YYYY-MM-DDTHH:MM:SS`` (``T``). Представления отдают значения в формате X
(``replace(col,' ','T')``), триггеры пишут в ядро в его формате
(``replace(param,'T',' ')``). Так диапазонные сравнения внутри X-кода
(``published_at_utc >= db.iso(...)``) остаются корректными без единой правки
логики.

Уроки ТЗ-2/ТЗ-2c (см. TECH-DEBT D-21/D-22)
------------------------------------------
* ``connect()`` сначала гарантирует схему ядра (:func:`migrate_schema`), и
  только потом ставит слой совместимости — иначе «no such table: main.source».
* Представления ОДНОтабличные: у ядра есть денормализованные ``external_id`` /
  ``platform`` рядом с данными, поэтому ``content``/``score``/``metric_snapshot``
  читаются напрямую, без ``JOIN`` к ``content``. SQLite разворачивает такие
  представления на правой стороне ``LEFT JOIN`` (``SEARCH ... USING COVERING
  INDEX``), а не материализует (``MATERIALIZE``).
* Запись в ядро из функций адаптера идёт через
  :func:`tuber.core.db.write_tx` (``BEGIN IMMEDIATE`` + повтор при
  ``database is locked``).
* ``ensure_planner_stats()`` — ``ANALYZE`` один раз: без статистики
  планировщик берёт полный перебор (D-21).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from tuber.core import db as core_db
from tuber.core import sqlcompat as core_sqlcompat
from tuber.core import storage, timeutil, urls
from tuber.core import schema as core_schema
from tuber.core.schema import migrate_schema

from . import config

log = logging.getLogger(__name__)

# Legacy-имена, совпадающие с именами таблиц ядра (ТЗ-3c). Такие представления
# создаются под «своим» именем (см. :mod:`tuber.core.sqlcompat`), иначе TEMP-схема
# затеняет таблицу ядра и DML из INSTEAD OF-триггера не доходит до ядра.
X_COMPAT_VIEWS: dict[str, str] = {
    "blocklist": "x_blocklist",
    "run_log": "x_run_log",
    "metrics_daily": "x_metrics_daily",
    "classify_daily": "x_classify_daily",
}

# Версия legacy-схемы (последняя, ТЗ «починка виральности»: posts.author_handle).
# В монорепозитории номера версий живут в ``schema_meta`` ядра (см.
# :mod:`tuber.core.schema`); константа сохранена как часть прежнего API
# ``tuber_x.db`` — её читают перенесённые тесты.
SCHEMA_VERSION = 8

# Максимальное число попыток при ``database is locked`` (как в ядре).
WRITE_RETRIES = 5
WRITE_BASE_DELAY = 0.5


# ---------------------------------------------------------------------------
# Время: формат X (``T``) ⇄ формат ядра (пробел)
# ---------------------------------------------------------------------------

def _t(col: str) -> str:
    """SQL-выражение: значение ядра → формат X (``T``)."""
    return f"replace({col}, ' ', 'T')"


def _c(param: str) -> str:
    """SQL-выражение: значение X (``T``) → формат ядра (пробел)."""
    return f"CASE WHEN {param} IS NULL THEN NULL ELSE replace({param}, 'T', ' ') END"


def iso(dt) -> str:
    """ISO-8601 в UTC, секундная точность, формат legacy X (``T``)."""
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return iso(utcnow())


def parse_iso(value):
    """Разбор ISO-строки из БД. Возвращает aware-datetime или None.

    Понимает оба формата (``T`` и пробел): адаптер отдаёт ``T``, но старые
    значения в ``source.meta_json`` могли быть записаны любым из них.
    """
    if not value:
        return None
    v = str(value).strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_iso() -> str:
    """Время ядра (``YYYY-MM-DD HH:MM:SS``) — для прямых записей в ядро."""
    return timeutil.iso_now()


def post_author(post):
    """АВТОР поста: ``COALESCE(author_handle, owner_handle, acc_handle)``.

    ``author_handle`` — реальный автор из CDN (``screen_name``), ``owner_handle``
    — владелец ЛЕНТЫ (он же автор только при сборе по аккаунту, поэтому служит
    фолбэком для старых строк), ``acc_handle`` — последний фолбэк для выборок с
    ``JOIN accounts``. Возвращает хендл без ведущего ``@`` или None.
    Работает и со ``sqlite3.Row``, и со словарём.
    """
    if post is None:
        return None
    for key in ("author_handle", "owner_handle", "acc_handle"):
        try:
            value = post[key]
        except (KeyError, IndexError, TypeError):
            value = None
        if value:
            return str(value).lstrip("@")
    return None


# ---------------------------------------------------------------------------
# Подключение
# ---------------------------------------------------------------------------

class _RetryConnection(core_sqlcompat.CompatConnection):
    """Соединение с повтором записи при ``database is locked``.

    В единой базе пишут три платформы из разных процессов; WAL допускает одного
    писателя. Код X пишет плоским SQL (мимо ``write_tx``), поэтому повтор при
    блокировке встроен в соединение: пауза 0.5 → 1 → 2 → 4 c. ``busy_timeout``
    ядра (15 с) закрывает обычные случаи, повтор — редкие длинные транзакции
    соседей (миграция, ``ANALYZE``).

    Плюс трансляция legacy-имён конфликтующих таблиц (ТЗ-3c, см.
    :class:`tuber.core.sqlcompat.CompatConnection`).
    """

    collision_map = X_COMPAT_VIEWS

    def execute(self, sql, parameters=()):  # type: ignore[override]
        sql = self._translate(sql)
        delay = WRITE_BASE_DELAY
        for attempt in range(WRITE_RETRIES):
            try:
                return sqlite3.Connection.execute(self, sql, parameters)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if ("locked" not in msg and "busy" not in msg) or attempt == WRITE_RETRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2
        raise AssertionError("недостижимо")


def connect(path=None, *, readonly=False, check_same_thread=True,
            install=True) -> sqlite3.Connection:
    """Открыть ЕДИНУЮ базу ядра и поставить слой совместимости с legacy X.

    Схема ядра гарантируется здесь же (:func:`migrate_schema`, идемпотентно и
    дёшево: ``CREATE IF NOT EXISTS``), как в прежнем ``tuber_x.db.connect``,
    который сам создавал таблицы. Без этого шага любой запрос падал бы на
    «no such table: main.source» (грабли ТЗ-2).

    Модель транзакций сохранена legacy-шной: Python-овские неявные транзакции
    + явные ``commit()``/``rollback()``. Их использует перенесённый код
    (``feeds.import_candidates(dry=True)`` откатывает импорт), поэтому
    ``isolation_level`` НЕ выставляется в ``None``.
    """
    p = str(path or config.DB_PATH)
    core_sqlcompat.ensure_supported_sqlite()
    if not readonly:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True,
                               timeout=config.BUSY_TIMEOUT_SEC, check_same_thread=check_same_thread,
                               factory=_RetryConnection)
    else:
        conn = sqlite3.connect(p, timeout=config.BUSY_TIMEOUT_SEC,
                               check_same_thread=check_same_thread,
                               factory=_RetryConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=%d" % int(config.BUSY_TIMEOUT_SEC * 1000))
    if not readonly:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
    # Схема ядра ДО слоя совместимости (ТЗ-3 §1.1 п.1). Если база уже на текущей
    # версии схемы — писать не нужно: ``init_schema`` тянет UPSERT-ы в
    # ``platform``/``schema_meta``, а это лишний захват блокировки записи на
    # КАЖДОМ соединении. Соединений в процессе несколько (роутер каналов,
    # брокер, основной CLI), и в WAL-базе второй писатель ждал бы первого.
    if not _schema_is_current(conn):
        migrate_schema(conn)
    if install:
        install_compat(conn)
    return conn


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """База уже на текущей версии схемы ядра (и денормализации)?

    Дешёвая проверка (два чтения ``schema_meta``) вместо полного
    :func:`migrate_schema` на каждом соединении. Любая ошибка (нет таблицы,
    чужая база) — «нет, нужно мигрировать».
    """
    try:
        row = conn.execute(
            "SELECT value FROM main.schema_meta WHERE key='version'").fetchone()
        if row is None or row[0] != core_schema.SCHEMA_VERSION:
            return False
        row = conn.execute(
            "SELECT value FROM main.schema_meta WHERE key='denorm_version'").fetchone()
        return row is not None and row[0] == core_schema.DENORM_VERSION
    except sqlite3.DatabaseError:
        return False


def init_db(path=None) -> sqlite3.Connection:
    """Р2/Р1: создать контур. Идемпотентно (замена legacy ``init_db``)."""
    conn = connect(path)
    migrate(conn)
    ensure_planner_stats(conn)
    conn.commit()
    return conn


def migrate(con: sqlite3.Connection) -> None:
    """Идемпотентная миграция схемы.

    В legacy это был набор ``ALTER TABLE`` по ``PRAGMA user_version``. Теперь
    схема принадлежит ядру, поэтому миграция одна: :func:`migrate_schema`
    (аддитивные колонки → таблицы/индексы/представления/триггеры →
    денормализация). Повторный вызов не меняет ничего.

    Перед миграцией слой совместимости снимается: TEMP-представления
    затеняют одноимённые таблицы ядра (``run_log``), а ядро внутри
    ``migrate_schema`` создаёт по ним индексы и триггеры — «views may not be
    indexed». После миграции слой ставится заново.
    """
    con.execute("PRAGMA foreign_keys=ON")
    uninstall_compat(con)
    try:
        migrate_schema(con)
    finally:
        install_compat(con)


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Транзакция записи ядра: ``BEGIN IMMEDIATE`` с повтором при блокировке.

    Если неявная транзакция Python уже открыта (код X пишет плоским SQL и
    коммитит сам), вложенный ``BEGIN`` невозможен — тогда просто отдаём
    соединение как есть: блокировка уже взята.
    """
    if conn.in_transaction:
        yield conn
        return
    with core_db.write_tx(conn):
        yield conn


def ensure_planner_stats(conn: sqlite3.Connection) -> bool:
    """Собрать статистику планировщика (``ANALYZE``), если её ещё нет.

    Без ``sqlite_stat1`` SQLite оценивает селективность «на глаз» и на
    представлениях совместимости уходит в полный перебор (D-21: отчёт
    YouTube шёл 5 мин вместо 15 с). Идемпотентно: если статистика есть — no-op.
    """
    row = conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table' AND name='sqlite_stat1'"
    ).fetchone()
    if row is not None:
        return False
    with write_tx(conn):
        conn.execute("ANALYZE")
    log.info("ANALYZE: собрана статистика планировщика (см. TECH-DEBT D-21)")
    return True


# ---------------------------------------------------------------------------
# Защита рабочей базы (ТЗ-6 задача 5, сохранено как есть)
# ---------------------------------------------------------------------------

def production_path() -> str:
    """Абсолютный (realpath) путь рабочей базы из конфига."""
    return os.path.realpath(config.DB_PATH)


def is_production_db(path) -> bool:
    """True, если путь указывает на рабочую БД (по realpath)."""
    if not path:
        return False
    try:
        return os.path.realpath(str(path)) == production_path()
    except OSError:
        return False


def ensure_not_production_db(path, action="запись") -> str:
    """Запрет писать в рабочую БД из вспомогательных путей (ТЗ-6 задача 5)."""
    if is_production_db(path):
        raise RuntimeError(
            f"отказ: путь {path!r} совпадает с рабочей БД — {action} в неё запрещена"
            " (все изменяющие операции выполняются на копии)")
    return str(path)


# ---------------------------------------------------------------------------
# Хелперы счётчиков (обход отсутствия rowcount у представлений)
# ---------------------------------------------------------------------------

def changes_since(conn: sqlite3.Connection, before: int) -> int:
    """Сколько строк фактически изменилось с момента ``before``.

    Зачем. ``cursor.rowcount`` (он же ``sqlite3_changes()``) для DM-команд по
    ПРЕДСТАВЛЕНИЮ всегда 0: изменения делает ``INSTEAD OF``-триггер. Зато
    ``Connection.total_changes`` считает и строки, изменённые триггерами.
    Поэтому «сколько записала последняя команда» читается как разность
    ``total_changes`` до и после неё. Это единственная правка в местах, где
    legacy-код опирался на ``rowcount`` (см. TECH-DEBT D-23).
    """
    return conn.total_changes - before


def view_delete(conn: sqlite3.Connection, delete_sql: str, params=()) -> int:
    """``DELETE`` по представлению с честным числом удалённых строк.

    Выполняет ту же команду, но сначала считает подходящие строки
    (``DELETE FROM X`` → ``SELECT COUNT(*) FROM X``).
    """
    count_sql = re.sub(r"^\s*DELETE\s+FROM", "SELECT COUNT(*) FROM", delete_sql,
                       count=1, flags=re.IGNORECASE)
    n = conn.execute(count_sql, params).fetchone()[0]
    conn.execute(delete_sql, params)
    return int(n or 0)


def last_insert_id(conn: sqlite3.Connection, kind: str = "story"):
    """id последней строки, вставленной через представление (``lastrowid`` = 0).

    Триггер представления пишет настоящий rowid в TEMP-таблицу
    ``x_last_insert``; здесь он читается.
    """
    row = conn.execute("SELECT id FROM temp.x_last_insert WHERE k=?", (kind,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# run / run_log / instances: прежний API tuber_x.db
# ---------------------------------------------------------------------------

def log_run(con, level, msg, handle=None, run_id=None):
    # ``main.`` обязателен: без него имя разрешается в TEMP-представление
    # ``run_log`` (legacy-форма с колонкой ``handle``), а не в таблицу ядра.
    # ``platform='x'`` — принадлежность строки (D-24): нужна и для строк ВНЕ
    # прогона (``run_id IS NULL``), которые legacy писал в общую таблицу.
    con.execute(
        "INSERT INTO main.run_log (run_id, platform, ts, level, ref, msg) VALUES (?,?,?,?,?,?)",
        (run_id, "x", now_iso(), level, handle, msg),
    )


def mark_reserve_active(con, when=None, host=None):
    """Отметить, что аварийный резерв x_ssr активен (Nitter недоступен)."""
    host = host or config.XSSR_HOST
    stamp = when or utcnow_iso()
    with write_tx(con):
        con.execute(
            "INSERT OR IGNORE INTO main.transport_instance (platform, host, healthy)"
            " VALUES ('x', ?, NULL)", (host,))
        con.execute(
            "UPDATE main.transport_instance"
            " SET meta_json=json_set(COALESCE(meta_json,'{}'), '$.reserve_since', ?)"
            " WHERE platform='x' AND host=?"
            " AND json_extract(COALESCE(meta_json,'{}'), '$.reserve_since') IS NULL",
            (stamp, host))
    return stamp


def clear_reserve_active(con, host=None):
    """Снять признак активности резерва (Nitter снова отдаёт фиды)."""
    host = host or config.XSSR_HOST
    with write_tx(con):
        con.execute(
            "UPDATE main.transport_instance"
            " SET meta_json=json_set(COALESCE(meta_json,'{}'), '$.reserve_since', NULL)"
            " WHERE platform='x' AND host=?", (host,))


def reserve_active_since(con, host=None):
    """Момент включения резерва (aware datetime) или None."""
    host = host or config.XSSR_HOST
    row = con.execute(
        "SELECT json_extract(COALESCE(meta_json,'{}'), '$.reserve_since') AS reserve_since"
        " FROM transport_instance WHERE platform='x' AND host=?", (host,)).fetchone()
    if not row or not row["reserve_since"]:
        return None
    return parse_iso(row["reserve_since"])


def start_run(con, mode, note=None):
    with write_tx(con):
        cur = con.execute(
            "INSERT INTO main.run (platform, started_at, ok_count, fail_count, items_new,"
            " items_upd, errors, note, mode) VALUES ('x',?,0,0,0,0,0,?,?)",
            (now_iso(), note, mode))
    return cur.lastrowid


def finish_run(con, run_id, *, accounts_ok=0, accounts_fail=0, posts_new=0,
               posts_upd=0, errors=0, note=None):
    if run_id is None:
        return
    with write_tx(con):
        con.execute(
            "UPDATE main.run SET finished_at=?, ok_count=?, fail_count=?, items_new=?,"
            " items_upd=?, errors=?, note=COALESCE(?, note) WHERE id=?",
            (now_iso(), accounts_ok, accounts_fail, posts_new, posts_upd, errors,
             note, run_id))


# ===========================================================================
# Слой совместимости: TEMP-представления и INSTEAD OF триггеры
# ===========================================================================

# Мэппинг полей ``accounts`` ⇄ ``source``:
#   колонки ядра  : x_id→external_id, last_success_at→last_synced_at,
#                   added_at→first_seen_at, source_type→source_kind
#   meta_json ядра: last_attempt_at, ai_density_src, provisional_since,
#                   reject_reason, last_reject_at, promo_path
# Мэппинг ``posts`` ⇄ ``content``: tweet_id→external_id, account_id→source_id,
# is_retweet→is_repost, а likes/replies/metrics_at/pinned/… живут в meta_json
# (ровно так их положила миграция ТЗ-1, поэтому числа до/после совпадают).

# Индексы адаптера на таблицах ядра (ТЗ-3d). Раньше это были строки внутри
# ``_COMPAT_STATEMENTS``; теперь — структурные описания, потому что создаёт их
# ядровой страж :func:`tuber.core.schema.ensure_adapter_index`: он пропускает
# создание, если эквивалент уже есть у ядра, и ГРОМКО падает, если колонки
# дублируют/префиксуют существующий индекс (молча ввести дубль нельзя).
_COMPAT_INDEXES: list[tuple[str, str, tuple[str, ...]]] = [
    ("idx_x_content_post", "content", ("platform", "external_id", "source_id")),
    ("idx_x_score_story", "score", ("platform", "story_id")),
    ("idx_x_story_pub", "story", ("platform", "first_pub_at")),
    ("idx_x_story_xconf", "story", ("platform", "xconf")),
    ("idx_x_cache_status", "classify_cache", ("status",)),
    ("idx_x_cache_at", "classify_cache", ("classified_at",)),
    ("idx_x_req_ts", "transport_request", ("platform", "ts")),
    ("idx_x_req_kind", "transport_request", ("platform", "kind", "status")),
]

_COMPAT_STATEMENTS: list[str] = [
    # ------------------------------------------------------------------ служебное
    "CREATE TEMP TABLE IF NOT EXISTS x_last_insert (k TEXT PRIMARY KEY, id INTEGER)",
    # Индексы под access-path'ы представлений (аддитивные, семантику не меняют).
    # В legacy ключи и индексы были в схеме таблиц; в ядре нужных индексов нет,
    # и без них join представления вырождается в O(n·m).
    #
    # ТЗ-3d (D-33): адаптер создаёт индекс на таблице ЯДРА только если ядро эту
    # пару/порядок НЕ индексирует. Убраны (теперь ядровые или неудаляемые
    # автоиндексы): `idx_x_metric_ext` (префикс `idx_metric_ext` — перехватывает
    # план: планировщик берёт `idx_x_metric_ext` вместо ядрового `idx_metric_ext`;
    # замеры приёмки — 16–20 с на всех доступных копиях, деградации не измерено),
    # `idx_x_source_ext` (ядровой `idx_source_ext`),
    # `idx_x_source_handle` (автоиндекс `UNIQUE(platform, handle)`),
    # `idx_x_member_story` (префикс PK `story_member(story_id, content_id)`),
    # `idx_x_score_sig`/`idx_x_content_hash`/`idx_x_content_pub`/`idx_x_content_author`
    # (перекрыты ядровыми `idx_score_sig`/`idx_content_hash`/`idx_content_pub`/
    # `idx_content_author`). Список закрыт тестом `tests/core/test_index_hygiene.py`.
    #
    # Оставшиеся индексы создаются НЕ этим списком, а `_COMPAT_INDEXES` ниже —
    # через ядровой страж `schema.ensure_adapter_index`: он не даст создать дубль
    # или префикс-дубль ни сейчас, ни будущей платформе.

    # =================================================================== accounts
    "DROP VIEW IF EXISTS temp.accounts",
    """
    CREATE TEMP VIEW accounts AS
    SELECT s.id                                                  AS id,
           s.handle                                              AS handle,
           s.external_id                                         AS x_id,
           s.tier                                                AS tier,
           s.status                                              AS status,
           s.lang                                                AS lang,
           s.topic_guess                                         AS topic_guess,
           s.is_author                                           AS is_author,
           s.ai_density                                          AS ai_density,
           s.cv_interval                                         AS cv_interval,
           s.posts_per_day                                       AS posts_per_day,
           s.link_ratio                                          AS link_ratio,
           s.rt_ratio                                            AS rt_ratio,
           s.dup_ratio                                           AS dup_ratio,
           s.first_mover_score                                   AS first_mover_score,
           s.posts_collected                                     AS posts_collected,
           """ + _t("s.last_synced_at") + """                   AS last_success_at,
           """ + _t("json_extract(s.meta_json, '$.last_attempt_at')") + """ AS last_attempt_at,
           s.fail_streak                                         AS fail_streak,
           s.last_error                                          AS last_error,
           s.cursor                                              AS cursor,
           """ + _t("s.first_seen_at") + """                    AS added_at,
           s.added_by                                            AS added_by,
           s.source_kind                                         AS source_type,
           s.notes                                               AS notes,
           """ + _t("s.verified_at") + """                      AS verified_at,
           json_extract(s.meta_json, '$.ai_density_src')         AS ai_density_src,
           """ + _t("json_extract(s.meta_json, '$.provisional_since')") + """ AS provisional_since,
           json_extract(s.meta_json, '$.reject_reason')          AS reject_reason,
           """ + _t("json_extract(s.meta_json, '$.last_reject_at')") + """ AS last_reject_at,
           json_extract(s.meta_json, '$.promo_path')             AS promo_path
    FROM source s WHERE s.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.accounts_ins",
    """
    CREATE TEMP TRIGGER accounts_ins INSTEAD OF INSERT ON accounts BEGIN
      INSERT INTO source(id, platform, handle, external_id, tier, status, lang,
          topic_guess, is_author, ai_density, cv_interval, posts_per_day, link_ratio,
          rt_ratio, dup_ratio, first_mover_score, posts_collected, last_synced_at,
          fail_streak, last_error, cursor, first_seen_at, added_by, source_kind,
          notes, verified_at, meta_json)
      VALUES(NEW.id, 'x', NEW.handle, NEW.x_id, COALESCE(NEW.tier, 'C'),
          COALESCE(NEW.status, 'candidate'), NEW.lang, NEW.topic_guess, NEW.is_author,
          NEW.ai_density, NEW.cv_interval, NEW.posts_per_day, NEW.link_ratio,
          NEW.rt_ratio, NEW.dup_ratio, NEW.first_mover_score,
          COALESCE(NEW.posts_collected, 0), """ + _c("NEW.last_success_at") + """,
          COALESCE(NEW.fail_streak, 0), NEW.last_error, NEW.cursor,
          COALESCE(""" + _c("NEW.added_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          NEW.added_by, NEW.source_type, NEW.notes,
          """ + _c("NEW.verified_at") + """,
          json_object('last_attempt_at', """ + _c("NEW.last_attempt_at") + """,
                      'ai_density_src', NEW.ai_density_src,
                      'provisional_since', """ + _c("NEW.provisional_since") + """,
                      'reject_reason', NEW.reject_reason,
                      'last_reject_at', """ + _c("NEW.last_reject_at") + """,
                      'promo_path', NEW.promo_path));
    END
    """,
    "DROP TRIGGER IF EXISTS temp.accounts_upd",
    """
    CREATE TEMP TRIGGER accounts_upd INSTEAD OF UPDATE ON accounts BEGIN
      UPDATE source SET
        handle = NEW.handle,
        external_id = NEW.x_id,
        tier = NEW.tier,
        status = NEW.status,
        lang = NEW.lang,
        topic_guess = NEW.topic_guess,
        is_author = NEW.is_author,
        ai_density = NEW.ai_density,
        cv_interval = NEW.cv_interval,
        posts_per_day = NEW.posts_per_day,
        link_ratio = NEW.link_ratio,
        rt_ratio = NEW.rt_ratio,
        dup_ratio = NEW.dup_ratio,
        first_mover_score = NEW.first_mover_score,
        posts_collected = NEW.posts_collected,
        last_synced_at = """ + _c("NEW.last_success_at") + """,
        fail_streak = NEW.fail_streak,
        last_error = NEW.last_error,
        cursor = NEW.cursor,
        first_seen_at = """ + _c("NEW.added_at") + """,
        added_by = NEW.added_by,
        source_kind = NEW.source_type,
        notes = NEW.notes,
        verified_at = """ + _c("NEW.verified_at") + """,
        meta_json = json_set(COALESCE(meta_json, '{}'),
          '$.last_attempt_at', """ + _c("NEW.last_attempt_at") + """,
          '$.ai_density_src', NEW.ai_density_src,
          '$.provisional_since', """ + _c("NEW.provisional_since") + """,
          '$.reject_reason', NEW.reject_reason,
          '$.last_reject_at', """ + _c("NEW.last_reject_at") + """,
          '$.promo_path', NEW.promo_path)
      WHERE platform = 'x' AND id = OLD.id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.accounts_del",
    """
    CREATE TEMP TRIGGER accounts_del INSTEAD OF DELETE ON accounts BEGIN
      DELETE FROM source WHERE platform='x' AND id = OLD.id;
    END
    """,

    # ====================================================================== posts
    "DROP VIEW IF EXISTS temp.posts",
    """
    CREATE TEMP VIEW posts AS
    SELECT c.id                                                     AS id,
           c.source_id                                              AS account_id,
           c.external_id                                            AS tweet_id,
           """ + _t("c.published_at") + """                         AS published_at_utc,
           json_extract(c.meta_json, '$.published_src')              AS published_src,
           c.text                                                    AS text,
           c.text_hash                                               AS text_hash,
           c.url                                                     AS url,
           json_extract(c.meta_json, '$.text_src')                   AS text_src,
           c.lang                                                    AS lang,
           c.links                                                   AS links,
           c.mentions                                                AS mentions,
           c.hashtags                                                AS hashtags,
           c.is_repost                                               AS is_retweet,
           c.is_quote                                                AS is_quote,
           c.is_reply                                                AS is_reply,
           json_extract(c.meta_json, '$.owner_handle')               AS owner_handle,
           c.author_handle                                           AS author_handle,
           json_extract(c.meta_json, '$.orig_handle')                AS orig_handle,
           c.media_kind                                              AS media_kind,
           """ + _t("c.first_seen_at") + """                         AS first_seen_at,
           json_extract(c.meta_json, '$.likes')                      AS likes,
           json_extract(c.meta_json, '$.replies')                    AS replies,
           json_extract(c.meta_json, '$.has_quote')                  AS has_quote,
           json_extract(c.meta_json, '$.is_long')                    AS is_long,
           """ + _t("json_extract(c.meta_json, '$.metrics_at')") + """ AS metrics_at,
           json_extract(c.meta_json, '$.metrics_src')                AS metrics_src,
           json_extract(c.meta_json, '$.pinned')                     AS pinned,
           json_extract(c.meta_json, '$.retweet_count')              AS retweet_count,
           json_extract(c.meta_json, '$.author_verified')            AS author_verified,
           json_extract(c.meta_json, '$.spread_src')                 AS spread_src,
           """ + _t("c.deleted_at") + """                            AS deleted_at
    FROM content c
    WHERE c.platform = 'x' AND COALESCE(c.kind, 'post') <> 'story_stub'
    """,
    "DROP TRIGGER IF EXISTS temp.posts_ins",
    """
    CREATE TEMP TRIGGER posts_ins INSTEAD OF INSERT ON posts BEGIN
      INSERT INTO content(platform, external_id, source_id, url, kind, text, text_hash,
          lang, links, mentions, hashtags, author_handle, published_at, media_kind,
          is_repost, is_quote, is_reply, first_seen_at, last_seen_at, deleted_at,
          meta_json)
      VALUES('x', NEW.tweet_id, NEW.account_id,
          CASE WHEN COALESCE(NEW.author_handle, NEW.owner_handle,
                             (SELECT handle FROM source WHERE id=NEW.account_id)) IS NULL
               THEN NULL
               ELSE '""" + urls.X_HOST + """/' || COALESCE(NEW.author_handle, NEW.owner_handle,
                             (SELECT handle FROM source WHERE id=NEW.account_id))
                    || '/status/' || NEW.tweet_id END,
          'post', NEW.text, NEW.text_hash,
          NEW.lang, NEW.links, NEW.mentions, NEW.hashtags, NEW.author_handle,
          COALESCE(""" + _c("NEW.published_at_utc") + """, '1970-01-01 00:00:00'),
          NEW.media_kind, COALESCE(NEW.is_retweet, 0), COALESCE(NEW.is_quote, 0),
          COALESCE(NEW.is_reply, 0),
          COALESCE(""" + _c("NEW.first_seen_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          """ + _c("NEW.first_seen_at") + """,
          """ + _c("NEW.deleted_at") + """,
          json_object(
            'published_src', NEW.published_src,
            'text_src', NEW.text_src,
            'owner_handle', NEW.owner_handle,
            'orig_handle', NEW.orig_handle,
            'has_quote', NEW.has_quote,
            'is_long', NEW.is_long,
            'metrics_at', """ + _c("NEW.metrics_at") + """,
            'metrics_src', NEW.metrics_src,
            'pinned', NEW.pinned,
            'retweet_count', NEW.retweet_count,
            'author_verified', NEW.author_verified,
            'spread_src', NEW.spread_src,
            'likes', NEW.likes,
            'replies', NEW.replies));
    END
    """,
    "DROP TRIGGER IF EXISTS temp.posts_upd",
    """
    CREATE TEMP TRIGGER posts_upd INSTEAD OF UPDATE ON posts BEGIN
      UPDATE content SET
        source_id = NEW.account_id,
        text = NEW.text,
        text_hash = NEW.text_hash,
        lang = NEW.lang,
        links = NEW.links,
        mentions = NEW.mentions,
        hashtags = NEW.hashtags,
        author_handle = NEW.author_handle,
        published_at = COALESCE(""" + _c("NEW.published_at_utc") + """, published_at),
        media_kind = NEW.media_kind,
        is_repost = COALESCE(NEW.is_retweet, 0),
        is_quote = COALESCE(NEW.is_quote, 0),
        is_reply = COALESCE(NEW.is_reply, 0),
        first_seen_at = COALESCE(""" + _c("NEW.first_seen_at") + """, first_seen_at),
        deleted_at = """ + _c("NEW.deleted_at") + """,
        meta_json = json_set(COALESCE(meta_json, '{}'),
          '$.published_src', NEW.published_src,
          '$.text_src', NEW.text_src,
          '$.owner_handle', NEW.owner_handle,
          '$.orig_handle', NEW.orig_handle,
          '$.has_quote', NEW.has_quote,
          '$.is_long', NEW.is_long,
          '$.metrics_at', """ + _c("NEW.metrics_at") + """,
          '$.metrics_src', NEW.metrics_src,
          '$.pinned', NEW.pinned,
          '$.retweet_count', NEW.retweet_count,
          '$.author_verified', NEW.author_verified,
          '$.spread_src', NEW.spread_src,
          '$.likes', NEW.likes,
          '$.replies', NEW.replies)
      WHERE platform = 'x' AND external_id = OLD.tweet_id;
      -- «Последнее известное» состояние метрик (content_latest) — только когда
      -- метрики действительно изменились: иначе лишняя запись ломала бы счётчик
      -- changes_since() у соседних путей (текст/удаление).
      INSERT INTO content_latest(content_id, platform, external_id, captured_at,
                                 likes, replies)
      SELECT id, 'x', external_id, """ + _c("NEW.metrics_at") + """,
             NEW.likes, NEW.replies
      FROM content WHERE platform='x' AND external_id = OLD.tweet_id
        AND """ + _c("NEW.metrics_at") + """ IS NOT NULL
        AND (NEW.likes IS NOT OLD.likes OR NEW.replies IS NOT OLD.replies
             OR """ + _c("NEW.metrics_at") + """ IS NOT OLD.metrics_at)
      ON CONFLICT(content_id) DO UPDATE SET
        captured_at = excluded.captured_at, likes = excluded.likes,
        replies = excluded.replies, platform = 'x', external_id = excluded.external_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.posts_del",
    """
    CREATE TEMP TRIGGER posts_del INSTEAD OF DELETE ON posts BEGIN
      DELETE FROM content WHERE platform='x' AND external_id = OLD.tweet_id;
    END
    """,

    # ===================================================== post_metrics_history
    "DROP VIEW IF EXISTS temp.post_metrics_history",
    """
    CREATE TEMP VIEW post_metrics_history AS
    SELECT m.id                                                     AS id,
           m.external_id                                            AS tweet_id,
           """ + _t("m.captured_at") + """                          AS taken_at,
           m.age_hours                                              AS age_hours,
           m.likes                                                  AS likes,
           m.replies                                                AS replies,
           m.source                                                 AS src
    FROM metric_snapshot m
    WHERE m.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.pmh_ins",
    """
    CREATE TEMP TRIGGER pmh_ins INSTEAD OF INSERT ON post_metrics_history BEGIN
      INSERT INTO metric_snapshot(content_id, platform, external_id, captured_at,
          age_hours, likes, replies, source)
      SELECT c.id, 'x', NEW.tweet_id,
             COALESCE(""" + _c("NEW.taken_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
             NEW.age_hours, NEW.likes, NEW.replies, NEW.src
      FROM content c
      WHERE c.platform = 'x' AND c.external_id = NEW.tweet_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.pmh_upd",
    """
    CREATE TEMP TRIGGER pmh_upd INSTEAD OF UPDATE ON post_metrics_history BEGIN
      UPDATE metric_snapshot SET
        captured_at = COALESCE(""" + _c("NEW.taken_at") + """, captured_at),
        age_hours = NEW.age_hours, likes = NEW.likes, replies = NEW.replies,
        source = NEW.src
      WHERE id = OLD.id AND platform = 'x';
    END
    """,
    "DROP TRIGGER IF EXISTS temp.pmh_del",
    """
    CREATE TEMP TRIGGER pmh_del INSTEAD OF DELETE ON post_metrics_history BEGIN
      DELETE FROM metric_snapshot WHERE id = OLD.id AND platform = 'x';
    END
    """,

    # ==================================================================== cursors
    "DROP VIEW IF EXISTS temp.cursors",
    """
    CREATE TEMP VIEW cursors AS
    SELECT c.rowid                                                  AS id,
           c.kind                                                   AS kind,
           c.ref                                                    AS ref,
           c.cursor                                                 AS cursor,
           """ + _t("c.last_page_at") + """                          AS last_page_at,
           c.pages_total                                            AS pages_total,
           c.items_total                                            AS items_total
    FROM cursor c
    WHERE c.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.cursors_ins",
    """
    CREATE TEMP TRIGGER cursors_ins INSTEAD OF INSERT ON cursors BEGIN
      INSERT INTO cursor(platform, kind, ref, cursor, last_page_at, pages_total,
          items_total, updated_at)
      VALUES('x', NEW.kind, NEW.ref, NEW.cursor, """ + _c("NEW.last_page_at") + """,
          NEW.pages_total, NEW.items_total,
          COALESCE(""" + _c("NEW.last_page_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')))
      ON CONFLICT(platform, kind, ref) DO UPDATE SET
        cursor = excluded.cursor,
        last_page_at = excluded.last_page_at,
        pages_total = cursor.pages_total + COALESCE(excluded.pages_total, 1),
        items_total = cursor.items_total + excluded.items_total,
        updated_at = excluded.updated_at;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.cursors_upd",
    """
    CREATE TEMP TRIGGER cursors_upd INSTEAD OF UPDATE ON cursors BEGIN
      UPDATE cursor SET
        cursor = NEW.cursor,
        last_page_at = """ + _c("NEW.last_page_at") + """,
        pages_total = NEW.pages_total,
        items_total = NEW.items_total
      WHERE platform='x' AND kind = OLD.kind AND ref = OLD.ref;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.cursors_del",
    """
    CREATE TEMP TRIGGER cursors_del INSTEAD OF DELETE ON cursors BEGIN
      DELETE FROM cursor WHERE platform='x' AND kind = OLD.kind AND ref = OLD.ref;
    END
    """,

    # ================================================================== instances
    "DROP VIEW IF EXISTS temp.instances",
    """
    CREATE TEMP VIEW instances AS
    SELECT i.host                                                    AS host,
           i.healthy                                                 AS healthy,
           i.rss_ok                                                  AS rss_ok,
           i.items_last_test                                         AS items_last_test,
           """ + _t("i.last_check_at") + """                          AS last_check_at,
           i.fail_streak                                             AS fail_streak,
           COALESCE(json_extract(i.meta_json, '$.collect_fail_streak'), 0) AS collect_fail_streak,
           """ + _t("json_extract(i.meta_json, '$.reserve_since')") + """ AS reserve_since,
           """ + _t("i.cooldown_until") + """                         AS cooldown_until,
           i.requests_today                                          AS requests_today,
           i.day                                                     AS day,
           i.version                                                 AS version,
           i.last_error                                              AS last_error,
           i.rate_limited_429                                        AS rate_limited_429,
           i.blocked                                                 AS blocked
    FROM transport_instance i
    WHERE i.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.instances_ins",
    """
    CREATE TEMP TRIGGER instances_ins INSTEAD OF INSERT ON instances BEGIN
      INSERT INTO transport_instance(platform, host, healthy, rss_ok, items_last_test,
          last_check_at, fail_streak, cooldown_until, requests_today, day, version,
          last_error, rate_limited_429, blocked, meta_json)
      VALUES('x', NEW.host, NEW.healthy, NEW.rss_ok, NEW.items_last_test,
          """ + _c("NEW.last_check_at") + """, COALESCE(NEW.fail_streak, 0),
          """ + _c("NEW.cooldown_until") + """, NEW.requests_today, NEW.day, NEW.version,
          NEW.last_error, COALESCE(NEW.rate_limited_429, 0), COALESCE(NEW.blocked, 0),
          json_object('collect_fail_streak', COALESCE(NEW.collect_fail_streak, 0),
                      'reserve_since', """ + _c("NEW.reserve_since") + """));
    END
    """,
    "DROP TRIGGER IF EXISTS temp.instances_upd",
    """
    CREATE TEMP TRIGGER instances_upd INSTEAD OF UPDATE ON instances BEGIN
      UPDATE transport_instance SET
        healthy = NEW.healthy,
        rss_ok = NEW.rss_ok,
        items_last_test = NEW.items_last_test,
        last_check_at = """ + _c("NEW.last_check_at") + """,
        fail_streak = NEW.fail_streak,
        cooldown_until = """ + _c("NEW.cooldown_until") + """,
        requests_today = NEW.requests_today,
        day = NEW.day,
        version = NEW.version,
        last_error = NEW.last_error,
        rate_limited_429 = NEW.rate_limited_429,
        blocked = NEW.blocked,
        meta_json = json_set(COALESCE(meta_json, '{}'),
          '$.collect_fail_streak', NEW.collect_fail_streak,
          '$.reserve_since', """ + _c("NEW.reserve_since") + """)
      WHERE platform='x' AND host = OLD.host;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.instances_del",
    """
    CREATE TEMP TRIGGER instances_del INSTEAD OF DELETE ON instances BEGIN
      DELETE FROM transport_instance WHERE platform='x' AND host = OLD.host;
    END
    """,

    # =================================================================== requests
    "DROP VIEW IF EXISTS temp.requests",
    """
    CREATE TEMP VIEW requests AS
    SELECT r.id                                                      AS id,
           r.host                                                    AS host,
           """ + _t("r.ts") + """                                    AS ts,
           r.kind                                                    AS kind,
           r.url                                                     AS url,
           r.status                                                  AS status,
           r.items                                                   AS items,
           r.latency_ms                                              AS latency_ms,
           r.run_id                                                  AS run_id
    FROM transport_request r
    WHERE r.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.requests_ins",
    """
    CREATE TEMP TRIGGER requests_ins INSTEAD OF INSERT ON requests BEGIN
      INSERT INTO transport_request(platform, host, ts, kind, url, status, items,
          latency_ms, run_id)
      VALUES('x', NEW.host,
          COALESCE(""" + _c("NEW.ts") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          NEW.kind, NEW.url, NEW.status, NEW.items, NEW.latency_ms, NEW.run_id);
    END
    """,
    "DROP TRIGGER IF EXISTS temp.requests_del",
    """
    CREATE TEMP TRIGGER requests_del INSTEAD OF DELETE ON requests BEGIN
      DELETE FROM transport_request WHERE platform='x' AND id = OLD.id;
    END
    """,

    # ================================================================= candidates
    "DROP VIEW IF EXISTS temp.candidates",
    """
    CREATE TEMP VIEW candidates AS
    SELECT ca.handle                                                AS handle,
           ca.found_via                                             AS found_via,
           ca.found_in_handle                                       AS found_in_account,
           COALESCE(ca.seen_count, 1)                               AS seen_count,
           COALESCE(ca.distinct_sources, 1)                         AS distinct_sources,
           """ + _t("ca.first_seen_at") + """                        AS first_seen_at,
           """ + _t("ca.last_seen_at") + """                         AS last_seen_at,
           ca.validated                                             AS validated,
           ca.reject_reason                                         AS reject_reason,
           COALESCE(ca.llm_checked, 0)                              AS llm_checked,
           COALESCE(json_extract(ca.meta_json, '$.sources'),
                    ca.sources_json)                                AS sources,
           ca.score_priority                                        AS priority,
           ca.rubric                                                AS rubric,
           ca.lang_guess                                            AS lang_guess,
           COALESCE(ca.spam, 0)                                     AS spam,
           ca.promoted_by                                           AS promoted_by,
           """ + _t("json_extract(ca.meta_json, '$.verified_at')") + """ AS verified_at,
           COALESCE(ca.ai_hint, 0)                                  AS ai_hint,
           json_extract(ca.meta_json, '$.feed_source')              AS feed_source
    FROM candidate ca
    WHERE ca.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.candidates_ins",
    """
    CREATE TEMP TRIGGER candidates_ins INSTEAD OF INSERT ON candidates BEGIN
      INSERT INTO candidate(platform, kind, handle, external_id, found_via,
          found_in_handle, seen_count, distinct_sources, status, validated,
          reject_reason, llm_checked, score_priority, rubric, lang_guess, spam,
          promoted_by, ai_hint, first_seen_at, last_seen_at, meta_json)
      VALUES('x', 'handle', NEW.handle, NEW.handle, NEW.found_via,
          NEW.found_in_account, COALESCE(NEW.seen_count, 1),
          COALESCE(NEW.distinct_sources, 1), 'new', NEW.validated,
          NEW.reject_reason, COALESCE(NEW.llm_checked, 0), NEW.priority, NEW.rubric,
          NEW.lang_guess, COALESCE(NEW.spam, 0), NEW.promoted_by,
          COALESCE(NEW.ai_hint, 0),
          """ + _c("NEW.first_seen_at") + """, """ + _c("NEW.last_seen_at") + """,
          json_object('sources', NEW.sources,
                      'verified_at', """ + _c("NEW.verified_at") + """,
                      'feed_source', NEW.feed_source))
      ON CONFLICT(platform, handle) DO UPDATE SET
        found_via = COALESCE(excluded.found_via, candidate.found_via),
        found_in_handle = COALESCE(excluded.found_in_handle, candidate.found_in_handle),
        seen_count = COALESCE(excluded.seen_count, 1),
        distinct_sources = COALESCE(excluded.distinct_sources, 1),
        validated = COALESCE(excluded.validated, candidate.validated),
        reject_reason = excluded.reject_reason,
        llm_checked = COALESCE(excluded.llm_checked, 0),
        score_priority = excluded.score_priority,
        rubric = COALESCE(excluded.rubric, candidate.rubric),
        lang_guess = COALESCE(excluded.lang_guess, candidate.lang_guess),
        spam = COALESCE(excluded.spam, 0),
        promoted_by = COALESCE(excluded.promoted_by, candidate.promoted_by),
        ai_hint = COALESCE(excluded.ai_hint, 0),
        last_seen_at = COALESCE(""" + _c("NEW.last_seen_at") + """, candidate.last_seen_at),
        meta_json = json_set(COALESCE(candidate.meta_json, '{}'),
          '$.sources', NEW.sources, '$.verified_at', """ + _c("NEW.verified_at") + """,
          '$.feed_source', NEW.feed_source);
    END
    """,
    "DROP TRIGGER IF EXISTS temp.candidates_upd",
    """
    CREATE TEMP TRIGGER candidates_upd INSTEAD OF UPDATE ON candidates BEGIN
      UPDATE candidate SET
        found_via = NEW.found_via,
        found_in_handle = NEW.found_in_account,
        seen_count = NEW.seen_count,
        distinct_sources = NEW.distinct_sources,
        first_seen_at = """ + _c("NEW.first_seen_at") + """,
        last_seen_at = """ + _c("NEW.last_seen_at") + """,
        validated = NEW.validated,
        reject_reason = NEW.reject_reason,
        llm_checked = NEW.llm_checked,
        score_priority = NEW.priority,
        rubric = NEW.rubric,
        lang_guess = NEW.lang_guess,
        spam = NEW.spam,
        promoted_by = NEW.promoted_by,
        ai_hint = NEW.ai_hint,
        meta_json = json_set(COALESCE(meta_json, '{}'),
          '$.sources', NEW.sources,
          '$.verified_at', """ + _c("NEW.verified_at") + """,
          '$.feed_source', NEW.feed_source)
      WHERE platform='x' AND handle = OLD.handle;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.candidates_del",
    """
    CREATE TEMP TRIGGER candidates_del INSTEAD OF DELETE ON candidates BEGIN
      DELETE FROM candidate WHERE platform='x' AND handle = OLD.handle;
    END
    """,

    # ================================================================== blocklist
    "DROP VIEW IF EXISTS temp.x_blocklist",
    """
    CREATE TEMP VIEW x_blocklist AS
    SELECT b.handle                                                  AS handle,
           b.reason                                                  AS reason,
           """ + _t("b.added_at") + """                              AS added_at
    FROM blocklist b
    WHERE b.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.x_blocklist_ins",
    """
    CREATE TEMP TRIGGER x_blocklist_ins INSTEAD OF INSERT ON x_blocklist BEGIN
      INSERT INTO blocklist(platform, handle, reason, added_at)
      VALUES('x', NEW.handle, NEW.reason,
          COALESCE(""" + _c("NEW.added_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')))
      ON CONFLICT(platform, handle) DO UPDATE SET reason = excluded.reason;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.x_blocklist_upd",
    """
    CREATE TEMP TRIGGER x_blocklist_upd INSTEAD OF UPDATE ON x_blocklist BEGIN
      UPDATE blocklist SET reason = NEW.reason, added_at = """ + _c("NEW.added_at") + """
      WHERE platform='x' AND handle = OLD.handle;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.x_blocklist_del",
    """
    CREATE TEMP TRIGGER x_blocklist_del INSTEAD OF DELETE ON x_blocklist BEGIN
      DELETE FROM blocklist WHERE platform='x' AND handle = OLD.handle;
    END
    """,

    # ======================================================================= runs
    "DROP VIEW IF EXISTS temp.runs",
    """
    CREATE TEMP VIEW runs AS
    SELECT r.id                                                      AS id,
           """ + _t("r.started_at") + """                            AS started_at,
           """ + _t("r.finished_at") + """                           AS finished_at,
           r.mode                                                    AS mode,
           r.ok_count                                                AS accounts_ok,
           r.fail_count                                              AS accounts_fail,
           r.items_new                                               AS posts_new,
           r.items_upd                                               AS posts_upd,
           r.errors                                                  AS errors,
           r.note                                                    AS note
    FROM run r
    WHERE r.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.runs_ins",
    """
    CREATE TEMP TRIGGER runs_ins INSTEAD OF INSERT ON runs BEGIN
      INSERT INTO run(platform, started_at, finished_at, mode, ok_count, fail_count,
          items_new, items_upd, errors, note)
      VALUES('x', """ + _c("NEW.started_at") + """, """ + _c("NEW.finished_at") + """,
          NEW.mode, COALESCE(NEW.accounts_ok, 0), COALESCE(NEW.accounts_fail, 0),
          COALESCE(NEW.posts_new, 0), COALESCE(NEW.posts_upd, 0),
          COALESCE(NEW.errors, 0), NEW.note);
      INSERT OR REPLACE INTO x_last_insert(k, id)
      VALUES('run', last_insert_rowid());
    END
    """,
    "DROP TRIGGER IF EXISTS temp.runs_upd",
    """
    CREATE TEMP TRIGGER runs_upd INSTEAD OF UPDATE ON runs BEGIN
      UPDATE run SET
        started_at = """ + _c("NEW.started_at") + """,
        finished_at = """ + _c("NEW.finished_at") + """,
        mode = NEW.mode,
        ok_count = NEW.accounts_ok,
        fail_count = NEW.accounts_fail,
        items_new = NEW.posts_new,
        items_upd = NEW.posts_upd,
        errors = NEW.errors,
        note = COALESCE(NEW.note, note)
      WHERE platform='x' AND id = OLD.id;
    END
    """,

    # ==================================================================== run_log
    # Принадлежность строки платформе — явная колонка ``run_log.platform``
    # (ТЗ-3b, D-24). Раньше её роль играло допущение «run_id IS NULL — значит X»;
    # теперь строки X (и внутри прогона, и вне его) помечены ``platform='x'``,
    # и представление не может «присвоить» чужую строку Telegram.
    "DROP VIEW IF EXISTS temp.x_run_log",
    """
    CREATE TEMP VIEW x_run_log AS
    SELECT rl.id                                                     AS id,
           rl.run_id                                                 AS run_id,
           """ + _t("rl.ts") + """                                   AS ts,
           rl.level                                                  AS level,
           rl.ref                                                    AS handle,
           rl.msg                                                    AS msg
    FROM run_log rl
    WHERE rl.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.x_run_log_ins",
    """
    CREATE TEMP TRIGGER x_run_log_ins INSTEAD OF INSERT ON x_run_log BEGIN
      INSERT INTO run_log(run_id, platform, ts, level, ref, msg)
      VALUES(NEW.run_id, 'x',
          COALESCE(""" + _c("NEW.ts") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          NEW.level, NEW.handle, NEW.msg);
    END
    """,

    # =============================================================== metrics_daily
    "DROP VIEW IF EXISTS temp.x_metrics_daily",
    """
    CREATE TEMP VIEW x_metrics_daily AS
    SELECT m.day                                                    AS day,
           m.items_ingested                                         AS posts_ingested,
           m.dup_rate                                               AS dup_rate,
           m.coverage                                               AS coverage,
           m.fail_rate                                              AS fail_rate,
           m.latency_p95_min                                        AS latency_p95_min,
           m.valid_date_ratio                                       AS valid_date_ratio,
           m.instances_alive                                        AS instances_alive,
           m.likes_median                                           AS likes_median,
           m.enriched_ratio                                         AS enriched_ratio,
           json_extract(m.extra_json, '$.cdn_429_count')            AS cdn_429_count,
           json_extract(m.extra_json, '$.synd_429_count')           AS synd_429_count,
           json_extract(m.extra_json, '$.ssr_used')                 AS ssr_used,
           json_extract(m.extra_json, '$.stale_lag_p95_min')        AS stale_lag_p95_min
    FROM metrics_daily m
    WHERE m.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.x_metrics_daily_ins",
    """
    CREATE TEMP TRIGGER x_metrics_daily_ins INSTEAD OF INSERT ON x_metrics_daily BEGIN
      INSERT INTO metrics_daily(day, platform, items_ingested, dup_rate, coverage,
          fail_rate, latency_p95_min, valid_date_ratio, instances_alive, likes_median,
          enriched_ratio, extra_json)
      VALUES(NEW.day, 'x', NEW.posts_ingested, NEW.dup_rate, NEW.coverage,
          NEW.fail_rate, NEW.latency_p95_min, NEW.valid_date_ratio,
          NEW.instances_alive, NEW.likes_median, NEW.enriched_ratio,
          json_object('cdn_429_count', NEW.cdn_429_count,
                      'synd_429_count', NEW.synd_429_count,
                      'ssr_used', NEW.ssr_used,
                      'stale_lag_p95_min', NEW.stale_lag_p95_min))
      ON CONFLICT(day, platform) DO UPDATE SET
        items_ingested = excluded.items_ingested,
        dup_rate = excluded.dup_rate,
        coverage = excluded.coverage,
        fail_rate = excluded.fail_rate,
        latency_p95_min = excluded.latency_p95_min,
        valid_date_ratio = excluded.valid_date_ratio,
        instances_alive = excluded.instances_alive,
        likes_median = excluded.likes_median,
        enriched_ratio = excluded.enriched_ratio,
        extra_json = excluded.extra_json;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.x_metrics_daily_upd",
    """
    CREATE TEMP TRIGGER x_metrics_daily_upd INSTEAD OF UPDATE ON x_metrics_daily BEGIN
      UPDATE metrics_daily SET
        items_ingested = NEW.posts_ingested, dup_rate = NEW.dup_rate,
        coverage = NEW.coverage, fail_rate = NEW.fail_rate,
        latency_p95_min = NEW.latency_p95_min,
        valid_date_ratio = NEW.valid_date_ratio,
        instances_alive = NEW.instances_alive, likes_median = NEW.likes_median,
        enriched_ratio = NEW.enriched_ratio,
        extra_json = json_object('cdn_429_count', NEW.cdn_429_count,
                      'synd_429_count', NEW.synd_429_count,
                      'ssr_used', NEW.ssr_used,
                      'stale_lag_p95_min', NEW.stale_lag_p95_min)
      WHERE platform='x' AND day = OLD.day;
    END
    """,

    # ===================================================================== darks
    # ``darks`` в ядре живёт в ``source.meta_json`` (``$.darks``) — так её
    # положила миграция ТЗ-1.
    "DROP VIEW IF EXISTS temp.darks",
    """
    CREATE TEMP VIEW darks AS
    SELECT s.handle                                                  AS handle,
           """ + _t("json_extract(s.meta_json, '$.darks.computed_at')") + """ AS computed_at,
           json_extract(s.meta_json, '$.darks.stories_cur')          AS stories_cur,
           json_extract(s.meta_json, '$.darks.stories_prev')         AS stories_prev,
           json_extract(s.meta_json, '$.darks.growth')               AS growth,
           json_extract(s.meta_json, '$.darks.in_top')               AS in_top
    FROM source s
    WHERE s.platform = 'x'
      AND json_extract(s.meta_json, '$.darks') IS NOT NULL
    """,
    "DROP TRIGGER IF EXISTS temp.darks_ins",
    """
    CREATE TEMP TRIGGER darks_ins INSTEAD OF INSERT ON darks BEGIN
      INSERT INTO source(platform, handle) VALUES('x', NEW.handle)
      ON CONFLICT(platform, handle) DO NOTHING;
      UPDATE source SET meta_json = json_set(COALESCE(meta_json, '{}'), '$.darks',
          json_object('computed_at', """ + _c("NEW.computed_at") + """,
                      'stories_cur', COALESCE(NEW.stories_cur, 0),
                      'stories_prev', COALESCE(NEW.stories_prev, 0),
                      'growth', NEW.growth,
                      'in_top', COALESCE(NEW.in_top, 0)))
      WHERE platform='x' AND handle = NEW.handle;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.darks_del",
    """
    CREATE TEMP TRIGGER darks_del INSTEAD OF DELETE ON darks BEGIN
      UPDATE source SET meta_json = json_remove(COALESCE(meta_json, '{}'), '$.darks')
      WHERE platform='x' AND (OLD.handle IS NULL OR handle = OLD.handle);
    END
    """,

    # ================================================================== classified
    # Канон — ``classify_cache`` (как объявлено в ТЗ-1 и как положила миграция),
    # ``tweet_id`` legacy-строки хранится в ``meta_json``. Параллельно (только
    # если пост есть в ядре) обновляется ядровая проекция ``classification``.
    "DROP VIEW IF EXISTS temp.classified",
    """
    CREATE TEMP VIEW classified AS
    SELECT cc.text_hash                                              AS text_hash,
           json_extract(cc.meta_json, '$.tweet_id')                  AS tweet_id,
           cc.is_ai                                                  AS is_ai,
           cc.topic                                                  AS topic,
           cc.subtopic                                               AS subtopic,
           cc.claim_type                                             AS claim_type,
           cc.novelty                                                AS novelty,
           cc.lang                                                   AS lang,
           cc.status                                                 AS status,
           cc.method                                                 AS method,
           cc.model                                                  AS model,
           COALESCE(cc.attempts, 0)                                  AS attempts,
           cc.error                                                  AS error,
           cc.prompt_tokens                                          AS prompt_tokens,
           cc.completion_tokens                                      AS completion_tokens,
           cc.cost_usd                                               AS cost_usd,
           """ + _t("cc.classified_at") + """                        AS classified_at,
           """ + _t("cc.first_seen_at") + """                        AS first_seen_at
    FROM classify_cache cc
    """,
    "DROP TRIGGER IF EXISTS temp.classified_ins",
    """
    CREATE TEMP TRIGGER classified_ins INSTEAD OF INSERT ON classified BEGIN
      INSERT INTO classify_cache(text_hash, is_ai, topic, subtopic, claim_type, novelty,
          lang, status, method, model, attempts, error, prompt_tokens,
          completion_tokens, cost_usd, classified_at, first_seen_at, meta_json)
      VALUES(NEW.text_hash, NEW.is_ai, NEW.topic, NEW.subtopic, NEW.claim_type,
          NEW.novelty, NEW.lang, COALESCE(NEW.status, 'classified'), NEW.method,
          NEW.model, COALESCE(NEW.attempts, 0), NEW.error, NEW.prompt_tokens,
          NEW.completion_tokens, NEW.cost_usd, """ + _c("NEW.classified_at") + """,
          """ + _c("NEW.first_seen_at") + """,
          json_object('tweet_id', NEW.tweet_id))
      ON CONFLICT(text_hash) DO UPDATE SET
        is_ai = excluded.is_ai,
        topic = excluded.topic,
        subtopic = excluded.subtopic,
        claim_type = excluded.claim_type,
        novelty = excluded.novelty,
        lang = excluded.lang,
        status = excluded.status,
        method = excluded.method,
        model = excluded.model,
        attempts = COALESCE(classify_cache.attempts, 0) + COALESCE(excluded.attempts, 0),
        error = excluded.error,
        prompt_tokens = COALESCE(classify_cache.prompt_tokens, 0)
                        + COALESCE(excluded.prompt_tokens, 0),
        completion_tokens = COALESCE(classify_cache.completion_tokens, 0)
                            + COALESCE(excluded.completion_tokens, 0),
        cost_usd = COALESCE(classify_cache.cost_usd, 0) + COALESCE(excluded.cost_usd, 0),
        classified_at = excluded.classified_at,
        meta_json = json_set(COALESCE(classify_cache.meta_json, '{}'),
                             '$.tweet_id', COALESCE(excluded.meta_json,
                                                    classify_cache.meta_json));
      INSERT INTO classification(content_id, platform, external_id, is_ai, topic,
          subtopic, claim_type, novelty, lang, status, method, model, attempts, error,
          prompt_tokens, completion_tokens, cost_usd, classified_at)
      SELECT c.id, 'x', c.external_id, NEW.is_ai, NEW.topic, NEW.subtopic,
          NEW.claim_type, NEW.novelty, NEW.lang, COALESCE(NEW.status, 'classified'),
          NEW.method, NEW.model, COALESCE(NEW.attempts, 0), NEW.error,
          NEW.prompt_tokens, NEW.completion_tokens, NEW.cost_usd,
          """ + _c("NEW.classified_at") + """
      FROM content c
      WHERE c.platform = 'x' AND c.external_id = NEW.tweet_id
      ON CONFLICT(content_id) DO UPDATE SET
        is_ai = excluded.is_ai, topic = excluded.topic, subtopic = excluded.subtopic,
        claim_type = excluded.claim_type, novelty = excluded.novelty, lang = excluded.lang,
        status = excluded.status, method = excluded.method, model = excluded.model,
        attempts = COALESCE(classification.attempts, 0) + COALESCE(excluded.attempts, 0),
        error = excluded.error,
        prompt_tokens = COALESCE(classification.prompt_tokens, 0)
                        + COALESCE(excluded.prompt_tokens, 0),
        completion_tokens = COALESCE(classification.completion_tokens, 0)
                            + COALESCE(excluded.completion_tokens, 0),
        cost_usd = COALESCE(classification.cost_usd, 0) + COALESCE(excluded.cost_usd, 0),
        classified_at = excluded.classified_at;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.classified_upd",
    """
    CREATE TEMP TRIGGER classified_upd INSTEAD OF UPDATE ON classified BEGIN
      UPDATE classify_cache SET
        is_ai = NEW.is_ai, topic = NEW.topic, subtopic = NEW.subtopic,
        claim_type = NEW.claim_type, novelty = NEW.novelty, lang = NEW.lang,
        status = NEW.status, method = NEW.method, model = NEW.model,
        attempts = NEW.attempts, error = NEW.error, prompt_tokens = NEW.prompt_tokens,
        completion_tokens = NEW.completion_tokens, cost_usd = NEW.cost_usd,
        classified_at = """ + _c("NEW.classified_at") + """,
        first_seen_at = """ + _c("NEW.first_seen_at") + """,
        meta_json = json_set(COALESCE(meta_json, '{}'), '$.tweet_id', NEW.tweet_id)
      WHERE text_hash = OLD.text_hash;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.classified_del",
    """
    CREATE TEMP TRIGGER classified_del INSTEAD OF DELETE ON classified BEGIN
      DELETE FROM classify_cache WHERE text_hash = OLD.text_hash;
    END
    """,

    # ============================================================= classify_daily
    "DROP VIEW IF EXISTS temp.x_classify_daily",
    """
    CREATE TEMP VIEW x_classify_daily AS
    SELECT d.day                                                     AS day,
           COALESCE(d.posts, 0)                                      AS posts,
           COALESCE(d.model_calls, 0)                                AS model_calls,
           COALESCE(d.failed, 0)                                     AS failed,
           COALESCE(d.prompt_tokens, 0)                              AS prompt_tokens,
           COALESCE(d.completion_tokens, 0)                          AS completion_tokens,
           COALESCE(d.cost_usd, 0)                                   AS cost_usd
    FROM classify_daily d
    WHERE d.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.x_classify_daily_ins",
    """
    CREATE TEMP TRIGGER x_classify_daily_ins INSTEAD OF INSERT ON x_classify_daily BEGIN
      INSERT INTO classify_daily(day, platform, posts, model_calls, failed,
          prompt_tokens, completion_tokens, cost_usd)
      VALUES(NEW.day, 'x', COALESCE(NEW.posts, 0), COALESCE(NEW.model_calls, 0),
          COALESCE(NEW.failed, 0), COALESCE(NEW.prompt_tokens, 0),
          COALESCE(NEW.completion_tokens, 0), COALESCE(NEW.cost_usd, 0))
      ON CONFLICT(day, platform) DO UPDATE SET
        posts = COALESCE(classify_daily.posts, 0) + COALESCE(excluded.posts, 0),
        model_calls = COALESCE(classify_daily.model_calls, 0)
                      + COALESCE(excluded.model_calls, 0),
        failed = COALESCE(classify_daily.failed, 0) + COALESCE(excluded.failed, 0),
        prompt_tokens = COALESCE(classify_daily.prompt_tokens, 0)
                        + COALESCE(excluded.prompt_tokens, 0),
        completion_tokens = COALESCE(classify_daily.completion_tokens, 0)
                            + COALESCE(excluded.completion_tokens, 0),
        cost_usd = COALESCE(classify_daily.cost_usd, 0) + COALESCE(excluded.cost_usd, 0);
    END
    """,
    "DROP TRIGGER IF EXISTS temp.x_classify_daily_upd",
    """
    CREATE TEMP TRIGGER x_classify_daily_upd INSTEAD OF UPDATE ON x_classify_daily BEGIN
      UPDATE classify_daily SET posts = NEW.posts, model_calls = NEW.model_calls,
        failed = NEW.failed, prompt_tokens = NEW.prompt_tokens,
        completion_tokens = NEW.completion_tokens, cost_usd = NEW.cost_usd
      WHERE platform='x' AND day = OLD.day;
    END
    """,

    # ====================================================================== story
    "DROP VIEW IF EXISTS temp.stories",
    """
    CREATE TEMP VIEW stories AS
    SELECT s.id                                                      AS id,
           """ + _t("s.created_at") + """                            AS created_at,
           s.window_hours                                            AS window_hours,
           s.threshold                                               AS threshold,
           (SELECT c.external_id FROM content c WHERE c.id = s.first_content_id)
                                                                     AS first_tweet_id,
           (SELECT src.handle FROM source src WHERE src.id = s.first_mover_source_id)
                                                                     AS first_mover,
           """ + _t("s.first_pub_at") + """                          AS published_at,
           COALESCE(s.xconf, 0)                                      AS xconf,
           COALESCE(s.content_count, 0)                              AS post_count,
           s.lead_time_min                                           AS lead_time_min,
           s.topics                                                  AS topics,
           s.entities                                                AS entities,
           COALESCE(s.is_new_entity, 0)                              AS is_new_entity,
           COALESCE(s.is_single, 0)                                  AS is_single,
           COALESCE(s.suspect, 0)                                    AS suspect,
           """ + _t("s.claimed_at") + """                            AS claimed_at
    FROM story s
    WHERE s.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.stories_ins",
    """
    CREATE TEMP TRIGGER stories_ins INSTEAD OF INSERT ON stories BEGIN
      INSERT INTO story(platform, created_at, window_hours, threshold, first_content_id,
          first_mover_source_id, first_pub_at, xconf, content_count, lead_time_min,
          topics, entities, is_new_entity, is_single, suspect, claimed_at)
      VALUES('x', """ + _c("NEW.created_at") + """, NEW.window_hours, NEW.threshold,
          (SELECT c.id FROM content c WHERE c.platform='x'
             AND c.external_id = NEW.first_tweet_id),
          (SELECT src.id FROM source src WHERE src.platform='x'
             AND src.handle = NEW.first_mover),
          """ + _c("NEW.published_at") + """, COALESCE(NEW.xconf, 0),
          COALESCE(NEW.post_count, 0), NEW.lead_time_min, NEW.topics, NEW.entities,
          COALESCE(NEW.is_new_entity, 0), COALESCE(NEW.is_single, 0),
          COALESCE(NEW.suspect, 0), """ + _c("NEW.claimed_at") + """);
      INSERT OR REPLACE INTO x_last_insert(k, id)
      VALUES('story', last_insert_rowid());
    END
    """,
    "DROP TRIGGER IF EXISTS temp.stories_upd",
    """
    CREATE TEMP TRIGGER stories_upd INSTEAD OF UPDATE ON stories BEGIN
      UPDATE story SET
        created_at = """ + _c("NEW.created_at") + """,
        window_hours = NEW.window_hours,
        threshold = NEW.threshold,
        first_content_id = (SELECT c.id FROM content c WHERE c.platform='x'
                              AND c.external_id = NEW.first_tweet_id),
        first_mover_source_id = (SELECT src.id FROM source src WHERE src.platform='x'
                                   AND src.handle = NEW.first_mover),
        first_pub_at = """ + _c("NEW.published_at") + """,
        xconf = NEW.xconf,
        content_count = NEW.post_count,
        lead_time_min = NEW.lead_time_min,
        topics = NEW.topics,
        entities = NEW.entities,
        is_new_entity = NEW.is_new_entity,
        is_single = NEW.is_single,
        suspect = NEW.suspect,
        claimed_at = """ + _c("NEW.claimed_at") + """
      WHERE platform='x' AND id = OLD.id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.stories_del",
    """
    CREATE TEMP TRIGGER stories_del INSTEAD OF DELETE ON stories BEGIN
      DELETE FROM story WHERE platform='x' AND id = OLD.id;
    END
    """,

    # ================================================================= story_posts
    "DROP VIEW IF EXISTS temp.story_posts",
    """
    CREATE TEMP VIEW story_posts AS
    SELECT sm.story_id                                               AS story_id,
           (SELECT c.external_id FROM content c WHERE c.id = sm.content_id)
                                                                     AS tweet_id,
           -- handle пишет триггер (автор на момент кластеризации, как в legacy).
           -- У баз, перенесённых до появления колонки, он выводится из текущей
           -- строки content — тем же порядком, что в db.post_author.
           COALESCE(sm.handle,
             (SELECT COALESCE(c.author_handle,
                              json_extract(c.meta_json, '$.owner_handle'),
                              (SELECT src.handle FROM source src WHERE src.id = c.source_id))
                FROM content c WHERE c.id = sm.content_id))           AS handle,
           sm.role                                                   AS role,
           """ + _t("sm.added_at") + """                             AS added_at
    FROM story_member sm
    WHERE sm.story_id IN (SELECT id FROM story WHERE platform = 'x')
    """,
    "DROP TRIGGER IF EXISTS temp.story_posts_ins",
    """
    CREATE TEMP TRIGGER story_posts_ins INSTEAD OF INSERT ON story_posts BEGIN
      -- Ядро требует существующий ``content``, а legacy допускал «висячий»
      -- член сюжета (пост мог быть обрезан/удалён). Поэтому при отсутствии
      -- поста материализуем скрытую строку ``kind='story_stub'``: она даёт
      -- ссылочную целостность и НЕ видна в представлении ``posts``.
      INSERT INTO content(platform, external_id, kind, published_at, first_seen_at,
          author_handle)
      SELECT 'x', NEW.tweet_id, 'story_stub',
             COALESCE((SELECT first_pub_at FROM story WHERE id = NEW.story_id),
                      strftime('%Y-%m-%d %H:%M:%S','now')),
             strftime('%Y-%m-%d %H:%M:%S','now'), NEW.handle
      WHERE NOT EXISTS (SELECT 1 FROM content
                        WHERE platform='x' AND external_id = NEW.tweet_id);
      INSERT INTO story_member(story_id, content_id, role, is_canonical, added_at,
          handle)
      SELECT NEW.story_id, c.id, NEW.role,
             CASE WHEN NEW.role = 'primary' THEN 1 ELSE 0 END,
             """ + _c("NEW.added_at") + """, NEW.handle
      FROM content c
      WHERE c.platform='x' AND c.external_id = NEW.tweet_id;
      UPDATE content SET author_handle = COALESCE(author_handle, NEW.handle)
      WHERE platform='x' AND external_id = NEW.tweet_id AND NEW.handle IS NOT NULL;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.story_posts_upd",
    """
    CREATE TEMP TRIGGER story_posts_upd INSTEAD OF UPDATE ON story_posts BEGIN
      UPDATE story_member SET role = NEW.role,
             is_canonical = CASE WHEN NEW.role = 'primary' THEN 1 ELSE 0 END,
             added_at = """ + _c("NEW.added_at") + """,
             handle = COALESCE(NEW.handle, handle)
      WHERE story_id = OLD.story_id
        AND content_id = (SELECT id FROM content
                          WHERE platform='x' AND external_id = OLD.tweet_id);
    END
    """,
    "DROP TRIGGER IF EXISTS temp.story_posts_del",
    """
    CREATE TEMP TRIGGER story_posts_del INSTEAD OF DELETE ON story_posts BEGIN
      DELETE FROM story_member
      WHERE story_id = OLD.story_id
        AND content_id = (SELECT id FROM content
                          WHERE platform='x' AND external_id = OLD.tweet_id);
    END
    """,

    # ===================================================================== scores
    # Ядровая ``score`` хранит историю по ``(content_id, computed_at)``, а
    # legacy-``scores`` — ровно одну строку на пост (``PK tweet_id``). Чтобы
    # числа совпадали, триггер заменяет строку поста (как делал UPSERT).
    "DROP VIEW IF EXISTS temp.scores",
    """
    CREATE TEMP VIEW scores AS
    SELECT sc.external_id                                            AS tweet_id,
           sc.story_id                                               AS story_id,
           """ + _t("sc.computed_at") + """                          AS computed_at,
           sc.significance                                           AS significance,
           sc.branch                                                 AS branch,
           COALESCE(sc.metrics_missing, 0)                           AS metrics_missing,
           """ + _t("sc.metrics_at") + """                           AS metrics_at,
           sc.metrics_age_hours                                      AS metrics_age_hours,
           json_extract(sc.axes_json, '$.likes_at_6h')               AS likes_at_6h,
           json_extract(sc.axes_json, '$.replies_at_6h')             AS replies_at_6h,
           sc.engagement                                             AS engagement,
           sc.velocity                                               AS velocity,
           sc.xconf                                                  AS xconf,
           sc.spread                                                 AS spread,
           json_extract(sc.axes_json, '$.score_engage')              AS score_engage,
           json_extract(sc.axes_json, '$.score_spread')              AS score_spread,
           json_extract(sc.axes_json, '$.score_first')               AS score_first
    FROM score sc
    WHERE sc.platform = 'x'
    """,
    "DROP TRIGGER IF EXISTS temp.scores_ins",
    """
    CREATE TEMP TRIGGER scores_ins INSTEAD OF INSERT ON scores BEGIN
      DELETE FROM score WHERE platform='x' AND external_id = NEW.tweet_id;
      INSERT INTO score(content_id, platform, external_id, computed_at, significance,
          branch, metrics_missing, metrics_at, metrics_age_hours, engagement, velocity,
          xconf, spread, story_id, axes_json)
      SELECT c.id, 'x', NEW.tweet_id,
          COALESCE(""" + _c("NEW.computed_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          NEW.significance, NEW.branch, COALESCE(NEW.metrics_missing, 0),
          """ + _c("NEW.metrics_at") + """, NEW.metrics_age_hours, NEW.engagement,
          NEW.velocity, NEW.xconf, NEW.spread, NEW.story_id,
          json_object('likes_at_6h', NEW.likes_at_6h,
                      'replies_at_6h', NEW.replies_at_6h,
                      'score_engage', NEW.score_engage,
                      'score_spread', NEW.score_spread,
                      'score_first', NEW.score_first)
      FROM content c
      WHERE c.platform='x' AND c.external_id = NEW.tweet_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.scores_upd",
    """
    CREATE TEMP TRIGGER scores_upd INSTEAD OF UPDATE ON scores BEGIN
      UPDATE score SET
        story_id = NEW.story_id,
        computed_at = """ + _c("NEW.computed_at") + """,
        significance = NEW.significance,
        branch = NEW.branch,
        metrics_missing = NEW.metrics_missing,
        metrics_at = """ + _c("NEW.metrics_at") + """,
        metrics_age_hours = NEW.metrics_age_hours,
        engagement = NEW.engagement,
        velocity = NEW.velocity,
        xconf = NEW.xconf,
        spread = NEW.spread,
        axes_json = json_object('likes_at_6h', NEW.likes_at_6h,
                      'replies_at_6h', NEW.replies_at_6h,
                      'score_engage', NEW.score_engage,
                      'score_spread', NEW.score_spread,
                      'score_first', NEW.score_first)
      WHERE platform='x' AND external_id = OLD.tweet_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.scores_del",
    """
    CREATE TEMP TRIGGER scores_del INSTEAD OF DELETE ON scores BEGIN
      DELETE FROM score WHERE platform='x' AND external_id = OLD.tweet_id;
    END
    """,

    # ================================================================ report_texts
    "DROP VIEW IF EXISTS temp.report_texts",
    """
    CREATE TEMP VIEW report_texts AS
    SELECT rt.text_hash                                              AS text_hash,
           rt.ru                                                     AS ru,
           rt.model                                                  AS model,
           """ + _t("rt.created_at") + """                           AS created_at,
           rt.src                                                    AS src
    FROM report_text rt
    """,
    "DROP TRIGGER IF EXISTS temp.report_texts_ins",
    """
    CREATE TEMP TRIGGER report_texts_ins INSTEAD OF INSERT ON report_texts BEGIN
      INSERT INTO report_text(text_hash, ru, model, created_at, src)
      VALUES(NEW.text_hash, NEW.ru, NEW.model,
          COALESCE(""" + _c("NEW.created_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          NEW.src);
    END
    """,
    "DROP TRIGGER IF EXISTS temp.report_texts_upd",
    """
    CREATE TEMP TRIGGER report_texts_upd INSTEAD OF UPDATE ON report_texts BEGIN
      UPDATE report_text SET ru = NEW.ru, model = NEW.model,
        created_at = """ + _c("NEW.created_at") + """, src = NEW.src
      WHERE text_hash = OLD.text_hash;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.report_texts_del",
    """
    CREATE TEMP TRIGGER report_texts_del INSTEAD OF DELETE ON report_texts BEGIN
      DELETE FROM report_text WHERE text_hash = OLD.text_hash;
    END
    """,
]


def _install_indexes(conn: sqlite3.Connection) -> list[str]:
    """Создать индексы адаптера через ядровой страж (ТЗ-3d).

    Возвращает имена созданных индексов. Индекс, эквивалент которому уже есть
    у ядра (или среди автоиндексов ``UNIQUE``/``PRIMARY KEY``), не создаётся —
    страж вернул ``False``. Дубль/префикс-дубль ядрового индекса поднимает
    :class:`tuber.core.schema.DuplicateIndexError` и валит соединение с
    внятным сообщением — «молча» такая попытка не проходит.
    """
    created: list[str] = []
    for name, table, cols in _COMPAT_INDEXES:
        try:
            if core_schema.ensure_adapter_index(conn, name, table, cols):
                created.append(name)
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc).lower():
                raise core_sqlcompat.compat_install_error(exc)
            log.debug("install_compat: индекс %s пропущен на read-only соединении", name)
    return created


def install_compat(conn: sqlite3.Connection) -> None:
    """Создать TEMP-представления и триггеры совместимости с legacy X.

    На соединении «только чтение» (приёмочные инструменты открывают боевую базу
    как ``file:…?mode=ro``) TEMP-объекты создать можно, а вот аддитивные индексы
    в основной базе — нет. Такие утверждения пропускаются: индексы нужны только
    для скорости, и на боевой базе они уже созданы записывающим соединением
    (``ANALYZE``-путь).

    Трансляция legacy-имён (ТЗ-3c) включается ТОЛЬКО после установки слоя: иначе
    определения представлений/триггеров (в них конфликтующие имена стоят в
    ``FROM``/``INTO``) переписались бы на самих себя.
    """
    core_sqlcompat.disable_compat_sql(conn)
    try:
        _install_indexes(conn)
        for stmt in _COMPAT_STATEMENTS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "readonly" not in str(exc).lower():
                    raise core_sqlcompat.compat_install_error(exc)
                log.debug("install_compat: пропущено на read-only соединении: %.60s", stmt)
    finally:
        core_sqlcompat.enable_compat_sql(conn, X_COMPAT_VIEWS)


def _drop_statements() -> list[str]:
    """Утверждения слоя совместимости, снимающие его (``DROP ... IF EXISTS``).

    Список берётся из самого набора: в нём каждая сущность сначала снимается,
    потом создаётся.
    """
    return [s for s in _COMPAT_STATEMENTS
            if s.strip().upper().startswith(("DROP VIEW", "DROP TRIGGER"))]


def uninstall_compat(conn: sqlite3.Connection) -> None:
    """Снять TEMP-представления/триггеры совместимости (для миграции ядра)."""
    core_sqlcompat.disable_compat_sql(conn)
    for stmt in _drop_statements():
        conn.execute(stmt)


def compat_view_name(name: str) -> str:
    """Имя TEMP-представления, отдающего legacy-объект ``name``.

    Для конфликтующих с ядром имён (``run_log`` и т. п.) оно отличается —
    см. :data:`X_COMPAT_VIEWS`.
    """
    return X_COMPAT_VIEWS.get(name, name)


# ---------------------------------------------------------------------------
# Гарантии схемы ядра (используются приёмкой и тестами)
# ---------------------------------------------------------------------------

def view_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Есть ли представление совместимости для legacy-объекта ``name``."""
    row = conn.execute(
        "SELECT 1 FROM temp.sqlite_master WHERE type='view' AND name=?",
        (compat_view_name(name),),
    ).fetchone()
    return row is not None


def compat_tables() -> tuple[str, ...]:
    """Имена legacy-объектов, которые адаптер отдаёт представлениями."""
    return (
        "accounts", "posts", "cursors", "instances", "requests", "candidates",
        "blocklist", "runs", "run_log", "metrics_daily", "post_metrics_history",
        "classified", "classify_daily", "stories", "story_posts", "scores",
        "report_texts", "darks",
    )
