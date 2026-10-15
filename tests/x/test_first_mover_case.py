"""Зачин сюжета: регистронезависимое разрешение автора (ТЗ 2026-10-03).

X-сюжеты пишутся вставкой в TEMP-представление ``stories``; связь зачина с
реестром делал триггер точным ``src.handle = NEW.first_mover``. ``source.handle``
хранится в нижнем регистре, а ``story_member.handle``/``first_mover`` — в
исходном, поэтому отчёт печатал ``Первый: @None`` там, где автор известен.

Тесты фиксируют: (а) другой регистр разрешается; (б) ведущая ``@`` терпится;
(в) автора нет в ``source`` — представление отдаёт handle из состава сюжета;
(г) пустой состав не ломает представление; (д) при дублях приоритет — точное
совпадение, затем меньший ``id``.
"""
from __future__ import annotations

from tuber.platforms.x import store as db


def _source(con, handle, *, external_id=None):
    con.execute("INSERT INTO source(platform, handle, external_id) VALUES('x',?,?)",
                (handle, external_id or handle.lower()))
    con.commit()
    return con.execute("SELECT id FROM source WHERE platform='x' AND handle=?",
                       (handle,)).fetchone()["id"]


def _story(con, *, first_mover, tid="t1", published="2026-10-03T10:00:00"):
    con.execute(
        """INSERT INTO stories (created_at, window_hours, threshold, first_tweet_id,
             first_mover, published_at, xconf, post_count, lead_time_min, topics,
             entities, is_new_entity, is_single, suspect, claimed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?)""",
        (published.replace("T", " "), 24, 1, tid, first_mover, published,
         2, 2, 1, "[]", "[]", published.replace("T", " ")))
    sid = db.last_insert_id(con)
    con.commit()
    return sid


def _resolved(con, sid):
    return con.execute("SELECT first_mover_source_id FROM story WHERE id=?",
                       (sid,)).fetchone()["first_mover_source_id"]


def _view_fm(con, sid):
    return con.execute("SELECT first_mover FROM stories WHERE id=?",
                       (sid,)).fetchone()["first_mover"]


def test_trigger_resolves_handle_other_case(con):
    """(а) Триггер вставки разрешает handle в другом регистре."""
    src_id = _source(con, "insilicomeds")
    sid = _story(con, first_mover="InSilicoMeds")
    assert _resolved(con, sid) == src_id
    assert _view_fm(con, sid) == "insilicomeds"


def test_trigger_update_resolves_handle_other_case(con):
    """(а2) Триггер обновления тоже регистронезависим."""
    src_id = _source(con, "aiquanting")
    sid = _story(con, first_mover=None)
    con.execute("UPDATE stories SET first_mover=? WHERE id=?",
                ("AIQuanting", sid))
    con.commit()
    assert _resolved(con, sid) == src_id


def test_trigger_tolerates_leading_at(con):
    """(б) Ведущая ``@`` в NEW.first_mover не мешает разрешению."""
    src_id = _source(con, "iponewsroom_")
    sid = _story(con, first_mover="@IPONewsroom_")
    assert _resolved(con, sid) == src_id


def test_trigger_tolerates_leading_at_in_source(con):
    """(б2) Ведущая ``@`` в source.handle тоже терпится."""
    src_id = _source(con, "@someauthor")
    sid = _story(con, first_mover="SomeAuthor")
    assert _resolved(con, sid) == src_id


def test_view_falls_back_to_story_member(con):
    """(в) Автора нет в source — представление отдаёт handle из состава."""
    sid = _story(con, first_mover="Ghost_Author")
    # source для Ghost_Author не заведён: триггер оставляет NULL.
    assert _resolved(con, sid) is None
    con.execute(
        "INSERT INTO story_posts (story_id, tweet_id, handle, role, added_at)"
        " VALUES (?,?,?,?,?)", (sid, "t1", "Ghost_Author", "primary",
                                "2026-10-03 10:00:00"))
    con.commit()
    assert _view_fm(con, sid) == "Ghost_Author"


def test_view_fallback_prefers_primary_and_smallest_content(con):
    """(в2) Запасной путь берёт primary с меньшим content_id."""
    sid = _story(con, first_mover=None)
    for tid, role in (("e1", "echo"), ("p1", "primary"), ("p2", "primary")):
        con.execute(
            "INSERT INTO story_posts (story_id, tweet_id, handle, role, added_at)"
            " VALUES (?,?,?,?,?)", (sid, tid, f"H_{tid}", role,
                                    "2026-10-03 10:00:00"))
    con.commit()
    assert _view_fm(con, sid) == "H_p1"


def test_empty_story_does_not_break_view(con):
    """(г) Пустой состав сюжета: first_mover = NULL, представление живо."""
    sid = _story(con, first_mover=None)
    row = con.execute("SELECT first_mover FROM stories WHERE id=?",
                      (sid,)).fetchone()
    assert row["first_mover"] is None


def test_duplicate_case_priority_exact_then_smallest_id(con):
    """(д) При дублях (разный регистр) — точное совпадение, затем меньший id."""
    first = _source(con, "ab")      # меньший id
    exact = _source(con, "AB")      # точное совпадение, но больший id
    sid = _story(con, first_mover="AB")
    assert _resolved(con, sid) == exact
    assert exact != first


def test_patch_tool_restores_recoverable_and_is_idempotent(con):
    """Латка: план находит NULL-зачин с автором в source, запись идемпотентна."""
    from tools.fix_first_mover_case import plan, apply_pending

    src_id = _source(con, "insilicomeds")
    # Сюжет собран «до фикса»: автор в составе есть, source есть, а связь пуста.
    sid = _story(con, first_mover=None)
    con.execute(
        "INSERT INTO story_posts (story_id, tweet_id, handle, role, added_at)"
        " VALUES (?,?,?,?,?)", (sid, "t1", "InSilicoMeds", "primary",
                                "2026-10-03 10:00:00"))
    con.commit()

    pending = [(s, h, i) for s, h, i in plan(con) if s == sid]
    assert pending == [(sid, "InSilicoMeds", src_id)]

    path = con.execute("PRAGMA database_list").fetchone()[2]
    assert apply_pending(path, pending) == 1
    assert _resolved(con, sid) == src_id
    # Повторный прогон не находит кандидатов (идемпотентность).
    assert [s for s, _h, _i in plan(con) if s == sid] == []


def test_patch_tool_skips_author_absent_from_source(con):
    """Латка не выдумывает связь, если автора нет в source."""
    from tools.fix_first_mover_case import plan

    sid = _story(con, first_mover=None)
    con.execute(
        "INSERT INTO story_posts (story_id, tweet_id, handle, role, added_at)"
        " VALUES (?,?,?,?,?)", (sid, "t1", "Ghost_Author", "primary",
                                "2026-10-03 10:00:00"))
    con.commit()
    assert [s for s, _h, _i in plan(con) if s == sid] == []
