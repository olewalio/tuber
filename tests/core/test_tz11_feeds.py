"""ТЗ-11, волна 3 (W3-3/D-51): кандидат-URL фида проходит проход редиректов.

Сеть подменена: используется подставной брокер, повторяющий поведение
существующих тестов ``tests/graph/test_verify_feed.py``.
"""

from __future__ import annotations

import json

import pytest

from tuber.core import db, graph, schema
from tests.graph.conftest import add_content, add_source

HTML = "<html><body>" + "x" * 500 + "</body></html>"

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>a</title><updated>2026-09-01T10:00:00Z</updated></entry>
<entry><title>b</title><updated>2026-09-02T10:00:00Z</updated></entry>
<entry><title>c</title><updated>2026-09-03T10:00:00Z</updated></entry>
</feed>"""

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>RSS</title><language>ru</language>
<item><title>a</title><pubDate>Mon, 01 Sep 2026 10:00:00 +0000</pubDate></item>
<item><title>b</title><pubDate>Tue, 02 Sep 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""


@pytest.fixture()
def con(tmp_path):
    conn = db.connect(str(tmp_path / "feeds.db"))
    schema.init_schema(conn)
    yield conn
    conn.close()


class FakeBroker:
    """Отдаёт страницы по префиксу URL; ключи длиннее проверяются первыми."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        for key in sorted(self.pages, key=len, reverse=True):
            if url.startswith(key):
                return self.pages[key]
        return (404, {}, "")


def _eligible_domain(con, domain):
    base = con.execute("SELECT COALESCE(MAX(id), 0) FROM source").fetchone()[0]
    sids = (base + 1, base + 2)
    add_source(con, sids[0], "telegram", f"a{base}")
    add_source(con, sids[1], "telegram", f"b{base}")
    for sid in sids:
        add_content(con, sid, "telegram", sid, f"x/{sid}",
                    links=json.dumps([f"https://{domain}/p"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con, platforms=("telegram",))


def test_w33_feed_candidate_redirect_is_followed(con):
    """Кандидат /feed отвечает 301 → финальный Atom; вердикт feed, feed_url конечный."""
    _eligible_domain(con, "redirfeed.example")
    final_url = "https://redirfeed.example/feed/atom"
    broker = FakeBroker({
        "https://redirfeed.example/": (200, {}, HTML),
        "https://redirfeed.example/feed": (
            301, {"Location": "/feed/atom"}, "Moved"),
        "https://redirfeed.example/feed/atom": (200, {}, ATOM),
    })
    res = graph.verify_feed(con, "redirfeed.example", broker=broker)
    assert res["verdict"] == "feed"
    assert res["entries"] == 3
    assert res["feed_url"] == final_url
    assert final_url in broker.calls
    # Источник platform='web' ссылается на фактический (конечный) фид.
    row = con.execute("SELECT url FROM source WHERE platform='web'").fetchone()
    assert row is not None and row["url"] == final_url


def test_w33_unresolved_candidate_redirect_falls_through(con):
    """Неразрешённый редирект кандидата — переходим к следующему кандидату."""
    _eligible_domain(con, "candloop.example")
    broker = FakeBroker({
        "https://candloop.example/": (200, {}, HTML),
        # /feed — редирект без Location (Р1.2 unresolved): не фид.
        "https://candloop.example/feed": (302, {}, ""),
        # /rss — рабочий фид.
        "https://candloop.example/rss": (200, {}, RSS),
    })
    res = graph.verify_feed(con, "candloop.example", broker=broker)
    assert res["verdict"] == "feed"
    assert res["entries"] == 2
    assert res["feed_url"] == "https://candloop.example/rss"


def test_w33_candidate_redirect_loop_is_skipped(con):
    """Петля редиректов на кандидате не выдаёт feed и не подменяет главную."""
    _eligible_domain(con, "candloop2.example")
    broker = FakeBroker({
        "https://candloop2.example/": (200, {}, HTML),
        "https://candloop2.example/feed": (
            302, {"Location": "/feed/again"}, ""),
        "https://candloop2.example/feed/again": (
            302, {"Location": "/feed"}, ""),
    })
    res = graph.verify_feed(con, "candloop2.example", broker=broker)
    assert res["verdict"] == "html_only"


def test_w33_main_page_verdicts_unchanged(con):
    """Поведение главной (Р1.1/Р1.2/Р1.3) не меняется."""
    _eligible_domain(con, "main.example")
    blocked = FakeBroker({"https://main.example/": (403, {}, "Forbidden")})
    assert graph.verify_feed(con, "main.example", broker=blocked)["verdict"] == "blocked"

    _eligible_domain(con, "mainloop.example")
    loop = FakeBroker({"https://mainloop.example/": (302, {}, "")})
    assert graph.verify_feed(con, "mainloop.example", broker=loop)["verdict"] == "redirect"
