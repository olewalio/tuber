"""Единый разбор и формат дат ядра.

Инвариант §3 ТЗ-1: **все** даты в единой базе — TEXT ISO-8601 UTC вида
``YYYY-MM-DD HH:MM:SS``. Legacy-базы разнородны:

* tuber-os хранит epoch-секунды в INTEGER-колонках;
* tuber-x и tuber-telegram — ISO-строки ``YYYY-MM-DD HH:MM:SS`` (иногда с
  ``T`` и ``Z``).

Все конвертации при миграции идут только через этот модуль, чтобы не
размазывать форматы по коду.
"""

from __future__ import annotations

import datetime as _dt
import re

ISO_FMT = "%Y-%m-%d %H:%M:%S"
ISO_T_FMT = "%Y-%m-%dT%H:%M:%S"

_DIGITS_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def epoch_to_iso(value: int | float | None) -> str | None:
    """epoch-секунды → ISO UTC. ``None`` пробрасывается как ``None``."""
    if value is None:
        return None
    try:
        secs = int(value)
    except (TypeError, ValueError):
        return None
    try:
        dt = _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.strftime(ISO_FMT)


def iso_to_epoch(value: str | None) -> int | None:
    """ISO / epoch → epoch-секунды (для обратной совместимости во вью)."""
    iso = parse_any(value)
    if iso is None:
        return None
    dt = _dt.datetime.strptime(iso, ISO_FMT).replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp())


def iso_now() -> str:
    """Текущее время в ISO UTC (без микросекунд)."""
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime(ISO_FMT)


def parse_any(value) -> str | None:
    """Привести любое представление времени к ISO UTC.

    Принимает:

    * ``None`` / пустую строку → ``None``;
    * ``int``/``float`` (epoch-секунды) → ISO;
    * строку из одних цифр (epoch) → ISO;
    * ``YYYY-MM-DD HH:MM:SS``, ``YYYY-MM-DDTHH:MM:SS``,
      ``YYYY-MM-DDTHH:MM:SSZ``, ``YYYY-MM-DDTHH:MM:SS+03:00`` и т.п.

    Неизвестный формат → ``None`` (а не исключение: битая дата в legacy не
    должна ронять миграцию).
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool — подкласс int, но это не время
        return None
    if isinstance(value, (int, float)):
        return epoch_to_iso(value)

    text = str(value).strip()
    if not text:
        return None

    # epoch-строка: "1788998400", "1788998400.5"
    if _DIGITS_RE.match(text) and not text.startswith("-0"):
        try:
            return epoch_to_iso(float(text))
        except (TypeError, ValueError):
            return None

    # ISO с буквой Z в конце.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    # Нормализуем разделитель даты/времени и пробуем распарсить.
    candidates = [text, text.replace(" ", "T", 1), text.replace("T", " ", 1)]
    for cand in candidates:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                dt = _dt.datetime.strptime(cand, fmt)
            except ValueError:
                continue
            return dt.strftime(ISO_FMT)

    # Последний шанс: ISO с явным смещением (fromisoformat, Python 3.7+).
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return dt.strftime(ISO_FMT)


def date_of(value) -> str | None:
    """ISO-строка → ``YYYY-MM-DD`` (для дневных агрегатов)."""
    iso = parse_any(value)
    return iso[:10] if iso else None
