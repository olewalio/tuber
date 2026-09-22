"""Внешние кандидаты: экспорт Telegram/X и импорт YouTube из фида (ТЗ-16 B/C).

Сеть не используется: клиент YouTube подменяется моком, база — временная.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tuber.platforms.youtube import candidates, config, store as db, api as yt


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "candidates_test.db")
    db.init_db(c)
    yield c
    c.close()


def _video(conn, vid, description="", title=None, tags=None, published=1_700_000_000):
    conn.execute(
        "INSERT INTO videos (video_id, title, description, tags, published_at, "
        "first_seen) VALUES (?, ?, ?, ?, ?, ?)",
        (vid, title, description, tags, published, 1),
    )
    conn.commit()


class FakeClient:
    """Мок videos.list: возвращает заранее заданные элементы по id."""

    def __init__(self, mapping=None, boom_ids=()):
        self.mapping = mapping or {}
        self.boom_ids = set(boom_ids)
        self.calls: list[list[str]] = []

    def videos_by_ids(self, ids, parts="snippet,statistics,contentDetails"):
        self.calls.append(list(ids))
        vid = ids[0]
        if vid in self.boom_ids:
            raise yt.YouTubeError(403, {"error": {"message": "quota"}}, "videos")
        item = self.mapping.get(vid)
        return [item] if item else []


def _channel_item(vid, channel_id):
    return {
        "id": vid,
        "snippet": {"channelId": channel_id, "title": "Канал",
                    "description": "про ИИ"},
    }


def _write_feed(path, entries):
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )
    return path


# --- разбор ссылок ----------------------------------------------------------


def test_extract_mentions_accepts_valid_and_rejects_service_paths():
    text = (
        "Подпишись: t.me/FooBar и telegram.me/BazQux, заходи в x.com/SomeUser "
        "или twitter.com/other_user. Служебное: t.me/s/durov, t.me/c/123456, "
        "t.me/joinchat/AAAA, t.me/+InviteCode, x.com/i/home, x.com/status/123, "
        "x.com/search?q=ai, x.com/hashtag/ai, x.com/settings."
    )
    found = candidates.extract_mentions(text)
    assert ("telegram", "foobar") in found
    assert ("telegram", "bazqux") in found
    assert ("x", "someuser") in found
    assert ("x", "other_user") in found
    for bad in ("s", "c", "joinchat", "+invitecode", "i", "status", "search",
                "hashtag", "settings"):
        assert not any(h == bad for _k, h in found), bad


def test_extract_mentions_ignores_too_short_telegram_handle():
    # Telegram-имя короче 5 знаков шаблону не соответствует.
    assert candidates.extract_mentions("t.me/abc") == []
    # X допускает и короткие, но не служебные.
    assert candidates.extract_mentions("x.com/ab") == [("x", "ab")]


# --- экспорт ----------------------------------------------------------------


def test_export_writes_jsonl_with_all_fields(conn, tmp_path):
    _video(conn, "v1", "Канал про ИИ: t.me/AlphaChannel",
           title="AI news", published=1000)
    _video(conn, "v2", "Ещё раз t.me/alphachannel и x.com/BetaUser",
           published=2000)
    out = tmp_path / "out.jsonl"
    summary = candidates.export_external(conn, out=out)

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # Алфавит: AlphaChannel (v1) и alphachannel (v2) — один кандидат.
    assert summary["written"] == len(rows) == 2
    by_key = {(r["kind"], r["handle"]): r for r in rows}
    alpha = by_key[("telegram", "alphachannel")]
    assert alpha["source"] == candidates.EXTERNAL_SOURCE
    assert alpha["videos"] == 2
    # ТЗ-21/A: `videos` — число, список id видео лежит в `video_ids`.
    assert alpha["video_ids"] == ["v1", "v2"]
    assert alpha["mentions"] == 2
    assert alpha["first_seen"] == "1970-01-01T00:16:40Z"
    assert alpha["last_seen"] == "1970-01-01T00:33:20Z"
    assert alpha["ai_hint"] == 1
    assert alpha["examples"] and "v1" in alpha["examples"][0]
    assert len(alpha["examples"][0]) <= 120
    assert alpha["exported_at"]
    beta = by_key[("x", "betauser")]
    assert beta["kind"] == "x" and beta["videos"] == 1
    assert beta["video_ids"] == ["v2"]


def test_export_sorted_by_mentions_then_videos(conn, tmp_path):
    _video(conn, "v1", "t.me/manyone t.me/manyone t.me/manyone x.com/onlyone")
    out = tmp_path / "out.jsonl"
    candidates.export_external(conn, out=out)
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["handle"] == "manyone"
    assert rows[0]["mentions"] == 3
    assert rows[-1]["handle"] == "onlyone"


def test_export_min_mentions_and_limit(conn, tmp_path):
    _video(conn, "v1", "t.me/aaaab t.me/aaaab")
    _video(conn, "v2", "t.me/ccccd")
    out = tmp_path / "out.jsonl"
    summary = candidates.export_external(conn, out=out, min_mentions=2)
    assert summary["written"] == 1

    summary2 = candidates.export_external(conn, out=out, min_mentions=1, limit=1)
    assert summary2["written"] == 1
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1


def test_export_dry_writes_nothing(conn, tmp_path):
    _video(conn, "v1", "t.me/aaaab")
    out = tmp_path / "out.jsonl"
    summary = candidates.export_external(conn, out=out, dry=True)
    assert not out.exists()
    assert summary["written"] == 0
    assert summary["out"] is None
    assert summary["kind_counts"]["telegram"] == 1
    assert summary["top"][0]["handle"] == "aaaab"


def test_export_ai_hint_from_title_and_tags(conn, tmp_path):
    # ИИ-термин в теге у видео без него в описании → подсказка.
    _video(conn, "v1", "канал t.me/tagchannel", tags='["нейросети"]')
    # Ничего ИИ-шного нигде.
    _video(conn, "v2", "просто канал t.me/plainchannel")
    out = tmp_path / "out.jsonl"
    candidates.export_external(conn, out=out)
    rows = {(r["kind"], r["handle"]): r
            for r in (json.loads(l) for l in out.read_text(encoding="utf-8").splitlines())}
    assert rows[("telegram", "tagchannel")]["ai_hint"] == 1
    assert rows[("telegram", "plainchannel")]["ai_hint"] == 0


# --- чтение фида ------------------------------------------------------------


def test_read_youtube_feed_kinds_and_urls(tmp_path):
    feed = _write_feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "video_id": "dQw4w9WgXcQ"},
        {"kind": "youtube", "url": "https://www.youtube.com/watch?v=abcdefghijk"},
        {"kind": "youtube", "url": "https://youtu.be/lmnopqrstuv"},
        {"kind": "telegram", "video_id": "zzzzzzzzzzz"},  # не youtube
        {"kind": "youtube", "video_id": "dQw4w9WgXcQ"},  # дубль
        {"kind": "youtube", "url": "https://example.com/nope"},
    ])
    ids = candidates.read_youtube_feed(feed)
    assert ids == ["dQw4w9WgXcQ", "abcdefghijk", "lmnopqrstuv"]


def test_read_youtube_feed_missing_file(tmp_path):
    with pytest.raises(candidates.CandidatesError):
        candidates.read_youtube_feed(tmp_path / "absent.jsonl")


def test_read_youtube_feed_broken_json(tmp_path):
    feed = tmp_path / "feed.jsonl"
    feed.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(candidates.CandidatesError):
        candidates.read_youtube_feed(feed)


# --- импорт -----------------------------------------------------------------


def test_import_resolves_channels_and_bumps_mentions(conn, tmp_path):
    feed = _write_feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "video_id": "vid00000001"},
        {"kind": "youtube", "video_id": "vid00000002"},
    ])
    client = FakeClient({
        "vid00000001": _channel_item("vid00000001", "UCaaa"),
        "vid00000002": _channel_item("vid00000002", "UCbbb"),
    })
    summary = candidates.import_youtube_feed(conn, feed, client, limit=50)
    assert summary["resolved"] == 2
    assert summary["new"] == 2
    assert summary["calls"] == 2
    row = conn.execute(
        "SELECT source, evidence, mentions, status FROM channel_candidates "
        "WHERE channel_id='UCaaa'"
    ).fetchone()
    assert row["source"] == candidates.FEED_SOURCE
    assert row["evidence"] == "tg:vid00000001"
    assert row["mentions"] == 1
    assert row["status"] == "new"

    # Повторный импорт не дублирует, а увеличивает mentions.
    candidates.import_youtube_feed(conn, feed, client, limit=50)
    n = conn.execute("SELECT COUNT(*) AS n FROM channel_candidates").fetchone()["n"]
    assert n == 2
    assert conn.execute(
        "SELECT mentions FROM channel_candidates WHERE channel_id='UCaaa'"
    ).fetchone()["mentions"] == 2


def test_import_respects_limit(conn, tmp_path):
    feed = _write_feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "video_id": f"vid0000000{i}"} for i in range(5)
    ])
    client = FakeClient({f"vid0000000{i}": _channel_item(f"vid0000000{i}", f"UC{i}")
                         for i in range(5)})
    summary = candidates.import_youtube_feed(conn, feed, client, limit=2)
    assert summary["calls"] == 2
    assert len(client.calls) == 2


def test_import_stops_on_quota_error(conn, tmp_path):
    feed = _write_feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "video_id": "vid00000009"},
    ])
    client = FakeClient(boom_ids=["vid00000009"])
    summary = candidates.import_youtube_feed(conn, feed, client, limit=50)
    assert summary["calls"] == 0
    assert "quota" in summary["stopped_reason"]
    assert summary["errors"]


def test_import_dry_no_network_no_writes(conn, tmp_path):
    feed = _write_feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "video_id": "vid00000001"},
    ])
    client = FakeClient({"vid00000001": _channel_item("vid00000001", "UCaaa")})
    summary = candidates.import_youtube_feed(None, feed, client, limit=50, dry=True)
    assert summary["dry_run"] is True
    assert summary["youtube_ids"] == 1
    assert client.calls == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM channel_candidates"
    ).fetchone()["n"] == 0


# --- ТЗ-19 / Дефект 1: счётчик читается без row_factory ----------------------


def test_quota_total_reads_on_connection_without_row_factory(tmp_path):
    """`_quota_total` читает значение позиционно, обычный connect() не роняет."""
    path = tmp_path / "norow.db"
    c = db.connect(path)
    db.init_db(c)
    c.close()
    plain = sqlite3.connect(str(path))  # без row_factory: row — tuple
    db.install_compat(plain)  # слой совместимости ставится адаптером и на «сырое» соединение
    try:
        day = (config.now_ts() // 86400) * 86400
        db.log_quota(plain, day, "kid00000001", 1, 5, "videos", None)
        assert candidates._quota_total(plain) == 5
    finally:
        plain.close()


# --- ТЗ-19 / Дефект 2: приём id по каждому полю ------------------------------


@pytest.mark.parametrize("field", [
    "video_id", "id", "url", "link", "video_url", "handle",
])
def test_read_youtube_feed_accepts_each_id_field(tmp_path, field):
    """Каждое поле-источник id (включая handle) даёт тот же video_id."""
    value = {
        "video_id": "abcdefghijk",
        "id": "abcdefghijk",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "link": "https://youtu.be/abcdefghijk",
        "video_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "handle": "https://www.youtube.com/watch?v=abcdefghijk",
    }[field]
    feed = _write_feed(tmp_path / "feed.jsonl", [{"kind": "youtube", field: value}])
    assert candidates.read_youtube_feed(feed) == ["abcdefghijk"]


def test_read_youtube_feed_accepts_bare_id_in_handle(tmp_path):
    """Голый 11-символьный id в handle принимается как ссылка."""
    feed = _write_feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "handle": "abcdefghijk"},
        {"kind": "youtube", "handle": "youtu.be/lmnopqrstuv"},
    ])
    assert candidates.read_youtube_feed(feed) == ["abcdefghijk", "lmnopqrstuv"]


# --- ТЗ-19 / Дефект 3: толерантность к битым строкам -------------------------


def test_parse_feed_counts_skipped_kinds(tmp_path):
    """Строки чужого kind игнорируются, но считаются."""
    feed = _write_feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "handle": "abcdefghijk"},
        {"kind": "telegram", "handle": "chan"},
        {"kind": "x", "handle": "user"},
    ])
    parsed = candidates._parse_feed(feed)
    assert parsed["skipped_kinds"] == 2
    assert parsed["bad_lines"] == 0
    assert parsed["youtube_ids"] == ["abcdefghijk"]


def test_parse_feed_skips_bad_line_and_continues(tmp_path):
    """Одна битая строка не отменяет импорт, считается в bad_lines."""
    feed = tmp_path / "feed.jsonl"
    feed.write_text(
        json.dumps({"kind": "youtube", "handle": "abcdefghijk"}) + "\n"
        + "{битая строка\n"
        + json.dumps({"kind": "youtube", "handle": "lmnopqrstuv"}) + "\n",
        encoding="utf-8",
    )
    parsed = candidates._parse_feed(feed)
    assert parsed["bad_lines"] == 1
    assert parsed["youtube_ids"] == ["abcdefghijk", "lmnopqrstuv"]


def test_parse_feed_empty_file_raises(tmp_path):
    """Пустой файл (и файл из одних пустых строк) — понятная ошибка."""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(candidates.CandidatesError):
        candidates.read_youtube_feed(empty)
    blanks = tmp_path / "blanks.jsonl"
    blanks.write_text("\n\n   \n", encoding="utf-8")
    with pytest.raises(candidates.CandidatesError):
        candidates.read_youtube_feed(blanks)


def test_parse_feed_all_bad_lines_raises(tmp_path):
    """Если все строки битые — импорт отменяется с понятной ошибкой."""
    feed = tmp_path / "feed.jsonl"
    feed.write_text(
        "{битая\n"
        "[1, 2, 3]\n"          # не объект
        '{"kind": "youtube"}\n'  # kind=youtube, но id нет — негодная строка
        "ещё мусор\n",
        encoding="utf-8",
    )
    with pytest.raises(candidates.CandidatesError):
        candidates.read_youtube_feed(feed)


def test_import_skips_bad_line_and_reports_count(conn, tmp_path):
    """Импорт выполняется при битой строке, bad_lines попадает в итог."""
    feed = tmp_path / "feed.jsonl"
    feed.write_text(
        json.dumps({"kind": "youtube", "handle": "vid00000001"}) + "\n"
        + json.dumps({"kind": "youtube", "handle": "vid00000002"}) + "\n"
        + "{обрыв\n",
        encoding="utf-8",
    )
    client = FakeClient({
        "vid00000001": _channel_item("vid00000001", "UCaaa"),
        "vid00000002": _channel_item("vid00000002", "UCbbb"),
    })
    summary = candidates.import_youtube_feed(conn, feed, client, limit=50)
    assert summary["bad_lines"] == 1
    assert summary["calls"] == 2
    assert summary["new"] == 2


def test_import_repeat_no_duplicates(conn, tmp_path):
    """Повторный импорт того же фида не дублирует и даёт known, а не new."""
    feed = _write_feed(tmp_path / "feed.jsonl", [
        {"kind": "youtube", "handle": "vid00000001"},
        {"kind": "youtube", "handle": "vid00000002"},
    ])
    client = FakeClient({
        "vid00000001": _channel_item("vid00000001", "UCaaa"),
        "vid00000002": _channel_item("vid00000002", "UCbbb"),
    })
    first = candidates.import_youtube_feed(conn, feed, client, limit=50)
    assert (first["new"], first["known"]) == (2, 0)
    second = candidates.import_youtube_feed(conn, feed, client, limit=50)
    assert (second["new"], second["known"]) == (0, 2)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM channel_candidates"
    ).fetchone()["n"]
    assert n == 2
