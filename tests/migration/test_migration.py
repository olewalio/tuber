"""Тесты миграции на синтетических legacy-базах (ТЗ-1 §7)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tuber.core import db, schema
from tuber.tools import migrate_legacy


@pytest.fixture
def migrated_clean(target_db, legacy_paths):
    stats = migrate_legacy.migrate(
        target_db, os_path=legacy_paths["os"], x_path=legacy_paths["x"], tg_path=legacy_paths["tg"]
    )
    return db.connect(target_db), stats


def test_schema_created_after_migration(migrated_clean):
    conn, _ = migrated_clean
    try:
        assert schema.verify_schema(conn) == []
    finally:
        conn.close()


def test_counts_per_platform(migrated_clean):
    conn, _ = migrated_clean
    try:
        counts = dict(conn.execute(
            "SELECT platform, COUNT(*) FROM content GROUP BY platform").fetchall())
        assert counts == {"youtube": 3, "x": 3, "telegram": 3}
        src = dict(conn.execute(
            "SELECT platform, COUNT(*) FROM source GROUP BY platform").fetchall())
        assert src == {"youtube": 2, "x": 2, "telegram": 2}
    finally:
        conn.close()


def test_os_shorts_flag_and_kind(migrated_clean):
    conn, _ = migrated_clean
    try:
        row = conn.execute(
            "SELECT kind, is_short FROM content WHERE platform='youtube' AND external_id='v2'"
        ).fetchone()
        assert row["kind"] == "short" and row["is_short"] == 1
        row = conn.execute(
            "SELECT kind, is_short FROM content WHERE platform='youtube' AND external_id='v1'"
        ).fetchone()
        assert row["kind"] == "video" and row["is_short"] == 0
    finally:
        conn.close()


def test_os_empty_topic_becomes_null(migrated_clean):
    conn, _ = migrated_clean
    try:
        row = conn.execute(
            "SELECT cl.topic FROM classification cl JOIN content c ON c.id=cl.content_id "
            "WHERE c.platform='youtube' AND c.external_id='v3'").fetchone()
        assert row["topic"] is None
    finally:
        conn.close()


def test_legacy_map_populated(migrated_clean):
    conn, _ = migrated_clean
    try:
        assert conn.execute("SELECT COUNT(*) FROM legacy_map").fetchone()[0] > 0
        # old video_id → content.id (target_id TEXT, читаем через storage-хелпер).
        from tuber.core import storage
        mapped = storage.get_legacy_map(conn, "os", "videos", "v1")
        cid = conn.execute(
            "SELECT id FROM content WHERE platform='youtube' AND external_id='v1'").fetchone()[0]
        assert mapped == cid
    finally:
        conn.close()


def test_x_post_author_is_real_author_not_feed_owner(migrated_clean):
    conn, _ = migrated_clean
    try:
        row = conn.execute(
            "SELECT c.author_handle, c.source_id, s.handle AS owner, c.meta_json "
            "FROM content c JOIN source s ON s.id=c.source_id "
            "WHERE c.platform='x' AND c.external_id='t1'").fetchone()
        # Владелец ленты — feedowner, реальный автор поста — realauthor.
        assert row["owner"] == "feedowner"
        assert row["author_handle"] == "realauthor"
        meta = json.loads(row["meta_json"])
        assert meta["owner_handle"] == "someoneelse"  # не используется как автор
    finally:
        conn.close()


def test_tg_composite_external_id_and_missing_views(migrated_clean):
    conn, _ = migrated_clean
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT external_id FROM content WHERE platform='telegram' ORDER BY external_id")]
        assert "chan1/100" in ids and "chan2/100" in ids  # одинаковый message_id, разные каналы
        assert len(set(ids)) == 3
        # Пост chan2/100 без просмотров: снапшот есть, views = NULL.
        row = conn.execute(
            "SELECT m.views FROM metric_snapshot m JOIN content c ON c.id=m.content_id "
            "WHERE c.platform='telegram' AND c.external_id='chan2/100'").fetchone()
        assert row is not None and row["views"] is None
    finally:
        conn.close()


def test_x_story_membership(migrated_clean):
    conn, _ = migrated_clean
    try:
        stories = conn.execute(
            "SELECT COUNT(*) FROM story WHERE platform='x'").fetchone()[0]
        members = conn.execute(
            "SELECT COUNT(*) FROM story_member sm JOIN story s ON s.id=sm.story_id "
            "WHERE s.platform='x'").fetchone()[0]
        assert stories == 1 and members == 2
    finally:
        conn.close()


def test_idempotent_second_run(migrated_clean, legacy_paths, target_db):
    conn, _ = migrated_clean
    try:
        tables = ["source", "content", "metric_snapshot", "score", "classification",
                  "classify_cache", "story", "story_member", "candidate", "legacy_map",
                  "run", "run_log", "transport_request", "transport_instance", "llm_usage",
                  "seo_field", "thumbnail_vision", "content_comment", "comment_check",
                  "quota_usage", "metrics_daily", "report_text", "source_baseline", "topic",
                  "cursor", "classify_daily"]
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        migrate_legacy.migrate(
            target_db, os_path=legacy_paths["os"], x_path=legacy_paths["x"], tg_path=legacy_paths["tg"]
        )
        after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        assert before == after
    finally:
        conn.close()


def test_broken_rows_are_skipped_and_counted(legacy_paths_broken, target_db):
    stats = migrate_legacy.migrate(
        target_db, os_path=legacy_paths_broken["os"], x_path=legacy_paths_broken["x"],
        tg_path=legacy_paths_broken["tg"],
    )
    assert stats.skipped[("os", "snapshots")] == 1
    assert stats.skipped[("x", "post_metrics_history")] == 1
    # D-05: теперь переносятся ВСЕ курсоры (account и search), пропусков нет.
    assert stats.skipped[("x", "cursors")] == 0
    conn = db.connect(target_db)
    try:
        # Сиротские строки не создали контента сверх валидных.
        assert conn.execute(
            "SELECT COUNT(*) FROM content WHERE platform='youtube'").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM metric_snapshot m JOIN content c ON c.id=m.content_id "
            "WHERE c.platform='youtube'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM cursor WHERE platform='x'").fetchone()[0] == 2
    finally:
        conn.close()


def test_duplicate_handles_do_not_collapse_candidates(migrated_clean):
    """В legacy три channel_candidate, два из них с handle='dupe' — в ядре три."""
    conn, _ = migrated_clean
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM candidate WHERE platform='youtube' AND kind='channel'"
        ).fetchone()[0]
        assert n == 3
    finally:
        conn.close()


def test_legacy_db_is_read_only(tmp_path, legacy_paths):
    from tuber.core import legacy

    conn = legacy.open_legacy(legacy_paths["os"])
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO topics (name) VALUES ('x')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE videos SET title='x'")
    finally:
        conn.close()


def test_migration_does_not_touch_legacy_files(legacy_paths, target_db):
    import os

    before = {k: (os.path.getmtime(p), os.path.getsize(p)) for k, p in legacy_paths.items()}
    migrate_legacy.migrate(target_db, os_path=legacy_paths["os"], x_path=legacy_paths["x"],
                           tg_path=legacy_paths["tg"])
    after = {k: (os.path.getmtime(p), os.path.getsize(p)) for k, p in legacy_paths.items()}
    assert before == after


def test_cli_migrate(tmp_path, legacy_paths):
    target = str(tmp_path / "cli.db")
    rc = migrate_legacy.main([
        "--target", target, "--os", legacy_paths["os"], "--x", legacy_paths["x"],
        "--tg", legacy_paths["tg"],
    ])
    assert rc == 0
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM content").fetchone()[0] == 9
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ТЗ-1b: закрытие долгов D-01..D-06
# ---------------------------------------------------------------------------

def test_d03_classification_ru_fields_migrated(migrated_clean):
    """D-03: title_ru/summary_ru/reason доезжают один-в-один."""
    conn, _ = migrated_clean
    try:
        row = conn.execute(
            "SELECT cl.title_ru, cl.summary_ru, cl.reason "
            "FROM classification cl JOIN content c ON c.id=cl.content_id "
            "WHERE c.platform='youtube' AND c.external_id='v1'").fetchone()
        assert row["title_ru"] == "Заголовок RU"
        assert row["summary_ru"] == "Резюме RU"
        assert row["reason"] == "причина"
        # v3: title_ru/summary_ru в legacy NULL — остаются NULL, reason переносится.
        row3 = conn.execute(
            "SELECT cl.title_ru, cl.reason FROM classification cl "
            "JOIN content c ON c.id=cl.content_id "
            "WHERE c.platform='youtube' AND c.external_id='v3'").fetchone()
        assert row3["title_ru"] is None and row3["reason"] == "причина v3"
    finally:
        conn.close()


def test_d05_all_x_cursors_migrated(migrated_clean):
    """D-05: в cursor попадают и account-, и search-курсоры."""
    conn, _ = migrated_clean
    try:
        rows = {
            (r["kind"], r["ref"]): r["cursor"]
            for r in conn.execute("SELECT kind, ref, cursor FROM cursor WHERE platform='x'")
        }
        assert rows == {("account", "feedowner"): "CUR1", ("search", "q"): "CUR2"}
        # account-курсор по-прежнему доступен в source.cursor (не сломали).
        cur = conn.execute(
            "SELECT cursor FROM source WHERE platform='x' AND handle='feedowner'").fetchone()[0]
        assert cur == "CUR1"
    finally:
        conn.close()


def test_d04_classify_daily_migrated(migrated_clean):
    """D-04: дневная статистика классификации X перенесена."""
    conn, _ = migrated_clean
    try:
        row = conn.execute(
            "SELECT * FROM classify_daily WHERE platform='x'").fetchone()
        assert row["day"] == "2026-09-14"
        assert row["posts"] == 3 and row["model_calls"] == 1
        assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 50
        assert row["cost_usd"] == pytest.approx(0.01)
    finally:
        conn.close()


def test_d01_display_handle_normalized(migrated_clean):
    """D-01: display_handle — человекочитаемый handle без ведущего @."""
    conn, _ = migrated_clean
    try:
        rows = dict(conn.execute(
            "SELECT handle, display_handle FROM candidate WHERE platform='youtube' "
            "AND kind='channel'").fetchall())
        # handle-ключ = channel_id; display_handle — человекочитаемый handle.
        # В legacy у двух кандидатов handle='dupe' при разных channel_id.
        assert rows["@dupe"] == "dupe"
        assert rows["@dupe2"] == "dupe"
        assert rows["UCX"] is None  # пустой handle legacy → NULL
        # found_in_handle для YouTube-каналов больше не дублирует display_handle.
        found = conn.execute(
            "SELECT found_in_handle FROM candidate WHERE platform='youtube' AND handle='@dupe'"
        ).fetchone()[0]
        assert found is None
    finally:
        conn.close()


def test_d06_candidate_meta_json(migrated_clean):
    """D-06: остатки X-полей — в candidate.meta_json, а не sources_json."""
    conn, _ = migrated_clean
    try:
        row = conn.execute(
            "SELECT meta_json, sources_json FROM candidate WHERE platform='x' "
            "AND handle='candx'").fetchone()
        meta = json.loads(row["meta_json"])
        assert meta["verified_at"] is not None
        assert row["sources_json"] is None
        # OS-канал: остатки в meta_json, sources_json пуст.
        os_row = conn.execute(
            "SELECT meta_json, sources_json FROM candidate WHERE platform='youtube' "
            "AND handle='@dupe'").fetchone()
        assert json.loads(os_row["meta_json"])["evidence"] == "video:v2"
        assert os_row["sources_json"] is None
    finally:
        conn.close()


def test_d02_quota_legacy_map_composite_key_is_text(migrated_clean):
    """D-02: target_id — TEXT, реальный составной ключ (platform|key_id|day|endpoint)."""
    conn, _ = migrated_clean
    try:
        col_type = {r[1]: (r[2] or "").upper()
                    for r in conn.execute("PRAGMA table_info(legacy_map)")}
        assert col_type["target_id"] == "TEXT"
        vals = [r[0] for r in conn.execute(
            "SELECT target_id FROM legacy_map WHERE legacy_db='os' AND legacy_table='quota_log'")]
        assert vals, "quota_log должен быть в legacy_map"
        assert all(v.startswith("youtube|") and v.count("|") == 3 for v in vals)
        assert any(v.endswith("|videos/probe") for v in vals)
        assert "0" not in vals
    finally:
        conn.close()


def test_domigration_fills_new_columns_on_second_run(migrated_clean, legacy_paths, target_db):
    """Домерживание: обнуляем новые поля → повторный прогон их заполняет."""
    conn, _ = migrated_clean
    try:
        with db.write_tx(conn):
            conn.execute("UPDATE classification SET title_ru=NULL, summary_ru=NULL, reason=NULL")
            conn.execute("UPDATE candidate SET display_handle=NULL, meta_json=NULL")
            conn.execute("DELETE FROM cursor")
            conn.execute("DELETE FROM classify_daily")
    finally:
        conn.close()

    migrate_legacy.migrate(
        target_db, os_path=legacy_paths["os"], x_path=legacy_paths["x"], tg_path=legacy_paths["tg"]
    )

    conn = db.connect(target_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM classification WHERE title_ru IS NOT NULL").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM classification WHERE summary_ru IS NOT NULL").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM classification WHERE reason IS NOT NULL").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM cursor WHERE platform='x'").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM classify_daily WHERE platform='x'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM candidate WHERE display_handle IS NOT NULL").fetchone()[0] == 3
    finally:
        conn.close()


def test_migrate_schema_upgrades_v1_style_db(tmp_path):
    """Идемпотентная домиграция: v1-база без новых колонок получает их."""
    conn = db.connect(str(tmp_path / "old.db"))
    try:
        # Полная схема, из которой «откатили» изменения ТЗ-1b: убрали новые
        # колонки и вернули legacy_map.target_id INTEGER, как в версии 1.
        schema.init_schema(conn)
        conn.execute("ALTER TABLE classification DROP COLUMN title_ru")
        conn.execute("ALTER TABLE classification DROP COLUMN summary_ru")
        conn.execute("ALTER TABLE classification DROP COLUMN reason")
        conn.execute("ALTER TABLE candidate DROP COLUMN display_handle")
        conn.execute("ALTER TABLE candidate DROP COLUMN meta_json")
        conn.execute("DROP TABLE legacy_map")
        conn.execute(
            "CREATE TABLE legacy_map (legacy_db TEXT, legacy_table TEXT, legacy_id TEXT, "
            "target_table TEXT, target_id INTEGER NOT NULL, migrated_at TEXT, "
            "PRIMARY KEY (legacy_db, legacy_table, legacy_id))")
        with db.write_tx(conn):
            conn.execute(
                "INSERT INTO legacy_map VALUES ('os','videos','v1','content',5,'2026-01-01')")

        with db.write_tx(conn):
            result = schema.migrate_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(classification)")}
        assert {"title_ru", "summary_ru", "reason"} <= cols
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(candidate)")}
        assert {"display_handle", "meta_json"} <= ccols
        assert result["legacy_map_rebuilt"] is True
        info = {r[1]: (r[2] or "").upper() for r in conn.execute("PRAGMA table_info(legacy_map)")}
        assert info["target_id"] == "TEXT"
        # Данные legacy_map сохранены при пересборке.
        assert conn.execute(
            "SELECT target_id FROM legacy_map WHERE legacy_id='v1'").fetchone()[0] == "5"
        # Повторная домиграция ничего не меняет.
        with db.write_tx(conn):
            again = schema.migrate_schema(conn)
        assert again["added_columns"] == []
        assert again["legacy_map_rebuilt"] is False
        assert again["denormalized"] == {}
        # Бэкфилл run_log.platform (D-24) на повторе тоже ничего не меняет.
        assert again["run_log_platform"]["by_run"] == 0
        assert again["run_log_platform"]["by_legacy_map"] == 0
        assert again["run_log_platform"]["remaining"] == 0
        assert schema.verify_schema(conn) == []
    finally:
        conn.close()
