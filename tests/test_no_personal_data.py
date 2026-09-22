"""ТЗ-54: страж — личных данных владельца в коде репозитория быть не должно.

Репозиторий публикуется на GitHub. Документацию чистит упаковщик
(`/root/tz6/sanitize_package.py`), а КОД он не трогает — поэтому личные адреса
доставки и служебные идентификаторы обязаны лежать ВНЕ кода: в файле окружения
`/root/.hermes/tuber_owner.env` (читается обёрткой `tuber_digest_slivki.sh`).

Страж сканирует исходники `*.py` и `*.sh` (без `tests/data/` и `reports/`) на
личные литералы: ID личного Telegram-чата доставки, логин личного аккаунта,
IP боевого сервера, номер проекта Google Cloud и шесть коротких id ключей
YouTube (список — блок `REPLACEMENTS` в sanitize_package.py). Нашёл — падает с
перечислением файлов и строк.

ВАЖНО: искомые литералы собираются здесь из КУСКОВ (неявная конкатенация
строк), чтобы сам страж не содержал их целиком и не падал на себе.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Личные литералы, собранные из кусков (источник целых строк — sanitize_package.py).
PERSONAL_LITERALS = [
    "1915" "52727",           # ID личного Telegram-чата доставки
    "info_" "CacheCoin",      # логин личного Telegram-аккаунта (Telethon)
    "109.172" ".47.168",      # IP боевого сервера
    "483006" "627002",        # номер проекта Google Cloud
    "k90ec2" "d473a",         # короткий id ключа YouTube (1..6)
    "kbfba3" "2fd17",
    "kad52b" "c55c4",
    "k68670" "b7909",
    "ke453e" "7f3a3",
    "kaca27" "6fb22",
]

# Каталоги (по относительному пути от корня), которые не считаются кодом.
EXCLUDED_DIRS = ("tests/data/", "reports/")


def _is_excluded(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    return any(rel.startswith(prefix) for prefix in EXCLUDED_DIRS)


def scan_code(root: str) -> list[str]:
    """Строки-нарушения `файл:строка: литерал` в `*.py`/`*.sh` под `root`."""
    findings: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__",
                                                        ".pytest_cache", ".venv")]
        for name in filenames:
            if not (name.endswith(".py") or name.endswith(".sh")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if _is_excluded(rel):
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    lines = fh.readlines()
            except (OSError, UnicodeDecodeError):
                continue
            for num, line in enumerate(lines, start=1):
                for literal in PERSONAL_LITERALS:
                    if literal in line:
                        findings.append(f"{rel}:{num}: {literal}")
    return findings


def test_no_personal_data_in_code():
    findings = scan_code(ROOT)
    assert findings == [], (
        "в коде найдены личные данные (их место — файл окружения вне"
        " репозитория, ТЗ-54):\n  - " + "\n  - ".join(findings))


def test_scanner_detects_planted_personal_literal(tmp_path):
    """Стража нельзя ослепить: подсаженный литерал находится."""
    planted = tmp_path / "leak.py"
    planted.write_text("chat = " + '"' + PERSONAL_LITERALS[0] + '"\n',
                       encoding="utf-8")
    findings = scan_code(str(tmp_path))
    assert any("leak.py" in f and PERSONAL_LITERALS[0] in f for f in findings), findings


def test_scanner_skips_excluded_dirs(tmp_path):
    """tests/data/ и reports/ намеренно исключены (данные и выгрузки)."""
    data = tmp_path / "tests" / "data"
    data.mkdir(parents=True)
    (data / "sample.py").write_text("x = " + '"' + PERSONAL_LITERALS[0] + '"\n',
                                    encoding="utf-8")
    assert scan_code(str(tmp_path)) == []


if __name__ == "__main__":  # pragma: no cover - ручной прогон
    problems = scan_code(ROOT)
    if problems:
        print("\n".join(problems))
        raise SystemExit(1)
    print("личных данных в коде не найдено")
