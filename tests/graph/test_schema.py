"""ТЗ-8 §1: слой рёбер в схеме ядра и код платформы web."""
from __future__ import annotations

from tuber.core import schema


def test_schema_version_six_and_edge_table(con):
    assert schema.SCHEMA_VERSION == "6"
    assert schema.verify_schema(con) == []
    cols = {r[1] for r in con.execute("PRAGMA table_info(edge)")}
    for col in ("from_platform", "from_source_id", "from_content_id", "from_handle",
                "kind", "target_type", "target_platform", "target_value", "target_url",
                "weight", "evidence", "origin", "competitor", "first_seen_at",
                "last_seen_at", "seen_count"):
        assert col in cols, col


def test_platform_web_seeded(con):
    row = con.execute("SELECT title FROM platform WHERE code='web'").fetchone()
    assert row is not None and "RSS" in row[0]


def test_no_duplicate_indexes_after_edge(con):
    assert schema.duplicate_index_report(con) == []
