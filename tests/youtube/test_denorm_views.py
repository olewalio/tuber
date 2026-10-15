"""ТЗ-2c: однотабличные представления совместимости (закрытие долга D-22).

Долг D-22: представления `videos`/`snapshots`/`video_classification`/… были
двухтабличными (`*` JOIN `content` ради перевода `content_id` ⇄ `external_id`),
а SQLite разворачивает (flatten) подзапрос справа от `LEFT JOIN` только если он
ОДНОтабличный. Поэтому КАЖДЫЙ `LEFT JOIN` к такому представлению материализовал
его целиком (`MATERIALIZE snapshots` = 283 мс вместо `SEARCH` = 50 мс).

Что проверяется здесь:

* (а) представления однотабличные, и типовой `LEFT JOIN` не даёт `MATERIALIZE`;
* (б) денормализованные `platform`/`external_id` заполнены у ВСЕХ строк и
  совпадают с `content` (запрос-инвариант), плюс `content.source_external_id`;
* (в) миграция/бэкфилл идемпотентны;
* (г) запись через адаптер (включая сырой SQL через TEMP-представления)
  заполняет денормализацию — иначе новые строки потеряют связь.

Сеть не используется.
"""

from __future__ import annotations

import pytest

from tuber.core import db as core_db
from tuber.core import schema
from tuber.core import storage
from tuber.platforms.youtube import store as db

#: Представления совместимости, которые обязаны быть однотабличными.
SINGLE_TABLE_VIEWS = (
    "channels", "videos", "snapshots", "video_scores", "video_classification",
    "seo_fields", "video_comments", "comment_checks", "thumbnail_vision",
)

#: Таблицы ядра с денормализованным `platform` + `external_id`.
DENORM_TABLES = (
    "content_latest", "metric_snapshot", "classification", "score", "seo_field",
    "thumbnail_vision", "content_comment", "comment_check",
)

#: Типовые `LEFT JOIN` к представлениям из отчёта (report.py, comments.py).
LEFT_JOIN_QUERIES = (
    "SELECT v.video_id, s.views FROM videos v "
    "LEFT JOIN snapshots s ON s.video_id = v.video_id",
    "SELECT v.video_id FROM videos v "
    "LEFT JOIN video_classification vc ON vc.video_id = v.video_id",
    "SELECT v.video_id FROM videos v LEFT JOIN video_scores vs ON vs.video_id = v.video_id",
    "SELECT v.video_id FROM videos v LEFT JOIN seo_fields sf ON sf.video_id = v.video_id",
    "SELECT v.video_id FROM videos v LEFT JOIN video_comments vc ON vc.video_id = v.video_id",
    "SELECT v.video_id FROM videos v LEFT JOIN comment_checks ck ON ck.video_id = v.video_id",
    "SELECT v.video_id FROM videos v "
    "LEFT JOIN thumbnail_vision tv ON tv.video_id = v.video_id",
    "SELECT s.video_id FROM snapshots s JOIN videos v ON v.video_id = s.video_id",
)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "denorm.db")
    db.init_db(c)
    yield c
    c.close()


def _add_video(conn, video_id="v1", channel_id="c1", published_at=1_000_000, **extra):
    db.upsert_channel(conn, {"channel_id": channel_id, "title": "ch", "first_seen": 1})
    data = {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": "t",
        "published_at": published_at,
        "first_seen": 1,
    }
    data.update(extra)
    db.upsert_video(conn, data)


# --- (а) однотабличность ----------------------------------------------------

def _temp_view_sql(conn) -> dict[str, str]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_temp_master WHERE type='view'"
        ).fetchall()
    }


def test_compat_views_have_no_join(conn):
    """Представление совместимости читает РОВНО одну таблицу ядра."""
    sql = _temp_view_sql(conn)
    for name in SINGLE_TABLE_VIEWS:
        view = db.compat_view_name(name)
        assert view in sql, f"нет TEMP-представления {name}"
        assert " join " not in sql[view].lower(), f"{name} всё ещё многотабличное"


