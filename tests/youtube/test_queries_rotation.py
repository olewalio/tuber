"""Тесты пула запросов и ротации порций (tuber.config / tuber.collect).

Сеть не используется: YouTube-клиент — простой мок, БД и файл курсора живут
во временной папке.
"""

from __future__ import annotations

import json
import re

import pytest

from tuber.platforms.youtube import collect, config, store as db

# --- обязательные темы (заказчик) -----------------------------------------

MANDATORY_TOPIC_PARTS = ("дизайн", "стартап", "запуск", "влог")


def _norm(query: str) -> str:
    """Нормализация для поиска дублей: регистр, пробелы, ё -> е."""
    text = str(query).strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text)


# --- фикстуры --------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "rotation_test.db")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def rot_file(tmp_path, monkeypatch):
    """Изолированный файл курсора ротации (не трогаем боевой data/)."""
    path = tmp_path / "query_rotation.json"
    monkeypatch.setattr(config, "QUERY_ROTATION_FILE", str(path))
    return path


class RecordingClient:
    """Мок tuber.yt.YouTubeClient: пишет запросы и отдаёт пустую выдачу."""

    def __init__(self):
        self.queries: list[str] = []

    def search_videos(self, query, **kwargs):
        self.queries.append(query)
        return []


# --- 1-4. структура пула ---------------------------------------------------


def test_search_queries_pool_is_large_enough():
    assert len(config.SEARCH_QUERIES) >= 130


def test_search_queries_have_no_duplicates_after_normalization():
    normalized = [_norm(q) for q in config.SEARCH_QUERIES]
    assert len(normalized) == len(set(normalized))


def test_search_queries_are_non_empty_and_long_enough():
    for query in config.SEARCH_QUERIES:
        assert str(query).strip() != ""
        assert len(str(query).strip()) >= 3


def test_channel_search_queries_size_and_uniqueness():
    assert len(config.CHANNEL_SEARCH_QUERIES) >= 18
    normalized = [_norm(q) for q in config.CHANNEL_SEARCH_QUERIES]
    assert len(normalized) == len(set(normalized))


# --- 5-7. карта покрытия ---------------------------------------------------


def test_every_topic_group_has_at_least_three_queries():
    for topic, queries in config.QUERY_TOPIC_MAP.items():
        assert len(queries) >= 3, f"тема «{topic}» слишком маленькая"


def test_topic_map_matches_pool_exactly_without_repeats():
    flat = [q for group in config.QUERY_TOPIC_MAP.values() for q in group]
    assert len(flat) == len(set(flat)), "запрос встречается больше одного раза"
    assert set(flat) == set(config.SEARCH_QUERIES), (
        "объединение групп не совпадает с пулом запросов"
    )
    assert len(flat) == len(config.SEARCH_QUERIES), "размеры карты и пула разошлись"


def test_mandatory_topics_are_present():
    topics = list(config.QUERY_TOPIC_MAP)
    for part in MANDATORY_TOPIC_PARTS:
        assert any(part in topic for topic in topics), f"нет темы со словом «{part}»"


# --- 8. rotation_slice -----------------------------------------------------


def test_rotation_slice_basic_window_from_middle():
    pool = ["a", "b", "c", "d", "e", "f"]
    assert collect.rotation_slice(pool, 3, 2) == ["c", "d", "e"]


def test_rotation_slice_wraps_across_the_end():
    pool = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    assert collect.rotation_slice(pool, 5, 8) == ["i", "j", "a", "b", "c"]


def test_rotation_slice_size_larger_than_pool_returns_whole_pool():
    pool = ["a", "b", "c"]
    assert collect.rotation_slice(pool, 10, 0) == pool


def test_rotation_slice_cursor_zero_starts_at_beginning():
    pool = ["a", "b", "c", "d"]
    assert collect.rotation_slice(pool, 2, 0) == ["a", "b"]


def test_rotation_slice_handles_empty_and_bad_size():
    assert collect.rotation_slice([], 5, 0) == []
    assert collect.rotation_slice(["a", "b"], 0, 0) == []


# --- 9. полный цикл ротации ------------------------------------------------


def test_full_cycle_of_unit_portions_covers_pool_without_gaps_or_repeats():
    pool = list(config.SEARCH_QUERIES)
    seen: list[str] = []
    for cursor in range(len(pool)):
        seen.extend(collect.rotation_slice(pool, 1, cursor))
    assert seen == pool  # ни пропусков, ни повторов


