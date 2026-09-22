"""Стоп-лист дискавери (Р2.5).

Заводится пустым. Пополняется ТОЛЬКО через функции этого модуля (Р8): ручные
правки таблицы `blocklist` в обход этих функций запрещены. Причины попадания:
  * ручное добавление (оператор/аналитик);
  * отказ верификации с причинами `bad_handle`, `not_found`, `bot_pattern`,
    `duplicate_content` (Р3.2) — чтобы не проверять их повторно каждый день.

Служебные хендлы X (`search`, `explore`, `i`, `home`, ...) отбрасываются всегда,
независимо от содержимого стоп-листа.
"""
from __future__ import annotations

from . import config, store as db

# Причины отказа, по которым хендл уходит в стоп-лист навсегда (Р3.2).
BLOCKLIST_REASONS = ("bad_handle", "not_found", "bot_pattern", "duplicate_content")

SERVICE_HANDLES = frozenset(h.lower() for h in config.SERVICE_HANDLES)

# Дополнительный in-memory стоп-лист (например, ручные правки на прогон).
_EXTRA = set()


def add(con, handle, reason="manual", *, commit=True) -> bool:
    """Внести хендл в стоп-лист. Идемпотентно. True — если добавлен/обновлён."""
    from .registry import validate_handle
    h, err = validate_handle(handle)
    if err:
        # невалидный хендл и так не пройдёт дальше — пишем как есть
        h = str(handle).strip().lstrip("@").lower()
        if not h:
            return False
    con.execute(
        # UPSERT по представлению SQLite запрещает, поэтому «ON CONFLICT(handle)
        # DO UPDATE» делает триггер представления (та же семантика).
        "INSERT INTO blocklist (handle, reason, added_at) VALUES (?,?,?)",
        (h, reason, db.utcnow_iso()))
    if commit:
        con.commit()
    return True


def remove(con, handle, *, commit=True) -> bool:
    h = str(handle).strip().lstrip("@").lower()
    # rowcount у представления всегда 0 (строки меняет INSTEAD OF-триггер),
    # поэтому фактическое число удалённых строк берём из total_changes (D-23).
    before = con.total_changes
    con.execute("DELETE FROM blocklist WHERE handle=?", (h,))
    if commit:
        con.commit()
    return db.changes_since(con, before) > 0


def is_blocked(con, handle) -> bool:
    """True, если хендл служебный, в стоп-листе БД или в ручном стоп-листе."""
    h = str(handle).strip().lstrip("@").lower()
    if not h:
        return True
    if h in SERVICE_HANDLES or h in _EXTRA:
        return True
    row = con.execute("SELECT 1 FROM blocklist WHERE handle=?", (h,)).fetchone()
    return row is not None


def list_blocked(con, limit=None):
    q = "SELECT handle, reason, added_at FROM blocklist ORDER BY added_at DESC"
    if limit:
        q += " LIMIT ?"
        return con.execute(q, (limit,)).fetchall()
    return con.execute(q).fetchall()


def clear(con, *, commit=True):
    con.execute("DELETE FROM blocklist")
    if commit:
        con.commit()
