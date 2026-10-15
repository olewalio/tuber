"""Тесты расширенной таксономии тем и переразбора (tuber.classify).

Сеть не используется: модель подменяется моком. Боевая БД не затрагивается.
"""

from __future__ import annotations

import json
import re

import pytest

from tuber.platforms.youtube import classify, cli, config, store as db

NEW_TOPICS = (
    "запуски и анонсы",
    "стартапы и бизнес",
    "дизайн и креатив",
    "влоги и личный опыт",
    "обучение и навыки",
)


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


def _classify(conn, video_id, is_ai=1, topic="модели и релизы",
              classified_at=1_000):
    db.save_classification(
        conn, video_id, is_ai=is_ai, topic=topic, confidence=0.5,
        title_ru=f"старое описание {video_id}", summary_ru="старое",
        lang="ru", reason="ранее", model="deepseek-chat",
        classified_at=classified_at,
    )


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _arr(*items):
    return json.dumps(list(items), ensure_ascii=False)


def _item(vid, **kw):
    base = {
        "id": vid, "is_ai": True, "topic": "запуски и анонсы",
        "confidence": 0.9, "title_ru": f"новое описание {vid}",
        "summary_ru": "Коротко.", "lang": "ru", "reason": "смысл",
    }
    base.update(kw)
    return base


class ReclassSession:
    """Мок DeepSeek: отвечает по id из входного батча.

    fail_ids — id, на которых вызов падает (проверка устойчивости прогона).
    """

    def __init__(self, topic="запуски и анонсы", fail_ids=(), usage=None):
        self.topic = topic
        self.fail_ids = set(fail_ids)
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5}
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(json)
        user = json["messages"][-1]["content"]
        ids = re.findall(r'"id":\s*"([^"]+)"', user)
        if self.fail_ids.intersection(ids):
            raise RuntimeError("модель недоступна")
        content = _arr(*[_item(i, topic=self.topic) for i in ids])
        return _Resp({
            "choices": [{"message": {"content": content}}],
            "usage": dict(self.usage),
        })


# --- таксономия ------------------------------------------------------------


def test_topics_count_and_new_topics_present():
    assert len(config.TOPICS) == 17
    for topic in NEW_TOPICS:
        assert topic in config.TOPICS


def test_topics_no_duplicates_or_empty():
    assert len(set(config.TOPICS)) == len(config.TOPICS)
    assert all(t.strip() for t in config.TOPICS)


def test_prompt_contains_every_topic():
    for topic in config.TOPICS:
        assert topic in classify.SYSTEM_PROMPT


def test_prompt_contains_criteria_for_new_topics():
    for needle in (
        "только сами модели",
        "капитал и рынок",
        "визуал",
        "личный формат",
        "уроки, курсы",
    ):
        assert needle in classify.SYSTEM_PROMPT


# --- переразбор ------------------------------------------------------------


def test_reclassify_dry_run_does_not_call_model(conn):
    for i in range(3):
        _add_video(conn, f"v{i}")
        _classify(conn, f"v{i}", classified_at=100 + i)

    session = ReclassSession()
    res = classify.reclassify(conn, dry_run=True, session=session, api_key="k",
                             model=None)
    # _chat не зовётся: сессия пуста, вернулось только число кандидатов.
    assert session.posts == []
    assert res["selected"] == 3
    assert res["done"] == 0
    assert res["cost_usd"] == 0.0


def test_reclassify_limit_processes_exactly_limit(conn):
    for i in range(5):
        _add_video(conn, f"v{i}")
        _classify(conn, f"v{i}", topic="медиа и творчество", classified_at=100 + i)

    session = ReclassSession(topic="дизайн и креатив")
    res = classify.reclassify(conn, limit=3, session=session, api_key="k")

    assert res["selected"] == 3
    assert res["done"] == 3
    assert session.posts  # модель вызвана

    changed = conn.execute(
        "SELECT video_id FROM video_classification WHERE topic='дизайн и креатив'"
    ).fetchall()
    assert len(changed) == 3
    # Самые старые по classified_at: v0..v2.
    assert {r["video_id"] for r in changed} == {"v0", "v1", "v2"}
    # Обновлённые поля.
    row = conn.execute(
        "SELECT title_ru, model FROM video_classification WHERE video_id='v0'"
    ).fetchone()
    assert row["title_ru"] == "новое описание v0"
    assert row["model"] == classify.DEFAULT_MODEL


def test_reclassify_continues_from_limit(conn):
    """Идемпотентность: второй прогон берёт следующие по порядку видео."""
    for i in range(4):
        _add_video(conn, f"v{i}")
        _classify(conn, f"v{i}", topic="прочее", classified_at=100 + i)

    session = ReclassSession()
    first = classify.reclassify(conn, limit=2, session=session, api_key="k")
    second = classify.reclassify(conn, limit=2, session=session, api_key="k")

    assert first["done"] == 2
    assert second["done"] == 2
    rows = conn.execute(
        "SELECT video_id FROM video_classification WHERE topic='запуски и анонсы'"
    ).fetchall()
    assert {r["video_id"] for r in rows} == {"v0", "v1", "v2", "v3"}


