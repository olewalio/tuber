#!/usr/bin/env python3
"""Латка ``story.first_mover_source_id``: восстановить зачин, потерянный из-за регистра.

Контекст (ТЗ 2026-10-03). X-сюжеты пишутся вставкой в TEMP-представление
``stories``; связь зачина с реестром делал триггер ``stories_ins``/``stories_upd``
ТОЧНЫМ сравнением ``src.handle = NEW.first_mover``. ``source.handle`` хранится в
нижнем регистре, а ``story_member.handle``/``first_mover`` сохраняют исходный,
поэтому 196 зачинов из 294 остались с ``first_mover_source_id IS NULL``, хотя
автор в составе есть. Триггеры уже починены (регистронезависимо, терпимо к ``@``),
но уже записанные строки сами не исправятся: обычная пересборка X-сюжетов
(window 72 ч) восстанавливает только 196, а 98 авторов вообще не заведены в
``source`` и остаются NULL в любом случае (их handle отдаёт запасной путь
представления ``stories`` из ``story_member``).

Этот инструмент ДОПИСЫВАЕТ ``first_mover_source_id`` только у восстановимых
строк, у которых сейчас NULL. Ничего не удаляет и не трогает сюжеты с уже
заполненным зачином.

Правила:

* по умолчанию ``--dry-run`` — печатает, сколько строк и как исправит;
* запись выполняется ТОЛЬКО с ``--apply``;
* перед записью снимается копия базы (``sqlite3 .backup``) в ``<db>.bak-YYYYMMDDhhmmss``;
* запись идёт одной транзакцией ``BEGIN IMMEDIATE`` с ``busy_timeout``;
* затрагиваются исключительно строки ``platform='x' AND first_mover_source_id IS NULL``;
* идемпотентность: повторный ``--apply`` не находит кандидатов и ничего не пишет;
* проверяется, что «шлюз» (любой процесс) не держит удалённый inode базы:
  иначе запись ушла бы в файл, который никто больше не видит по пути.

Запуск::

    python3 tools/fix_first_mover_case.py --dry-run
    python3 tools/fix_first_mover_case.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_DB = "/root/tuber/data/tuber.db"


def _db_path(args) -> str:
    if getattr(args, "db", None):
        return args.db
    env = os.environ.get("TUBER_DB")
    if env:
        return env
    try:
        from tuber.platforms.x import config  # noqa: E402
        return config.db_path()
    except Exception:  # noqa: BLE001
        return DEFAULT_DB


def _source_index(con) -> dict[str, list[sqlite3.Row]]:
    """``lower(ltrim(handle,'@')) -> [строки source]`` для platform='x'."""
    idx: dict[str, list[sqlite3.Row]] = {}
    for r in con.execute(
            "SELECT id, handle FROM source WHERE platform='x'"):
        key = (r["handle"] or "").strip().lstrip("@").lower()
        if key:
            idx.setdefault(key, []).append(r)
    return idx


def _pick(rows: list[sqlite3.Row], raw: str) -> sqlite3.Row:
    """Приоритет: точное совпадение с исходным handle, затем меньший id.

    Ровно тот же порядок, что в починенных триггерах.
    """
    return min(rows, key=lambda r: (0 if r["handle"] == raw else 1, r["id"]))


def plan(con) -> list[tuple[int, str, int]]:
    """(story_id, raw_handle, source_id) для восстановимых NULL-зачинов."""
    idx = _source_index(con)
    rows = con.execute(
        """
        SELECT s.id AS story_id,
               (SELECT sm.handle FROM story_member sm
                 WHERE sm.story_id = s.id AND sm.role = 'primary'
                   AND sm.handle IS NOT NULL AND sm.handle != ''
                 ORDER BY sm.content_id ASC LIMIT 1) AS raw
        FROM story s
        WHERE s.platform = 'x' AND s.first_mover_source_id IS NULL
        ORDER BY s.id ASC
        """).fetchall()
    pending: list[tuple[int, str, int]] = []
    for r in rows:
        raw = (r["raw"] or "").strip()
        if not raw:
            continue
        cands = idx.get(raw.lstrip("@").lower())
        if not cands:
            continue
        pending.append((r["story_id"], raw, _pick(cands, raw)["id"]))
    return pending


def stale_deleted_inode(db_path: str) -> str | None:
    """Есть ли открытый fd на УДАЛЁННУЮ (по пути) базу у любого процесса.

    Если база была заменена копией, а «шлюз» держит fd на старый inode, запись
    уйдёт в файл, недостижимый по ``db_path``. Проверяем /proc: никакой fd не
    должен ссылаться на *deleted* цель с тем же именем файла.
    """
    name = os.path.basename(os.path.abspath(db_path))
    want_ino = os.stat(db_path).st_ino
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except (OSError, PermissionError):
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            base = os.path.basename(target.replace(" (deleted)", ""))
            if base != name:
                continue
            if target.endswith(" (deleted)"):
                return f"процесс {pid_dir.name} держит удалённый inode {target}"
            try:
                ino = os.stat(fd).st_ino
            except OSError:
                continue
            if ino != want_ino:
                return (f"процесс {pid_dir.name} держит другой inode базы "
                        f"({ino} != {want_ino})")
    return None


def make_backup(db_path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    dst = f"{db_path}.bak-{stamp}"
    subprocess.run(["sqlite3", f"file:{db_path}?mode=ro", ".backup '%s'" % dst],
                   check=True)
    return dst


def apply_pending(db_path: str, pending: list[tuple[int, str, int]]) -> int:
    con = sqlite3.connect(db_path, timeout=30.0)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("BEGIN IMMEDIATE")
        done = 0
        for story_id, _raw, source_id in pending:
            cur = con.execute(
                "UPDATE story SET first_mover_source_id = ?"
                " WHERE platform='x' AND id = ? AND first_mover_source_id IS NULL",
                (source_id, story_id))
            done += cur.rowcount
        con.commit()
        return done
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fix_first_mover_case",
        description="Восстановить first_mover_source_id у X-сюжетов (регистр/@)")
    p.add_argument("--db", help="путь к базе (по умолчанию боевая)")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="только показать план (по умолчанию)")
    p.add_argument("--apply", dest="apply", action="store_true",
                   help="выполнить запись (со снятием .backup)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = _db_path(args)
    if not os.path.exists(db_path):
        print(f"нет базы: {db_path}", file=sys.stderr)
        return 1

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        pending = plan(con)
        nulls = con.execute(
            "SELECT COUNT(*) FROM story WHERE platform='x'"
            " AND first_mover_source_id IS NULL").fetchone()[0]
    finally:
        con.close()

    print(f"база: {db_path}")
    print(f"NULL first_mover_source_id у X: {nulls}")
    print(f"восстановимо через story_member+source: {len(pending)}")
    print(f"останется NULL (автор не заведён в source): {nulls - len(pending)}")
    for story_id, raw, source_id in pending[:10]:
        print(f"    story {story_id}: {raw!r} -> source.id {source_id}")
    if len(pending) > 10:
        print(f"    … ещё {len(pending) - 10}")

    if not args.apply:
        print("\ndry-run: запись не выполнялась (для записи добавьте --apply)")
        return 0

    if not pending:
        print("\nнечего исправлять (идемпотентно)")
        return 0

    stale = stale_deleted_inode(db_path)
    if stale:
        print(f"ОТКАЗ: {stale}", file=sys.stderr)
        print("остановите держателя и повторите --apply", file=sys.stderr)
        return 1

    backup = make_backup(db_path)
    print(f"backup: {backup}")
    done = apply_pending(db_path, pending)
    print(f"обновлено строк: {done}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
