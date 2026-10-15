#!/usr/bin/env python3
"""Подписчики Telegram с публичной превью-страницы ``https://t.me/<handle>`` (ТЗ-45).

Зачем
-----
``subs`` у Telegram заполнялся ОДНОРАЗОВО (заливка ``init_db.py``, ``subs_at``
заморожен на 2026-09-14), живого ряда роста не существовало: единственная ветка,
умеющая писать подписчиков, — MTProto-резолв (``telegram/collect.py``,
``participants_count``) — в бою не запускалась. При этом превью-страница
``https://t.me/<handle>`` (БЕЗ ``/s/``) отдаёт ТОЧНОЕ число:

    <div class="tgme_page_extra">10 683 993 subscribers</div>

а у русской локали — ``12 727 подписчиков`` (разряды разделены пробелом либо
неразрывным ``\\u00a0``). Лента ``/s/`` в ``tgme_channel_info_counter`` отдаёт
ОКРУГЛЁННОЕ значение (``10.7M``) — для ряда роста оно не годится, поэтому оно
НЕ пишется: при неудаче с точным числом исход ``no_counter`` и старое значение
остаётся как было.

Три исхода разбора (:func:`parse_preview`):

* ``value``       — точное число взято;
* ``no_counter``  — страница есть, ``og:title`` есть, числа нет (это Пользователь);
* ``gone``        — нет ``og:title`` (канал переименован/удалён).

Статус канала команда НЕ трогает вообще (``private``/``dead``/``candidate``
не повышаются и не понижаются) — это отдельная миля ТЗ-46/``tg promote``.

CLI:
    python3 -m tuber tg followers [--limit N] [--db PATH] [--allow-production]
                                  [--dry-run]
"""
from __future__ import annotations

import argparse
import html as html_lib
import os
import re
import sys
import time
from typing import NamedTuple

from . import collect as _collect
from . import config
from . import scoring
from . import store as db

#: Три исхода разбора превью-страницы.
OUTCOME_VALUE = "value"
OUTCOME_NO_COUNTER = "no_counter"
OUTCOME_GONE = "gone"

#: Пауза между запросами, с (переопределяется TUBER_TG_FOLLOWERS_PAUSE).
DEFAULT_PAUSE = 1.2
#: Backoff на 403/429, с (как в ``collect.py`` ``_get_with_backoff``).
DEFAULT_BACKOFF = 30.0
#: Потолок времени прогона, мин (переопределяется ..._TIME_CAP_MIN/_SEC).
DEFAULT_TIME_CAP_MIN = 60.0

# ``<meta property="og:title" content="…">`` в любом порядке атрибутов.
_OG_TITLE_RES = (
    re.compile(r'<meta[^>]*?\bproperty=["\']og:title["\'][^>]*?\bcontent=["\'](.*?)["\']',
               re.S | re.I),
    re.compile(r'<meta[^>]*?\bcontent=["\'](.*?)["\'][^>]*?\bproperty=["\']og:title["\']',
               re.S | re.I),
)
# ``<div class="tgme_page_extra">…число…</div>`` (класс может стоять в любом месте).
_EXTRA_RE = re.compile(
    r'<div[^>]*\bclass=["\'][^"\']*\btgme_page_extra\b[^"\']*["\'][^>]*>(.*?)</div>',
    re.S | re.I)
