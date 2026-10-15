#!/usr/bin/env python3
"""Автопостановка канала владельца в реестр сбора (D-69, остаток D-65).

Команда ``tuber tg add-owner <handle>`` заводит собственный канал владельца в
реестр ``source`` КАК ОБЫЧНЫЙ ИСТОЧНИК: тот же ``platform='telegram'``, тот же
``status='active'``, тот же ``read_mode='web'``, что и у прочих активных каналов.
Дальше его собирает штатный коллектор, а подписчиков/просмотры пишут штатные
пути метрик (``source_metric_history`` / ``content_latest``) — отдельного
контура с выдуманными числами не заводится.

Отличие от обычного источника — только пометка принадлежности:
``source_kind='owner'`` и ``meta_json.owner=true``. По ней
:func:`tuber.analysis.digest.percentile_block` находит субъект «где я» БЕЗ
переменных окружения (после того как канал заведён), оставаясь совместимым с
прежним переопределением через ``TUBER_SLIVKI_ME``/``--me``.

Хендл берётся из аргумента либо из ``TUBER_OWNER_CHANNEL``. Если нет ни того,
ни другого — команда честно сообщает об этом и ничего не придумывает.

Идемпотентность: повторный ``add-owner`` не создаёт дубль (``UNIQUE(platform,
handle)`` в ``source``), а лишь обновляет пометку владельца.

Запуск::

    python3 -m tuber tg add-owner @my_channel [--db PATH] [--allow-production]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import config
from . import store as db

#: Источник принадлежности канала владельцу (в ``source.source_kind``).
OWNER_SOURCE_KIND = "owner"
#: Источники, помечаемые в digest как «мой» канал (совместимость имён).
OWNER_KINDS = (OWNER_SOURCE_KIND, "me", "self")


def normalize_handle(raw: str) -> str:
    """Снять ``@`` и ``t.me/`` с хендла; пустой вход → пустая строка."""
    h = (raw or "").strip()
    for prefix in ("https://", "http://"):
        if h.lower().startswith(prefix):
            h = h[len(prefix):]
    for prefix in ("t.me/", "telegram.me/"):
        if h.lower().startswith(prefix):
            h = h[len(prefix):]
    h = h.split("/", 1)[0].split("?", 1)[0]
    return h.lstrip("@").strip()


def resolve_handle(cli_handle: str | None) -> str:
    """Хендл из аргумента либо ``TUBER_OWNER_CHANNEL``; иначе — понятная ошибка."""
    raw = cli_handle or os.environ.get("TUBER_OWNER_CHANNEL")
    handle = normalize_handle(raw or "")
    if not handle:
        raise SystemExit(
            "отказ: не задан канал владельца. Укажи хендл аргументом "
            "(tuber tg add-owner @my_channel) или переменной окружения "
            "TUBER_OWNER_CHANNEL — выдумывать канал я не буду"
        )
    return handle


def add_owner(con, handle: str, *, dry: bool = False, now: str | None = None) -> dict:
    """Завести/обновить канал владельца в реестре ``source`` (идемпотентно).

    Возвращает ``{"handle", "action", "source_id", "created"}``; ``action`` —
    ``"inserted"`` либо ``"updated"``. Повторный вызов оставляет ровно одну
    строку ``source``.
    """
    from .store import now_iso

    handle = normalize_handle(handle)
    stamp = now or now_iso()
    url = f"https://t.me/{handle}"
    meta = {"owner": True}
    meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True)
    row = con.execute(
        "SELECT id FROM source WHERE platform='telegram' AND lower(handle)=lower(?)",
        (handle,)).fetchone()
    if row is not None:
        if not dry:
            con.execute(
                "UPDATE source SET status='active', "
                "read_mode=COALESCE(read_mode,'web'), source_kind=?, "
                "added_by=COALESCE(added_by,'owner'), url=COALESCE(url,?), "
                "meta_json=?, last_synced_at=COALESCE(last_synced_at,?) "
                "WHERE id=?",
                (OWNER_SOURCE_KIND, url, meta_json, stamp, row["id"]),
            )
            con.commit()
        return {"handle": handle, "action": "updated", "source_id": row["id"],
                "created": False}
    if dry:
        return {"handle": handle, "action": "inserted", "source_id": None,
                "created": False}
    cur = con.execute(
        "INSERT INTO source(platform, handle, title, url, status, read_mode, "
        "source_kind, added_by, added_at, first_seen_at, last_synced_at, meta_json) "
        "VALUES('telegram', ?, NULL, ?, 'active', 'web', ?, 'owner', ?, ?, ?, ?)",
        (handle, url, OWNER_SOURCE_KIND, stamp, stamp, stamp, meta_json),
    )
    con.commit()
    return {"handle": handle, "action": "inserted", "source_id": cur.lastrowid,
            "created": True}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Завести канал владельца в реестр сбора Telegram (D-69)")
    ap.add_argument("handle", nargs="?", default=None,
                    help="хендл канала владельца (по умолчанию TUBER_OWNER_CHANNEL)")
    ap.add_argument("--db", default=None, help="путь к базе (по умолчанию — из config)")
    ap.add_argument("--dry", action="store_true", help="показать план без записи")
    ap.add_argument("--allow-production", action="store_true",
                    help="разрешить запись в боевую базу (по умолчанию — только копия, D-43)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        handle = resolve_handle(args.handle)
    except SystemExit as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    path = config.resolve_db(args.db)
    # D-43: изменяющая подкоманда -> единый гейт боевой базы.
    if not args.dry:
        gate = db.production_gate_error(path, args.allow_production)
        if gate:
            sys.stderr.write(gate + "\n")
            return 2
    con = db.connect(path)
    try:
        result = add_owner(con, handle, dry=args.dry)
    finally:
        con.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
