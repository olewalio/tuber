"""ТЗ-48: новинки — сопоставление с внешним контуром и два разреза.

Проверяем на маленькой базе с подготовленным ``external_result`` (сеть не
зовётся):

* новинка проходит только при внешнем подтверждении и объединённом счёте
  ``≥ 3`` источников на ``≥ 2`` платформах;
* строгий разрез требует ещё и внутренней новизны, ``≥ 3`` внутренних
  источников и ``≥ 2`` внутренних платформ;
* ``Show HN`` даёт имя продукта из заголовка, а не первое слово предложения;
* отказ внешнего источника честно печатается, а не подменяется заглушкой.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.analysis import names, novelties
from tuber.core import db, schema

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _make(tmp_path):
    con = db.connect(str(tmp_path / "nov.db"))
    schema.init_schema(con)
    for sid, platform in [(1, "telegram"), (2, "x"), (3, "telegram")]:
        con.execute("INSERT INTO source(id, platform, handle, status, subs)"
                    " VALUES (?,?,?,?,?)", (sid, platform, f"ch{sid}", "active", 1000))
    return con


def _post(con, cid, sid, platform, published, text):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at, text, url)"
        " VALUES (?,?,?,?,?,?,?)",
        (cid, platform, sid, f"e{cid}", published.strftime("%Y-%m-%d %H:%M:%S"),
         text, f"https://example.com/{cid}"))


def _github(url="https://github.com/zai-org/ZCode", title="zai-org/ZCode"):
    return {"platform": "github", "title": title, "name_text": title, "url": url,
            "score": 5000, "comments": None, "published_at": "2026-09-20 10:00:00"}


def _external(items, *, sources=None):
    return {"now": "2026-09-21 12:00:00", "window_hours": 48, "items": items,
            "sources": sources or {
                "github": {"platform": "github", "title": "GitHub", "status": "ok",
                           "count": len(items), "error": None}}}


def test_external_tier_requires_spread_and_confirmation(tmp_path):
    con = _make(tmp_path)
    try:
        _post(con, 1, 1, "telegram", NOW - timedelta(hours=2), "ZCode released today")
        _post(con, 2, 2, "x", NOW - timedelta(hours=1), "ZCode is here")
        con.commit()
        data = novelties.build(con, now=NOW, external_result=_external([_github()]))
        assert data["strict"] == []
        ext = next(r for r in data["external"] if r["entity"] == "zcode")
        assert ext["internal_sources"] == 2
        assert ext["external_sources"] == 1
        assert ext["total_sources"] == 3
        assert set(ext["platforms"]) == {"telegram", "x", "github"}
    finally:
        con.close()


def test_strict_tier_needs_three_internal_sources(tmp_path):
    con = _make(tmp_path)
    try:
        _post(con, 1, 1, "telegram", NOW - timedelta(hours=3), "ZCode news")
        _post(con, 2, 3, "telegram", NOW - timedelta(hours=2), "ZCode review")
        _post(con, 3, 2, "x", NOW - timedelta(hours=1), "ZCode launch")
        con.commit()
        data = novelties.build(con, now=NOW, external_result=_external([_github()]))
        strict = next(r for r in data["strict"] if r["entity"] == "zcode")
        assert strict["tier"] == "strict"
        assert strict["new_internal"] is True
        assert strict["internal_sources"] == 3
        assert set(strict["internal_platforms"]) == {"telegram", "x"}
    finally:
        con.close()


def test_no_external_means_no_novelty(tmp_path):
    con = _make(tmp_path)
    try:
        _post(con, 1, 1, "telegram", NOW - timedelta(hours=2), "ZCode released")
        _post(con, 2, 2, "x", NOW - timedelta(hours=1), "ZCode here")
        con.commit()
        data = novelties.build(con, now=NOW, external_result=_external([]))
        assert data["external"] == [] and data["strict"] == []
    finally:
        con.close()


def test_known_entity_without_release_is_not_novelty(tmp_path):
    """Сущность была в базе ДО окна и внешняя запись — не выпуск -> не новинка."""
    con = _make(tmp_path)
    try:
        # след в истории до окна
        _post(con, 99, 1, "telegram", NOW - timedelta(days=5), "ZCode early mention")
        _post(con, 1, 1, "telegram", NOW - timedelta(hours=2), "ZCode again")
        _post(con, 2, 2, "x", NOW - timedelta(hours=1), "ZCode again")
        con.commit()
        news = {"platform": "hackernews", "title": "ZCode review", "name_text": "ZCode review",
                "url": "https://news.ycombinator.com/item?id=1", "score": 30,
                "comments": 1, "published_at": "2026-09-21 09:00:00"}
        data = novelties.build(con, now=NOW, external_result=_external([news]))
        assert all(r["entity"] != "zcode" for r in data["external"])
    finally:
        con.close()


def test_show_hn_name_extraction():
    from tuber.analysis.external import _hn_name_text
    assert _hn_name_text("Show HN: Mini-AGI – Dynamic continual learning") == "Mini-AGI"
    assert _hn_name_text("Show HN: Radius – A Meetup.com Alternative") == "Radius"
    assert _hn_name_text("Ordinary story about ships") == "Ordinary story about ships"


def test_distinctive_names_skip_sentence_words():
    hn = names.distinctive_names("Dynamic continual learning model", capitalized="none")
    assert "dynamic" not in hn
    show = names.distinctive_names("Radius", capitalized="first")
    assert "radius" in show
    camel = names.distinctive_names("zai-org/ZCode")
    assert "zcode" in camel


def test_is_release_item():
    assert novelties.is_release_item({"platform": "github", "title": "x"})
    assert novelties.is_release_item({"platform": "hackernews", "title": "Show HN: X"})
    assert not novelties.is_release_item({"platform": "hackernews", "title": "The Claude Delusion"})


def test_report_prints_source_failure(tmp_path):
    con = _make(tmp_path)
    try:
        con.commit()
        data = novelties.build(con, now=NOW, external_result=_external(
            [], sources={"arxiv": {"platform": "arxiv", "title": "arXiv",
                                   "status": "http_error", "count": 0, "error": "HTTP 406"}}))
        text = novelties.format_report(data, limit=5)
        assert "НЕ ОТВЕТИЛ" in text
        assert "HTTP 406" in text
    finally:
        con.close()
