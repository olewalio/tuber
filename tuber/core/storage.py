"""Функции доступа к ядру (единственное место с SQL-записями ядра).

Требование ТЗ-1 §4: миграция не дублирует SQL, а пользуется этим слоем.
Все функции рассчитаны на то, что транзакцией управляет вызывающий код
(``tuber.core.db.write_tx``); сами они транзакций не открывают.

Каждый upsert идемпотентен: при повторном прогоне те же строки дают те же
идентификаторы и не создают дублей (опора на ``UNIQUE``/``PRIMARY KEY`` и
``ON CONFLICT DO UPDATE``).
"""

from __future__ import annotations

import json
import sqlite3

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
# TODO(debt-D-56): ряд роста подписчиков живёт в meta_json (обрезка 500 точек),
# отдельной таблицы-ряда нет — см. TECH-DEBT.md D-56. Функция общая для
# платформ X (ТЗ-44) и Telegram (ТЗ-45): второй формат ряда плодить нельзя.
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


def add_snapshot(
    conn: sqlite3.Connection, content_id: int, captured_at: str, **fields
) -> int:
    # TODO(debt-D-70): остаток ТЗ-53 — монотонность просмотров закреплена на
    # путях очереди (`metrics._write_snapshot`) и сборщика Telegram
    # (`collect.py` + триггеры `telegram/store.py`), но этот общий писатель и
    # X-путь (`x/store.py::record_session_metrics`) по-прежнему могут записать
    # меньше сохранённого; `content_latest` тоже не клампится при прямых записях
    # (например `telegram/prelim.py`). Ввести единый клэмп на уровне
    # `metric_snapshot`/`add_snapshot`. См. TECH-DEBT.md D-70.
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
            captured_at=excluded.captured_at, views=excluded.views, likes=excluded.likes,
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
