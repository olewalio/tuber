"""Общие фикстуры тестов Telegram-платформы (ТЗ-4).

Сеть не используется: HTTP-страницы подменяются локальными фикстурами
`tests/telegram/fixtures`. Рабочая (боевая) единая база не трогается: все
проверки идут на временных базах в `tmp_path`.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tuber.platforms.telegram import config, store as db  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture
def tg_path(tmp_path):
    """Путь к временной единой базе со схемой ядра и слоем совместимости."""
    p = str(tmp_path / "tuber_tg_test.db")
    con = db.init_db(p)
    con.close()
    return p


@pytest.fixture
def con(tg_path):
    """Соединение-адаптер к временной базе (legacy-форма таблиц Telegram)."""
    c = db.connect(tg_path)
    yield c
    c.close()


@pytest.fixture
def fixture_text():
    def _read(name):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            return fh.read()
    return _read
