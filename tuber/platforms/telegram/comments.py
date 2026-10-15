"""Тексты ответов Telegram через Telethon (ТЗ-49, контур 6).

``t.me/s`` отдаёт только счётчик ответов — текст требует MTProto. Этот модуль
добирает тексты ответов к постам топ-каналов через Telethon (учётка контура уже
настроена: сессия ``/root/.hermes/swarm/sessions/tg_collector``, ключи
``TG_API_ID``/``TG_API_HASH`` в ``/root/.hermes/.env``).

Что делает
----------
* **``select_channels``** — топ-N каналов по вовлечённости (сумма
  ``score.engagement`` за окно, при отсутствии скоров — по просмотрам и
  форвардам). Числа прикладываются к каждой позиции.
* **``collect``** — по каждому каналу: последние ``COMMENT_MESSAGES_PER_CHANNEL``
  сообщений, у которых есть ответы; тексты ответов пишутся в
  ``content_comment`` (``platform='telegram'``), привязанные к строке
  ``content`` поста (``external_id = handle/message_id``).

Связь с D-64 (живая проверка 24.09.2026, долг закрыт)
-----------------------------------------------------
Ответы (``GetReplies``) читаются у **САМОГО КАНАЛА**: ``iter_messages(entity,
reply_to=<id поста>)``. Peer — канал, ``msg_id`` — пост; связанную группу
Telegram находит сам. Чтение по связанной группе даёт ``MsgIdInvalidError``:
у группы своя нумерация сообщений, поэтому id поста там невалиден (замер
24.09.2026: способ A — 2 ответа с текстами, способ B — ``MsgIdInvalidError``).
``GetFullChannelRequest`` (``linked_chat_id``) служит ТОЛЬКО флагом «ответы
возможны» и шлётся только каналам, у которых при сканировании нашёлся хотя бы
один пост с ``n_replies > 0``; канал без группы честно считается «нет связанной
группы» (``channels_no_group``), каналы без постов с ответами — «ответов нет»
(``channels_no_replies``). Пост с ответами, которого нет в ``content``,
досоздаётся (``_ensure_post_row``), а не пропускается.

Предохранители (плана п.3)
--------------------------
* пауза между запросами (``COMMENT_PAUSE_SEC``);
* потолок ответов за прогон (``COMMENT_MAX_REPLIES_PER_RUN``);
* честная остановка при ``FloodWaitError``: прогон прекращается, время спада
  пишется в БД (``transport_account_state.flood_until`` и ``cursor.meta_json``),
  не дожидаясь истечения;
* состояние в БД как у снимков метрик — курсор на канал
  (``cursor(kind='comments', ref=handle)``) хранит последний обработанный
  ``message_id``, счётчики и ``updated_at``; состояние не теряется между
  прогонами и видно в отчёте.

Честность: нет ответа — не выдумываем. Сбой по каналу/сообщению попадает в
``errors`` и не валит прогон; ``dry_run`` не трогает ни сеть, ни базу.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from tuber.core import storage, timeutil
from tuber.core import db as core_db

from .collect import SESSION_PATH, load_env

log = logging.getLogger(__name__)

#: Метки.
PLATFORM = "telegram"
CURSOR_KIND = "comments"
ACCOUNT_NAME = "tg_comments"

# --- Пороги/потолки (ТЗ-49, дополнено ТЗ-B12/D-64) -------------------------
#: Топ каналов по вовлечённости за прогон.
COMMENT_TOP_CHANNELS = 30
#: Лимит каналов за прогон для пути со связанными группами (D-64).
COMMENT_LINKED_MAX_CHANNELS = 200
#: Сколько последних сообщений канала просматривать на предмет ответов.
COMMENT_MESSAGES_PER_CHANNEL = 50
#: Сколько ответов максимум брать у одного сообщения.
COMMENT_REPLIES_PER_MESSAGE = 100
#: Потолок ответов за весь прогон (общий предохранитель).
COMMENT_MAX_REPLIES_PER_RUN = 3000
#: Потолок MTProto-запросов за весь прогон (ТЗ-B12: общая волна ≤ 1500).
COMMENT_MAX_REQUESTS_PER_RUN = 1500
#: Пауза между MTProto-запросами, с (лимит ~10 запросов/30с).
COMMENT_PAUSE_SEC = 1.5
#: Окно вовлечённости каналов, дней.
COMMENT_ENGAGEMENT_WINDOW_DAYS = 30


def _now_ts() -> int:
    return int(time.time())


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def engagement_sql() -> str:
    """SQL отбора топ-каналов по вовлечённости (числа прикладываются)."""
    return """
        SELECT c.source_id                                   AS source_id,
               s.handle                                       AS handle,
               s.title                                        AS title,
               COUNT(*)                                       AS posts,
               COALESCE(SUM(cl.views), 0)                     AS views,
               COALESCE(SUM(cl.forwards), 0)                  AS forwards,
               COALESCE(SUM(cl.reactions), 0)                 AS reactions,
               COALESCE(SUM(sc.engagement), 0)                AS engagement
        FROM content c
        JOIN source s         ON s.id = c.source_id AND s.platform = 'telegram'
        LEFT JOIN content_latest cl ON cl.content_id = c.id
        LEFT JOIN score sc    ON sc.content_id = c.id
        WHERE c.platform = 'telegram'
          AND c.published_at >= datetime('now', ?)
        GROUP BY c.source_id
        ORDER BY engagement DESC, views DESC
        LIMIT ?
    """


def select_channels(con, *, n: int = COMMENT_TOP_CHANNELS,
                    window_days: int = COMMENT_ENGAGEMENT_WINDOW_DAYS) -> list[dict]:
    """Топ каналов Telegram по вовлечённости за окно (только БД)."""
    rows = con.execute(
        engagement_sql(), (f"-{int(window_days)} days", int(n))
    ).fetchall()
    return [dict(r) for r in rows]


def _post_index(con, source_id: int) -> dict[str, int]:
    """Карта ``message_id`` (строка) → ``content.id`` для канала."""
    rows = con.execute(
        "SELECT id, external_id FROM content WHERE platform='telegram' "
        "AND source_id=?",
        (source_id,),
    ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        ext = r["external_id"] or ""
        if "/" in ext:
            out[ext.rsplit("/", 1)[1]] = r["id"]
    return out


def _is_flood(exc: BaseException) -> bool:
    """Похоже ли исключение на FloodWait (без жёсткой зависимости от Telethon).

    Сначала по имени класса (``FloodWaitError``/``FloodWait``), затем — точная
    проверка ``isinstance``, если Telethon установлен. Так модуль импортируется
    и тестируется без Telethon, а боевой прогон ловит настоящий flood-wait.
    """
    if "FloodWait" in type(exc).__name__:
        return True
    try:
        from telethon.errors import FloodWaitError
    except Exception:  # noqa: BLE001 — Telethon не установлен
        return False
    return isinstance(exc, FloodWaitError)


def _reply_likes(reply: Any) -> int | None:
    """Реакции ответа, если Telethon их отдал (лайков в Telegram нет)."""
    reactions = getattr(reply, "reactions", None)
    if reactions is None:
        return None
    try:
        results = getattr(reactions, "results", None) or []
        total = 0
        for res in results:
            total += int(getattr(res, "count", 0) or 0)
        return total
    except (TypeError, ValueError):
        return None


def _reply_fields(reply: Any, entity_handle: str, msg_id: int) -> dict | None:
    text = (getattr(reply, "message", None) or "").strip()
    if not text:
        return None
    rid = getattr(reply, "id", None)
    if rid is None:
        return None
    sender = getattr(reply, "sender", None)
    author = None
    if sender is not None:
        author = (getattr(sender, "title", None)
                  or " ".join(x for x in (getattr(sender, "first_name", None),
                                          getattr(sender, "last_name", None)) if x)
                  or getattr(sender, "username", None))
    date = getattr(reply, "date", None)
    published = int(date.timestamp()) if date is not None else None
    return {
        "comment_id": f"tg:{entity_handle}/{msg_id}/{rid}",
        "external_id": f"{entity_handle}/{msg_id}",
        "author": author or "—",
        "text": text,
        "likes": _reply_likes(reply),
        "published_at": published,
    }


def save_replies(con, source_id: int, handle: str, msg_id: int,
                 thread_id: int | None, replies: list[dict],
                 post_index: dict[str, int], captured_at: int) -> int:
    """Записать тексты ответов. Возвращает число новых строк.

    Ответ привязывается к строке ``content`` поста (если пост есть в базе);
    иначе — обходится (ответ есть, а поста нет: не наша выборка, не выдумываем
    связь).
    """
    cid = post_index.get(str(msg_id))
    if cid is None:
        return 0
    written = 0
    with core_db.write_tx(con):
        for r in replies:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO content_comment
                    (comment_id, content_id, platform, external_id, author, text,
                     likes, published_at, captured_at)
                VALUES (?, ?, 'telegram', ?, ?, ?, ?, ?, ?)
                """,
                (r["comment_id"], cid, r["external_id"], r["author"], r["text"],
                 r["likes"], timeutil.epoch_to_iso(r["published_at"]),
                 timeutil.epoch_to_iso(captured_at)),
            )
            if cur.rowcount and cur.rowcount > 0:
                written += 1
    return written


