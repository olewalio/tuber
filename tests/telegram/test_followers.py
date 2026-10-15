"""Подписчики Telegram с публичной превью-страницы (ТЗ-45).

Проверяем главное:
* строгий разбор превью-страницы (en + ru + неразрывный пробел/сущность);
* три исхода явно: ``value`` / ``no_counter`` / ``gone``;
* округлённое значение из ``tgme_channel_info_counter`` НЕ берётся;
* при ``no_counter``/``gone`` прежние ``subs``/``subs_at`` НЕ затираются;
* порядок обхода: сначала никогда не снятые, потом самые старые;
* пауза между запросами и backoff на 403/429 по мокам, без реальной сети;
* ряд растёт: два прогона по одному каналу → две точки в meta_json.

Сеть и боевая база не используются: подменяем HTTP-клиент и часы/сон.
"""
from __future__ import annotations

import json

import pytest

from tuber.platforms.telegram import followers as f
from tuber.platforms.telegram import store as db


# --------------------------------------------------------------- HTTP-заглушки
class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakeClient:
    """Клиент, отдающий заранее заданные страницы по хендлу из URL."""

    def __init__(self, pages=None, default=None, statuses=None):
        self.pages = pages or {}
        self.default = default
        self.statuses = statuses or {}
        self.calls = []
        self.closed = False

    def get(self, url):
        self.calls.append(url)
        handle = url.rsplit("/", 1)[-1]
        status = self.statuses.get(handle, 200)
        body = self.pages.get(handle, self.default)
        return FakeResponse(status, body)

    def close(self):
        self.closed = True


def _value_html(n):
    return ('<meta property="og:title" content="c">'
            f'<div class="tgme_page_extra">{n} subscribers</div>')


def _seed(con, handle, *, subs=None, subs_at=None, status="active"):
    con.execute(
        "INSERT INTO source(platform, handle, status, subs, subs_at) VALUES"
        "('telegram', ?, ?, ?, ?)",
        (handle, status, subs, subs_at))
    con.commit()
    return con.execute(
        "SELECT id FROM source WHERE platform='telegram' AND handle=?", (handle,)
    ).fetchone()[0]


# --------------------------------------------------------------------- парсер
def test_parse_english(fixture_text):
    p = f.parse_preview(fixture_text("followers_en.html"))
    assert p == f.FollowersParse(f.OUTCOME_VALUE, 9503236)


def test_parse_russian(fixture_text):
    p = f.parse_preview(fixture_text("followers_ru.html"))
    assert p == f.FollowersParse(f.OUTCOME_VALUE, 12727)


def test_parse_nbsp_entity(fixture_text):
    # &nbsp; раскрывается в неразрывный пробел и вычищается.
    p = f.parse_preview(fixture_text("followers_nbsp.html"))
    assert p == f.FollowersParse(f.OUTCOME_VALUE, 1234567)


def test_parse_user_is_no_counter(fixture_text):
    p = f.parse_preview(fixture_text("followers_user.html"))
    assert p == f.FollowersParse(f.OUTCOME_NO_COUNTER, None)


def test_parse_without_og_title_is_gone(fixture_text):
    p = f.parse_preview(fixture_text("followers_gone.html"))
    assert p == f.FollowersParse(f.OUTCOME_GONE, None)


def test_parse_empty_is_gone():
    assert f.parse_preview("") == f.FollowersParse(f.OUTCOME_GONE, None)
    assert f.parse_preview(None) == f.FollowersParse(f.OUTCOME_GONE, None)


def test_rounded_counter_not_used():
    html = ('<meta property="og:title" content="c">'
            '<div class="tgme_channel_info_counter">'
            '<span class="counter_value">10.7M</span></div>')
    p = f.parse_preview(html)
    assert p.outcome == f.OUTCOME_NO_COUNTER
    assert p.value is None


# ------------------------------------------------------------------ запись/ряд
def test_value_writes_snapshot_and_series(con):
    sid = _seed(con, "chan_live")
    client = FakeClient(pages={"chan_live": _value_html("1 500")})
    s = f.collect_followers(con, client=client, pause=0.0, time_cap=10,
                            now_iso=lambda: "2026-09-21 00:00:00")
    assert s["updated"] == 1 and s["failed"] == 0
    row = con.execute("SELECT subs, subs_at, meta_json FROM source WHERE id=?",
                      (sid,)).fetchone()
    assert row["subs"] == 1500 and row["subs_at"] == "2026-09-21 00:00:00"
    hist = json.loads(row["meta_json"])["followers_history"]
    assert hist == [{"at": "2026-09-21 00:00:00", "subs": 1500}]


