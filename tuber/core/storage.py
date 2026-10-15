"""Функции доступа к ядру (единственное место с SQL-записями ядра).

Требование ТЗ-1 §4: миграция не дублирует SQL, а пользуется этим слоем.
Все функции рассчитаны на то, что транзакцией управляет вызывающий код
(``tuber.core.db.write_tx``); сами они транзакций не открывают.

Каждый upsert идемпотентен: при повторном прогоне те же строки дают те же
идентификаторы и не создают дублей (опора на ``UNIQUE``/``PRIMARY KEY`` и
``ON CONFLICT DO UPDATE``).
"""

from __future__ import annotations

import atexit as _atexit
import json
import os as _os
import sqlite3
import threading as _threading
import time as _time
from contextlib import contextmanager

from tuber.core import timeutil
from tuber.core import urls


# ---------------------------------------------------------------------------
# Служебные хелперы
# ---------------------------------------------------------------------------

def jdump(obj) -> str | None:
    """Сериализовать объект в JSON; ``None`` → ``None``."""
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _filter_fields(conn: sqlite3.Connection, table: str, fields: dict) -> dict:
    allowed = _columns(conn, table)
    return {k: v for k, v in fields.items() if k in allowed and k not in ("id",)}


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    key: dict,
    fields: dict,
    *,
    update: bool = True,
) -> int:
    """Идемпотентный upsert по естественному ключу ``key``; вернуть rowid."""
    payload = dict(key)
    payload.update(_filter_fields(conn, table, fields))
    cols = list(payload.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(cols)
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"
    if update:
        conflict_cols = ", ".join(key.keys())
        assigns = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in key)
        if assigns:
            sql += f" ON CONFLICT({conflict_cols}) DO UPDATE SET {assigns}"
        else:
            sql += f" ON CONFLICT({conflict_cols}) DO NOTHING"
    else:
        sql += " ON CONFLICT DO NOTHING"
    cur = conn.execute(sql, [payload[c] for c in cols])
    if cur.lastrowid:
        return cur.lastrowid
    where = " AND ".join(f"{c} IS ?" for c in key)
    row = conn.execute(
        f"SELECT rowid FROM {table} WHERE {where}", list(key.values())
    ).fetchone()
    return row[0] if row else None


