"""Фикстуры тестов графа (ТЗ-8)."""
from __future__ import annotations

import pytest

from tuber.core import db, schema


@pytest.fixture
def con(tmp_path):
    conn = db.connect(str(tmp_path / "graph.db"))
    schema.init_schema(conn)
    yield conn
    conn.close()


def add_source(con, sid, platform, handle, tier=None, status="active"):
    con.execute(
        "INSERT INTO source(id, platform, handle, status, tier) VALUES (?,?,?,?,?)",
        (sid, platform, handle, status, tier))
    con.commit()
    return sid


def add_content(con, cid, platform, source_id, external_id, links=None, mentions=None,
                published="2026-09-01 00:00:00", author_handle=None, meta_json=None):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at, links,"
        " mentions, author_handle, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, platform, source_id, external_id, published, links, mentions, author_handle,
         meta_json))
    con.commit()
    return cid