def test_two_runs_make_two_series_points(con):
    sid = _seed(con, "chan_grow")
    pages = {"chan_grow": _value_html("1000")}
    f.collect_followers(con, client=FakeClient(pages=pages), pause=0.0, time_cap=10,
                        now_iso=lambda: "2026-09-21 00:00:00")
    pages["chan_grow"] = _value_html("1200")
    f.collect_followers(con, client=FakeClient(pages=pages), pause=0.0, time_cap=10,
                        now_iso=lambda: "2026-09-22 00:00:00")
    row = con.execute("SELECT subs, subs_at, meta_json FROM source WHERE id=?",
                      (sid,)).fetchone()
    assert row["subs"] == 1200 and row["subs_at"] == "2026-09-22 00:00:00"
    hist = json.loads(row["meta_json"])["followers_history"]
    assert [h["subs"] for h in hist] == [1000, 1200]
    assert [h["at"] for h in hist] == ["2026-09-21 00:00:00", "2026-09-22 00:00:00"]


def test_no_counter_does_not_overwrite(con, fixture_text):
    sid = _seed(con, "user_chan", subs=4242, subs_at="2026-09-14 00:00:00")
    client = FakeClient(default=fixture_text("followers_user.html"))
    s = f.collect_followers(con, client=client, pause=0.0, time_cap=10)
    assert s["no_counter"] == 1 and s["updated"] == 0
    row = con.execute("SELECT subs, subs_at, meta_json FROM source WHERE id=?",
                      (sid,)).fetchone()
    assert row["subs"] == 4242 and row["subs_at"] == "2026-09-14 00:00:00"
    assert row["meta_json"] in (None, "", "{}")  # ряд не создан


def test_gone_does_not_overwrite(con, fixture_text):
    sid = _seed(con, "dead_chan", subs=99, subs_at="2026-09-14 00:00:00")
    client = FakeClient(default=fixture_text("followers_gone.html"))
    s = f.collect_followers(con, client=client, pause=0.0, time_cap=10)
    assert s["gone"] == 1
    row = con.execute("SELECT subs, subs_at FROM source WHERE id=?", (sid,)).fetchone()
    assert row["subs"] == 99 and row["subs_at"] == "2026-09-14 00:00:00"


def test_status_is_never_touched(con, fixture_text):
    _seed(con, "cand", status="candidate")
    _seed(con, "priv", status="private")
    _seed(con, "dead", status="dead")
    client = FakeClient(default=_value_html("123"))
    f.collect_followers(con, client=client, pause=0.0, time_cap=10)
    got = {r["handle"]: r["status"] for r in con.execute(
        "SELECT handle, status FROM source WHERE platform='telegram'")}
    assert got == {"cand": "candidate", "priv": "private", "dead": "dead"}


# ------------------------------------------------------------------- порядок
def test_order_never_sampled_first_then_oldest(con):
    _seed(con, "b_mid", subs=10, subs_at="2026-09-10 00:00:00")
    _seed(con, "a_never", subs=None, subs_at=None)
    _seed(con, "c_new", subs=20, subs_at="2026-09-20 00:00:00")
    client = FakeClient(default=_value_html("5"))
    f.collect_followers(con, client=client, pause=0.0, time_cap=10)
    order = [u.rsplit("/", 1)[-1] for u in client.calls]
    assert order == ["a_never", "b_mid", "c_new"]


# -------------------------------------------------------------- предохранители
def test_pause_between_requests(con):
    for h in ("h1", "h2", "h3"):
        _seed(con, h)
    sleeps = []
    f.collect_followers(con, client=FakeClient(default=_value_html("1")),
                        pause=0.5, time_cap=10, sleep=sleeps.append)
    assert sleeps == [0.5, 0.5]  # N запросов → N-1 пауз


def test_backoff_and_honest_stop_on_repeated_403(con):
    _seed(con, "h1")
    _seed(con, "h2")
    client = FakeClient(default=_value_html("1"), statuses={"h1": 403, "h2": 403})
    sleeps = []
    s = f.collect_followers(con, client=client, pause=0.0, backoff=30.0,
                            time_cap=10, sleep=sleeps.append)
    assert s["refused"] is True
    assert s["failed"] == 1
    assert s["attempted"] == 1          # второй канал не трогали
    assert 30.0 in sleeps               # был backoff
    assert len(client.calls) == 2       # две попытки на одном канале
    assert s["updated"] == 0            # фальшивых нулей не записали


def test_backoff_then_success(con):
    sid = _seed(con, "h1")
    seen = {"n": 0}

    class FlakyClient(FakeClient):
        def get(self, url):
            seen["n"] += 1
            status = 429 if seen["n"] == 1 else 200
            return FakeResponse(status, _value_html("777"))

    sleeps = []
    s = f.collect_followers(con, client=FlakyClient(), pause=0.0, backoff=30.0,
                            time_cap=10, sleep=sleeps.append)
    assert s["updated"] == 1 and s["failed"] == 0
    assert sleeps == [30.0]
    assert con.execute("SELECT subs FROM source WHERE id=?", (sid,)).fetchone()[0] == 777


def test_time_cap_marks_skipped(con):
    for h in ("h1", "h2", "h3"):
        _seed(con, h)
    ticks = iter([0.0, 0.0, 5.0, 100.0, 100.0])
    s = f.collect_followers(con, client=FakeClient(default=_value_html("1")),
                            pause=0.0, time_cap=10, clock=lambda: next(ticks),
                            sleep=lambda _s: None)
    assert s["updated"] == 1
    assert s["skipped"] == 2


