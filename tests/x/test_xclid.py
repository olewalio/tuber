"""ТЗ A1: перенос генератора ``x-client-transaction-id`` — тесты без сети.

Проверяем: (1) математику переноса против зафиксированных эталонов twscrape
(эталоны посчитаны ОДИН РАЗ offline, сам twscrape в тестах не импортируется),
(2) ``xclid.create(fetch)`` на локальных фикстурах вёрстки, (3) кэш файла
генератора, (4) страховку: сломанный генератор не добавляет заголовок и не
роняет запрос.
"""
from __future__ import annotations

import base64
import json
import os
import time

from tests.x.test_session_transport import (
    FakeX, mk_session, profile_payload)
from tuber.platforms.x import xclid

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures")
PAGE_URL = "https://x.com/tesla"
MAIN_URL = "https://abs.twimg.com/responsive-web/client-web/main.abc.js"
SIGN_URL = "https://abs.twimg.com/responsive-web/client-web/sign.o-abc.js"


# ------------------------------------------------------------------ помощники
def _fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def _fixture_fetch(counter=None):
    """fetch по локальным фикстурам вёрстки X (сети нет)."""
    routes = {PAGE_URL: "xclid_page.html",
              MAIN_URL: "xclid_main.js",
              SIGN_URL: "xclid_indices.js"}

    def fetch(url):
        if counter is not None:
            counter.append(url)
        if url not in routes:
            raise KeyError(url)
        return _fixture(routes[url])

    return fetch


def _seed_xclid_file(path, gen):
    vk_bytes, anim_key = gen.keys()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": int(time.time()),
                   "vk_bytes": list(vk_bytes), "anim_key": anim_key}, fh)
    os.chmod(path, 0o600)
    return path


# ------------------------------------------------------- эквивалентность переноса
def test_calc_matches_twscrape_cases(monkeypatch):
    with open(os.path.join(FIX, "xclid_cases.json"), encoding="utf-8") as fh:
        cases = json.load(fh)
    assert cases, "нет эталонных случаев xclid_cases.json"
    for c in cases:
        gen = xclid.XClIdGen(list(c["vk_bytes"]), c["anim_key"])
        monkeypatch.setattr(xclid.time, "time", lambda t=c["time"]: t)
        monkeypatch.setattr(xclid.random, "randint",
                            lambda a, b, r=c["randint"]: r)
        got = gen.calc(c["method"], c["path"])
        assert got == c["expected"], f"расхождение с twscrape: {c} -> {got}"


# ---------------------------------------------------------- create() по фикстурам
def test_create_from_local_fixtures():
    seen = []
    gen = xclid.create(_fixture_fetch(seen))
    assert gen.vk_bytes and isinstance(gen.anim_key, str) and gen.anim_key
    out = gen.calc("GET", "/i/api/graphql/abc/SearchTimeline")
    assert out and "=" not in out
    raw = base64.b64decode(out + "=" * (-len(out) % 4), validate=True)
    assert len(raw) == len(gen.vk_bytes) + 4 + 16 + 1 + 1
    # Скачаны ровно страница, бандл и файл индексов; порядок детерминирован.
    assert seen == [PAGE_URL, MAIN_URL, SIGN_URL], seen
    assert gen.anim_key == "12310170a3d70a3d70a0170a3d70a3d70a100"


# -------------------------------------------------------------------- кэш
def test_second_xclid_call_does_not_refetch(con, tmp_path, monkeypatch):
    calls = []
    real_gen = xclid.create(_fixture_fetch())

    def counting_create(fetch, **kw):
        calls.append(1)
        return real_gen

    monkeypatch.setattr(xclid, "create", counting_create)
    s = mk_session(con, tmp_path, FakeX(),
                   xclid_file=str(tmp_path / "xclid.json"))
    g1 = s._xclid()
    g2 = s._xclid()
    assert g1 is not None and g1 is g2
    assert len(calls) == 1, "второй вызов _xclid() не должен пересоздавать генератор"
    assert os.path.exists(s.xclid_file), "генератор должен лечь в файл кэша"
    # Данные из файла восстанавливаются в новый объект, create НЕ дёргается.
    s2 = mk_session(con, tmp_path, FakeX(), xclid_file=s.xclid_file)
    g3 = s2._xclid()
    assert g3 is not None and g3.keys() == g1.keys()
    assert len(calls) == 1, "свежий файл кэша не должен дёргать create"


