"""Адаптер хранения Telegram-кода к единому ядру монорепозитория (ТЗ-4).

Зачем он нужен
--------------
Код ``tuber-telegram`` (``collect``, ``scoring``, ``discover``, ``bridge``, …)
писал прямым плоским SQL в таблицы ``tuber_telegram.db`` (``channels``,
``posts``, ``scores``, ``channel_baselines``, …). В монорепозитории таких
таблиц нет: есть единое ядро ``source`` / ``content`` / ``metric_snapshot`` /
``score`` / ``candidate`` / … (см. :mod:`tuber.core.schema`). Чтобы НЕ
переписывать логику Telegram, этот модуль играет роль прежнего слоя доступа к
БД:

1. отдаёт ВРЕМЕННЫЕ (``TEMP``) представления с именами и колонками
   legacy-таблиц Telegram (``channels``, ``posts``, ``scores``, …). Поэтому
   плоский SQL Telegram-кода и перенесённых тестов продолжает работать без
   правок: он видит привычные таблицы, но данные приходят из ядра;
2. временные ``INSTEAD OF`` триггеры принимают legacy-запись
   (INSERT/UPDATE/DELETE) и раскладывают её по таблицам ядра.

Соответствие legacy → ядро (ТЗ-4 §0, тот же мэппинг, что в
:mod:`tuber.tools.migrate_legacy`):

=========================  ==========================================================
legacy                     ядро
=========================  ==========================================================
``channels``               ``source`` (``platform='telegram'``, ``tg_id``→``external_id``)
``posts``                  ``content`` (``external_id`` = ``<handle>/<message_id>``,
                           ``views/forwards/reactions`` → ``content_latest`` +
                           ``metric_snapshot``, остатки → ``meta_json``)
``scores``                 ``score`` (оси ``er``/``eng_channel``/``eng_global``/
                           ``wsrc``/``dup_penalty``/``fr``/``topic_weight``/
                           ``age_days`` → ``axes_json``, ``eng``→``engagement``)
``channel_baselines``      ``source_baseline``
``classified``             ``classification``
``stories``/``story_members``  ``story``/``story_member``
``account_state``          ``transport_account_state``
``runs``/``run_log``       ``run``/``run_log`` (``platform='telegram'``)
``metrics_daily``          ``metrics_daily`` (``platform='telegram'``)
=========================  ==========================================================

Идентификатор поста
-------------------
``message_id`` уникален только ВНУТРИ канала, поэтому ``content.external_id``
Telegram — составной ``<handle>/<message_id>``
(:func:`tuber.core.ids.tg_external_id`; там же обоснование). Разделитель —
``/``: именно в этом виде данные уже перенесены ТЗ-1 и читаются ядровыми
представлениями ``v_tg_posts``/``v_tg_scores``, поэтому адаптер сохраняет
существующий канон, а не вводит второй. Представление выводит ``message_id``
обратно из ``external_id`` (стабильно и восстановимо), а не из ``meta_json``.

Формат дат
----------
Ядро хранит даты как ``YYYY-MM-DD HH:MM:SS``. Legacy-код Telegram писал
смешанно (``datetime('now')`` — с пробелом, разбор t.me/s — с ``T`` и
``+00:00``), а сравнения делает через ``datetime(col)``, поэтому представления
отдают формат ядра как есть, а триггеры нормализуют вход через
``strftime('%Y-%m-%d %H:%M:%S', …)``. Единственное строковое сравнение legacy —
``views_checked_at < datetime('now','-1 day')`` — при формате ядра корректно.

Уроки ТЗ-2/ТЗ-2c/ТЗ-3d
----------------------
* ``connect()`` сначала гарантирует схему ядра (:func:`migrate_schema`), и
  только потом ставит слой совместимости — иначе «no such table: main.source».
* Представления опираются на ядровые ``v_tg_posts`` и на таблицы ядра
  (не дублируют мэппинг), поэтому мэппинг живёт в одном месте.
* Индексы на таблицах ядра создаются только через ядровой страж
  (:func:`tuber.core.schema.ensure_adapter_index`), иначе дубль молча
  перехватит план (ТЗ-3d, D-33).
* ``ensure_planner_stats()`` — ``ANALYZE`` один раз: без статистики
  планировщик берёт полный перебор (D-21).

Скоринг Telegram: ``significance`` между каналами НЕсравнима (множитель малого
канала) — адаптер это не нормирует и не «поправляет».
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from tuber.core import db as core_db
from tuber.core import schema as core_schema
from tuber.core import sqlcompat as core_sqlcompat
from tuber.core import storage as core_storage
from tuber.core import timeutil, urls
from tuber.core.schema import migrate_schema

from . import config

log = logging.getLogger(__name__)

# Legacy-имена, совпадающие с именами таблиц ядра (ТЗ-3c). Такие представления
# создаются под «своим» именем, иначе TEMP-схема затеняет таблицу ядра и DML из
# INSTEAD OF-триггера не доходит до ядра.
TG_COMPAT_VIEWS: dict[str, str] = {
    "run_log": "tg_run_log",
    "metrics_daily": "tg_metrics_daily",
}

# Версия legacy-схемы (последняя, ТЗ-17 «мост источников»). В монорепозитории
# номера версий живут в ``schema_meta`` ядра; константа сохранена как часть
# прежнего API — её читают перенесённые тесты.
SCHEMA_VERSION = 3

# Максимальное число попыток при ``database is locked`` (как в ядре).
WRITE_RETRIES = 5
WRITE_BASE_DELAY = 0.5


# ---------------------------------------------------------------------------
# Время и нормализация
# ---------------------------------------------------------------------------

def iso(dt) -> str:
    """ISO-8601 в UTC, секундная точность (формат legacy Telegram)."""
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
    """Разбор ISO-строки из БД (``T`` и пробел). Возвращает aware-datetime или None."""
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


def _ts(param: str) -> str:
    """SQL: значение legacy → формат ядра (``YYYY-MM-DD HH:MM:SS``), NULL-safe."""
    return (f"CASE WHEN {param} IS NULL THEN NULL "
            f"ELSE strftime('%Y-%m-%d %H:%M:%S', {param}) END")


def _ts_t(param: str) -> str:
    """SQL: значение ядра → ISO с зоной (``YYYY-MM-DDTHH:MM:SS+00:00``), NULL-safe.

    Нужно там, где legacy-код разбирает дату в Python и сравнивает с aware-«сейчас»
    (``account_state.flood_until``). Наивная строка дала бы ``TypeError``, который
    legacy глушит ``except Exception`` — и предохранитель флуда молча перестал бы
    работать. Ядро хранит время без зоны, поэтому зона приписывается здесь.
    """
    return (f"CASE WHEN {param} IS NULL THEN NULL "
            f"ELSE strftime('%Y-%m-%dT%H:%M:%S', {param}) || '+00:00' END")


def _jobj(*pairs: str) -> str:
    """``json_object(...)`` из пар «ключ, выражение»."""
    return "json_object(%s)" % ", ".join(pairs)


# ---------------------------------------------------------------------------
# Подключение
# ---------------------------------------------------------------------------

class _RetryConnection(core_sqlcompat.CompatConnection):
    """Соединение с повтором записи при ``database is locked``.

    В единой базе пишут три платформы из разных процессов; WAL допускает одного
    писателя. Код Telegram пишет плоским SQL (мимо ``write_tx``), поэтому повтор
    при блокировке встроен в соединение: пауза 0.5 → 1 → 2 → 4 c. ``busy_timeout``
    ядра закрывает обычные случаи, повтор — редкие длинные транзакции соседей.

    Плюс трансляция legacy-имён конфликтующих таблиц (ТЗ-3c, см.
    :class:`tuber.core.sqlcompat.CompatConnection`).
    """

    collision_map = TG_COMPAT_VIEWS

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
    """Открыть ЕДИНУЮ базу ядра и поставить слой совместимости с legacy Telegram.

    Схема ядра гарантируется здесь же (:func:`migrate_schema`, идемпотентно и
    дёшево), как в прежнем слое доступа, который сам создавал таблицы. Без этого
    шага любой запрос падал бы на «no such table: main.source».

    Модель транзакций сохранена legacy-шной: Python-овские неявные транзакции +
    явные ``commit()``/``rollback()``.
    """
    p = str(path or config.DB_PATH)
    core_sqlcompat.ensure_supported_sqlite()
    if not readonly:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True,
                               timeout=config.BUSY_TIMEOUT_SEC,
                               check_same_thread=check_same_thread,
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
    # Схема ядра ДО слоя совместимости: иначе TEMP-представление ``run_log``
    # затеняет одноимённую таблицу ядра, и ядро не может создать по ней индекс
    # («views may not be indexed»).
    if not _schema_is_current(conn):
        migrate_schema(conn)
    if install:
        install_compat(conn)
    return conn


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """База уже на текущей версии схемы ядра (и денормализации)?"""
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
    """Создать контур (схему ядра) и поставить слой совместимости. Идемпотентно."""
    conn = connect(path)
    migrate(conn)
    ensure_planner_stats(conn)
    conn.commit()
    return conn


def migrate(con: sqlite3.Connection) -> None:
    """Идемпотентная миграция схемы (схема принадлежит ядру)."""
    con.execute("PRAGMA foreign_keys=ON")
    uninstall_compat(con)
    try:
        migrate_schema(con)
    finally:
        install_compat(con)


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Транзакция записи ядра: ``BEGIN IMMEDIATE`` с повтором при блокировке."""
    if conn.in_transaction:
        yield conn
        return
    with core_db.write_tx(conn):
        yield conn


