"""Границы суточной квоты по Pacific Time (America/Los_Angeles).

Google сбрасывает суточную квоту ПРОЕКТА в полночь Pacific Time; в сентябре
это 07:00 UTC. Предохранитель суточной квоты раньше считал сутки по UTC-полночи
(`ts // 86400 * 86400`), из-за чего в окне 00:00–07:00 UTC расход обнулялся
раньше Google и можно было превысить реальный остаток.

Сеть не используется: только мок сессии и локальная база. Время подменяется
через ``config.now_ts``.
"""

from __future__ import annotations

import sqlite3

import pytest

from tuber.platforms.youtube import config, store as db, api as yt
from zoneinfo import ZoneInfoNotFoundError


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self._handler(dict(params or {}))


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "quota_pacific.db")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset_tz_warning():
    """Сброс флага «предупреждение о поясе уже выдано» между тестами."""
    yt._quota_tz_warned = False
    yield
    yt._quota_tz_warned = False


def _utc(s: str) -> int:
    from datetime import datetime, timezone

    return int(
        datetime.strptime(s, "%Y-%m-%d %H:%M")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _client(conn, keys):
    return yt.YouTubeClient(
        keys=list(keys),
        conn=conn,
        session=FakeSession(lambda params: FakeResp(200, {"items": []})),
        min_interval=0,
        sleep=lambda _s: None,
    )


def _legacy_row(conn, date, key_id, units):
    """Строка «старого» формата: без ts (как до появления колонки)."""
    conn.execute(
        "INSERT INTO quota_log (date, key_id, calls, units, endpoint) "
        "VALUES (?, ?, 1, ?, 'search')",
        (date, key_id, units),
    )
    conn.commit()


# --- 1. сутки квоты не меняются на UTC-полуночи ----------------------------


def test_same_pt_day_across_utc_midnight(conn, monkeypatch):
    """Расход в 23:30 UTC виден в 00:30 UTC следующих суток — тот же день PT.

    Сентябрь: PDT (UTC−7), полночь PT = 07:00 UTC, поэтому 00:30 UTC 14 сентября
    всё ещё относится к суткам квоты 13 сентября. Старый код по UTC-полуночи
    счёл бы день новым и позволил бы расход сверх реального остатка.
    """
    logged_at = _utc("2026-09-13 23:30")   # PT 13 сентября 16:30
    check_at = _utc("2026-09-14 00:30")    # PT всё ещё 13 сентября 17:30
    db.log_quota(
        conn, yt._day_start(logged_at), yt.YouTubeClient.key_id("k1"),
        1, 8000, "search", ts=logged_at,
    )
    client = _client(conn, ["k1"])
    monkeypatch.setattr(config, "now_ts", lambda: check_at)

    assert client.spent_today("k1") == 8000
    assert client.quota_exceeded("k1") is True


# --- 2. смена суток наступает в полночь PT (07:00 UTC) ---------------------


def test_pt_midnight_rolls_to_new_quota_day(conn, monkeypatch):
    """После 07:00 UTC расход до полудни PT в текущее окно не попадает.

    Обе отметки — одна UTC-дата (14 сентября), но разные сутки квоты PT:
    05:00 UTC = 13 сентября 22:00 PDT, 08:00 UTC = 14 сентября 01:00 PDT.
    Старый код по UTC-дате посчитал бы расход действующим (та же дата) — тест
    это и ловит.
    """
    logged_at = _utc("2026-09-14 05:00")   # PT 13 сентября
    check_at = _utc("2026-09-14 08:00")    # PT 14 сентября (после полуночи PT)
    db.log_quota(
        conn, yt._day_start(logged_at), yt.YouTubeClient.key_id("k1"),
        1, 8000, "search", ts=logged_at,
    )
    client = _client(conn, ["k1"])
    monkeypatch.setattr(config, "now_ts", lambda: check_at)

    # Сутки сменились: расход прошлых суток PT не учитываем.
    assert client.spent_today("k1") == 0
    assert client.quota_exceeded("k1") is False


# --- 3. старые строки без ts ------------------------------------------------


def test_legacy_rows_counted_from_start_of_quota_day_utc(conn, monkeypatch):
    """Старые строки без ts учитываются начиная с UTC-суток начала суток квоты.

    Порог — `_day_start(quota_day)`: при проверке 14.09 03:00 UTC сутки квоты
    начались 13.09 07:00 UTC, значит годится UTC-дата 13.09 (и новее), а 12.09
    и старше — нет. Плюс строка нового формата с ts, попадающая в текущие сутки
    квоты PT, но записанная в прошлую UTC-дату: она тоже должна учитываться.
    """
    check_at = _utc("2026-09-14 03:00")  # PT 13 сентября 20:00
    utc_today = yt._day_start(check_at)          # 2026-09-14 00:00 UTC
    utc_prev = utc_today - 86400                 # 2026-09-13 00:00 UTC = порог
    utc_too_old = utc_today - 2 * 86400          # 2026-09-12 00:00 UTC — старее
    in_pt_day = _utc("2026-09-13 20:00")         # тот же день PT, прошлая UTC-дата

    _legacy_row(conn, utc_today, yt.YouTubeClient.key_id("k1"), 100)
    _legacy_row(conn, utc_prev, yt.YouTubeClient.key_id("k1"), 200)
    _legacy_row(conn, utc_too_old, yt.YouTubeClient.key_id("k1"), 50)
    db.log_quota(
        conn, yt._day_start(in_pt_day), yt.YouTubeClient.key_id("k1"),
        1, 300, "search", ts=in_pt_day,
    )
    client = _client(conn, ["k1"])
    monkeypatch.setattr(config, "now_ts", lambda: check_at)

    # 100 + 200 (старые, не старше порога) + 300 (ts в текущих сутках PT);
    # 50 (старее порога) не учитывается.
    assert client.spent_today("k1") == 600


def test_legacy_transitional_row_previous_utc_day_counted(conn, monkeypatch):
    """Переходный день: legacy-строка за 13.09 видна в прогоне 14.09 03:00 UTC.

    Ошибка прежнего кода шла в опасную сторону (недооценка): условие
    `date = текущая UTC-дата` отбрасывало запись 13.09 20:00 UTC (777 units)
    при проверке 14.09 03:00 UTC, хотя Google относил её к текущим суткам
    квоты. Предохранитель должен видеть расход, а не пропускать его.
    """
    check_at = _utc("2026-09-14 03:00")   # PT 13 сентября 20:00
    legacy_day = yt._day_start(_utc("2026-09-13 20:00"))  # 2026-09-13 00:00 UTC
    _legacy_row(conn, legacy_day, yt.YouTubeClient.key_id("k1"), 777)
    client = _client(conn, ["k1"])
    monkeypatch.setattr(config, "now_ts", lambda: check_at)

    assert client.spent_today("k1") == 777


def test_legacy_row_older_than_quota_day_not_counted(conn, monkeypatch):
    """Обратная граница: legacy-строка за 11.09 в сутки квоты 13.09 не входит.

    UTC-дата 11.09 старее порога `_day_start(quota_day)` (13.09 00:00 UTC),
    поэтому не учитывается: преувеличение расхода не должно распространяться
    на произвольно устаревшие сутки.
    """
    check_at = _utc("2026-09-14 03:00")   # PT 13 сентября 20:00
    old_day = yt._day_start(_utc("2026-09-11 12:00"))  # 2026-09-11 00:00 UTC
    _legacy_row(conn, old_day, yt.YouTubeClient.key_id("k1"), 999)
    client = _client(conn, ["k1"])
    monkeypatch.setattr(config, "now_ts", lambda: check_at)

    assert client.spent_today("k1") == 0


def test_rows_with_ts_counted_by_ts_regardless_of_date(conn, monkeypatch):
    """Регресс: строки с ts считаются по ts, а колонка date на них не влияет."""
    check_at = _utc("2026-09-14 03:00")   # PT 13 сентября 20:00
    quota_day = yt._quota_day_start(check_at)   # 13.09 07:00 UTC
    inside_ts = quota_day + 3600                 # 13.09 08:00 UTC
    before_ts = quota_day - 3600                 # 13.09 06:00 UTC — до суток квоты
    old_date = yt._day_start(_utc("2026-09-11 12:00"))  # намеренно устаревшая date

    # ts внутри суток квоты, date намеренно старая — должна учитываться по ts.
    db.log_quota(
        conn, old_date, yt.YouTubeClient.key_id("k1"),
        1, 123, "search", ts=inside_ts,
    )
    # ts до начала суток квоты — не учитывается, несмотря на близкую дату.
    db.log_quota(
        conn, yt._day_start(before_ts), yt.YouTubeClient.key_id("k1"),
        1, 456, "search", ts=before_ts,
    )
    client = _client(conn, ["k1"])
    monkeypatch.setattr(config, "now_ts", lambda: check_at)

    assert client.spent_today("k1") == 123


# --- 4. миграция колонки ts -------------------------------------------------


# --- 5. отсутствие данных о поясах -----------------------------------------


def test_missing_zoneinfo_falls_back_without_crash(conn, monkeypatch, caplog):
    """При отсутствии tzdata клиент не падает: фикс. −8 и одно предупреждение.

    Перенесено из legacy `tests/test_quota_pacific.py` (ТЗ-2b): поведение
    `_quota_tzinfo()` (запасное смещение PST −8) осталось в
    `tuber/platforms/youtube/api.py`, поэтому тест обязан жить и здесь.
    Отличие от legacy одно — имя логгера нового модуля.
    """
    import logging

    def _no_zone(name):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(yt, "ZoneInfo", _no_zone)
    client = _client(conn, ["k1"])
    with caplog.at_level(logging.WARNING, logger=yt.__name__):
        assert client.spent_today("k1") == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("zoneinfo" in m for m in messages)
    # Второй вызов не дублирует предупреждение (флаг «уже предупредили»).
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=yt.__name__):
        assert client.spent_today("k1") == 0
    assert not [r for r in caplog.records if "zoneinfo" in r.getMessage()]
