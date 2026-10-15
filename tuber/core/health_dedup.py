"""Дедуп алертов сторожа (часть 2 «волна 2»).

Зачем
-----
Сторож X запускается каждые 30 минут и печатает одну и ту же строку ALERT,
пока проблема жива: при ``deliver=origin`` это спам владельцу (с 03.10.2026
доставка включена). Здесь одна и та же проблема докладывается не чаще одного
раза в :data:`DEFAULT_WINDOW_HOURS`; когда проблема исчезла — ключ удаляется,
поэтому следующая вспышка снова доложит (дедуп не «залипает» навсегда).

Состояние — маленький JSON-файл (атомарная запись через ``os.replace``):
``{sha1(текста): метка времени последней отправки}``. Именно последней, а не
первой: иначе после первой отправки проблема молчала бы вечно. Метка
обновляется при каждой реальной отправке, поэтому окно скользящее
(не чаще одного раза в ``window_hours``).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

#: Окно дедупа по умолчанию, часы.
DEFAULT_WINDOW_HOURS = 6.0


def alert_key(text: str) -> str:
    """Ключ алерта: ``sha1`` его текста (одинаковый текст — один ключ)."""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def load_seen(path: str) -> dict[str, float]:
    """Прочитать состояние; битый/отсутствующий файл — пустое состояние."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in data.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def save_seen(path: str, seen: dict[str, float]) -> None:
    """Атомарно записать состояние (``os.replace`` в том же каталоге)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".health_seen", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(seen, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _text_of(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("msg") or item.get("text") or "")
    return str(item)


def filter_alerts(alerts, path: str, *, now: float | None = None,
                  window_hours: float = DEFAULT_WINDOW_HOURS) -> list[str]:
    """Вернуть тексты, которые надо отправить СЕЙЧАС, и обновить состояние.

    * проблема, которой нет в текущем наборе, теряет ключ (исчезла — сброс);
    * одинаковая проблема уходит не чаще одного раза в ``window_hours``;
    * при повторной отправке через окно метка обновляется (скользящее окно).
    """
    now_ts = time.time() if now is None else float(now)
    ordered: list[tuple[str, str]] = []
    keys: set[str] = set()
    for item in alerts:
        text = _text_of(item)
        key = alert_key(text)
        keys.add(key)
        ordered.append((key, text))

    seen = load_seen(path)
    # Проблема исчезла со строки — ключ удаляется (следующая вспышка доложит).
    seen = {key: ts for key, ts in seen.items() if key in keys}

    emit: list[str] = []
    for key, text in ordered:
        last = seen.get(key)
        if last is None or (now_ts - last) >= float(window_hours) * 3600.0:
            emit.append(text)
            seen[key] = now_ts
    save_seen(path, seen)
    return emit
