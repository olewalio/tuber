"""Тесты разбора обложек топ-видео (tuber.thumbs).

Сеть не используется: urllib.request.urlopen и thumbs.fetch_image
подменяются моками. Боевая БД не затрагивается.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from tuber.platforms.youtube import cli, config, store as db, thumbs

NOW = 1_700_000_000


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "thumbs_test.db")
    db.init_db(c)
    yield c
    c.close()


def add_channel(conn, cid="c1"):
    db.upsert_channel(conn, {"channel_id": cid, "title": "Канал", "first_seen": 1})


def add_video(conn, vid, views, cid="c1", title=None, thumbnail="auto",
              is_shorts=None, is_ai=1):
    url = None
    if thumbnail == "auto":
        url = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
    elif thumbnail is not None:
        url = thumbnail
    data = {
        "video_id": vid,
        "channel_id": cid,
        "title": title or f"Видео {vid}",
        "thumbnail_url": url,
        "published_at": NOW - 86400,
        "first_seen": 1,
    }
    if is_shorts is not None:
        data["is_shorts"] = is_shorts
    db.upsert_video(conn, data)
    db.insert_snapshot(conn, vid, NOW, "d", views=views)
    if is_ai is not None:
        db.save_classification(conn, vid, is_ai=is_ai, confidence=0.9)


def add_pool(conn):
    """5 базовых видео (медиана 1000) + 3 выброса: 9x, 7x, 5x + 1.5x."""
    add_channel(conn)
    for i in range(5):
        add_video(conn, f"base{i}", 1000)
    add_video(conn, "low", 1500)      # 1.5x — не выброс
    add_video(conn, "h1", 9000)       # 9x
    add_video(conn, "h2", 7000)       # 7x
    add_video(conn, "h3", 5000)       # 5x


# --- подмена сети ----------------------------------------------------------


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _chat_resp(content: str, usage: dict | None = None) -> _FakeResp:
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": usage or {
            "prompt_tokens": 1315, "completion_tokens": 174, "cached_tokens": 0,
        },
    }
    return _FakeResp(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def _patch_chat(monkeypatch, content, usage=None):
    """urlopen для POST модели; картинка отдаётся через fetch_image."""
    monkeypatch.setattr(thumbs, "fetch_image", lambda url, timeout=None: b"\xff\xd8img")
    monkeypatch.setenv("KIMI_API_KEY", "test-key")

    def fake_urlopen(request, timeout=None):
        return _chat_resp(content, usage)

    monkeypatch.setattr(thumbs.urllib.request, "urlopen", fake_urlopen)


# --- 1. отбор ---------------------------------------------------------------


def test_select_only_outliers_sorted(conn):
    add_pool(conn)
    items = thumbs.select_top_videos(conn, max_per_channel=0)
    ids = [i["video_id"] for i in items]
    assert ids == ["h1", "h2", "h3"]
    mults = [i["mult"] for i in items]
    assert mults == sorted(mults, reverse=True)
    assert all(i["mult"] >= 3.0 for i in items)
    assert "low" not in ids
    assert items[0]["thumbnail_url"].startswith("https://")


def test_max_per_channel_limit_and_zero(conn):
    add_pool(conn)
    two = thumbs.select_top_videos(conn, max_per_channel=2)
    assert [i["video_id"] for i in two] == ["h1", "h2"]
    unlimited = thumbs.select_top_videos(conn, max_per_channel=0)
    assert len(unlimited) == 3


def test_limit_caps_selection(conn):
    add_pool(conn)
    items = thumbs.select_top_videos(conn, max_per_channel=0, limit=2)
    assert [i["video_id"] for i in items] == ["h1", "h2"]


def test_already_analyzed_excluded(conn):
    add_pool(conn)
    conn.execute(
        "INSERT INTO thumbnail_vision (video_id, model) VALUES ('h1', 'kimi-k2.6')"
    )
    conn.commit()
    ids = [i["video_id"] for i in thumbs.select_top_videos(conn, max_per_channel=0)]
    assert "h1" not in ids
    assert ids == ["h2", "h3"]


def test_missing_thumbnail_excluded(conn):
    add_pool(conn)
    db.upsert_video(conn, {
        "video_id": "h1", "channel_id": "c1", "thumbnail_url": None,
    })
    conn.commit()
    ids = [i["video_id"] for i in thumbs.select_top_videos(conn, max_per_channel=0)]
    assert "h1" not in ids
    assert ids == ["h2", "h3"]


# --- 1b. достоверность канала (фильтр min_channel_n) ------------------------


def _force_outliers(monkeypatch, mapping):
    """Подменить report._outlier_map заданной картой video_id -> кратность."""
    monkeypatch.setattr(
        thumbs.report, "_outlier_map",
        lambda conn, fmt=None: dict(mapping),
    )


def test_channel_with_four_videos_rejected_and_counted(conn, monkeypatch):
    add_channel(conn)
    for i in range(4):
        add_video(conn, f"v{i}", 1000)
    _force_outliers(monkeypatch, {"v3": 995.48})
    assert thumbs.select_top_videos(conn, max_per_channel=0, min_channel_n=5) == []
    res = thumbs.run(conn, max_per_channel=0, dry_run=True, min_channel_n=5)
    assert res["selected"] == 0
    assert res["skipped_low_n"] == 1


def test_channel_with_five_videos_passes(conn):
    add_channel(conn)
    for i in range(4):
        add_video(conn, f"base{i}", 1000)
    add_video(conn, "hero", 9000)
    items = thumbs.select_top_videos(conn, max_per_channel=0)  # min_channel_n=5 по умолчанию
    assert [i["video_id"] for i in items] == ["hero"]
    assert items[0]["mult"] == pytest.approx(9.0)


def test_min_channel_n_zero_disables_filter(conn, monkeypatch):
    add_channel(conn)
    add_video(conn, "only", 5000)
    _force_outliers(monkeypatch, {"only": 100.0})
    assert thumbs.select_top_videos(conn, max_per_channel=0, min_channel_n=5) == []
    items = thumbs.select_top_videos(conn, max_per_channel=0, min_channel_n=0)
    assert [i["video_id"] for i in items] == ["only"]


def test_mult_comes_from_outlier_map(conn, monkeypatch):
    add_channel(conn)
    add_video(conn, "hero", 1234)
    _force_outliers(monkeypatch, {"hero": 42.25})
    items = thumbs.select_top_videos(conn, max_per_channel=0, min_channel_n=0)
    assert len(items) == 1
    assert items[0]["mult"] == pytest.approx(42.25)


# --- 2. стоимость ----------------------------------------------------------


def test_cost_zero():
    assert thumbs.cost(0, 0, 0) == 0.0


def test_cost_cache_hit():
    assert thumbs.cost(1_000_000, 0, 1_000_000) == pytest.approx(
        config.THUMB_PRICE_IN_CACHE, abs=1e-9
    )


def test_cost_regular_case():
    # 1000 входных (200 в кэше), 500 выходных.
    expected = (
        800 * config.THUMB_PRICE_IN_MISS / 1e6
        + 200 * config.THUMB_PRICE_IN_CACHE / 1e6
        + 500 * config.THUMB_PRICE_OUT / 1e6
    )
    assert thumbs.cost(1000, 500, 200) == pytest.approx(expected, abs=1e-9)


# --- 3. разбор ответа модели ------------------------------------------------


def test_analyze_strips_json_fence(monkeypatch):
    content = '```json\n{"text_on_image": "ТОП 5", "objects": ["лицо"], '
    content += '"face_count": 1, "has_arrows_or_circles": true, '
    content += '"style": "лицо-крупным-планом", "colors": ["красный"], '
    content += '"description": "Лицо и текст."}\n```'
    _patch_chat(monkeypatch, content)
    res = thumbs.analyze("v1", "https://example.test/a.jpg")
    assert res["ok"] is True
    assert res["parsed"]["text_on_image"] == "ТОП 5"
    assert res["parsed"]["face_count"] == 1
    assert res["error"] is None


def test_analyze_plain_json(monkeypatch):
    _patch_chat(monkeypatch, '{"text_on_image": "", "objects": [], "face_count": 0}')
    res = thumbs.analyze("v1", "https://example.test/a.jpg")
    assert res["ok"] is True
    assert res["parsed"] == {"text_on_image": "", "objects": [], "face_count": 0}


def test_analyze_non_json_keeps_raw(monkeypatch):
    _patch_chat(monkeypatch, "извините, не могу описать")
    res = thumbs.analyze("v1", "https://example.test/a.jpg")
    assert res["ok"] is True
    assert res["parsed"] is None
    assert "извините" in res["raw"]
    assert res["error"]


def test_analyze_http_400_not_retried(monkeypatch):
    monkeypatch.setattr(thumbs, "fetch_image", lambda url, timeout=None: b"\xff\xd8img")
    monkeypatch.setenv("KIMI_API_KEY", "test-key")
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(
            "https://api.moonshot.ai", 400, "Bad Request", None,
            io.BytesIO(b'{"error":"invalid temperature"}'),
        )

    monkeypatch.setattr(thumbs.urllib.request, "urlopen", fake_urlopen)
    res = thumbs.analyze("v1", "https://example.test/a.jpg")
    assert res["ok"] is False
    assert "400" in res["error"]
    assert len(calls) == 1  # 400 не ретраится


def test_fetch_image_returns_none_after_retries(monkeypatch):
    monkeypatch.setattr(
        thumbs.urllib.request, "urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(OSError("нет сети")),
    )
    assert thumbs.fetch_image("https://example.test/a.jpg") is None


def test_get_api_key(monkeypatch):
    monkeypatch.setattr(thumbs.config, "load_env", lambda *a, **k: 0)
    monkeypatch.setenv("KIMI_API_KEY", "  secret  ")
    assert thumbs.get_api_key() == "secret"
    monkeypatch.delenv("KIMI_API_KEY")
    assert thumbs.get_api_key() is None


# --- 4. запись --------------------------------------------------------------


def _ok_res(cost_usd=0.002, parsed=None):
    return {
        "ok": True, "parsed": parsed if parsed is not None else {
            "text_on_image": "ТОП 5 инструментов",
            "objects": ["лицо", "ноутбук"],
            "face_count": 1,
            "has_arrows_or_circles": True,
            "style": "лицо-крупным-планом",
            "colors": ["красный", "белый", "чёрный"],
            "description": "Крупное лицо и текст.",
        },
        "raw": '{"...": "..."}', "error": None,
        "tokens_in": 1315, "tokens_out": 174, "cached_tokens": 0,
        "cost_usd": cost_usd, "latency_ms": 10000,
        "model": config.THUMB_MODEL, "prompt_version": config.THUMB_PROMPT_VERSION,
    }


def test_save_writes_all_tables(conn):
    add_channel(conn)
    add_video(conn, "v1", 1000)
    conn.execute("INSERT INTO seo_fields (video_id) VALUES ('v1')")
    conn.commit()

    thumbs.save(conn, "v1", _ok_res())

    tv = conn.execute("SELECT * FROM thumbnail_vision WHERE video_id='v1'").fetchone()
    assert tv["model"] == config.THUMB_MODEL
    assert tv["extracted_text"] == "ТОП 5 инструментов"
    assert tv["cost_usd"] == pytest.approx(0.002)
    assert tv["latency_ms"] == 10000

    sf = conn.execute("SELECT * FROM seo_fields WHERE video_id='v1'").fetchone()
    assert sf["thumb_text"] == "ТОП 5 инструментов"
    assert sf["thumb_text_words"] == 3
    assert json.loads(sf["thumb_objects"]) == ["лицо", "ноутбук"]
    assert sf["thumb_face_count"] == 1
    assert sf["thumb_arrows"] == 1
    assert json.loads(sf["thumb_colors"]) == ["красный", "белый", "чёрный"]
    assert sf["thumb_style"] == "лицо-крупным-планом"

    usage = conn.execute(
        "SELECT * FROM llm_usage WHERE stage='thumbnail_vision'"
    ).fetchone()
    assert usage["model"] == config.THUMB_MODEL
    assert usage["tokens_in"] == 1315
    assert usage["tokens_out"] == 174


def test_save_does_not_create_seo_row(conn):
    add_channel(conn)
    add_video(conn, "v1", 1000)
    thumbs.save(conn, "v1", _ok_res())
    assert conn.execute(
        "SELECT COUNT(*) FROM seo_fields WHERE video_id='v1'"
    ).fetchone()[0] == 0


# --- 5. прогон --------------------------------------------------------------


def test_run_http_error_not_written(conn, monkeypatch):
    add_pool(conn)
    monkeypatch.setattr(thumbs, "fetch_image", lambda url, timeout=None: b"\xff\xd8img")
    monkeypatch.setenv("KIMI_API_KEY", "test-key")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.moonshot.ai", 500, "Server Error", None,
            io.BytesIO(b"boom"),
        )

    monkeypatch.setattr(thumbs.urllib.request, "urlopen", fake_urlopen)
    res = thumbs.run(conn, max_per_channel=0)
    assert res["errors"] == 3
    assert res["done"] == 0
    assert conn.execute("SELECT COUNT(*) FROM thumbnail_vision").fetchone()[0] == 0


def test_run_broken_image_not_written(conn, monkeypatch):
    add_pool(conn)
    monkeypatch.setattr(thumbs, "fetch_image", lambda url, timeout=None: None)
    monkeypatch.setenv("KIMI_API_KEY", "test-key")
    res = thumbs.run(conn, max_per_channel=0)
    assert res["errors"] == 3
    assert res["done"] == 0
    assert conn.execute("SELECT COUNT(*) FROM thumbnail_vision").fetchone()[0] == 0


def test_run_success_writes(conn, monkeypatch):
    add_pool(conn)
    monkeypatch.setattr(thumbs, "analyze", lambda vid, url: _ok_res())
    res = thumbs.run(conn, max_per_channel=0)
    assert res["done"] == 3
    assert res["errors"] == 0
    assert res["cost_usd"] == pytest.approx(0.006)
    assert conn.execute("SELECT COUNT(*) FROM thumbnail_vision").fetchone()[0] == 3


def test_run_budget_stops(conn, monkeypatch):
    add_pool(conn)
    monkeypatch.setattr(thumbs, "analyze", lambda vid, url: _ok_res(cost_usd=0.20))
    res = thumbs.run(conn, max_per_channel=0, budget_usd=0.25)
    assert res["done"] == 2          # третий вызов не состоялся
    assert res["cost_usd"] == pytest.approx(0.40)


def test_run_skipped_existing_count(conn, monkeypatch):
    add_pool(conn)
    conn.execute(
        "INSERT INTO thumbnail_vision (video_id, model) VALUES ('h1', 'kimi-k2.6')"
    )
    conn.commit()
    monkeypatch.setattr(thumbs, "analyze", lambda vid, url: _ok_res())
    res = thumbs.run(conn, max_per_channel=0)
    assert res["skipped_existing"] == 1
    assert res["done"] == 2


def test_run_dry_run_no_network_no_write(conn, monkeypatch, capsys):
    add_pool(conn)

    def boom(*a, **k):
        raise AssertionError("сеть в dry-run запрещена")

    monkeypatch.setattr(thumbs.urllib.request, "urlopen", boom)
    monkeypatch.setattr(thumbs, "fetch_image", boom)
    res = thumbs.run(conn, max_per_channel=0, dry_run=True)
    out = capsys.readouterr().out
    assert res["done"] == 0
    assert res["selected"] == 3
    assert "h1" in out
    assert conn.execute("SELECT COUNT(*) FROM thumbnail_vision").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM seo_fields WHERE thumb_text IS NOT NULL"
    ).fetchone()[0] == 0


# --- 6. формат вывода -------------------------------------------------------


def test_format_run_numbers():
    text = thumbs.format_run(
        {"selected": 5, "done": 4, "errors": 1,
         "skipped_existing": 2, "cost_usd": 0.0078}
    )
    assert "разобрано 4" in text
    assert "ошибок 1" in text
    assert "0.0078" in text
    assert "уже было 2" in text


def test_format_run_mentions_skipped_low_n():
    text = thumbs.format_run(
        {"selected": 3, "done": 2, "errors": 1, "skipped_existing": 0,
         "skipped_low_n": 7, "cost_usd": 0.0}
    )
    assert "отсеяно (мало видео у канала) 7" in text


def test_dry_run_line_has_mult_views_format(conn, capsys):
    add_pool(conn)
    res = thumbs.run(conn, max_per_channel=0, dry_run=True)
    out = capsys.readouterr().out
    assert res["skipped_low_n"] == 0
    assert "Обложки (сухой прогон): к разбору 3, отсеяно по достоверности 0" in out
    assert "9.0x | 9 000 просмотров | long | Канал — Видео h1" in out
    # не больше 10 строк-кандидатов
    assert len([ln for ln in out.splitlines() if "x |" in ln]) <= 10


# --- 7. консольный вход ------------------------------------------------------


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Изолированные пути БД, замка и лога для cli."""
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "tuber.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "tuber.log")
    return tmp_path


