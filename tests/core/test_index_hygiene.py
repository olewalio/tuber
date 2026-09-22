"""ТЗ-3d: гигиена индексов на таблицах ядра.

Дефект (находка заказчика): адаптеры X и YouTube создавали индексы на таблицах
ЯДРА в обход ядрового DDL. Часть из них — точные дубли или строгие
префикс-дубли ядровых индексов; ``idx_x_metric_ext (platform, external_id)``
(строгий префикс ``idx_metric_ext``) перехватывает план: планировщик берёт
адаптерный индекс вместо ядрового.

Здесь три уровня защиты:

1. ``duplicate_index_report`` — детектор дублей/префиксов по ВСЕМ таблицам ядра
   (ловит новую платформу, которая внесла дубль молча);
2. точный ожидаемый набор индексов каждого адаптера после ``migrate``;
3. идемпотентность чистки уже собранных баз.
"""

from __future__ import annotations

import sqlite3

import pytest

from tuber.core import db, schema
from tuber.platforms.x import store as x_store
from tuber.platforms.youtube import store as yt_store

# Индексы, которые адаптеры X/YouTube создают НА ТАБЛИЦАХ ЯДРА.
# Всё остальное на таблицах ядра — ядровое (или автоиндекс UNIQUE/PK).
EXPECTED_X_INDEXES = (
    "idx_x_cache_at",
    "idx_x_cache_status",
    "idx_x_content_post",
    "idx_x_req_kind",
    "idx_x_req_ts",
    "idx_x_score_story",
    "idx_x_story_pub",
    "idx_x_story_xconf",
)

EXPECTED_YOUTUBE_INDEXES = (
    "idx_youtube_content_videos",
    "idx_youtube_metric_quality",
    "idx_youtube_score_vpd",
)


def _indexes(conn: sqlite3.Connection) -> dict[str, dict[str, tuple[str, ...]]]:
    """Явно созданные индексы, таблица → {имя: список колонок}."""
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, table in conn.execute(
        "SELECT name, tbl_name FROM sqlite_master "
        "WHERE type='index' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex%'"
    ).fetchall():
        cols = tuple(r[2] for r in conn.execute(f"PRAGMA main.index_info({name})").fetchall())
        out.setdefault(table, {})[name] = cols
    return out


def _fresh_with_adapters(tmp_path) -> sqlite3.Connection:
    """Ядро + слой совместимости обоих адаптеров на ОДНОЙ базе."""
    path = str(tmp_path / "hygiene.db")
    conn = db.connect(path)
    schema.migrate_schema(conn)
    conn.commit()
    conn.close()
    x_store.connect(path).close()
    yt_store.connect(path).close()
    return db.connect(path)


# --------------------------------------------------------------------------
# 1. Детектор на все таблицы ядра
# --------------------------------------------------------------------------

def test_no_duplicate_or_prefix_indexes_on_core_tables(tmp_path):
    """Ни на одной таблице ядра нет точных дублей и префикс-дублей индексов."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        assert schema.duplicate_index_report(conn) == []
    finally:
        conn.close()


def test_detector_actually_detects_injected_duplicate(tmp_path):
    """Негативный контроль: детектор НЕ вакуумный — он видит внесённый дубль."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        assert schema.duplicate_index_report(conn) == []
        conn.execute("CREATE INDEX idx_injected_dup ON metric_snapshot(platform, external_id)")
        problems = schema.duplicate_index_report(conn)
        assert any("idx_injected_dup" in p for p in problems), problems
    finally:
        conn.close()


def test_detector_detects_injected_core_prefix(tmp_path):
    """Внесённый префикс ядрового индекса тоже виден детектору."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        conn.execute("CREATE INDEX idx_injected_pref ON content(platform, external_id)")
        problems = schema.duplicate_index_report(conn)
        assert any("idx_injected_pref" in p for p in problems), problems
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 2. Точный набор индексов адаптеров
# --------------------------------------------------------------------------

def test_x_adapter_index_set_is_exact(tmp_path):
    conn = _fresh_with_adapters(tmp_path)
    try:
        actual = tuple(sorted(
            name for table, idx in _indexes(conn).items() for name in idx
            if name.startswith("idx_x_")
        ))
        assert actual == EXPECTED_X_INDEXES
    finally:
        conn.close()


def test_youtube_adapter_index_set_is_exact(tmp_path):
    conn = _fresh_with_adapters(tmp_path)
    try:
        actual = tuple(sorted(
            name for table, idx in _indexes(conn).items() for name in idx
            if name.startswith("idx_youtube_")
        ))
        assert actual == EXPECTED_YOUTUBE_INDEXES
    finally:
        conn.close()


def test_core_owns_source_ext_index(tmp_path):
    """``source(platform, external_id)`` индексирует ядро, а не адаптеры."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        assert "idx_source_ext" in schema.REQUIRED_INDEXES
        by_table = _indexes(conn)
        assert "idx_source_ext" in by_table.get("source", {})
        assert by_table["source"]["idx_source_ext"] == ("platform", "external_id")
        # Ни один адаптерный индекс не должен претендовать на эту пару.
        assert "idx_x_source_ext" not in by_table.get("source", {})
        assert "idx_youtube_source_ext" not in by_table.get("source", {})
    finally:
        conn.close()


