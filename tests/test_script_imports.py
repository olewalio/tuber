"""Страж живых импортов в обёртках (Tuber).

Обёртки расписания (``scripts/**/*.sh``) запускают питон-код внутри heredoc и
при этом лежат ВНЕ обычного импортного графа: ошибка в имени модуля или символа
не ловится ни компилятором, ни остальными тестами, а всплывает только в cron в
виде ``ALERT``. Так был потерян целый шаг дискавери X: в
``scripts/x/tuber_x_discover.sh`` импортировался несуществующий
``tuber.platforms.x.db`` (слой доступа — ``store.py``), и прогон вообще не
запускался.

Тест достаёт из каждого ``.sh`` (и, для полноты, ``.py``) все импорты имён из
``tuber.*`` — включая многострочные импорты в скобках — и проверяет, что
модуль импортируется, а символ в нём существует. Наличие любой «битой» строки
даёт явный ``AssertionError`` со списком ``файл:строка → модуль.символ``.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

# ``from tuber.x.y import a, b as c`` — начало импорта.
FROM_RE = re.compile(r"^\s*from\s+(tuber[\w.]*)\s+import\s+(.*)$")
# ``import tuber.x.y`` / ``import tuber.x.y as z``.
IMPORT_RE = re.compile(r"^\s*import\s+(tuber[\w.]*(?:\s*,\s*tuber[\w.]*)*)\s*$")
NAME_RE = re.compile(r"^([\w.]+)(?:\s+as\s+(\w+))?$")


def _strip_comment(text: str) -> str:
    return text.split("#", 1)[0].strip()


def _parse_names(rest: str) -> list[tuple[str, str | None]]:
    """Разбирает хвост ``import`` в список (имя, псевдоним)."""
    cleaned = rest.replace("(", " ").replace(")", " ").replace("\\", " ")
    names: list[tuple[str, str | None]] = []
    for token in cleaned.split(","):
        token = _strip_comment(token)
        if not token:
            continue
        match = NAME_RE.match(token)
        if match:
            names.append((match.group(1), match.group(2)))
    return names


def _collect_imports(text: str):
    """Отдаёт список (строка_начала, модуль, [(имя, псевдоним), ...])."""
    lines = text.splitlines()
    found = []
    i = 0
    while i < len(lines):
        match = FROM_RE.match(lines[i])
        if match:
            module = match.group(1)
            rest = match.group(2)
            start = i + 1
            if "(" in rest and ")" not in rest:
                buf = rest
                j = i + 1
                while j < len(lines) and ")" not in lines[j]:
                    buf += " " + lines[j]
                    j += 1
                if j < len(lines):
                    buf += " " + lines[j]
                rest = buf
                i = j
            found.append((start, module, _parse_names(rest)))
            i += 1
            continue
        match = IMPORT_RE.match(lines[i])
        if match:
            for chunk in match.group(1).split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                alias = None
                if " as " in chunk:
                    chunk, alias = [p.strip() for p in chunk.split(" as ", 1)]
                found.append((i + 1, chunk, [(chunk.split(".")[-1], alias)]))
        i += 1
    return found


def _iter_script_files():
    for path in sorted(SCRIPTS.rglob("*.sh")):
        yield path
    for path in sorted(SCRIPTS.rglob("*.py")):
        yield path


def _check(module: str, name: str) -> str | None:
    """Возвращает текст ошибки либо None, если символ импортируется."""
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 — нужен любой сбой импорта
        return f"модуль {module!r} не импортируется: {type(exc).__name__}: {exc}"
    if hasattr(mod, name):
        return None
    try:
        importlib.import_module(f"{module}.{name}")
        return None
    except Exception:  # noqa: BLE001
        return f"в модуле {module!r} нет символа {name!r}"


def test_script_tuber_imports_are_alive():
    """Каждый импорт из tuber.* в scripts/ должен указывать на живой символ."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    broken: list[str] = []
    checked = 0
    for path in _iter_script_files():
        rel = path.relative_to(REPO_ROOT)
        for line, module, names in _collect_imports(path.read_text(encoding="utf-8")):
            for name, alias in names:
                if name == "SYNTAX":  # защита от нештатного разбора
                    continue
                checked += 1
                error = _check(module, name)
                if error:
                    shown = f"{name} as {alias}" if alias else name
                    broken.append(f"{rel}:{line} → {module}.{shown} → {error}")

    assert checked > 0, "не найдено ни одного импорта tuber.* в scripts/ — разбор сломан"
    assert not broken, "мёртвые импорты в обёртках:\n" + "\n".join(broken)
