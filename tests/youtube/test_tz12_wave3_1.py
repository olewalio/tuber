"""ТЗ-12, волна 3.1: довести обход uploads до реального охвата реестра.

Сеть не используется: YouTube-клиент — мок, база и файлы состояния ротации
живут во временной папке. Живых вызовов API нет.

Проверяется:
* дефолт лимита обхода uploads равен 400 (был 60 — круг ~27 суток);
* env-переопределение COLLECT_UPLOADS_CHANNELS_PER_RUN работает, а кривое
  значение молча откатывается на конфиг;
* при исчерпанной квоте прогон завершается штатно (без исключения), пишет
  причину остановки и фактическое число обойдённых каналов;
* обход обрывается, когда остатка units меньше стоимости следующего канала.
"""

from __future__ import annotations

import pytest

from tuber.platforms.youtube import collect, config, store as db
from tests.youtube.test_collect import (
    FakeYouTube,
    _playlist_item,
    make_channel_item,
    make_video_meta,
)
from tests.youtube.test_tz11_wave3 import Cfg, NOW, _mark_ai


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(config, "now_ts", lambda: NOW)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "tz12.db")
    db.init_db(c)
    yield c
    c.close()


def _spend(conn, units, endpoint="playlist", key_id="k1", day=NOW):
    """Записать расход в quota_log (temp-вью поверх quota_usage)."""
    conn.execute(
        "INSERT INTO quota_log (key_id, date, units, endpoint) VALUES (?, ?, ?, ?)",
        (key_id, day, units, endpoint),
    )
    conn.commit()


class QuotaFake(FakeYouTube):
    """Мок, который честно пишет расход 1 unit за каждый playlist_items."""

    def __init__(self, conn, **kwargs):
        super().__init__(**kwargs)
        self.conn = conn

    def playlist_items(self, playlist_id, max_results=50):
        _spend(self.conn, 1, endpoint="playlist")
        return super().playlist_items(playlist_id, max_results)


# --- п.1: дефолт лимита ----------------------------------------------------


def test_tz12_default_uploads_limit_is_400():
    assert config.COLLECT_UPLOADS_CHANNELS_PER_RUN == 400


# --- п.2: env-переопределение ----------------------------------------------


def test_tz12_env_override_uploads_limit(monkeypatch):
    monkeypatch.delenv(config.COLLECT_UPLOADS_CHANNELS_PER_RUN_ENV, raising=False)
    # Без env — значение конфига (или дефолт модуля, если cfg пуст).
    assert config.collect_uploads_channels_per_run(None) == 400
    monkeypatch.setenv(config.COLLECT_UPLOADS_CHANNELS_PER_RUN_ENV, "5")
    assert config.collect_uploads_channels_per_run(None) == 5
    # env важнее конфига.
    assert config.collect_uploads_channels_per_run(Cfg.__new__(Cfg)) == 5


def test_tz12_broken_env_falls_back_to_config(monkeypatch):
    monkeypatch.setenv(config.COLLECT_UPLOADS_CHANNELS_PER_RUN_ENV, "мусор")
    cfg = type("C", (), {"COLLECT_UPLOADS_CHANNELS_PER_RUN": 123})()
    assert config.collect_uploads_channels_per_run(cfg) == 123
    # Пустое значение — тоже откат.
    monkeypatch.setenv(config.COLLECT_UPLOADS_CHANNELS_PER_RUN_ENV, "  ")
    assert config.collect_uploads_channels_per_run(cfg) == 123
    # Без cfg — дефолт модуля.
    assert config.collect_uploads_channels_per_run(None) == 400


def test_tz12_env_limits_actual_scan(conn, tmp_path, monkeypatch):
    """env реально урезает порцию обхода: 1 канал вместо двух."""
    _mark_ai(conn, "a1", "ch1")
    _mark_ai(conn, "a2", "ch1")
    _mark_ai(conn, "b1", "ch2")
    _mark_ai(conn, "b2", "ch2")
    monkeypatch.setenv(config.COLLECT_UPLOADS_CHANNELS_PER_RUN_ENV, "1")
    cfg = Cfg(tmp_path, COLLECT_UPLOADS_CHANNELS_PER_RUN=400)
    fake = FakeYouTube(
        search=[],
        channels=[make_channel_item("ch1"), make_channel_item("ch2")],
        playlist=[],
    )
    res = collect.run_collect(conn, cfg, queries=["q"], client=fake, classify=False)
    assert res["registry_total"] == 2
    assert res["channels_scanned"] == 1
    assert [pid for pid, _ in fake.playlist_calls] == ["UUch1"]


