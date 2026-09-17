#!/usr/bin/env python3
"""Создание контура и импорт проверенного реестра каналов (UNIVERSE.csv).

В монорепозитории схему БД создаёт ЯДРО (``tuber.core.schema``), а legacy-форму
таблиц отдаёт адаптер :mod:`tuber.platforms.telegram.store`. Поэтому этот модуль
больше не объявляет DDL: он (1) гарантирует схему единой базы и (2) заливает
реестр каналов из CSV в ``source`` через представление ``channels`` (тот же
путь, что у остального Telegram-кода — ТЗ-4).

Схема спроектирована под 7 слоёв из DESIGN: реестр, посты, сюжеты,
классификация, метрики, состояние квот; в ядре им соответствуют
``source`` / ``content`` / ``story`` / ``classification`` / ``metric_snapshot`` /
``transport_account_state``.

Запуск::

    python3 -m tuber tg init-db [--db PATH] [--csv PATH]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

from . import config as _config
from . import store as db

#: Прежний путь реестра (историческое напоминание для читателя кода).
LEGACY_CSV = "/root/import/tgai/UNIVERSE.csv"
DEFAULT_CSV = os.path.join(_config.DATA_DIR, "UNIVERSE.csv")


def num(v):
    try:
        return float(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def flag(title, handle):
    """Эвристика авторства по названию канала (перенесена без изменений)."""
    t = (title or "").lower()
    if any(k in t for k in ["агрегатор", "аггрегатор", "новости", "лента",
                            "дайджест", "журнал", "медиа"]):
        return 0, "aggregator?"
    if any(k in t for k in ["авторск"]):
        return 1, "author?"
    return None, "unknown"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Telegram: контур + реестр из UNIVERSE.csv")
    ap.add_argument("--db", default=None, help="путь к единой базе (по умолчанию боевая)")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="реестр каналов (CSV)")
    return ap.parse_args(argv)


def import_universe(con, path: str) -> int:
    """Импорт реестра в ``source`` (platform='telegram') через представление ``channels``."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    ins = 0
    for r in rows:
        h = (r.get("handle") or "").strip()
        if not h:
            continue
        subs, views = num(r.get("subs")), num(r.get("avg_views"))
        days = num(r.get("days"))
        status = "candidate"
        if days is not None:
            status = "active" if days <= 14 else ("dead" if days > 90 else "candidate")
        vr = round(100.0 * views / subs, 1) if subs and views else None
        af = 1 if (vr and vr > 80 and (views or 0) < 3000) else 0
        auth, note = flag(r.get("title"), h)
        con.execute(
            """INSERT INTO channels
               (handle,title,subs,subs_at,last_post_at,posts_7d,avg_views,vr,lang,status,
                read_mode,is_author,antifraud_flag,source,checked_at,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?)""",
            (h, r.get("title"), int(subs) if subs else None, "2026-09-14", r.get("last"),
             num(r.get("posts_7d")), int(views) if views else None, vr,
             r.get("lang") or "RU", status, "web", auth, af, r.get("источник"), note))
        ins += 1
    con.commit()
    return ins


def main(argv=None) -> int:
    args = parse_args(argv)
    path = _config.resolve_db(args.db)
    con = db.init_db(path)
    try:
        ins = import_universe(con, args.csv)
        q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
        print(f"БАЗА: {path}")
        print(f"каналов импортировано: {ins}")
        print("по статусам:",
              dict(con.execute("SELECT status, COUNT(*) FROM channels GROUP BY status").fetchall()))
        print("с подписчиками:", q("SELECT COUNT(*) FROM channels WHERE subs IS NOT NULL"),
              "| с VR:", q("SELECT COUNT(*) FROM channels WHERE vr IS NOT NULL"),
              "| авторских помечено:", q("SELECT COUNT(*) FROM channels WHERE is_author=1"),
              "| флагов накрутки:", q("SELECT COUNT(*) FROM channels WHERE antifraud_flag=1"))
        print("\nТОП по отклику (VR) среди живых — то, что реально стоит читать:")
        for h, t, s, v, vr, p in con.execute(
                """SELECT handle,title,subs,avg_views,vr,posts_7d FROM channels
                   WHERE vr IS NOT NULL AND antifraud_flag=0 AND status='active'
                   ORDER BY vr DESC LIMIT 14"""):
            print(f"  @{h[:22]:22s} VR={vr:5.1f}%  подп={s:>8} просмотры={v:>6} "
                  f"{str(p):>5}/нед  {(t or '')[:34]}")
        print("\nКрупнейшие по подписчикам (агрегаторный слой):")
        for h, t, s, vr in con.execute(
                "SELECT handle,title,subs,vr FROM channels WHERE subs IS NOT NULL"
                " ORDER BY subs DESC LIMIT 8"):
            print(f"  @{h[:22]:22s} {s:>9} VR={vr if vr is not None else '?'}%  {(t or '')[:36]}")
    except FileNotFoundError as exc:
        sys.stderr.write(f"ошибка: реестр не найден: {exc}\n")
        return 2
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
