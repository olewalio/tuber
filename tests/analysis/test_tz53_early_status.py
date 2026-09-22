"""ТЗ-53: блок «Раннее» — без вырожденного ``accel`` и с различимым статусом стадии.

Детерминированные проверки без сети:

* нулевой ранний темп (``m6 == m1``) даёт ``accel = None``, а не ``1e11``; в
  текстовой выдаче нет ни чисел вида ``1e11``, ни ``inf``/``nan`` (D-67);
* ``metric_schedule.status`` различает ``pending`` / ``measured`` /
  ``closed_no_data``; просроченная стадия без данных видна отдельно от «ещё не
  наступило»;
* миграция статуса идемпотентна и безопасна (выполняется при запуске);
* просмотры монотонны: 50 пар «снимок с большим значением → новый с меньшим»
  дают 0 откатов — ни очередь, ни сборщик Telegram не понижают ряд.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tuber.core import db, metrics, schema
from tuber.platforms.telegram import collect as tg_collect
from tuber.platforms.telegram import store as tg_store

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _db(tmp_path):
    path = str(tmp_path / "tz53.db")
    con = db.connect(path)
    schema.init_schema(con)
    return path, con


def _source(con, sid, platform, handle="ch"):
    con.execute(
        "INSERT INTO source(id, platform, handle, status) VALUES (?,?,?, 'active')",
        (sid, platform, f"{handle}{sid}"))


def _content(con, cid, platform, sid, published: datetime, external_id=None):
    con.execute(
        "INSERT INTO content(id, platform, source_id, external_id, published_at)"
        " VALUES (?,?,?,?,?)",
        (cid, platform, sid, external_id or f"{platform}-{cid}", _iso(published)))


def _snap(con, cid, platform, captured: datetime, **fields):
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    con.execute(
        f"INSERT INTO metric_snapshot(content_id, platform, captured_at, {cols})"
        f" VALUES (?,?,?,{marks})",
        (cid, platform, _iso(captured), *fields.values()))


def _early_fixture(con, *, cid, sid, m1, m6, m24, platform="telegram", published=None):
    published = published or (NOW - timedelta(hours=25))
    _content(con, cid, platform, sid, published)
    _snap(con, cid, platform, published + timedelta(hours=1), views=m1)
    _snap(con, cid, platform, published + timedelta(hours=6), views=m6)
    _snap(con, cid, platform, published + timedelta(hours=24), views=m24)


# ---------------------------------------------------------------------------
# 1. accel не вырождается
# ---------------------------------------------------------------------------

def test_accel_null_for_zero_median_and_zero_early_rate(tmp_path):
    """Материал с медианой6ч=0 и m6 == m1: accel = None, в выдаче нет мусора."""
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        # Три поста автора с нулевыми просмотрами на 6-м часу → медиана 0.
        for cid in (2, 3, 4):
            pub = NOW - timedelta(hours=30 + cid)
            _content(con, cid, "telegram", 1, pub)
            _snap(con, cid, "telegram", pub + timedelta(hours=6), views=0)
        # Целевой: ранний темп (m6 - m1)/5 == 0 → делить не на что.
        _early_fixture(con, cid=1, sid=1, m1=5, m6=5, m24=100)
        metrics.backfill_snapshots(con, platforms=("telegram",))
        data = metrics.build_early(con, now=NOW, limit=5)
        target = next(it for it in data["items"] if it["content_id"] == 1)
        assert target["author_median_6h"] == 0
        assert target["accel"] is None

        text = metrics.format_early(data)
        assert "нет раннего темпа" in text
        assert "inf" not in text and "nan" not in text
        assert "e+" not in text and "e-" not in text  # нет экспоненциальной записи
        # Никакого большого вырожденного числа (было 111111111.11).
        assert "111111111" not in text and "55555555" not in text
    finally:
        con.close()


def test_zero_median_with_positive_early_rate_keeps_finite_accel(tmp_path):
    """Медиана6ч=0, но ранний темп > 0 — accel честный и конечный (не None)."""
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        for cid in (2, 3, 4):
            pub = NOW - timedelta(hours=30 + cid)
            _content(con, cid, "telegram", 1, pub)
            _snap(con, cid, "telegram", pub + timedelta(hours=6), views=0)
        _early_fixture(con, cid=1, sid=1, m1=0, m6=3, m24=4)
        metrics.backfill_snapshots(con, platforms=("telegram",))
        data = metrics.build_early(con, now=NOW, limit=5)
        target = next(it for it in data["items"] if it["content_id"] == 1)
        assert target["accel"] is not None
        # accel = ((4-3)/18) / ((3-0)/5) = 0.0926
        assert abs(target["accel"] - ((1 / 18) / 0.6)) < 1e-9
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 2. Статус стадии
# ---------------------------------------------------------------------------

def test_status_measured_when_snapshot_linked(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pub = NOW - timedelta(hours=7)
        _content(con, 1, "telegram", 1, pub)
        _snap(con, 1, "telegram", pub + timedelta(hours=1), views=1000)
        metrics.plan_stages(con, now=NOW)
        stats = metrics.sweep_overdue(con, now=NOW, fetch=None)
        assert stats["linked"] == 1
        status = con.execute(
            "SELECT status FROM metric_schedule WHERE stage='1h'").fetchone()[0]
        assert status == metrics.STAGE_STATUS_MEASURED
    finally:
        con.close()


def test_status_pending_when_not_due(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        _content(con, 1, "telegram", 1, NOW - timedelta(minutes=30))
        metrics.plan_stages(con, now=NOW)
        stats = metrics.sweep_overdue(con, now=NOW, fetch=None)
        assert stats["processed"] == 0
        statuses = [r[0] for r in con.execute("SELECT status FROM metric_schedule")]
        assert statuses and set(statuses) == {metrics.STAGE_STATUS_PENDING}
        counts = metrics.schedule_status_counts(con, now=NOW)
        assert counts["pending"] == 4 and counts["overdue_pending"] == 0
    finally:
        con.close()


def test_status_closed_no_data_and_overdue_separate(tmp_path):
    """Просроченная стадия: ждали и не получили цифр ≠ ещё не наступило."""
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        _content(con, 1, "telegram", 1, NOW - timedelta(hours=200))
        metrics.plan_stages(con, now=NOW, lookback_hours=240)
        # До добора все 4 стадии просрочены и ещё pending.
        before = metrics.schedule_status_counts(con, now=NOW)
        assert before["pending"] == 4 and before["overdue_pending"] == 4
        assert before["closed_no_data"] == 0
        stats = metrics.sweep_overdue(con, now=NOW, fetch=None)
        assert stats["missed"] == 4
        after = metrics.schedule_status_counts(con, now=NOW)
        assert after["closed_no_data"] == 4
        assert after["measured"] == 0
        assert after["overdue_pending"] == 0  # done_at поставлен, строка не висит
    finally:
        con.close()


def test_status_deferred_stays_pending(tmp_path):
    """Замер опоздал в пределах grace, но сети нет → отложен, статус pending."""
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram", handle="ch")
        pub = NOW - timedelta(hours=7)
        _content(con, 1, "telegram", 1, pub, external_id="ch/1")
        metrics.plan_stages(con, now=NOW)

        def fetch(*_a, **_k):
            return None

        stats = metrics.sweep_overdue(con, now=pub + timedelta(hours=7), fetch=fetch)
        assert stats["deferred"] >= 1
        status = con.execute(
            "SELECT status FROM metric_schedule WHERE stage='1h'").fetchone()[0]
        assert status == metrics.STAGE_STATUS_PENDING
    finally:
        con.close()


def test_build_early_reports_schedule_statuses(tmp_path):
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        _content(con, 1, "telegram", 1, NOW - timedelta(hours=200))
        metrics.plan_stages(con, now=NOW, lookback_hours=240)
        metrics.sweep_overdue(con, now=NOW, fetch=None)
        data = metrics.build_early(con, now=NOW)
        assert data["schedule"]["closed_no_data"] == 4
        line = metrics.format_schedule(data["schedule"])
        assert "закрыто без данных 4" in line
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Миграция статуса: идемпотентная, безопасная
# ---------------------------------------------------------------------------

def test_status_migration_adds_column_and_backfills(tmp_path):
    path = str(tmp_path / "legacy.db")
    con = db.connect(path)
    # Legacy-таблица ТЗ-43: шесть колонок, без status.
    con.execute(
        "CREATE TABLE metric_schedule ("
        " content_id INTEGER NOT NULL, platform TEXT, stage TEXT NOT NULL,"
        " due_at TEXT NOT NULL, done_at TEXT, attempt INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (content_id, stage))")
    con.commit()
    added = schema.ensure_columns(con)
    assert "metric_schedule.status" in added, added
    info = {r[1] for r in con.execute("PRAGMA table_info(metric_schedule)")}
    assert "status" in info
    con.close()

    # Полная миграция на нормальной базе: три состояния + идемпотентность.
    path2, con2 = _db(tmp_path)
    try:
        _source(con2, 1, "telegram")
        # measured: есть снимок с bucket='24h'.
        _content(con2, 1, "telegram", 1, NOW - timedelta(hours=30))
        _snap(con2, 1, "telegram", NOW - timedelta(hours=6), views=10, bucket="24h")
        # closed_no_data: done_at есть, снимка нет.
        _content(con2, 2, "telegram", 1, NOW - timedelta(hours=30))
        # pending: done_at NULL.
        _content(con2, 3, "telegram", 1, NOW - timedelta(hours=30))
        _snap(con2, 3, "telegram", NOW - timedelta(hours=6), views=10, bucket="24h")
        con2.executemany(
            "INSERT INTO metric_schedule(content_id, platform, stage, due_at, done_at,"
            " attempt, status) VALUES (?,?,?,?,?,?,NULL)",
            [
                (1, "telegram", "24h", _iso(NOW), _iso(NOW), 1),
                (2, "telegram", "24h", _iso(NOW), _iso(NOW), 1),
                (3, "telegram", "24h", _iso(NOW), None, 0),
            ])
        con2.commit()
        n1 = schema.backfill_metric_schedule_status(con2)
        assert n1 == 3
        rows = {r[0]: r[1] for r in con2.execute(
            "SELECT content_id, status FROM metric_schedule")}
        assert rows[1] == "measured"
        assert rows[2] == "closed_no_data"
        assert rows[3] == "pending"
        assert schema.backfill_metric_schedule_status(con2) == 0  # идемпотентно
    finally:
        con2.close()


# ---------------------------------------------------------------------------
# 3. Просмотры не убывают
# ---------------------------------------------------------------------------

def test_views_never_decrease_50_pairs(tmp_path):
    """50 пар «большой снимок → новый с меньшим»: 0 откатов (очередь)."""
    path, con = _db(tmp_path)
    try:
        _source(con, 1, "telegram")
        pairs = 50
        rollbacks = 0
        for i in range(pairs):
            cid = 1000 + i
            _content(con, cid, "telegram", 1, NOW - timedelta(hours=30))
            high = 10_000 + i * 7
            metrics._write_snapshot(
                con, cid, "telegram", _iso(NOW - timedelta(hours=2)),
                {"views": high}, bucket="24h")
            # Свежий сетевой ответ меньше уже сохранённого — ряд не понижаем.
            metrics._write_snapshot(
                con, cid, "telegram", _iso(NOW - timedelta(hours=1)),
                {"views": 5}, bucket="72h")
            stored = con.execute(
                "SELECT MAX(views) FROM metric_snapshot WHERE content_id=?",
                (cid,)).fetchone()[0]
            if stored < high:
                rollbacks += 1
        assert pairs == 50
        assert rollbacks == 0, f"откатов просмотров: {rollbacks}"
    finally:
        con.close()


def test_collector_does_not_lower_saved_views(tmp_path):
    """Сборщик Telegram не понижает просмотры при устаревшем сетевом ответе."""
    path = str(tmp_path / "tg53.db")
    handle = "tz53chan"
    tg_store.init_db(path).close()
    con = tg_store.connect(path)
    con.execute(
        "INSERT INTO channels(handle, title, subs, status, read_mode)"
        " VALUES (?,?,?,'active','web')", (handle, handle, 1000))
    con.commit()
    channel_id = con.execute(
        "SELECT id FROM channels WHERE handle=?", (handle,)).fetchone()[0]
    con.close()

    def post(views):
        return {
            "message_id": 1, "date_utc": "2026-09-21T10:00:00+00:00", "text": "пост",
            "text_hash": "h1", "views": views, "forwards": 1, "reactions": 1,
            "media_kind": None, "links": None, "hashtags": None, "mentions": None,
            "fwd_from": None, "is_forward": False, "has_own_media": False, "is_ad": False,
            "has_media": False,
        }

    col = tg_collect.Collector(db_path=path, mode="web", deadline=60,
                               sleep_scale=0.0, client=object())
    col.con = tg_store.connect(path)
    col._store_posts(channel_id, [post(1000)])
    # «Прошли сутки»: метку проверки просмотров сбрасываем в прошлое.
    con = tg_store.connect(path)
    con.execute("UPDATE posts SET views_checked_at='2000-01-01 00:00:00'")
    con.commit()
    con.close()

    col2 = tg_collect.Collector(db_path=path, mode="web", deadline=60,
                                sleep_scale=0.0, client=object())
    col2.con = tg_store.connect(path)
    col2._store_posts(channel_id, [post(5)])
    con = tg_store.connect(path)
    try:
        stored = con.execute(
            "SELECT views FROM posts WHERE channel_id=? AND message_id=1",
            (channel_id,)).fetchone()[0]
        assert stored == 1000, f"просмотры понижены до {stored}"
        snap = con.execute(
            "SELECT MAX(views) FROM metric_snapshot WHERE content_id="
            "(SELECT id FROM content WHERE platform='telegram' AND external_id=?)",
            (f"{handle}/1",)).fetchone()[0]
        assert snap == 1000
    finally:
        con.close()
