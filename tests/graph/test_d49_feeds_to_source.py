"""ТЗ-B6 D-49: `graph verify-feeds` доводит фид до записи `source`.

Разбор очереди (`tuber/analysis/queue.py::review_web`) отвергает ВСЕ веб-кандидаты
лишь потому, что «нет сборщика постов», выставляя ``validated='reject'``. Старый
селектор ``graph.eligible_candidates`` отсекал такие строки
(``validated NOT IN ('ok','reject')``), поэтому `verify-feeds` не видел ни одного
фида и ``source`` не появлялся (на копии боевой базы 23.09.2026 — 0 источников из
918 прошедших порог). Новый селектор :func:`graph.feed_verify_queue` это исправляет.
"""
from __future__ import annotations

import json

from tuber.core import graph

from tests.graph.conftest import add_content, add_source

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Habr</title><language>ru</language>
<item><title>a</title><pubDate>Mon, 01 Sep 2026 10:00:00 +0000</pubDate></item>
<item><title>b</title><pubDate>Tue, 02 Sep 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""


class FakeBroker:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        for key in sorted(self.pages, key=len, reverse=True):
            if url.startswith(key):
                return self.pages[key]
        return (404, {}, "")


def _feed_broker(domain):
    return FakeBroker({
        f"https://{domain}/": (200, {}, "<html><head><link rel='alternate' "
                                         "type='application/rss+xml' href='/rss.xml'>"
                                         "</head><body>ok</body></html>"),
        f"https://{domain}/rss.xml": (200, {}, RSS),
    })


def _eligible_domain(con, domain):
    """Домен с порогом входа: 2 независимых источника ссылаются на него."""
    base = con.execute("SELECT COALESCE(MAX(id), 0) FROM source").fetchone()[0]
    sids = (base + 1, base + 2)
    add_source(con, sids[0], "telegram", f"a{base}")
    add_source(con, sids[1], "telegram", f"b{base}")
    for sid in sids:
        add_content(con, sid, "telegram", sid, f"x/{sid}",
                    links=json.dumps([f"https://{domain}/p"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con, platforms=("telegram",))


def _reject_by_queue(con, domain):
    """Имитировать вердикт очереди `review_web`: reject с validated='reject'."""
    con.execute(
        "UPDATE candidate SET status='rejected', validated='reject',"
        " reject_reason='веб-фид: нет сборщика постов (D-49)'"
        " WHERE platform='web' AND handle=?", (domain,))
    con.commit()


def test_feed_verify_queue_includes_queue_rejected(con):
    _eligible_domain(con, "habr.com")
    _reject_by_queue(con, "habr.com")
    # старый селектор не видит кандидата (регресс D-49), новый — видит
    assert graph.eligible_candidates(con, platforms=("web",)) == []
    rows = graph.feed_verify_queue(con)
    assert [r["handle"] for r in rows] == ["habr.com"]


def test_verify_feed_creates_source_after_queue_reject_and_is_idempotent(con):
    _eligible_domain(con, "habr.com")
    _reject_by_queue(con, "habr.com")

    row = graph.feed_verify_queue(con)[0]
    res = graph.verify_feed(con, row["handle"], broker=_feed_broker("habr.com"))
    assert res["verdict"] == "feed"
    assert res["source_created"] is True
    src = con.execute("SELECT platform, handle, url FROM source WHERE platform='web'")
    src = src.fetchone()
    assert (src["platform"], src["handle"]) == ("web", "habr.com")
    assert src["url"].endswith("/rss.xml")

    # повторный прогон: кандидат уже подключён — в очередь не попадает и дубль не растёт
    assert graph.feed_verify_queue(con) == []
    graph.verify_feed(con, "habr.com", broker=_feed_broker("habr.com"))
    assert con.execute(
        "SELECT COUNT(*) FROM source WHERE platform='web' AND handle='habr.com'"
    ).fetchone()[0] == 1


def test_verify_feed_links_feed_url_to_existing_source(con):
    """Если источник уже есть без url — найденный адрес фида привязывается (D-49)."""
    _eligible_domain(con, "habr.com")
    con.execute("INSERT INTO source(platform, handle, url, status) "
                "VALUES ('web','habr.com', NULL, 'active')")
    con.commit()
    res = graph.verify_feed(con, "habr.com", broker=_feed_broker("habr.com"))
    assert res["verdict"] == "feed"
    assert res["source_created"] is False  # строка уже была
    row = con.execute("SELECT url FROM source WHERE platform='web' AND handle='habr.com'")
    assert row.fetchone()["url"].endswith("/rss.xml")
