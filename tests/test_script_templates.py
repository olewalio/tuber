"""Страж дрейфа репозиторных шаблонов скриптов (Tuber, ТЗ-17).

За вечер дважды сработал один и тот же класс дефекта: рабочий скрипт правят,
а его репозиторный шаблон (единственный источник истины, из которого
``scripts/install_hermes_cron.sh`` генерирует рабочие шимы) остаётся старым.

* ТЗ-14 — retention дампов жило только в генерируемом файле и терялось при
  переустановке кронов;
* ТЗ-16 — обёртка бордов откатывала периоды (1/3/5/10) назад и роняла 404.

Этот тест — дешёвый статический страж: он НЕ ходит в базу и НЕ запускает
боевые скрипты, только читает файлы и делает ``bash -n`` (секунды, без
``pytest.mark.live``). Если шаблон разъедется с рабочей логикой — тест падает
с явным перечнем расхождений.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "scripts" / "common"

# Пути шаблонов. Переопределяемы через окружение — это шов для наглядной
# проверки, что страж реально ловит дрейф: подсовываем копию шаблона с
# устаревшей величиной и убеждаемся, что тест падает (приёмка ТЗ-17 §2).
BOARDS_TEMPLATE = Path(
    os.environ.get("TUBER_TEMPLATE_BOARDS", TEMPLATES / "tuber_boards_build.sh")
)
BACKUP_TEMPLATE = Path(
    os.environ.get("TUBER_TEMPLATE_BACKUP", TEMPLATES / "tuber_db_backup.sh")
)
# Обёртка сводки (ТЗ-B ТЗ-19, D-52): раздел Telegram и пост-уровневый фильтр
# темы включаются ОДНОЙ строкой каждая — страж следит, чтобы обе переменные
# остались в шаблоне.
REPORT_TEMPLATE = Path(
    os.environ.get("TUBER_TEMPLATE_REPORT", TEMPLATES / "tuber_report.sh")
)

# ``PERIODS="1 3 5 10"`` (допускаем кавычки/без кавычек и пробелы).
PERIODS_RE = re.compile(
    r"""^\s*PERIODS=(?:"([^"]*)"|'([^']*)'|([^\s#]+))""", re.MULTILINE
)
# Цикл по всем окнам: ``for d in $PERIODS`` (или ${PERIODS[@]}).
PERIODS_LOOP_RE = re.compile(r"for\s+(\w+)\s+in\s+\$\{?PERIODS")
# Устаревшие маркеры дрейфа.
STALE_LIMIT_RE = re.compile(r"--limit\s+15\b")
STALE_THRESHOLD_RE = re.compile(r"-lt\s+20\b")


def _read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def boards_template_problems(path: Path) -> list[str]:
    """Возвращает список расхождений шаблона бордов (пусто = ок)."""
    text = _read(path)
    problems: list[str] = []

    # 1) Окна отчёта: ровно четыре значения 1/3/5/10.
    match = PERIODS_RE.search(text)
    if not match:
        problems.append("нет присваивания PERIODS=...")
    else:
        value = next(g for g in match.groups() if g is not None)
        periods = value.split()
        if sorted(periods, key=lambda v: int(v) if v.isdigit() else v) != [
            "1",
            "3",
            "5",
            "10",
        ]:
            problems.append(f'PERIODS={value!r} не равно "1 3 5 10"')

    # 2) Цикл по периодам и выкладка в подкаталог "<d>d".
    loop = PERIODS_LOOP_RE.search(text)
    if not loop:
        problems.append("нет цикла `for <var> in $PERIODS`")
    else:
        var = loop.group(1)
        if f"${{{var}}}d" not in text:
            problems.append(f'нет выкладки в подкаталог "${{{var}}}d"')

    # 3) Лимит по умолчанию 50 (ТЗ-11).
    if "TUBER_BOARDS_LIMIT:-50" not in text:
        problems.append("нет лимита по умолчанию `TUBER_BOARDS_LIMIT:-50`")

    # 4) Адаптивный порог карточек (ТЗ-12).
    if "MIN_CARDS" not in text:
        problems.append("нет адаптивного порога `MIN_CARDS`")

    # 5) Отсутствие устаревших величин.
    if STALE_LIMIT_RE.search(text):
        problems.append("остался устаревший `--limit 15`")
    if STALE_THRESHOLD_RE.search(text):
        problems.append("остался устаревший порог `-lt 20`")

    return problems


def backup_template_problems(path: Path) -> list[str]:
    """Возвращает список расхождений шаблона бэкапа (пусто = ок)."""
    text = _read(path)
    problems: list[str] = []

    # 1) Переменная хранения (KEEP).
    if not re.search(
        r"^\s*(?:TUBER_DB_BACKUP_KEEP|TUBER_BACKUP_KEEP|KEEP)=", text, re.MULTILINE
    ):
        problems.append("нет переменной хранения `KEEP`/`TUBER_*_KEEP`")

    # 2) Есть ветка удаления дампов по возрасту/количеству (retention),
    #    а не жёсткое поведение «без retention».
    removes = re.search(r"\brm\s+-f\b", text)
    by_quantity = re.search(r">=\s*[\"']?\$?\{?keep\}?", text)
    by_age = re.search(r"-mtime\b", text)
    if not removes:
        problems.append("нет удаления старых дампов (`rm -f`)")
    if not (by_quantity or by_age):
        problems.append(
            "нет ветки удаления по возрасту/количеству "
            "(сравнение с keep или -mtime)"
        )

    return problems


def report_template_problems(path: Path) -> list[str]:
    """Обёртка сводки объявляет раздел Telegram и фильтр темы (ТЗ-19 B, D-52)."""
    text = _read(path)
    problems: list[str] = []
    if not re.search(r"^\s*export\s+TUBER_TG_SECTION=", text, re.MULTILINE):
        problems.append("нет `export TUBER_TG_SECTION=...` (раздел Telegram)")
    if not re.search(r"^\s*export\s+TUBER_TG_TOPIC_FILTER=", text, re.MULTILINE):
        problems.append(
            "нет `export TUBER_TG_TOPIC_FILTER=...` (пост-уровневый фильтр темы)")
    return problems


def _assert_syntax_ok(path: Path) -> None:
    """``bash -n`` шаблона; без bash в окружении — skip, не падать."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash недоступен в окружении — синтаксис не проверяем")
    proc = subprocess.run(
        [bash, "-n", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"bash -n {path} не прошёл ({proc.returncode}):\n{proc.stderr}"
    )


def test_boards_template_matches_working_script():
    """Шаблон бордов не разъехался: периоды, лимит 50, порог, нет легаси."""
    problems = boards_template_problems(BOARDS_TEMPLATE)
    assert not problems, "дрейф шаблона бордов:\n" + "\n".join(problems)


def test_boards_template_syntax():
    _assert_syntax_ok(BOARDS_TEMPLATE)


def test_backup_template_has_retention():
    """Шаблон бэкапа хранит retention (KEEP + удаление старых дампов)."""
    problems = backup_template_problems(BACKUP_TEMPLATE)
    assert not problems, "дрейф шаблона бэкапа:\n" + "\n".join(problems)


def test_backup_template_syntax():
    _assert_syntax_ok(BACKUP_TEMPLATE)


def test_report_template_enables_telegram_and_topic_filter():
    """ТЗ-19 B (D-52): обёртка сводки объявляет ОБЕ переменные Telegram."""
    problems = report_template_problems(REPORT_TEMPLATE)
    assert not problems, "дрейф шаблона сводки:\n" + "\n".join(problems)


def test_report_template_syntax():
    _assert_syntax_ok(REPORT_TEMPLATE)