def _cursor_update(con, handle: str, *, last_message_id: int | None,
                   messages: int, replies: int, flood_until: str | None,
                   captured_at: int, dry_run: bool) -> None:
    if dry_run:
        return
    meta = {"last_message_id": last_message_id, "messages": messages,
            "replies": replies, "updated_at": timeutil.epoch_to_iso(captured_at)}
    if flood_until:
        meta["flood_until"] = flood_until
    storage.upsert_cursor(
        con, PLATFORM, CURSOR_KIND, handle,
        cursor=str(last_message_id) if last_message_id is not None else None,
        last_page_at=timeutil.epoch_to_iso(captured_at),
        items_total=replies,
        meta_json=storage.jdump(meta),
        updated_at=timeutil.epoch_to_iso(captured_at),
    )


def _set_flood(con, seconds: int, dry_run: bool) -> str:
    until = (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).strftime(
        "%Y-%m-%dT%H:%M:%S")
    if not dry_run:
        storage.upsert_transport_account_state(
            con, PLATFORM, ACCOUNT_NAME, flood_until=until,
            last_error=f"FLOOD_WAIT {int(seconds)}s",
            updated_at=_iso_now(),
        )
        con.commit()
    return until


def _msg_requests(limit: int) -> int:
    """Приблизительное число MTProto-запросов на ``iter_messages(limit=N)``.

    Telethon тянет по 100 сообщений за запрос, поэтому ``ceil(N/100)``. Это
    учёт бюджета (ТЗ-B12), а не точный счётчик сети.
    """
    n = int(limit)
    return 0 if n <= 0 else (n + 99) // 100