def _select_id(conn: sqlite3.Connection, table: str, key: dict) -> int | None:
    where = " AND ".join(f"{c} IS ?" for c in key)
    row = conn.execute(
        f"SELECT id FROM {table} WHERE {where}", list(key.values())
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# legacy_map
# ---------------------------------------------------------------------------

def set_legacy_map(
    conn: sqlite3.Connection,
    legacy_db: str,
    legacy_table: str,
    legacy_id,
    target_table: str,
    target_id,
    *,
    migrated_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO legacy_map (legacy_db, legacy_table, legacy_id,
                                target_table, target_id, migrated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(legacy_db, legacy_table, legacy_id) DO UPDATE SET
            target_table=excluded.target_table,
            target_id=excluded.target_id,
            migrated_at=excluded.migrated_at
        """,
        (legacy_db, legacy_table, str(legacy_id), target_table, target_id,
         migrated_at or timeutil.iso_now()),
    )


def get_legacy_map(
    conn: sqlite3.Connection, legacy_db: str, legacy_table: str, legacy_id
) -> object | None:
    """Вернуть ``target_id``.

    ``legacy_map.target_id`` — TEXT (см. TECH-DEBT D-02), поэтому после хранения
    SQLite отдаёт целочисленные id строками. Для обратной совместимости числовые
    значения возвращаются как ``int``; составные ключи (``platform|key|...``)
    остаются строками.
    """
    row = conn.execute(
        "SELECT target_id FROM legacy_map WHERE legacy_db=? AND legacy_table=? AND legacy_id=?",
        (legacy_db, legacy_table, str(legacy_id)),
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------

def upsert_source(conn: sqlite3.Connection, platform: str, handle: str, **fields) -> int:
    handle = handle if handle not in (None, "") else ""
    fields.setdefault("first_seen_at", None)
    _upsert(conn, "source", {"platform": platform, "handle": handle}, fields)
    sid = _select_id(conn, "source", {"platform": platform, "handle": handle})
    return sid


def upsert_source_baseline(conn: sqlite3.Connection, source_id: int, **fields) -> None:
    _upsert(conn, "source_baseline", {"source_id": source_id}, fields)


def merge_source_meta(conn: sqlite3.Connection, source_id: int, patch: dict) -> None:
    """Дописать поля в ``source.meta_json``, не теряя уже записанные."""
    row = conn.execute("SELECT meta_json FROM source WHERE id=?", (source_id,)).fetchone()
    meta: dict = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0])
        except (TypeError, ValueError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update(patch)
    conn.execute("UPDATE source SET meta_json=? WHERE id=?", (jdump(meta), source_id))


def set_source_cursor(conn: sqlite3.Connection, source_id: int, cursor, patch: dict | None = None) -> None:
    conn.execute("UPDATE source SET cursor=? WHERE id=?", (cursor, source_id))
    if patch:
        merge_source_meta(conn, source_id, patch)


#: Сколько последних снимков подписчиков хранить в ``source.meta_json``.
# D-56 закрыт (23.09.2026): ряд подписчиков X дополнительно живёт в отдельной
# таблице ``x_follower_history`` (без обрезки) — см. ``platforms/x/store.py``.
# Функция общая для платформ X (ТЗ-44) и Telegram (ТЗ-45); JSON-ряд оставлен
# как есть (может ещё читаться) и по-прежнему ограничен этой константой.
FOLLOWERS_HISTORY_MAX = 500


def set_follower_snapshot(conn: sqlite3.Connection, source_id: int, subs,
                          *, avg_views=None, taken_at=None) -> list[dict]:
    """Снимок подписчиков источника: ``source.subs``/``subs_at`` + ряд в meta_json.

    Общая функция ядра (ТЗ-44 X, ТЗ-45 Telegram): обновляет
    ``source.subs``/``subs_at`` (+ ``avg_views``, если передано) и дописывает
    точку ``{"at": ..., "subs": ...}`` в ``source.meta_json.followers_history``
    с обрезкой до :data:`FOLLOWERS_HISTORY_MAX` последних точек. Так появляется
    ряд роста без новой таблицы.

    Возвращает получившийся ряд. ``taken_at`` — ISO UTC ``YYYY-MM-DD HH:MM:SS``;
    по умолчанию текущее время. Повторная точка с тем же ``at`` перезаписывает
    прежнюю (идемпотентность повторного прогона).
    """
    taken = taken_at or timeutil.iso_now()
    row = conn.execute("SELECT meta_json FROM source WHERE id=?", (source_id,)).fetchone()
    history = []
    if row is not None and row[0]:
        try:
            history = (json.loads(row[0]) or {}).get("followers_history") or []
        except (ValueError, TypeError):
            history = []
    history = [h for h in history
               if isinstance(h, dict) and h.get("at") != taken]
    history.append({"at": taken, "subs": int(subs)})
    history = history[-FOLLOWERS_HISTORY_MAX:]
    conn.execute(
        "UPDATE source SET subs=?, subs_at=?,"
        " avg_views=COALESCE(?, avg_views),"
        " meta_json=json_set(COALESCE(meta_json,'{}'), '$.followers_history', json(?))"
        " WHERE id=?",
        (int(subs), taken, avg_views, json.dumps(history, ensure_ascii=False),
         source_id))
    return history


# ---------------------------------------------------------------------------
# content / metric_snapshot / content_latest
# ---------------------------------------------------------------------------

def upsert_content(
    conn: sqlite3.Connection, platform: str, external_id: str, **fields
) -> int:
    """Идемпотентно записать материал; ``url`` синтезируется, если не задан.

    D-45: чтобы долг «``content.url`` пуст» не накапливался заново, прямая ссылка
    заполняется здесь же, на общем пути записи. Правила — в
    :func:`tuber.core.urls.content_url`. Если компонентов не хватает, поле НЕ
    добавляется: существующее значение не затирается пустым.
    """
    if not fields.get("url"):
        handle = fields.get("author_handle")
        if handle is None and fields.get("source_id") is not None:
            row = conn.execute(
                "SELECT handle FROM source WHERE id=?", (fields["source_id"],)
            ).fetchone()
            handle = row[0] if row else None
        derived = urls.content_url(platform, external_id, handle)
        if derived:
            fields["url"] = derived
    _upsert(conn, "content", {"platform": platform, "external_id": str(external_id)}, fields)
    return _select_id(conn, "content", {"platform": platform, "external_id": str(external_id)})


#: Счётчик срабатываний клэмпа просмотров (ТЗ-B6, D-70). Единый клэмп живёт на
#: уровне записи снимка метрик, поэтому любой путь записи (очередь, сборщики,
#: прямые записи) получает один и тот же инвариант, а не только пути очереди.
_VIEWS_CLAMP_STATS: dict[str, int] = {"clamped": 0}


def reset_views_clamp_count() -> None:
    """Сбросить счётчик клэмпа просмотров (для тестов/отчёта, D-70)."""
    _VIEWS_CLAMP_STATS["clamped"] = 0


def views_clamp_count() -> int:
    """Сколько раз запись снимка пыталась понизить просмотры (D-70)."""
    return _VIEWS_CLAMP_STATS["clamped"]


def monotonic_views(conn: sqlite3.Connection, content_id: int, views) -> int | None:
    """Не позволить просмотрам убывать для материала (ТЗ-53/ТЗ-B6 D-70).

    Счётчик просмотров платформы монотонен: новое значение не может быть меньше
    уже сохранённого максимума для того же ``content_id``. Возвращает
    ``max(views, MAX(metric_snapshot.views))``; ``None`` пропускает как есть.
    Это единственная точка правды клэмпа: очередь (:func:`metrics._write_snapshot`)
    и общий писатель :func:`add_snapshot` зовут её, а не дублируют правило.
    """
    if views is None:
        return None
    row = conn.execute(
        "SELECT MAX(views) FROM metric_snapshot WHERE content_id=?",
        (content_id,)).fetchone()
    prev = row[0] if row is not None else None
    if prev is None:
        return views
    return max(int(prev), int(views))


def add_snapshot(
    conn: sqlite3.Connection, content_id: int, captured_at: str, **fields
) -> int:
    """Записать снимок метрик с клэмпом просмотров (ТЗ-B6, D-70).

    Клэмп монотонности перенесён С ЮДА на уровень записи снимка метрик: любой
    путь записи (X-сессия, прямые записи ``prelim``, миграции) больше не может
    записать убывание просмотров. При попытке записать меньшее значение
    сохраняется предыдущий максимум, а факт клэмпа инкрементирует счётчик
    :func:`views_clamp_count`. Пути очереди и Telegram-триггеры уже клампят сами,
    поэтому их результат не меняется (значение и так не меньше максимума).
    """
    fields = dict(fields)
    views = fields.get("views")
    if views is not None:
        clamped = monotonic_views(conn, content_id, views)
        if clamped is not None and int(clamped) != int(views):
            _VIEWS_CLAMP_STATS["clamped"] += 1
            fields["views"] = clamped
    return _upsert(
        conn, "metric_snapshot", {"content_id": content_id, "captured_at": captured_at}, fields
    )


def recompute_content_latest(conn: sqlite3.Connection, content_id: int | None = None,
                             *, platforms: tuple[str, ...] | None = None) -> int:
    """Пересчитать «последнее известное» состояние из последнего снапшота.

    ``platforms`` ограничивает пересчёт платформами (например, ``("x",)``):
    D-66 — у X ``views`` приходили в ``metric_snapshot``, но терялись при
    обновлении ``content_latest``, поэтому оси «охват на подписчика» и «реакции
    на 1 000» строились без X. Полный пересчёт без фильтра — для миграции.

    Просмотры монотонны: при конфликте берётся ``max(existing, excluded)``, а не
    последнее значение вслепую (ТЗ-53, ч.1.2). Иначе пересчёт из снимка, где
    счётчик платформы на миг «просел», понизил бы уже сохранённое значение
    ``content_latest.views`` и разошёлся бы с правилом :func:`monotonic_views`.
    """
    where_parts: list[str] = []
    params: list = []
    if content_id is not None:
        where_parts.append("content_id = ?")
        params.append(content_id)
    if platforms:
        marks = ",".join("?" for _ in platforms)
        where_parts.append(f"platform IN ({marks})")
        params.extend(platforms)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    cur = conn.execute(
        f"""
        INSERT INTO content_latest (content_id, captured_at, views, likes, comments, replies,
                                    reposts, forwards, reactions, views_per_day, views_per_hour)
        SELECT m.content_id, m.captured_at,
               COALESCE(m.views, (
                   SELECT m2.views FROM metric_snapshot m2
                    WHERE m2.content_id = m.content_id AND m2.views IS NOT NULL
                    ORDER BY m2.captured_at DESC LIMIT 1)),
               m.likes, m.comments, m.replies,
               m.reposts, m.forwards, m.reactions, m.views_per_day, m.views_per_hour
        FROM metric_snapshot m
        JOIN (
            SELECT content_id, MAX(captured_at) AS mc FROM metric_snapshot
            {where} GROUP BY content_id
        ) x ON x.content_id = m.content_id AND x.mc = m.captured_at
        ON CONFLICT(content_id) DO UPDATE SET
            captured_at=excluded.captured_at,
            views=CASE
                WHEN excluded.views IS NULL THEN content_latest.views
                WHEN content_latest.views IS NULL THEN excluded.views
                WHEN excluded.views > content_latest.views THEN excluded.views
                ELSE content_latest.views END,
            likes=excluded.likes,
            comments=excluded.comments, replies=excluded.replies, reposts=excluded.reposts,
            forwards=excluded.forwards, reactions=excluded.reactions,
            views_per_day=excluded.views_per_day, views_per_hour=excluded.views_per_hour
        """,
        params,
    )
    return cur.rowcount


def set_content_latest(conn: sqlite3.Connection, content_id: int, **fields) -> None:
    _upsert(conn, "content_latest", {"content_id": content_id}, fields)


# ---------------------------------------------------------------------------
# classification / classify_cache
# ---------------------------------------------------------------------------

def set_classification(conn: sqlite3.Connection, content_id: int, **fields) -> None:
    _upsert(conn, "classification", {"content_id": content_id}, fields)


def set_classify_cache(conn: sqlite3.Connection, text_hash: str, **fields) -> None:
    _upsert(conn, "classify_cache", {"text_hash": text_hash}, fields)


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------

def upsert_score(
    conn: sqlite3.Connection, content_id: int, computed_at: str, **fields
) -> None:
    _upsert(conn, "score", {"content_id": content_id, "computed_at": computed_at}, fields)


# ---------------------------------------------------------------------------
# story / story_member
# ---------------------------------------------------------------------------

def add_story(conn: sqlite3.Connection, **fields) -> int:
    cols = _filter_fields(conn, "story", fields)
    if not cols:
        cur = conn.execute("INSERT INTO story DEFAULT VALUES")
        return cur.lastrowid
    col_sql = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO story ({col_sql}) VALUES ({placeholders})", list(cols.values())
    )
    return cur.lastrowid


def add_story_member(
    conn: sqlite3.Connection, story_id: int, content_id: int, **fields
) -> None:
    _upsert(conn, "story_member", {"story_id": story_id, "content_id": content_id}, fields)


# ---------------------------------------------------------------------------
# candidate
# ---------------------------------------------------------------------------

def add_candidate(conn: sqlite3.Connection, platform: str, handle: str, **fields) -> int:
    handle = handle if handle not in (None, "") else ""
    _upsert(conn, "candidate", {"platform": platform, "handle": handle}, fields)
    row = conn.execute(
        "SELECT id FROM candidate WHERE platform=? AND handle=?", (platform, handle)
    ).fetchone()
    return row[0]


def promote_candidate(
    conn: sqlite3.Connection,
    platform: str,
    handle: str,
    *,
    status: str = "promoted",
    promoted_by: str | None = None,
    promoted_at: str | None = None,
    **fields,
) -> int | None:
    payload = dict(fields)
    payload.update(
        status=status,
        promoted_by=promoted_by,
        promoted_at=promoted_at or timeutil.iso_now(),
    )
    payload = _filter_fields(conn, "candidate", payload)
    if not payload:
        return None
    assigns = ", ".join(f"{c}=?" for c in payload)
    conn.execute(
        f"UPDATE candidate SET {assigns} WHERE platform=? AND handle=?",
        list(payload.values()) + [platform, handle],
    )
    row = conn.execute(
        "SELECT id FROM candidate WHERE platform=? AND handle=?", (platform, handle)
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# run / run_log
# ---------------------------------------------------------------------------

def add_run(conn: sqlite3.Connection, platform: str, **fields) -> int:
    fields = dict(fields)
    fields["platform"] = platform
    cols = _filter_fields(conn, "run", fields)
    col_sql = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO run ({col_sql}) VALUES ({placeholders})", list(cols.values())
    )
    # Часть 1 (надёжность журнала прогонов): берём прогон под наблюдение
    # процесса, чтобы аварийный выход закрыл строку, а не оставил её висеть.
    register_open_run(conn, cur.lastrowid)
    return cur.lastrowid


def log_run(
    conn: sqlite3.Connection, run_id: int | None, ts: str, level: str,
    ref: str | None, msg: str | None, platform: str | None = None,
) -> int:
    """Строка журнала прогона.

    ``platform`` (ТЗ-3b, D-24) — принадлежность платформе. Нужна строкам БЕЗ
    ``run_id`` (внепрогонные предупреждения X): без неё их нельзя отличить от
    строк Telegram, кроме как догадкой по ``run``.
    """
    cur = conn.execute(
        "INSERT INTO run_log (run_id, platform, ts, level, ref, msg) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, platform, ts, level, ref, msg),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Прочие общие таблицы (на естественных ключах)
# ---------------------------------------------------------------------------

def upsert_topic(conn: sqlite3.Connection, name: str, platform: str | None = None, weight=None) -> None:
    _upsert(conn, "topic", {"name": name}, {"platform": platform, "weight": weight})


def upsert_blocklist(conn: sqlite3.Connection, platform: str, handle: str, **fields) -> None:
    _upsert(conn, "blocklist", {"platform": platform, "handle": handle}, fields)


def upsert_metrics_daily(conn: sqlite3.Connection, day: str, platform: str, **fields) -> None:
    _upsert(conn, "metrics_daily", {"day": day, "platform": platform}, fields)


def upsert_cursor(
    conn: sqlite3.Connection, platform: str, kind: str, ref: str, **fields
) -> None:
    """Курсор пагинации: ``(platform, kind, ref)`` → ``cursor`` (TECH-DEBT D-05)."""
    _upsert(conn, "cursor", {"platform": platform, "kind": kind, "ref": ref}, fields)


def upsert_classify_daily(conn: sqlite3.Connection, day: str, platform: str, **fields) -> None:
    """Дневная статистика LLM-классификации (TECH-DEBT D-04)."""
    _upsert(conn, "classify_daily", {"day": day, "platform": platform}, fields)


def upsert_report_text(conn: sqlite3.Connection, text_hash: str, **fields) -> None:
    _upsert(conn, "report_text", {"text_hash": text_hash}, fields)


def upsert_transport_instance(conn: sqlite3.Connection, platform: str, host: str, **fields) -> None:
    _upsert(conn, "transport_instance", {"platform": platform, "host": host}, fields)


def upsert_transport_account_state(conn: sqlite3.Connection, platform: str, name: str, **fields) -> None:
    _upsert(conn, "transport_account_state", {"platform": platform, "name": name}, fields)


def upsert_quota_usage(
    conn: sqlite3.Connection, platform: str, day: str, key_id: str | None,
    endpoint: str, **fields,
) -> None:
    key = {"platform": platform, "key_id": key_id or "", "day": day, "endpoint": endpoint or ""}
    _upsert(conn, "quota_usage", key, fields)


def upsert_seo_field(conn: sqlite3.Connection, content_id: int, **fields) -> None:
    _upsert(conn, "seo_field", {"content_id": content_id}, fields)


def upsert_content_comment(conn: sqlite3.Connection, comment_id: str, **fields) -> None:
    _upsert(conn, "content_comment", {"comment_id": comment_id}, fields)


def upsert_comment_check(conn: sqlite3.Connection, content_id: int, **fields) -> None:
    _upsert(conn, "comment_check", {"content_id": content_id}, fields)


def add_thumbnail_vision(conn: sqlite3.Connection, content_id: int | None, **fields) -> int:
    payload = _filter_fields(conn, "thumbnail_vision", fields)
    payload["content_id"] = content_id
    cols = list(payload.keys())
    cur = conn.execute(
        f"INSERT INTO main.thumbnail_vision ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        list(payload.values()),
    )
    return cur.lastrowid


def add_transport_request(conn: sqlite3.Connection, platform: str, **fields) -> int:
    payload = _filter_fields(conn, "transport_request", fields)
    payload["platform"] = platform
    cols = list(payload.keys())
    cur = conn.execute(
        f"INSERT INTO transport_request ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        list(payload.values()),
    )
    return cur.lastrowid


def add_llm_usage(conn: sqlite3.Connection, platform: str, **fields) -> int:
    payload = _filter_fields(conn, "llm_usage", fields)
    payload["platform"] = platform
    cols = list(payload.keys())
    cur = conn.execute(
        f"INSERT INTO llm_usage ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        list(payload.values()),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Надёжность журнала прогонов (ТЗ «волна 2», часть 1)
# ---------------------------------------------------------------------------
#
# Было: ``finish_run`` вызывался ПОСЛЕДНЕЙ строкой тела команды. Исключение,
# аварийный выход или зависание оставляли строку ``run`` с ``finished_at IS NULL``
# навсегда. Три уровня защиты:
#
# 1. :func:`close_run` — идемпотентное закрытие: трогает только незакрытые
#    строки, поэтому повторный вызов не портит уже проставленные счётчики;
# 2. :func:`run_guard` — контекстный менеджер: закрывает прогон в ``finally``
#    при исключении (``note='error:<Тип>'``) и по дедлайну (``note='deadline'``);
# 3. реестр открытых прогонов + ``atexit`` — страховка на аварийный выход
#    процесса, где ``finally`` не выполнился.
#
# :func:`abandon_open_runs` — разовая ИДЕМПОТЕНТНАЯ реконсиляция «зависших»
# строк (для прогонов, оставшихся до этой правки).

_OPEN_RUNS: dict[int, tuple[str, int]] = {}   # run_id -> (путь к базе, pid)
_OPEN_RUNS_LOCK = _threading.Lock()
_ATEXIT_REGISTERED = False


def _db_path_of(conn) -> str | None:
    """Путь к основной базе соединения (``None`` для ``:memory:``)."""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return str(row[2])
    except sqlite3.DatabaseError:
        return None
    return None


def register_open_run(conn, run_id: int | None) -> None:
    """Взять прогон под наблюдение процесса (для ``atexit``-страховки)."""
    if not run_id:
        return
    path = _db_path_of(conn)
    if not path:
        return
    global _ATEXIT_REGISTERED
    with _OPEN_RUNS_LOCK:
        _OPEN_RUNS[int(run_id)] = (path, _os.getpid())
        if not _ATEXIT_REGISTERED:
            _atexit.register(_close_registered_runs)
            _ATEXIT_REGISTERED = True


def unregister_open_run(run_id: int | None) -> None:
    """Снять прогон с наблюдения (после явного закрытия)."""
    if not run_id:
        return
    with _OPEN_RUNS_LOCK:
        _OPEN_RUNS.pop(int(run_id), None)


def _close_registered_runs(note: str = "abandoned: процесс завершился") -> int:
    """Закрыть незакрытые прогоны ЭТОГО процесса (вызывается из ``atexit``)."""
    pid = _os.getpid()
    with _OPEN_RUNS_LOCK:
        own = [(rid, path) for rid, (path, rpid) in _OPEN_RUNS.items()
               if rpid == pid]
        for rid, _path in own:
            _OPEN_RUNS.pop(rid, None)
    closed = 0
    for run_id, path in own:
        try:
            con = sqlite3.connect(path, timeout=5.0)
            try:
                cur = con.execute(
                    "UPDATE main.run SET finished_at=?, note=?"
                    " WHERE id=? AND finished_at IS NULL",
                    (timeutil.iso_now(), note, int(run_id)))
                con.commit()
                closed += cur.rowcount
            finally:
                con.close()
        except Exception:  # noqa: BLE001 — страховка не имеет права падать
            pass
    return closed


def close_open_runs_now(note: str = "error: unhandled") -> int:
    """Немедленно закрыть незакрытые прогоны процесса (для CLI-перехвата)."""
    return _close_registered_runs(note)


def close_run(conn, run_id: int | None, *, note: str | None = None,
              ok: int | None = None, fail: int | None = None,
              items_new: int | None = None, items_upd: int | None = None,
              errors: int | None = None) -> int:
    """Идемпотентно закрыть прогон: только строки с ``finished_at IS NULL``.

    Второй закрывающий вызов возвращает 0 и НЕ затирает уже записанные
    ``finished_at``/``note``/счётчики. Снимает прогон с наблюдения процесса.
    """
    if run_id is None:
        return 0
    sets = ["finished_at=?"]
    params: list = [timeutil.iso_now()]
    for col, value in (("ok_count", ok), ("fail_count", fail),
                       ("items_new", items_new), ("items_upd", items_upd),
                       ("errors", errors)):
        if value is not None:
            sets.append(f"{col}=?")
            params.append(value)
    if note is not None:
        sets.append("note=?")
        params.append(note)
    params.append(int(run_id))
    cur = conn.execute(
        f"UPDATE main.run SET {', '.join(sets)}"
        " WHERE id=? AND finished_at IS NULL", params)
    try:
        conn.commit()
    except sqlite3.DatabaseError:
        pass
    unregister_open_run(run_id)
    return cur.rowcount


class _RunDeadlineWatchdog(_threading.Thread):
    """Сторожевой поток: по истечении дедлайна закрывает прогон ``note='deadline'``."""

    def __init__(self, db_path: str | None, run_id: int, seconds: float):
        super().__init__(name=f"run-deadline-{run_id}", daemon=True)
        self._db_path = db_path
        self._run_id = int(run_id)
        self._seconds = float(seconds)
        self._stop = _threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - таймингом проверяется косвенно
        if self._stop.wait(self._seconds):
            return
        if not self._db_path:
            return
        try:
            con = sqlite3.connect(self._db_path, timeout=5.0)
            try:
                con.execute(
                    "UPDATE run SET finished_at=?, note=?"
                    " WHERE id=? AND finished_at IS NULL",
                    (timeutil.iso_now(), "deadline", self._run_id))
                con.commit()
            finally:
                con.close()
        except Exception:  # noqa: BLE001 — дедлайн не имеет права падать
            pass
        unregister_open_run(self._run_id)


@contextmanager
def run_guard(conn, run_id, *, note: str | None = None,
              deadline_seconds: float | None = None):
    """Гарантировать, что прогон закрыт, даже при исключении.

    * нормальный выход — закрывается ``note`` (если тело уже вызвало
      ``finish_run``, повторный вызов — no-op и не портит счётчики);
    * исключение — ``note='error:<Тип>'``, исключение пробрасывается дальше;
    * превышение ``deadline_seconds`` — ``note='deadline'`` (пишет сторожевой
      поток на своём соединении, поэтому зависшее тело не держит строку открытой).
    """
    started = _time.monotonic()
    watchdog = None
    if deadline_seconds:
        watchdog = _RunDeadlineWatchdog(_db_path_of(conn), run_id, deadline_seconds)
        watchdog.start()
    try:
        yield run_id
    except BaseException as exc:  # noqa: BLE001 — обязаны закрыть и пробросить
        close_run(conn, run_id, note=f"error:{type(exc).__name__}")
        raise
    else:
        if deadline_seconds and (_time.monotonic() - started) > float(deadline_seconds):
            close_run(conn, run_id, note="deadline")
        else:
            close_run(conn, run_id, note=note)
    finally:
        if watchdog is not None:
            watchdog.cancel()


def abandon_open_runs(conn, *, older_hours: int = 1,
                      note: str = "abandoned: ревизия 03.10.2026") -> dict:
    """Разовая реконсиляция: закрыть прогоны старше порога (идемпотентно).

    Условие — ``finished_at IS NULL`` И ``started_at`` старее ``older_hours``
    (сравнение через ``julianday``: даты в базе бывают и с ``T``, и с пробелом).
    ``finished_at = started_at`` (когда прогон реально перестал жить неизвестно),
    ``note`` — переданная причина. Возвращает счётчики закрытых и остаток.
    """
    cutoff = f"-{int(older_hours)} hours"
    by_platform = {
        (r[0] or "(null)"): int(r[1]) for r in conn.execute(
            "SELECT platform, COUNT(*) FROM main.run"
            " WHERE finished_at IS NULL AND started_at IS NOT NULL"
            "   AND julianday(started_at) < julianday('now', ?)"
            " GROUP BY platform", (cutoff,))}
    cur = conn.execute(
        "UPDATE main.run SET finished_at=started_at, note=?"
        " WHERE finished_at IS NULL AND started_at IS NOT NULL"
        "   AND julianday(started_at) < julianday('now', ?)",
        (note, cutoff))
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM main.run WHERE finished_at IS NULL"
        " AND started_at IS NOT NULL"
        " AND julianday(started_at) < julianday('now', '-1 hour')").fetchone()[0]
    return {"closed": cur.rowcount, "by_platform": by_platform,
            "remaining_stale": int(remaining)}
