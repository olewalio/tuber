"""ТЗ-50: ежедневная выдача «Сливки» — сборка, ссылки, перцентиль, формат.

Тесты идут на синтетической базе и с подставными проверялкой ссылок/отправителем:
сеть и боевая база не нужны. Проверяется ровно ТЗ:

* четыре блока собираются (авторы, подтемы, новинки, обсуждения);
* каждая позиция — с числом и ссылкой; позиция с неоткрывающейся ссылкой
  отбрасывается, а не печатается;
* перцентиль «где я» считается кодом по осям внутри НАШЕЙ базы и называет базу;
* формат сообщения — короткие строки с заголовками блоков;
* «пустой день» → молчание (stdout пуст), пустой отчёт не отправляется;
* строка журнала доставки имеет вид ``delivered …`` (факт, а не «отправлено»).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tuber.analysis import digest as D
from tuber.core import db, schema

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def con(tmp_path):
    conn = db.connect(str(tmp_path / "digest.db"))
    schema.init_schema(conn)
    yield conn
    conn.close()


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _source(con, sid, handle, subs, platform="telegram"):
    con.execute(
        "INSERT INTO source(id, platform, handle, status, subs) VALUES (?,?,?,'active',?)",
        (sid, platform, handle, subs))


def _content(con, cid, platform, sid, published, url=None):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at, url)"
        " VALUES (?,?,?,?,?,?)",
        (cid, platform, sid, f"e{cid}", published,
         url if url is not None else f"https://example.invalid/{cid}"))


def _latest(con, cid, platform, views=None, likes=None, reactions=None):
    con.execute(
        "INSERT INTO content_latest(content_id, platform, views, likes, reactions)"
        " VALUES (?,?,?,?,?)", (cid, platform, views, likes, reactions))


def _seed_posts(con, sid, platform, handle, subs, values, *, base=0):
    """Посты источника с метриками: ``values`` — список (views, reaction)."""
    _source(con, sid, handle, subs, platform)
    for i, (views, reaction) in enumerate(values):
        cid = base + i + 1
        _content(con, cid, platform, sid, _iso(NOW - timedelta(days=2)))
        if platform == "telegram":
            _latest(con, cid, platform, views=views, reactions=reaction)
        else:
            _latest(con, cid, platform, views=views, likes=reaction)


# ---------------------------------------------------------------------------
# Перцентиль
# ---------------------------------------------------------------------------

def test_percentile_rank_basic():
    assert D.percentile_rank([1, 2, 3, 4], 3) == pytest.approx(75.0)
    assert D.percentile_rank([1, 2, 3, 4], 0) == pytest.approx(0.0)
    assert D.percentile_rank([1, 2, 3, 4], 4) == pytest.approx(100.0)
    assert D.percentile_rank([], 1) is None
    assert D.percentile_rank([1], None) is None


def test_base_axis_distributions_reach_and_reactions(con):
    # source 1: медиана охвата 2.0 (600/300, 900/300, 300/300 → median 2.0),
    # медиана реакций на 1000: 10.0 (0.5→ ...) — считаем явно ниже.
    _seed_posts(con, 1, "telegram", "one", 300,
                [(600, 3), (900, 9), (300, 3)])
    _seed_posts(con, 2, "telegram", "two", 1000,
                [(500, 5), (1500, 30), (1000, 10)], base=10)
    dist = D.base_axis_distributions(con, now=NOW)
    assert dist["by_platform"]["telegram"]["reach"] == sorted([2.0, 1.0])
    # реакции на 1000: источник 1 → 5,10,10 → median 10; источник 2 → 10,20,10 → 10
    assert dist["by_platform"]["telegram"]["reactions"] == sorted([10.0, 10.0])


def test_base_axis_needs_min_posts(con):
    _seed_posts(con, 1, "telegram", "one", 300, [(600, 3)])  # 1 пост < min
    dist = D.base_axis_distributions(con, now=NOW, min_posts=3)
    assert dist["by_platform"].get("telegram", {}).get("reach", []) == []


def test_base_axis_includes_x_with_views_only(con):
    """D-66: X-источник с просмотрами входит в обе оси, без просмотров — нет."""
    _seed_posts(con, 1, "x", "xwith", 1000, [(1000, 10), (2000, 20), (3000, 30)])
    # X без просмотров: ось обязана его НЕ учитывать (в базе оси его нет).
    _seed_posts(con, 2, "x", "xwithout", 1000,
                [(None, 5), (None, 5), (None, 5)], base=10)
    dist = D.base_axis_distributions(con, now=NOW)
    x = dist["by_platform"].get("x", {})
    assert x.get("reach") == [2.0]          # только источник с просмотрами
    assert x.get("reactions") == [10.0]     # (10/1000,20/2000,30/3000)×1000 → 10
    block = D.percentile_block(con, now=NOW, subject="x:xwith")
    # Поимённая база каждой оси: ровно один X-источник с просмотрами.
    assert block["axis_bases"]["reach"]["x"] == 1
    assert block["axis_bases"]["reactions"]["x"] == 1
    reach = next(a for a in block["axes"] if a["key"] == "reach")
    assert reach["platform"] == "x"
    assert reach["platform_n"] == 1
    assert reach["platform_percentile"] == pytest.approx(100.0)
    assert reach["base_by_platform"]["x"] == 1


def test_axis_base_line_names_platforms():
    line = D._axis_base_line({
        "title": "охват на подписчика",
        "base_by_platform": {"youtube": 8943, "telegram": 1372, "x": 5}})
    assert "8 943 YouTube + 1 372 Telegram + 5 X" in line


def test_percentile_block_explicit_values(con):
    _seed_posts(con, 1, "telegram", "one", 100, [(10, 1), (10, 1), (10, 1)])
    _seed_posts(con, 2, "telegram", "two", 100, [(100, 1), (100, 1), (100, 1)], base=10)
    _seed_posts(con, 3, "telegram", "three", 100, [(1000, 1), (1000, 1), (1000, 1)], base=20)
    block = D.percentile_block(con, now=NOW, subject=None,
                               explicit={"reach": 2.0})
    reach = next(a for a in block["axes"] if a["key"] == "reach")
    assert reach["value"] == pytest.approx(2.0)
    # база — объединённая (субъект без платформы): 0.1, 1.0, 10.0 → 2 из 3
    assert reach["platform_percentile"] == pytest.approx(200 / 3, abs=0.01)
    assert reach["platform_n"] == 3
    assert reach["platform"] == "все платформы"


def test_percentile_block_tracked_subject(con):
    _seed_posts(con, 1, "telegram", "one", 100, [(10, 1), (10, 1), (10, 1)])
    _seed_posts(con, 2, "telegram", "two", 100, [(100, 1), (100, 1), (100, 1)], base=10)
    block = D.percentile_block(con, now=NOW, subject="telegram:two")
    assert block["subject"]["found"] is True
    reach = next(a for a in block["axes"] if a["key"] == "reach")
    assert reach["platform"] == "telegram"
    assert reach["platform_n"] == 2
    assert reach["platform_percentile"] == pytest.approx(100.0)


def test_percentile_block_missing_subject_has_base_median(con):
    _seed_posts(con, 1, "telegram", "one", 100, [(10, 1), (10, 1), (10, 1)])
    _seed_posts(con, 2, "telegram", "two", 100, [(100, 1), (100, 1), (100, 1)], base=10)
    block = D.percentile_block(con, now=NOW, subject="telegram:ghost")
    assert block["subject"]["found"] is False
    reach = next(a for a in block["axes"] if a["key"] == "reach")
    assert reach["value"] is None
    assert reach["platform_median"] == pytest.approx(0.55)


def test_cli_me_env_computes_registry_percentile(tmp_path, monkeypatch, capsys):
    """D-65: TUBER_SLIVKI_ME=platform:handle → перцентиль считается по базе."""
    path = str(tmp_path / "me.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    _seed_posts(conn, 1, "telegram", "one", 100, [(10, 1), (10, 1), (10, 1)])
    _seed_posts(conn, 2, "telegram", "two", 100, [(100, 1), (100, 1), (100, 1)], base=10)
    conn.commit()
    conn.close()

    monkeypatch.setenv("TUBER_SLIVKI_ME", "telegram:two")
    rc = D.main(["--db", path, "--no-network", "--no-check-links", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    pct = payload["percentile"]
    assert pct["subject"]["found"] is True
    assert pct["subject"]["handle"] == "two"
    reach = next(a for a in pct["axes"] if a["key"] == "reach")
    # Медианы: one → 0.1, two → 1.0; «two» не меньше обеих → 100%.
    assert reach["platform_percentile"] == pytest.approx(100.0)
    assert reach["base_by_platform"]["telegram"] == 2
    assert pct["axis_bases"]["reach"]["telegram"] == 2


def test_cli_without_me_env_is_honest(tmp_path, monkeypatch, capsys):
    """D-65/ТЗ-54: без переменных субъект не задан — честная строка, без личных значений."""
    path = str(tmp_path / "nome.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    _seed_posts(conn, 1, "telegram", "one", 100, [(10, 1), (10, 1), (10, 1)])
    conn.commit()
    conn.close()
    monkeypatch.delenv("TUBER_SLIVKI_ME", raising=False)
    monkeypatch.delenv("TUBER_SLIVKI_REACH", raising=False)
    monkeypatch.delenv("TUBER_SLIVKI_REACTIONS", raising=False)
    monkeypatch.delenv("TUBER_SLIVKI_G7", raising=False)
    # Блок данных подставляем, чтобы выдача не была пустой (иначе CLI молчит).
    monkeypatch.setattr(D, "collect_blocks", lambda *a, **k: dict(_data()))
    monkeypatch.setattr(D, "verify_all_links", lambda data, **k: data)
    rc = D.main(["--db", path, "--no-network", "--no-check-links"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "субъект не задан (--me или TUBER_SLIVKI_ME)" in out
    assert "нет данных о субъекте" in out


# ---------------------------------------------------------------------------
# Ссылки
# ---------------------------------------------------------------------------

def test_filter_by_links_drops_closed_and_uses_alternate():
    items = [
        {"url": "https://open/1", "urls": ["https://open/1"]},
        {"url": "https://dead/2", "urls": ["https://dead/2"]},
        {"url": "https://dead/3", "urls": ["https://dead/3", "https://open/3"]},
    ]
    opened = {"https://open/1", "https://open/3"}

    def checker(url, *, timeout=10.0):
        return url in opened

    res = D.filter_by_links(items, checker=checker)
    assert [i["url"] for i in res["items"]] == ["https://open/1", "https://open/3"]
    assert res["dropped"] == 1


def test_http_opens_rejects_non_http():
    assert D.http_opens("") is False
    assert D.http_opens("ftp://example.com") is False


# ---------------------------------------------------------------------------
# Формат сообщения
# ---------------------------------------------------------------------------

def _data(**over):
    data = {
        "day": "2026-09-21",
        "authors": [{"kind": "author", "handle": "bob", "platform": "x", "subs": 1200,
                     "g7": None, "breakout": 42.0, "outlier": 5.0,
                     "growth": "unknown", "url": "https://x.com/bob/status/1",
                     "urls": ["https://x.com/bob/status/1"]}],
        "subtopics": [{"kind": "subtopic", "entity": "gpt", "accel_sub": 1.5,
                       "share_t": 0.03, "authors": 7, "posts": 9,
                       "platforms": ["telegram"], "story_title": "s",
                       "url": "https://t.me/c/1", "urls": ["https://t.me/c/1"]}],
        "novelties": [{"kind": "novelty", "entity": "zcode", "total_sources": 7,
                       "internal_sources": 4, "external_sources": 3,
                       "platforms": ["telegram", "github"],
                       "url": "https://github.com/z", "internal_url": "https://t.me/c/2",
                       "external_url": "https://github.com/z", "urls": ["https://github.com/z"]}],
        "discussions": [{"kind": "discussion", "platform": "youtube", "title": "T",
                         "source": "S", "comments": 20, "authors": 20,
                         "question_share": 0.1, "url": "https://youtu.be/1",
                         "urls": ["https://youtu.be/1"]}],
        "links": {"checked": 4, "dropped": 0},
    }
    data.update(over)
    return data


def test_format_message_has_block_headers_and_links():
    text = D.format_message(_data())
    for header in ("СЛИВКИ", "АВТОРЫ", "ПОДТЕМЫ", "НОВИНКИ", "ОБСУЖДЕНИЯ"):
        assert header in text
    assert "https://x.com/bob/status/1" in text
    assert "https://github.com/z" in text
    assert "ссылок проверено 4, отброшено 0" in text


def test_format_message_has_no_emoji_and_short_lines():
    text = D.format_message(_data())
    assert len(max(text.splitlines(), key=len)) <= 100


def test_is_empty():
    assert D.is_empty({}) is True
    assert D.is_empty({"authors": [1]}) is False
    assert D.is_empty({"novelties": [1]}) is False


def test_split_message_respects_limit():
    text = "\n".join(f"строка {i} " + "x" * 50 for i in range(50))
    parts = D.split_message(text, limit=200)
    assert len(parts) > 1
    assert all(len(p) <= 200 for p in parts)
    assert "\n".join(parts).replace("\n", "") == text.replace("\n", "")


# ---------------------------------------------------------------------------
# Доставка и журнал
# ---------------------------------------------------------------------------

def test_deliver_uses_fake_poster_and_reports_ids():
    calls = []

    def poster(token, chat_id, thread_id, text, *, timeout=30.0):
        calls.append((token, chat_id, thread_id, text))
        return {"ok": True, "result": {"message_id": 100 + len(calls)}}

    outcome = D.deliver("tok", "123", "7", "привет", poster=poster)
    assert outcome["ok"] is True
    assert outcome["parts"] == 1
    assert outcome["message_ids"] == [101]
    assert calls[0][1] == "123" and calls[0][2] == "7"


def test_deliver_without_thread_does_not_guess():
    seen = {}

    def poster(token, chat_id, thread_id, text, *, timeout=30.0):
        seen["thread"] = thread_id
        return {"ok": True, "result": {"message_id": 1}}

    D.deliver("tok", "123", None, "привет", poster=poster)
    assert seen["thread"] is None


def test_journal_line_delivered_has_fact_fields():
    line = D.journal_line({"ok": True, "parts": 2, "message_ids": [5, 6]},
                          chat_id="100200300", thread_id=None, text="текст", when=NOW)
    assert line.startswith("delivered ")
    assert "chat_id=100200300" in line
    assert "thread_id=null" in line
    assert "message_id=6" in line


def test_journal_line_failed_marks_error():
    line = D.journal_line({"ok": False, "parts": 0, "message_ids": [],
                           "error": "boom"},
                          chat_id="1", thread_id="7", text="", when=NOW)
    assert line.startswith("failed ")
    assert "error=boom" in line


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_save_message_writes_exact_text(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "msg.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    conn.close()

    sample = _data()
    monkeypatch.setattr(D, "collect_blocks", lambda *a, **k: dict(sample))
    monkeypatch.setattr(D, "verify_all_links", lambda data, **k: data)
    out_file = tmp_path / "sent.txt"
    rc = D.main(["--db", path, "--no-network", "--no-check-links",
                 "--save-message", str(out_file)])
    assert rc == 0
    text = out_file.read_text(encoding="utf-8")
    assert "СЛИВКИ" in text and "https://x.com/bob/status/1" in text
    # stdout без --send равен тому же тексту (аудит-файл — точная копия).
    assert text == capsys.readouterr().out.rstrip("\n")


def test_cli_empty_day_is_silent(tmp_path, capsys):
    """Пустая база: отчёт не печатается (stdout пуст), код 0."""
    path = str(tmp_path / "empty.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    conn.close()
    rc = D.main(["--db", path, "--no-network", "--no-check-links"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "", "пустой день обязан молчать"


def test_cli_dry_run_send_prints_plan_with_write_flag(tmp_path, capsys):
    path = str(tmp_path / "plan.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    conn.close()
    rc = D.main(["slivki", "--send", "--dry-run", "--db", path,
                 "--no-network", "--no-check-links", "--chat-id", "555"])
    out = capsys.readouterr()
    assert rc == 0
    assert "--allow-production" in out.out
    assert "chat_id=555" in out.out
    assert "thread_id=null" in out.out


# ---------------------------------------------------------------------------
# ТЗ-54: адрес доставки — только из окружения, личных значений в коде нет
# ---------------------------------------------------------------------------

def _send_db(tmp_path):
    path = str(tmp_path / "send.db")
    conn = db.connect(path)
    schema.init_schema(conn)
    conn.close()
    return path


def test_send_without_chat_id_refuses_and_does_not_deliver(tmp_path, capsys, monkeypatch):
    """Нет --chat-id и нет TUBER_DIGEST_CHAT_ID → rc=2, честный отказ, отправки нет."""
    path = _send_db(tmp_path)
    monkeypatch.delenv("TUBER_DIGEST_CHAT_ID", raising=False)
    monkeypatch.setattr(D, "collect_blocks", lambda *a, **k: dict(_data()))
    calls = []
    monkeypatch.setattr(D, "deliver", lambda *a, **k: calls.append(a))
    rc = D.main(["slivki", "--send", "--db", path, "--no-network",
                 "--no-check-links", "--journal", str(tmp_path / "j.log")])
    out = capsys.readouterr()
    assert rc == 2
    assert "нет chat_id" in out.err
    assert calls == [], "при пустом chat_id отправка не должна запускаться"


def test_send_chat_id_from_env(tmp_path, capsys, monkeypatch):
    """TUBER_DIGEST_CHAT_ID из окружения подхватывается без --chat-id."""
    path = _send_db(tmp_path)
    monkeypatch.setenv("TUBER_DIGEST_CHAT_ID", "100200300")
    monkeypatch.setattr(D, "collect_blocks", lambda *a, **k: dict(_data()))
    monkeypatch.setattr(D, "load_bot_token", lambda: "tok")
    seen = {}

    def fake_deliver(token, chat_id, thread_id, text, **kw):
        seen["chat_id"] = chat_id
        return {"ok": True, "parts": 1, "message_ids": [1]}

    monkeypatch.setattr(D, "deliver", fake_deliver)
    rc = D.main(["slivki", "--send", "--db", path, "--no-network",
                 "--no-check-links", "--journal", str(tmp_path / "j.log")])
    assert rc == 0, capsys.readouterr().err
    assert seen["chat_id"] == "100200300"


def test_send_chat_id_flag_beats_env(tmp_path, capsys, monkeypatch):
    """--chat-id важнее окружения (приоритет явного аргумента)."""
    path = _send_db(tmp_path)
    monkeypatch.setenv("TUBER_DIGEST_CHAT_ID", "999999999")
    monkeypatch.setattr(D, "collect_blocks", lambda *a, **k: dict(_data()))
    monkeypatch.setattr(D, "load_bot_token", lambda: "tok")
    seen = {}

    def fake_deliver(token, chat_id, thread_id, text, **kw):
        seen["chat_id"] = chat_id
        return {"ok": True, "parts": 1, "message_ids": [1]}

    monkeypatch.setattr(D, "deliver", fake_deliver)
    rc = D.main(["slivki", "--send", "--db", path, "--no-network", "--no-check-links",
                 "--chat-id", "555", "--journal", str(tmp_path / "j.log")])
    assert rc == 0, capsys.readouterr().err
    assert seen["chat_id"] == "555"
