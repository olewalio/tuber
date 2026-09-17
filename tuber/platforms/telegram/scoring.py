#!/usr/bin/env python3
"""Tuber-Telegram: скоринг значимости постов (ТЗ-виральность, части 1 и 2).

Собирает то, чего в базе не было:
  * ``posts.forwards`` — бэкфилл из разметки t.me/s (Part 1.1; замер 16.09.2026:
    веб-превью счётчик форвардов не отдаёт, поэтому ожидаемо 0 — см. отчёт);
  * ``channel_baselines`` — медианы просмотров/реакций/ER по постам канала за
    скользящее окно (Part 1.2) и признак агрегатора данными (Part 1.3);
  * ``scores`` — значимость поста по формуле DESIGN.md:173/232 (Part 2.1..2.3).

Формула (docs/DESIGN.md:173, :232):

    Significance = Fr × Wsrc × Xconf × Eng × (1 − DupPenalty) × TopicWeight

плюс распад по возрасту ``decay = 0.5 ** (age_days / half_life_days)``.

Порог микровыборки честного рейтинга (ТЗ от 16.09.2026): пост попадает в честный
рейтинг только при ``views >= min_views`` И ``reactions >= min_reactions``
(config/scoring.json, по умолчанию 100 и 5). Порог — часть ранжирования (SQL-выборки),
а не печати. Посты ниже порога не исчезают: они уходят отдельным блоком
«МАЛАЯ ВЫБОРКА» с числом отсечённых в шапке; посты ниже ``top_n`` в честном
рейтинге выдаются как есть с предупреждением, список не добивается мелочью.
``views``/``reactions IS NULL`` трактуется как «порог не пройден» (нельзя
подтвердить достаточность) и НЕ подменяется нулём.

Границы честности (не выдумано, помечено долгом):
  * ``Fr`` в DESIGN не определён однозначно → 1.0 (D-02);
  * ``TopicWeight`` требует классификатора тем (этап 2) → 1.0 (D-03);
  * реклама по-прежнему keyword-эвристикой сборщика → нужен семантический
    классификатор (D-04);
  * ``Xconf`` пока по точному ``text_hash`` без кластера simhash (D-05).

Защита рабочей БД: без ``--allow-production`` скрипт отказывается писать в
``data/tuber.db`` монорепозитория. Проверки — на копии::

    TUBER_TELEGRAM_DB=/tmp/tg_copy.db python3 -m tuber tg scoring all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from . import collect as C  # noqa: E402  (parse_page, parse_count, extract_forwards)
from . import config as _config
from . import store as db
from tuber.core import urls

ROOT = _config.ROOT
DEFAULT_DB = _config.DEFAULT_DB
CONFIG_PATH = _config.SCORING_CONFIG
REPORTS_DIR = _config.REPORTS_DIR

# Дефолты конфига. Источник истины — config/scoring.json; этот словарь нужен только
# если файла конфига нет. Расхождение ловит тест test_config_constants_not_duplicated_in_code
# (требование 7: числа формулы не размазаны по коду).
DEFAULTS = {
    "half_life_days": 30.0,
    "window_days": 90,
    "min_posts_in_window": 5,
    "er_anomaly_threshold": 0.5,
    "dup_window_hours": 48,
    "author_dup_ratio_threshold": 0.5,
    "min_posts_for_author": 5,
    "top_n": 15,
    "min_views": 100,
    "min_reactions": 5,
}

# Схема БД больше не создаётся этим модулем: её владелец — ядро, а legacy-форму
# таблиц ``channel_baselines``/``scores`` отдаёт адаптер
# :mod:`tuber.platforms.telegram.store` (ТЗ-4). UPSERT-семантику перенесли в
# триггеры представлений (SQLite: «cannot UPSERT a view»).


def load_config(path: str = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for k in DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
        cfg["_path"] = path
    except OSError:
        cfg["_path"] = None
    return cfg


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sql_now() -> str:
    return utcnow().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value: str) -> datetime | None:
    """Разобрать date_utc. Строки без таймзоны (старый prelim_ingest) считаем UTC.

    Иначе вычитание наивной даты из aware-`now` бросает TypeError, и возраст
    молча обнуляется (баг: старые посты получали decay=1.0)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ----------------------------------------------------------------------------
