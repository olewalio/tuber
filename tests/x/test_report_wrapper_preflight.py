"""Обёртка отчёта X не должна сама себе ломать предпроверку базы.

Регресс ТЗ-42 (класс «рядом с найденной строкой живёт копия»): в
``scripts/x/tuber_x_report.sh`` предпроверка открывала единую базу голым
``sqlite3.connect`` и спрашивала legacy-таблицы ``posts``/``accounts``/``stories``.
Эти таблицы живут только в слое совместимости (TEMP-представления над ядром
``source``/``content``), поэтому предпроверка ВСЕГДА падала и обёртка печатала
``ALERT Tuber-x: … база данных недоступна или повреждена``, не собирая отчёт.

Тест берёт heredoc предпроверки прямо из обёртки и исполняет его на временной
единой базе: пока код открывает базу через ``store.connect`` (со слоем
совместимости), проверка проходит; возврат к ``sqlite3.connect`` снова её ломает.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "x", "tuber_x_report.sh")


def _first_python_heredoc(text: str) -> str:
    """Текст первого heredoc ``<<'PY' … PY`` из shell-скрипта."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "<<'PY'" in line:
            start = i + 1
            break
    assert start is not None, "в обёртке нет python-heredoc"
    body = []
    for line in lines[start:]:
        if line.strip() == "PY":
            return "\n".join(body)
        body.append(line)
    raise AssertionError("python-heredoc не закрыт")


def test_report_wrapper_db_preflight_passes(db_path):
    """Предпроверка обёртки открывает единую базу и видит legacy-представления."""
    with open(SCRIPT, encoding="utf-8") as fh:
        code = _first_python_heredoc(fh.read())

    saved = sys.argv
    sys.argv = ["-", db_path]
    try:
        # exec именно так, как это делает `python3 - "$DB_PATH" <<'PY'`.
        exec(compile(code, SCRIPT + "::preflight", "exec"), {"__name__": "__main__"})
    finally:
        sys.argv = saved
