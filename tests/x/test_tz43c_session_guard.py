"""ТЗ-43C: предохранители сессии X — cooldown и бюджет живут в базе.

Все внешние ответы подменяются; живого интернета нет. Проверяются обе дыры
ТЗ: cooldown переживает новый процесс, а бюджет запросов считается по журналу
``transport_request`` (общий для всех процессов-кронов).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.x.mocking import FakeNitter, VClock
from tests.x.test_session_transport import mk_session  # стенд ТЗ-43A
from tuber.platforms.x import config, store as db
from tuber.platforms.x import session as xsession
from tuber.platforms.x.broker import NitterBroker


def _status_transport(status, body=""):
    """Подменяемый транспорт с фиксированным HTTP-статусом и журналом вызовов."""
    calls = []

    def transport(url, headers):
        calls.append(url)
        return status, {}, body

    transport.calls = calls
    return transport


def _insert_requests(con, count, *, step_sec, kind="x_session", host="x.com"):
    """Записать в журнал ``count`` запросов назад от «сейчас» с шагом ``step_sec``."""
    now = datetime.now(timezone.utc)
    for i in range(count):
        ts = (now - timedelta(seconds=i * step_sec)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute(
            "INSERT INTO main.transport_request"
            " (platform, host, ts, kind, url, status, items, latency_ms, run_id)"
            " VALUES ('x', ?, ?, ?, 'url', 200, 0, 0, NULL)", (host, ts, kind))
    con.commit()


# ------------------------------------------------------------------ 1) cooldown
def test_429_cooldown_visible_to_new_transport(con, tmp_path):
    """Дыра 1: cooldown из памяти не теряется — его видит НОВЫЙ экземпляр."""
    s1 = mk_session(con, tmp_path, _status_transport(429))
    with pytest.raises(xsession.XSessionRateLimited):
        s1.profile("nasa")
    st = db.read_session_state(con)
    assert st["cooldown_until"] is not None
    assert st["consecutive_429"] == 1

    second = _status_transport(200)
    s2 = mk_session(con, tmp_path, second)
    with pytest.raises(xsession.XSessionRateLimited):
        s2.profile("nasa")
    assert second.calls == [], "новый экземпляр в cooldown не должен идти в сеть"


# ---------------------------------------------------------------- 2) эскалация
def test_two_429_escalate_to_hard_cooldown(con, tmp_path):
    """Два 429 подряд (за сутки) → жёсткий cooldown 3600 с, а не 900."""
    s1 = mk_session(con, tmp_path, _status_transport(429))
    with pytest.raises(xsession.XSessionRateLimited):
        s1.profile("nasa")

    # «Отпускаем» мягкий cooldown, чтобы второй 429 реально дошёл до сети.
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.write_session_state(con, cooldown_until=past)

    s2 = mk_session(con, tmp_path, _status_transport(429))
    with pytest.raises(xsession.XSessionRateLimited):
        s2.profile("nasa")

    st = db.read_session_state(con)
    assert st["consecutive_429"] == 2
    left = (st["cooldown_until"] - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < left <= 3600, f"ожидался жёсткий cooldown 3600, осталось {left}"


# ------------------------------------------------------------------ 3) окно
def test_window_budget_blocks_51st_request(con, tmp_path, monkeypatch):
    """50 записей в окне 900 с → 51-й запрос не уходит."""
    monkeypatch.setattr(config, "X_SESSION_WINDOW_CAP", 50)
    monkeypatch.setattr(config, "X_SESSION_WINDOW_SEC", 900)
    _insert_requests(con, 50, step_sec=10)      # все внутри последних 500 с

    transport = _status_transport(200)
    s = mk_session(con, tmp_path, transport)
    with pytest.raises(xsession.XSessionRateLimited) as ei:
        s.profile("nasa")
    assert "окно" in str(ei.value) and "50" in str(ei.value)
    assert transport.calls == [], "при исчерпанном окне сеть не трогается"

    budget = db.session_budget(con)
    assert budget["window_count"] == 50 and budget["window_exhausted"]
    # Факт отказа попал в run_log.
    msgs = [r["msg"] for r in con.execute("SELECT msg FROM main.run_log")]
    assert any("бюджет окна" in m for m in msgs), msgs


# ------------------------------------------------------------------ 4) сутки
def test_daily_budget_blocks_and_old_rows_ignored(con, tmp_path, monkeypatch):
    """800 записей за 24 ч → следующий не уходит; старше 24 ч не в счёт."""
    monkeypatch.setattr(config, "X_SESSION_DAILY_CAP", 800)
    monkeypatch.setattr(config, "X_SESSION_WINDOW_CAP", 10_000)
    _insert_requests(con, 800, step_sec=60)     # растянуты на ~13 ч
    _insert_requests(con, 5, step_sec=3600)     # но это тоже в пределах суток
    # 7 записей СТАРШЕ 24 ч — в суточный счёт входить не должны.
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    for i in range(7):
        ts = (old - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute(
            "INSERT INTO main.transport_request"
            " (platform, host, ts, kind, url, status) VALUES ('x','x.com',?,"
            "'x_session','url',200)", (ts,))
    con.commit()

    budget = db.session_budget(con)
    assert budget["daily_count"] == 805, f"старые записи учлись: {budget['daily_count']}"
    assert budget["daily_exhausted"]

    transport = _status_transport(200)
    s = mk_session(con, tmp_path, transport)
    with pytest.raises(xsession.XSessionRateLimited) as ei:
        s.profile("nasa")
    assert "суточн" in str(ei.value)
    assert transport.calls == []


# ------------------------------------------------- 5) ленты через сессию = флаг
def test_feeds_flag_controls_session_feed(con, tmp_path, monkeypatch):
    """X_SESSION_FEDS_ENABLED=0 → лента мимо сессии; =1 → сессия вызывается."""
    monkeypatch.setattr(config, "X_SESSION_ENABLED", True)

    class FakeSession:
        def __init__(self):
            self.calls = []

        def fetch_feed(self, handle, **kw):
            self.calls.append(handle)
            return None

    def make(session):
        return NitterBroker(instances=["https://one.test"], db_path=config.DB_PATH,
                            transport=FakeNitter(clock=VClock()), clock=VClock(),
                            sleeper=lambda s: None, async_mode=False, session=session)

    monkeypatch.setattr(config, "X_SESSION_FEDS_ENABLED", False)
    off = FakeSession()
    b_off = make(off)
    try:
        b_off.fetch_feed("nasa")
    finally:
        b_off.close()
    assert off.calls == [], "при выключенных лентах сессия не вызывается"

    monkeypatch.setattr(config, "X_SESSION_FEDS_ENABLED", True)
    on = FakeSession()
    b_on = make(on)
    try:
        b_on.fetch_feed("nasa")
    finally:
        b_on.close()
    assert on.calls == ["nasa"], "при включённых лентах сессия вызывается первой"


# -------------------------------------------------------------------- 6) 403
def test_403_cooldown_survives_to_new_transport(con, tmp_path):
    """403 → cooldown 24 ч, его видит новый экземпляр транспорта."""
    s1 = mk_session(con, tmp_path, _status_transport(403))
    with pytest.raises(xsession.SessionBlocked):
        s1.profile("nasa")

    st = db.read_session_state(con)
    assert st["blocked"] is True
    left = (st["cooldown_until"] - datetime.now(timezone.utc)).total_seconds()
    assert 86000 < left <= 86400, f"ожидался cooldown 24 ч, осталось {left}"

    second = _status_transport(200)
    s2 = mk_session(con, tmp_path, second)
    with pytest.raises(xsession.SessionBlocked):
        s2.profile("nasa")
    assert second.calls == [], "новый экземпляр в блокировке не должен идти в сеть"


# ------------------------------------------- 7) синглтон читает cooldown из БД
def test_constructor_reads_cooldown_from_db(con, tmp_path):
    """Cooldown, записанный одним «процессом», конструктор нового видит сразу."""
    s1 = mk_session(con, tmp_path, _status_transport(429))
    with pytest.raises(xsession.XSessionRateLimited):
        s1.profile("nasa")

    fresh = mk_session(con, tmp_path, _status_transport(200))
    assert fresh._state["cooldown_until"] is not None
    assert fresh._state["consecutive_429"] == 1


# ------------------------------- 8) метрики постов через сессию (точ. путь)
def test_point_metrics_hydration_via_session(monkeypatch):
    """Метрики существующих постов дотягиваются точечно (ТЗ-43C, п.5)."""
    from tuber.platforms.x import collect as xcollect
    from tuber.platforms.x import session as xsess

    class FakeSession:
        def __init__(self):
            self.ids = []

        def tweets_by_ids(self, ids):
            self.ids.append(list(ids))
            return [{"id": "111", "views": 777, "likes": 5, "replies": 2,
                     "retweets": 1, "quotes": 0}]

    fake = FakeSession()
    monkeypatch.setattr(config, "X_SESSION_ENABLED", True)
    monkeypatch.setattr(xsess, "get_session_transport", lambda **kw: fake)

    posts = [{"tweet_id": "111", "text": "nitter"},
             {"tweet_id": "222", "text": "nitter2"}]
    xcollect.enrich_session_metrics(posts)
    assert fake.ids == [["111", "222"]]
    assert posts[0]["views"] == 777 and posts[0]["reposts"] == 1
    assert posts[1].get("views") is None, "нет данных — поля не подделываем"

    # Посты, у которых метрики уже есть (лента шла через сессию), не трогаем.
    fake.ids.clear()
    xcollect.enrich_session_metrics([{"tweet_id": "111", "views": 1, "likes": 1,
                                      "reposts": 1, "quotes": 1}])
    assert fake.ids == []

    # Мастер-выключатель транспорта гасит гидратацию.
    monkeypatch.setattr(config, "X_SESSION_ENABLED", False)
    fake.ids.clear()
    xcollect.enrich_session_metrics([{"tweet_id": "333", "text": "x"}])
    assert fake.ids == []