def test_adapter_indexes_do_not_duplicate_any_existing_index(tmp_path):
    """Индекс адаптера не дублирует и не префиксует НИЧЕГО (включая автоиндексы).

    Это и есть правило владения ТЗ-3d: адаптер создаёт индекс только на том,
    чего у ядра нет. Автоиндексы ``UNIQUE``/``PK`` удалить нельзя, поэтому
    адаптерный индекс, чей список колонок совпадает или является префиксом
    любого (в т.ч. авто-)индекса, — ошибка.
    """
    conn = _fresh_with_adapters(tmp_path)
    try:
        all_idx: dict[str, dict[str, tuple[str, ...]]] = {}
        for name, table in conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index'"
        ).fetchall():
            cols = tuple(
                r[2] for r in conn.execute(f"PRAGMA main.index_info({name})").fetchall()
            )
            all_idx.setdefault(table, {})[name] = cols

        adapter_prefixes = ("idx_x_", "idx_youtube_")
        for table, idx in all_idx.items():
            for name, cols in idx.items():
                if not name.startswith(adapter_prefixes):
                    continue
                for other, ocols in idx.items():
                    if other == name:
                        continue
                    assert cols != ocols, f"{name} дублирует {other} ({list(cols)})"
                    assert not (len(cols) < len(ocols) and ocols[:len(cols)] == cols), (
                        f"{name} {list(cols)} — префикс {other} {list(ocols)}"
                    )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 3. Идемпотентная чистка уже собранных баз
# --------------------------------------------------------------------------

LEGACY_ADAPTER_INDEXES = (
    "CREATE INDEX idx_x_metric_ext ON metric_snapshot(platform, external_id)",
    "CREATE INDEX idx_x_source_ext ON source(platform, external_id)",
    "CREATE INDEX idx_youtube_source_ext ON source(platform, external_id)",
    "CREATE INDEX idx_x_source_handle ON source(platform, handle)",
    "CREATE INDEX idx_x_member_story ON story_member(story_id)",
    "CREATE INDEX idx_x_score_sig ON score(platform, significance)",
    "CREATE INDEX idx_x_content_hash ON content(platform, text_hash)",
    "CREATE INDEX idx_x_content_pub ON content(platform, is_repost, published_at)",
    "CREATE INDEX idx_x_content_author ON content(platform, author_handle)",
)


