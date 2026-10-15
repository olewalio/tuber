"""Инвариант нумерации технического долга (Tuber) на уровне корня тестов.

`tests/youtube/test_debt_registry.py` закрывает уникальность номеров; здесь тот
же контракт продублирован на корне (файл читается в контексте общей приёмки ТЗ),
плюс проверяется, что долги ТЗ-19 (D-37, D-52) действительно помечены
``**закрыт**`` и их маркеры ``TODO(debt-...)`` убраны из кода.

Проверки:
1. Каждый номер долга встречается в ``TECH-DEBT.md`` ровно один раз как
   заголовок ``### D-XX.``.
2. Каждый маркер ``TODO(debt-D-XX)`` в ``tuber/`` ссылается на существующий
   раздел (маркер без раздела — ошибка).
3. D-37 и D-52 закрыты (строка реестра с ``~~D-XX~~`` и статусом ``**закрыт**``).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "TECH-DEBT.md"
CODE_DIR = ROOT / "tuber"

ID_PATTERN = r"[A-Za-z][A-Za-z0-9]*-?\d+"
SECTION_RE = re.compile(r"^###\s+(" + ID_PATTERN + r")\.\s")
MARKER_RE = re.compile(r"TODO\(debt-(" + ID_PATTERN + r")\)")


def _registry_text() -> str:
    assert REGISTRY.exists(), f"нет реестра техдолга: {REGISTRY}"
    return REGISTRY.read_text(encoding="utf-8")


def _registry_sections() -> list[str]:
    return [m.group(1) for line in _registry_text().splitlines()
            for m in [SECTION_RE.match(line)] if m]


def _code_markers() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted(CODE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in MARKER_RE.finditer(line):
                out.append((m.group(1), f"{path.relative_to(ROOT)}:{lineno}"))
    return out


def test_debt_numbers_are_unique_in_registry():
    counts = Counter(_registry_sections())
    duplicates = {num: n for num, n in counts.items() if n > 1}
    assert not duplicates, (
        "Номера долгов переиспользованы в TECH-DEBT.md: "
        + ", ".join(f"{num} — {n} раз(а)" for num, n in sorted(duplicates.items())))


def test_code_markers_have_registry_sections():
    sections = set(_registry_sections())
    orphans = [(num, where) for num, where in _code_markers() if num not in sections]
    assert not orphans, (
        "Маркеры TODO(debt-D-XX) без раздела в TECH-DEBT.md: "
        + ", ".join(f"{num} ({where})" for num, where in orphans))


def test_d37_d52_are_closed_in_registry():
    """ТЗ-19: D-37 и D-52 помечены ``~~D-XX~~`` со статусом ``**закрыт**``."""
    text = _registry_text()
    for num in ("D-37", "D-52"):
        row = next((line for line in text.splitlines()
                    if line.startswith(f"| ~~{num}~~ |")), None)
        assert row is not None, f"{num} не помечен закрытым (нет строки `| ~~{num}~~ |`)"
        assert "**закрыт**" in row, f"{num} без статуса **закрыт**: {row[:120]!r}"
        assert f"TODO(debt-{num})" not in text, (
            f"в реестре остался маркер TODO(debt-{num})")
    markers = {num for num, _ in _code_markers()}
    assert "D-37" not in markers and "D-52" not in markers, (
        "закрытые D-37/D-52 всё ещё имеют TODO(debt-...) в коде")
