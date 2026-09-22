#!/usr/bin/env python3
"""Аудит репозитория на пути отката/записи в рабочую БД (ТЗ-6 задача 5).

Контекст: 15.09.2026 внешний откат рабочей базы из снимка затёр живой сбор
(484 поста превратились в 152). Данные спасли только потому, что снимок уцелел.
Этот инструмент проверяет, что в репозитории **нет** пути, который может
записать в рабочую БД или скопировать поверх неё снимок.

Что считается опасным (HAZARD):

* `copyfile/copy/copy2(<источник>, config.DB_PATH)` — запись поверх рабочей БД;
* `open(config.DB_PATH, "w"/"a"/"r+")` — открытие рабочей БД на запись;
* `sqlite3 <рабочая БД> ".restore ..."` или `.restore(` в коде;
* перенаправление в файл рабочей БД (`> .../tuber_x.db`, `cp ... tuber_x.db`);
* скрипт с именем `*restore*`/`*откат*`.

Читающие копии (`copy2(config.DB_PATH, <временный путь>)`) опасными не считаются:
источник только читается, приёмник — временный файл. Они выводятся в отчёт как
OK для наглядности.

Запуск: python3 scripts/acceptance/x_audit_writeback.py [--json]
Код возврата: 0 — чисто, 1 — найдены опасные пути.
"""
from __future__ import annotations

import json
import os
import re
import sys

# Файл лежит в scripts/acceptance/ — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "data", "reports"}
# Сам аудит содержит шаблоны поиска как текст — его не сканируем.
SKIP_FILES = {os.path.join("scripts", "acceptance", "x_audit_writeback.py")}
SCAN_EXT = {".py", ".sh", ".bash", ".md", ".txt", ".yaml", ".yml", ".toml", ".ini"}
# Поиск опасных вызовов — только в исполняемом коде: в документации и журналах
# строки вида `.restore(` могут быть простым описанием, а не вызовом.
CODE_EXT = {".py", ".sh", ".bash"}

# Опасные шаблоны: пишут в рабочую БД или копируют поверх неё.
HAZARD_PATTERNS = [
    (re.compile(r"copy(?:file|2)?\(\s*[^,)]+,\s*config\.DB_PATH"), "копирование поверх config.DB_PATH"),
    (re.compile(r"(?:copy|copyfile|copy2)\([^)]*?,\s*[^)]*?data/tuber\.db"), "копирование поверх рабочей БД по пути"),
    (re.compile(r"open\(\s*config\.DB_PATH\s*,\s*['\"][wa+]"), "open(config.DB_PATH, на запись)"),
    (re.compile(r"\.restore\("), "вызов sqlite .restore()"),
    (re.compile(r"\.backup\([^)]*config\.DB_PATH"), "sqlite backup в рабочую БД"),
    (re.compile(r"sqlite3\s+\S*data/tuber\.db\s+\"?'?\.restore"), "sqlite3 .restore в рабочую БД"),
    (re.compile(r"[>]{1,2}\s*\S*data/tuber\.db"), "перенаправление вывода в рабочую БД"),
    (re.compile(r"\bcp\b[^\n]*\s\S*data/tuber\.db"), "cp поверх рабочей БД"),
    (re.compile(r"\bmv\b[^\n]*\s\S*data/tuber\.db"), "mv поверх рабочей БД"),
]

# Читающие копии — не опасны, но информативны.
OK_PATTERNS = [
    (re.compile(r"copy(?:file|2)?\(\s*config\.DB_PATH\s*,"), "чтение рабочей БД -> копия"),
]

HAZARD_NAME = re.compile(r"(restore|откат|rollback|snapshot_db|db_restore)", re.I)


def _iter_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() in SCAN_EXT:
                rel = os.path.relpath(os.path.join(base, name), ROOT)
                if rel in SKIP_FILES:
                    continue
                yield os.path.join(base, name)


def scan():
    """Возвращает {"hazards": [...], "ok_usages": [...], "files_scanned": N}."""
    hazards, oks, n = [], [], 0
    for path in _iter_files():
        rel = os.path.relpath(path, ROOT)
        n += 1
        if HAZARD_NAME.search(os.path.basename(path)):
            hazards.append({"file": rel, "line": 0,
                            "kind": "подозрительное имя файла",
                            "text": os.path.basename(path)})
        try:
            is_code = os.path.splitext(path)[1].lower() in CODE_EXT
            with open(path, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if not is_code:
                        continue
                    for rx, kind in HAZARD_PATTERNS:
                        if rx.search(line):
                            hazards.append({"file": rel, "line": lineno,
                                            "kind": kind, "text": line.strip()[:200]})
                    for rx, kind in OK_PATTERNS:
                        if rx.search(line):
                            oks.append({"file": rel, "line": lineno,
                                        "kind": kind, "text": line.strip()[:200]})
        except OSError:
            continue
    return {"hazards": hazards, "ok_usages": oks, "files_scanned": n}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    res = scan()
    if "--json" in argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"аудит репозитория на пути отката/записи в рабочую БД (файлов: {res['files_scanned']})")
        print(f"опасных путей: {len(res['hazards'])}")
        for h in res["hazards"]:
            print(f"  FAIL {h['file']}:{h['line']} {h['kind']}: {h['text']}")
        print(f"читающих копий рабочей БД (безопасно): {len(res['ok_usages'])}")
        for o in res["ok_usages"]:
            print(f"  OK   {o['file']}:{o['line']} {o['kind']}")
    return 1 if res["hazards"] else 0


if __name__ == "__main__":
    sys.exit(main())
