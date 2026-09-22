"""ТЗ-49: CLI ``tuber comments`` — гейт записи, dry-run и выдача ``top``.

Проверяем:
* гейт записи в боевую базу без ``--allow-production`` (код 2, без записи);
* ``--dry-run`` разрешён даже на боевой (гейт пропускает);
* ``comments top`` печатает обсуждения с числами, вопросы аудитории дословно и
  сигнал X по счётчику ответов (без текста).

Боевая база не изменяется: тесты идут на временных базах, а гейт проверяется на
пути боевой без подключения.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tuber import config as core_config
from tuber.analysis import comments as C
from tuber.core import db as core_db
from tuber.core import schema as core_schema


def _open(path):
    con = core_db.connect(path)
    core_schema.migrate_schema(con)
    return con


@pytest.fixture()
def path(tmp_path):
    p = tmp_path / "tz49_cli.db"
    con = _open(str(p))
    con.close()
    return str(p)


def _seed(con):
    con.execute(
        "INSERT INTO source(platform, handle, title) VALUES"
        "('youtube', 'chan', 'Канал Ютуба')")
    con.execute(
        "INSERT INTO content(platform, source_id, external_id, kind, url, "
        "published_at, title) VALUES"
        "('youtube', 1, 'vid1', 'video', 'https://youtu.be/vid1', "
        "datetime('now','-2 days'), 'Видео про агентов')")
    for i, (text, likes) in enumerate([
        ("Это работает локально?", 40),
        ("Будет ли поддержка Windows?", 90),
        ("Спасибо за разбор", 5),
    ]):
        con.execute(
            "INSERT INTO content_comment(comment_id, content_id, platform, "
            "external_id, author, text, likes, captured_at) VALUES"
            "(?, 1, 'youtube', 'vid1', ?, ?, ?, datetime('now'))",
            (f"c{i}", f"Автор {i}", text, likes))
    # X: пост с большим conversation_count; текст не собираем.
    con.execute(
        "INSERT INTO source(platform, handle, title) VALUES('x', 'elonmusk', 'Elon')")
    con.execute(
        "INSERT INTO content(platform, source_id, external_id, kind, "
        "published_at, text, meta_json) VALUES"
        "('x', 2, 'tweet1', 'post', datetime('now','-1 day'), 'X текст', "
        "'{\"owner_handle\": \"elonmusk\"}')")
    con.execute(
        "INSERT INTO content_latest(content_id, captured_at, replies, likes) "
        "VALUES(2, datetime('now'), 515, 1000)")
    con.commit()


def test_gate_refuses_production_without_flag():
    args = SimpleNamespace(db=str(core_config.DEFAULT_DB_PATH), dry_run=False,
                           allow_production=False)
    assert C._gate(args, str(core_config.DEFAULT_DB_PATH)) is True


def test_gate_allows_dry_run_on_production():
    args = SimpleNamespace(db=str(core_config.DEFAULT_DB_PATH), dry_run=True,
                           allow_production=False)
    assert C._gate(args, str(core_config.DEFAULT_DB_PATH)) is False


def test_gate_allows_copy_without_flag():
    args = SimpleNamespace(db="/tmp/copy.db", dry_run=False, allow_production=False)
    assert C._gate(args, "/tmp/copy.db") is False


def test_cli_refuses_production_youtube(capsys):
    rc = C.main(["youtube", "--db", str(core_config.DEFAULT_DB_PATH)])
    assert rc == 2
    assert "allow-production" in capsys.readouterr().err


def test_cli_dry_run_writes_nothing(path):
    con = _open(path)
    try:
        _seed(con)
    finally:
        con.close()
    rc = C.main(["youtube", "--db", path, "--dry-run", "--json"])
    assert rc == 0
    con = _open(path)
    try:
        assert con.execute(
            "SELECT COUNT(*) AS n FROM candidate").fetchone()["n"] == 0
    finally:
        con.close()


def test_top_reports_numbers_questions_and_x(path, capsys):
    con = _open(path)
    try:
        _seed(con)
    finally:
        con.close()
    rc = C.main(["top", "--db", path, "--top", "5", "--questions", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Всего комментариев: 3" in out
    assert "ОБСУЖДЕНИЯ" in out
    assert "ВОПРОСЫ АУДИТОРИИ" in out
    assert "Будет ли поддержка Windows?" in out          # дословно
    assert "доля вопросов" in out
    assert "@elonmusk" in out and "ответов 515" in out
    assert "текста нет" in out


def test_top_json_shape(path, capsys):
    import json

    con = _open(path)
    try:
        _seed(con)
    finally:
        con.close()
    assert C.main(["top", "--db", path, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"][0]["comments"] == 3
    assert payload["discussions"][0]["authors"] == 3
    assert payload["discussions"][0]["questions"] == 2
    assert payload["x_hot"][0]["replies"] == 515
    assert payload["x_hot"][0]["text_available"] is False
