"""Повышение кандидата Telegram в активные (последняя миля, измерения; ТЗ-51).

Проверяем правило и гарантии из :mod:`tuber.platforms.telegram.promote`:

* повышение при достаточном материале, антифроде и посчитанном ``vr``;
* ``vr`` считается ПО ФАКТИЧЕСКИМ ПОСТАМ (медиана просмотров / подписчики), а не
  берётся из колонки ``source.vr`` (D-54);
* «vr неизвестен» — явное состояние отказа, а не тихий отказ;
* связь очередь ↔ реестр по каноническому хендлу/``tg_id``: повышение закрывает
  строку очереди (``promoted_at``/``promoted``), а при её отсутствии заводит
  (D-55);
* неприкосновенность private/dead/rejected и отсутствие понижения статуса;
* идемпотентность (повторный прогон не меняет ни одной строки и не плодит
  дубликатов в очереди);
* проверка вырождения (порог не повышает всех подряд);
* пустой вход;
* dry-run без записи;
* CLI-подкоманду `tg promote` (включая гейт боевой базы).
"""
from __future__ import annotations

from tuber.platforms.telegram import config, promote
from tuber.platforms.telegram import linking
from tuber.platforms.telegram import store as db


def _add_source(con, handle, *, status="candidate", subs=1000, antifraud=0,
                posts=0, views=1000, external_id=None):
    """Источник с ``posts`` постами; у каждого поста просмотры ``views``."""
    con.execute(
        "INSERT INTO source(platform, handle, external_id, status, subs, "
        "antifraud_flag) VALUES('telegram', ?, ?, ?, ?, ?)",
        (handle, external_id, status, subs, antifraud),
    )
    sid = con.execute(
        "SELECT id FROM source WHERE platform='telegram' AND handle=?", (handle,)
    ).fetchone()[0]
    for i in range(posts):
        meta = None if views is None else '{"views": %d}' % views
        con.execute(
            "INSERT INTO content(platform, source_id, external_id, published_at, meta_json) "
            "VALUES('telegram', ?, ?, ?, ?)",
            (sid, f"{handle}/{i}", "2026-09-01 00:00:00", meta),
        )
    con.commit()
    return sid


