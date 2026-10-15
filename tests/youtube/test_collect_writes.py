"""ТЗ-6: тесты не должны затирать боевое состояние обхода (``data/*.json``).

Сторож: снимаем слепок всех файлов ``data/*.json`` (набор, sha256, mtime_ns) до
прогона ``run_collect`` и сверяем после. Реестр подтверждённых ИИ-каналов в
тестовой базе непуст — значит фаза 1 обхода uploads доходит до ``save_cursor``
(именно там была утечка: тесты писали в боевой
``data/uploads_rotation.json`` и сбрасывали курсор обхода в 0).

До изоляции тестов этот тест падает; после — проходит. Дополнительно
``_write_rotation_state`` отказывается писать в ``<repo>/data/`` под pytest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tests.youtube.test_collect import _mark_ai
from tuber.platforms.youtube import collect, config
from tuber.platforms.youtube import store as db

# Боевой каталог состояния (не tmp_path): именно его нельзя трогать тестам.
DATA_DIR = Path(config.TUBER_DIR) / "data"


class RecordingClient:
    """Клиент YouTube-заглушка: в сеть не ходит, только фиксирует вызовы."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def search_videos(self, query, published_after=None, order="viewCount", max_pages=1):
        self.calls.append(("search", query))
        return []

    def videos_by_ids(self, ids, parts="snippet,statistics,contentDetails"):
        self.calls.append(("videos", tuple(ids)))
        return []

    def channels_by_ids(self, ids, parts="snippet,statistics,contentDetails"):
        self.calls.append(("channels", tuple(ids)))
        return []

    def playlist_items(self, playlist_id, max_results=50):
        self.calls.append(("playlist", playlist_id))
        return []


def _snapshot() -> dict[str, tuple[str, int]]:
    """Слепок ``data/*.json``: имя -> (sha256, mtime_ns)."""
    out: dict[str, tuple[str, int]] = {}
    for path in sorted(DATA_DIR.glob("*.json")):
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[path.name] = (digest, stat.st_mtime_ns)
    return out


def test_run_collect_does_not_touch_production_state(tmp_path):
    conn = db.connect(tmp_path / "writes.db")
    db.init_db(conn)
    try:
        # Непустой реестр подтверждённых ИИ-каналов: без него фаза 1 не дошла
        # бы до save_cursor и утечка не воспроизвелась.
        _mark_ai(conn, "seed1", "ch1")
        _mark_ai(conn, "seed2", "ch1")

        before = _snapshot()
        collect.run_collect(conn, config, client=RecordingClient(), classify=False)
        after = _snapshot()
    finally:
        conn.close()

    assert set(after) == set(before), (
        "набор файлов data/*.json изменился тестом:\n"
        f"было: {sorted(before)}\nстало: {sorted(after)}"
    )
    changed = sorted(name for name in before if before[name] != after[name])
    assert not changed, (
        "тест перезаписал боевые файлы состояния: "
        f"{changed}\nдо:   {[before[n] for n in changed]}\n"
        f"после: {[after[n] for n in changed]}"
    )
