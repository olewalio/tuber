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
  * ``Xconf`` — по кластеру почти-дублей simhash (D-38, 23.09.2026), точный
    ``text_hash`` остаётся быстрым путём; ``dup_ratio`` — тоже по кластеру
    (D-39). Порог и калибровка — ``tuber.platforms.telegram.dedup``.

Защита рабочей БД: без ``--allow-production`` скрипт отказывается писать в
``data/tuber.db`` монорепозитория. Проверки — на копии::

    TUBER_TELEGRAM_DB=/tmp/tg_copy.db python3 -m tuber tg scoring all
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import hashlib
import json
import logging
import math
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from . import collect as C  # noqa: E402  (parse_page, parse_count, extract_forwards)
from . import config as _config
from . import dedup
from . import store as db
from tuber.core import urls

ROOT = _config.ROOT
DEFAULT_DB = _config.DEFAULT_DB
CONFIG_PATH = _config.SCORING_CONFIG
REPORTS_DIR = _config.REPORTS_DIR

log = logging.getLogger(__name__)

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

# --- MTProto-бэкфилл форвардов (D-34, ТЗ-B12) -------------------------------
# Форварды нельзя взять из разметки ``t.me/s`` (замер 16.09.2026 = 0), поэтому их
# читает Telethon/MTProto из поля ``Message.forwards`` по каналам реестра.
# Предохранители ТЗ-B12: окно 90 дней, не более 500 каналов за прогон, не более
# 1500 запросов MTProto за волну, пауза между запросами, немедленный стоп при
# FloodWait с записью числа сделанных запросов.
MT_FORWARDS_DAYS = 90
MT_FORWARDS_MAX_CHANNELS = 500
MT_FORWARDS_MAX_REQUESTS = 1500
MT_FORWARDS_MESSAGES_PER_CHANNEL = 100
MT_FORWARDS_PAUSE_SEC = 1.5
#: Окно повторной проверки канала после успешного/пустого прохода (D-34, ТЗ-D-34
#: §2.2). 30 суток: тот же порядок, что half_life_days по умолчанию, и примерно
#: треть 90-дневного окна измерения. Так канал не «залипает» навсегда (свежие
#: посты доберутся за окно), но и не жжёт квоту MTProto каждый прогон.
MT_FORWARDS_RECHECK_DAYS = 30