def ensure_planner_stats(conn: sqlite3.Connection) -> bool:
    """Собрать статистику планировщика (``ANALYZE``), если её ещё нет (D-21)."""
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
# Защита рабочей базы
# ---------------------------------------------------------------------------

def production_path() -> str:
    """Абсолютный (realpath) путь боевой единой базы."""
    return os.path.realpath(config.PRODUCTION_DB)


def is_production_db(path) -> bool:
    """True, если путь указывает на боевую единую базу (по realpath)."""
    if not path:
        return False
    try:
        return os.path.realpath(str(path)) == production_path()
    except OSError:
        return False


def ensure_not_production_db(path, action="запись") -> str:
    """Запрет писать в боевую базу из вспомогательных путей."""
    if is_production_db(path):
        raise RuntimeError(
            f"отказ: путь {path!r} совпадает с боевой единой базой — {action} в неё "
            "запрещена (все изменяющие операции выполняются на копии)")
    return str(path)


# ---------------------------------------------------------------------------
# Хелперы счётчиков (обход отсутствия rowcount у представлений)
# ---------------------------------------------------------------------------

def changes_since(conn: sqlite3.Connection, before: int) -> int:
    """Сколько строк фактически изменилось с момента ``before``.

    ``cursor.rowcount`` (он же ``sqlite3_changes()``) для DM-команд по
    ПРЕДСТАВЛЕНИЮ всегда 0: изменения делает ``INSTEAD OF``-триггер. Зато
    ``Connection.total_changes`` считает и строки, изменённые триггерами.
    """
    return conn.total_changes - before


def view_delete(conn: sqlite3.Connection, delete_sql: str, params=()) -> int:
    """``DELETE`` по представлению с честным числом удалённых строк."""
    count_sql = re.sub(r"^\s*DELETE\s+FROM", "SELECT COUNT(*) FROM", delete_sql,
                       count=1, flags=re.IGNORECASE)
    n = conn.execute(count_sql, params).fetchone()[0]
    conn.execute(delete_sql, params)
    return int(n or 0)


