"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

from tests.fixtures import build_legacy


@pytest.fixture(autouse=True)
def isolate_rotation_state(tmp_path, monkeypatch):
    """ТЗ-6: увести ВСЕ файловые состояния ротации в tmp_path.

    Тесты не должны писать в боевые ``data/*.json``: прогон ронял курсор
    обхода uploads в 0 (файл ``data/uploads_rotation.json`` перезаписывался
    тестовым состоянием), из-за чего каждый боевой прогон обходил одну и ту
    же голову реестра.

    Подменяем и пути в модуле ``config`` (их видят тесты, передающие ``config``
    как cfg), и ``_DEFAULT``-константы ``collect`` (откат, если атрибута
    конфига нет). ``save_cursor``/``save_query_stats`` дополнительно защищены
    предохранителем в ``collect._write_rotation_state``.
    """
    from tuber.platforms.youtube import collect
    from tuber.platforms.youtube import config as yt_config

    query_path = tmp_path / "query_rotation.json"
    uploads_path = tmp_path / "uploads_rotation.json"
    monkeypatch.setattr(yt_config, "QUERY_ROTATION_FILE", str(query_path), raising=False)
    monkeypatch.setattr(yt_config, "UPLOADS_ROTATION_FILE", str(uploads_path), raising=False)
    monkeypatch.setattr(collect, "_ROTATION_FILE_DEFAULT", str(query_path), raising=False)
    monkeypatch.setattr(
        collect, "_UPLOADS_ROTATION_FILE_DEFAULT", str(uploads_path), raising=False
    )
    return {"query": query_path, "uploads": uploads_path}


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
