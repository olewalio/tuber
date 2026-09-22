"""ТЗ-8 Р3.1/Р3.4: верификация веб-фида (сеть подменена)."""
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


def test_verify_feed_verdict_feed_and_source(con):
    _eligible_domain(con, "habr.com")
    broker = FakeBroker({
        "https://habr.com/": (200, {}, "<html><head><link rel='alternate' "
                                      "type='application/rss+xml' href='/rss.xml'>"
                                      "</head><body>ok</body></html>"),
        "https://habr.com/rss.xml": (200, {}, RSS),
    })
    res = graph.verify_feed(con, "habr.com", broker=broker)
    assert res["verdict"] == "feed"
    assert res["title"] == "Habr" and res["lang"] == "ru"
    assert res["entries"] == 2
    assert res["source_created"] is True
    row = con.execute("SELECT platform, handle, url, status FROM source WHERE platform='web'"
                      ).fetchone()
    assert (row["platform"], row["handle"]) == ("web", "habr.com")
    # учёт сетевого запроса
    assert graph.feed_verifications_today(con) == 1
    assert con.execute("SELECT kind FROM transport_request").fetchone()[0] == "feed_verify"


def test_verify_feed_html_only(con):
    _eligible_domain(con, "no-feed.example")
    broker = FakeBroker({
        "https://no-feed.example/": (200, {}, "<html><body>" + "x" * 500 + "</body></html>"),
    })
    res = graph.verify_feed(con, "no-feed.example", broker=broker)
    assert res["verdict"] == "html_only"
    assert con.execute("SELECT COUNT(*) FROM source WHERE platform='web'").fetchone()[0] == 0


def test_verify_feed_dead(con):
    _eligible_domain(con, "dead.example")
    broker = FakeBroker({})
    res = graph.verify_feed(con, "dead.example", broker=broker)
    assert res["verdict"] == "dead"


def test_verify_feed_no_source_without_threshold(con):
    # домен есть в рёбрах, но порог не пройден — источник не создаётся
    add_source(con, 1, "telegram", "solo")
    add_content(con, 1, "telegram", 1, "solo/1", links=json.dumps(["https://solo.example/p"]))
    graph.backfill(con, platforms=("telegram",))
    graph.consume(con, platforms=("telegram",))
    broker = FakeBroker({
        "https://solo.example/": (200, {}, "<html><head><link rel='alternate' "
                                          "type='application/rss+xml' href='/rss.xml'>"
                                          "</head></html>"),
        "https://solo.example/rss.xml": (200, {}, RSS),
    })
    res = graph.verify_feed(con, "solo.example", broker=broker)
    assert res["verdict"] == "feed"
    assert res["source_created"] is False


def test_feed_verify_budget(con):
    assert graph.feed_verify_budget(con, limit=2) == 2
    con.execute("INSERT INTO transport_request(platform, host, ts, kind, url, status)"
                " VALUES ('web','a', strftime('%Y-%m-%d %H:%M:%S','now'),"
                " 'feed_verify','http://a/',200)")
    con.commit()
    assert graph.feed_verify_budget(con, limit=2) == 1


# --------------------------------------------------------------------------
# ТЗ-8.1: вердикты blocked/redirect, ложный dead убран (Т2.1-Т2.5)
# --------------------------------------------------------------------------

HTML = "<html><body>" + "x" * 500 + "</body></html>"


def _cand_status_meta(con, domain):
    row = con.execute("SELECT status, meta_json FROM candidate WHERE platform='web'"
                      " AND handle=?", (domain,)).fetchone()
    return row["status"], json.loads(row["meta_json"] or "{}")


def test_blocked_403_keeps_candidate_and_marks_meta(con):
    _eligible_domain(con, "waf.example")
    broker = FakeBroker({"https://waf.example/": (403, {}, "Forbidden")})
    res = graph.verify_feed(con, "waf.example", broker=broker)
    assert res["verdict"] == "blocked"
    assert res["reason"] == "http 403"
    status, meta = _cand_status_meta(con, "waf.example")
    assert status == "new"
    assert meta["blocked"] == 1
    assert meta["blocked_http"] == 403
    assert meta["blocked_at"]
    # источник не заводится
    assert con.execute("SELECT COUNT(*) FROM source WHERE platform='web'").fetchone()[0] == 0
    # Т2.5: 503 — тоже blocked, не dead
    broker2 = FakeBroker({"https://busy.example/": (503, {}, "unavailable")})
    _eligible_domain(con, "busy.example")
    assert graph.verify_feed(con, "busy.example", broker=broker2)["verdict"] == "blocked"


