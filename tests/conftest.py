"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

from tests.fixtures import build_legacy


@pytest.fixture
def legacy_paths(tmp_path):
    """Чистый набор синтетических legacy-баз (без битых строк)."""
    return build_legacy(tmp_path / "legacy", with_broken=False)


@pytest.fixture
def legacy_paths_broken(tmp_path):
    """Набор с битыми случаями (сироты, дубль handle, пост без просмотров)."""
    return build_legacy(tmp_path / "legacy_broken", with_broken=True)


@pytest.fixture
def target_db(tmp_path):
    """Путь к единой базе (ещё не созданной)."""
    return str(tmp_path / "tuber.db")


@pytest.fixture
def migrated(tmp_path, legacy_paths, target_db):
    """Готовая миграция чистого набора: отдаёт (target_path, stats)."""
    from tuber.tools.migrate_legacy import migrate

    stats = migrate(target_db, os_path=legacy_paths["os"], x_path=legacy_paths["x"],
                    tg_path=legacy_paths["tg"])
    return target_db, stats