# --- п.3: предохранитель по квоте ------------------------------------------


def test_tz12_exhausted_quota_stops_gracefully(conn, tmp_path):
    """Остатка нет с самого начала: прогон завершается штатно, без исключения."""
    _mark_ai(conn, "a1", "ch1")
    _mark_ai(conn, "a2", "ch1")
    _mark_ai(conn, "b1", "ch2")
    _mark_ai(conn, "b2", "ch2")
    # Порог проекта = 10000 - 2000 = 8000; расход 9000 > порога.
    _spend(conn, 9000, endpoint="search")
    cfg = Cfg(tmp_path, COLLECT_UPLOADS_CHANNELS_PER_RUN=400)
    fake = FakeYouTube(
        search=[],
        channels=[make_channel_item("ch1"), make_channel_item("ch2")],
        playlist=[_playlist_item("v1", NOW - 3600)],
        videos=[make_video_meta("v1", channel_id="ch1")],
    )
    res = collect.run_collect(conn, cfg, queries=["q"], client=fake, classify=False)
    assert res["stop_reason"] == "quota"
    assert res["stopped_by_quota"] is True
    assert res["stopped_by_budget"] is False
    assert res["units_remaining"] == 0
    assert res["channels_scanned"] == 0
    assert res["registry_total"] == 2
    # Ни одного сетевого вызова: обход и поиск не запускались.
    assert fake.playlist_calls == []
    assert fake.search_calls == []


def test_tz12_quota_runs_out_mid_batch(conn, tmp_path):
    """Остаток 1 unit: первый канал обойдён, дальше обход штатно обрывается."""
    for cid in ("ch1", "ch2", "ch3"):
        _mark_ai(conn, f"{cid}-a", cid)
        _mark_ai(conn, f"{cid}-b", cid)
    _spend(conn, 7999, endpoint="search")  # остаток = 8000 - 7999 = 1
    cfg = Cfg(tmp_path, COLLECT_UPLOADS_CHANNELS_PER_RUN=400)
    fake = QuotaFake(
        conn,
        search=[],
        channels=[make_channel_item(c) for c in ("ch1", "ch2", "ch3")],
    )
    res = collect.run_collect(conn, cfg, queries=["q"], client=fake, classify=False)
    assert res["stop_reason"] == "quota"
    assert res["stopped_by_quota"] is True
    assert res["channels_scanned"] == 1
    assert [pid for pid, _ in fake.playlist_calls] == ["UUch1"]
    assert res["units_remaining"] == 0


def test_tz12_old_quota_rows_do_not_halt_run(conn, tmp_path):
    """Расход прошлых суток квоты не останавливает сегодняшний обход.

    quota_log хранит историю; если бы порог вычитался из all-time суммы, после
    пары дней обход встал бы навсегда. Поэтому остаток считается за текущие
    сутки Pacific Time.
    """
    _mark_ai(conn, "a1", "ch1")
    _mark_ai(conn, "a2", "ch1")
    # 50 000 units «вчера»: больше порога, но к сегодняшнему дню не относится.
    _spend(conn, 50000, endpoint="search", day=NOW - 86400)
    cfg = Cfg(tmp_path, COLLECT_UPLOADS_CHANNELS_PER_RUN=400)
    fake = FakeYouTube(
        search=[],
        channels=[make_channel_item("ch1")],
        playlist=[],
    )
    res = collect.run_collect(conn, cfg, queries=["q"], client=fake, classify=False)
    assert res["stop_reason"] is None
    assert res["stopped_by_quota"] is False
    assert res["channels_scanned"] == 1
    assert res["units_remaining"] == 8000