def test_cli_thumbs_without_key_is_clear_error(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load_env", lambda *a, **k: 0)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    rc = cli.main(["seo", "--thumbs", "--dry-run"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "KIMI_API_KEY" in err
    assert "Traceback" not in err


def test_cli_thumbs_dry_run(sandbox, monkeypatch, capsys):
    conn = db.connect(sandbox / "tuber.db")
    db.init_db(conn)
    add_pool(conn)
    conn.close()
    monkeypatch.setattr(cli.thumbs, "get_api_key", lambda: "test-key")
    rc = cli.main(["seo", "--thumbs", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "сухой прогон" in out
    assert "h1" in out


def _mock_daily(monkeypatch):
    monkeypatch.setattr(cli.collect, "run_collect", lambda *a, **k: {
        "queries": 1, "queries_done": 1, "requests": 1, "found": 1, "new": 1,
        "channels_scanned": 1, "units": 1, "errors": [], "stopped_by_budget": False,
    })
    monkeypatch.setattr(cli.classify, "classify_videos", lambda *a, **k: {
        "requested": 1, "batches": 1, "classified": 1, "ai": 1, "not_ai": 0,
        "failed": 0, "topics": {}, "tokens_in": 1, "tokens_out": 1,
        "cost_usd": 0.0, "errors": [],
    })
    monkeypatch.setattr(cli.expand, "run_expand", lambda *a, **k: {
        "dry_run": False, "budget": 1, "sources": {}, "probed": 0, "accepted": 0,
        "rejected": 0, "units": 0, "stopped_reason": None, "steps": [],
    })
    monkeypatch.setattr(cli.schedule, "run_snapshots", lambda *a, **k: {
        "planned": 1, "captured": 1, "batches": 1, "failed_batches": 0, "errors": [],
    })
    monkeypatch.setattr(cli.seo, "analyze", lambda *a, **k: {
        "videos": 1, "written": 1, "skipped": 0, "no_title": 0,
    })


def test_daily_skips_thumbs_without_key(sandbox, monkeypatch, capsys):
    _mock_daily(monkeypatch)
    monkeypatch.setattr(cli.config, "load_env", lambda *a, **k: 0)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    rc = cli.main(["daily", "--days", "3", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["thumbs"]["skipped"] is True


def test_daily_thumbs_failure_does_not_break_cycle(sandbox, monkeypatch, capsys):
    _mock_daily(monkeypatch)
    monkeypatch.setattr(cli.thumbs, "get_api_key", lambda: "test-key")

    def boom(*a, **k):
        raise RuntimeError("обложки сломались")

    monkeypatch.setattr(cli.thumbs, "run", boom)
    rc = cli.main(["daily", "--days", "3", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" in payload["thumbs"]
    assert payload["collect"]["new"] == 1


# --- 8. схема v2: main_text вместо простыни, обратная совместимость ---------


def _seo_row(conn, vid):
    conn.execute("INSERT INTO seo_fields (video_id) VALUES (?)", (vid,))
    conn.commit()


def _saved(conn, vid):
    tv = conn.execute(
        "SELECT * FROM thumbnail_vision WHERE video_id=?", (vid,)
    ).fetchone()
    sf = conn.execute("SELECT * FROM seo_fields WHERE video_id=?", (vid,)).fetchone()
    return tv, sf


def test_old_keys_text_on_image_goes_to_thumb_text(conn):
    add_channel(conn)
    add_video(conn, "v1", 1000)
    _seo_row(conn, "v1")
    thumbs.save(conn, "v1", _ok_res(parsed={
        "text_on_image": "СТАРЫЙ ТЕКСТ",
        "objects": ["лицо"],
        "face_count": 2,
        "arrows_or_circles": True,
        "colors": ["красный"],
        "style": "лицо-крупным-планом",
    }))
    tv, sf = _saved(conn, "v1")
    assert sf["thumb_text"] == "СТАРЫЙ ТЕКСТ"
    assert sf["thumb_text_words"] == 2
    assert sf["thumb_face_count"] == 2          # face_count → people_count
    assert sf["thumb_arrows"] == 1              # arrows_or_circles
    assert json.loads(sf["thumb_colors"]) == ["красный"]
    assert tv["extracted_text"] == "СТАРЫЙ ТЕКСТ"
    assert tv["description_raw"] == _ok_res()["raw"]


def test_main_text_longer_than_200_is_clipped(conn):
    add_channel(conn)
    add_video(conn, "v1", 1000)
    _seo_row(conn, "v1")
    long_text = "простыня " * 300
    thumbs.save(conn, "v1", _ok_res(parsed={"main_text": long_text}))
    tv, sf = _saved(conn, "v1")
    assert len(sf["thumb_text"]) <= thumbs.THUMB_TEXT_MAX
    assert sf["thumb_text"].endswith("…")
    assert tv["extracted_text"] == long_text.strip()   # без обрезки


def test_thumb_objects_capped_at_six(conn):
    add_channel(conn)
    add_video(conn, "v1", 1000)
    _seo_row(conn, "v1")
    thumbs.save(conn, "v1", _ok_res(parsed={
        "main_text": "ТОП",
        "objects": [f"obj{i}" for i in range(12)],
    }))
    _tv, sf = _saved(conn, "v1")
    objects = json.loads(sf["thumb_objects"])
    assert len(objects) == thumbs.THUMB_OBJECTS_MAX == 6
    assert objects == ["obj0", "obj1", "obj2", "obj3", "obj4", "obj5"]


def test_thumb_text_words_counts_clipped_text(conn):
    add_channel(conn)
    add_video(conn, "v1", 1000)
    _seo_row(conn, "v1")
    many = " ".join(f"слово{i}" for i in range(500))
    thumbs.save(conn, "v1", _ok_res(parsed={"main_text": many}))
    _tv, sf = _saved(conn, "v1")
    assert sf["thumb_text_words"] == len(sf["thumb_text"].split())
    assert sf["thumb_text_words"] > 0


def test_prompt_forbids_screenshot_text(monkeypatch):
    monkeypatch.setattr(thumbs, "fetch_image", lambda url, timeout=None: b"\xff\xd8img")
    monkeypatch.setenv("KIMI_API_KEY", "test-key")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _chat_resp('{"main_text": "ТОП"}')

    monkeypatch.setattr(thumbs.urllib.request, "urlopen", fake_urlopen)
    res = thumbs.analyze("v1", "https://example.test/a.jpg")
    assert res["ok"] is True
    text = captured["payload"]["messages"][0]["content"][1]["text"]
    assert "не перечисляй текст со скриншотов" in text
    assert "main_text" in text
    assert "objects" in text


def test_missing_main_text_writes_empty_without_error(conn):
    add_channel(conn)
    add_video(conn, "v1", 1000)
    _seo_row(conn, "v1")
    thumbs.save(conn, "v1", _ok_res(parsed={"objects": ["лицо"]}))
    tv, sf = _saved(conn, "v1")
    assert sf["thumb_text"] == ""
    assert sf["thumb_text_words"] == 0
    assert tv["extracted_text"] == ""


def test_new_keys_and_unknown_keys_ignored(conn):
    add_channel(conn)
    add_video(conn, "v1", 1000)
    _seo_row(conn, "v1")
    thumbs.save(conn, "v1", _ok_res(parsed={
        "main_text": "ЗАГОЛОВОК",
        "small_text": "мелкая подпись",
        "objects": ["робот"],
        "people_count": 3,
        "has_arrows_or_circles": True,
        "dominant_colors": ["синий", "белый"],
        "style": "градиент",
        "description": "мусорный ключ",
    }))
    tv, sf = _saved(conn, "v1")
    assert sf["thumb_text"] == "ЗАГОЛОВОК"
    assert sf["thumb_face_count"] == 3
    assert sf["thumb_arrows"] == 1
    assert json.loads(sf["thumb_colors"]) == ["синий", "белый"]
    assert sf["thumb_style"] == "градиент"
    assert tv["extracted_text"] == "ЗАГОЛОВОК"


def test_new_prompt_version():
    assert config.THUMB_PROMPT_VERSION == "v2"
    res = _ok_res()
    assert res["prompt_version"] == "v2"


