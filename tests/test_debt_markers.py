"""Двусторонняя проверка связки «маркер техдолга ↔ строка реестра» (Tuber).

Каждый ID из сводной таблицы ``TECH-DEBT.md`` обязан иметь хотя бы один маркер
``TODO(debt-D-XX)`` в коде монорепозитория, и каждый маркер обязан ссылаться на
существующий ID. Расхождение — явный ``AssertionError`` со списком.

Таблица реестра — единственный источник ID, который читает и внешний сканер
``/root/.hermes/scripts/debt_scan.py``; поэтому проверка двусторонняя и не
зависит от заголовков разделов ``### D-XX.``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "TECH-DEBT.md"
SCAN_ROOTS = [REPO_ROOT]
SCAN_EXTS = (".py", ".sh", ".yaml", ".yml")

MARKER_RE = re.compile(r"TODO\(debt-(D-\d+)\)")
REGISTRY_ROW_RE = re.compile(r"^\|\s*(D-\d+)\s*\|")

EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "venv",
    ".venv",
    "dist",
    "build",
}


def _registry_ids() -> set[str]:
    assert REGISTRY.exists(), f"нет реестра техдолга: {REGISTRY}"
    ids: set[str] = set()
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        m = REGISTRY_ROW_RE.match(line)
        if m:
            ids.add(m.group(1))
    return ids


def _code_markers() -> dict[str, list[str]]:
    """ID → список 'путь:строка' по файлам SCAN_EXTS из SCAN_ROOTS."""
    found: dict[str, list[str]] = {}
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for name in filenames:
                if not name.endswith(SCAN_EXTS):
                    continue
                fp = Path(dirpath) / name
                try:
                    if fp.stat().st_size > 1024 * 1024:
                        continue
                    text = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    for m in MARKER_RE.finditer(line):
                        found.setdefault(m.group(1), []).append(f"{fp}:{lineno}")
    return found


def test_ids_in_registry_have_markers() -> None:
    """Каждая строка сводной таблицы реестра подкреплена маркером в коде."""
    registry_ids = _registry_ids()
    markers = _code_markers()
    missing = sorted(i for i in registry_ids if i not in markers)
    assert not missing, "ID из реестра без маркера в коде: " + ", ".join(missing)


def test_markers_reference_registry_ids() -> None:
    """Каждый маркер TODO(debt-D-XX) ссылается на строку сводной таблицы."""
    registry_ids = _registry_ids()
    markers = _code_markers()
    stray: list[str] = []
    for mid, places in sorted(markers.items()):
        if mid not in registry_ids:
            stray.extend(places)
    assert not stray, "маркер ссылается на отсутствующий в реестре ID: " + ", ".join(stray)
