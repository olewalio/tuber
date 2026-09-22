"""Синтез прямой ссылки на материал по данным строки ``content`` (долг D-45).

До этой волны ссылку собирала сторона выдачи (:mod:`tuber.analysis.report`),
поэтому каждый новый потребитель (отчёт, дайджест, сайт) был обязан повторять
синтез. Здесь правила синтеза живут в ОДНОМ месте; заполнение ``content.url``
идёт при переносе/сборе (см. :mod:`tuber.core.storage` и адаптеры платформ), а
выдача просто читает готовую колонку.

Правила — по факту того, что реально лежит в данных (замер 17.09.2026):

* **YouTube** — ``external_id`` это идентификатор видео (``--8Rr6ahsGI``):
  ``https://www.youtube.com/watch?v=<external_id>``;
* **X** — ``external_id`` это номер статуса (``1565707185229017090``), handle
  берётся из ``content.author_handle`` или ``source.handle``:
  ``https://x.com/<handle>/status/<external_id>``;
* **Telegram** — ``external_id`` ядра имеет вид ``<канал>/<номер>``
  (``AGI_and_RL/1346``), номер сообщения числовой; канал берётся из ``handle``
  (источник) либо из префикса ``external_id``:
  ``https://t.me/<канал>/<номер>``.

Если любого компонента нет — **NULL**: выдумывать ссылку запрещено (прежний
синтез подставлял для X заглушку ``handle='i'`` — таких ссылок больше нет).
"""

from __future__ import annotations

YOUTUBE_WATCH_PREFIX = "https://www.youtube.com/watch?v="
X_HOST = "https://x.com"
TELEGRAM_HOST = "https://t.me"


def _clean(value) -> str | None:
    """Строковый компонент без пробелов и ведущей ``@``; пустое → ``None``."""
    if value is None:
        return None
    text = str(value).strip().lstrip("@").strip()
    return text or None


def content_url(platform: str, external_id: str | None,
                handle: str | None = None) -> str | None:
    """Собрать прямую ссылку на материал или вернуть ``None``.

    ``handle`` — реальный handle канала/автора (``content.author_handle`` или
    ``source.handle``). Ничего не выдумывается: не хватает компонента — ``None``.
    """
    ext = _clean(external_id)
    if not ext:
        return None

    if platform == "youtube":
        return YOUTUBE_WATCH_PREFIX + ext

    if platform == "x":
        h = _clean(handle)
        if not h:
            return None
        return f"{X_HOST}/{h}/status/{ext}"

    if platform == "telegram":
        channel, sep, message_id = ext.partition("/")
        channel = _clean(handle) or _clean(channel)
        message_id = _clean(message_id)
        if not sep or not channel or not message_id:
            return None
        return f"{TELEGRAM_HOST}/{channel}/{message_id}"

    return None


def material_url(db_url: str | None, platform: str, external_id: str | None,
                 handle: str | None = None) -> str | None:
    """Ссылка материала с приоритетом значения из базы (правило выдачи, D-45).

    Если в ``content.url`` уже лежит непустая ссылка (бэкфилл/сбор заполнили её),
    печатается она — синтез её не перекрывает. Синтез (:func:`content_url`)
    подключается только когда колонка пуста; если и для синтеза не хватает
    компонентов, честно возвращается ``None`` (выдача печатает «нет ссылки»).
    """
    existing = _clean(db_url)
    if existing:
        return existing
    return content_url(platform, external_id, handle)
