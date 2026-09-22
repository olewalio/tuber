"""Тесты смыслового разбора (tuber.classify). Сеть — только мок."""

from __future__ import annotations

import json

import pytest

from tuber.platforms.youtube import classify, config, store as db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "tuber_test.db")
    db.init_db(c)
    yield c
    c.close()


def _add_video(conn, video_id, published_at=1_000_000):
    db.upsert_channel(conn, {"channel_id": "c1", "title": "Канал", "first_seen": 1})
    db.upsert_video(
        conn,
        {
            "video_id": video_id,
            "channel_id": "c1",
            "title": f"title {video_id}",
            "description": "описание",
            "duration_seconds": 600,
            "published_at": published_at,
            "thumbnail_url": "https://i.ytimg.com/vi/x/maxresdefault.jpg",
            "first_seen": 1,
        },
    )


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    """Мок requests.Session: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, contents, usage=None):
        self.contents = list(contents)
        self.usage = usage or {"prompt_tokens": 100, "completion_tokens": 50}
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        content = self.contents.pop(0) if self.contents else "[]"
        return _Resp({
            "choices": [{"message": {"content": content}}],
            "usage": dict(self.usage),
        })


def _arr(*items):
    return json.dumps(list(items), ensure_ascii=False)


def _item(vid, **kw):
    base = {
        "id": vid, "is_ai": True, "topic": "модели и релизы",
        "confidence": 0.9, "title_ru": "Разбор новой модели",
        "summary_ru": "Коротко о релизе.", "lang": "en",
        "reason": "предмет видео — ИИ",
    }
    base.update(kw)
    return base


# --- разбор ответа ---------------------------------------------------------


def test_parse_response_with_json_fence():
    text = "```json\n" + _arr(_item("v1")) + "\n```"
    parsed = classify.parse_response(text, ["v1"])
    assert parsed is not None
    assert parsed["v1"]["is_ai"] == 1
    assert parsed["v1"]["topic"] == "модели и релизы"


def test_parse_response_with_prose_around_array():
    text = "Вот результат:\n" + _arr(_item("v1")) + "\nГотово."
    parsed = classify.parse_response(text, ["v1"])
    assert parsed is not None and "v1" in parsed


def test_topic_outside_closed_list_is_dropped():
    text = _arr(_item("v1", topic="крипта и мемы"))
    parsed = classify.parse_response(text, ["v1"])
    assert parsed["v1"]["topic"] is None


def test_unknown_ids_are_ignored():
    text = _arr(_item("v1"), _item("ghost"))
    parsed = classify.parse_response(text, ["v1"])
    assert set(parsed) == {"v1"}


# --- батчи и запись --------------------------------------------------------


def test_batches_of_20(conn):
    for i in range(25):
        _add_video(conn, f"v{i}", published_at=1_000_000 + i)
    contents = [
        _arr(*[_item(f"v{i}") for i in range(25)]),
        _arr(*[_item(f"v{i}") for i in range(25)]),
    ]
    session = FakeSession(contents)
    res = classify.classify_videos(conn, config, session=session, api_key="k")
    assert res["batches"] == 2
    assert len(session.posts) == 2
    assert res["classified"] == 25
    assert res["ai"] == 25


def test_no_reclassification_of_done_video(conn):
    _add_video(conn, "v1")
    db.save_classification(conn, "v1", is_ai=1, topic="модели и релизы",
                           reason="уже разобрано", model="deepseek-chat")
    rows = db.get_unclassified(conn)
    assert rows == []


def test_get_unclassified_freshest_first(conn):
    _add_video(conn, "old", published_at=1_000)
    _add_video(conn, "new", published_at=9_000)
    ids = [r["video_id"] for r in db.get_unclassified(conn)]
    assert ids == ["new", "old"]


def test_retry_on_invalid_json_then_success(conn):
    _add_video(conn, "v1")
    session = FakeSession(["это не json", _arr(_item("v1"))])
    res = classify.classify_videos(conn, config, session=session, api_key="k")
    assert len(session.posts) == 2
    assert res["failed"] == 0
    assert res["ai"] == 1
    row = conn.execute(
        "SELECT * FROM video_classification WHERE video_id='v1'"
    ).fetchone()
    assert row["is_ai"] == 1


def test_is_ai_null_after_two_failures(conn):
    _add_video(conn, "v1")
    session = FakeSession(["нет json", "снова мусор"])
    res = classify.classify_videos(conn, config, session=session, api_key="k")
    assert len(session.posts) == 2
    assert res["failed"] == 1
    row = conn.execute(
        "SELECT * FROM video_classification WHERE video_id='v1'"
    ).fetchone()
    assert row["is_ai"] is None
    assert row["reason"] == "parse_error"


def test_cost_and_tokens_logged(conn):
    _add_video(conn, "v1")
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    session = FakeSession([_arr(_item("v1"))], usage=usage)
    res = classify.classify_videos(conn, config, session=session, api_key="k")
    # 1M входа * 0.14/1M + 1M выхода * 0.28/1M = 0.42
    assert res["cost_usd"] == pytest.approx(0.42, abs=1e-6)
    assert res["tokens_in"] == 1_000_000
    assert res["tokens_out"] == 1_000_000
    logged = conn.execute("SELECT * FROM llm_usage").fetchone()
    assert logged["stage"] == "classify"
    assert logged["cost_usd"] == pytest.approx(0.42, abs=1e-6)


def test_missing_api_key_raises(conn, monkeypatch):
    # D-01 закрыт: classify сам читает .env, поэтому для проверки отсутствия
    # ключа изолируем чтение файла (иначе тест зависел бы от /root/.hermes/.env).
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(classify.config, "load_env", lambda *a, **k: 0)
    _add_video(conn, "v1")
    with pytest.raises(RuntimeError):
        classify.classify_videos(conn, config, session=FakeSession(["[]"]), api_key=None)


def test_not_ai_video_not_counted_in_topics(conn):
    _add_video(conn, "v1")
    session = FakeSession([_arr(_item("v1", is_ai=False, topic=None, confidence=0.2))])
    res = classify.classify_videos(conn, config, session=session, api_key="k")
    assert res["ai"] == 0
    assert res["not_ai"] == 1
    assert res["topics"] == {}
