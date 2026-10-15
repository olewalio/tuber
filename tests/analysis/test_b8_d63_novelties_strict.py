"""D-63: строгий разрез новинок пересчитан и НЕ пуст на нашем корпусе.

Строгий разрез теперь — плотность ТОЛЬКО по внутреннему корпусу:
``new_internal`` И ≥ 3 внутренних источника И ≥ 2 внутренние платформы;
внешнее подтверждение для него больше не требуется (печатается отдельным
полем ``external_confirmed``). Пересчёт 23.09.2026: на боевом корпусе
(окно 48 ч, 3 внутренние платформы Telegram/X/YouTube) разрез дал 44 сущности.

Здесь проверяем офлайн (сеть не зовётся): на мини-корпусе с новым именем на
двух внутренних платформах без внешнего результата строгий разрез непуст, а
имя на одной платформе в строгий разрез не попадает.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.analysis import novelties
from tuber.core import db, schema

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _make(tmp_path):
    con = db.connect(str(tmp_path / "nov_b8.db"))
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


def _empty_external():
    return {"now": "2026-09-23 12:00:00", "window_hours": 48, "items": [], "sources": {}}


def test_strict_tier_nonempty_without_external_on_two_internal_platforms(tmp_path):
    con = _make(tmp_path)
    try:
        # новое имя ZCode: 3 внутренних источника на 2 внутренних платформах
        _post(con, 1, 1, "telegram", NOW - timedelta(hours=3), "ZCode released today")
        _post(con, 2, 3, "telegram", NOW - timedelta(hours=2), "ZCode is a new tool")
        _post(con, 3, 2, "x", NOW - timedelta(hours=1), "ZCode launched on X")
        con.commit()
        data = novelties.build(con, now=NOW, external_result=_empty_external())
        strict = next((r for r in data["strict"] if r["entity"] == "zcode"), None)
        assert strict is not None, "строгий разрез пуст при плотности по внутреннему корпусу"
        assert strict["tier"] == "strict"
        assert strict["new_internal"] is True
        assert strict["internal_sources"] == 3
        assert set(strict["internal_platforms"]) == {"telegram", "x"}
        assert strict["external_confirmed"] is False
        # внешнее подтверждение не требуется, поэтому внешний разрез пуст
        assert data["external"] == []
    finally:
        con.close()


def test_strict_tier_requires_two_internal_platforms(tmp_path):
    """Документированное поведение: одно платформенное имя в strict не идёт."""
    con = _make(tmp_path)
    try:
        # SoloTool: 3 источника, но ВСЕ на одной внутренней платформе
        _post(con, 1, 1, "telegram", NOW - timedelta(hours=3), "SoloTool released")
        _post(con, 2, 3, "telegram", NOW - timedelta(hours=2), "SoloTool review")
        _post(con, 4, 1, "telegram", NOW - timedelta(hours=1), "SoloTool again")
        con.commit()
        data = novelties.build(con, now=NOW, external_result=_empty_external())
        assert all(r["entity"] != "solotool" for r in data["strict"])
    finally:
        con.close()
