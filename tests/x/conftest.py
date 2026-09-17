"""Общие фикстуры тестов. Сеть не используется: транспорт подменяется."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.x.mocking import FakeNitter, VClock, make_feed  # noqa: E402
from tuber.platforms.x import config, store as db  # noqa: E402


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = str(tmp_path / "tuber_x_test.db")
    monkeypatch.setattr(config, "DB_PATH", p)
    con = db.init_db(p)
    con.close()
    return p


@pytest.fixture
def con(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def vclock():
    return VClock()


@pytest.fixture
def fixture_feed():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "sample_feed.xml"), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def make_broker(db_path):
    """Фабрика брокера с подменённым транспортом и виртуальными часами."""
    from tuber.platforms.x.broker import NitterBroker
    created = []

    def _make(transport=None, instances=None, clock=None, sleeper=None, **kw):
        clock = clock or VClock()
        sleeper = sleeper or (lambda s: clock.advance(s))
        tr = transport or FakeNitter(clock=clock)
        b = NitterBroker(instances=instances or ["https://one.test"],
                         db_path=db_path, transport=tr, clock=clock, sleeper=sleeper,
                         async_mode=False, **kw)
        b._fake_transport = tr
        b._vclock = clock
        created.append(b)
        return b

    yield _make
    for b in created:
        b.close()