def test_limit_and_skipped(con):
    for h in ("h1", "h2", "h3"):
        _seed(con, h)
    s = f.collect_followers(con, client=FakeClient(default=_value_html("1")),
                            limit=1, pause=0.0, time_cap=10)
    assert s["updated"] == 1 and s["attempted"] == 1 and s["skipped"] == 2


def test_dry_run_writes_nothing(con):
    sid = _seed(con, "h1", subs=5, subs_at="2026-09-01 00:00:00")
    s = f.collect_followers(con, client=FakeClient(default=_value_html("999")),
                            pause=0.0, time_cap=10, dry_run=True)
    assert s["updated"] == 1  # посчитали
    row = con.execute("SELECT subs, subs_at FROM source WHERE id=?", (sid,)).fetchone()
    assert row["subs"] == 5 and row["subs_at"] == "2026-09-01 00:00:00"


def test_each_request_logged_to_run_log(con):
    _seed(con, "logchan")
    run_id = db.start_run(con, "followers")
    f.collect_followers(con, client=FakeClient(default=_value_html("321")),
                        pause=0.0, time_cap=10, run_id=run_id,
                        now_iso=lambda: "2026-09-21 00:00:00")
    rows = list(con.execute(
        "SELECT level, handle, msg FROM run_log WHERE run_id=?", (run_id,)))
    assert rows, "каждый запрос обязан оставлять строку run_log"
    text = " ".join(r["msg"] for r in rows)
    assert "logchan" in text and "321" in text


def test_run_finish_survives_close(tg_path, fixture_text):
    """Регрессия: незакоммиченный INSERT run_log откатывал итог прогона при close.

    Исход ``no_counter`` (нет записи подписчиков) не вызывал ни одного commit,
    и неявная транзакция pysqlite откатывала ``finish_run``. Проверяем на новом
    соединении: итог и строка журнала обязаны пережить закрытие.
    """
    con = db.connect(tg_path)
    _seed(con, "user_only")
    run_id = db.start_run(con, "followers")
    f.collect_followers(con, client=FakeClient(
        default=fixture_text("followers_user.html")),
        pause=0.0, time_cap=10, run_id=run_id)
    db.finish_run(con, run_id, channels_ok=0, channels_fail=0, errors=0)
    con.close()
    con2 = db.connect(tg_path)
    try:
        row = con2.execute("SELECT finished_at FROM run WHERE id=?",
                           (run_id,)).fetchone()
        assert row["finished_at"], "итог прогона обязан переживать close"
        logs = con2.execute("SELECT COUNT(*) FROM run_log WHERE run_id=?",
                            (run_id,)).fetchone()[0]
        assert logs >= 1, "строка журнала запроса обязана переживать close"
    finally:
        con2.close()


# ---------------------------------------------------------------------- вывод
def test_summary_line_format():
    line = f.summary_line({"updated": 3, "no_counter": 1, "gone": 2,
                           "skipped": 4, "failed": 0, "elapsed": 12.6})
    assert line == ("обновлено 3, недоступно 1, нет канала 2, "
                    "пропущено 4, отказов 0, время 13с")


# ----------------------------------------------------------------------- CLI
def test_cli_registered():
    from tuber.platforms.telegram import cli
    assert cli.COMMANDS["followers"] == "followers"


def test_cli_runs_and_prints_one_line(tg_path, capsys, monkeypatch):
    from tuber.platforms.telegram import cli
    con = db.connect(tg_path)
    _seed(con, "cli_chan")
    con.close()

    def fake_collect(con, **kwargs):
        return {"updated": 1, "no_counter": 0, "gone": 0, "skipped": 0,
                "failed": 0, "elapsed": 1.0}

    # Путь задаём через config (не через --db): _apply_db_override меняет
    # config.DB_PATH/DEFAULT_DB глобально, а monkeypatch вернёт их после теста.
    monkeypatch.setattr(f.config, "DB_PATH", tg_path)
    monkeypatch.setattr(f.config, "DEFAULT_DB", tg_path)
    monkeypatch.setattr(f, "collect_followers", fake_collect)
    rc = cli.main(["followers"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["обновлено 1, недоступно 0, нет канала 0, пропущено 0, "
                   "отказов 0, время 1с"]


def test_cli_gate_blocks_production(tg_path, monkeypatch, capsys):
    from tuber.platforms.telegram import cli
    monkeypatch.setattr(f.config, "DB_PATH", tg_path)
    monkeypatch.setattr(f.config, "PRODUCTION_DB", tg_path)
    with pytest.raises(SystemExit):
        cli.main(["followers"])


def test_cli_gate_allows_dry_run_on_production(tg_path, monkeypatch, capsys):
    from tuber.platforms.telegram import cli
    monkeypatch.setattr(f.config, "DB_PATH", tg_path)
    monkeypatch.setattr(f.config, "PRODUCTION_DB", tg_path)
    monkeypatch.setattr(f, "collect_followers",
                        lambda con, **kw: {"updated": 0, "no_counter": 0, "gone": 0,
                                           "skipped": 0, "failed": 0, "elapsed": 0.0})
    assert cli.main(["followers", "--dry-run"]) == 0