def test_migrate_drops_legacy_adapter_indexes_idempotently(tmp_path):
    path = str(tmp_path / "old.db")
    conn = db.connect(path)
    schema.migrate_schema(conn)
    conn.commit()
    conn.close()

    # Имитируем базу, собранную старой ревизией.
    raw = sqlite3.connect(path)
    for stmt in LEGACY_ADAPTER_INDEXES:
        raw.execute(stmt)
    raw.commit()
    raw.close()

    from tuber.tools.migrate_legacy import migrate

    stats = migrate(path, vacuum=False)
    assert sorted(stats.dropped_indexes) == sorted(
        [s.split()[2] for s in LEGACY_ADAPTER_INDEXES]
    )

    conn = db.connect(path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        for stmt in LEGACY_ADAPTER_INDEXES:
            assert stmt.split()[2] not in names
        assert schema.duplicate_index_report(conn) == []
    finally:
        conn.close()

    # Повторный прогон ничего не меняет.
    stats2 = migrate(path, vacuum=False)
    assert stats2.dropped_indexes == []


def test_cleanup_is_registered_in_schema_report(tmp_path):
    """``migrate_schema`` возвращает список снятых индексов (для сводки migrate)."""
    path = str(tmp_path / "rep.db")
    conn = db.connect(path)
    schema.migrate_schema(conn)
    conn.commit()
    conn.execute("CREATE INDEX idx_x_metric_ext ON metric_snapshot(platform, external_id)")
    report = schema.migrate_schema(conn)
    assert report["dropped_indexes"] == ["idx_x_metric_ext"]
    assert schema.migrate_schema(conn)["dropped_indexes"] == []
    conn.close()


# --------------------------------------------------------------------------
# 4. Страж: молча дубль не проходит (главное требование ТЗ-3d)
# --------------------------------------------------------------------------

def test_guard_skips_index_when_core_has_equivalent(tmp_path):
    """Эквивалент уже есть у ядра → адаптер индекс НЕ создаёт (берём дубль)."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        before = set(schema.table_indexes(conn, "score"))
        created = schema.ensure_adapter_index(
            conn, "idx_new_score_ext", "score", ("platform", "external_id", "computed_at"))
        assert created is False, "индекс-эквивалент ядрового должен быть пропущен"
        assert set(schema.table_indexes(conn, "score")) == before
    finally:
        conn.close()


def test_guard_skips_index_equivalent_to_autoinindex(tmp_path):
    """Автоиндекс ``UNIQUE``/``PK`` тоже считается эквивалентом."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        # story_member: PRIMARY KEY(story_id, content_id) — неудаляемый автоиндекс.
        created = schema.ensure_adapter_index(
            conn, "idx_new_member_pk", "story_member", ("story_id", "content_id"))
        assert created is False
    finally:
        conn.close()


def test_guard_raises_on_prefix_of_core_index(tmp_path):
    """Узкий префикс ядрового индекса — громкая ошибка (это и был дефект ТЗ-3d)."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        with pytest.raises(schema.DuplicateIndexError) as exc:
            schema.ensure_adapter_index(
                conn, "idx_bad_metric_ext", "metric_snapshot", ("platform", "external_id"))
        assert "idx_metric_ext" in str(exc.value)
    finally:
        conn.close()


def test_guard_raises_when_requested_index_covers_core_index(tmp_path):
    """Обратная сторона: запрашиваемый индекс шире ядрового — тоже ошибка."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        with pytest.raises(schema.DuplicateIndexError):
            schema.ensure_adapter_index(
                conn, "idx_bad_wide", "metric_snapshot",
                ("platform", "external_id", "captured_at", "interval_quality"))
    finally:
        conn.close()


def test_guard_creates_index_when_no_conflict(tmp_path):
    conn = _fresh_with_adapters(tmp_path)
    try:
        created = schema.ensure_adapter_index(
            conn, "idx_new_content_repost", "content", ("is_repost",))
        assert created is True
        assert schema.index_columns(conn, "idx_new_content_repost") == ("is_repost",)
    finally:
        conn.close()


def test_guard_ignores_platform_tables_outside_core(tmp_path):
    """На платформенной (не ядровой) таблице страж не мешает — она ничья не ядро."""
    conn = db.connect(str(tmp_path / "plat.db"))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS x_only (a INTEGER, b INTEGER)")
        assert schema.ensure_adapter_index(conn, "idx_x_only_ab", "x_only", ("a",)) is True
        # Не ядровая таблица: проверок нет (префикс-полиция действует только на ядре).
        assert schema.ensure_adapter_index(conn, "idx_x_only_a", "x_only", ("a",)) is True
    finally:
        conn.close()


def test_new_platform_cannot_silently_add_duplicate(tmp_path):
    """Новая платформа с дублем падает на первом же соединении, а не молчит."""
    conn = _fresh_with_adapters(tmp_path)
    try:
        # Так выглядел бы адаптер ещё одной платформы, скопировавший старую
        # ревизию X/YouTube. ПЕРВАЯ группа — то, что обязано падать ГРОМКО:
        # узкий префикс ядрового индекса (ровно дефект ТЗ-3d: перехват плана) и
        # индекс шире ядрового (ядровой становится лишним).
        loud = [
            ("idx_tg_metric_ext", "metric_snapshot", ("platform", "external_id")),
            ("idx_tg_metric_content_wide", "metric_snapshot",
             ("content_id", "captured_at", "interval_quality")),
            ("idx_tg_source_handle_ext", "source", ("platform",)),
        ]
        for name, table, cols in loud:
            with pytest.raises(schema.DuplicateIndexError):
                schema.ensure_adapter_index(conn, name, table, cols)

        # ВТОРАЯ группа — эквивалент уже есть у ядра (или у неудаляемого
        # автоиндекса): индекс просто НЕ создаётся, а не падает.
        silent = [
            ("idx_tg_source_ext", "source", ("platform", "external_id")),
            ("idx_tg_score_sig", "score", ("significance",)),
            ("idx_tg_member_story", "story_member", ("story_id",)),
        ]
        for name, table, cols in silent:
            assert schema.ensure_adapter_index(conn, name, table, cols) is False

        # Ничего не создано — «молча» в базу не попало.
        assert schema.duplicate_index_report(conn) == []
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert not [n for n in names if n.startswith("idx_tg_")]
    finally:
        conn.close()


def test_adapter_connect_is_loud_when_core_gained_narrower_index(tmp_path):
    """Реальный путь соединения адаптера: конфликт с ядром валит connect()."""
    path = str(tmp_path / "loud.db")
    conn = db.connect(path)
    schema.migrate_schema(conn)
    conn.commit()
    # Ядро обзавелось узким индексом, который делает индекс адаптера перекрытием.
    conn.execute("CREATE INDEX idx_core_content_platform ON content(platform)")
    conn.commit()
    conn.close()

    with pytest.raises(schema.DuplicateIndexError):
        x_store.connect(path).close()
    with pytest.raises(schema.DuplicateIndexError):
        yt_store.connect(path).close()


def test_adapters_have_no_raw_index_ddl_in_compat_statements():
    """Обход стража невозможен: в наборах совместимости нет ``CREATE INDEX``.

    Индексы адаптеров объявлены декларативно (``_COMPAT_INDEXES``) и создаются
    только через ``ensure_adapter_index`` — так новая платформа не сможет
    протащить дубль «сырым» SQL мимо детектора.
    """
    for module in (x_store, yt_store):
        raw = [s for s in module._COMPAT_STATEMENTS
               if s.strip().upper().startswith(("CREATE INDEX", "CREATE UNIQUE INDEX"))]
        assert raw == [], f"{module.__name__}: сырой DDL индексов в _COMPAT_STATEMENTS"
        assert module._COMPAT_INDEXES, f"{module.__name__}: пустой список индексов"


# --------------------------------------------------------------------------
# 5. Сводка ``migrate --schema-only`` показывает сделанную работу
# --------------------------------------------------------------------------

def _aged_base(path) -> None:
    """Боеподобная «состаренная» база: снят ядровой индекс, достроен адаптерный."""
    conn = db.connect(path)
    schema.migrate_schema(conn)
    conn.commit()
    conn.execute("DROP INDEX idx_metric_ext")
    conn.execute("DROP INDEX idx_score_ext")
    conn.execute("DROP TRIGGER trg_metric_snapshot_denorm_ins")
    # Ровно тот дефект, из-за которого появился ТЗ-3d.
    conn.execute("CREATE INDEX idx_x_metric_ext ON metric_snapshot(platform, external_id)")
    conn.commit()
    conn.close()


def test_schema_only_summary_reports_work_not_cleanliness(tmp_path):
    """Сводка schema-only печатает работу и результат, а не «база уже чистая»."""
    from tuber.tools.migrate_legacy import format_summary, migrate_schema_only

    path = str(tmp_path / "aged.db")
    _aged_base(path)

    stats = migrate_schema_only(path, analyze=False)
    text = format_summary(stats)

    assert stats.created_indexes and "idx_metric_ext" in stats.created_indexes
    assert "idx_score_ext" in stats.created_indexes
    assert stats.created_triggers == ["trg_metric_snapshot_denorm_ins"]
    assert stats.dropped_indexes == ["idx_x_metric_ext"]
    assert "до 3, после 0" in text, text
    assert "idx_x_metric_ext" in text
    assert "снято индексов 1" in text
    assert "база уже чистая" not in text
    # Сняли 1 индекс, поэтому строки «лишних нет» быть не должно.
    assert "Лишних индексов на таблицах ядра нет" not in text


def test_schema_only_summary_second_run_says_nothing_to_do(tmp_path):
    """Повторный прогон: работа нулевая и это видно в сводке."""
    from tuber.tools.migrate_legacy import format_summary, migrate_schema_only

    path = str(tmp_path / "aged.db")
    _aged_base(path)
    migrate_schema_only(path, analyze=False)

    stats = migrate_schema_only(path, analyze=False)
    text = format_summary(stats)

    assert stats.created_indexes == []
    assert stats.dropped_indexes == []
    assert stats.schema_problems_before == []
    assert stats.schema_problems_after == []
    assert "Схема уже актуальна (делать нечего)." in text, text
    assert "Лишних индексов на таблицах ядра нет (проверка дублей пройдена)." in text
    assert "Проблем схемы ядра: до 0, после 0." in text