def test_redirect_with_location_follows_to_target(con):
    _eligible_domain(con, "redir.example")
    broker = FakeBroker({
        "https://redir.example/": (302, {"Location": "https://redir.example/home"}, ""),
        "https://redir.example/home": (
            200, {}, "<html><head><link rel='alternate' type='application/rss+xml'"
                     " href='/rss.xml'></head><body>ok</body></html>"),
        "https://redir.example/rss.xml": (200, {}, RSS),
    })
    res = graph.verify_feed(con, "redir.example", broker=broker)
    assert res["verdict"] == "feed"
    assert res["entries"] == 2
    assert res["source_created"] is True
    assert "https://redir.example/home" in broker.calls


def test_redirect_with_location_to_html_only(con):
    _eligible_domain(con, "redir2.example")
    broker = FakeBroker({
        "https://redir2.example/": (301, {"Location": "https://redir2.example/x"}, ""),
        "https://redir2.example/x": (200, {}, HTML),
    })
    res = graph.verify_feed(con, "redir2.example", broker=broker)
    assert res["verdict"] == "html_only"


def test_redirect_without_location_is_redirect_not_dead(con):
    _eligible_domain(con, "nolo.example")
    broker = FakeBroker({"https://nolo.example/": (302, {}, "")})
    res = graph.verify_feed(con, "nolo.example", broker=broker)
    assert res["verdict"] == "redirect"
    assert res["reason"] == "http 302 loop"
    status, meta = _cand_status_meta(con, "nolo.example")
    assert status == "new" and meta["blocked"] == 1 and meta["blocked_http"] == 302


def test_redirect_loop_is_redirect_not_dead(con):
    _eligible_domain(con, "loop.example")
    broker = FakeBroker({
        "https://loop.example/": (302, {"Location": "https://loop.example/a"}, ""),
        "https://loop.example/a": (302, {"Location": "https://loop.example/b"}, ""),
        "https://loop.example/b": (302, {"Location": "https://loop.example/c"}, ""),
        "https://loop.example/c": (302, {"Location": "https://loop.example/"}, ""),
    })
    res = graph.verify_feed(con, "loop.example", broker=broker)
    assert res["verdict"] == "redirect"
    # не больше 3 доп. запросов: главная + 3 хопа
    assert len([u for u in broker.calls if u.startswith("https://loop.example")]) <= 4


def test_transport_error_is_dead(con):
    _eligible_domain(con, "gone.example")

    class DeadBroker:
        def get(self, url, timeout=None):
            return (0, {}, "__transport_error__:DNS")

    res = graph.verify_feed(con, "gone.example", broker=DeadBroker())
    assert res["verdict"] == "dead"


def test_404_and_500_are_dead(con):
    _eligible_domain(con, "notfound.example")
    broker = FakeBroker({"https://notfound.example/": (404, {}, "nope")})
    assert graph.verify_feed(con, "notfound.example", broker=broker)["verdict"] == "dead"
    _eligible_domain(con, "broken.example")
    broker2 = FakeBroker({"https://broken.example/": (500, {}, "boom")})
    assert graph.verify_feed(con, "broken.example", broker=broker2)["verdict"] == "dead"


def test_blocked_candidate_waits_retry_window(con):
    from datetime import datetime, timedelta, timezone
    _eligible_domain(con, "waf.example")
    broker = FakeBroker({"https://waf.example/": (403, {}, "Forbidden")})
    graph.verify_feed(con, "waf.example", broker=broker)
    assert graph.eligible_candidates(con, platforms=("web",)) == []
    later = datetime.now(timezone.utc) + timedelta(days=graph.EDGE_FEED_RETRY_DAYS + 1)
    rows = graph.eligible_candidates(con, platforms=("web",), now=later)
    assert [r["handle"] for r in rows] == ["waf.example"]


def test_report_counts_verdicts_separately(con):
    _eligible_domain(con, "waf.example")
    graph.verify_feed(con, "waf.example",
                      broker=FakeBroker({"https://waf.example/": (403, {}, "no")}))
    r = graph.report(con)
    assert r["feed_verdicts"]["blocked"] == 1
    assert r["feed_verdicts"]["dead"] == 0
    text = graph.format_report(con)
    assert "вердикты веб-фидов" in text
