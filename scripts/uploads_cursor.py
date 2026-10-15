"""Управление боевым курсором ротации обхода uploads (ТЗ-6).

Курсор — позиция в реестре подтверждённых ИИ-каналов (``ORDER BY channel_id``),
с которой фаза 1 ``collect.run_collect`` начинает порцию обхода uploads.
Состояние живёт в ``data/uploads_rotation.json`` (поля ``cursor``/
``updated_at``/``full_len``).

Примеры:
    python3 scripts/uploads_cursor.py --show
    python3 scripts/uploads_cursor.py --set 800
    python3 scripts/uploads_cursor.py --advance 400

База открывается ТОЛЬКО на чтение (нужен лишь размер реестра); пишется только
файл курсора. Курсор нормализуется по модулю фактического реестра, а
``full_len`` и ``updated_at`` пересчитываются по реальности — это лечит уже
сбитый тестами ``{"cursor": 0, "full_len": 1}``.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tuber.platforms.youtube import collect, config
from tuber.platforms.youtube import store as db


def _open_readonly(db_path: Any) -> sqlite3.Connection:
    """Соединение с базой только на чтение (реестр не изменяем).

    ``store.open_readonly`` ставит слой совместимости (TEMP-представления
    ``video_classification``/``videos``) на URI ``mode=ro``: запись в main-базу
    физически невозможна, а реестр виден через те же имена, что и в сборе.
    """
    return db.open_readonly(db_path)


def _registry_size(conn: sqlite3.Connection) -> int:
    """Размер фактического реестра подтверждённых ИИ-каналов."""
    min_ai = int(config.MIN_AI_VIDEOS_PER_CHANNEL)
    return len(collect._confirmed_ai_channels(conn, min_ai))


def _fmt_ts(raw: Any) -> str:
    """Unixtime в человекочитаемый UTC; нечисло/None не ломают вывод."""
    if raw is None:
        return "—"
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _cursor_path() -> Path:
    return collect._uploads_rotation_path(config)


def _raw_cursor(state: dict[str, Any]) -> int:
    raw = state.get("cursor")
    if isinstance(raw, bool):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _window(cursor: int, per_run: int, total: int) -> tuple[int, int, int]:
    """Окно обхода текущего прогона: (начало, конец, размер окна).

    ``rotation_slice`` отдаёт весь реестр, если порция не меньше реестра:
    тогда окно покрывает весь круг.
    """
    if total <= 0:
        return 0, 0, 0
    start = cursor % total
    size = min(per_run, total) if per_run > 0 else 0
    return start, start + size, size


def _print_show(conn: sqlite3.Connection) -> None:
    path = _cursor_path()
    state = collect._read_rotation_state(path)
    total = _registry_size(conn)
    per_run = config.collect_uploads_channels_per_run(config)
    raw = _raw_cursor(state)
    effective = raw % total if total else 0
    start, end, size = _window(raw, per_run, total)
    full_len = state.get("full_len")
    covered = "весь реестр" if size >= total else f"{size} каналов"

    print(f"Файл курсора:   {path}")
    print(f"  существует:   {'да' if path.exists() else 'нет'}")
    print(f"Реестр:         {total} каналов "
          f"(MIN_AI_VIDEOS_PER_CHANNEL={config.MIN_AI_VIDEOS_PER_CHANNEL})")
    print(f"Курсор в файле: {raw}")
    print(f"  в круге:      {effective} (нормализован по модулю реестра)")
    print(f"full_len в файле: {full_len} "
          f"({'совпадает с реестром' if full_len == total else 'не совпадает с реестром'})")
    print(f"Порция прогона: {per_run} (COLLECT_UPLOADS_CHANNELS_PER_RUN)")
    if total > 0:
        print(f"Окно сейчас:    [{start}, {end}) из {total} — {covered}")
    else:
        print("Окно сейчас:    реестр пуст, обход uploads не выполняется")
    print(f"updated_at:     {_fmt_ts(state.get('updated_at'))} "
          f"({state.get('updated_at')})")


def _write(conn: sqlite3.Connection, value: int, note: str) -> int:
    total = _registry_size(conn)
    path = _cursor_path()
    before_state = collect._read_rotation_state(path)
    before = _raw_cursor(before_state)
    cursor = value % total if total else 0
    collect.save_cursor(path, cursor, total)
    print(f"{note}: курсор {before} -> {cursor}; файл {path}; "
          f"full_len={total}; updated_at={_fmt_ts(collect._read_rotation_state(path).get('updated_at'))}")
    return cursor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Курсор ротации обхода uploads (ТЗ-6).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", action="store_true", help="показать текущее состояние")
    group.add_argument("--set", type=int, metavar="N", help="задать абсолютный курсор")
    group.add_argument("--advance", type=int, metavar="N", help="сдвинуть курсор на N")
    parser.add_argument(
        "--db", default=None,
        help="путь к базе (по умолчанию config.DB_PATH / $TUBER_DB)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else Path(config.DB_PATH)
    if not db_path.exists():
        print(f"база не найдена: {db_path}", file=sys.stderr)
        return 2

    conn = _open_readonly(db_path)
    try:
        if args.show:
            _print_show(conn)
        elif args.set is not None:
            _write(conn, args.set, "--set")
        else:
            current = _raw_cursor(collect._read_rotation_state(_cursor_path()))
            _write(conn, current + args.advance, f"--advance {args.advance}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