@pytest.mark.parametrize("query", LEFT_JOIN_QUERIES)
def test_left_join_has_no_materialize(conn, query):
    """Планировщик разворачивает представление, а не материализует его."""
    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert not any("MATERIALIZE" in step.upper() for step in plan), plan


def test_left_join_uses_denormalized_index(conn):
    """Развёрнутый `LEFT JOIN` берёт индекс по (platform, external_id)."""
    plan = [r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT s.video_id FROM videos v "
        "LEFT JOIN snapshots s ON s.video_id = v.video_id"
    )]
    assert any("idx_metric_ext" in step for step in plan), plan


# --- (г) запись через адаптер заполняет денормализацию ----------------------

def test_adapter_write_fills_denormalized(conn):
    _add_video(conn, video_id="v1", channel_id="c1")
    cid = conn.execute(
        "SELECT id FROM content WHERE platform='youtube' AND external_id='v1'"
    ).fetchone()[0]

    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 100, likes=5, comments=1)
    db.save_classification(conn, "v1", is_ai=1, topic="t")
    db.save_score(conn, "v1", 2_000_000, vpd=1.0)
    db.upsert_seo_field(conn, "v1", title_length=3)
    db.upsert_comment_check(conn, "v1", checked_at=2_000_000, status="ok")
    db.add_comments(conn, "v1", [{
        "comment_id": "cm1", "text": "hi", "published_at": 2_000_000,
    }])
    # Сырой SQL YouTube-кода идёт через TEMP-представления адаптера.
    conn.execute("INSERT INTO thumbnail_vision (video_id, model) VALUES ('v1', 'm')")
    db.set_viral_indices(conn, [("v1", 2_000_000, 1.5)])

    for table in ("metric_snapshot", "score", "classification", "seo_field",
                  "content_comment", "comment_check", "thumbnail_vision"):
        # `main.` — имя thumbnail_vision совпадает с TEMP-представлением.
        row = conn.execute(
            f"SELECT platform, external_id FROM main.{table} WHERE content_id = ?", (cid,)
        ).fetchone()
        assert row is not None, table
        assert row["platform"] == "youtube", table
        assert row["external_id"] == "v1", table

    # content.source_external_id заполнен от канала.
    assert conn.execute(
        "SELECT source_external_id FROM content WHERE id=?", (cid,)
    ).fetchone()[0] == "c1"

    # Чтение через представления не сломалось.
    assert conn.execute("SELECT video_id FROM snapshots").fetchone()[0] == "v1"
    assert conn.execute("SELECT video_id FROM thumbnail_vision").fetchone()[0] == "v1"


def test_content_latest_gets_denormalized(conn):
    _add_video(conn, video_id="v1", channel_id="c1")
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 100)
    cid = conn.execute("SELECT id FROM content WHERE external_id='v1'").fetchone()[0]
    with core_db.write_tx(conn):
        # content_latest пересчитывается кроном из metric_snapshot.
        storage.recompute_content_latest(conn, cid)
    row = conn.execute("SELECT platform, external_id FROM content_latest").fetchone()
    assert row is not None
    assert (row["platform"], row["external_id"]) == ("youtube", "v1")


# --- (б) инвариант на мигрированной базе ------------------------------------

def test_denormalized_matches_content_on_migrated_db(migrated):
    """После `tuber migrate` денормализация заполнена и совпадает с content."""
    target, _ = migrated
    conn = core_db.connect(target)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM content WHERE source_id IS NOT NULL "
            "AND source_external_id IS NOT (SELECT external_id FROM source WHERE id=content.source_id)"
        ).fetchone()[0] == 0
        for table in DENORM_TABLES:
            bad = conn.execute(
                f"SELECT COUNT(*) FROM main.{table} t JOIN content c ON c.id = t.content_id "
                f"WHERE t.platform IS NOT c.platform OR t.external_id IS NOT c.external_id"
            ).fetchone()[0]
            assert bad == 0, f"{table}: {bad} расхождений"
            nulls = conn.execute(
                f"SELECT COUNT(*) FROM main.{table} WHERE content_id IS NOT NULL "
                f"AND (external_id IS NULL OR platform IS NULL)"
            ).fetchone()[0]
            assert nulls == 0, f"{table}: {nulls} строк без external_id/platform"
        # Хотя бы где-то строки есть: фикстура наполняет снапшоты.
        assert conn.execute("SELECT COUNT(*) FROM metric_snapshot").fetchone()[0] > 0
    finally:
        conn.close()


