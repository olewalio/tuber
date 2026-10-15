"""Обход ленты X не тратит запрос на резолв имени (ТЗ).

Проверяем точечно:
* при известном ``external_id`` обход стоит ровно один ``UserTweets``;
* при неизвестном — резолв (точечная задача) + ``UserTweets``, после чего id
  записан в ``source.external_id`` и повторный обход снова один запрос;
* ``known_user_id`` не принимает мусор, ``remember_user_id`` не перетирает.

Внешняя сеть не используется: ``FakeX`` из ``tests/x/test_session_transport``.
"""
from __future__ import annotations

from tests.x.test_session_transport import (
    FakeX, mk_session, profile_payload, timeline_payload, tweet,
)
from tuber.platforms.x import store as db


def _add_source(con, handle, external_id=None):
    """Строка реестра X (без неё ``remember_user_id`` тихо пропускает)."""
    con.execute(
        "INSERT INTO source(platform, handle, external_id) VALUES('x', ?, ?)",
        (handle, external_id))
    con.commit()


def _uid(con, handle):
    row = con.execute("SELECT external_id FROM source WHERE platform='x' AND handle=?",
                      (handle,)).fetchone()
    return row[0] if row else None


def _routes():
    return {"UserByScreenName": profile_payload("nasa", uid="4242"),
            "UserTweets": timeline_payload([tweet("111", "nasa")])}


# ------------------------------------------------------- 1. известный id
def test_known_id_walk_is_single_request(con, tmp_path):
    """Обход при известном id: один ``UserTweets``, резолва нет вообще."""
    _add_source(con, "nasa", "4242")
    fake = FakeX(routes=_routes())
    s = mk_session(con, tmp_path, fake)

    posts = s.fetch_feed("nasa")

    assert posts, "лента обязана отдаться"
    assert fake.gql_hits.get("UserTweets", 0) == 1
    assert fake.gql_hits.get("UserByScreenName", 0) == 0, \
        "при известном id запрос UserByScreenName недопустим"


def test_explicit_user_id_skips_resolve_and_db(con, tmp_path):
    """Переданный снаружи ``user_id`` = один запрос и никакой записи в базу."""
    fake = FakeX(routes=_routes())
    s = mk_session(con, tmp_path, fake)

    assert s.fetch_feed("nasa", user_id="777")
    assert fake.gql_hits.get("UserTweets", 0) == 1
    assert fake.gql_hits.get("UserByScreenName", 0) == 0
    assert _uid(con, "nasa") is None, "переданный id не должен писаться в реестр"


# ----------------------------------------------------- 2. неизвестный id
def test_unknown_id_resolves_then_remembers(con, tmp_path):
    """Неизвестный id: резолв + лента, id записан, повторный обход один запрос."""
    _add_source(con, "nasa", None)
    fake = FakeX(routes=_routes())
    s = mk_session(con, tmp_path, fake)

    first = s.fetch_feed("nasa")
    assert first
    assert fake.gql_hits.get("UserByScreenName", 0) == 1, "первый обход резолвит имя"
    assert fake.gql_hits.get("UserTweets", 0) == 1
    assert _uid(con, "nasa") == "4242", "id обязан записаться в source.external_id"

    second = s.fetch_feed("nasa")  # новая сессия не нужна, но кэш обнулим
    s._user_ids.clear()
    assert second
    assert fake.gql_hits.get("UserByScreenName", 0) == 1, \
        "повторный обход не должен резолвить имя"
    assert fake.gql_hits.get("UserTweets", 0) == 2


# --------------------------------------------------- 3. known_user_id
def test_known_user_id_rejects_garbage(con):
    """Пусто, NULL, «abc», «123abc» → None; валидный числовой id отдаётся."""
    _add_source(con, "empty", "")
    _add_source(con, "null", None)
    _add_source(con, "abc", "abc")
    _add_source(con, "mixed", "123abc")
    _add_source(con, "good", "813286")
    _add_source(con, "spaced", "  4242  ")

    assert db.known_user_id(con, "empty") is None
    assert db.known_user_id(con, "null") is None
    assert db.known_user_id(con, "abc") is None
    assert db.known_user_id(con, "mixed") is None
    assert db.known_user_id(con, "good") == "813286"
    assert db.known_user_id(con, "spaced") == "4242"
    assert db.known_user_id(con, "нет_такой") is None
    assert db.known_user_id(con, "") is None
    assert db.known_user_id(con, None) is None
    # «@handle» нормализуется, регистр не важен.
    assert db.known_user_id(con, "@GOOD") == "813286"


# --------------------------------------------------- 4. remember_user_id
def test_remember_user_id_never_overwrites(con):
    """Уже существующий id не перетирается; пустая строка заполняется."""
    _add_source(con, "kept", "999")
    _add_source(con, "blank", None)

    assert db.remember_user_id(con, "kept", "111") is False
    assert _uid(con, "kept") == "999"

    assert db.remember_user_id(con, "blank", "222") is True
    assert _uid(con, "blank") == "222"

    # Строки нет — тихо ничего не делаем, не падаем.
    assert db.remember_user_id(con, "missing", "333") is False
    # Мусорный id не пишем.
    assert db.remember_user_id(con, "blank2", "abc") is False
