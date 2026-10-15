"""Тесты разбора дат (ТЗ-1 §7)."""

from __future__ import annotations

import pytest

from tuber.core import timeutil


def test_epoch_to_iso():
    assert timeutil.epoch_to_iso(0) == "1970-01-01 00:00:00"
    assert timeutil.epoch_to_iso(1700000000) == "2023-11-14 22:13:20"
    assert timeutil.epoch_to_iso(None) is None


def test_iso_now_shape():
    value = timeutil.iso_now()
    assert len(value) == 19 and value[4] == "-" and value[10] == " "


@pytest.mark.parametrize(
    "value,expected",
    [
        (1700000000, "2023-11-14 22:13:20"),
        ("1700000000", "2023-11-14 22:13:20"),
        ("2026-09-16 15:21:00", "2026-09-16 15:21:00"),
        ("2026-09-16T15:21:00", "2026-09-16 15:21:00"),
        ("2026-09-16T15:21:00Z", "2026-09-16 15:21:00"),
        ("2026-09-16T15:21:00+03:00", "2026-09-16 12:21:00"),
        ("2026-09-16", "2026-09-16 00:00:00"),
    ],
)
def test_parse_any(value, expected):
    assert timeutil.parse_any(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-date", "2026-13-45"])
def test_parse_any_empty_and_bad(value):
    assert timeutil.parse_any(value) is None


def test_parse_any_round_trip_and_iso_to_epoch():
    iso = timeutil.epoch_to_iso(1788998400)
    assert timeutil.parse_any(iso) == iso
    assert timeutil.iso_to_epoch(iso) == 1788998400
    assert timeutil.date_of(iso) == iso[:10]
