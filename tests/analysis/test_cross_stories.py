"""Тесты сквозных сюжетов (связывание материалов разных платформ).

Требования ТЗ: минимум три регрессионных теста, включая обратный (разные
события из разных платформ НЕ склеиваются) и тест на окно времени (старое и
новое не связываются). Дополнительно проверяем идемпотентность/детерминизм и
неизменность X-сюжетов. Сеть и LLM не используются.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tuber.analysis import cross_stories
from tuber.core import db, schema

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)

_A_TEXT = ("SkyVenture @skyventure launches the Qwen vision model with open "
           "weights today, benchmark results and deployment details inside the "
           "release notes")

# content.id синтетического ядра.
A_X, A_TG, A_YT = 1, 2, 3      # одно событие, три платформы
B_TG = 4                       # ДРУГОЕ событие, но та же якорная сущность
C_X = 5                        # тот же текст, но СТАРОЕ (вне time-gate)
_X_STORY, _X_MEMBER = 6, 7     # «чужой» X-сюжет, который трогать нельзя


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def con(tmp_path):
    conn = db.connect(str(tmp_path / "core.db"))
    schema.init_schema(conn)
    conn.execute("INSERT INTO source(id, platform, external_id, handle)"
                 " VALUES (1,'x','sx','sky'),(2,'telegram','stg','chan'),"
                 "(3,'youtube','syt','ytchan')")
    # 60 классифицированных X-постов образуют контекст правила (IDF/«гиганты»).
    for i in range(60):
        cid = 1000 + i
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "text) VALUES (?, 'x', 1, ?, ?, ?)",
            (cid, f"ctx{i}", _iso(NOW - timedelta(hours=10)),
             f"AI research note number {i} about generic machine topics and results"))
        conn.execute("INSERT INTO classification(content_id,is_ai) VALUES (?,1)",
                     (cid,))
    conn.execute(
        "INSERT INTO content(id,platform,source_id,external_id,published_at,text,"
        "mentions) VALUES"
        " (?, 'x', 1, 'ax', ?, ?, '[\"skyventure\"]'),"
        " (?, 'telegram', 2, 'chan/1', ?, ?, '[\"skyventure\"]'),"
        " (?, 'youtube', 3, 'ytA', ?, '', '[\"skyventure\"]')",
        (A_X, _iso(NOW - timedelta(hours=2)), _A_TEXT,
         A_TG, _iso(NOW - timedelta(hours=1)), _A_TEXT,
         A_YT, _iso(NOW - timedelta(hours=3))))
    # YouTube несёт событие в заголовке.
    conn.execute("UPDATE content SET title=? WHERE id=?", (_A_TEXT, A_YT))
    conn.execute(
        "INSERT INTO content(id,platform,source_id,external_id,published_at,text,"
        "mentions) VALUES (?, 'telegram', 2, 'chan/2', ?, ?, '[\"skyventure\"]')",
        (B_TG, _iso(NOW - timedelta(hours=1)),
         "SkyVenture @skyventure quarterly earnings call scheduled for next month "
         "in Berlin with analysts and investors"))
    conn.execute(
        "INSERT INTO content(id,platform,source_id,external_id,published_at,text,"
        "mentions) VALUES (?, 'x', 1, 'cx', ?, ?, '[\"skyventure\"]')",
        (C_X, _iso(NOW - timedelta(hours=60)), _A_TEXT))
    # Чужой X-сюжет (старый контур): сквозной прогон не должен его менять.
    conn.execute("INSERT INTO story(id,platform,title) VALUES (?,'x','чужой сюжет')",
                 (_X_STORY,))
    conn.execute("INSERT INTO story_member(story_id,content_id,handle)"
                 " VALUES (?,?, 'sky')", (_X_STORY, A_X))
    yield conn
    conn.close()


def _cross_members(conn):
    out = {}
    for st in cross_stories.cross_stories(conn):
        out[st["id"]] = {m["content_id"] for m in cross_stories.story_members(conn, st["id"])}
    return out


def test_cross_platform_same_event_is_linked(con):
    """Одно событие на X + Telegram + YouTube собирается в один сквозной сюжет."""
    s = cross_stories.run(con, now=NOW)
    assert s["stories"] == 1
    members = _cross_members(con)
    assert len(members) == 1
    only = next(iter(members.values()))
    assert only == {A_X, A_TG, A_YT}


def test_different_events_across_platforms_are_not_linked(con):
    """ОБРАТНЫЙ тест: разные события с общей сущностью не склеиваются."""
    cross_stories.run(con, now=NOW)
    members = _cross_members(con)
    assert members, "ожидался хотя бы один сквозной сюжет"
    joined = set().union(*members.values())
    assert B_TG not in joined, "разное событие с общей сущностью склеилось"


def test_old_and_new_are_not_linked(con):
    """ТЕСТ ОКНА: одинаковый текст за пределами STORY_TIME_GATE_HOURS не связывается."""
    cross_stories.run(con, now=NOW)
    members = _cross_members(con)
    joined = set().union(*members.values())
    assert C_X not in joined, "старый пост склеился с новым вне временного окна"


def test_idempotent_and_deterministic(con):
    """Повторный прогон даёт тот же состав и те же id."""
    first = cross_stories.run(con, now=NOW)
    snap1 = _cross_members(con)
    second = cross_stories.run(con, now=NOW)
    snap2 = _cross_members(con)
    assert first["stories"] == second["stories"] == 1
    assert snap1 == snap2


def test_dry_run_writes_nothing(con):
    before = con.execute("SELECT COUNT(*) FROM story").fetchone()[0]
    s = cross_stories.run(con, now=NOW, dry_run=True)
    assert s["dry_run"] is True and s["stories"] == 1
    assert con.execute("SELECT COUNT(*) FROM story").fetchone()[0] == before
    assert con.execute("SELECT COUNT(*) FROM story WHERE platform='cross'"
                       ).fetchone()[0] == 0


def test_existing_x_stories_untouched(con):
    """Сквозной прогон не переписывает сюжеты X (требование 2)."""
    cross_stories.run(con, now=NOW)
    row = con.execute("SELECT title FROM story WHERE id=?", (_X_STORY,)).fetchone()
    assert row is not None and row["title"] == "чужой сюжет"
    assert con.execute("SELECT COUNT(*) FROM story_member WHERE story_id=?",
                       (_X_STORY,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM story WHERE platform='x'"
                       ).fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Причины ложных склеек (ТЗ 17.09.2026): отдельные механизмы
# --------------------------------------------------------------------------- #
def _base_con(tmp_path, name):
    conn = db.connect(str(tmp_path / name))
    schema.init_schema(conn)
    conn.execute("INSERT INTO source(id, platform, external_id, handle)"
                 " VALUES (1,'x','sx','sky'),(2,'telegram','stg','chan'),"
                 "(3,'youtube','syt','ytchan')")
    for i in range(60):
        cid = 1000 + i
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "text) VALUES (?, 'x', 1, ?, ?, ?)",
            (cid, f"ctx{i}", _iso(NOW - timedelta(hours=10)),
             f"AI research note number {i} about generic machine topics and results"))
        conn.execute("INSERT INTO classification(content_id,is_ai) VALUES (?,1)", (cid,))
    return conn


#: Цепочка A~B~C, где A≁C: A и C разной платформы и почти не пересекаются.
_CHAIN = {
    # id: (platform, text)
    11: ("x", "acme alpha beta gamma delta epsilon zeta q1 q2 q3 q4 q5"),
    12: ("telegram", "acme alpha beta gamma delta epsilon zeta r1 r2 r3 r4 r5"),
    13: ("youtube", "acme eta theta iota kappa lambda mu s1 s2 s3 s4 s5"),
}


@pytest.fixture
def chain_con(tmp_path):
    conn = _base_con(tmp_path, "chain.db")
    for cid, (plat, text) in _CHAIN.items():
        sid = {"x": 1, "telegram": 2, "youtube": 3}[plat]
        conn.execute(
            "INSERT INTO content(id,platform,source_id,external_id,published_at,"
            "text,mentions) VALUES (?,?,?,?,?,?,?)",
            (cid, plat, sid, f"c{cid}", _iso(NOW - timedelta(hours=1)), text,
             '["acme"]'))
    yield conn
    conn.close()


def test_chain_a_b_c_is_not_one_story(chain_con):
    """Цепочка A~B и B~C при A≁C НЕ должна становиться одним сюжетом (страж цепи)."""
    cross_stories.run(chain_con, now=NOW)
    members = _cross_members(chain_con)
    groups = list(members.values())
    assert not any(11 in g and 13 in g for g in groups), \
        "A и C (разные платформы, A≁C) оказались в одном сюжете"
    assert any({11, 12} <= g for g in groups), "прямая пара A~B должна склеиться"


def test_short_post_sharing_one_word_does_not_link(tmp_path):
    """Короткий заголовок и длинный пост с одним общим словом не склеиваются.

    Прежний containment на этом давал text_part≈1.0 (короткий пост как знаменатель);
    симметричная мера устойчива к длине (ТЗ, причина 3).
    """
    conn = _base_con(tmp_path, "short.db")
    conn.execute(
        "INSERT INTO content(id,platform,source_id,external_id,published_at,text,"
        "mentions) VALUES (30,'youtube',3,'short','%s','Qwen model on apple silicon',"
        "'[\"qwen\"]')" % _iso(NOW - timedelta(hours=1)))
    conn.execute(
        "INSERT INTO content(id,platform,source_id,external_id,published_at,text,"
        "mentions) VALUES (31,'x',1,'long','%s',?, '[\"qwen\"]')"
        % _iso(NOW - timedelta(hours=1)),
        ("Qwen " + " ".join(f"w{i}" for i in range(120)),))
    cross_stories.run(conn, now=NOW)
    members = _cross_members(conn)
    joined = set().union(*members.values()) if members else set()
    assert 30 not in joined and 31 not in joined, \
        "короткий и длинный пост с одним общим словом склеились"
    conn.close()


def test_single_specific_entity_is_not_enough(tmp_path):
    """Одной общей значимой сущности недостаточно для склейки (ТЗ, причина 1)."""
    conn = _base_con(tmp_path, "ent.db")
    conn.execute(
        "INSERT INTO content(id,platform,source_id,external_id,published_at,text,"
        "mentions) VALUES (40,'telegram',2,'e1','%s',?, '[\"nvidia\"]')"
        % _iso(NOW - timedelta(hours=1)),
        ("nvidia quarterly earnings call scheduled in Berlin with analysts and "
         "investors tomorrow morning",))
    conn.execute(
        "INSERT INTO content(id,platform,source_id,external_id,published_at,text,"
        "mentions) VALUES (41,'x',1,'e2','%s',?, '[\"nvidia\"]')"
        % _iso(NOW - timedelta(hours=1)),
        ("nvidia unveils new GPU architecture for datacenters with redesigned "
         "memory subsystem and cooler design",))
    cross_stories.run(conn, now=NOW)
    members = _cross_members(conn)
    joined = set().union(*members.values()) if members else set()
    assert 40 not in joined and 41 not in joined, \
        "разные события с одной общей сущностью склеились"
    conn.close()
