"""Пересборка docx из готового markdown (режим ``--from-md``).

Проверяем, что ``reports/build_viral_topics.py --from-md <md> [--out-docx <dx>]``
строит docx БЕЗ чтения данных Tuber и без перезаписи md, применяя ту же логику
форматирования, что и обычный прогон: ``# `` → 16pt bold, ``## `` → 14pt bold,
``N. `` → 11pt, отступ в 3 пробела → 10pt, ``- `` → ``• `` 11pt, прочее → 11pt.

Главная проверка — побайтовое (по тексту абзацев) совпадение docx с
нормализованными непустыми строками md. Сеть и боевая база не используются:
скрипт в этом режиме выходит до загрузки выгрузки и штатного отчёта.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "reports", "build_viral_topics.py")
MD = os.path.join(ROOT, "reports", "viral-topics-2026-10-01.md")


def _norm(line: str) -> str | None:
    """Нормализация строки md в текст абзаца docx (пустые строки пропускаются)."""
    if not line.strip():
        return None
    if line.startswith("# "):
        return line[2:]
    if line.startswith("## "):
        return line[3:]
    if re.match(r"^\d+\. ", line):
        return line
    if line.startswith("   "):
        return line.strip()
    if line.startswith("- "):
        return "• " + line[2:]
    return line


def _expected_lines(md_text: str) -> list[str]:
    return [x for x in (_norm(ln) for ln in md_text.splitlines()) if x is not None]


def _md5(path: str) -> str:
    return hashlib.md5(open(path, "rb").read()).hexdigest()


@pytest.mark.skipif(not os.path.exists(MD), reason="нет готового md 01.10 (артефакт прогона)")
def test_from_md_matches_markdown(tmp_path):
    from docx import Document

    md_before = open(MD, encoding="utf-8").read()
    md_hash_before = _md5(MD)

    out = tmp_path / "rebuilt.docx"
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--from-md", MD, "--out-docx", str(out)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists(), proc.stdout

    # md не перезаписан.
    assert _md5(MD) == md_hash_before

    expected = _expected_lines(md_before)
    got = [p.text for p in Document(str(out)).paragraphs]
    assert len(got) == len(expected), (len(got), len(expected))
    assert got == expected

    # Маркеры списка переведены из "- " в "• ".
    assert sum(1 for t in got if t.startswith("• ")) == \
        sum(1 for ln in md_before.splitlines() if ln.startswith("- "))


@pytest.mark.skipif(not os.path.exists(MD), reason="нет готового md 01.10 (артефакт прогона)")
def test_from_md_does_not_touch_default_docx(tmp_path):
    """С указанным --out-docx боевой docx рядом с md не трогается."""
    target = os.path.splitext(MD)[0] + ".docx"
    before = _md5(target) if os.path.exists(target) else None
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--from-md", MD, "--out-docx", str(tmp_path / "x.docx")],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    after = _md5(target) if os.path.exists(target) else None
    assert after == before
