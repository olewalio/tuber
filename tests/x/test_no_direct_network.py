"""Р7.9 — изоляция транспорта: urlopen только в broker.py (бывший nitter_broker.py)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = os.path.join(ROOT, "tuber", "platforms", "x")

FORBIDDEN = [
    r"\burlopen\b",
    r"urllib\.request\.Request\b",
    r"\brequests\.(get|post|Session)\b",
    r"\bhttp\.client\b",
]


def _sources():
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py"):
            yield name, os.path.join(PKG, name)


def test_no_urlopen_outside_broker():
    hits = []
    for name, path in _sources():
        if name in ("broker.py",):
            continue
        text = open(path, encoding="utf-8").read()
        for pat in FORBIDDEN:
            for m in re.finditer(pat, text):
                line = text[:m.start()].count("\n") + 1
                hits.append(f"{name}:{line}: {pat}")
    assert not hits, "сеть вне брокера: " + "; ".join(hits)


def test_urlopen_exists_in_broker():
    """Брокер — единственная точка выхода в сеть, и она там есть."""
    path = os.path.join(PKG, "broker.py")
    text = open(path, encoding="utf-8").read()
    assert "urlopen" in text
    n = len(re.findall(r"urllib\.request\.urlopen\(", text))
    assert n == 1, f"вызовов urllib.request.urlopen в брокере {n}, ожидался 1"


def test_broker_is_single_entry_point():
    """Коллектор и реестр ходят в сеть только через объект брокера."""
    for name in ("collect.py", "registry.py", "cli.py"):
        text = open(os.path.join(PKG, name), encoding="utf-8").read()
        assert "urlopen" not in text
        assert "import requests" not in text


# ------------------------------------------------- ТЗ-4: покрытие нового канала
TZ4_MODULES = ("channels.py", "enrich.py", "scoring.py", "health.py")


def test_new_channel_modules_have_no_direct_network():
    """Роутер каналов и его потребители не ходят в сеть сами (ТЗ-4 2.1)."""
    hits = []
    for name in TZ4_MODULES:
        text = open(os.path.join(PKG, name), encoding="utf-8").read()
        for pat in FORBIDDEN:
            for m in re.finditer(pat, text):
                line = text[:m.start()].count("\n") + 1
                hits.append(f"{name}:{line}: {pat}")
    assert not hits, "сеть вне транспортных модулей: " + "; ".join(hits)


def test_collector_scoring_cli_use_channel_transport():
    """Коллектор, скоринг и CLI получают сеть только через каналы/транспорт."""
    for name in ("collect.py", "enrich.py", "scoring.py", "health.py", "cli.py"):
        text = open(os.path.join(PKG, name), encoding="utf-8").read()
        for pat in ("socket", "urlopen", "http.client", "import requests"):
            assert pat not in text, f"{name} содержит прямой сетевой вызов: {pat}"


def test_channel_router_reuses_single_transport():
    """Транспорт один на все каналы: каналы вызывают raw_http_get брокера."""
    text = open(os.path.join(PKG, "channels.py"), encoding="utf-8").read()
    assert "raw_http_get" in text
    from tuber.platforms.x import channels
    assert callable(channels.timeline_snapshot)
    assert hasattr(channels, "ChannelRouter")
    for name in ("CdnTweetBroker", "SyndTimelineBroker", "XssrBroker"):
        assert hasattr(channels, name)


def test_only_broker_and_channels_are_network_modules():
    """Единственный сырой транспорт — broker.py (ТЗ-4 инвариант)."""
    for name, path in _sources():
        if name in ("broker.py",):
            continue
        text = open(path, encoding="utf-8").read()
        assert "urllib.request" not in text or name == "channels.py", name