def last_insert_id(conn: sqlite3.Connection, kind: str = "story"):
    """id последней строки, вставленной через представление (``lastrowid`` = 0).

    Триггер пишет настоящий rowid в TEMP-таблицу ``tg_last_insert``.
    """
    row = conn.execute("SELECT id FROM temp.tg_last_insert WHERE k=?", (kind,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# run / run_log: прежний API
# ---------------------------------------------------------------------------

def log_run(con, level, msg, handle=None, run_id=None):
    """Строка журнала прогона (``platform='telegram'`` — явная принадлежность, D-24)."""
    con.execute(
        "INSERT INTO main.run_log (run_id, platform, ts, level, ref, msg) VALUES (?,?,?,?,?,?)",
        (run_id, "telegram", now_iso(), level, handle, msg),
    )


def start_run(con, mode, note=None):
    with write_tx(con):
        cur = con.execute(
            "INSERT INTO main.run (platform, started_at, ok_count, fail_count, items_new,"
            " items_upd, errors, note, mode) VALUES ('telegram',?,0,0,0,0,0,?,?)",
            (now_iso(), note, mode))
    return cur.lastrowid


def finish_run(con, run_id, *, channels_ok=0, channels_fail=0, posts_new=0,
               posts_upd=0, errors=0, note=None):
    if run_id is None:
        return
    with write_tx(con):
        con.execute(
            "UPDATE main.run SET finished_at=?, ok_count=?, fail_count=?, items_new=?,"
            " items_upd=?, errors=?, note=COALESCE(?, note) WHERE id=?",
            (now_iso(), channels_ok, channels_fail, posts_new, posts_upd, errors,
             note, run_id))


# ---------------------------------------------------------------------------
# Хендлы кандидатов: реестр (source) и обмен (candidate)
# ---------------------------------------------------------------------------

def known_handles(con) -> set[str]:
    """Все хендлы Telegram во ВСЕХ статусах: реестр (``source``) + ``candidate``.

    Дедупликация моста (ТЗ-17) обязана видеть обе половины: канал, попавший в
    реестр, не должен импортироваться повторно как кандидат, и наоборот.
    """
    out: set[str] = set()
    for row in con.execute(
            "SELECT handle FROM source WHERE platform='telegram'"):
        if row[0]:
            out.add(str(row[0]).lower())
    for row in con.execute(
            "SELECT handle FROM candidate WHERE platform='telegram'"):
        if row[0]:
            out.add(str(row[0]).lower())
    return out


# ---------------------------------------------------------------------------
# Подписчики: ряд роста в ``source.meta_json`` (ТЗ-45)
# ---------------------------------------------------------------------------
#: Сколько последних снимков хранить в ряду (та же обрезка, что у X).
FOLLOWERS_HISTORY_MAX = core_storage.FOLLOWERS_HISTORY_MAX


def set_follower_snapshot(con, source_id, subs, *, avg_views=None, taken_at=None):
    """Снимок подписчиков Telegram (ТЗ-45) — ОБЩАЯ функция ядра.

    Тот же формат ряда, что у X (:func:`tuber.core.storage.set_follower_snapshot`),
    чтобы не плодить второй формат ряда подписчиков (долг D-56). Обновляет
    ``source.subs``/``subs_at`` и дописывает точку в
    ``source.meta_json.followers_history``.
    """
    history = core_storage.set_follower_snapshot(
        con, source_id, subs, avg_views=avg_views, taken_at=taken_at)
    con.commit()
    return history


def import_candidate(con, handle: str, *, found_via: str, notes: str | None = None,
                     meta: dict | None = None) -> str:
    """Записать кандидата в каноническую таблицу ``candidate`` (идемпотентно).

    Возвращает ``"inserted"`` или ``"updated"``: повторный импорт того же хендла
    НЕ размножает строки (идемпотентность моста — требование ТЗ-4 §2 п.3).
    """
    import json

    payload = {"notes": notes} if notes else {}
    if meta:
        payload.update(meta)
    row = con.execute(
        "SELECT id FROM candidate WHERE platform='telegram' AND handle=?",
        (handle,)).fetchone()
    if row is not None:
        con.execute(
            "UPDATE candidate SET last_seen_at=?, seen_count=COALESCE(seen_count,1)+1"
            " WHERE id=?", (now_iso(), row[0]))
        return "updated"
    con.execute(
        "INSERT INTO candidate(platform, kind, handle, found_via, status, meta_json,"
        " first_seen_at, last_seen_at) VALUES ('telegram','channel',?,?,'new',?,?,?)",
        (handle, found_via, json.dumps(payload, ensure_ascii=False, sort_keys=True)
         if payload else None, now_iso(), now_iso()),
    )
    return "inserted"


def upsert_candidate(con, platform: str, handle: str, *, kind: str, found_via: str,
                     external_id: str | None = None, meta: dict | None = None,
                     first_seen: str | None = None, last_seen: str | None = None) -> str:
    """UPSERT кандидата в ``candidate`` по (platform, handle).

    Возвращает ``"inserted"`` или ``"updated"`` — чтобы приёмка и тесты могли
    показать идемпотентность (повтор не размножает строки).
    """
    import json

    meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True) if meta else None
    row = con.execute(
        "SELECT id, found_via FROM candidate WHERE platform=? AND handle=?",
        (platform, handle)).fetchone()
    now = now_iso()
    if row is None:
        con.execute(
            "INSERT INTO candidate(platform, kind, handle, external_id, found_via, status,"
            " meta_json, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,'new',?,?,?)",
            (platform, kind, handle, external_id, found_via, meta_json,
             first_seen or now, last_seen or now))
        return "inserted"
    if row[1] != found_via:
        # Строку завёл ДРУГОЙ продюсер (другая площадка/другой путь): он владеет
        # её meta_json/first_seen_at. Для нас это лишь «хендл встретился ещё раз» —
        # отмечаем факт встречи и НЕ перезаписываем чужое содержимое: иначе экспорт
        # одного фида молча стирает чужие поля кандидата.
        con.execute(
            "UPDATE candidate SET seen_count = COALESCE(seen_count,1)+1, last_seen_at=?"
            " WHERE id=?", (last_seen or now, row[0]))
        return "updated"
    con.execute(
        "UPDATE candidate SET seen_count = COALESCE(seen_count,1)+1, last_seen_at=?,"
        " external_id=COALESCE(?, external_id), meta_json=COALESCE(?, meta_json)"
        " WHERE platform=? AND handle=?",
        (last_seen or now, external_id, meta_json, platform, handle))
    return "updated"


def candidates_by_kind(con, platform: str, kind: str) -> list[sqlite3.Row]:
    """Кандидаты платформы заданного вида (для экспорта фида)."""
    return con.execute(
        "SELECT * FROM candidate WHERE platform=? AND kind=? ORDER BY handle",
        (platform, kind)).fetchall()


# ===========================================================================
# Слой совместимости: TEMP-представления и INSTEAD OF триггеры
# ===========================================================================

# Индексы адаптера на таблицах ядра (ТЗ-3d). Создаются через ядровой страж
# ``schema.ensure_adapter_index``: он не даст создать дубль или префикс-дубль
# ядрового индекса. Здесь — только access-path'ы, которых у ядра нет.
_COMPAT_INDEXES: list[tuple[str, str, tuple[str, ...]]] = [
    ("idx_tg_score_story", "score", ("platform", "story_id")),
    ("idx_tg_story_pub", "story", ("platform", "first_pub_at")),
]


_COMPAT_STATEMENTS: list[str] = [
    # ------------------------------------------------------------------ служебное
    "CREATE TEMP TABLE IF NOT EXISTS tg_last_insert (k TEXT PRIMARY KEY, id INTEGER)",

    # =================================================================== channels
    "DROP VIEW IF EXISTS temp.channels",
    """
    CREATE TEMP VIEW channels AS
    SELECT s.id                                          AS id,
           s.handle                                      AS handle,
           CAST(s.external_id AS INTEGER)                AS tg_id,
           s.title                                       AS title,
           s.subs                                        AS subs,
           s.subs_at                                     AS subs_at,
           json_extract(COALESCE(s.meta_json,'{}'), '$.last_post_at')  AS last_post_at,
           json_extract(COALESCE(s.meta_json,'{}'), '$.posts_7d')      AS posts_7d,
           s.avg_views                                   AS avg_views,
           s.vr                                          AS vr,
           s.lang                                        AS lang,
           s.topic_guess                                 AS topic_guess,
           s.status                                      AS status,
           s.read_mode                                   AS read_mode,
           COALESCE(s.is_author, 0)                      AS is_author,
           COALESCE(s.antifraud_flag, 0)                 AS antifraud_flag,
           """ + _ts_t("s.flood_until") + """                      AS flood_until,
           s.added_at                                    AS added_at,
           s.checked_at                                  AS checked_at,
           s.source_kind                                 AS source,
           s.notes                                       AS notes
    FROM main.source s
    WHERE s.platform = 'telegram'
    """,
    "DROP TRIGGER IF EXISTS temp.channels_ins",
    """
    CREATE TEMP TRIGGER channels_ins INSTEAD OF INSERT ON channels BEGIN
      INSERT INTO source(platform, handle, external_id, title, subs, subs_at, avg_views, vr,
          lang, topic_guess, status, read_mode, is_author, antifraud_flag, flood_until,
          added_at, checked_at, source_kind, notes, posts_collected, first_seen_at,
          last_synced_at, meta_json)
      VALUES('telegram', NEW.handle, CAST(NEW.tg_id AS TEXT), NEW.title, NEW.subs,
          NEW.subs_at, NEW.avg_views, NEW.vr, NEW.lang, NEW.topic_guess,
          COALESCE(NEW.status, 'candidate'), COALESCE(NEW.read_mode, 'web'),
          NEW.is_author, COALESCE(NEW.antifraud_flag, 0), NEW.flood_until,
          COALESCE(""" + _ts("NEW.added_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          """ + _ts("NEW.checked_at") + """, NEW.source, NEW.notes, 0,
          COALESCE(""" + _ts("NEW.added_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          """ + _ts("NEW.checked_at") + """,
          """ + _jobj("'posts_7d'", "NEW.posts_7d", "'last_post_at'", "NEW.last_post_at") + """)
      ON CONFLICT(platform, handle) DO UPDATE SET
          external_id = COALESCE(excluded.external_id, source.external_id),
          title = COALESCE(excluded.title, source.title),
          subs = COALESCE(excluded.subs, source.subs),
          subs_at = COALESCE(excluded.subs_at, source.subs_at),
          avg_views = COALESCE(excluded.avg_views, source.avg_views),
          vr = COALESCE(excluded.vr, source.vr),
          lang = COALESCE(excluded.lang, source.lang),
          status = excluded.status,
          read_mode = excluded.read_mode,
          is_author = COALESCE(excluded.is_author, source.is_author),
          antifraud_flag = excluded.antifraud_flag,
          notes = COALESCE(excluded.notes, source.notes),
          source_kind = COALESCE(excluded.source_kind, source.source_kind),
          checked_at = COALESCE(excluded.checked_at, source.checked_at),
          meta_json = excluded.meta_json;
      INSERT OR REPLACE INTO tg_last_insert(k, id)
      VALUES('channel', (SELECT id FROM source WHERE platform='telegram' AND handle=NEW.handle));
    END
    """,
    "DROP TRIGGER IF EXISTS temp.channels_upd",
    """
    CREATE TEMP TRIGGER channels_upd INSTEAD OF UPDATE ON channels BEGIN
      UPDATE source SET
        handle = NEW.handle,
        external_id = CAST(NEW.tg_id AS TEXT),
        title = COALESCE(NEW.title, title),
        subs = NEW.subs,
        subs_at = NEW.subs_at,
        avg_views = NEW.avg_views,
        vr = NEW.vr,
        lang = NEW.lang,
        topic_guess = NEW.topic_guess,
        status = NEW.status,
        read_mode = NEW.read_mode,
        is_author = NEW.is_author,
        antifraud_flag = NEW.antifraud_flag,
        flood_until = """ + _ts("NEW.flood_until") + """,
        added_at = COALESCE(""" + _ts("NEW.added_at") + """, added_at),
        checked_at = """ + _ts("NEW.checked_at") + """,
        source_kind = NEW.source,
        notes = NEW.notes,
        last_synced_at = """ + _ts("NEW.checked_at") + """,
        meta_json = json_set(COALESCE(meta_json,'{}'),
                             '$.posts_7d', NEW.posts_7d,
                             '$.last_post_at', NEW.last_post_at)
      WHERE platform='telegram' AND id = OLD.id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.channels_del",
    """
    CREATE TEMP TRIGGER channels_del INSTEAD OF DELETE ON channels BEGIN
      DELETE FROM source WHERE platform='telegram' AND id = OLD.id;
    END
    """,

    # ====================================================================== posts
    # Определение — ровно ядровое ``v_tg_posts``: мэппинг живёт в одном месте и
    # не дублируется (ТЗ-4 §0). Колонки совпадают с legacy ``posts``.
    "DROP VIEW IF EXISTS temp.posts",
    """
    CREATE TEMP VIEW posts AS
    SELECT p.id             AS id,
           p.channel_id     AS channel_id,
           p.message_id     AS message_id,
           p.date_utc       AS date_utc,
           p.text           AS text,
           p.text_hash      AS text_hash,
           p.views          AS views,
           p.forwards       AS forwards,
           p.reactions      AS reactions,
           p.media_kind     AS media_kind,
           p.links          AS links,
           p.hashtags       AS hashtags,
           p.mentions       AS mentions,
           p.fwd_from       AS fwd_from,
           p.is_forward     AS is_forward,
           p.has_own_media  AS has_own_media,
           p.is_ad          AS is_ad,
           p.first_seen_at  AS first_seen_at,
           p.views_checked_at AS views_checked_at
    FROM main.v_tg_posts p
    """,
    "DROP TRIGGER IF EXISTS temp.posts_ins",
    """
    CREATE TEMP TRIGGER posts_ins INSTEAD OF INSERT ON posts BEGIN
      INSERT INTO content(platform, source_id, external_id, kind, url, published_at, text,
          text_hash, media_kind, links, hashtags, mentions, is_repost, is_promo,
          first_seen_at, last_seen_at, meta_json)
      SELECT 'telegram', NEW.channel_id,
          s.handle || '/' || NEW.message_id,
          'post',
          CASE WHEN s.handle IS NULL THEN NULL
               ELSE '""" + urls.TELEGRAM_HOST + """/' || s.handle || '/' || NEW.message_id END,
          COALESCE(""" + _ts("NEW.date_utc") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          NEW.text, NEW.text_hash, NEW.media_kind, NEW.links, NEW.hashtags, NEW.mentions,
          COALESCE(NEW.is_forward, 0), COALESCE(NEW.is_ad, 0),
          COALESCE(""" + _ts("NEW.first_seen_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          COALESCE(""" + _ts("NEW.first_seen_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          """ + _jobj("'message_id'", "NEW.message_id", "'fwd_from'", "NEW.fwd_from",
                      "'has_own_media'", "NEW.has_own_media", "'views'", "NEW.views",
                      "'forwards'", "NEW.forwards", "'reactions'", "NEW.reactions",
                      "'views_checked_at'", "NEW.views_checked_at") + """
      FROM source s WHERE s.platform='telegram' AND s.id = NEW.channel_id;

      INSERT OR REPLACE INTO tg_last_insert(k, id)
      VALUES('post', (SELECT id FROM content WHERE platform='telegram'
                        AND external_id = (SELECT s.handle || '/' || NEW.message_id
                                             FROM source s
                                            WHERE s.platform='telegram' AND s.id = NEW.channel_id)));

      -- Показатели поста: снимок метрик + последнее значение (ядро читает
      -- ``content_latest``, поэтому legacy-строка сразу видна отчётам).
      INSERT INTO metric_snapshot(content_id, captured_at, views, forwards, reactions, source)
      SELECT c.id,
             COALESCE(""" + _ts("NEW.views_checked_at") + """,
                      """ + _ts("NEW.first_seen_at") + """,
                      """ + _ts("NEW.date_utc") + """,
                      strftime('%Y-%m-%d %H:%M:%S','now')),
             NEW.views, NEW.forwards, NEW.reactions, 'telegram'
      FROM content c
      WHERE c.platform='telegram'
        AND c.external_id = (SELECT s.handle || '/' || NEW.message_id
                               FROM source s
                              WHERE s.platform='telegram' AND s.id = NEW.channel_id)
      ON CONFLICT(content_id, captured_at) DO UPDATE SET
             views=excluded.views, forwards=excluded.forwards, reactions=excluded.reactions;

      INSERT INTO content_latest(content_id, captured_at, views, forwards, reactions)
      SELECT c.id,
             COALESCE(""" + _ts("NEW.views_checked_at") + """,
                      """ + _ts("NEW.first_seen_at") + """,
                      """ + _ts("NEW.date_utc") + """,
                      strftime('%Y-%m-%d %H:%M:%S','now')),
             NEW.views, NEW.forwards, NEW.reactions
      FROM content c
      WHERE c.platform='telegram'
        AND c.external_id = (SELECT s.handle || '/' || NEW.message_id
                               FROM source s
                              WHERE s.platform='telegram' AND s.id = NEW.channel_id)
      ON CONFLICT(content_id) DO UPDATE SET
             captured_at=excluded.captured_at, views=excluded.views,
             forwards=excluded.forwards, reactions=excluded.reactions;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.posts_upd",
    """
    CREATE TEMP TRIGGER posts_upd INSTEAD OF UPDATE ON posts BEGIN
      UPDATE content SET
        published_at = COALESCE(""" + _ts("NEW.date_utc") + """, published_at),
        text = NEW.text,
        text_hash = NEW.text_hash,
        media_kind = NEW.media_kind,
        is_repost = COALESCE(NEW.is_forward, is_repost),
        is_promo = COALESCE(NEW.is_ad, is_promo),
        first_seen_at = COALESCE(""" + _ts("NEW.first_seen_at") + """, first_seen_at),
        last_seen_at = strftime('%Y-%m-%d %H:%M:%S','now'),
        meta_json = json_set(COALESCE(meta_json,'{}'),
                             '$.message_id', NEW.message_id,
                             '$.fwd_from', NEW.fwd_from,
                             '$.has_own_media', NEW.has_own_media,
                             '$.views', NEW.views,
                             '$.forwards', NEW.forwards,
                             '$.reactions', NEW.reactions,
                             '$.views_checked_at', NEW.views_checked_at)
      WHERE platform='telegram' AND id = OLD.id;

      -- Метка времени снапшота: момент проверки просмотров, иначе «сейчас».
      -- Триггер обязан быть идемпотентным при любом порядке и любом значении
      -- views_checked_at (ТЗ-10 B1). Сначала «переносим» метку, и только если
      -- она свободна; затем дописываем метрики upsert'ом по
      -- UNIQUE(content_id, captured_at). Если метка уже занята другой строкой,
      -- её метрики обновляются, а прежний «хвост» остаётся: история не теряется
      -- и конфликта UNIQUE не возникает.
      UPDATE metric_snapshot SET
        captured_at = COALESCE(""" + _ts("NEW.views_checked_at") + """,
                               strftime('%Y-%m-%d %H:%M:%S','now')),
        -- Просмотры монотонны (ТЗ-53): снимок не может показать меньше
        -- сохранённого максимума того же материала. Так откат сетевого
        -- ответа (устаревшая страница t.me/s) не понижает ряд — та же
        -- защита в сборщике (`collect.py`) и в очереди
        -- (`metrics.monotonic_views`).
        views = CASE WHEN NEW.views IS NULL THEN views
                     ELSE MAX(NEW.views, COALESCE(
                          (SELECT MAX(views) FROM metric_snapshot WHERE content_id=OLD.id),
                          NEW.views)) END,
        forwards = NEW.forwards, reactions = NEW.reactions
      WHERE content_id = OLD.id
        AND captured_at = (SELECT MAX(captured_at) FROM metric_snapshot WHERE content_id=OLD.id)
        AND NOT EXISTS (SELECT 1 FROM metric_snapshot m
                         WHERE m.content_id = OLD.id
                           AND m.captured_at = COALESCE(""" + _ts("NEW.views_checked_at") + """,
                                                        strftime('%Y-%m-%d %H:%M:%S','now')));

      INSERT INTO metric_snapshot(content_id, captured_at, views, forwards, reactions, source)
      VALUES(OLD.id,
             COALESCE(""" + _ts("NEW.views_checked_at") + """,
                      strftime('%Y-%m-%d %H:%M:%S','now')),
             NEW.views, NEW.forwards, NEW.reactions, 'telegram')
      ON CONFLICT(content_id, captured_at) DO UPDATE SET
             views = CASE WHEN excluded.views IS NULL THEN views
                          ELSE MAX(excluded.views, COALESCE(
                               (SELECT MAX(views) FROM metric_snapshot
                                 WHERE content_id=excluded.content_id),
                               excluded.views)) END,
             forwards=excluded.forwards, reactions=excluded.reactions;

      INSERT INTO content_latest(content_id, captured_at, views, forwards, reactions)
      VALUES(OLD.id,
             COALESCE(""" + _ts("NEW.views_checked_at") + """,
                      strftime('%Y-%m-%d %H:%M:%S','now')),
             NEW.views, NEW.forwards, NEW.reactions)
      ON CONFLICT(content_id) DO UPDATE SET
             captured_at=excluded.captured_at, views=excluded.views,
             forwards=excluded.forwards, reactions=excluded.reactions;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.posts_del",
    """
    CREATE TEMP TRIGGER posts_del INSTEAD OF DELETE ON posts BEGIN
      DELETE FROM content WHERE platform='telegram' AND id = OLD.id;
    END
    """,

    # ===================================================================== scores
    # Ядровая ``score`` хранит историю по ``(content_id, computed_at)``, а
    # legacy-``scores`` — ровно одну строку на пост (``PK post_id``). Чтобы числа
    # совпадали, триггер ЗАМЕНЯЕТ строку поста (как делал legacy UPSERT).
    "DROP VIEW IF EXISTS temp.scores",
    """
    CREATE TEMP VIEW scores AS
    SELECT sc.content_id                                              AS post_id,
           json_extract(sc.axes_json, '$.er')                         AS er,
           json_extract(sc.axes_json, '$.eng_channel')                AS eng_channel,
           json_extract(sc.axes_json, '$.eng_global')                 AS eng_global,
           sc.engagement                                              AS eng,
           sc.xconf                                                   AS xconf,
           json_extract(sc.axes_json, '$.wsrc')                       AS wsrc,
           json_extract(sc.axes_json, '$.dup_penalty')                AS dup_penalty,
           json_extract(sc.axes_json, '$.fr')                         AS fr,
           json_extract(sc.axes_json, '$.topic_weight')               AS topic_weight,
           json_extract(sc.axes_json, '$.age_days')                   AS age_days,
           sc.decay                                                   AS decay,
           sc.significance                                            AS significance,
           COALESCE(sc.anomaly, 0)                                    AS anomaly,
           sc.computed_at                                             AS computed_at
    FROM main.score sc
    WHERE sc.platform = 'telegram'
    """,
    "DROP TRIGGER IF EXISTS temp.scores_ins",
    """
    CREATE TEMP TRIGGER scores_ins INSTEAD OF INSERT ON scores BEGIN
      DELETE FROM score WHERE platform='telegram' AND content_id = NEW.post_id;
      INSERT INTO score(content_id, computed_at, significance, engagement, decay, xconf,
          anomaly, axes_json)
      VALUES(NEW.post_id,
          COALESCE(""" + _ts("NEW.computed_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          NEW.significance, NEW.eng, NEW.decay, NEW.xconf, COALESCE(NEW.anomaly, 0),
          """ + _jobj("'er'", "NEW.er", "'eng_channel'", "NEW.eng_channel",
                      "'eng_global'", "NEW.eng_global", "'wsrc'", "NEW.wsrc",
                      "'dup_penalty'", "NEW.dup_penalty", "'fr'", "NEW.fr",
                      "'topic_weight'", "NEW.topic_weight",
                      "'age_days'", "NEW.age_days") + """);
    END
    """,
    "DROP TRIGGER IF EXISTS temp.scores_upd",
    """
    CREATE TEMP TRIGGER scores_upd INSTEAD OF UPDATE ON scores BEGIN
      UPDATE score SET
        computed_at = """ + _ts("NEW.computed_at") + """,
        significance = NEW.significance,
        engagement = NEW.eng,
        decay = NEW.decay,
        xconf = NEW.xconf,
        anomaly = NEW.anomaly,
        axes_json = """ + _jobj("'er'", "NEW.er", "'eng_channel'", "NEW.eng_channel",
                                "'eng_global'", "NEW.eng_global", "'wsrc'", "NEW.wsrc",
                                "'dup_penalty'", "NEW.dup_penalty", "'fr'", "NEW.fr",
                                "'topic_weight'", "NEW.topic_weight",
                                "'age_days'", "NEW.age_days") + """
      WHERE platform='telegram' AND content_id = OLD.post_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.scores_del",
    """
    CREATE TEMP TRIGGER scores_del INSTEAD OF DELETE ON scores BEGIN
      DELETE FROM score WHERE platform='telegram' AND content_id = OLD.post_id;
    END
    """,

    # ========================================================= channel_baselines
    "DROP VIEW IF EXISTS temp.channel_baselines",
    """
    CREATE TEMP VIEW channel_baselines AS
    SELECT sb.source_id                 AS channel_id,
           sb.window_days               AS window_days,
           sb.items_in_window           AS posts_in_window,
           sb.median_views              AS median_views,
           sb.median_reactions          AS median_reactions,
           sb.median_er                 AS median_er,
           sb.is_author_data            AS is_author_data,
           sb.hashed_items              AS hashed_posts,
           sb.dup_items                 AS dup_posts,
           sb.dup_ratio                 AS dup_ratio,
           sb.computed_at               AS computed_at
    FROM main.source_baseline sb
    WHERE sb.source_id IN (SELECT id FROM main.source WHERE platform='telegram')
    """,
    "DROP TRIGGER IF EXISTS temp.cb_ins",
    """
    CREATE TEMP TRIGGER cb_ins INSTEAD OF INSERT ON channel_baselines BEGIN
      INSERT INTO source_baseline(source_id, window_days, items_in_window, median_views,
          median_reactions, median_er, is_author_data, hashed_items, dup_items, dup_ratio,
          computed_at)
      VALUES(NEW.channel_id, NEW.window_days, NEW.posts_in_window, NEW.median_views,
          NEW.median_reactions, NEW.median_er, NEW.is_author_data, NEW.hashed_posts,
          NEW.dup_posts, NEW.dup_ratio, """ + _ts("NEW.computed_at") + """)
      ON CONFLICT(source_id) DO UPDATE SET
          window_days=excluded.window_days, items_in_window=excluded.items_in_window,
          median_views=excluded.median_views, median_reactions=excluded.median_reactions,
          median_er=excluded.median_er, is_author_data=excluded.is_author_data,
          hashed_items=excluded.hashed_items, dup_items=excluded.dup_items,
          dup_ratio=excluded.dup_ratio, computed_at=excluded.computed_at;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.cb_upd",
    """
    CREATE TEMP TRIGGER cb_upd INSTEAD OF UPDATE ON channel_baselines BEGIN
      UPDATE source_baseline SET
        window_days = NEW.window_days,
        items_in_window = NEW.posts_in_window,
        median_views = NEW.median_views,
        median_reactions = NEW.median_reactions,
        median_er = NEW.median_er,
        is_author_data = NEW.is_author_data,
        hashed_items = NEW.hashed_posts,
        dup_items = NEW.dup_posts,
        dup_ratio = NEW.dup_ratio,
        computed_at = """ + _ts("NEW.computed_at") + """
      WHERE source_id = OLD.channel_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.cb_del",
    """
    CREATE TEMP TRIGGER cb_del INSTEAD OF DELETE ON channel_baselines BEGIN
      DELETE FROM source_baseline WHERE source_id = OLD.channel_id;
    END
    """,

    # ================================================================= classified
    "DROP VIEW IF EXISTS temp.classified",
    """
    CREATE TEMP VIEW classified AS
    SELECT cl.content_id     AS post_id,
           cl.is_ai          AS is_ai,
           cl.topic          AS topic,
           cl.source_type    AS source_type,
           cl.claim_type     AS claim_type,
           cl.entity_tier    AS entity_tier,
           cl.novelty        AS novelty,
           cl.lang           AS lang,
           cl.confidence     AS confidence,
           cl.model          AS model,
           cl.prompt_ver     AS prompt_ver,
           cl.classified_at  AS classified_at
    FROM main.classification cl
    WHERE cl.content_id IN (SELECT id FROM main.content WHERE platform='telegram')
    """,
    "DROP TRIGGER IF EXISTS temp.classified_ins",
    """
    CREATE TEMP TRIGGER classified_ins INSTEAD OF INSERT ON classified BEGIN
      INSERT INTO classification(content_id, is_ai, topic, source_type, claim_type,
          entity_tier, novelty, lang, confidence, model, prompt_ver, classified_at)
      VALUES(NEW.post_id, NEW.is_ai, NEW.topic, NEW.source_type, NEW.claim_type,
          NEW.entity_tier, NEW.novelty, NEW.lang, NEW.confidence, NEW.model,
          NEW.prompt_ver, """ + _ts("NEW.classified_at") + """)
      ON CONFLICT(content_id) DO UPDATE SET
          is_ai=excluded.is_ai, topic=excluded.topic, source_type=excluded.source_type,
          claim_type=excluded.claim_type, entity_tier=excluded.entity_tier,
          novelty=excluded.novelty, lang=excluded.lang, confidence=excluded.confidence,
          model=excluded.model, prompt_ver=excluded.prompt_ver,
          classified_at=excluded.classified_at;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.classified_upd",
    """
    CREATE TEMP TRIGGER classified_upd INSTEAD OF UPDATE ON classified BEGIN
      UPDATE classification SET
        is_ai=NEW.is_ai, topic=NEW.topic, source_type=NEW.source_type,
        claim_type=NEW.claim_type, entity_tier=NEW.entity_tier, novelty=NEW.novelty,
        lang=NEW.lang, confidence=NEW.confidence, model=NEW.model,
        prompt_ver=NEW.prompt_ver, classified_at=""" + _ts("NEW.classified_at") + """
      WHERE content_id = OLD.post_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.classified_del",
    """
    CREATE TEMP TRIGGER classified_del INSTEAD OF DELETE ON classified BEGIN
      DELETE FROM classification WHERE content_id = OLD.post_id;
    END
    """,

    # ==================================================================== stories
    "DROP VIEW IF EXISTS temp.stories",
    """
    CREATE TEMP VIEW stories AS
    SELECT s.id                             AS id,
           s.canonical_content_id           AS canonical_post_id,
           s.title                          AS title,
           s.topic                          AS topic,
           COALESCE(s.source_count, 0)      AS channel_count,
           COALESCE(s.xconf, 0)             AS xconf,
           s.first_pub_at                   AS first_pub_at,
           s.last_pub_at                    AS last_pub_at,
           s.significance                   AS significance,
           s.rank_score                     AS rank_score,
           s.created_at                     AS created_at
    FROM main.story s
    WHERE s.platform = 'telegram'
    """,
    "DROP TRIGGER IF EXISTS temp.stories_ins",
    """
    CREATE TEMP TRIGGER stories_ins INSTEAD OF INSERT ON stories BEGIN
      INSERT INTO story(platform, canonical_content_id, title, topic, source_count, xconf,
          first_pub_at, last_pub_at, significance, rank_score, created_at, content_count)
      VALUES('telegram', NEW.canonical_post_id, NEW.title, NEW.topic,
          COALESCE(NEW.channel_count, 0), COALESCE(NEW.xconf, 0),
          """ + _ts("NEW.first_pub_at") + """, """ + _ts("NEW.last_pub_at") + """,
          NEW.significance, NEW.rank_score,
          COALESCE(""" + _ts("NEW.created_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          COALESCE(NEW.channel_count, 0));
      INSERT OR REPLACE INTO tg_last_insert(k, id)
      VALUES('story', last_insert_rowid());
    END
    """,
    "DROP TRIGGER IF EXISTS temp.stories_upd",
    """
    CREATE TEMP TRIGGER stories_upd INSTEAD OF UPDATE ON stories BEGIN
      UPDATE story SET
        canonical_content_id = NEW.canonical_post_id,
        title = NEW.title, topic = NEW.topic,
        source_count = NEW.channel_count, content_count = NEW.channel_count,
        xconf = NEW.xconf,
        first_pub_at = """ + _ts("NEW.first_pub_at") + """,
        last_pub_at = """ + _ts("NEW.last_pub_at") + """,
        significance = NEW.significance, rank_score = NEW.rank_score
      WHERE platform='telegram' AND id = OLD.id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.stories_del",
    """
    CREATE TEMP TRIGGER stories_del INSTEAD OF DELETE ON stories BEGIN
      DELETE FROM story WHERE platform='telegram' AND id = OLD.id;
    END
    """,

    # ============================================================= story_members
    "DROP VIEW IF EXISTS temp.story_members",
    """
    CREATE TEMP VIEW story_members AS
    SELECT sm.story_id      AS story_id,
           sm.content_id    AS post_id,
           sm.sim           AS sim,
           sm.is_canonical  AS is_canonical,
           sm.role          AS role
    FROM main.story_member sm
    WHERE sm.story_id IN (SELECT id FROM main.story WHERE platform='telegram')
    """,
    "DROP TRIGGER IF EXISTS temp.sm_ins",
    """
    CREATE TEMP TRIGGER sm_ins INSTEAD OF INSERT ON story_members BEGIN
      INSERT INTO story_member(story_id, content_id, role, sim, is_canonical, added_at)
      VALUES(NEW.story_id, NEW.post_id, NEW.role, NEW.sim,
          COALESCE(NEW.is_canonical, 0), strftime('%Y-%m-%d %H:%M:%S','now'))
      ON CONFLICT(story_id, content_id) DO UPDATE SET
          role=excluded.role, sim=excluded.sim, is_canonical=excluded.is_canonical;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.sm_upd",
    """
    CREATE TEMP TRIGGER sm_upd INSTEAD OF UPDATE ON story_members BEGIN
      UPDATE story_member SET role=NEW.role, sim=NEW.sim, is_canonical=NEW.is_canonical
      WHERE story_id = OLD.story_id AND content_id = OLD.post_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.sm_del",
    """
    CREATE TEMP TRIGGER sm_del INSTEAD OF DELETE ON story_members BEGIN
      DELETE FROM story_member WHERE story_id = OLD.story_id AND content_id = OLD.post_id;
    END
    """,

    # ============================================================== account_state
    "DROP VIEW IF EXISTS temp.account_state",
    """
    CREATE TEMP VIEW account_state AS
    SELECT a.name           AS name,
           """ + _ts_t("a.flood_until") + """    AS flood_until,
           a.last_error     AS last_error,
           COALESCE(a.resolves_today, 0) AS resolves_today,
           a.day            AS day,
           a.updated_at     AS updated_at
    FROM main.transport_account_state a
    WHERE a.platform = 'telegram'
    """,
    "DROP TRIGGER IF EXISTS temp.account_state_ins",
    """
    CREATE TEMP TRIGGER account_state_ins INSTEAD OF INSERT ON account_state BEGIN
      INSERT INTO transport_account_state(platform, name, flood_until, last_error,
          resolves_today, day, updated_at)
      VALUES('telegram', NEW.name, """ + _ts("NEW.flood_until") + """, NEW.last_error,
          COALESCE(NEW.resolves_today, 0), NEW.day,
          COALESCE(""" + _ts("NEW.updated_at") + """, strftime('%Y-%m-%d %H:%M:%S','now')))
      ON CONFLICT(platform, name) DO NOTHING;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.account_state_upd",
    """
    CREATE TEMP TRIGGER account_state_upd INSTEAD OF UPDATE ON account_state BEGIN
      UPDATE transport_account_state SET
        flood_until = """ + _ts("NEW.flood_until") + """,
        last_error = NEW.last_error,
        resolves_today = NEW.resolves_today,
        day = NEW.day,
        updated_at = COALESCE(""" + _ts("NEW.updated_at") + """,
                              strftime('%Y-%m-%d %H:%M:%S','now'))
      WHERE platform='telegram' AND name = OLD.name;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.account_state_del",
    """
    CREATE TEMP TRIGGER account_state_del INSTEAD OF DELETE ON account_state BEGIN
      DELETE FROM transport_account_state WHERE platform='telegram' AND name = OLD.name;
    END
    """,

    # ======================================================================= runs
    "DROP VIEW IF EXISTS temp.runs",
    """
    CREATE TEMP VIEW runs AS
    SELECT r.id             AS id,
           r.started_at     AS started_at,
           r.finished_at    AS finished_at,
           r.mode           AS mode,
           r.ok_count       AS channels_ok,
           r.fail_count     AS channels_fail,
           r.items_new      AS posts_new,
           r.items_upd      AS posts_upd,
           r.errors         AS errors,
           r.note           AS note
    FROM main.run r
    WHERE r.platform = 'telegram'
    """,
    "DROP TRIGGER IF EXISTS temp.runs_ins",
    """
    CREATE TEMP TRIGGER runs_ins INSTEAD OF INSERT ON runs BEGIN
      INSERT INTO run(platform, started_at, finished_at, mode, ok_count, fail_count,
          items_new, items_upd, errors, note)
      VALUES('telegram', """ + _ts("NEW.started_at") + """, """ + _ts("NEW.finished_at") + """,
          NEW.mode, COALESCE(NEW.channels_ok, 0), COALESCE(NEW.channels_fail, 0),
          COALESCE(NEW.posts_new, 0), COALESCE(NEW.posts_upd, 0),
          COALESCE(NEW.errors, 0), NEW.note);
      INSERT OR REPLACE INTO tg_last_insert(k, id) VALUES('run', last_insert_rowid());
    END
    """,
    "DROP TRIGGER IF EXISTS temp.runs_upd",
    """
    CREATE TEMP TRIGGER runs_upd INSTEAD OF UPDATE ON runs BEGIN
      UPDATE run SET
        started_at = """ + _ts("NEW.started_at") + """,
        finished_at = """ + _ts("NEW.finished_at") + """,
        mode = NEW.mode,
        ok_count = NEW.channels_ok,
        fail_count = NEW.channels_fail,
        items_new = NEW.posts_new,
        items_upd = NEW.posts_upd,
        errors = NEW.errors,
        note = COALESCE(NEW.note, note)
      WHERE platform='telegram' AND id = OLD.id;
    END
    """,

    # ==================================================================== run_log
    "DROP VIEW IF EXISTS temp.tg_run_log",
    """
    CREATE TEMP VIEW tg_run_log AS
    SELECT rl.id        AS id,
           rl.run_id    AS run_id,
           rl.ts        AS ts,
           rl.level     AS level,
           rl.ref       AS handle,
           rl.msg       AS msg
    FROM main.run_log rl
    WHERE rl.platform = 'telegram'
    """,
    "DROP TRIGGER IF EXISTS temp.tg_run_log_ins",
    """
    CREATE TEMP TRIGGER tg_run_log_ins INSTEAD OF INSERT ON tg_run_log BEGIN
      INSERT INTO run_log(run_id, platform, ts, level, ref, msg)
      VALUES(NEW.run_id, 'telegram',
          COALESCE(""" + _ts("NEW.ts") + """, strftime('%Y-%m-%d %H:%M:%S','now')),
          NEW.level, NEW.handle, NEW.msg);
    END
    """,

    # =============================================================== metrics_daily
    "DROP VIEW IF EXISTS temp.tg_metrics_daily",
    """
    CREATE TEMP VIEW tg_metrics_daily AS
    SELECT m.day                AS day,
           m.items_ingested     AS posts_ingested,
           m.dup_rate           AS dup_rate,
           m.coverage           AS coverage_est,
           m.latency_p95_min    AS latency_p90_min,
           m.enriched_ratio     AS enrich_cov,
           m.errors             AS errors
    FROM main.metrics_daily m
    WHERE m.platform = 'telegram'
    """,
    "DROP TRIGGER IF EXISTS temp.tg_md_ins",
    """
    CREATE TEMP TRIGGER tg_md_ins INSTEAD OF INSERT ON tg_metrics_daily BEGIN
      INSERT INTO metrics_daily(day, platform, items_ingested, dup_rate, coverage,
          latency_p95_min, enriched_ratio, errors)
      VALUES(NEW.day, 'telegram', NEW.posts_ingested, NEW.dup_rate, NEW.coverage_est,
          NEW.latency_p90_min, NEW.enrich_cov, NEW.errors)
      ON CONFLICT(day, platform) DO UPDATE SET
          items_ingested=excluded.items_ingested, dup_rate=excluded.dup_rate,
          coverage=excluded.coverage, latency_p95_min=excluded.latency_p95_min,
          enriched_ratio=excluded.enriched_ratio, errors=excluded.errors;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.tg_md_upd",
    """
    CREATE TEMP TRIGGER tg_md_upd INSTEAD OF UPDATE ON tg_metrics_daily BEGIN
      UPDATE metrics_daily SET
          items_ingested=NEW.posts_ingested, dup_rate=NEW.dup_rate,
          coverage=NEW.coverage_est, latency_p95_min=NEW.latency_p90_min,
          enriched_ratio=NEW.enrich_cov, errors=NEW.errors
      WHERE platform='telegram' AND day = OLD.day;
    END
    """,
]


def _install_indexes(conn: sqlite3.Connection) -> list[str]:
    """Создать индексы адаптера через ядровой страж (ТЗ-3d)."""
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
    """Создать TEMP-представления и триггеры совместимости с legacy Telegram."""
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
        core_sqlcompat.enable_compat_sql(conn, TG_COMPAT_VIEWS)


def _drop_statements() -> list[str]:
    return [s for s in _COMPAT_STATEMENTS
            if s.strip().upper().startswith(("DROP VIEW", "DROP TRIGGER"))]


def uninstall_compat(conn: sqlite3.Connection) -> None:
    """Снять TEMP-представления/триггеры совместимости (перед миграцией ядра)."""
    core_sqlcompat.disable_compat_sql(conn)
    for stmt in _drop_statements():
        conn.execute(stmt)


def compat_view_name(name: str) -> str:
    """Имя TEMP-представления, отдающего legacy-объект ``name``."""
    return TG_COMPAT_VIEWS.get(name, name)


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
        "channels", "posts", "scores", "channel_baselines", "classified",
        "stories", "story_members", "account_state", "runs", "run_log",
        "metrics_daily",
    )
