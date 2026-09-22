"""ТЗ-44C: обход подписчиков на границе бюджета окна.

Проверяется ГЛАВНОЕ отличие «бюджет исчерпан» от «отказ аккаунта»:
  * бюджет окна/суток -> аккаунты ОТЛОЖЕНЫ, а не записаны в отказы; код 0;
  * в Nitter за подписчиками не ходим (подписчиков там нет);
  * настоящий отказ транспорта (429/401/403/сеть) -> код 1;
  * сессия недоступна -> код 2.

Сеть не используется: сессионный транспорт подменяется объектом-заглушкой,
Nitter-брокер подменяется «ловушкой», которая падает при создании.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tuber.platforms.x import cli, config, session as xsession
from tuber.platforms.x import store as db


def _seed(con, handle, status="active"):
    con.execute("INSERT OR IGNORE INTO source (platform, handle, status)"
                " VALUES ('x', ?, ?)", (handle, status))
    con.commit()


def _insert_request(con, seconds_ago=100):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
          ).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "INSERT INTO main.transport_request"
        " (platform, host, ts, kind, url, status) VALUES ('x','x.com',?,"
        "'x_session','url',200)", (ts,))
    con.commit()


class FakeSession:
    """Подменяемая сессия: первые N вызовов дают профиль, затем исключение."""

    def __init__(self, *, fail_on=None, exc=None, count=1000):
        self.fail_on = fail_on
        self.exc = exc
        self.count = count
        self.calls = []

    def followers(self, handle):
        self.calls.append(handle)
        if self.fail_on is not None and len(self.calls) >= self.fail_on:
            raise self.exc
        return {"id": "1", "handle": handle, "followers": self.count}


def _no_nitter(monkeypatch):
    """Ловушка: любое обращение к Nitter в followers — ошибка (ТЗ-44C)."""
    def boom(*a, **kw):
        raise AssertionError("followers не должен трогать Nitter")
    monkeypatch.setattr(cli, "NitterBroker", boom)
    monkeypatch.setattr(cli, "NitterError", Exception)


def test_window_budget_defers_not_fails(con, monkeypatch, capsys):
    """Бюджет окна -> отложено, код 0, строка с временем продолжения."""
    for h in ("a1", "a2", "a3"):
        _seed(con, h)
    _insert_request(con)
    fake = FakeSession(fail_on=2, exc=xsession.XSessionWindowCap("окно"))
    monkeypatch.setattr(xsession, "get_session_transport", lambda **kw: fake)
    _no_nitter(monkeypatch)

    rc = cli.cmd_followers(SimpleNamespace(limit=10, handle=None))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "обновлено 1" in out and "отложено 2" in out and "отказов 0" in out
    assert "бюджет окна" in out and "продолжение после" in out and "UTC" in out
    assert "nitter" not in out.lower()


def test_daily_budget_defers_not_fails(con, monkeypatch, capsys):
    """Бюджет суток -> тоже отложено (не отказ), код 0."""
    for h in ("b1", "b2"):
        _seed(con, h)
    fake = FakeSession(fail_on=1, exc=xsession.XSessionDailyCap("сутки"))
    monkeypatch.setattr(xsession, "get_session_transport", lambda **kw: fake)
    _no_nitter(monkeypatch)

    rc = cli.cmd_followers(SimpleNamespace(limit=10, handle=None))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "отложено 2" in out and "бюджет суток" in out and "отказов 0" in out


def test_real_transport_refusal_exit_1(con, monkeypatch, capsys):
    """429/cooldown — настоящий отказ: в отказы и код 1."""
    for h in ("c1", "c2"):
        _seed(con, h)
    fake = FakeSession(fail_on=1, exc=xsession.XSessionRateLimited("429"))
    monkeypatch.setattr(xsession, "get_session_transport", lambda **kw: fake)
    _no_nitter(monkeypatch)

    rc = cli.cmd_followers(SimpleNamespace(limit=10, handle=None))
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "отказов 1" in out and "отложено 0" in out


def test_session_unavailable_exit_2(con, monkeypatch, capsys):
    """Нет сессии вовсе — код 2, отдельная строка."""
    _seed(con, "d1")
    monkeypatch.setattr(xsession, "get_session_transport", lambda **kw: None)
    _no_nitter(monkeypatch)

    rc = cli.cmd_followers(SimpleNamespace(limit=10, handle=None))
    out = capsys.readouterr().out
    assert rc == 2, out
    assert "сессия недоступна" in out


def test_no_failures_exit_0_even_when_deferred(con, monkeypatch, capsys):
    """Ноль настоящих отказов при части отложенных — строго код 0 (ТЗ-44C п.4)."""
    for h in ("e1", "e2", "e3"):
        _seed(con, h)
    _insert_request(con)
    fake = FakeSession(fail_on=2, exc=xsession.XSessionWindowCap("окно"))
    monkeypatch.setattr(xsession, "get_session_transport", lambda **kw: fake)
    _no_nitter(monkeypatch)

    rc = cli.cmd_followers(SimpleNamespace(limit=10, handle=None))
    assert rc == 0