#: Идемпотентная миграция маркера «форварды проверены» (D-34, ТЗ-D-34 §2.2).
#: Отдельная таблица нужна потому, что ``posts.forwards IS NULL`` — это НЕ
#: «канал не обработан»: Telegram честно отдаёт отсутствие счётчика как None
#: (см. причину 2 в ТЗ). Стиль — как в ``analysis/digest_memory.py``.
_TG_FORWARDS_CHECK_DDL = (
    """
    CREATE TABLE IF NOT EXISTS tg_forwards_check (
      channel_id   INTEGER PRIMARY KEY,
      checked_at   TEXT NOT NULL,
      window_days  INTEGER,
      posts_seen   INTEGER NOT NULL DEFAULT 0,
      posts_filled INTEGER NOT NULL DEFAULT 0,
      note         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tg_forwards_check_at ON tg_forwards_check(checked_at)",
)


def ensure_forwards_check_schema(con) -> None:
    """Создать ``tg_forwards_check`` (повторный вызов безопасен)."""
    for stmt in _TG_FORWARDS_CHECK_DDL:
        con.execute(stmt)


def _forwards_check_table_exists(con) -> bool:
    """Есть ли таблица маркера. Нужно, чтобы ``--dry-run`` НЕ писал в базу."""
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_forwards_check'"
    ).fetchone()
    return row is not None


def _record_forwards_check(con, channel_id, *, window_days, posts_seen,
                           posts_filled, note=None, dry=False) -> None:
    """UPSERT строки «канал проверен» (включая «0 постов» и ошибку сущности)."""
    if dry:
        return
    ensure_forwards_check_schema(con)
    con.execute(
        """
        INSERT INTO tg_forwards_check
          (channel_id, checked_at, window_days, posts_seen, posts_filled, note)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
          checked_at   = excluded.checked_at,
          window_days  = excluded.window_days,
          posts_seen   = excluded.posts_seen,
          posts_filled = excluded.posts_filled,
          note         = excluded.note
        """,
        (int(channel_id), sql_now(), int(window_days), int(posts_seen),
         int(posts_filled), note),
    )

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
    """Свести две базы нормировки в один Eng: ``sqrt(eng_channel × eng_global)``.

    ТЗ требует ДВЕ базы сравнения (медиана канала и общая медиана). Одна только
    медиана канала позволяет крупному каналу всегда лидировать за счёт базы;
    геометрическое среднее двух отношений сдерживает и то и другое. Свойства
    (монотонность по каждой базе, границы между базами, поведение на нуле)
    покрыты тестами и описаны в docs/tg/docs/DESIGN.md §4.4.1.

    Закреплено как решение проекта (D-41, 23.09.2026). Ранжирование топ-50 не
    изменилось: формула была той же, теперь зафиксирована в DESIGN и под тестом.

    Если одна база недоступна — берётся доступная; если обе — None. Ноль в
    одной базе НЕ обнуляет итог (берётся доступная база), поэтому крупный канал
    без общей медианы не выпадает из выдачи.
    """
    if eng_channel is None and eng_global is None:
        return None
    if eng_channel is None:
        return eng_global
    if eng_global is None:
        return eng_channel
    if eng_channel > 0 and eng_global > 0:
        return math.sqrt(eng_channel * eng_global)
    # Одна из баз нулевая: берём положительную, чтобы ноль базы не обнулял
    # итог (ноль базы канала = er=0, тогда и общая база нулевая — вариант
    # return eng_channel остаётся честным нулём).
    if eng_channel > 0:
        return eng_channel
    if eng_global > 0:
        return eng_global
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
def assert_can_write(db_path: str, allow_production: bool):
    """Рабочая БД = DEFAULT_DB. Без --allow-production писать в неё запрещено.

    Тонкая обёртка над ЕДИНЫМ гейтом :func:`store.production_gate_error`
    (D-43): формулировка отказа живёт в одном месте, а этот вход сохранён ради
    обратной совместимости тестов и вызовов платформы.
    """
    msg = db.production_gate_error(db_path, allow_production)
    if msg:
        print(msg, file=sys.stderr)
        raise SystemExit(2)


def resolve_db(cli_db: str | None) -> str:
    # --db → TUBER_TELEGRAM_DB → TUBER_DB → боевая единая база (см. config).
    return _config.resolve_db(cli_db)


def connect(db_path: str) -> sqlite3.Connection:
    """Соединение с ЕДИНОЙ базой ядра + слой совместимости legacy-формы (ТЗ-4)."""
    con = db.connect(db_path)
    return con


# ----------------------------------------------------------------------------
# Part 1.1 — бэкфилл форвардов из t.me/s
# ----------------------------------------------------------------------------
# D-34 (закрыт 24.09.2026): счётчика форвардов нет в t.me/s — нужен MTProto — см. TECH-DEBT.md
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
# D-34 — MTProto-бэкфилл форвардов (Message.forwards через Telethon)
# ----------------------------------------------------------------------------
# Приоритет канала — СУЩЕСТВУЮЩАЯ метрика проекта ``candidate.score_priority``
# (в ``SCHEMA.md`` legacy-поле ``priority`` отображается в ``score_priority``;
# ту же метрику использует приоритетный обход очередей в волнах B10/B11).
# Связь очередь↔реестр — по каноническому хендлу (нижний регистр). У канала без
# строки-кандидата приоритет = 0; тайбрейк — подписчики (``subs``), затем хендл.
def select_priority_channels(con, *, limit: int = MT_FORWARDS_MAX_CHANNELS,
                             days: int = MT_FORWARDS_DAYS,
                             recheck_days: int = MT_FORWARDS_RECHECK_DAYS) -> list[dict]:
    """Каналы реестра по убыванию приоритета (``candidate.score_priority``, ``subs``).

    В выборку попадают ТОЛЬКО живые каналы: у канала есть хотя бы один пост в
    ``content`` внутри окна ``days`` (D-34, ТЗ-D-34 §2.3). Мёртвые каналы (последний
    пост 2022–2026 вне 90-дневного окна) не могут дать результата, но каждый прогон
    жгли MTProto-запросы и загоняли учётку в FloodWait.

    Возобновляемость (D-34, ТЗ-D-34 §2.2): канал, попавший в ``tg_forwards_check``
    свежее ``recheck_days``, исключается ДОПОЛНИТЕЛЬНО — независимо от того,
    нашёлся ли у него счётчик. Прежний признак «есть пост с ``forwards IS NOT NULL``»
    остаётся (он отсекает уже измеренные каналы), но один его недостаточно: Telegram
    отдаёт отсутствие счётчика как ``None``, значение остаётся ``NULL``, и без маркера
    такой канал выбирался бы вечно. Если таблицы маркера ещё нет (первый запуск после
    обновления / ``--dry-run`` на копии), фильтр по маркеру просто не применяется.
    """
    params: list = [f"-{int(days)} day"]
    check_clause = ""
    if _forwards_check_table_exists(con):
        check_clause = (
            " AND NOT EXISTS (SELECT 1 FROM tg_forwards_check f"
            "                 WHERE f.channel_id = s.id"
            "                   AND f.checked_at > datetime('now', ?))"
        )
        params.append(f"-{int(recheck_days)} day")
    params.append(int(limit))
    rows = con.execute(
        f"""
        SELECT s.id AS id, s.handle AS handle, s.status AS status,
               s.subs AS subs, COALESCE(c.priority, 0) AS priority
        FROM source s
        LEFT JOIN (
            SELECT lower(handle) AS h, MAX(score_priority) AS priority
            FROM candidate WHERE platform='telegram' AND handle IS NOT NULL
            GROUP BY lower(handle)
        ) c ON c.h = lower(s.handle)
        WHERE s.platform='telegram' AND s.handle IS NOT NULL AND s.handle != ''
          AND EXISTS (SELECT 1 FROM content ct
                      WHERE ct.source_id = s.id AND ct.platform='telegram'
                        AND datetime(ct.published_at) >= datetime('now', ?))
          AND NOT EXISTS (SELECT 1 FROM content ct2
                      JOIN content_latest cl ON cl.content_id = ct2.id
                      WHERE ct2.source_id = s.id AND ct2.platform='telegram'
                        AND cl.forwards IS NOT NULL){check_clause}
        ORDER BY priority DESC, COALESCE(s.subs, 0) DESC, s.handle
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def _msg_requests(limit: int) -> int:
    """Сколько запросов MTProto нужно на ``get_messages(limit=N)`` (пачки по 100)."""
    n = int(limit)
    if n <= 0:
        return 0
    return (n + 99) // 100


def _channel_message_ids(con, channel_id: int, *, days: int, limit: int) -> list[int]:
    """``message_id`` постов канала ВНУТРИ окна ``days`` (D-34, ТЗ-D-34 §2.1).

    Читаем строго то, что у нас ЕСТЬ: id берём из представления ``posts``
    (``message_id`` вычисляет ядро из ``content.external_id`` в ``v_tg_posts``), а
    не парсим внешние строки вручную. Окно — по ``published_at``. Возвращаем не
    больше ``limit`` последних по дате id. Именно этих сообщений не хватало
    прежнему ``get_messages(entity, limit=N)``: он возвращал СВЕЖИЕ id канала,
    которых в базе нет, поэтому пересечение было пусто и писалось 0.
    """
    rows = con.execute(
        """
        SELECT message_id FROM posts
        WHERE channel_id = ? AND message_id IS NOT NULL
          AND datetime(date_utc) >= datetime('now', ?)
        ORDER BY datetime(date_utc) DESC, message_id DESC
        LIMIT ?
        """,
        (int(channel_id), f"-{int(days)} day", int(limit)),
    ).fetchall()
    return [int(r["message_id"]) for r in rows]


def _chunks(seq, size: int = 100):
    """Разбить список на пачки не больше ``size`` (Telethon: ids до 100 за вызов)."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _is_flood(exc: BaseException) -> bool:
    """FloodWait ли это (по имени класса, без жёсткой зависимости от Telethon)."""
    from . import comments as CM
    return CM._is_flood(exc)


def _flood_seconds(exc: BaseException) -> int:
    return int(getattr(exc, "seconds", 0) or 0)


#: Вид работы для строки состояния флуда (``transport_account_state.name``).
FLOOD_KIND_FORWARDS = "forwards_mtproto"


def _flood_until_iso(seconds: int) -> str:
    """UTC ISO-время окончания бана: ``now + seconds``."""
    return (utcnow() + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%S")


def save_flood_state(con, seconds: int, *, kind: str = FLOOD_KIND_FORWARDS,
                     dry: bool = False) -> str:
    """Записать окно флуда в ``transport_account_state`` (D-34, ТЗ-B12e).

    Без этого следующий прогон снова шёл бы в бан: CLI определял FloodWait, но
    НИКУДА его не сохранял, а предохранитель обёртки читал лишь одну строку
    состояния. Ключ — ``(platform='telegram', name=kind)``: схема ядра хранит
    вид работы в колонке ``name`` (в копии ТЗ-B12 там лежит ``tg_comments``).

    Сбой записи НЕ валит прогон — только предупреждение в лог; возвращаем
    вычисленное время окончания в любом случае. ``dry=True`` ничего не пишет.
    """
    until = _flood_until_iso(seconds)
    if dry:
        return until
    try:
        from tuber.core import storage
        storage.upsert_transport_account_state(
            con, "telegram", kind, flood_until=until,
            last_error=f"FLOOD_WAIT {int(seconds)}s",
            updated_at=sql_now(),
        )
        con.commit()
    except Exception as exc:  # noqa: BLE001 — сбой записи не должен ронять прогон
        log.warning("tg forwards-mtproto: не удалось записать окно флуда (%s)", exc)
    return until


def _summary_template(*, channels_selected: int, days: int, dry: bool) -> dict:
    """Полная сводка с нулями — чтобы все ключи были даже при пустой выборке."""
    return {
        "channels_selected": int(channels_selected),
        "channels_processed": 0,
        "channels_ok": 0,
        "channels_left": int(channels_selected),
        "messages_seen": 0,
        "forwards_filled": 0,
        "posts_with_forwards_after": 0,
        "requests": 0,
        "errors": 0,
        "flood_wait": 0,
        "flood_until": None,
        "stopped": None,
        "days": int(days),
        "dry_run": bool(dry),
    }


async def _backfill_forwards_mtproto_async(con, channels, *, client, days, messages_per_channel,
                                           max_requests, pause, dry=False):
    """Прочитать ``Message.forwards`` и записать в ``posts.forwards`` (D-34).

    АДРЕСНОЕ чтение (ТЗ-D-34 §2.1): у канала собираем ``message_id``, которые уже
    есть в ``content`` внутри окна, и читаем ровно их
    (``client.get_messages(entity, ids=[...])``, пачки до 100). Прежний
    ``get_messages(entity, limit=N)`` возвращал свежие id канала, которых в базе нет,
    поэтому пересечение было пусто и FORWARDS_FILLED всегда = 0.
    """
    before_filled = con.execute("SELECT COUNT(*) FROM posts WHERE forwards IS NOT NULL").fetchone()[0]
    summary = {
        "channels_selected": len(channels),
        "channels_processed": 0,
        "channels_ok": 0,
        "channels_left": 0,
        "messages_seen": 0,
        "forwards_filled": 0,
        "posts_with_forwards_after": before_filled,
        "requests": 0,
        "errors": 0,
        "flood_wait": 0,
        "flood_until": None,
        "stopped": None,
        "days": int(days),
        "dry_run": bool(dry),
    }
    requests = 0

    def _budget(n: int = 1) -> bool:
        """Списать ``n`` запросов. False — бюджет исчерпан (запрос НЕ делаем)."""
        nonlocal requests
        if requests + int(n) > max_requests:
            summary["stopped"] = "request_budget"
            return False
        requests += int(n)
        return True

    def _stop_flood(exc, idx):
        summary["flood_wait"] = _flood_seconds(exc)
        summary["stopped"] = "flood_wait"
        summary["channels_left"] = len(channels) - idx
        # D-34 / ТЗ-B12e: сохранить окно флуда, иначе следующий прогон снова
        # пойдёт в бан (предохранитель обёртки не увидит состояния).
        summary["flood_until"] = save_flood_state(con, summary["flood_wait"], dry=dry)

    for idx, ch in enumerate(channels):
        if summary["stopped"]:
            summary["channels_left"] = len(channels) - idx
            break
        handle = ch["handle"]
        cid = int(ch["id"])

        # Строго то, что у нас есть, и только внутри окна (ТЗ-D-34 §2.1/§2.3).
        ids = _channel_message_ids(con, cid, days=days, limit=messages_per_channel)
        if not ids:
            _record_forwards_check(con, cid, window_days=days, posts_seen=0,
                                   posts_filled=0, note="no posts in window", dry=dry)
            if not dry:
                con.commit()
            summary["channels_processed"] += 1
            summary["channels_ok"] += 1
            continue

        if not _budget(1):
            summary["channels_left"] = len(channels) - idx
            break
        try:
            entity = await client.get_entity(handle)
        except Exception as exc:  # noqa: BLE001
            if _is_flood(exc):
                _stop_flood(exc, idx)
                break
            summary["errors"] += 1
            # ТЗ-D-34 §2.4: упавший канал тоже помечаем — уйдёт из пула по сроку
            # повторной проверки, а не будет падать каждый прогон.
            _record_forwards_check(con, cid, window_days=days, posts_seen=0,
                                   posts_filled=0,
                                   note=f"get_entity: {str(exc)[:180]}", dry=dry)
            if not dry:
                con.commit()
            continue

        seen = filled = 0
        stopped_budget = False
        for chunk in _chunks(ids, 100):
            if not _budget(1):
                stopped_budget = True
                break
            try:
                msgs = await client.get_messages(entity, ids=chunk)
            except Exception as exc:  # noqa: BLE001
                if _is_flood(exc):
                    _stop_flood(exc, idx)
                    break
                summary["errors"] += 1
                continue
            for m in (msgs or []):
                if m is None:  # удалённое сообщение (Telethon отдаёт None) — пропуск
                    continue
                seen += 1
                mid = getattr(m, "id", None)
                fwd = getattr(m, "forwards", None)
                if mid is None or fwd is None:
                    # Счётчика нет — это законное «данных нет», а не ошибка.
                    continue
                cur_row = con.execute(
                    "SELECT forwards FROM posts WHERE channel_id=? AND message_id=?",
                    (cid, int(mid)),
                ).fetchone()
                if cur_row is None or cur_row["forwards"] == int(fwd):
                    continue
                if dry:
                    continue
                con.execute(
                    "UPDATE posts SET forwards=? WHERE channel_id=? AND message_id=?",
                    (int(fwd), cid, int(mid)),
                )
                filled += 1
            if pause:
                await asyncio.sleep(pause)

        if stopped_budget:
            summary["channels_left"] = len(channels) - idx
            break
        if summary["stopped"]:
            break

        summary["messages_seen"] += seen
        summary["forwards_filled"] += filled
        if not dry:
            con.commit()
        _record_forwards_check(con, cid, window_days=days, posts_seen=seen,
                               posts_filled=filled, note=None, dry=dry)
        if not dry:
            con.commit()
        summary["channels_processed"] += 1
        summary["channels_ok"] += 1

    summary["requests"] = requests
    if summary["stopped"] is None and summary["channels_left"] == 0:
        summary["channels_left"] = max(0, len(channels) - summary["channels_processed"])
    if not dry:
        summary["posts_with_forwards_after"] = con.execute(
            "SELECT COUNT(*) FROM posts WHERE forwards IS NOT NULL").fetchone()[0]
    return summary


def backfill_forwards_mtproto(con, *, client=None, days: int = MT_FORWARDS_DAYS,
                              limit_channels: int = MT_FORWARDS_MAX_CHANNELS,
                              max_requests: int = MT_FORWARDS_MAX_REQUESTS,
                              messages_per_channel: int = MT_FORWARDS_MESSAGES_PER_CHANNEL,
                              pause: float = MT_FORWARDS_PAUSE_SEC,
                              dry: bool = False) -> dict:
    """Собрать форварды через MTProto (D-34, ТЗ-B12).

    Берёт каналы реестра с наивысшим приоритетом (``candidate.score_priority``,
    тайбрейк ``subs``), читает ``Message.forwards`` за ``days`` дней и пишет в
    ``posts.forwards``. ``client`` можно подставить (асинхронный Telethon-
    совместимый фейк) — тогда сеть/учётка не нужны.
    """
    if not dry:
        # Идемпотентная миграция маркера ДО отбора: первый прогон после
        # обновления ещё не имеет таблицы, а отбор по ней уже хочет фильтровать.
        # Фиксируем сразу, чтобы миграция пережила падение сети/FloodWait.
        ensure_forwards_check_schema(con)
        con.commit()
    channels = select_priority_channels(con, limit=limit_channels)
    if not channels:
        return _summary_template(channels_selected=0, days=days, dry=dry)
    if dry:
        if client is None:
            out = _summary_template(channels_selected=len(channels), days=days, dry=True)
            out["channels_left"] = 0
            out["channels"] = [c["handle"] for c in channels]
            return out
        return asyncio.run(_backfill_forwards_mtproto_async(
            con, channels, client=client, days=days,
            messages_per_channel=messages_per_channel, max_requests=max_requests,
            pause=pause, dry=True))

    own = client is None
    if own:
        async def _run():
            from .comments import make_telethon_client
            c = make_telethon_client()
            await c.connect()
            try:
                if not await c.is_user_authorized():
                    raise RuntimeError("сессия Telethon не авторизована")
                return await _backfill_forwards_mtproto_async(
                    con, channels, client=c, days=days,
                    messages_per_channel=messages_per_channel,
                    max_requests=max_requests, pause=pause, dry=False)
            finally:
                await c.disconnect()
        summary = asyncio.run(_run())
    else:
        summary = asyncio.run(_backfill_forwards_mtproto_async(
            con, channels, client=client, days=days,
            messages_per_channel=messages_per_channel,
            max_requests=max_requests, pause=pause, dry=False))
    summary.setdefault("dry_run", False)
    summary["channels_remaining"] = max(
        0, len(select_priority_channels(con, limit=MT_FORWARDS_MAX_CHANNELS)) - summary.get("channels_processed", 0))
    return summary


# ----------------------------------------------------------------------------
# Part 1.2 + 1.3 — базы каналов и признак агрегатора данными
# ----------------------------------------------------------------------------
def _incremental_watermark(con, table, column="computed_at"):
    """Метка последнего пересчёта (max(computed_at)) для инкремента (D-42).

    Пустая таблица → ``None``: «пересчитано ещё ничего», инкремент обязан взять
    весь корпус (на чистой базе инкремент и полный пересчёт совпадают бит-в-бит).
    """
    row = con.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
    return row[0] if row else None


def compute_baselines(con, cfg, *, now=None, dry=False, incremental=False):
    """Базы каналов: полный (по умолчанию) либо инкрементальный пересчёт.

    ``incremental=True`` (D-42): пересчитываются только каналы, у которых есть
    посты с ``first_seen_at`` позже метки последнего пересчёта, либо у которых
    базы ещё нет вовсе. На одинаковых данных инкремент даёт тот же результат,
    что полный пересчёт (на чистой таблице шлюз ``None`` берёт весь корпус).
    """
    now = now or utcnow()
    window_days = int(cfg["window_days"])
    min_posts = int(cfg["min_posts_in_window"])
    dup_hours = int(cfg["dup_window_hours"])
    author_thr = float(cfg["author_dup_ratio_threshold"])
    author_min = int(cfg["min_posts_for_author"])
    since = (now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")

    # D-39 (23.09.2026): почти-дубли ловит КЛАСТЕР simhash, а точный text_hash
    # остаётся быстрым путём внутри кластера. Порог — dedup.SIMHASH_CLUSTER_THRESHOLD.
    dup_by_cid = {}
    win_posts = con.execute(
        "SELECT id, text, channel_id, date_utc FROM posts"
        " WHERE text IS NOT NULL AND length(trim(text)) > 0"
    ).fetchall()
    dup_data = [(r["id"], r["text"], r["channel_id"], parse_dt(r["date_utc"]))
                for r in win_posts]
    dup_data = [d for d in dup_data if d[3] is not None]
    if dup_data:
        dup_by_cid, _dup_total = dedup.dup_posts_by_channel(dup_data, dup_hours=dup_hours)

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
    # Инкремент НЕ сбрасывает признак всем каналам: он трогает только изменённые
    # (иначе полный сброс обнулил бы «is_author» у каналов вне выборки, D-42).
    changed: set | None = None
    if incremental:
        wm = _incremental_watermark(con, "channel_baselines")
        have = {r[0] for r in con.execute("SELECT channel_id FROM channel_baselines")}
        changed = set(per_channel) - have
        if wm is not None:
            changed |= {
                r[0] for r in con.execute(
                    "SELECT DISTINCT channel_id FROM posts "
                    "WHERE datetime(first_seen_at) > datetime(?)", (wm,))
            }
        changed &= set(per_channel)
    else:
        if not dry:
            con.execute("UPDATE channels SET is_author=NULL")
    for cid, d in per_channel.items():
        if changed is not None and cid not in changed:
            continue
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
        # D-39 (23.09.2026): dup_ratio считается по КЛАСТЕРУ почти-дублей
        # (dedup.dup_posts_by_channel), а не по точному text_hash.
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
            "window_days": window_days, "min_posts_in_window": min_posts,
            "incremental": bool(incremental),
            "channels_selected": len(per_channel) if changed is None else len(changed)}


# ----------------------------------------------------------------------------
# Part 2.1..2.3 — scores
# ----------------------------------------------------------------------------
def global_median_er(con):
    rows = con.execute("SELECT views, reactions FROM posts WHERE views > 0 AND reactions IS NOT NULL").fetchall()
    return median([er_of(r["views"], r["reactions"]) for r in rows])


def forwards_percentile_map(con):
    """Fr-коэффициент поста по форвардам (D-35, 23.09.2026).

    Правило расчёта (закреплено в DESIGN и здесь): ``Fr = 1 + перцентиль`` числа
    форвардов поста среди постов РЕЕСТРА, у которых форварды ИЗМЕРЕНЫ
    (``posts.forwards IS NOT NULL``). Перцентиль — СРЕДНИЙ ранг (доля значений
    строго меньше + половина равных, делённая на объём выборки) в [0, 1]: у самого
    слабого поста ≈ 0, у самого сильного ≈ 1, у медианы ≈ 0.5.

    Свойства: Fr ≥ 1 всегда (форварды не обнуляют пост), монотонен по числу
    форвардов, пост с НЕИЗВЕСТНЫМИ форвардами получает нейтральный 1.0
    (не наказываем за отсутствие данных). Данные форвардов даёт D-34 (MTProto);
    пока их нет, выборка пуста и Fr = 1.0 для ВСЕХ постов — заглушка честно
    совпадает с «нет данных», а не с выдуманным числом.

    Возвращает ``(map post_id → Fr, объём выборки)``.
    """
    rows = con.execute(
        "SELECT id, forwards FROM posts WHERE forwards IS NOT NULL"
    ).fetchall()
    values = sorted(int(r["forwards"]) for r in rows)
    n = len(values)
    out: dict[int, float] = {}
    if n == 0:
        return out, 0
    for r in rows:
        f = int(r["forwards"])
        lo = bisect.bisect_left(values, f)
        hi = bisect.bisect_right(values, f)
        pct = ((lo + hi) / 2.0) / n
        out[r["id"]] = 1.0 + pct
    return out, n


def compute_scores(con, cfg, *, now=None, dry=False, incremental=False):
    """Значимость постов: полный (по умолчанию) либо инкрементальный пересчёт.

    ``incremental=True`` (D-42): пересчитываются только посты, которых ещё нет в
    ``scores``, и посты с ``first_seen_at`` позже метки последнего пересчёта.
    Базы и глобальная медиана считаются по всему корпусу — как в полном
    пересчёте, — поэтому на одинаковых данных инкремент совпадает с полным.
    """
    now = now or utcnow()
    half_life = float(cfg["half_life_days"])
    er_thr = float(cfg["er_anomaly_threshold"])

    base = {r["channel_id"]: r for r in con.execute("SELECT * FROM channel_baselines").fetchall()}
    g_er = global_median_er(con)

    # D-35 (закрыт 24.09.2026): Fr по форвардам не вычислим без боевых данных (сейчас
    # заглушка 1.0). Код forwards_percentile_map готов; пересчёт — после боевого
    # добора форвардов (D-34) — см. TECH-DEBT.md.
    # D-35 (23.09.2026): Fr — измеренный коэффициент по форвардам (см.
    # forwards_percentile_map); пока форвардов нет, словарь пуст и Fr=1.0.
    fr_of, fr_measured = forwards_percentile_map(con)

    # D-38 (23.09.2026): Xconf считается по КЛАСТЕРУ похожих текстов (порог
    # dedup.SIMHASH_CLUSTER_THRESHOLD), а не только по точному text_hash.
    # Точный хеш остаётся быстрым путём/фолбэком для постов без текста.
    hash_stats = {}
    for r in con.execute(
        "SELECT text_hash, COUNT(DISTINCT channel_id) AS chans, COUNT(*) AS n "
        "FROM posts WHERE text_hash IS NOT NULL GROUP BY text_hash"
    ):
        hash_stats[r["text_hash"]] = (r["chans"], r["n"])

    # D-36 (ТЗ-B11): тема поста из классификатора (`classification`, post-level).
    # Тема есть только у постов, помеченных `is_ai=1` (ТЗ-G). Отсутствие темы —
    # законное состояние: вес остаётся нейтральным 1.0 (см. config.topic_weight_of),
    # чтобы непомеченный пост не «проваливался» из-за порядка прогонов.
    topic_of = {}
    for r in con.execute(
        "SELECT content_id, topic FROM classification "
        "WHERE platform='telegram' AND is_ai=1 AND topic IS NOT NULL"
    ):
        topic_of[r["content_id"]] = r["topic"]

    all_rows = con.execute(
        "SELECT id, channel_id, message_id, date_utc, views, reactions, text_hash, text FROM posts"
    ).fetchall()
    id_channel = {r["id"]: r["channel_id"] for r in all_rows}
    cluster_of, clusters = dedup.cluster_by_text(
        (r["id"], r["text"]) for r in all_rows if r["text"] and r["text"].strip()
    )
    cluster_chans = {c: len({id_channel[m] for m in members})
                     for c, members in clusters.items()}
    cluster_size = {c: len(members) for c, members in clusters.items()}

    # Отбор строк к пересчёту: полный корпус либо только новые/незакрытые посты
    # (D-42). Статистика кластеров/хешей выше всё равно берётся по ВСЕМУ корпусу,
    # иначе Xconf отличался бы от полного пересчёта.
    rows = all_rows
    if incremental:
        wm = _incremental_watermark(con, "scores")
        cond = "id NOT IN (SELECT post_id FROM scores)"
        params: tuple = ()
        if wm is not None:
            cond = f"({cond}) OR datetime(first_seen_at) > datetime(?)"
            params = (wm,)
        selected = {r[0] for r in con.execute(f"SELECT id FROM posts WHERE {cond}", params)}
        rows = [r for r in all_rows if r["id"] in selected]

    def _xconf_of(row):
        """(xconf, reposts) поста: из кластера почти-дублей, иначе точный хеш."""
        c = cluster_of.get(row["id"])
        if c is not None:
            return cluster_chans[c], cluster_size[c] - 1
        chans, n = hash_stats.get(row["text_hash"]) or (1, 1)
        return chans, max(0, n - 1)

    distinct_channels = 0
    posts_scored = 0
    posts_null = 0
    anomalies = 0
    seen_channels = set()
    for r in rows:
        seen_channels.add(r["channel_id"])
        er = er_of(r["views"], r["reactions"])
        xconf, reposts = _xconf_of(r)
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
        # D-35 (23.09.2026): Fr из измеренных форвардов (перцентиль; 1.0 без
        # данных). Заглушка 1.0 снята — значение приходит из D-34.
        fr = fr_of.get(r["id"], 1.0)
        # D-36 (ТЗ-B11): TopicWeight теперь из классификатора тем. Тема не
        # распознана → нейтральный 1.0 (не наказываем немаркированный пост).
        # D-36 (закрыт 24.09.2026): классификатор тем подключён (ТЗ-B11), но полный
        # прогон не выполнен — цена замерена на пилоте, ждём решения владельца
        # по порогу; см. TECH-DEBT.md D-36.
        topic_weight = _config.topic_weight_of(topic_of.get(r["id"]))
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
        "posts_total": len(all_rows),
        "posts_selected": len(rows),
        "channels_total": len(seen_channels),
        "posts_scored": posts_scored,
        "posts_null_views": posts_null,
        "anomalies": anomalies,
        "global_median_er": g_er,
        "channels_with_baseline": len(base),
        "half_life_days": half_life,
        "er_anomaly_threshold": er_thr,
        "forwards_posts_measured": fr_measured,
        "incremental": bool(incremental),
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
    # Перекалибровано по НЕСКОЛЬКИМ снимкам (D-44, 23.09.2026): 10 суток
    # 14.09–23.09.2026, 60 922 замера / 54 121 пост. Пороги 100/5 НЕ изменились
    # и подтверждены распределением: views<100 отсекает 8.4% замеров (значение
    # ≈ 10-й перцентиль, p10=127), reactions<5 — 15.9% (p15=4, p20=6). Это
    # совпадает со статистическим обоснованием из config/scoring.json (ошибка ER
    # на n≈100 сопоставима между постами). Снимок — в threshold_audit.
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
    ap.add_argument("cmd", choices=["forwards", "forwards-mtproto", "baselines", "authors", "scores", "report", "all"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--allow-production", action="store_true")
    ap.add_argument("--limit-channels", type=int, default=None)
    ap.add_argument("--handle", default=None)
    ap.add_argument("--deadline", type=int, default=600)
    ap.add_argument("--days", type=int, default=None,
                    help="окно MTProto-бэкфилла форвардов, дней (D-34, по умолчанию 90)")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="потолок запросов MTProto за прогон (D-34, по умолчанию 1500)")
    ap.add_argument("--messages-per-channel", type=int, default=None,
                    help="сколько последних сообщений читать у канала (D-34)")
    ap.add_argument("--pause", type=float, default=None,
                    help="пауза между MTProto-запросами, с (D-34)")
    ap.add_argument("--top-n", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true",
                    help="полный пересчёт баз/скоринга (по умолчанию — инкремент по first_seen_at, D-42)")
    ap.add_argument("--json", action="store_true", help="печать сводки JSON")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    db_path = resolve_db(args.db)
    cfg = load_config()
    # D-42 (23.09.2026): по умолчанию — ИНКРЕМЕНТ (считаем по запросу, а не
    # гоняем весь корпус в расписании). Полный пересчёт — явный флаг --full.
    incremental = not args.full
    needs_write = args.cmd in ("forwards", "forwards-mtproto", "baselines", "scores", "all") and not args.dry_run
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
        if args.cmd == "forwards-mtproto":
            kw = {}
            if args.days is not None:
                kw["days"] = args.days
            if args.limit_channels is not None:
                kw["limit_channels"] = args.limit_channels
            if args.max_requests is not None:
                kw["max_requests"] = args.max_requests
            if args.messages_per_channel is not None:
                kw["messages_per_channel"] = args.messages_per_channel
            if args.pause is not None:
                kw["pause"] = args.pause
            results["forwards_mtproto"] = backfill_forwards_mtproto(
                con, dry=args.dry_run, **kw)
            if not args.json:
                s = results["forwards_mtproto"]
                print(
                    f"Форварды MTProto (D-34): каналов выбрано {s.get('channels_selected', 0)}, "
                    f"обработано {s.get('channels_processed', 0)}, "
                    f"постов с форвардами {s.get('messages_seen', 0)}, "
                    f"заполнено {s.get('forwards_filled', 0)}, "
                    f"запросов {s.get('requests', 0)}, "
                    f"осталось каналов {s.get('channels_remaining', s.get('channels_left', 0))}, "
                    f"стоп: {s.get('stopped') or '—'}"
                    + (f" (flood-wait {s['flood_wait']}s, до {s['flood_until']})"
                       if s.get('flood_wait') else "")
                )
        if args.cmd in ("baselines", "authors", "all"):
            results["baselines"] = compute_baselines(
                con, cfg, dry=args.dry_run, incremental=incremental)
        if args.cmd in ("scores", "all"):
            results["scores"] = compute_scores(
                con, cfg, dry=args.dry_run, incremental=incremental)
        if args.cmd in ("report", "all"):
            rep = build_report(con, cfg, top_n=args.top_n)
            text = format_report(rep, cfg)
            # D-40 (23.09.2026): Tuber — аналитический контур; дайджест/постер в
            # Telegram не нужен, выдача читается файлом отчёта. Решение владельца
            # зафиксировано в docs/tg/docs/DESIGN.md §7 (осознанный отказ).
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