# Чистые функции (покрыты тестами без БД)
# ----------------------------------------------------------------------------
def median(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return float(statistics.median(vals))


def er_of(views, reactions):
    """ER = reactions/views. views пусто/0 → None (ТЗ 2.1)."""
    if views is None or reactions is None:
        return None
    if views <= 0:
        return None
    return reactions / views


def combine_eng(eng_channel, eng_global):
    """Свести две базы нормировки в один Eng.

    ТЗ требует ДВЕ базы сравнения (медиана канала и общая медиана). Одна только
    медиана канала позволяет крупному каналу всегда лидировать за счёт базы;
    геометрическое среднее двух отношений сдерживает и то и другое. Если одна
    база недоступна — берётся доступная; если обе — None.
    """
    if eng_channel is None and eng_global is None:
        return None
    if eng_channel is None:
        return eng_global
    if eng_global is None:
        return eng_channel
    # TODO(debt-D-41): геом. среднее двух баз — решение агента, не DESIGN — см. TECH-DEBT.md
    if eng_channel > 0 and eng_global > 0:
        return math.sqrt(eng_channel * eng_global)
    return eng_channel


def dup_penalty_of(xconf: int) -> float:
    """1 − 1/xconf: уникальный текст (xconf=1) → 0; чем больше копий, тем выше."""
    if not xconf or xconf < 1:
        return 0.0
    return 1.0 - 1.0 / xconf


def wsrc_of(reposts: int) -> float:
    """Wsrc = 1 / (1 + число перепостов того же текста)."""
    return 1.0 / (1.0 + max(0, int(reposts)))


def decay_of(age_days: float, half_life_days: float) -> float:
    age = max(0.0, float(age_days))
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age / half_life_days)


def significance_of(*, fr, wsrc, xconf, eng, dup_penalty, topic_weight, decay):
    """Significance = Fr × Wsrc × Xconf × Eng × (1 − DupPenalty) × TopicWeight × decay.

    Eng=None (нет views) → significance None (ТЗ 2.1: не считать при views=0).
    """
    if eng is None:
        return None
    return fr * wsrc * xconf * eng * (1.0 - dup_penalty) * topic_weight * decay


def post_url(handle: str, message_id: int) -> str:
    # Единый хелпер синтеза ссылок (D-45): не дублируем правило Telegram.
    return urls.content_url("telegram", f"{handle}/{message_id}", handle)


# ----------------------------------------------------------------------------
# Защита рабочей БД
# ----------------------------------------------------------------------------
# TODO(debt-D-43): гейт --allow-production только здесь, сборщик пишет по умолчанию — см. TECH-DEBT.md
def assert_can_write(db_path: str, allow_production: bool):
    """Рабочая БД = DEFAULT_DB. Без --allow-production писать в неё запрещено."""
    if db.is_production_db(db_path) and not allow_production:
        raise SystemExit(
            f"отказ: {db_path} — рабочая БД. Проверки делаются на копии "
            "(TUBER_TELEGRAM_DB=/tmp/tg_copy.db); для записи в боевую добавь "
            "--allow-production"
        )


def resolve_db(cli_db: str | None) -> str:
    # --db → TUBER_TELEGRAM_DB → TUBER_DB → боевая единая база (см. config).
    return _config.resolve_db(cli_db)


# TODO(debt-D-42): базы/скоринг считаются по запросу, не в расписании — см. TECH-DEBT.md
def connect(db_path: str) -> sqlite3.Connection:
    """Соединение с ЕДИНОЙ базой ядра + слой совместимости legacy-формы (ТЗ-4)."""
    con = db.connect(db_path)
    return con


# ----------------------------------------------------------------------------
# Part 1.1 — бэкфилл форвардов из t.me/s
# ----------------------------------------------------------------------------
# TODO(debt-D-34): счётчика форвардов нет в t.me/s — нужен MTProto — см. TECH-DEBT.md
def backfill_forwards(con, *, client=None, limit_channels=None, handle=None,
                      deadline=600, sleep_scale=1.0, dry=False):
    """Перечитать страницы каналов и дописать posts.forwards там, где разметка даёт счётчик.

    Возвращает dict-сводку: сколько каналов/постов проверено, сколько обновлено,
    сколько осталось NULL и почему.
    """
    if client is None:
        client = C.make_client()
    before_filled = con.execute("SELECT COUNT(*) FROM posts WHERE forwards IS NOT NULL").fetchone()[0]
    before_total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    where = ["EXISTS (SELECT 1 FROM posts p WHERE p.channel_id = channels.id AND p.forwards IS NULL)"]
    params = []
    if handle:
        where = ["handle = ?"]
        params = [handle]
    sql = f"SELECT id, handle FROM channels WHERE {' AND '.join(where)} ORDER BY COALESCE(subs,0) DESC"
    channels = con.execute(sql, params).fetchall()
    if limit_channels:
        channels = channels[:limit_channels]

    start = time.monotonic()
    checked = updated = pages_total = 0
    errors = 0
    for ch in channels:
        if time.monotonic() - start >= deadline:
            break
        cid, ch_handle = ch["id"], ch["handle"]
        try:
            text = client.get(f"https://t.me/s/{ch_handle}").text
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        parsed = C.parse_page(text, ch_handle)
        pages_total += 1
        checked += len(parsed)
        for p in parsed:
            if p["forwards"] is None:
                continue
            if dry:
                updated += 1
                continue
            # ``rowcount`` у представления всегда 0 (изменения делает
            # INSTEAD OF-триггер) — считаем по ``total_changes``.
            before = con.total_changes
            con.execute(
                "UPDATE posts SET forwards=? WHERE channel_id=? AND message_id=? "
                "AND (forwards IS NULL OR forwards != ?)",
                (p["forwards"], cid, p["message_id"], p["forwards"]),
            )
            updated += db.changes_since(con, before)
        if not dry:
            con.commit()
        if sleep_scale:
            time.sleep(0.5 * sleep_scale)

    after_filled = con.execute("SELECT COUNT(*) FROM posts WHERE forwards IS NOT NULL").fetchone()[0]
    null_left = con.execute("SELECT COUNT(*) FROM posts WHERE forwards IS NULL").fetchone()[0]
    return {
        "channels_scanned": len(channels),
        "pages_fetched": pages_total,
        "posts_seen": checked,
        "forwards_filled_before": before_filled,
        "forwards_filled_after": after_filled,
        "forwards_null_left": null_left,
        "rows_updated": updated,
        "errors": errors,
        "posts_total": before_total,
        "reason_no_data": (
            "разметка t.me/s не содержит счётчика форвардов "
            "(есть только tgme_widget_message_views и ..._forwarded_from)"
        ),
    }


