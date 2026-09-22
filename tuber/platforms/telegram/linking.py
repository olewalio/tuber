"""Связывание очереди ``candidate`` с реестром ``source`` для Telegram (ТЗ-51, D-55).

Зачем
-----
Дискавери-очередь ``candidate`` и реестр ``source`` исторически жили порознь:
``handle`` в них хранился как пришёл из источника (``@X``, ``https://t.me/X``,
``X``, ``x/s``), поэтому одно и то же название канала могло дать две строки.
Промоушен реестра (``candidate → active``) искал строку очереди по «сырому»
``handle`` и не находил её — маркер ``promoted_at`` в очереди оставался пустым
(D-55).

Правило
-------
Один канонический ключ для хендла:

* снять ``@`` и пробелы;
* убрать схему и хост ``t.me``/``telegram.me``/``telegram.dog``/``telegram.org``;
* отбросить хвост ``/s`` и ``/123`` (номер сообщения);
* привести к нижнему регистру.

Плюс второй ключ — ``tg_id`` (числовой идентификатор канала): у реестра он лежит
в ``source.external_id``, у очереди — в ``candidate.external_id`` или
``candidate.meta_json``. Совпадения по любому из ключей достаточно для связи.

Модуль только читает; индексы строятся в памяти (объём Telegram-реестра —
тысячи строк, скан дешевле, чем запрос на каждый хендл).
"""
from __future__ import annotations

import json
import re

#: Хосты, которые может нести ссылка Telegram.
_TG_HOST_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?"
    r"(?:t\.me|telegram\.me|telegram\.dog|telegram\.org)/(.+)$",
    re.IGNORECASE,
)
_VALID_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]+$")


def normalize_handle(raw) -> str:
    """Канонический ключ хендла (``@X`` / ``t.me/x`` / ``X`` / ``x/s`` → ``x``)."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    m = _TG_HOST_RE.match(s)
    if m:
        s = m.group(1)
    # Отбросить query/fragment (t.me/x?start=1).
    s = s.split("?", 1)[0].split("#", 1)[0]
    s = s.strip().lstrip("@").strip().strip("/")
    # Взять первый сегмент пути: handle/123, handle/s → handle.
    if "/" in s:
        s = s.split("/", 1)[0]
    s = s.strip().strip("/")
    if s.endswith("/s"):
        s = s[:-2]
    s = s.lower()
    if not _VALID_HANDLE_RE.match(s):
        return ""
    return s


def _field(row, name):
    """Значение поля строки (``sqlite3.Row``, dict или объект)."""
    if row is None:
        return None
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, None)


def _meta_dict(row) -> dict:
    raw = _field(row, "meta_json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def tg_id_of(row) -> str | None:
    """Числовой ``tg_id`` строки: ``external_id`` либо ``meta_json.tg_id``."""
    for key in ("external_id", "tg_id"):
        value = _field(row, key)
        if value is not None and str(value).strip().isdigit():
            return str(value).strip()
    meta = _meta_dict(row)
    for key in ("tg_id", "telegram_id", "id"):
        value = meta.get(key)
        if value is not None and str(value).strip().isdigit():
            return str(value).strip()
    return None


def handle_key(row) -> str:
    """Канонический хендл строки (учитывает ``display_handle``, если есть)."""
    for key in ("handle", "display_handle"):
        value = _field(row, key)
        if value:
            key_norm = normalize_handle(value)
            if key_norm:
                return key_norm
    return ""


class Index:
    """Индекс строк по каноническому хендлу и по ``tg_id``."""

    def __init__(self):
        self.by_handle: dict[str, int] = {}
        self.by_tg_id: dict[str, int] = {}

    def add(self, row_id, handle=None, tg_id=None, row=None) -> None:
        key = normalize_handle(handle) if handle else ""
        if row is not None and not key:
            key = handle_key(row)
        if key:
            self.by_handle.setdefault(key, row_id)
        tid = tg_id or (tg_id_of(row) if row is not None else None)
        if tid:
            self.by_tg_id.setdefault(tid, row_id)

    def match(self, *, handle=None, tg_id=None, row=None) -> int | None:
        """id строки по любому из ключей (сначала ``tg_id``, затем хендл)."""
        tid = tg_id or (tg_id_of(row) if row is not None else None)
        if tid and tid in self.by_tg_id:
            return self.by_tg_id[tid]
        key = normalize_handle(handle) if handle else ""
        if not key and row is not None:
            key = handle_key(row)
        if key and key in self.by_handle:
            return self.by_handle[key]
        return None


def _load_index(con, sql: str, params) -> Index:
    index = Index()
    for row in con.execute(sql, params):
        index.add(row["id"], row=row)
    return index


def registry_index(con, platform: str = "telegram") -> Index:
    """Индекс реестра ``source`` (только строки платформы)."""
    return _load_index(
        con,
        "SELECT id, handle, external_id, meta_json FROM source"
        " WHERE platform = ?",
        (platform,),
    )


def queue_index(con, platform: str = "telegram") -> Index:
    """Индекс очереди ``candidate`` (только строки платформы)."""
    return _load_index(
        con,
        "SELECT id, handle, display_handle, external_id, meta_json FROM candidate"
        " WHERE platform = ?",
        (platform,),
    )


def find_source(con, *, handle=None, tg_id=None, platform: str = "telegram"):
    """id строки реестра по ``tg_id``/каноническому хендлу либо None."""
    match = registry_index(con, platform).match(handle=handle, tg_id=tg_id)
    return None if match is None else int(match)


def find_queue_row(con, *, handle=None, tg_id=None, platform: str = "telegram"):
    """id строки очереди по ``tg_id``/каноническому хендлу либо None."""
    match = queue_index(con, platform).match(handle=handle, tg_id=tg_id)
    return None if match is None else int(match)