# ------------------------------------------------------ страховка: нет заголовка
def test_broken_generator_sends_no_header_and_survives(con, tmp_path, monkeypatch):
    def boom(fetch, **kw):
        raise xclid.XClIdParseError("сломанная вёрстка")

    monkeypatch.setattr(xclid, "create", boom)
    routes = {"UserByScreenName": profile_payload("nasa", followers=5)}
    fake = FakeX(routes=routes)
    s = mk_session(con, tmp_path, fake, xclid_file=str(tmp_path / "none.json"))
    prof = s.profile("nasa")           # не должно упасть
    assert prof["followers"] == 5
    gql = [(u, h) for u, h in fake.header_calls if "/UserByScreenName" in u]
    assert gql, "запрос GraphQL не ушёл"
    assert all("x-client-transaction-id" not in h for _, h in gql), \
        "при сломанном генераторе заголовка быть не должно"
    assert s._xclid_failed is True


# ------------------------------------------------- заголовок при живом генераторе
def test_header_present_with_cached_generator(con, tmp_path, monkeypatch):
    monkeypatch.setattr(xclid.time, "time", lambda: 1750000000.25)
    monkeypatch.setattr(xclid.random, "randint", lambda a, b: 77)
    gen = xclid.create(_fixture_fetch())
    path = _seed_xclid_file(str(tmp_path / "xclid.json"), gen)
    fake = FakeX(routes={"UserByScreenName": profile_payload("nasa", followers=9)})
    s = mk_session(con, tmp_path, fake, xclid_file=path)
    assert s.profile("nasa")["followers"] == 9
    gql = [(u, h) for u, h in fake.header_calls if "/UserByScreenName" in u]
    assert gql
    url, headers = gql[-1]
    assert "x-client-transaction-id" in headers
    import urllib.parse
    expected = gen.calc("GET", urllib.parse.urlparse(url).path or "/")
    assert headers["x-client-transaction-id"] == expected


# ------------------------------------------------------------- сброс кэша (404)
def test_reset_xclid_clears_memory_and_file(con, tmp_path):
    gen = xclid.create(_fixture_fetch())
    path = _seed_xclid_file(str(tmp_path / "xclid.json"), gen)
    s = mk_session(con, tmp_path, FakeX(), xclid_file=path)
    assert s._xclid() is not None
    s._reset_xclid()
    assert s._xclid_cache is None
    assert not os.path.exists(path)


class _XclidAwareX(FakeX):
    """FakeX + отдача фикстур вёрстки X для пересоздания генератора на 404."""

    def __call__(self, url, headers):
        served = {
            PAGE_URL: "xclid_page.html",
            MAIN_URL: "xclid_main.js",
            SIGN_URL: "xclid_indices.js",
        }
        if url in served:
            self.calls.append((url, self.clock() if self.clock else 0.0))
            self.header_calls.append((url, headers))
            return 200, {}, _fixture(served[url])
        return super().__call__(url, headers)


def test_gql_404_recreates_generator_and_adds_header(con, tmp_path, monkeypatch):
    monkeypatch.setattr(xclid.time, "time", lambda: 1750000000.25)
    monkeypatch.setattr(xclid.random, "randint", lambda a, b: 77)
    gen = xclid.create(_fixture_fetch())
    path = _seed_xclid_file(str(tmp_path / "xclid.json"), gen)
    home = ('<html>main.fresh.js '
            'https://abs.twimg.com/responsive-web/client-web/main.fresh.js</html>')
    bundle = ('{queryId:"QID_fresh_byScreenName_01",operationName:"UserByScreenName"}'
              '["view_counts_everywhere_api_enabled"]')
    fake = _XclidAwareX(home_html=home, bundle_js=bundle,
                        routes={"UserByScreenName": profile_payload("nasa", followers=13)},
                        fail_ops=("UserByScreenName",))
    s = mk_session(con, tmp_path, fake, xclid_file=path)
    assert s.profile("nasa")["followers"] == 13
    assert fake.gql_hits["UserByScreenName"] == 2, "после 404 ровно один повтор"
    gql = [(u, h) for u, h in fake.header_calls if "/UserByScreenName" in u]
    assert len(gql) == 2
    assert all("x-client-transaction-id" in h for _, h in gql), \
        "после пересоздания генератора заголовок обязан быть на обоих запросах"
    # Файл кэша сброшен и записан заново пересозданным генератором.
    assert os.path.exists(path)
    tesla_fetches = [u for u, _ in fake.calls if u == PAGE_URL]
    assert len(tesla_fetches) == 1, "пересоздание генератора = один поход за страницей"