def test_full_cycle_of_equal_portions_tiles_whole_pool():
    pool = [f"q{i}" for i in range(20)]
    collected: list[str] = []
    for cursor in range(0, len(pool), 5):
        collected.extend(collect.rotation_slice(pool, 5, cursor))
    assert collected == pool
    assert len(set(collected)) == len(pool)


# --- 10-11. load_cursor / save_cursor --------------------------------------


def test_load_cursor_returns_zero_for_missing_file(tmp_path):
    assert collect.load_cursor(tmp_path / "nope.json", 100) == 0


def test_load_cursor_returns_zero_for_broken_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{не json", encoding="utf-8")
    assert collect.load_cursor(path, 100) == 0


def test_load_cursor_returns_zero_for_negative_value(tmp_path):
    path = tmp_path / "neg.json"
    path.write_text(json.dumps({"cursor": -3}), encoding="utf-8")
    assert collect.load_cursor(path, 100) == 0


def test_load_cursor_returns_zero_for_cursor_beyond_pool(tmp_path):
    path = tmp_path / "big.json"
    path.write_text(json.dumps({"cursor": 101}), encoding="utf-8")
    assert collect.load_cursor(path, 100) == 0


def test_load_cursor_returns_zero_for_non_numeric(tmp_path):
    path = tmp_path / "text.json"
    path.write_text(json.dumps({"cursor": "много"}), encoding="utf-8")
    assert collect.load_cursor(path, 100) == 0


def test_save_then_load_cursor_round_trip(tmp_path):
    path = tmp_path / "rot.json"
    collect.save_cursor(path, 7, 100)
    assert collect.load_cursor(path, 100) == 7
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cursor"] == 7
    assert payload["full_len"] == 100
    assert "updated_at" in payload


def test_save_cursor_wraps_value_into_range(tmp_path):
    path = tmp_path / "rot.json"
    collect.save_cursor(path, 130, 100)
    assert collect.load_cursor(path, 100) == 30


# --- 12-13. ротация в run_collect ------------------------------------------


def test_run_collect_rotates_portions_and_advances_cursor(conn, rot_file):
    first = RecordingClient()
    res1 = collect.run_collect(conn, config, client=first, classify=False)
    assert res1["rotation"]["enabled"] is True
    assert res1["rotation"]["cursor_before"] == 0
    assert res1["rotation"]["cursor_after"] == len(first.queries)

    second = RecordingClient()
    res2 = collect.run_collect(conn, config, client=second, classify=False)
    assert second.queries != first.queries, "порции двух прогонов совпали"
    assert res2["rotation"]["cursor_before"] == res1["rotation"]["cursor_after"]

    pool = list(config.SEARCH_QUERIES)
    size = config.COLLECT_QUERIES_PER_RUN
    assert first.queries == pool[:size]
    assert second.queries == pool[size:size * 2]

    stored = json.loads(rot_file.read_text(encoding="utf-8"))
    assert stored["cursor"] == res2["rotation"]["cursor_after"]


class FailingClient:
    """Все поиски падают, но слот ротации всё равно расходуется."""

    def __init__(self):
        self.calls = 0

    def search_videos(self, query, **kwargs):
        self.calls += 1
        raise collect.yt.YouTubeError(500, {"error": {}}, "search")


def test_run_collect_advances_rotation_past_failed_searches(conn, rot_file):
    client = FailingClient()
    res = collect.run_collect(conn, config, client=client, classify=False)
    assert res["queries_done"] == 0
    assert res["errors"]
    assert res["rotation"]["cursor_before"] == 0
    # Несмотря на сбои, окно сдвинулось на длину порции.
    assert res["rotation"]["cursor_after"] == config.COLLECT_QUERIES_PER_RUN


def test_run_collect_explicit_queries_do_not_touch_rotation(conn, rot_file):
    collect.save_cursor(rot_file, 5, len(config.SEARCH_QUERIES))
    client = RecordingClient()
    res = collect.run_collect(
        conn, config, queries=["only-one", "only-two"],
        client=client, classify=False,
    )
    assert client.queries == ["only-one", "only-two"]
    assert res["rotation"]["enabled"] is False
    # Курсор остался прежним.
    assert collect.load_cursor(rot_file, len(config.SEARCH_QUERIES)) == 5