def test_reclassify_skips_not_ai_by_default_and_includes_with_all(conn):
    _add_video(conn, "v_ai")
    _add_video(conn, "v_noai")
    _classify(conn, "v_ai", is_ai=1, classified_at=1)
    _classify(conn, "v_noai", is_ai=0, topic="прочее", classified_at=2)

    session = ReclassSession()
    default = classify.reclassify(conn, session=session, api_key="k")
    assert default["selected"] == 1
    assert session.posts[-1]["messages"][-1]["content"].find("v_noai") == -1

    row = conn.execute(
        "SELECT topic FROM video_classification WHERE video_id='v_noai'"
    ).fetchone()
    assert row["topic"] == "прочее"  # не ИИ-видео не тронули

    everything = classify.reclassify(
        conn, only_ai=False, session=session, api_key="k"
    )
    assert everything["selected"] == 2
    row = conn.execute(
        "SELECT topic FROM video_classification WHERE video_id='v_noai'"
    ).fetchone()
    assert row["topic"] == "запуски и анонсы"


def test_topics_added_nonempty_then_empty(conn):
    # Эмулируем состояние до прогона: новых тем в справочнике ещё нет.
    conn.executemany(
        "DELETE FROM topics WHERE name = ?", [(t,) for t in NEW_TOPICS]
    )
    conn.commit()
    _add_video(conn, "v0")
    _classify(conn, "v0", classified_at=1)

    first = classify.reclassify(conn, dry_run=True)
    assert set(NEW_TOPICS).issubset(set(first["topics_added"]))

    second = classify.reclassify(conn, dry_run=True)
    assert second["topics_added"] == []


def test_model_error_counted_but_run_continues(conn, monkeypatch):
    monkeypatch.setattr(classify, "BATCH_SIZE", 1)
    for i in range(3):
        _add_video(conn, f"v{i}")
        _classify(conn, f"v{i}", topic="прочее", classified_at=100 + i)

    session = ReclassSession(fail_ids={"v1"})
    res = classify.reclassify(conn, session=session, api_key="k")

    assert res["errors"] == 1
    assert res["done"] == 2
    # Сбойное видео не переразобрано: попало в skipped, но не остановило прогон.
    assert res["skipped"] == 1
    assert res["done"] + res["skipped"] == res["selected"] == 3
    # Сбойное видео сохранило старый разбор (переразбор его не портит).
    row = conn.execute(
        "SELECT topic FROM video_classification WHERE video_id='v1'"
    ).fetchone()
    assert row["topic"] == "прочее"
    # Остальные обновлены.
    rows = conn.execute(
        "SELECT video_id FROM video_classification WHERE topic='запуски и анонсы'"
    ).fetchall()
    assert {r["video_id"] for r in rows} == {"v0", "v2"}


# --- CLI -------------------------------------------------------------------


def _sandbox(monkeypatch, tmp_path):
    """Изолированные пути CLI, чтобы не трогать боевую БД."""
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "tuber.db")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "tuber.log")
    monkeypatch.setattr(cli.config, "load_env", lambda *a, **k: 0)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    return tmp_path / "tuber.db"


def _seed_db(path, is_ai=1):
    c = db.connect(path)
    db.init_db(c)
    c.executemany("DELETE FROM topics WHERE name=?", [(t,) for t in NEW_TOPICS])
    c.commit()
    db.upsert_channel(c, {"channel_id": "c1", "title": "K", "first_seen": 1})
    db.upsert_video(c, {"video_id": "v1", "channel_id": "c1", "title": "v1",
                        "description": "d", "duration_seconds": 600,
                        "published_at": 1, "first_seen": 1})
    db.save_classification(c, "v1", is_ai=is_ai, topic="прочее",
                           classified_at=1)
    c.close()


def test_cli_reclassify_dry_run_text(monkeypatch, tmp_path, capsys):
    path = _sandbox(monkeypatch, tmp_path)
    _seed_db(path)

    assert cli.main(["classify", "--reclassify", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "переразбор: обработано 0 из 1" in out
    assert "ошибок 0" in out
    assert "новые темы:" in out
    assert "запуски и анонсы" in out
    assert "$0.0" in out


def test_cli_reclassify_full_run_and_all_ai(monkeypatch, tmp_path, capsys):
    path = _sandbox(monkeypatch, tmp_path)
    _seed_db(path, is_ai=0)

    def fake_chat(api_key, messages, session, model, timeout=60.0):
        user = messages[-1]["content"]
        ids = re.findall(r'"id":\s*"([^"]+)"', user)
        return _arr(*[_item(i) for i in ids]), {"prompt_tokens": 10,
                                                "completion_tokens": 5}

    monkeypatch.setattr(classify, "_chat", fake_chat)

    # Без --all-ai не ИИ-видео не переразбирается.
    assert cli.main(["classify", "--reclassify"]) == 0
    assert "обработано 0 из 0" in capsys.readouterr().out

    # С --all-ai видео попадает в переразбор и получает новую тему.
    assert cli.main(["classify", "--reclassify", "--all-ai", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["selected"] == 1
    assert payload["done"] == 1
    assert payload["topics"] == {"запуски и анонсы": 1}
