"""Регрессия ТЗ-13: экспорт внешних кандидатов читает базу ЯДРА через слой совместимости.

Дефект на боевой базе: ``candidates.open_readonly`` открывал файл «сырым»
``sqlite3.connect("file:...?mode=ro")`` без TEMP-представлений совместимости, а
``export_external`` читает legacy-имя ``videos``. В схеме ядра v5 таблицы
``videos`` нет — есть только TEMP-представление из ``store.install_compat`` —
поэтому боевая команда падала с ``no such table: videos``.

Старые тесты этого не ловили: ``tests/fixtures`` создаёт НАСТОЯЩУЮ legacy-таблицу
``videos``, а ``tests/youtube/test_candidates.py`` передаёт в ``export_external``
соединение из ``db.connect()`` (уже со слоем совместимости). Здесь база — как
боевая (единое ядро, без legacy ``videos``), а соединение берётся ТЕМ ЖЕ
хелпером, что и в ``cmd_candidates_export``.
"""

from __future__ import annotations

import json
import sqlite3

from tuber.platforms.youtube import candidates, cli, store as db


def _core_db(tmp_path, name="core.db"):
    """Собрать базу ЯДРА (как боевую): legacy-таблицы ``videos`` в ней НЕТ."""
    path = tmp_path / name
    conn = db.connect(path)
    db.init_db(conn)
    db.upsert_channel(conn, {"channel_id": "c1", "title": "ch", "first_seen": 1})
    db.upsert_video(
        conn,
        {
            "video_id": "v1",
            "channel_id": "c1",
            "title": "AI news",
            "description": "подпишись t.me/AlphaChannel и x.com/BetaUser",
            "published_at": 1_700_000_000,
            "first_seen": 1,
        },
    )
    conn.commit()
    conn.close()
    return path


def test_core_db_has_no_legacy_videos_table(tmp_path):
    """Предпосылка теста: в базе ядра нет постоянной таблицы ``videos``.

    Иначе регрессия была бы неотличима от старого ``tests/fixtures``, где
    ``videos`` — настоящая таблица.
    """
    path = _core_db(tmp_path)
    raw = sqlite3.connect(path)
    try:
        names = {r[0] for r in raw.execute("SELECT name FROM main.sqlite_master")}
    finally:
        raw.close()
    assert "videos" not in names
    assert "content" in names  # ядро на месте


def test_open_readonly_sees_legacy_videos_and_export_writes(tmp_path):
    """Склейка CLI: соединение из ``candidates.open_readonly`` + экспорт.

    На старом коде (голый ``mode=ro`` без слоя совместимости) это падало с
    ``no such table: videos`` и ``written`` был недостижим.
    """
    path = _core_db(tmp_path)
    out = tmp_path / "feed.jsonl"
    conn = candidates.open_readonly(path)  # тот же хелпер, что и в CLI
    try:
        summary = candidates.export_external(conn, out=out)
    finally:
        conn.close()

    assert summary["written"] > 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    keys = {(r["kind"], r["handle"]) for r in rows}
    assert ("telegram", "alphachannel") in keys
    assert ("x", "betauser") in keys


def test_open_readonly_creates_temp_objects_but_cannot_write_main(tmp_path):
    """Слой совместимости ставится на read-only соединении, а запись в main — нет."""
    path = _core_db(tmp_path)
    conn = candidates.open_readonly(path)
    try:
        # TEMP-представление ``videos`` создано и читается.
        assert conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 1
        # Запись в main-базу физически невозможна (file:...?mode=ro).
        try:
            conn.execute("CREATE TABLE main.tz13_probe(x)")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower()
        else:  # pragma: no cover — сюда попадать нельзя
            raise AssertionError("запись в main на read-only соединении прошла")
    finally:
        conn.close()


def test_candidates_export_cli_returns_nonzero_when_unreadable(tmp_path, monkeypatch, capsys):
    """CLI обязан вернуть ненулевой код и не печатать «успешный» JSON.

    Раньше ошибка чтения уходила в stderr при ``exit 0`` (через обёртку крона
    джоб показывал ``ok``). Файла базы нет — ``mode=ro`` его не создаёт.
    """
    missing = tmp_path / "nope.db"
    monkeypatch.setattr(cli, "DB_PATH", missing)
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "tuber.log")
    monkeypatch.setattr(cli.config, "load_env", lambda *a, **k: 0)

    rc = cli.main(["candidates-export", "--out", str(tmp_path / "out.jsonl")])

    assert rc != 0
    captured = capsys.readouterr()
    assert captured.out == ""  # формат успеха (один JSON) не имитируется
    assert "ошибка" in captured.err