# ----------------------------------------------------------------------------
# Part 1.2 + 1.3 — базы каналов и признак агрегатора данными
# ----------------------------------------------------------------------------
def compute_baselines(con, cfg, *, now=None, dry=False):
    now = now or utcnow()
    window_days = int(cfg["window_days"])
    min_posts = int(cfg["min_posts_in_window"])
    dup_hours = int(cfg["dup_window_hours"])
    author_thr = float(cfg["author_dup_ratio_threshold"])
    author_min = int(cfg["min_posts_for_author"])
    since = (now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")

    # перепосты: для каждого (канал, пост) — есть ли тот же текст в ДРУГОМ канале ±dup_hours
    dup_rows = con.execute(
        """
        SELECT p.channel_id AS cid, COUNT(*) AS dup_posts
        FROM posts p
        WHERE p.text_hash IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM posts q
            WHERE q.text_hash = p.text_hash AND q.channel_id != p.channel_id
              AND ABS(strftime('%s', q.date_utc) - strftime('%s', p.date_utc))
                  <= ? * 3600
          )
        GROUP BY p.channel_id
        """,
        (dup_hours,),
    ).fetchall()
    dup_by_cid = {r["cid"]: r["dup_posts"] for r in dup_rows}

    # посты каналов в окне
    post_rows = con.execute(
        """
        SELECT channel_id AS cid, views, reactions, text_hash
        FROM posts WHERE datetime(date_utc) >= datetime(?)
        """,
        (since,),
    ).fetchall()
    per_channel: dict[int, dict] = {}
    for r in post_rows:
        d = per_channel.setdefault(r["cid"], {"views": [], "reactions": [], "er": [], "hashed": 0})
        if r["views"] is not None:
            d["views"].append(r["views"])
        if r["reactions"] is not None:
            d["reactions"].append(r["reactions"])
        e = er_of(r["views"], r["reactions"])
        if e is not None:
            d["er"].append(e)
        if r["text_hash"]:
            d["hashed"] += 1

    written = 0
    # Признак авторства по данным пишем в channels.is_author (Part 1.3).
    # Каналы, не попавшие в базы (< min_posts в окне), оставляем NULL = «неизвестно»:
    # это не ручная разметка, и выдавать «не знаем» за «агрегатор (0)» нельзя.
    if not dry:
        con.execute("UPDATE channels SET is_author=NULL")
    for cid, d in per_channel.items():
        # число постов в окне = всего строк этого канала в окне (не только с views)
        total_in_window = con.execute(
            "SELECT COUNT(*) FROM posts WHERE channel_id=? AND datetime(date_utc) >= datetime(?)", (cid, since)
        ).fetchone()[0]
        if total_in_window < min_posts:
            continue
        dup = dup_by_cid.get(cid, 0)
        hashed = d["hashed"]
        dup_ratio = (dup / hashed) if hashed else 0.0
        # Мало постов с текстом (< author_min) → данных на вывод нет, оставляем NULL.
        # TODO(debt-D-39): точный text_hash не ловит почти-дубли — см. TECH-DEBT.md
        if hashed >= author_min:
            is_author = 1 if dup_ratio < author_thr else 0
        else:
            is_author = None
        if dry:
            written += 1
            continue
        # UPSERT выполняет INSTEAD OF-триггер представления channel_baselines
        # (SQLite не умеет UPSERT по представлению), семантика прежняя.
        con.execute(
            """INSERT INTO channel_baselines
               (channel_id, window_days, posts_in_window, median_views, median_reactions,
                median_er, is_author_data, hashed_posts, dup_posts, dup_ratio, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, window_days, total_in_window, median(d["views"]), median(d["reactions"]),
             median(d["er"]), is_author, hashed, dup, dup_ratio, sql_now()),
        )
        # признак агрегатора по данным пишем в channels.is_author (Part 1.3)
        con.execute("UPDATE channels SET is_author=? WHERE id=?", (is_author, cid))
        written += 1
    if not dry:
        con.commit()
    return {"channels_in_window": len(per_channel), "baselines_written": written,
            "window_days": window_days, "min_posts_in_window": min_posts}


# ----------------------------------------------------------------------------
# Part 2.1..2.3 — scores
# ----------------------------------------------------------------------------
def global_median_er(con):
    rows = con.execute("SELECT views, reactions FROM posts WHERE views > 0 AND reactions IS NOT NULL").fetchall()
    return median([er_of(r["views"], r["reactions"]) for r in rows])


def compute_scores(con, cfg, *, now=None, dry=False):
    now = now or utcnow()
    half_life = float(cfg["half_life_days"])
    er_thr = float(cfg["er_anomaly_threshold"])

    base = {r["channel_id"]: r for r in con.execute("SELECT * FROM channel_baselines").fetchall()}
    g_er = global_median_er(con)

    # TODO(debt-D-38): Xconf только по точному text_hash, без кластера simhash — см. TECH-DEBT.md
    hash_stats = {}
    for r in con.execute(
        "SELECT text_hash, COUNT(DISTINCT channel_id) AS chans, COUNT(*) AS n "
        "FROM posts WHERE text_hash IS NOT NULL GROUP BY text_hash"
    ):
        hash_stats[r["text_hash"]] = (r["chans"], r["n"])

    distinct_channels = 0
    posts_scored = 0
    posts_null = 0
    anomalies = 0
    seen_channels = set()
    rows = con.execute(
        "SELECT id, channel_id, message_id, date_utc, views, reactions, text_hash FROM posts"
    ).fetchall()
    for r in rows:
        seen_channels.add(r["channel_id"])
        er = er_of(r["views"], r["reactions"])
        xconf, n_hash = hash_stats.get(r["text_hash"]) or (1, 1)
        reposts = max(0, n_hash - 1)
        b = base.get(r["channel_id"])
        med_er = b["median_er"] if b else None
        eng_channel = (er / med_er) if (er is not None and med_er) else None
        eng_global = (er / g_er) if (er is not None and g_er) else None
        eng = combine_eng(eng_channel, eng_global)
        dt = parse_dt(r["date_utc"])
        age = ((now - dt).total_seconds() / 86400.0) if dt else 0.0
        decay = decay_of(age, half_life)
        dp = dup_penalty_of(xconf)
        wsrc = wsrc_of(reposts)
        # TODO(debt-D-35): Fr не определён в DESIGN → 1.0 — см. TECH-DEBT.md
        # TODO(debt-D-36): TopicWeight требует классификатора тем (этап 2) → 1.0 — см. TECH-DEBT.md
        fr = 1.0
        topic_weight = 1.0
        sig = significance_of(fr=fr, wsrc=wsrc, xconf=xconf, eng=eng,
                              dup_penalty=dp, topic_weight=topic_weight, decay=decay)
        anomaly = 1 if (er is not None and er > er_thr) else 0
        if anomaly:
            anomalies += 1
        if sig is None:
            posts_null += 1
        else:
            posts_scored += 1
        if dry:
            continue
        # UPSERT выполняет INSTEAD OF-триггер представления scores: legacy-таблица
        # ``scores`` держит РОВНО одну строку на пост (PK post_id), поэтому
        # триггер заменяет строку поста, а не копит историю по computed_at.
        con.execute(
            """INSERT INTO scores
               (post_id, er, eng_channel, eng_global, eng, xconf, wsrc, dup_penalty,
                fr, topic_weight, age_days, decay, significance, anomaly, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["id"], er, eng_channel, eng_global, eng, xconf, wsrc, dp,
             fr, topic_weight, age, decay, sig, anomaly, sql_now()),
        )
    if not dry:
        con.commit()
    return {
        "posts_total": len(rows),
        "channels_total": len(seen_channels),
        "posts_scored": posts_scored,
        "posts_null_views": posts_null,
        "anomalies": anomalies,
        "global_median_er": g_er,
        "channels_with_baseline": len(base),
        "half_life_days": half_life,
        "er_anomaly_threshold": er_thr,
    }


# ----------------------------------------------------------------------------
# Part 2.3 — фильтры рейтинга
# ----------------------------------------------------------------------------
def ranking_filters_sql(alias="p", chan="c"):
    """SQL-условие честного рейтинга: не реклама, канал не flooded/flood_until в будущем."""
    return (
        f" AND COALESCE({alias}.is_ad,0)=0 "
        f" AND COALESCE({chan}.status,'') != 'flooded' "
        f" AND ({chan}.flood_until IS NULL OR {chan}.flood_until <= datetime('now')) "
    )


# ----------------------------------------------------------------------------
# Part 2.4 — порог микровыборки честного рейтинга (ТЗ от 16.09.2026)
# ----------------------------------------------------------------------------
# Порог показов/реакций — часть ФОРМУЛЫ ранжирования (ТЗ п.4), а не косметика
# печати: он применяется в SQL-выборке честного рейтинга, поэтому воспроизводим.
# NULL трактуется как «порог не пройден»: нельзя подтвердить достаточность
# показов/реакций, а выдумывать значения запрещено (ТЗ «не подменять NULL»).
def threshold_clauses(cfg, alias="p"):
    """Вернуть (views_ok, reactions_ok) — SQL-условия порога честного рейтинга."""
    # TODO(debt-D-44): min_views/min_reactions откалиброваны по снимку 16.09.2026;
    # после классификатора тем (D-03) и форвардов (D-01) перекалибровать — см. TECH-DEBT.md
    mv = int(cfg["min_views"])
    mr = int(cfg["min_reactions"])
    views_ok = f"({alias}.views IS NOT NULL AND {alias}.views >= {mv})"
    react_ok = f"({alias}.reactions IS NOT NULL AND {alias}.reactions >= {mr})"
    return views_ok, react_ok


def _count_sample(con, where_sql):
    row = con.execute(
        f"""SELECT COUNT(*) AS posts, COUNT(DISTINCT p.channel_id) AS channels
            FROM scores s JOIN posts p ON p.id=s.post_id JOIN channels c ON c.id=p.channel_id
            WHERE 1=1 {where_sql}"""
    ).fetchone()
    return {"posts": row["posts"], "channels": row["channels"]}


def threshold_table(con, cfg):
    """Чувствительность порога (ТЗ п.1): сколько постов/каналов остаётся в честном
    рейтинге при порогах показов 100/200/300/500 и реакций 3/5/10.

    База — честный рейтинг без порога (не реклама, не flooded, не аномалия,
    significance посчитана). Пересчитывается на боевой копии при каждом отчёте.
    """
    filters = ranking_filters_sql()
    base = filters + " AND COALESCE(s.anomaly,0)=0 AND s.significance IS NOT NULL"
    rows = []
    for v in (100, 200, 300, 500):
        s = _count_sample(con, base + f" AND p.views IS NOT NULL AND p.views >= {v}")
        rows.append((f"views>={v}", s["posts"], s["channels"]))
    for r in (3, 5, 10):
        s = _count_sample(con, base + f" AND p.reactions IS NOT NULL AND p.reactions >= {r}")
        rows.append((f"reactions>={r}", s["posts"], s["channels"]))
    return rows


# ----------------------------------------------------------------------------
# Part 2.5 — выдача
# ----------------------------------------------------------------------------
def _fetch_ranked(con, *, order_by, where_extra="", limit=15, require_significance=True):
    sig_cond = " AND s.significance IS NOT NULL" if require_significance else ""
    sql = f"""
      SELECT p.id, p.message_id, p.date_utc, p.views, p.reactions, p.text,
             c.handle, c.title, s.er, s.xconf, s.eng, s.wsrc, s.dup_penalty,
             s.decay, s.significance, s.anomaly
      FROM scores s
      JOIN posts p ON p.id = s.post_id
      JOIN channels c ON c.id = p.channel_id
      WHERE 1=1 {sig_cond} {where_extra}
      ORDER BY {order_by}
      LIMIT ?
    """
    return con.execute(sql, (limit,)).fetchall()


def ranking_sample(con, cfg):
    """Множество честного рейтинга с порогом И блок микровыборки — единый источник
    правды и для выдачи, и для шапки (ТЗ п.2, п.3, п.4).

    Возвращает dict с SQL-условиями и счётчиками; используется build_report.
    """
    filters = ranking_filters_sql()
    no_anomaly = filters + " AND COALESCE(s.anomaly,0)=0"
    anomaly_only = filters + " AND s.anomaly=1"
    views_ok, react_ok = threshold_clauses(cfg)

    # честный рейтинг: значимость + порог показов и реакций
    honest = no_anomaly + f" AND s.significance IS NOT NULL AND {views_ok} AND {react_ok}"
    # «до порога» — та же выборка без порога (для сравнения до/после в приёмке)
    unthresholded = no_anomaly + " AND s.significance IS NOT NULL"
    # малая выборка: всё, что прошло общие фильтры и не аномалия, но в честный
    # рейтинг не попало. Значимость есть, но порог не пройден, ЛИБО значимости нет
    # (views/reactions NULL/0) — такие посты не скрываются и не дорисовываются.
    small = no_anomaly + f" AND (s.significance IS NULL OR NOT ({views_ok} AND {react_ok}))"
    # счётчики отсечённых: разбиение блока без пересечений, чтобы сумма совпадала
    cut_views = small + f" AND s.significance IS NOT NULL AND NOT {views_ok}"
    cut_react = small + f" AND s.significance IS NOT NULL AND {views_ok} AND NOT {react_ok}"
    unscored = small + " AND s.significance IS NULL"
    return {
        "no_anomaly": no_anomaly,
        "anomaly_only": anomaly_only,
        "honest": honest,
        "unthresholded": unthresholded,
        "small": small,
        "cut_views": cut_views,
        "cut_react": cut_react,
        "unscored": unscored,
        "min_views": int(cfg["min_views"]),
        "min_reactions": int(cfg["min_reactions"]),
    }


def build_report(con, cfg, *, top_n=None):
    top_n = int(top_n or cfg["top_n"])
    flt = ranking_sample(con, cfg)
    honest = flt["honest"]
    anomaly_only = flt["anomaly_only"]

    dist = con.execute(
        """SELECT
             SUM(significance IS NOT NULL) AS scored,
             SUM(significance > 0) AS nonzero,
             SUM(anomaly=1) AS anomalies,
             COUNT(*) AS total
           FROM scores s JOIN posts p ON p.id=s.post_id JOIN channels c ON c.id=p.channel_id"""
    ).fetchone()

    buckets = []
    edges = [0.0, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]
    for i, lo in enumerate(edges):
        hi = edges[i + 1] if i + 1 < len(edges) else None
        if hi is None:
            n = con.execute("SELECT COUNT(*) FROM scores WHERE significance >= ?", (lo,)).fetchone()[0]
            label = f"≥{lo:g}"
        else:
            n = con.execute("SELECT COUNT(*) FROM scores WHERE significance >= ? AND significance < ?",
                            (lo, hi)).fetchone()[0]
            label = f"[{lo:g},{hi:g})"
        buckets.append((label, n))

    top_after = _fetch_ranked(
        con, order_by="s.significance DESC, p.reactions DESC", where_extra=honest, limit=top_n
    )
    # «до порога» = честный рейтинг старой формулой выдачи (без порога микровыборки),
    # именно он показывал посты с 14/30 просмотрами.
    top_unthresholded = _fetch_ranked(
        con, order_by="s.significance DESC, p.reactions DESC",
        where_extra=flt["unthresholded"], limit=top_n
    )
    # «до» = по числу упоминаний (xconf), как было заведено в мосте источников
    top_before = _fetch_ranked(
        con, order_by="s.xconf DESC, p.reactions DESC, p.views DESC",
        where_extra=flt["unthresholded"], limit=top_n
    )
    anomalies = _fetch_ranked(
        con, order_by="s.er DESC, p.reactions DESC", where_extra=anomaly_only, limit=top_n
    )
    # малая выборка сортируется по значимости; NULL (views/reactions неизвестны) — в конец
    small_sample = _fetch_ranked(
        con, order_by="(s.significance IS NULL), s.significance DESC, p.reactions DESC",
        where_extra=flt["small"], limit=top_n + 5, require_significance=False
    )
    # по сырым реакциям — для доказательства требования 4 (честная подвыборка, без аномалий)
    top_raw = _fetch_ranked(con, order_by="p.reactions DESC", where_extra=honest, limit=top_n)

    sample = con.execute(
        f"""SELECT COUNT(*) AS posts, COUNT(DISTINCT p.channel_id) AS channels
            FROM posts p JOIN channels c ON c.id=p.channel_id WHERE 1=1 {ranking_filters_sql()}"""
    ).fetchone()
    honest_sample = con.execute(
        f"""SELECT COUNT(*) AS posts, COUNT(DISTINCT p.channel_id) AS channels
            FROM posts p JOIN scores s ON s.post_id=p.id JOIN channels c ON c.id=p.channel_id
            WHERE 1=1 {flt['unthresholded']}"""
    ).fetchone()

    remaining = _count_sample(con, flt["honest"])
    cut_views = _count_sample(con, flt["cut_views"])
    cut_react = _count_sample(con, flt["cut_react"])
    unscored = _count_sample(con, flt["unscored"])
    small_total = _count_sample(con, flt["small"])
    # инвариант: разбиение блока не пересекается и покрывает блок целиком
    assert (cut_views["posts"] + cut_react["posts"] + unscored["posts"]
            == small_total["posts"]), "счётчики отсечённых не совпадают с блоком «малая выборка»"

    after_ids = {r["id"] for r in top_after}
    raw_ids = {r["id"] for r in top_raw}
    dropped = [r for r in top_raw if r["id"] not in after_ids][:3]
    risen = [r for r in top_after if r["id"] not in raw_ids][:3]
    # кандидаты, выпавшие из топа именно из-за порога (для приёмки п.3)
    dropped_by_threshold = [r for r in top_unthresholded if r["id"] not in after_ids]

    return {
        "dist": dict(dist),
        "buckets": buckets,
        "top_after": top_after,
        "top_before": top_before,
        "top_unthresholded": top_unthresholded,
        "anomalies": anomalies,
        "small_sample": small_sample,
        "dropped_from_raw": dropped,
        "risen_from_raw": risen,
        "dropped_by_threshold": dropped_by_threshold,
        "sample": dict(sample),
        "honest_sample": dict(honest_sample),
        "remaining": remaining,
        "cut_views": cut_views,
        "cut_react": cut_react,
        "unscored": unscored,
        "small_total": small_total,
        "min_views": flt["min_views"],
        "min_reactions": flt["min_reactions"],
        "threshold_table": threshold_table(con, cfg),
        "top_n": top_n,
    }


def _row_line(r):
    sig = r["significance"]
    sigs = "NULL" if sig is None else f"{sig:.4g}"
    er = "NULL" if r["er"] is None else f"{r['er']*100:.1f}%"
    return (f"  @{r['handle'][:24]:24s} {str(r['date_utc'])[:10]} "
            f"views={str(r['views']):>8} react={str(r['reactions']):>6} ER={er:>7} "
            f"xconf={r['xconf']:>2} sig={sigs:>9} {post_url(r['handle'], r['message_id'])}")


def _small_line(r, min_views, min_reactions):
    """Строка блока «малая выборка» с пометкой, по какому порогу отсечён пост."""
    if r["views"] is None:
        why = "views=NULL"
    elif r["views"] < min_views:
        why = f"views<{min_views}"
    elif r["reactions"] is None:
        why = "react=NULL"
    else:
        why = f"react<{min_reactions}"
    return _row_line(r) + f"  [{why}]"


def format_report(rep, cfg):
    mv, mr = rep["min_views"], rep["min_reactions"]
    remaining = rep["remaining"]["posts"]
    threshold = f"views>={mv} И reactions>={mr}"

    L = []
    A = L.append
    A("=== Tuber-Telegram: выдача по значимости ===")
    A(f"выборка после фильтров (без рекламы и flooded): постов {rep['sample']['posts']}, "
      f"каналов {rep['sample']['channels']}")
    A(f"честный рейтинг без порога (без аномалий ER>{cfg['er_anomaly_threshold']*100:g}%): "
      f"постов {rep['honest_sample']['posts']}, каналов {rep['honest_sample']['channels']}")
    # ТЗ п.3: шапка сообщает разбивку отсечённых и остаток честного рейтинга
    A(f"ПОРОГ ЧЕСТНОГО РЕЙТИНГА: {threshold} (config/scoring.json: min_views={mv}, min_reactions={mr})")
    A(f"  отсечено порогом показов: {rep['cut_views']['posts']} "
      f"(значимость есть, но views<{mv}; каналов {rep['cut_views']['channels']})")
    A(f"  из прошедших показы отсечено порогом реакций: {rep['cut_react']['posts']} "
      f"(reactions<{mr}; каналов {rep['cut_react']['channels']})")
    A(f"  не оценены (views/reactions NULL или 0): {rep['unscored']['posts']} "
      f"(не ранжируются и не дорисовываются; каналов {rep['unscored']['channels']})")
    A(f"  всего в блоке «малая выборка»: {rep['small_total']['posts']} "
      f"(каналов {rep['small_total']['channels']})")
    A(f"  осталось в честном рейтинге: {remaining} постов "
      f"(каналов {rep['remaining']['channels']})")
    if remaining < rep["top_n"]:
        A(f"  ВНИМАНИЕ: после порога в честном рейтинге меньше top_n ({remaining} < {rep['top_n']}) — "
          f"выдан весь остаток; список НЕ добивается постами ниже порога.")
    A(f"top_n={rep['top_n']}, half_life_days={cfg['half_life_days']}, "
      f"er_anomaly_threshold={cfg['er_anomaly_threshold']}")
    A("")
    A(f"Топ-{rep['top_n']} ПО ЗНАЧИМОСТИ [ПОСЛЕ порога] (реклама и flooded исключены):")
    for r in rep["top_after"]:
        A(_row_line(r))
    if not rep["top_after"]:
        A("  (пусто)")
    A("")
    A(f"Топ-{rep['top_n']} ПО ЗНАЧИМОСТИ [ДО порога — как было, включая микровыборку]:")
    for r in rep["top_unthresholded"]:
        A(_row_line(r))
    A("")
    A("ОТДЕЛЬНЫЙ БЛОК — МАЛАЯ ВЫБОРКА (не идут в честный рейтинг):")
    A(f"  отсечено постов: {rep['small_total']['posts']} "
      f"(показы<{mv}: {rep['cut_views']['posts']}, реакции<{mr}: {rep['cut_react']['posts']}, "
      f"без данных: {rep['unscored']['posts']})")
    for r in rep["small_sample"]:
        A(_small_line(r, mv, mr))
    shown = len(rep["small_sample"])
    if rep["small_total"]["posts"] > shown:
        A(f"  ... показаны первые {shown} из {rep['small_total']['posts']} "
          f"(остальные не скрыты — блок несёт полное число в шапке).")
    if not rep["small_sample"]:
        A("  (нет)")
    A("")
    A(f"ОТДЕЛЬНЫЙ БЛОК — помечены как аномалия "
      f"(ER>{cfg['er_anomaly_threshold']*100:g}%, исключены из честного рейтинга):")
    for r in rep["anomalies"]:
        A(_row_line(r))
    if not rep["anomalies"]:
        A("  (нет)")
    A("")
    A("Доказательство (приёмка п.3): выпали из топа именно из-за порога:")
    for r in rep["dropped_by_threshold"]:
        A(f"  views={r['views']} react={r['reactions']} "
          f"er={('%.1f%%'%(r['er']*100)) if r['er'] is not None else 'NULL'} "
          f"sig={('%g'%r['significance']) if r['significance'] is not None else 'NULL'} "
          f"@{r['handle']} {post_url(r['handle'], r['message_id'])}")
    if not rep["dropped_by_threshold"]:
        A("  (топ не изменился)")
    A("")
    A(f"Топ-{rep['top_n']} ДО (по числу упоминаний xconf, как было):")
    for r in rep["top_before"]:
        A(_row_line(r))
    A("")
    A("Распределение значимости:")
    A(f"  всего строк scores: {rep['dist']['total']}, со значимостью: {rep['dist']['scored']}, "
      f"ненулевых: {rep['dist']['nonzero']}, аномалий: {rep['dist']['anomalies']}")
    for label, n in rep["buckets"]:
        A(f"  {label:>12}: {n}")
    A("")
    A("Подбор порога (ТЗ п.1): сколько постов/каналов остаётся в честном рейтинге")
    A("  (база: не реклама, не flooded, не аномалия, significance посчитана):")
    for label, posts, channels in rep["threshold_table"]:
        A(f"  {label:>14}: постов {posts:>5}, каналов {channels:>4}")
    A("")
    A("Доказательство (требование 4): ушли вниз из топа по СЫРЫМ реакциям:")
    for r in rep["dropped_from_raw"]:
        A(f"  react={r['reactions']} er={('%.1f%%'%(r['er']*100)) if r['er'] is not None else 'NULL'} "
          f"xconf={r['xconf']} sig={('%g'%r['significance']) if r['significance'] is not None else 'NULL'} "
          f"@{r['handle']} {post_url(r['handle'], r['message_id'])}")
    A("Доказательство (требование 4): поднялись в топ по значимости (вне сырого топа):")
    for r in rep["risen_from_raw"]:
        A(f"  react={r['reactions']} er={('%.1f%%'%(r['er']*100)) if r['er'] is not None else 'NULL'} "
          f"xconf={r['xconf']} sig={('%g'%r['significance']) if r['significance'] is not None else 'NULL'} "
          f"@{r['handle']} {post_url(r['handle'], r['message_id'])}")
    return "\n".join(L)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Tuber-Telegram scoring / virality")
    ap.add_argument("cmd", choices=["forwards", "baselines", "authors", "scores", "report", "all"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--allow-production", action="store_true")
    ap.add_argument("--limit-channels", type=int, default=None)
    ap.add_argument("--handle", default=None)
    ap.add_argument("--deadline", type=int, default=600)
    ap.add_argument("--top-n", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="печать сводки JSON")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    db_path = resolve_db(args.db)
    cfg = load_config()
    needs_write = args.cmd in ("forwards", "baselines", "scores", "all") and not args.dry_run
    if needs_write:
        assert_can_write(db_path, args.allow_production)
    con = connect(db_path)
    results = {}
    try:
        if args.cmd in ("forwards", "all"):
            results["forwards"] = backfill_forwards(
                con, limit_channels=args.limit_channels, handle=args.handle,
                deadline=args.deadline, dry=args.dry_run,
            )
        if args.cmd in ("baselines", "authors", "all"):
            results["baselines"] = compute_baselines(con, cfg, dry=args.dry_run)
        if args.cmd in ("scores", "all"):
            results["scores"] = compute_scores(con, cfg, dry=args.dry_run)
        if args.cmd in ("report", "all"):
            rep = build_report(con, cfg, top_n=args.top_n)
            text = format_report(rep, cfg)
            # TODO(debt-D-40): выдача — только файл отчёта, нет дайджеста/постера — см. TECH-DEBT.md
            if not args.dry_run:
                os.makedirs(REPORTS_DIR, exist_ok=True)
                path = os.path.join(REPORTS_DIR, f"significance-{utcnow().strftime('%Y-%m-%d')}.md")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
                results["report_path"] = path
            print(text)
        if args.json:
            def default(o):
                return dict(o) if isinstance(o, sqlite3.Row) else str(o)
            print(json.dumps(results, ensure_ascii=False, default=default))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
