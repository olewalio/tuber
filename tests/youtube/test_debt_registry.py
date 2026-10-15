"""Инвариант нумерации технического долга.

Защита от повторения дефекта: новый долг был заведён под уже занятым номером
(в реестре существовал закрытый долг с тем же номером).

Проверки:
1. Каждый номер долга встречается в ``docs/TECH-DEBT.md`` ровно один раз
   как заголовок раздела вида ``### D-XX.`` — номера уникальны, повторное
   использование занятого номера запрещено даже для закрытого долга.
2. Каждый маркер ``TODO(debt-D-XX)`` в ``tuber/`` ссылается на номер, у
   которого в реестре есть раздел (маркер без раздела — ошибка).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "TECH-DEBT.md"
CODE_DIR = ROOT / "tuber"

ID_PATTERN = r"[A-Za-z][A-Za-z0-9]*-?\d+"
SECTION_RE = re.compile(r"^###\s+(" + ID_PATTERN + r")\.\s")
MARKER_RE = re.compile(r"TODO\(debt-(" + ID_PATTERN + r")\)")


def _registry_sections() -> list[str]:
    text = REGISTRY.read_text(encoding="utf-8")
    return [m.group(1) for line in text.splitlines() for m in [SECTION_RE.match(line)] if m]


def _code_markers() -> list[tuple[str, str]]:
    """Возвращает [(номер, путь/строка)] по всем маркерам в tuber/."""
    out: list[tuple[str, str]] = []
    for path in sorted(CODE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in MARKER_RE.finditer(line):
                out.append((m.group(1), f"{path.relative_to(ROOT)}:{lineno}"))
    return out


def test_debt_numbers_are_unique_in_registry():
    """Ни один номер долга не используется дважды в заголовках реестра."""
    sections = _registry_sections()
    counts = Counter(sections)
    duplicates = {num: n for num, n in counts.items() if n > 1}
    assert not duplicates, (
        "Номера долгов переиспользованы в docs/TECH-DEBT.md "
        "(каждый номер обязан встречаться ровно один раз): "
        + ", ".join(f"{num} — {n} раз(а)" for num, n in sorted(duplicates.items()))
    )


def test_code_markers_have_registry_sections():
    """Каждый маркер TODO(debt-D-XX) в tuber/ ссылается на существующий раздел."""
    sections = set(_registry_sections())
    orphans = [(num, where) for num, where in _code_markers() if num not in sections]
    assert not orphans, (
        "Маркеры TODO(debt-D-XX) без раздела в docs/TECH-DEBT.md: "
        + ", ".join(f"{num} ({where})" for num, where in orphans)
    )