async def _linked_chat(client, entity):
    """``linked_chat_id`` канала обсуждений через GetFullChannelRequest (D-64).

    Возвращает id связанной группы или ``None``, если её нет. Telethon
    импортируется лениво: без него (юнит-тесты) вернётся ``None``; тесты
    подставляют свой ``linked_chat_lookup``.
    """
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
    except Exception:  # noqa: BLE001 — Telethon не установлен
        return None
    full = await client(GetFullChannelRequest(entity))
    return getattr(getattr(full, "full_chat", None), "linked_chat_id", None)


def _ensure_post_row(con, source_id: int, handle: str, msg: Any) -> int | None:
    """Завести строку ``content`` для поста с ответами, которого нет в выборке (D-64).

    Полнота сбора требует не пропускать пост, у которого есть ответы, только
    потому что его не было в нашей выборке ``content``. Возвращает id строки
    (существующей или созданной) либо ``None``, если у сообщения нет id.
    """
    mid = getattr(msg, "id", None)
    if mid is None:
        return None
    existing = con.execute(
        "SELECT id FROM content WHERE platform='telegram' AND source_id=? AND external_id=?",
        (int(source_id), f"{handle}/{mid}"),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    date = getattr(msg, "date", None)
    published = date.isoformat() if date is not None else None
    text = (getattr(msg, "message", None) or "").strip() or None
    cur = con.execute(
        "INSERT INTO content(platform, source_id, external_id, kind, published_at, text, first_seen_at)"
        " VALUES('telegram', ?, ?, 'post', ?, ?, datetime('now'))",
        (int(source_id), f"{handle}/{mid}", published, text),
    )
    new_id = cur.lastrowid
    # Закрываем неявную транзакцию INSERT: иначе следующий ``write_tx`` (BEGIN
    # IMMEDIATE в save_replies) падает «cannot start a transaction within a transaction».
    con.commit()
    return new_id


async def _collect_async(con, targets: list[dict], *, client, cfg,
                         max_replies: int, messages_per_channel: int,
                         replies_per_message: int, pause: float,
                         dry_run: bool, progress=None,
                         use_linked_chat: bool = False,
                         create_missing_posts: bool = False,
                         linked_chat_lookup=None,
                         max_requests: int | None = None) -> dict:
    summary = {
        "channels": 0, "channels_ok": 0, "channels_no_group": 0,
        "channels_no_replies": 0,
        "messages": 0, "replies": 0, "posts_created": 0,
        "errors": 0, "requests": 0, "flood_wait": 0, "flood_until": None,
        "stopped": False, "stop_reason": None, "top": [],
        "dry_run": bool(dry_run), "use_linked_chat": bool(use_linked_chat),
    }
    if max_requests is None:
        max_requests = COMMENT_MAX_REQUESTS_PER_RUN
    captured_at = _now_ts()
    total_replies = 0
    requests = 0

    def _budget(n: int = 1) -> bool:
        """Списать ``n`` запросов. False — бюджет исчерпан (запрос НЕ делаем)."""
        nonlocal requests
        if requests + int(n) > max_requests:
            summary["stopped"] = True
            summary["stop_reason"] = "request_budget"
            return False
        requests += int(n)
        return True

    def _stop_flood(handle: str, seconds: int, last_mid, msgs: int, rep: int) -> None:
        summary["flood_wait"] = int(seconds)
        summary["stopped"] = True
        summary["stop_reason"] = "flood_wait"
        summary["flood_until"] = _set_flood(con, int(seconds), dry_run)
        _cursor_update(con, handle, last_message_id=last_mid, messages=msgs,
                       replies=rep, flood_until=summary["flood_until"],
                       captured_at=captured_at, dry_run=dry_run)
        log.warning("tg comments: FLOOD_WAIT %ss — остановка", seconds)

    async def _gap() -> None:
        """Пауза между ЛЮБЫМИ MTProto-запросами (не только между постами)."""
        if pause:
            await asyncio.sleep(pause)

    for ch in targets:
        if total_replies >= max_replies or summary["stopped"]:
            break
        handle = ch.get("handle")
        if not handle:
            continue
        summary["channels"] += 1
        if not _budget(1):
            break
        try:
            entity = await client.get_entity(handle)
        except Exception as exc:  # noqa: BLE001
            if _is_flood(exc):
                _stop_flood(handle, int(getattr(exc, "seconds", 0) or 0), None, 0, 0)
                break
            summary["errors"] += 1
            log.warning("tg comments: канал %s не разрешён (%s)", handle, exc)
            continue
        await _gap()
        post_index = _post_index(con, int(ch["source_id"]))
        ch_messages = 0
        ch_replies = 0
        ch_created = 0
        last_mid: int | None = None
        posts_with_replies: list[Any] = []
        linked_id = None
        linked_checked = False
        try:
            # Сначала сканирование постов: только оно решает, нужен ли
            # GetFullChannelRequest. Каналам без постов с ответами он не нужен.
            if not _budget(_msg_requests(messages_per_channel)):
                break
            async for m in client.iter_messages(entity, limit=messages_per_channel):
                if summary["stopped"]:
                    break
                mid = getattr(m, "id", None)
                if mid is not None:
                    last_mid = mid
                replies_field = getattr(m, "replies", None)
                n_replies = int(getattr(replies_field, "replies", 0) or 0)
                if n_replies > 0:
                    posts_with_replies.append(m)
            # D-64: связанная группа — только ФЛАГ «ответы возможны» и только
            # если у канала есть хоть один пост с ответами. Порядок менять
            # нельзя: сначала сканирование, потом (при необходимости) запрос.
            if use_linked_chat and posts_with_replies:
                lookup = linked_chat_lookup or _linked_chat
                if not _budget(1):
                    break
                linked_id = await lookup(client, entity)
                await _gap()
                linked_checked = True
                if not linked_id:
                    # Группы нет — «нет связанной группы» явно, не выдумываем.
                    summary["channels_no_group"] += 1
                    _cursor_update(con, handle, last_message_id=last_mid,
                                   messages=0, replies=0, flood_until=None,
                                   captured_at=captured_at, dry_run=dry_run)
                    if not dry_run:
                        con.commit()
                    summary["top"].append({"handle": handle, "linked_group": False,
                                           "messages": 0, "replies": 0,
                                           "no_replies": False})
                    continue
            if not posts_with_replies:
                # Ни одного поста с n_replies > 0 — ответов нет по определению.
                summary["channels_no_replies"] += 1
            # Ответы читаются у САМОГО КАНАЛА (peer канала + reply_to=<id поста>):
            # по связанной группе id поста невалиден (MsgIdInvalidError).
            for m in posts_with_replies:
                if summary["stopped"]:
                    break
                mid = getattr(m, "id", None)
                if mid is None:
                    continue
                ch_messages += 1
                summary["messages"] += 1
                if pause:
                    await asyncio.sleep(pause)
                # D-64: пост с ответами, которого нет в content — досоздаём
                # строку, а не пропускаем ответы.
                if create_missing_posts and str(mid) not in post_index:
                    new_id = _ensure_post_row(con, int(ch["source_id"]), handle, m)
                    if new_id is not None:
                        post_index[str(mid)] = new_id
                        ch_created += 1
                        summary["posts_created"] += 1
                if not _budget(_msg_requests(replies_per_message)):
                    break
                try:
                    async for r in client.iter_messages(
                            entity, reply_to=mid, limit=replies_per_message):
                        if summary["stopped"]:
                            break
                        f = _reply_fields(r, handle, mid)
                        if f is None:
                            continue
                        if total_replies >= max_replies:
                            break
                        written = save_replies(con, int(ch["source_id"]), handle,
                                               mid, None, [f], post_index,
                                               captured_at)
                        total_replies += 1
                        ch_replies += 1
                        summary["replies"] += written
                except Exception as exc:  # noqa: BLE001 — один тред не валит прогон
                    if _is_flood(exc):
                        raise
                    summary["errors"] += 1
                    log.warning("tg comments: ответы %s/%s не прочитаны (%s)",
                                handle, mid, exc)
        except Exception as exc:  # noqa: BLE001 — один канал не валит прогон
            if _is_flood(exc):
                _stop_flood(handle, int(getattr(exc, "seconds", 0) or 0),
                            last_mid, ch_messages, ch_replies)
                break
            summary["errors"] += 1
            log.warning("tg comments: канал %s упал (%s)", handle, exc)
            continue
        if not summary["stopped"]:
            summary["channels_ok"] += 1
        _cursor_update(con, handle, last_message_id=last_mid,
                       messages=ch_messages, replies=ch_replies,
                       flood_until=None, captured_at=captured_at, dry_run=dry_run)
        if not dry_run:
            con.commit()
        summary["top"].append({
            "handle": handle,
            "linked_group": (bool(linked_id) if linked_checked else None),
            "messages": ch_messages, "replies": ch_replies,
            "no_replies": not posts_with_replies,
            "posts_created": ch_created,
        })
        if progress:
            progress(handle, ch_messages, ch_replies)
    summary["requests"] = requests
    summary["channels_remaining"] = max(0, len(targets) - summary["channels"])
    return summary


def make_telethon_client():
    """Собрать TelegramClient из учётки контура (сессия + ключи .env)."""
    from telethon import TelegramClient

    env = load_env()
    api_id = env.get("TG_API_ID")
    api_hash = env.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("не заданы TG_API_ID/TG_API_HASH (см. /root/.hermes/.env)")
    return TelegramClient(SESSION_PATH, int(api_id), api_hash)


def collect(con, cfg: Any = None, *, client=None, dry_run: bool = False,
            n_channels: int | None = None, max_replies: int | None = None,
            messages_per_channel: int | None = None,
            replies_per_message: int | None = None, pause: float | None = None,
            progress=None, use_linked_chat: bool = False,
            create_missing_posts: bool | None = None,
            linked_chat_lookup=None, max_requests: int | None = None,
            targets: list[dict] | None = None) -> dict:
    """Собрать тексты ответов по топ-каналам (ТЗ-49 п.3, D-64).

    ``client`` можно подставить (асинхронный Telethon-совместимый фейк) — тогда
    сеть/учётка не нужны. ``dry_run`` только отбирает каналы и возвращает план.

    Ответы читаются у САМОГО КАНАЛА (peer канала + ``reply_to=<id поста>``);
    ``use_linked_chat=True`` (путь CLI/контура) лишь включает проверку
    ``linked_chat_id`` через ``GetFullChannelRequest`` — и только для каналов,
    где найден хотя бы один пост с ответами. У канала без группы честно пишем
    «нет связанной группы» (``channels_no_group``), у каналов без постов с
    ответами — «ответов нет» (``channels_no_replies``). Заодно досоздаём строку
    ``content`` постам с ответами, которых нет в выборке.
    """
    n_channels = COMMENT_TOP_CHANNELS if n_channels is None else int(n_channels)
    if use_linked_chat:
        # ТЗ-B12/D-64: не более 200 каналов за прогон.
        n_channels = min(n_channels, COMMENT_LINKED_MAX_CHANNELS)
    if create_missing_posts is None:
        create_missing_posts = bool(use_linked_chat)
    max_replies = (COMMENT_MAX_REPLIES_PER_RUN if max_replies is None
                   else int(max_replies))
    messages_per_channel = (COMMENT_MESSAGES_PER_CHANNEL if messages_per_channel is None
                            else int(messages_per_channel))
    replies_per_message = (COMMENT_REPLIES_PER_MESSAGE if replies_per_message is None
                           else int(replies_per_message))
    if pause is None:
        pause = COMMENT_PAUSE_SEC
    if targets is None:
        targets = select_channels(con, n=n_channels)
    plan = {
        "channels": len(targets),
        "max_replies": max_replies,
        "messages_per_channel": messages_per_channel,
        "use_linked_chat": bool(use_linked_chat),
        "top": [{"handle": t.get("handle"), "engagement": t.get("engagement")}
                for t in targets],
    }
    if dry_run:
        return {"dry_run": True, **plan, "replies": 0, "errors": 0,
                "flood_wait": 0, "stopped": False}

    own_client = client is None
    summary: dict
    if own_client:
        async def _run():
            c = make_telethon_client()
            await c.connect()
            try:
                if not await c.is_user_authorized():
                    raise RuntimeError("сессия Telethon не авторизована")
                return await _collect_async(
                    con, targets, client=c, cfg=cfg, max_replies=max_replies,
                    messages_per_channel=messages_per_channel,
                    replies_per_message=replies_per_message, pause=pause,
                    dry_run=dry_run, progress=progress,
                    use_linked_chat=use_linked_chat,
                    create_missing_posts=create_missing_posts,
                    linked_chat_lookup=linked_chat_lookup,
                    max_requests=max_requests)
            finally:
                await c.disconnect()
        summary = asyncio.run(_run())
    else:
        summary = asyncio.run(_collect_async(
            con, targets, client=client, cfg=cfg, max_replies=max_replies,
            messages_per_channel=messages_per_channel,
            replies_per_message=replies_per_message, pause=pause,
            dry_run=dry_run, progress=progress,
            use_linked_chat=use_linked_chat,
            create_missing_posts=create_missing_posts,
            linked_chat_lookup=linked_chat_lookup,
            max_requests=max_requests))
    summary.setdefault("dry_run", False)
    return summary


def format_summary(summary: dict) -> str:
    """Человеческая сводка по прогону Telegram-комментариев."""
    if summary.get("dry_run"):
        return (f"Комментарии Telegram (план): каналов {summary.get('channels', 0)}, "
                f"потолок ответов {summary.get('max_replies', 0)}.")
    tail = ""
    if summary.get("flood_wait"):
        tail = (f" ОСТАНОВКА: flood-wait {summary['flood_wait']}s "
                f"(до {summary.get('flood_until')}).")
    elif summary.get("stop_reason") == "request_budget":
        tail = " ОСТАНОВКА: исчерпан бюджет MTProto-запросов."
    nogroup = f" без ответов {summary.get('channels_no_replies', 0)},"
    if summary.get("use_linked_chat"):
        nogroup = (f" без группы {summary.get('channels_no_group', 0)}," + nogroup)
    return (
        f"Комментарии Telegram: каналов {summary.get('channels', 0)} "
        f"(обработано {summary.get('channels_ok', 0)},{nogroup} "
        f"осталось {summary.get('channels_remaining', 0)}), "
        f"постов с ответами {summary.get('messages', 0)}, "
        f"записано ответов {summary.get('replies', 0)}, "
        f"запросов {summary.get('requests', 0)}, "
        f"ошибок {summary.get('errors', 0)}.{tail}"
    )