def test_denormalized_is_filled_not_null(migrated):
    """Строки с content_id всегда имеют непустой external_id (D-22)."""
    target, _ = migrated
    conn = core_db.connect(target)
    try:
        for table in DENORM_TABLES:
            left = conn.execute(
                f"SELECT COUNT(*) FROM main.{table} t WHERE t.content_id IS NOT NULL "
                f"AND (SELECT external_id FROM content WHERE id = t.content_id) IS NOT t.external_id"
            ).fetchone()[0]
            assert left == 0, table
    finally:
        conn.close()


# --- (в) идемпотентность ----------------------------------------------------

def test_backfill_denormalized_is_idempotent(conn):
    _add_video(conn, video_id="v1", channel_id="c1")
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 100)

    with core_db.write_tx(conn):
        # Имитируем базу до ТЗ-2c: значения снесены, маркер версии отсутствует.
        conn.execute("DELETE FROM schema_meta WHERE key='denorm_version'")
        conn.execute("UPDATE metric_snapshot SET platform=NULL, external_id=NULL")
        conn.execute("UPDATE content SET source_external_id=NULL")

    first = schema.ensure_denormalized(conn)
    assert first["metric_snapshot"] == 1
    assert first["content.source_external_id"] == 1
    row = conn.execute("SELECT platform, external_id FROM metric_snapshot").fetchone()
    assert (row["platform"], row["external_id"]) == ("youtube", "v1")

    # Повторный прогон ничего не меняет (и не переписывает строки).
    assert schema.ensure_denormalized(conn) == {}
    assert schema.backfill_denormalized(conn) == {
        "content.source_external_id": 0, **{t: 0 for t in DENORM_TABLES}
    }


def test_migrate_schema_twice_reports_no_denorm(conn):
    with core_db.write_tx(conn):
        again = schema.migrate_schema(conn)
    assert again["denormalized"] == {}


def test_migrate_is_idempotent_for_denorm(tmp_path, legacy_paths):
    """Повторный `tuber migrate` не меняет денормализацию и не дублирует строки."""
    from tuber.tools import migrate_legacy

    target = str(tmp_path / "u.db")
    migrate_legacy.migrate(target, os_path=legacy_paths["os"], x_path=legacy_paths["x"],
                           tg_path=legacy_paths["tg"], analyze=False)
    conn = core_db.connect(target)
    try:
        before = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("content", "metric_snapshot", "score", "classification")
        }
        bad_before = _denorm_mismatches(conn)
    finally:
        conn.close()

    migrate_legacy.migrate(target, os_path=legacy_paths["os"], x_path=legacy_paths["x"],
                           tg_path=legacy_paths["tg"], analyze=False)
    conn = core_db.connect(target)
    try:
        after = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("content", "metric_snapshot", "score", "classification")
        }
        assert after == before
        assert _denorm_mismatches(conn) == bad_before == 0
        assert schema.migrate_schema(conn)["denormalized"] == {}
    finally:
        conn.close()


def _denorm_mismatches(conn) -> int:
    total = 0
    for table in DENORM_TABLES:
        total += conn.execute(
            f"SELECT COUNT(*) FROM main.{table} t JOIN content c ON c.id = t.content_id "
            f"WHERE t.platform IS NOT c.platform OR t.external_id IS NOT c.external_id"
        ).fetchone()[0]
    return total


# --- денормализация держится при смене content_id ---------------------------

