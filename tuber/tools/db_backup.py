"""Суточный бэкап единой базы + проверка целостности (ТЗ-5 §1.4).

До этой волны бэкапов было ТРИ, каждый на свою legacy-базу. После сведения
данных в одно ядро ``data/tuber.db`` страховка должна быть одна и делаться
штатной командой:

    python3 -m tuber db backup [--db data/tuber.db] [--dir data/backups]
                               [--keep N] [--json]

Что делает команда:

1. снимает копию базы через штатный SQLite backup API
   (``sqlite3.Connection.backup``) — источник открывается ТОЛЬКО на чтение,
   поэтому бэкап безопасен и на WAL-базе, куда прямо сейчас пишут коллекторы;
2. проверяет целостность И источника, и полученной копии по
   ``PRAGMA integrity_check``;
3. пишет результат в журнал ``<dir>/backup.log`` (дата, имя файла, размер,
   вердикт ``integrity_check``) — «с записью результата» из ТЗ;
4. при ``--keep N`` оставляет N самых свежих копий, остальные удаляет.

Имя копии: ``tuber-YYYYmmdd-HHMMSS.db`` (UTC). Парный модуль
``scripts/cutover_prepare.sh`` использует ``--dir`` и ``--dry-run``-логику
пересборки боевой базы на этапе переключения.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sqlite3
import sys
from pathlib import Path

from tuber import config

#: Сколько копий оставлять по умолчанию, если задан --keep (None — не чистить).
DEFAULT_KEEP: int | None = None
#: Имя журнала результатов внутри каталога копий.
LOG_NAME = "backup.log"


def _utc_stamp() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def integrity_check(path: str) -> tuple[bool, str]:
    """``PRAGMA integrity_check`` по файлу (read-only). Вернуть (ok, вердикт)."""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60)
    try:
        rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    finally:
        conn.close()
    verdict = "; ".join(rows) if rows else "(пусто)"
    ok = len(rows) == 1 and rows[0].lower() == "ok"
    return ok, verdict


def backup_database(
    db_path: str,
    *,
    backup_dir: str | None = None,
    keep: int | None = DEFAULT_KEEP,
    prefix: str = "tuber",
    log: bool = True,
) -> dict:
    """Снять копию базы и проверить целостность. Вернуть сводку-словарь.

    Источник не открывается на запись. Если файла нет — ``FileNotFoundError``
    (команда не должна молча «бэкапить» пустоту).
    """
    src_path = Path(db_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"базы нет: {src_path}")
    out_dir = Path(backup_dir) if backup_dir else src_path.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{prefix}-{_utc_stamp()}.db"

    # Источник — строго read-only (mode=ro). Копия — обычный файл.
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    src_ok, src_verdict = integrity_check(str(src_path))
    copy_ok, copy_verdict = integrity_check(str(dest))
    size = dest.stat().st_size

    summary = {
        "source": str(src_path),
        "backup": str(dest),
        "size_bytes": size,
        "created_at": _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "source_integrity": src_verdict,
        "source_ok": src_ok,
        "backup_integrity": copy_verdict,
        "backup_ok": copy_ok,
        "removed": [],
    }

    if keep is not None:
        summary["removed"] = _prune(out_dir, prefix, keep)

    if log:
        _append_log(out_dir, summary)
    return summary


def _prune(out_dir: Path, prefix: str, keep: int) -> list[str]:
    """Оставить ``keep`` свежих копий, вернуть имена удалённых."""
    if keep < 1:
        return []
    copies = sorted(out_dir.glob(f"{prefix}-*.db"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for path in copies[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    return removed


def _append_log(out_dir: Path, summary: dict) -> None:
    """Записать строку результата в журнал (обязательное требование §1.4)."""
    line = (
        "{created_at} backup={backup} size={size_bytes} "
        "source_integrity={source_ok} ({source_integrity}) "
        "backup_integrity={backup_ok} ({backup_integrity})"
    ).format(**summary)
    with open(out_dir / LOG_NAME, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def format_summary(summary: dict, *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(summary, ensure_ascii=False, indent=2)
    verdict = "ok" if (summary["source_ok"] and summary["backup_ok"]) else "ПРОБЛЕМА"
    lines = [
        "tuber db backup",
        f"  источник:      {summary['source']}",
        f"  копия:         {summary['backup']}",
        f"  размер:        {summary['size_bytes']} байт",
        f"  целостность источника: {summary['source_integrity']}",
        f"  целостность копии:     {summary['backup_integrity']}",
        f"  итог:          {verdict}",
    ]
    if summary.get("removed"):
        lines.append("  удалено старых копий: " + ", ".join(summary["removed"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tuber db", description="Операции с единой базой")
    sub = parser.add_subparsers(dest="action", required=True)

    p_backup = sub.add_parser("backup", help="снять копию базы и проверить целостность")
    p_backup.add_argument("--db", dest="db_path", default=None,
                          help="путь к базе (по умолчанию data/tuber.db)")
    p_backup.add_argument("--dir", dest="backup_dir", default=None,
                          help="каталог копий (по умолчанию <каталог базы>/backups)")
    p_backup.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                          help="оставить N самых свежих копий (по умолчанию — не чистить)")
    p_backup.add_argument("--json", action="store_true", help="машинный вывод")

    p_check = sub.add_parser("integrity", help="только проверка целостности базы")
    p_check.add_argument("--db", dest="db_path", default=None)
    p_check.add_argument("--json", action="store_true")

    p_urls = sub.add_parser(
        "backfill-urls",
        help="дозаполнить content.url прямой ссылкой (идемпотентно, D-45)")
    p_urls.add_argument("--db", dest="db_path", default=None)
    p_urls.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    db_path = config.db_path(args.db_path)

    if args.action == "integrity":
        ok, verdict = integrity_check(db_path)
        payload = {"db": db_path, "ok": ok, "integrity": verdict}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"tuber db integrity: {verdict} ({'ok' if ok else 'ПРОБЛЕМА'})")
        return 0 if ok else 2

    if args.action == "backfill-urls":
        from tuber.core import db as core_db
        from tuber.core import schema as core_schema
        conn = core_db.connect(db_path)
        try:
            with core_db.write_tx(conn):
                counts = core_schema.ensure_content_url(conn)
        finally:
            conn.close()
        if args.json:
            print(json.dumps(counts, ensure_ascii=False))
        else:
            filled = counts.get("filled", 0) if counts else 0
            remaining = counts.get("remaining", 0) if counts else 0
            if not counts:
                print("tuber db backfill-urls: уже дозаполнено (url_version актуален)")
            else:
                print("tuber db backfill-urls: заполнено %s, осталось NULL %s"
                      % (filled, remaining))
        return 0

    try:
        summary = backup_database(
            db_path, backup_dir=args.backup_dir, keep=args.keep)
    except FileNotFoundError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    print(format_summary(summary, as_json=args.json))
    return 0 if (summary["source_ok"] and summary["backup_ok"]) else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