def _status(con, handle):
    return con.execute(
        "SELECT status FROM source WHERE platform='telegram' AND handle=?", (handle,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Канонизация хендлов (D-55)
# ---------------------------------------------------------------------------

def test_normalize_handle_aliases_collapse():
    for raw in ("@X", "t.me/x", "https://t.me/x", "X", " x/s", "https://t.me/x/s"):
        assert linking.normalize_handle(raw) == "x", raw
    assert linking.normalize_handle("") == ""
    assert linking.normalize_handle(None) == ""


# ---------------------------------------------------------------------------
# Правило повышения
# ---------------------------------------------------------------------------

def test_promotes_with_enough_material(con):
    _add_source(con, "rich", posts=config.PROMOTE_MIN_POSTS, subs=1000, views=1000)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 1, summary
    assert _status(con, "rich") == "active"


def test_no_promotion_without_material(con):
    _add_source(con, "poor", posts=config.PROMOTE_MIN_POSTS - 1)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 0
    assert summary["reasons"][promote.REASON_TOO_FEW_POSTS] == 1
    assert _status(con, "poor") == "candidate"


def test_no_promotion_on_antifraud(con):
    _add_source(con, "fraud", posts=config.PROMOTE_MIN_POSTS, antifraud=1)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 0
    assert summary["reasons"][promote.REASON_ANTIFRAUD] == 1
    assert _status(con, "fraud") == "candidate"


# ---------------------------------------------------------------------------
# Расчёт vr по фактическим постам (D-54)
# ---------------------------------------------------------------------------

def test_vr_computed_from_posts(con):
    """vr = 100 · медиана просмотров / подписчики; source.vr при этом не читается."""
    # Просмотры через разные посты: медиана 2000 из (1000, 2000, 3000).
    sid = _add_source(con, "mid", posts=0, subs=1000)
    for i, v in enumerate((1000, 2000, 3000)):
        con.execute(
            "INSERT INTO content(platform, source_id, external_id, published_at, meta_json)"
            " VALUES('telegram', ?, ?, ?, ?)",
            (sid, f"mid/{i}", "2026-09-01 00:00:00", '{"views": %d}' % v),
        )
    # Добить до порога постов постами с просмотрами 2000.
    for i in range(3, config.PROMOTE_MIN_POSTS):
        con.execute(
            "INSERT INTO content(platform, source_id, external_id, published_at, meta_json)"
            " VALUES('telegram', ?, ?, ?, ?)",
            (sid, f"mid/{i}", "2026-09-01 00:00:00", '{"views": 2000}'),
        )
    con.commit()
    vr = promote.compute_vr(con, sid, 1000)
    assert vr["state"] == "ok"
    assert vr["vr"] == 200.0, vr  # 100 · 2000 / 1000
    # Прежняя (неверная) колонка source.vr=5 не должна мешать.
    con.execute("UPDATE source SET vr=5.0 WHERE id=?", (sid,))
    con.commit()
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 1, summary


def test_vr_unknown_is_explicit(con):
    """Нет подписчиков/просмотров → «vr неизвестен», а не тихий отказ."""
    _add_source(con, "nosubs", posts=config.PROMOTE_MIN_POSTS, subs=None)
    _add_source(con, "noviews", posts=config.PROMOTE_MIN_POSTS, subs=1000, views=None)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 0
    assert summary["reasons"][promote.REASON_VR_UNKNOWN] == 2
    assert summary["vr_unknown"] == 2
    assert _status(con, "nosubs") == "candidate"
    report = promote.format_report(summary)
    assert "vr неизвестен: 2" in report


def test_vr_below_threshold_rejected(con):
    # subs=1000, views=100 → vr=10 < 15.
    _add_source(con, "lowvr", posts=config.PROMOTE_MIN_POSTS, subs=1000, views=100)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 0
    assert summary["reasons"][promote.REASON_VR_LOW] == 1


def test_no_require_vr_ignores_vr(con):
    _add_source(con, "nosubs", posts=config.PROMOTE_MIN_POSTS, subs=None)
    summary = promote.promote_candidates(con, dry_run=False, require_vr=False)
    assert summary["promoted"] == 1
    assert _status(con, "nosubs") == "active"


# ---------------------------------------------------------------------------
# Связь очередь ↔ реестр + promoted_at (D-55) и идемпотентность
# ---------------------------------------------------------------------------

def test_promotion_closes_queue_row_with_alias_handle(con):
    """`@X`/`t.me/x`/`X` — одна запись очереди; повышение её закрывает."""
    _add_source(con, "Target", posts=config.PROMOTE_MIN_POSTS)
    con.execute(
        "INSERT INTO candidate(platform, kind, handle, status) "
        "VALUES('telegram','channel','@target','new')"
    )
    con.commit()
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 1
    assert summary["queue_closed"] == 1, summary
    row = con.execute(
        "SELECT status, promoted_at, promoted_by FROM candidate "
        "WHERE platform='telegram' AND handle='@target'").fetchone()
    assert row[0] == "promoted"
    assert row[1]
    assert row[2] == promote.PROMOTE_SOURCE


def test_promotion_closes_queue_row_by_tg_id(con):
    """Разные хендлы, общий ``tg_id`` — связь по ``tg_id``."""
    _add_source(con, "byid", posts=config.PROMOTE_MIN_POSTS, external_id="555")
    con.execute(
        "INSERT INTO candidate(platform, kind, handle, external_id, status) "
        "VALUES('telegram','channel','other_name','555','new')"
    )
    con.commit()
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 1
    assert summary["queue_closed"] == 1, summary
    row = con.execute(
        "SELECT status, promoted_at FROM candidate WHERE handle='other_name'").fetchone()
    assert row[0] == "promoted" and row[1]


def test_missing_queue_row_is_created(con):
    """Нет строки очереди — повышение её заводит, а не теряет кандидата."""
    _add_source(con, "Solo", posts=config.PROMOTE_MIN_POSTS)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 1
    assert summary["queue_created"] == 1, summary
    row = con.execute(
        "SELECT status, promoted_at, found_via, handle FROM candidate "
        "WHERE platform='telegram'").fetchone()
    assert row[0] == "promoted" and row[1]
    assert row[2] == "registry_promote"
    assert row[3] == "solo"  # канонический ключ


def test_idempotent(con):
    _add_source(con, "rich", posts=config.PROMOTE_MIN_POSTS)
    first = promote.promote_candidates(con, dry_run=False)
    assert first["promoted"] == 1
    assert _status(con, "rich") == "active"
    second = promote.promote_candidates(con, dry_run=False)
    assert second["promoted"] == 0
    assert second["checked"] == 0
    assert _status(con, "rich") == "active"
    n = con.execute("SELECT COUNT(*) FROM candidate WHERE platform='telegram'").fetchone()[0]
    assert n == 1, "повторный прогон размножил строки очереди"


def test_protected_statuses_untouched(con):
    for status in ("private", "dead", "rejected"):
        _add_source(con, f"chan_{status}", status=status,
                    posts=config.PROMOTE_MIN_POSTS + 5)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["promoted"] == 0
    assert summary["checked"] == 0  # в выборку кандидатов они вообще не попали
    for status in ("private", "dead", "rejected"):
        assert _status(con, f"chan_{status}") == status


def test_active_never_downgraded(con):
    _add_source(con, "already", status="active", posts=0, subs=None)
    promote.promote_candidates(con, dry_run=False)
    assert _status(con, "already") == "active"


def test_dry_run_does_not_write(con):
    _add_source(con, "rich", posts=config.PROMOTE_MIN_POSTS)
    summary = promote.promote_candidates(con, dry_run=True)
    assert summary["promoted"] == 1
    assert summary["dry_run"] is True
    assert _status(con, "rich") == "candidate"
    assert con.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Вырождение и пустой вход
# ---------------------------------------------------------------------------

def test_degenerate_threshold_aborts(con, monkeypatch):
    """Если проходят ВСЕ подряд на заметной выборке — боевой прогон отменяется."""
    monkeypatch.setattr(config, "PROMOTE_DEGENERATE_MIN_CHECKED", 3)
    for i in range(3):
        _add_source(con, f"all{i}", posts=config.PROMOTE_MIN_POSTS)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["degenerate"] is True
    assert summary.get("aborted") is True
    assert summary["promoted"] == 0
    assert _status(con, "all0") == "candidate"


def test_degenerate_check_not_triggered_when_some_removed(con, monkeypatch):
    monkeypatch.setattr(config, "PROMOTE_DEGENERATE_MIN_CHECKED", 3)
    for i in range(3):
        _add_source(con, f"chan{i}", posts=config.PROMOTE_MIN_POSTS)
    _add_source(con, "poor", posts=0)
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["degenerate"] is False
    assert summary["promoted"] == 3


def test_empty_input(con):
    summary = promote.promote_candidates(con, dry_run=False)
    assert summary["checked"] == 0
    assert summary["promoted"] == 0
    assert summary["degenerate"] is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_subcommand_runs(tg_path, capsys):
    con = db.connect(tg_path)
    _add_source(con, "cli_chan", posts=config.PROMOTE_MIN_POSTS)
    con.close()
    rc = promote.main(["--db", tg_path])
    assert rc == 0
    out = capsys.readouterr().out
    assert "повышено: 1" in out
    con = db.connect(tg_path)
    assert _status(con, "cli_chan") == "active"
    con.close()


def test_cli_dry_run(tg_path, capsys):
    con = db.connect(tg_path)
    _add_source(con, "cli_dry", posts=config.PROMOTE_MIN_POSTS)
    con.close()
    rc = promote.main(["--db", tg_path, "--dry"])
    assert rc == 0
    assert "dry-run" in capsys.readouterr().out
    con = db.connect(tg_path)
    assert _status(con, "cli_dry") == "candidate"
    con.close()


def test_cli_refuses_production_without_flag(tg_path, monkeypatch, capsys):
    con = db.connect(tg_path)
    _add_source(con, "prod", posts=config.PROMOTE_MIN_POSTS)
    con.close()
    monkeypatch.setattr(config, "PRODUCTION_DB", tg_path)
    rc = promote.main(["--db", tg_path])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--allow-production" in err


def test_cli_degenerate_returns_three(tg_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "PROMOTE_DEGENERATE_MIN_CHECKED", 2)
    con = db.connect(tg_path)
    for i in range(2):
        _add_source(con, f"d{i}", posts=config.PROMOTE_MIN_POSTS)
    con.close()
    rc = promote.main(["--db", tg_path])
    assert rc == 3
    assert "вырождение" in capsys.readouterr().out