# Точное число + «subscribers»/«подписчиков» (любая форма слова «подписчик»).
_NUM_RE = re.compile(r"([0-9][0-9\s\u00a0\u202f.,]*?)\s*(?:subscriber|подписчик)", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


class FollowersParse(NamedTuple):
    """Результат разбора превью-страницы: исход и (при ``value``) число."""

    outcome: str
    value: int | None


def _og_title(html: str) -> str | None:
    """Содержимое ``og:title`` (с раскрытием HTML-сущностей), иначе None."""
    for rx in _OG_TITLE_RES:
        m = rx.search(html)
        if m:
            title = html_lib.unescape(m.group(1)).strip()
            if title:
                return title
    return None


def _to_int(raw: str) -> int | None:
    """Точное число из «10 683 993» / «12\\u00a0727» — только цифры."""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def parse_preview(html: str | None) -> FollowersParse:
    """Строгий разбор превью-страницы ``t.me/<handle>`` (en + ru + ``\\u00a0``).

    Округлённое значение из ``tgme_channel_info_counter`` здесь НЕ разбирается
    намеренно: 10.7M → 10.7M → 10.7M убило бы ряд роста.
    """
    if not html:
        return FollowersParse(OUTCOME_GONE, None)
    m = _EXTRA_RE.search(html)
    if m:
        text = html_lib.unescape(_TAG_RE.sub(" ", m.group(1)))
        nm = _NUM_RE.search(text)
        if nm:
            value = _to_int(nm.group(1))
            if value is not None:
                return FollowersParse(OUTCOME_VALUE, value)
    if _og_title(html) is None:
        return FollowersParse(OUTCOME_GONE, None)
    return FollowersParse(OUTCOME_NO_COUNTER, None)


# ---------------------------------------------------------------------------
# Предохранители (env)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def default_pause() -> float:
    return _env_float("TUBER_TG_FOLLOWERS_PAUSE", DEFAULT_PAUSE)


def default_backoff() -> float:
    return _env_float("TUBER_TG_FOLLOWERS_BACKOFF", DEFAULT_BACKOFF)


def default_time_cap() -> float:
    """Потолок времени прогона, с: ..._SEC перебивает ..._MIN (по умолчанию 60 мин)."""
    sec = os.environ.get("TUBER_TG_FOLLOWERS_TIME_CAP_SEC")
    if sec not in (None, ""):
        try:
            return float(sec)
        except ValueError:
            pass
    mins = _env_float("TUBER_TG_FOLLOWERS_TIME_CAP_MIN", DEFAULT_TIME_CAP_MIN)
    return mins * 60.0


# ---------------------------------------------------------------------------
# Отбор каналов
# ---------------------------------------------------------------------------
def select_targets(con, limit=None):
    """Каналы реестра Telegram с хендлом: сначала НИКОГДА не снятые, потом старые.

    Порядок ровно как в ТЗ-45: ``subs_at IS NULL DESC, subs_at ASC, handle ASC``.
    Статус НЕ используется: обходятся все каналы с хендлом.
    """
    sql = ("SELECT id, handle, subs, subs_at FROM source "
           "WHERE platform='telegram' AND handle IS NOT NULL AND handle != '' "
           "ORDER BY (subs_at IS NULL) DESC, subs_at ASC, handle ASC")
    if limit is not None:
        sql += " LIMIT ?"
        return list(con.execute(sql, (int(limit),)))
    return list(con.execute(sql))


def count_targets(con) -> int:
    row = con.execute(
        "SELECT COUNT(*) FROM source "
        "WHERE platform='telegram' AND handle IS NOT NULL AND handle != ''").fetchone()
    return int(row[0])


def preview_url(handle: str) -> str:
    return f"https://t.me/{handle}"


# ---------------------------------------------------------------------------
# Обход
# ---------------------------------------------------------------------------

def collect_followers(con, *, client=None, limit=None, pause=None, backoff=None,
                      time_cap=None, dry_run=False, run_id=None,
                      sleep=time.sleep, clock=time.monotonic, now_iso=None) -> dict:
    """Обойти реестр и снять подписчиков с превью-страниц. Возвращает сводку.

    Предохранители: пауза между запросами, backoff 30 с на 403/429 (повтор на
    том же канале — честная остановка прогона без записи фальшивых нулей),
    потолок времени прогона. Каждый запрос — строка ``run_log`` уровня info.

    ``client`` — объект с ``.get(url)`` (httpx-совместимый); ``sleep``/``clock``
    инъектируются тестами.
    """
    pause = default_pause() if pause is None else float(pause)
    backoff = default_backoff() if backoff is None else float(backoff)
    time_cap = default_time_cap() if time_cap is None else float(time_cap)
    now_iso = now_iso or db.now_iso

    total = count_targets(con)
    rows = select_targets(con, limit=limit)

    owns_client = client is None
    if owns_client:
        client = _collect.make_client()

    summary = {
        "updated": 0,       # обновлено (value записан)
        "no_counter": 0,    # недоступно (страница без числа)
        "gone": 0,          # нет канала
        "failed": 0,        # отказов (сеть/5xx/непойманный 403)
        "skipped": 0,       # пропущено (потолок/лимит)
        "total": total,
        "attempted": 0,
        "elapsed": 0.0,
        "refused": False,   # честная остановка на повторе 403/429
        "dry_run": bool(dry_run),
    }
    start = clock()
    first = True
    try:
        for row in rows:
            if clock() - start >= time_cap:
                break
            handle = row["handle"]
            if not first:
                sleep(pause)
            first = False
            if clock() - start >= time_cap:
                break

            parsed, status = _fetch_preview(client, handle, backoff=backoff,
                                            sleep=sleep, run_id=run_id, con=con)
            summary["attempted"] += 1
            if status == "refused":
                # Повтор 403/429 на том же канале: честная остановка прогона.
                summary["failed"] += 1
                summary["refused"] = True
                break
            if status == "error":
                summary["failed"] += 1
                continue
            if parsed.outcome == OUTCOME_VALUE:
                if not dry_run:
                    db.set_follower_snapshot(con, row["id"], parsed.value,
                                             avg_views=None, taken_at=now_iso())
                summary["updated"] += 1
            elif parsed.outcome == OUTCOME_GONE:
                summary["gone"] += 1
            else:
                summary["no_counter"] += 1
    finally:
        if owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    summary["elapsed"] = max(0.0, clock() - start)
    summary["skipped"] = max(0, total - summary["attempted"])
    return summary


def _fetch_preview(client, handle, *, backoff, sleep, run_id, con):
    """Запросить превью-страницу. Возвращает (FollowersParse | None, статус).

    ``статус``: ``"ok"`` — страница разобрана; ``"error"`` — сеть/5xx/пустой
    ответ; ``"refused"`` — повтор 403/429 (прогон надо останавливать).
    """
    url = preview_url(handle)
    attempts = 0
    while True:
        attempts += 1
        try:
            # Прямой запрос, а не ``collect.http_get``: тот же контракт
            # (status, text), но нет зависимости от глобальной подмены символа
            # ``collect.http_get`` в тестах сбора постов.
            resp = client.get(url)
            status, text = resp.status_code, resp.text
        except Exception as exc:  # noqa: BLE001
            _log(con, run_id, "error", handle, f"request error: {type(exc).__name__}: {exc}")
            return None, "error"
        if status in (403, 429):
            _log(con, run_id, "warn", handle,
                 f"HTTP {status} on {url}, backoff {backoff:.0f}s (attempt {attempts}/2)")
            if attempts >= 2:
                return None, "refused"
            sleep(backoff)
            continue
        if status is None or status >= 400:
            _log(con, run_id, "warn", handle, f"HTTP {status} on {url}")
            return None, "error"
        parsed = parse_preview(text)
        detail = (f"subs={parsed.value}" if parsed.outcome == OUTCOME_VALUE
                  else parsed.outcome)
        _log(con, run_id, "info", handle, f"t.me/{handle} {detail}")
        return parsed, "ok"


def _log(con, run_id, level, handle, msg):
    """Строка ``run_log`` (история дефекта не должна теряться, как у X).

    Обязательно COMMIT: ``INSERT`` в legacy-режиме pysqlite открывает неявную
    транзакцию, и без коммита последующий ``finish_run``/``close`` теряет и
    строку журнала, и итог прогона (проверено на боевой копии). Так же делает
    ``collect.py`` (``self.con.commit()`` после строки журнала).
    """
    if run_id is None:
        return
    try:
        db.log_run(con, level, msg, handle=handle, run_id=run_id)
        con.commit()
    except Exception:  # noqa: BLE001
        pass


def summary_line(summary: dict) -> str:
    """ОДНА строка сводки ровно в формате ТЗ-45."""
    return (f"обновлено {summary['updated']}, недоступно {summary['no_counter']}, "
            f"нет канала {summary['gone']}, пропущено {summary['skipped']}, "
            f"отказов {summary['failed']}, время {int(round(summary['elapsed']))}с")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Подписчики Telegram с публичной превью-страницы (ТЗ-45)")
    ap.add_argument("--db", default=None, help="путь к базе (по умолчанию — из config)")
    ap.add_argument("--limit", type=int, default=None,
                    help="сколько каналов обойти за прогон (по умолчанию без потолка)")
    ap.add_argument("--allow-production", action="store_true",
                    help="разрешить запись в боевую базу (приёмка — на копии)")
    ap.add_argument("--dry-run", action="store_true",
                    help="без записи в БД (сеть всё равно опрашивается)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    path = config.resolve_db(args.db)
    if not args.dry_run:
        scoring.assert_can_write(path, args.allow_production)
    con = db.connect(path)
    try:
        run_id = None
        if not args.dry_run:
            run_id = db.start_run(con, "followers")
        summary = collect_followers(con, limit=args.limit, dry_run=args.dry_run,
                                    run_id=run_id)
        if not args.dry_run:
            db.finish_run(con, run_id, channels_ok=summary["updated"],
                          channels_fail=summary["failed"],
                          errors=summary["failed"])
        print(summary_line(summary))
        return 0
    finally:
        con.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