def test_denormalized_follows_content_id_change(conn):
    """Перепривязка строки к другому content обновляет external_id."""
    _add_video(conn, video_id="v1", channel_id="c1")
    _add_video(conn, video_id="v2", channel_id="c1")
    db.insert_snapshot(conn, "v1", 2_000_000, "h0", 100)
    row = conn.execute("SELECT id, content_id FROM metric_snapshot").fetchone()
    snap_id, old_cid = row["id"], row["content_id"]
    new_cid = conn.execute(
        "SELECT id FROM content WHERE external_id='v2'").fetchone()[0]
    with core_db.write_tx(conn):
        conn.execute("UPDATE metric_snapshot SET content_id=? WHERE id=?",
                     (new_cid, snap_id))
    row = conn.execute(
        "SELECT platform, external_id FROM metric_snapshot WHERE id=?", (snap_id,)
    ).fetchone()
    assert old_cid != new_cid
    assert (row["platform"], row["external_id"]) == ("youtube", "v2")


def test_source_external_id_null_when_no_source(conn):
    """Видео без канала: source_external_id остаётся NULL, а не выдуман."""
    with core_db.write_tx(conn):
        conn.execute(
            "INSERT INTO content (platform, external_id, kind, published_at) "
            "VALUES ('youtube','nosrc','video','2026-01-01 00:00:00')"
        )
    row = conn.execute(
        "SELECT source_external_id FROM content WHERE external_id='nosrc'").fetchone()
    assert row["source_external_id"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM videos WHERE video_id='nosrc' AND channel_id IS NULL"
    ).fetchone()[0] == 1


# --- домиграция уже заполненной базы (v2 → v3, порядок DDL) ------------------

def test_migrate_schema_upgrades_populated_db_in_place(tmp_path):
    """Старая база без денормализованных колонок доводится ``migrate_schema``.

    Регресс-тест на порядок DDL: индексы/триггеры денормализации ссылаются на
    новые колонки, поэтому колонки обязаны добавляться ДО ``init_schema``.
    Иначе на заполненной базе апгрейд падал с
    ``no such column: source_external_id``.
    """
    c = db.connect(tmp_path / "old.db")
    db.init_db(c)
    _add_video(c, video_id="v1", channel_id="c1")
    db.insert_snapshot(c, "v1", 2_000_000, "h0", 100)

    with core_db.write_tx(c):
        c.execute("DELETE FROM schema_meta WHERE key='denorm_version'")
        # TEMP-представления адаптера тоже ссылаются на новые колонки — снимаем.
        for (name,) in c.execute(
                "SELECT name FROM sqlite_temp_master WHERE type='view'").fetchall():
            c.execute(f"DROP VIEW IF EXISTS temp.{name}")
        # «Откатываем» ТЗ-2c: снимаем представления, индексы, триггеры и колонки.
        for name in schema.REQUIRED_VIEWS:
            c.execute(f"DROP VIEW IF EXISTS {name}")
        for (name,) in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_autoindex%'").fetchall():
            c.execute(f"DROP INDEX IF EXISTS main.{name}")
        for name in schema.REQUIRED_TRIGGERS:
            c.execute(f"DROP TRIGGER IF EXISTS {name}")
        for table, cols in (
            ("metric_snapshot", ("platform", "external_id")),
            ("score", ("platform", "external_id")),
            ("classification", ("platform", "external_id")),
            ("content_latest", ("platform", "external_id")),
            ("seo_field", ("platform", "external_id")),
            ("thumbnail_vision", ("platform", "external_id")),
            ("content_comment", ("external_id",)),
            ("comment_check", ("platform", "external_id")),
            ("content", ("source_external_id",)),
        ):
            for col in cols:
                c.execute(f"ALTER TABLE main.{table} DROP COLUMN {col}")

    result = schema.migrate_schema(c)
    assert "metric_snapshot.platform" in result["added_columns"]
    assert "content.source_external_id" in result["added_columns"]
    assert result["denormalized"]["metric_snapshot"] == 1
    assert schema.verify_schema(c) == []

    row = c.execute(
        "SELECT platform, external_id FROM main.metric_snapshot").fetchone()
    assert (row["platform"], row["external_id"]) == ("youtube", "v1")
    assert c.execute(
        "SELECT source_external_id FROM content").fetchone()[0] == "c1"
    # Идемпотентно: второй прогон ничего не делает.
    assert schema.migrate_schema(c)["denormalized"] == {}
    c.close()
