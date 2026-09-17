"""Идентификаторы внешних сущностей в ядре.

Для YouTube и X внешний ключ глобально уникален сам по себе (``video_id``,
``tweet_id``). Для Telegram — нет: ``message_id`` уникален только внутри
канала (проверено на живой базе: 14 877 постов, но всего 6 939 различных
``message_id``). Поэтому для Telegram ``content.external_id`` — составной
``<handle>/<message_id>``.
"""

from __future__ import annotations


def tg_external_id(handle: str, message_id) -> str:
    """Составной внешний id Telegram-поста: ``<handle>/<message_id>``."""
    return f"{handle}/{message_id}"


def tg_message_id(external_id: str) -> int | None:
    """Обратное разворачивание составного id Telegram."""
    if not external_id or "/" not in external_id:
        return None
    tail = external_id.rsplit("/", 1)[1]
    try:
        return int(tail)
    except (TypeError, ValueError):
        return None


def plain_external_id(value) -> str:
    """Внешний id как строка (для YouTube/X — как есть)."""
    return "" if value is None else str(value)
