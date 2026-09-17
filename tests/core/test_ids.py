"""Тесты внешних идентификаторов (Telegram — составной ключ)."""

from __future__ import annotations

from tuber.core import ids


def test_tg_external_id_and_back():
    ext = ids.tg_external_id("chan1", 100)
    assert ext == "chan1/100"
    assert ids.tg_message_id(ext) == 100


def test_tg_message_id_bad_input():
    assert ids.tg_message_id("") is None
    assert ids.tg_message_id("nodash") is None
    assert ids.tg_message_id("chan/x") is None


def test_plain_external_id():
    assert ids.plain_external_id(None) == ""
    assert ids.plain_external_id("v1") == "v1"
    assert ids.plain_external_id(123) == "123"
