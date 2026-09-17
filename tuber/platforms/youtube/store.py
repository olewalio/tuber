"""Адаптер хранения YouTube-кода к единому ядру монорепозитория (ТЗ-2).

Зачем он нужен
--------------
YouTube-код (``collect``, ``seo``, ``report``, …) писал прямо в таблицы
``tuber-os`` (``videos``, ``snapshots``, ``channel_candidates``, …). В монорепо
таких таблиц нет: есть единое ядро ``source`` / ``content`` / ``metric_snapshot``
/ ``score`` / ``candidate`` / … (см. :mod:`tuber.core.schema`). Чтобы НЕ
переписывать логику YouTube, этот модуль играет роль прежнего ``tuber/db.py``:

1. отдаёт те же функции (``upsert_video``, ``insert_snapshot``, ``save_score``,
   ``get_query_candidates``…), но пишет и читает единое ядро;
2. на соединении заводит ВРЕМЕННЫЕ (``TEMP``) представления с именами и
   колонками legacy-таблиц (``videos``, ``snapshots``, ``channel_candidates``…).
   Поэтому сырые SQL-запросы YouTube-кода и перенесённых тестов продолжают
   работать без правок: они видят привычные таблицы, но данные приходят из ядра;
3. временные ``INSTEAD OF`` триггеры принимают legacy-запись (INSERT/UPDATE/
   DELETE) и раскладывают её по таблицам ядра (epoch ⇄ ISO через
   :mod:`tuber.core.timeutil`).

Вся работа со схемой ядра (единый источник SQL) живёт здесь; привязка к
платформе — ``platform='youtube'``.

Правила волны ТЗ-2:

* запись — только через :func:`tuber.core.db.write_tx` (``BEGIN IMMEDIATE`` +
  повтор при ``database is locked``; в единой базе пишут три платформы);
* ``video_id`` YouTube → ``content.external_id``, ``channel_id`` → ``source.external_id``;
* ``is_shorts=1`` → ``content.kind='short'``, иначе ``kind='video'``; обратно
  ``kind ∈ {video,short}`` → ``is_shorts``;
* оси ``video_scores``, которых нет колонками в ядре (``vpd_ratio``,
  ``likes_per_1000``, …), читаются/пишутся через ``score.axes_json`` и
  отдаются представлениями как обычные колонки.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from tuber.core import db as core_db
from tuber.core import schema as core_schema
from tuber.core import sqlcompat as core_sqlcompat
from tuber.core import storage, timeutil, urls
from tuber.core.schema import init_schema, migrate_schema

from . import config

log = logging.getLogger(__name__)

# Legacy-имена, совпадающие с именами таблиц ядра (ТЗ-3c). Их представления
# совместимости создаются под «своим» именем: иначе TEMP-схема затеняет таблицу
# ядра и DML из INSTEAD OF-триггера не доходит до ядра (см.
# :mod:`tuber.core.sqlcompat`).
YT_COMPAT_VIEWS: dict[str, str] = {
    "llm_usage": "yt_llm_usage",
    "thumbnail_vision": "yt_thumbnail_vision",
}


class _CompatConnection(core_sqlcompat.CompatConnection):
    """Соединение YouTube с трансляцией legacy-имён конфликтующих таблиц."""

    collision_map = YT_COMPAT_VIEWS


def compat_view_name(name: str) -> str:
    """Имя TEMP-представления, отдающего legacy-объект ``name``.

    Для конфликтующих с ядром имён (``llm_usage``, ``thumbnail_vision``) оно
    отличается — см. :data:`YT_COMPAT_VIEWS`.
    """
    return YT_COMPAT_VIEWS.get(name, name)

# --- колонки legacy, которые адаптер принимает на запись --------------------

CHANNEL_COLUMNS = (
    "channel_id", "title", "handle", "subscriber_count", "video_count",
    "view_count", "country", "default_language", "topic_categories",
    "uploads_playlist_id", "is_russian", "first_seen", "last_synced_at",
)

VIDEO_COLUMNS = (
    "video_id", "channel_id", "title", "description", "tags", "category_id",
    "default_language", "duration_seconds", "is_shorts", "published_at",
    "thumbnail_url", "thumbnail_width", "thumbnail_height",
    "thumbnail_checked_at", "live_broadcast", "caption_available",
    "primary_topic", "topic_confidence", "first_seen", "last_seen",
)

CLASSIFICATION_COLUMNS = (
    "video_id", "is_ai", "topic", "confidence", "title_ru", "summary_ru",
    "lang", "reason", "model", "classified_at",
)

# Колонки video_scores, которые умеет писать save_score (без ключей и без
# viral_index: его пишет set_viral_indices). D-05: freshness/viral_score удалены.
SCORE_COLUMNS = (
    "outlier_score", "vpd", "vpd_ratio", "likes_per_1000",
    "comments_per_1000", "comment_velocity", "score_parts", "packaging_score",
)

# Оси, которые в ядре лежат в score.axes_json.
SCORE_AXES = (
    "outlier_score", "vpd", "vpd_ratio", "likes_per_1000",
    "comments_per_1000", "comment_velocity", "packaging_score", "viral_index",
)

# Поля videos, которые живут в content.meta_json.
_VIDEO_META = (
    "tags", "thumbnail_url", "thumbnail_width", "thumbnail_height",
    "thumbnail_checked_at", "live_broadcast", "caption_available",
    "primary_topic", "topic_confidence",
)


def _pick(data: Mapping[str, Any], columns: Iterable[str]) -> dict[str, Any]:
    return {c: data[c] for c in columns if c in data}


def _iso(value) -> str | None:
    """epoch/ISO → ISO UTC ядра."""
    return timeutil.parse_any(value)


def _now_iso() -> str:
    return timeutil.iso_now()


# ---------------------------------------------------------------------------
# Соединение и установка слоя совместимости
# ---------------------------------------------------------------------------

def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Открыть единую базу как единое ядро и поставить слой совместимости.

    В отличие от прежнего ``tuber.db.connect`` база — это единая база ядра
    (WAL, busy_timeout=15000, foreign_keys=ON).

    Схема ГАРАНТИРУЕТСЯ здесь же (:func:`migrate_schema`, идемпотентно и
    дёшево: CREATE IF NOT EXISTS, ~0.2 мс на прогретую базу). Так было и в
    legacy: ``tuber.db.connect`` создавал таблицы сам, поэтому `connect()` на
    пустом/чужом файле давал рабочую базу. Перенесённый YouTube-код и его тесты
    рассчитывают на это же поведение; ``init_db`` остаётся точкой входа для
    справочников (темы, пул фраз) поверх уже готовой схемы.
    """
    path = Path(db_path) if db_path is not None else config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    core_sqlcompat.ensure_supported_sqlite()
    conn = core_db.connect(str(path), factory=_CompatConnection)
    migrate_schema(conn)
    install_compat(conn)
    return conn


def ensure_planner_stats(conn: sqlite3.Connection) -> bool:
    """Прогнать ``ANALYZE``, если статистики планировщика в базе ещё нет.

    Зачем. Представления совместимости SQLite материализует (подзапрос справа
    от ``LEFT JOIN`` не разворачивается), и соединение с ним идёт либо через
    автоматический индекс, либо полным перебором. Без ``sqlite_stat1``
    планировщик оценивает селективность «на глаз» и выбирает перебор: отчёт
    YouTube на копии боевой базы (49 640 видео, 109 441 замер) шёл 5 мин 14 с
    вместо 15 с — медленнее legacy в 20 раз. Первый ``ANALYZE`` это чинит
    (62 с -> 0,2 с на самом тяжёлом запросе). См. TECH-DEBT D-21.

    Идемпотентно и дёшево: если ``sqlite_stat1`` уже есть, не делает ничего.
    Стоит один раз после массовой загрузки (``tuber migrate`` тоже зовёт
    ``ANALYZE``); в кроне повторно не срабатывает. Возвращает True, если
    статистика была собрана сейчас.
    """
    row = conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table' AND name='sqlite_stat1'"
    ).fetchone()
    if row is not None:
        return False
    with core_db.write_tx(conn):
        conn.execute("ANALYZE")
    log.info("ANALYZE: собрана статистика планировщика (см. TECH-DEBT D-21)")
    return True


def init_db(conn: sqlite3.Connection) -> None:
    """Гарантировать схему ядра, справочник тем и слой совместимости.

    Идемпотентно: повторный вызов ничего не меняет (миграция ядра тоже
    идемпотентна).
    """
    migrate_schema(conn)
    install_compat(conn)
    ensure_planner_stats(conn)
    with core_db.write_tx(conn):
        for topic in getattr(config, "TOPICS", ()):
            storage.upsert_topic(conn, topic, platform="youtube")
    # Совместимость: тот же порядок обработки пула фраз, что был в legacy
    # init_db (отсев мёртвых фраз → возврат ошибочно закрытых → регистрация
    # статических фраз конфига). Идемпотентно.
    migrate_drop_dead_queries(conn)
    migrate_restore_usable_queries(conn)
    register_static_phrases(conn)


def _install_indexes(conn: sqlite3.Connection) -> list[str]:
    """Создать индексы адаптера через ядровой страж (ТЗ-3d).

    Возвращает имена созданных индексов; эквивалент у ядра — не создаём,
    дубль/префикс-дубль ядрового индекса поднимает
    :class:`tuber.core.schema.DuplicateIndexError`.
    """
    created: list[str] = []
    for name, table, cols in _COMPAT_INDEXES:
        try:
            if core_schema.ensure_adapter_index(conn, name, table, cols):
                created.append(name)
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc).lower():
                raise core_sqlcompat.compat_install_error(exc)
            log.debug("install_compat: индекс %s пропущен на read-only соединении", name)
    return created


def install_compat(conn: sqlite3.Connection) -> None:
    """Создать TEMP-представления и триггеры совместимости с legacy.

    Индексы адаптера (``_COMPAT_INDEXES``) создаются через ядровой страж
    ``ensure_adapter_index``: эквивалент у ядра — не создаём, дубль/префикс
    ядрового индекса — громкая ошибка (ТЗ-3d).

    Трансляция legacy-имён конфликтующих таблиц (ТЗ-3c) включается только
    ПОСЛЕ установки слоя: в его же DDL конфликтующие имена стоят в ``FROM``
    представлений и целях триггеров — иначе они переписались бы на самих себя.
    """
    core_sqlcompat.disable_compat_sql(conn)
    try:
        _install_indexes(conn)
        for stmt in _COMPAT_STATEMENTS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "readonly" not in str(exc).lower():
                    raise core_sqlcompat.compat_install_error(exc)
                log.debug("install_compat: пропущено на read-only соединении: %.60s", stmt)
    finally:
        core_sqlcompat.enable_compat_sql(conn, YT_COMPAT_VIEWS)


def _epoch(col: str) -> str:
    return f"CAST(strftime('%s', {col}) AS INTEGER)"


def _iso_expr(param: str) -> str:
    return f"CASE WHEN {param} IS NULL THEN NULL ELSE datetime({param}, 'unixepoch') END"


# ---------------------------------------------------------------------------
# Представления совместимости (чтение) и триггеры (запись)
# ---------------------------------------------------------------------------

# Индексы адаптера на таблицах ядра (ТЗ-3d) — создаются ядровым стражем
# ``ensure_adapter_index`` (см. одноимённый блок в x/store.py).
_COMPAT_INDEXES: list[tuple[str, str, tuple[str, ...]]] = [
    ("idx_youtube_metric_quality", "metric_snapshot", ("content_id", "interval_quality")),
    # Представление `videos` в большинстве запросов читает ровно
    # (video_id, channel_id, is_shorts). Без покрывающего индекса внешний цикл
    # идёт по `idx_content_source_ext` и на КАЖДУЮ строку ходит в таблицу за
    # `external_id`/`is_short` — на 49 683 строках это +400 мс в `_outlier_map`
    # (замер: 525 мс -> 186 мс). Колонки — ровно проекция представления.
    ("idx_youtube_content_videos", "content",
     ("platform", "external_id", "source_external_id", "is_short")),
    # `video_scores` по скорости (vpd отсортирован): legacy хранил vpd колонкой
    # и сортировал перебором (19,7 мс), ядро хранит оси в score.axes_json, и
    # json_extract на 86 тыс. строк стоит ~88 мс. Выражение-индекс возвращает
    # доступ к порядку (замер: 88 мс -> 0,1 мс). Ось `vpd` — YouTube-смысл,
    # поэтому индекс живёт здесь, а не в общей схеме ядра.
    ("idx_youtube_score_vpd", "score", ("platform", "json_extract(axes_json, '$.vpd')")),
]

_COMPAT_STATEMENTS: list[str] = [
    # Индексы под access-path'ы представлений. В legacy они были в схеме таблиц
    # (PK/индексы `videos`/`channels`/`snapshots`); в ядре нужного индекса нет,
    # и без него join представления `channels` вырождается в O(n·m). Аддитивные
    # индексы семантику не меняют.
    #
    # ТЗ-2c (D-22): индекс `idx_youtube_metric_ext` и прочие `*_ext` живут в схеме
    # ядра (см. tuber.core.schema) — они держат денормализованные `platform` +
    # `external_id`. Представления ниже читают НАПРЯМУЮ из таблиц ядра, поэтому
    # SQLite разворачивает их на правой стороне `LEFT JOIN` и берёт индекс, а не
    # материализует всё представление.
    #
    # ТЗ-3d (D-33): `source(platform, external_id)` теперь индексирует ЯДРО
    # (`idx_source_ext`) — адаптер свой `idx_youtube_source_ext` больше не создаёт
    # (это был точный дубль, причём такой же создавал и X). Оставшиеся индексы
    # (ниже) уходят в `_COMPAT_INDEXES` и создаются через ядровой страж.
    # ------------------------------------------------------------------ channels
    "DROP VIEW IF EXISTS temp.channels",
    """
    CREATE TEMP VIEW channels AS
    SELECT s.external_id                                        AS channel_id,
           s.title                                              AS title,
           s.handle                                             AS handle,
           s.subs                                               AS subscriber_count,
           json_extract(s.meta_json, '$.video_count')           AS video_count,
           json_extract(s.meta_json, '$.view_count')            AS view_count,
           s.country                                            AS country,
           s.lang                                               AS default_language,
           json_extract(s.meta_json, '$.topic_categories')      AS topic_categories,
           json_extract(s.meta_json, '$.uploads_playlist_id')   AS uploads_playlist_id,
           json_extract(s.meta_json, '$.is_russian')            AS is_russian,
           CAST(strftime('%s', s.first_seen_at) AS INTEGER)     AS first_seen,
           CAST(strftime('%s', s.last_synced_at) AS INTEGER)    AS last_synced_at
    FROM source s WHERE s.platform = 'youtube'
    """,
    "DROP TRIGGER IF EXISTS temp.channels_ins",
    """
    CREATE TEMP TRIGGER channels_ins INSTEAD OF INSERT ON channels BEGIN
      INSERT INTO source(platform, handle, external_id, first_seen_at, source_kind)
      VALUES('youtube',
             COALESCE(NULLIF(NEW.handle, ''), NEW.channel_id),
             NEW.channel_id,
             """ + _iso_expr("NEW.first_seen") + """,
             'youtube')
      ON CONFLICT(platform, handle) DO NOTHING;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.channels_upd",
    """
    CREATE TEMP TRIGGER channels_upd INSTEAD OF UPDATE ON channels BEGIN
      UPDATE source SET
        external_id = NEW.channel_id,
        title = NEW.title,
        subs = NEW.subscriber_count,
        country = NEW.country,
        lang = NEW.default_language,
        first_seen_at = COALESCE(first_seen_at, """ + _iso_expr("NEW.first_seen") + """),
        last_synced_at = """ + _iso_expr("NEW.last_synced_at") + """,
        meta_json = json_set(COALESCE(meta_json, '{}'),
            '$.video_count', NEW.video_count,
            '$.view_count', NEW.view_count,
            '$.topic_categories', NEW.topic_categories,
            '$.uploads_playlist_id', NEW.uploads_playlist_id,
            '$.is_russian', NEW.is_russian)
      WHERE platform = 'youtube' AND external_id = OLD.channel_id;
    END
    """,
    # -------------------------------------------------------------------- videos
    "DROP VIEW IF EXISTS temp.videos",
    """
    CREATE TEMP VIEW videos AS
    SELECT c.external_id                                        AS video_id,
           c.source_external_id                                 AS channel_id,
           c.title                                              AS title,
           c.text                                               AS description,
           json_extract(c.meta_json, '$.tags')                  AS tags,
           CAST(c.category AS INTEGER)                          AS category_id,
           c.lang                                               AS default_language,
           c.duration_seconds                                   AS duration_seconds,
           c.is_short                                           AS is_shorts,
           CAST(strftime('%s', c.published_at) AS INTEGER)      AS published_at,
           json_extract(c.meta_json, '$.thumbnail_url')         AS thumbnail_url,
           json_extract(c.meta_json, '$.thumbnail_width')       AS thumbnail_width,
           json_extract(c.meta_json, '$.thumbnail_height')      AS thumbnail_height,
           CAST(strftime('%s', json_extract(c.meta_json, '$.thumbnail_checked_at')) AS INTEGER)
                                                                AS thumbnail_checked_at,
           json_extract(c.meta_json, '$.live_broadcast')        AS live_broadcast,
           json_extract(c.meta_json, '$.caption_available')     AS caption_available,
           json_extract(c.meta_json, '$.primary_topic')         AS primary_topic,
           json_extract(c.meta_json, '$.topic_confidence')      AS topic_confidence,
           CAST(strftime('%s', c.first_seen_at) AS INTEGER)     AS first_seen,
           CAST(strftime('%s', c.last_seen_at) AS INTEGER)      AS last_seen
    FROM content c
    WHERE c.platform = 'youtube'
    """,
    "DROP TRIGGER IF EXISTS temp.videos_ins",
    """
    CREATE TEMP TRIGGER videos_ins INSTEAD OF INSERT ON videos BEGIN
      INSERT INTO content(platform, external_id, source_id, url, kind, title, text, lang,
                          published_at, duration_seconds, category, is_short,
                          first_seen_at, last_seen_at, meta_json)
      VALUES('youtube', NEW.video_id,
        (SELECT id FROM source WHERE platform='youtube' AND external_id=NEW.channel_id),
        '""" + urls.YOUTUBE_WATCH_PREFIX + """' || NEW.video_id,
        CASE WHEN NEW.is_shorts = 1 THEN 'short' ELSE 'video' END,
        NEW.title, NEW.description, NEW.default_language,
        COALESCE(datetime(NEW.published_at, 'unixepoch'), '1970-01-01 00:00:00'),
        NEW.duration_seconds,
        CASE WHEN NEW.category_id IS NULL THEN NULL ELSE CAST(NEW.category_id AS TEXT) END,
        COALESCE(NEW.is_shorts, 0),
        """ + _iso_expr("NEW.first_seen") + """,
        """ + _iso_expr("NEW.last_seen") + """,
        json_object(
          'tags', NEW.tags,
          'thumbnail_url', NEW.thumbnail_url,
          'thumbnail_width', NEW.thumbnail_width,
          'thumbnail_height', NEW.thumbnail_height,
          'thumbnail_checked_at', """ + _iso_expr("NEW.thumbnail_checked_at") + """,
          'live_broadcast', NEW.live_broadcast,
          'caption_available', NEW.caption_available,
          'primary_topic', NEW.primary_topic,
          'topic_confidence', NEW.topic_confidence))
      ON CONFLICT(platform, external_id) DO UPDATE SET
        source_id=excluded.source_id, kind=excluded.kind, title=excluded.title,
        text=excluded.text, lang=excluded.lang, published_at=excluded.published_at,
        duration_seconds=excluded.duration_seconds, category=excluded.category,
        is_short=excluded.is_short,
        first_seen_at=COALESCE(content.first_seen_at, excluded.first_seen_at),
        last_seen_at=excluded.last_seen_at,
        meta_json=json_set(COALESCE(content.meta_json, '{}'),
          '$.tags', NEW.tags,
          '$.thumbnail_url', NEW.thumbnail_url,
          '$.thumbnail_width', NEW.thumbnail_width,
          '$.thumbnail_height', NEW.thumbnail_height,
          '$.thumbnail_checked_at', """ + _iso_expr("NEW.thumbnail_checked_at") + """,
          '$.live_broadcast', NEW.live_broadcast,
          '$.caption_available', NEW.caption_available,
          '$.primary_topic', NEW.primary_topic,
          '$.topic_confidence', NEW.topic_confidence);
    END
    """,
    "DROP TRIGGER IF EXISTS temp.videos_upd",
    """
    CREATE TEMP TRIGGER videos_upd INSTEAD OF UPDATE ON videos BEGIN
      UPDATE content SET
        source_id = (SELECT id FROM source WHERE platform='youtube' AND external_id=NEW.channel_id),
        kind = CASE WHEN NEW.is_shorts = 1 THEN 'short' ELSE 'video' END,
        title = NEW.title,
        text = NEW.description,
        lang = NEW.default_language,
        published_at = COALESCE(datetime(NEW.published_at, 'unixepoch'), published_at),
        duration_seconds = NEW.duration_seconds,
        category = CASE WHEN NEW.category_id IS NULL THEN NULL ELSE CAST(NEW.category_id AS TEXT) END,
        is_short = COALESCE(NEW.is_shorts, 0),
        first_seen_at = COALESCE(""" + _iso_expr("NEW.first_seen") + """, first_seen_at),
        last_seen_at = COALESCE(""" + _iso_expr("NEW.last_seen") + """, last_seen_at),
        meta_json = json_set(COALESCE(meta_json, '{}'),
          '$.tags', NEW.tags,
          '$.thumbnail_url', NEW.thumbnail_url,
          '$.thumbnail_width', NEW.thumbnail_width,
          '$.thumbnail_height', NEW.thumbnail_height,
          '$.thumbnail_checked_at', """ + _iso_expr("NEW.thumbnail_checked_at") + """,
          '$.live_broadcast', NEW.live_broadcast,
          '$.caption_available', NEW.caption_available,
          '$.primary_topic', NEW.primary_topic,
          '$.topic_confidence', NEW.topic_confidence)
      WHERE platform = 'youtube' AND external_id = OLD.video_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.videos_del",
    """
    CREATE TEMP TRIGGER videos_del INSTEAD OF DELETE ON videos BEGIN
      DELETE FROM content WHERE platform='youtube' AND external_id = OLD.video_id;
    END
    """,
    # ----------------------------------------------------------------- snapshots
    "DROP VIEW IF EXISTS temp.snapshots",
    """
    CREATE TEMP VIEW snapshots AS
    SELECT m.id                                                   AS id,
           m.external_id                                          AS video_id,
           CAST(strftime('%s', m.captured_at) AS INTEGER)         AS captured_at,
           m.bucket                                               AS bucket,
           m.views                                                AS views,
           m.likes                                                AS likes,
           m.comments                                             AS comments,
           CAST(strftime('%s', json_extract(m.raw_json, '$.prev_captured_at')) AS INTEGER)
                                                                  AS prev_captured_at,
           m.interval_seconds                                     AS interval_seconds,
           m.interval_quality                                     AS interval_quality,
           m.delta_views                                          AS delta_views,
           m.delta_likes                                          AS delta_likes,
           m.delta_comments                                       AS delta_comments,
           m.views_per_day                                        AS views_per_day,
           m.views_per_hour                                       AS views_per_hour,
           m.age_hours                                            AS age_hours,
           m.source                                               AS source,
           m.is_anomaly                                           AS is_anomaly
    FROM metric_snapshot m
    WHERE m.platform = 'youtube'
    """,
    "DROP TRIGGER IF EXISTS temp.snapshots_upd",
    """
    CREATE TEMP TRIGGER snapshots_upd INSTEAD OF UPDATE ON snapshots BEGIN
      UPDATE metric_snapshot SET
        bucket = NEW.bucket,
        views = NEW.views, likes = NEW.likes, comments = NEW.comments,
        interval_seconds = NEW.interval_seconds,
        interval_quality = NEW.interval_quality,
        delta_views = NEW.delta_views, delta_likes = NEW.delta_likes,
        delta_comments = NEW.delta_comments,
        views_per_day = NEW.views_per_day, views_per_hour = NEW.views_per_hour,
        age_hours = NEW.age_hours, source = NEW.source, is_anomaly = NEW.is_anomaly
      WHERE id = OLD.id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.snapshots_del",
    """
    CREATE TEMP TRIGGER snapshots_del INSTEAD OF DELETE ON snapshots BEGIN
      DELETE FROM metric_snapshot WHERE id = OLD.id;
    END
    """,
    # ------------------------------------------------------------- video_scores
    "DROP VIEW IF EXISTS temp.video_scores",
    """
    CREATE TEMP VIEW video_scores AS
    SELECT sc.external_id                                       AS video_id,
           CAST(strftime('%s', sc.computed_at) AS INTEGER)       AS computed_at,
           json_extract(sc.axes_json, '$.outlier_score')         AS outlier_score,
           json_extract(sc.axes_json, '$.vpd')                   AS vpd,
           json_extract(sc.axes_json, '$.vpd_ratio')             AS vpd_ratio,
           json_extract(sc.axes_json, '$.likes_per_1000')        AS likes_per_1000,
           json_extract(sc.axes_json, '$.comments_per_1000')     AS comments_per_1000,
           json_extract(sc.axes_json, '$.comment_velocity')      AS comment_velocity,
           sc.parts_json                                         AS score_parts,
           json_extract(sc.axes_json, '$.packaging_score')       AS packaging_score,
           json_extract(sc.axes_json, '$.viral_index')           AS viral_index
    FROM score sc
    WHERE sc.platform = 'youtube'
    """,
    # ------------------------------------------------------- video_classification
    "DROP VIEW IF EXISTS temp.video_classification",
    """
    CREATE TEMP VIEW video_classification AS
    SELECT cl.external_id                                       AS video_id,
           cl.is_ai                                             AS is_ai,
           cl.topic                                             AS topic,
           cl.confidence                                        AS confidence,
           cl.title_ru                                          AS title_ru,
           cl.summary_ru                                        AS summary_ru,
           cl.lang                                              AS lang,
           cl.reason                                            AS reason,
           cl.model                                             AS model,
           CAST(strftime('%s', cl.classified_at) AS INTEGER)    AS classified_at
    FROM classification cl
    WHERE cl.platform = 'youtube'
    """,
    # ------------------------------------------------------------------ seo_fields
    "DROP VIEW IF EXISTS temp.seo_fields",
    """
    CREATE TEMP VIEW seo_fields AS
    SELECT sf.external_id AS video_id,
           sf.title_length, sf.title_words, sf.title_has_number, sf.title_has_question,
           sf.title_has_colon, sf.title_caps_ratio, sf.title_emoji_count, sf.title_top_words,
           sf.thumb_text, sf.thumb_text_words, sf.thumb_objects, sf.thumb_face_count,
           sf.thumb_arrows, sf.thumb_colors, sf.thumb_style,
           sf.desc_length, sf.desc_links, sf.desc_hashtags, sf.desc_timestamps, sf.desc_cta,
           sf.tags_count, sf.tags_common,
           sf.published_hour_msk, sf.published_weekday, sf.published_hour_local,
           sf.title_matches_topic, sf.seo_pattern
    FROM seo_field sf
    WHERE sf.platform = 'youtube'
    """,
    "DROP TRIGGER IF EXISTS temp.seo_fields_ins",
    """
    CREATE TEMP TRIGGER seo_fields_ins INSTEAD OF INSERT ON seo_fields BEGIN
      INSERT INTO seo_field(content_id, title_length, title_words, title_has_number,
        title_has_question, title_has_colon, title_caps_ratio, title_emoji_count,
        title_top_words, thumb_text, thumb_text_words, thumb_objects, thumb_face_count,
        thumb_arrows, thumb_colors, thumb_style, desc_length, desc_links, desc_hashtags,
        desc_timestamps, desc_cta, tags_count, tags_common, published_hour_msk,
        published_weekday, published_hour_local, title_matches_topic, seo_pattern)
      VALUES(
        (SELECT id FROM content WHERE platform='youtube' AND external_id=NEW.video_id),
        NEW.title_length, NEW.title_words, NEW.title_has_number, NEW.title_has_question,
        NEW.title_has_colon, NEW.title_caps_ratio, NEW.title_emoji_count, NEW.title_top_words,
        NEW.thumb_text, NEW.thumb_text_words, NEW.thumb_objects, NEW.thumb_face_count,
        NEW.thumb_arrows, NEW.thumb_colors, NEW.thumb_style, NEW.desc_length, NEW.desc_links,
        NEW.desc_hashtags, NEW.desc_timestamps, NEW.desc_cta, NEW.tags_count, NEW.tags_common,
        NEW.published_hour_msk, NEW.published_weekday, NEW.published_hour_local,
        NEW.title_matches_topic, NEW.seo_pattern)
      ON CONFLICT(content_id) DO UPDATE SET
        title_length=excluded.title_length, title_words=excluded.title_words,
        title_has_number=excluded.title_has_number, title_has_question=excluded.title_has_question,
        title_has_colon=excluded.title_has_colon, title_caps_ratio=excluded.title_caps_ratio,
        title_emoji_count=excluded.title_emoji_count, title_top_words=excluded.title_top_words,
        thumb_text=excluded.thumb_text, thumb_text_words=excluded.thumb_text_words,
        thumb_objects=excluded.thumb_objects, thumb_face_count=excluded.thumb_face_count,
        thumb_arrows=excluded.thumb_arrows, thumb_colors=excluded.thumb_colors,
        thumb_style=excluded.thumb_style, desc_length=excluded.desc_length,
        desc_links=excluded.desc_links, desc_hashtags=excluded.desc_hashtags,
        desc_timestamps=excluded.desc_timestamps, desc_cta=excluded.desc_cta,
        tags_count=excluded.tags_count, tags_common=excluded.tags_common,
        published_hour_msk=excluded.published_hour_msk,
        published_weekday=excluded.published_weekday,
        published_hour_local=excluded.published_hour_local,
        title_matches_topic=excluded.title_matches_topic, seo_pattern=excluded.seo_pattern;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.seo_fields_upd",
    """
    CREATE TEMP TRIGGER seo_fields_upd INSTEAD OF UPDATE ON seo_fields BEGIN
      UPDATE seo_field SET
        title_length=NEW.title_length, title_words=NEW.title_words,
        title_has_number=NEW.title_has_number, title_has_question=NEW.title_has_question,
        title_has_colon=NEW.title_has_colon, title_caps_ratio=NEW.title_caps_ratio,
        title_emoji_count=NEW.title_emoji_count, title_top_words=NEW.title_top_words,
        thumb_text=NEW.thumb_text, thumb_text_words=NEW.thumb_text_words,
        thumb_objects=NEW.thumb_objects, thumb_face_count=NEW.thumb_face_count,
        thumb_arrows=NEW.thumb_arrows, thumb_colors=NEW.thumb_colors,
        thumb_style=NEW.thumb_style, desc_length=NEW.desc_length,
        desc_links=NEW.desc_links, desc_hashtags=NEW.desc_hashtags,
        desc_timestamps=NEW.desc_timestamps, desc_cta=NEW.desc_cta,
        tags_count=NEW.tags_count, tags_common=NEW.tags_common,
        published_hour_msk=NEW.published_hour_msk, published_weekday=NEW.published_weekday,
        published_hour_local=NEW.published_hour_local,
        title_matches_topic=NEW.title_matches_topic, seo_pattern=NEW.seo_pattern
      WHERE content_id = (SELECT id FROM content WHERE platform='youtube' AND external_id=OLD.video_id);
    END
    """,
    # ------------------------------------------------------------ thumbnail_vision
    "DROP VIEW IF EXISTS temp.yt_thumbnail_vision",
    """
    CREATE TEMP VIEW yt_thumbnail_vision AS
    SELECT tv.id, tv.external_id AS video_id, tv.model, tv.prompt_version,
           tv.description_raw, tv.extracted_text, tv.cost_usd, tv.latency_ms,
           CAST(strftime('%s', tv.created_at) AS INTEGER) AS created_at
    FROM thumbnail_vision tv
    WHERE tv.content_id IS NULL OR tv.platform = 'youtube'
    """,
    "DROP TRIGGER IF EXISTS temp.yt_thumbnail_vision_ins",
    """
    CREATE TEMP TRIGGER yt_thumbnail_vision_ins INSTEAD OF INSERT ON yt_thumbnail_vision BEGIN
      INSERT INTO thumbnail_vision(content_id, model, prompt_version, description_raw,
        extracted_text, cost_usd, latency_ms, created_at)
      VALUES(
        (SELECT id FROM content WHERE platform='youtube' AND external_id=NEW.video_id),
        NEW.model, NEW.prompt_version, NEW.description_raw, NEW.extracted_text,
        NEW.cost_usd, NEW.latency_ms, """ + _iso_expr("NEW.created_at") + """);
    END
    """,
    # ------------------------------------------------------------- video_comments
    "DROP VIEW IF EXISTS temp.video_comments",
    """
    CREATE TEMP VIEW video_comments AS
    SELECT cc.comment_id, cc.external_id AS video_id, cc.author, cc.text, cc.likes,
           CAST(strftime('%s', cc.published_at) AS INTEGER) AS published_at,
           CAST(strftime('%s', cc.captured_at) AS INTEGER) AS captured_at
    FROM content_comment cc
    WHERE cc.platform = 'youtube' OR cc.content_id IS NULL
    """,
    "DROP TRIGGER IF EXISTS temp.video_comments_ins",
    """
    CREATE TEMP TRIGGER video_comments_ins INSTEAD OF INSERT ON video_comments BEGIN
      INSERT INTO content_comment(comment_id, content_id, author, text, likes,
        published_at, captured_at, platform)
      VALUES(NEW.comment_id,
        (SELECT id FROM content WHERE platform='youtube' AND external_id=NEW.video_id),
        NEW.author, NEW.text, NEW.likes,
        """ + _iso_expr("NEW.published_at") + """,
        """ + _iso_expr("NEW.captured_at") + """, 'youtube')
      ON CONFLICT(comment_id) DO UPDATE SET
        content_id=excluded.content_id, author=excluded.author, text=excluded.text,
        likes=excluded.likes, published_at=excluded.published_at,
        captured_at=excluded.captured_at, platform='youtube';
    END
    """,
    # ------------------------------------------------------------- comment_checks
    "DROP VIEW IF EXISTS temp.comment_checks",
    """
    CREATE TEMP VIEW comment_checks AS
    SELECT ck.external_id AS video_id,
           CAST(strftime('%s', ck.checked_at) AS INTEGER) AS checked_at,
           ck.status, ck.error
    FROM comment_check ck
    WHERE ck.platform = 'youtube'
    """,
    # ------------------------------------------------------------------- topics
    "DROP VIEW IF EXISTS temp.topics",
    "CREATE TEMP VIEW topics AS SELECT name FROM topic WHERE platform = 'youtube'",
    "DROP TRIGGER IF EXISTS temp.topics_ins",
    """
    CREATE TEMP TRIGGER topics_ins INSTEAD OF INSERT ON topics BEGIN
      INSERT INTO topic(name, platform) VALUES(NEW.name, 'youtube')
      ON CONFLICT(name) DO UPDATE SET platform=COALESCE(topic.platform, 'youtube');
    END
    """,
    "DROP TRIGGER IF EXISTS temp.topics_del",
    """
    CREATE TEMP TRIGGER topics_del INSTEAD OF DELETE ON topics BEGIN
      DELETE FROM topic WHERE name = OLD.name AND platform = 'youtube';
    END
    """,
    # ------------------------------------------------------------------ llm_usage
    "DROP VIEW IF EXISTS temp.yt_llm_usage",
    """
    CREATE TEMP VIEW yt_llm_usage AS
    SELECT id, stage, model, tokens_in, tokens_out, cost_usd,
           CAST(strftime('%s', created_at) AS INTEGER) AS created_at
    FROM llm_usage WHERE platform = 'youtube'
    """,
    "DROP TRIGGER IF EXISTS temp.yt_llm_usage_ins",
    """
    CREATE TEMP TRIGGER yt_llm_usage_ins INSTEAD OF INSERT ON yt_llm_usage BEGIN
      INSERT INTO llm_usage(platform, stage, model, tokens_in, tokens_out, cost_usd, created_at)
      VALUES('youtube', NEW.stage, NEW.model, NEW.tokens_in, NEW.tokens_out, NEW.cost_usd,
             """ + _iso_expr("NEW.created_at") + """);
    END
    """,
    # ------------------------------------------------------------------- quota_log
    # quota_usage агрегирует расход по (platform,key_id,day,endpoint). Старый
    # YouTube-код считает ЧИСЛО вызовов через COUNT(*) и сумму units. Поэтому
    # представление «разворачивает» агрегат обратно в строки-вызовы:
    # COUNT(*) = calls, SUM(units) = units. Единицы делятся ровно между копиями
    # (в пределах одного endpoint стоимость вызова одинакова).
    "DROP VIEW IF EXISTS temp.quota_log",
    """
    CREATE TEMP VIEW quota_log AS
    WITH RECURSIVE expand(rid, n, cnt, units, key_id, day, endpoint, project, ts) AS (
      SELECT rowid, 1, MAX(COALESCE(calls, 1), 1), COALESCE(units, 0),
             COALESCE(key_id, ''), day, COALESCE(endpoint, ''), project, ts
      FROM quota_usage WHERE platform = 'youtube'
      UNION ALL
      SELECT rid, n + 1, cnt, units, key_id, day, endpoint, project, ts
      FROM expand WHERE n < cnt
    )
    SELECT rid AS id,
           CAST(strftime('%s', day) AS INTEGER) AS date,
           key_id,
           1 AS calls,
           (units * 1.0 / cnt) AS units,
           endpoint,
           project,
           CAST(strftime('%s', ts) AS INTEGER) AS ts
    FROM expand
    """,
    "DROP TRIGGER IF EXISTS temp.quota_log_ins",
    """
    CREATE TEMP TRIGGER quota_log_ins INSTEAD OF INSERT ON quota_log BEGIN
      INSERT INTO quota_usage(platform, key_id, day, endpoint, project, calls, units, ts)
      VALUES('youtube', COALESCE(NEW.key_id, ''),
             COALESCE(strftime('%Y-%m-%d', NEW.date, 'unixepoch'), DATE('now')),
             COALESCE(NEW.endpoint, ''),
             NEW.project, COALESCE(NEW.calls, 1), COALESCE(NEW.units, 0),
             """ + _iso_expr("NEW.ts") + """)
      ON CONFLICT(platform, key_id, day, endpoint) DO UPDATE SET
        calls = COALESCE(quota_usage.calls, 0) + COALESCE(excluded.calls, 1),
        units = COALESCE(quota_usage.units, 0) + COALESCE(excluded.units, 0),
        project = COALESCE(excluded.project, quota_usage.project),
        ts = COALESCE(excluded.ts, quota_usage.ts);
    END
    """,
    # ------------------------------------------------------------ channel_candidates
    "DROP VIEW IF EXISTS temp.channel_candidates",
    """
    CREATE TEMP VIEW channel_candidates AS
    SELECT ca.handle                                          AS channel_id,
           json_extract(ca.meta_json, '$.title')              AS title,
           ca.display_handle                                  AS handle,
           json_extract(ca.meta_json, '$.description')        AS description,
           ca.found_via                                       AS source,
           json_extract(ca.meta_json, '$.evidence')           AS evidence,
           COALESCE(ca.seen_count, 1)                         AS mentions,
           json_extract(ca.meta_json, '$.subscriber_count')   AS subscriber_count,
           ca.score_priority                                  AS score,
           ca.status                                          AS status,
           ca.reject_reason                                   AS reject_reason,
           COALESCE(json_extract(ca.meta_json, '$.resolve_attempts'), 0) AS resolve_attempts,
           ca.first_seen_at                                   AS discovered_at,
           json_extract(ca.meta_json, '$.probed_at')          AS probed_at
    FROM candidate ca
    WHERE ca.platform = 'youtube' AND ca.kind = 'channel'
    """,
    "DROP TRIGGER IF EXISTS temp.channel_candidates_ins",
    """
    CREATE TEMP TRIGGER channel_candidates_ins INSTEAD OF INSERT ON channel_candidates BEGIN
      INSERT INTO candidate(platform, kind, handle, external_id, display_handle, found_via,
        score_priority, seen_count, status, reject_reason, first_seen_at, last_seen_at, meta_json)
      VALUES('youtube', 'channel', NEW.channel_id, NEW.channel_id, NEW.handle, NEW.source,
        NEW.score, COALESCE(NEW.mentions, 1), COALESCE(NEW.status, 'new'), NEW.reject_reason,
        CASE WHEN NEW.discovered_at IS NOT NULL AND NEW.discovered_at NOT GLOB '*[^0-9]*' THEN datetime(CAST(NEW.discovered_at AS INTEGER), 'unixepoch') WHEN NEW.discovered_at IS NULL THEN strftime('%Y-%m-%d %H:%M:%S', 'now') ELSE NEW.discovered_at END,
        """ + _iso_expr("NEW.probed_at") + """,
        json_object('title', NEW.title, 'description', NEW.description,
          'evidence', NEW.evidence, 'subscriber_count', NEW.subscriber_count,
          'resolve_attempts', COALESCE(NEW.resolve_attempts, 0),
          'probed_at', """ + _iso_expr("NEW.probed_at") + """))
      ON CONFLICT(platform, handle) DO UPDATE SET
        display_handle=excluded.display_handle, found_via=excluded.found_via,
        score_priority=excluded.score_priority,
        seen_count=COALESCE(candidate.seen_count, 0) + 1,
        meta_json=json_set(COALESCE(candidate.meta_json, '{}'),
          '$.title', NEW.title, '$.description', NEW.description,
          '$.evidence', NEW.evidence, '$.subscriber_count', NEW.subscriber_count);
    END
    """,
    "DROP TRIGGER IF EXISTS temp.channel_candidates_upd",
    """
    CREATE TEMP TRIGGER channel_candidates_upd INSTEAD OF UPDATE ON channel_candidates BEGIN
      UPDATE candidate SET
        display_handle = NEW.handle,
        found_via = COALESCE(NEW.source, found_via),
        score_priority = NEW.score,
        seen_count = NEW.mentions,
        status = NEW.status,
        reject_reason = NEW.reject_reason,
        last_seen_at = COALESCE(""" + _iso_expr("NEW.probed_at") + """, last_seen_at),
        meta_json = json_set(COALESCE(meta_json, '{}'),
          '$.title', NEW.title, '$.description', NEW.description,
          '$.evidence', NEW.evidence, '$.subscriber_count', NEW.subscriber_count,
          '$.resolve_attempts', NEW.resolve_attempts,
          '$.probed_at', """ + _iso_expr("NEW.probed_at") + """)
      WHERE platform = 'youtube' AND kind = 'channel' AND handle = OLD.channel_id;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.channel_candidates_del",
    """
    CREATE TEMP TRIGGER channel_candidates_del INSTEAD OF DELETE ON channel_candidates BEGIN
      DELETE FROM candidate WHERE platform='youtube' AND kind='channel' AND handle = OLD.channel_id;
    END
    """,
    # --------------------------------------------------------------- query_candidates
    "DROP VIEW IF EXISTS temp.query_candidates",
    """
    CREATE TEMP VIEW query_candidates AS
    SELECT ca.handle                                            AS query,
           ca.found_via                                         AS source,
           json_extract(ca.meta_json, '$.evidence')             AS evidence,
           COALESCE(ca.seen_count, 1)                           AS hits,
           ca.score_priority                                    AS score,
           ca.status                                            AS status,
           ca.reject_reason                                     AS reject_reason,
           ca.first_seen_at                                     AS discovered_at,
           COALESCE(json_extract(ca.meta_json, '$.kind'), 'query') AS kind,
           COALESCE(json_extract(ca.meta_json, '$.runs'), 0)    AS runs,
           COALESCE(json_extract(ca.meta_json, '$.accepted'), 0) AS accepted,
           json_extract(ca.meta_json, '$.last_run_at')          AS last_run_at,
           COALESCE(json_extract(ca.meta_json, '$.fail_count'), 0) AS fail_count,
           json_extract(ca.meta_json, '$.last_fail_at')         AS last_fail_at
    FROM candidate ca
    WHERE ca.platform = 'youtube' AND ca.kind = 'query'
    """,
    "DROP TRIGGER IF EXISTS temp.query_candidates_ins",
    """
    CREATE TEMP TRIGGER query_candidates_ins INSTEAD OF INSERT ON query_candidates BEGIN
      INSERT INTO candidate(platform, kind, handle, external_id, found_via, score_priority,
        seen_count, status, reject_reason, first_seen_at, last_seen_at, meta_json)
      VALUES('youtube', 'query', NEW.query, NEW.query, NEW.source, NEW.score,
        COALESCE(NEW.hits, 1), COALESCE(NEW.status, 'new'), NEW.reject_reason,
        CASE WHEN NEW.discovered_at IS NOT NULL AND NEW.discovered_at NOT GLOB '*[^0-9]*' THEN datetime(CAST(NEW.discovered_at AS INTEGER), 'unixepoch') WHEN NEW.discovered_at IS NULL THEN strftime('%Y-%m-%d %H:%M:%S', 'now') ELSE NEW.discovered_at END,
        """ + _iso_expr("NEW.last_run_at") + """,
        json_object('evidence', NEW.evidence, 'kind', COALESCE(NEW.kind, 'query'),
          'runs', COALESCE(NEW.runs, 0), 'accepted', COALESCE(NEW.accepted, 0),
          'last_run_at', NEW.last_run_at, 'fail_count', COALESCE(NEW.fail_count, 0),
          'last_fail_at', NEW.last_fail_at))
      ON CONFLICT(platform, handle) DO UPDATE SET
        found_via=excluded.found_via, score_priority=excluded.score_priority,
        seen_count=COALESCE(candidate.seen_count, 0) + 1,
        meta_json=json_set(COALESCE(candidate.meta_json, '{}'),
          '$.evidence', NEW.evidence, '$.kind', COALESCE(NEW.kind, 'query'));
    END
    """,
    "DROP TRIGGER IF EXISTS temp.query_candidates_upd",
    """
    CREATE TEMP TRIGGER query_candidates_upd INSTEAD OF UPDATE ON query_candidates BEGIN
      UPDATE candidate SET
        found_via = COALESCE(NEW.source, found_via),
        score_priority = NEW.score,
        seen_count = NEW.hits,
        status = NEW.status,
        reject_reason = NEW.reject_reason,
        meta_json = json_set(COALESCE(meta_json, '{}'),
          '$.evidence', NEW.evidence, '$.kind', COALESCE(NEW.kind, 'query'),
          '$.runs', NEW.runs, '$.accepted', NEW.accepted,
          '$.last_run_at', NEW.last_run_at, '$.fail_count', NEW.fail_count,
          '$.last_fail_at', NEW.last_fail_at)
      WHERE platform = 'youtube' AND kind = 'query' AND handle = OLD.query;
    END
    """,
    "DROP TRIGGER IF EXISTS temp.query_candidates_del",
    """
    CREATE TEMP TRIGGER query_candidates_del INSTEAD OF DELETE ON query_candidates BEGIN
      DELETE FROM candidate WHERE platform='youtube' AND kind='query' AND handle = OLD.query;
    END
    """,
]


# ---------------------------------------------------------------------------
# source / content: вспомогательные функции
# ---------------------------------------------------------------------------

def _source_id(conn: sqlite3.Connection, channel_id) -> int | None:
    if channel_id in (None, ""):
        return None
    row = conn.execute(
        "SELECT id FROM source WHERE platform='youtube' "
        "AND (external_id=? OR handle=?) ORDER BY (external_id=?) DESC LIMIT 1",
        (str(channel_id), str(channel_id), str(channel_id)),
    ).fetchone()
    return row[0] if row else None


def _content_id(conn: sqlite3.Connection, video_id) -> int | None:
    if video_id in (None, ""):
        return None
    row = conn.execute(
        "SELECT id FROM content WHERE platform='youtube' AND external_id=?",
        (str(video_id),),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Запись каналов и видео (сигнатуры и семантика прежнего tuber/db.py)
# ---------------------------------------------------------------------------

def upsert_channel(conn: sqlite3.Connection, data: Mapping[str, Any]) -> None:
    """Идемпотентно записать канал. ``first_seen`` не перезаписывается."""
    row = _pick(data, CHANNEL_COLUMNS)
    channel_id = row.get("channel_id")
    if not channel_id:
        raise ValueError("upsert_channel: нужен channel_id")
    handle = str(row.get("handle") or "").strip().lstrip("@") or str(channel_id)
    meta = {
        "video_count": row.get("video_count"),
        "view_count": row.get("view_count"),
        "topic_categories": row.get("topic_categories"),
        "uploads_playlist_id": row.get("uploads_playlist_id"),
        "is_russian": row.get("is_russian"),
    }
    with core_db.write_tx(conn):
        storage.upsert_source(
            conn, "youtube", handle,
            external_id=str(channel_id),
            title=row.get("title"),
            country=row.get("country"),
            lang=row.get("default_language"),
            subs=row.get("subscriber_count"),
            source_kind="youtube",
            meta_json=storage.jdump(meta),
        )
        # first_seen не перезаписывается, если уже был.
        first = _iso(row.get("first_seen"))
        if first is not None:
            conn.execute(
                "UPDATE source SET first_seen_at=COALESCE(first_seen_at, ?) "
                "WHERE platform='youtube' AND handle=?",
                (first, handle),
            )
        last = _iso(row.get("last_synced_at"))
        if last is not None:
            conn.execute(
                "UPDATE source SET last_synced_at=COALESCE(?, last_synced_at), "
                "external_id=COALESCE(external_id, ?) "
                "WHERE platform='youtube' AND handle=?",
                (last, str(channel_id), handle),
            )


def upsert_video(conn: sqlite3.Connection, data: Mapping[str, Any]) -> None:
    """Идемпотентно записать видео. ``first_seen`` не перезаписывается."""
    row = _pick(data, VIDEO_COLUMNS)
    video_id = row.get("video_id")
    if not video_id:
        raise ValueError("upsert_video: нужен video_id")
    video_id = str(video_id)

    cols = ["platform", "external_id"]
    vals: list[Any] = ["youtube", video_id]
    upd = ["platform=excluded.platform"]

    def add(col, value):
        cols.append(col)
        vals.append(value)
        upd.append(f"{col}=excluded.{col}")

    source_id = _source_id(conn, row.get("channel_id"))
    add("source_id", source_id)
    # D-45: прямая ссылка заполняется при записи (единый хелпер ядра).
    add("url", urls.content_url("youtube", video_id))
    if "title" in row:
        add("title", row["title"])
    if "description" in row:
        add("text", row["description"])
    if "default_language" in row:
        add("lang", row["default_language"])
    if "duration_seconds" in row:
        add("duration_seconds", row["duration_seconds"])
    if "category_id" in row:
        cid = row["category_id"]
        add("category", None if cid is None else str(cid))
    if "is_shorts" in row:
        add("is_short", int(bool(row["is_shorts"])))
        add("kind", "short" if row["is_shorts"] else "video")
    else:
        # Неизвестный формат — честный NULL (как legacy videos.is_shorts):
        # report/seo по нему доопределяют формат по длительности.
        add("is_short", None)
    pub = _iso(row.get("published_at"))
    add("published_at", pub if pub is not None else "1970-01-01 00:00:00")
    first = _iso(row.get("first_seen"))
    if first is not None:
        add("first_seen_at", first)
        upd[-1] = "first_seen_at=COALESCE(content.first_seen_at, excluded.first_seen_at)"
    last = _iso(row.get("last_seen"))
    if last is not None:
        add("last_seen_at", last)
    kind = "short" if row.get("is_shorts") else "video"
    if "kind" not in cols:
        add("kind", kind)
        upd[-1] = "kind=excluded.kind"

    meta = {k: row[k] for k in _VIDEO_META if k in row}
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO content ({', '.join(cols)}) VALUES ({placeholders})"
    sql += " ON CONFLICT(platform, external_id) DO UPDATE SET " + ", ".join(upd)
    with core_db.write_tx(conn):
        conn.execute(sql, vals)
        if meta:
            # Поля без колонок в ядре уезжают в meta_json (только заданные:
            # неуказанное не затирается).
            sets = []
            mvals: list[Any] = []
            for k, v in meta.items():
                sets.append(f"'$.{k}', ?")
                mvals.append(_iso(v) if k == "thumbnail_checked_at" else v)
            conn.execute(
                "UPDATE content SET meta_json=json_set(COALESCE(meta_json, '{}'), "
                + ", ".join(sets)
                + ") WHERE platform='youtube' AND external_id=?",
                mvals + [video_id],
            )


def _age_hours(conn: sqlite3.Connection, content_id: int, captured_at: str) -> int | None:
    row = conn.execute("SELECT published_at FROM content WHERE id=?", (content_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        pub = int(timeutil.iso_to_epoch(row[0]))
        cap = int(timeutil.iso_to_epoch(captured_at))
    except (TypeError, ValueError):
        return None
    return (cap - pub) // 3600


def insert_snapshot(
    conn: sqlite3.Connection,
    video_id: str,
    captured_at: int,
    bucket: str,
    views: int | None,
    likes: int | None = None,
    comments: int | None = None,
    age_hours: int | None = None,
    source: str | None = None,
) -> sqlite3.Row:
    """Записать замер и посчитать дельты/скорость (как прежний tuber.db)."""
    cid = _content_id(conn, video_id)
    if cid is None:
        raise ValueError(f"insert_snapshot: неизвестное video_id {video_id!r}")
    cap_iso = timeutil.epoch_to_iso(captured_at)
    prev = conn.execute(
        """
        SELECT captured_at, views, likes, comments FROM metric_snapshot
        WHERE content_id=? AND captured_at < ? ORDER BY captured_at DESC LIMIT 1
        """,
        (cid, cap_iso),
    ).fetchone()

    prev_captured_at = None
    interval_seconds = None
    interval_quality = "first"
    delta_views = delta_likes = delta_comments = None
    views_per_day = views_per_hour = None
    is_anomaly = 0
    speed_threshold = int(config.min_interval_for_speed_seconds())

    if prev is not None:
        prev_captured_at = timeutil.iso_to_epoch(prev["captured_at"])
        interval_seconds = int(captured_at) - int(prev_captured_at)
        if interval_seconds >= config.MIN_PAIR_INTERVAL_SECONDS:
            if views is not None and prev["views"] is not None:
                delta_views = int(views) - int(prev["views"])
                if delta_views < 0:
                    is_anomaly = 1
            if likes is not None and prev["likes"] is not None:
                delta_likes = int(likes) - int(prev["likes"])
            if comments is not None and prev["comments"] is not None:
                delta_comments = int(comments) - int(prev["comments"])
        if interval_seconds >= speed_threshold:
            interval_quality = "ok"
            if delta_views is not None:
                views_per_day = delta_views / (interval_seconds / 86400)
                views_per_hour = delta_views / (interval_seconds / 3600)
        else:
            interval_quality = "short"

    if age_hours is None:
        age_hours = _age_hours(conn, cid, cap_iso)

    with core_db.write_tx(conn):
        storage.add_snapshot(
            conn, cid, cap_iso,
            bucket=bucket, views=views, likes=likes, comments=comments,
            prev_captured_at=None, interval_seconds=interval_seconds,
            interval_quality=interval_quality, delta_views=delta_views,
            delta_likes=delta_likes, delta_comments=delta_comments,
            views_per_day=views_per_day, views_per_hour=views_per_hour,
            age_hours=age_hours, source=source, is_anomaly=is_anomaly,
            raw_json=storage.jdump({"prev_captured_at": timeutil.epoch_to_iso(prev_captured_at)}),
        )
    return conn.execute(
        "SELECT * FROM snapshots WHERE video_id=? AND bucket=? AND captured_at=?",
        (video_id, bucket, int(captured_at)),
    ).fetchone()


# ---------------------------------------------------------------------------
# Квота и LLM
# ---------------------------------------------------------------------------

def log_quota(
    conn: sqlite3.Connection,
    date: int,
    key_id: str,
    calls: int,
    units: int,
    endpoint: str,
    project: str | None = None,
    ts: int | None = None,
) -> None:
    """Записать расход квоты (агрегируется ядром по key_id/day/endpoint)."""
    if ts is None:
        ts = config.now_ts()
    day = timeutil.date_of(timeutil.epoch_to_iso(date)) or timeutil.date_of(timeutil.iso_now())
    ts_iso = timeutil.epoch_to_iso(ts)
    with core_db.write_tx(conn):
        conn.execute(
            """
            INSERT INTO quota_usage(platform, key_id, day, endpoint, project, calls, units, ts)
            VALUES('youtube', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, key_id, day, endpoint) DO UPDATE SET
              calls = COALESCE(quota_usage.calls, 0) + excluded.calls,
              units = COALESCE(quota_usage.units, 0) + excluded.units,
              project = COALESCE(excluded.project, quota_usage.project),
              ts = COALESCE(excluded.ts, quota_usage.ts)
            """,
            (key_id, day, endpoint, project, calls, units, ts_iso),
        )


def log_llm_usage(
    conn: sqlite3.Connection,
    stage: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    created_at: int | None = None,
) -> None:
    """Записать стоимость вызова модели."""
    if created_at is None:
        created_at = config.now_ts()
    with core_db.write_tx(conn):
        conn.execute(
            "INSERT INTO main.llm_usage(platform, stage, model, tokens_in, tokens_out, "
            "cost_usd, created_at) VALUES('youtube', ?, ?, ?, ?, ?, ?)",
            (stage, model, tokens_in, tokens_out, cost_usd,
             timeutil.epoch_to_iso(created_at)),
        )


# ---------------------------------------------------------------------------
# Классификация и скоры
# ---------------------------------------------------------------------------

def save_classification(conn: sqlite3.Connection, video_id: str, **fields: Any) -> None:
    """Записать разбор видео. Повторная запись перезаписывает строку."""
    if not video_id:
        raise ValueError("save_classification: нужен video_id")
    cid = _content_id(conn, video_id)
    if cid is None:
        return
    row = _pick(fields, CLASSIFICATION_COLUMNS)
    row.pop("video_id", None)
    if row.get("classified_at") is None:
        row["classified_at"] = config.now_ts()
    payload = {k: v for k, v in row.items() if k != "classified_at"}
    payload["classified_at"] = _iso(row["classified_at"])
    with core_db.write_tx(conn):
        storage.set_classification(conn, cid, **payload)


def upsert_seo_field(conn: sqlite3.Connection, video_id: str, **fields: Any) -> None:
    """Идемпотентно записать поля упаковки видео (ядро: seo_field)."""
    cid = _content_id(conn, video_id)
    if cid is None:
        return
    payload = {k: v for k, v in fields.items() if k != "video_id"}
    with core_db.write_tx(conn):
        storage.upsert_seo_field(conn, cid, **payload)


def add_comments(conn: sqlite3.Connection, video_id: str, rows: Iterable[Mapping[str, Any]]) -> int:
    """Записать верхние комментарии видео. Возвращает число новых строк.

    Повтор уже пойманного ``comment_id`` дубль не создаёт и не учитывается в
    счётчике (как прежний ``INSERT OR IGNORE INTO video_comments``).
    """
    cid = _content_id(conn, video_id)
    if cid is None:
        return 0
    written = 0
    with core_db.write_tx(conn):
        for row in rows:
            comment_id = row.get("comment_id")
            if not comment_id:
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO content_comment
                    (comment_id, content_id, author, text, likes, published_at, captured_at, platform)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'youtube')
                """,
                (str(comment_id), cid, row.get("author"), row.get("text"), row.get("likes"),
                 _iso(row.get("published_at")), _iso(row.get("captured_at"))),
            )
            if cur.rowcount and cur.rowcount > 0:
                written += 1
    return written


def upsert_comment_check(
    conn: sqlite3.Connection,
    video_id: str,
    checked_at=None,
    status: str | None = None,
    error: str | None = None,
) -> None:
    """Записать статус последнего обращения к commentThreads по видео."""
    cid = _content_id(conn, video_id)
    if cid is None:
        return
    with core_db.write_tx(conn):
        storage.upsert_comment_check(
            conn, cid, checked_at=_iso(checked_at), status=status, error=error,
        )


def save_score(
    conn: sqlite3.Connection,
    video_id: str,
    computed_at: int,
    **fields: Any,
) -> None:
    """Записать скор упаковки в video_scores (ядро: score). Идемпотентно."""
    if not video_id:
        raise ValueError("save_score: нужен video_id")
    if computed_at is None:
        raise ValueError("save_score: нужен computed_at")
    cid = _content_id(conn, video_id)
    if cid is None:
        return
    picked = _pick(fields, SCORE_COLUMNS)
    axes = {k: picked.get(k) for k in SCORE_AXES if k != "viral_index"}
    parts = picked.get("score_parts")
    significance = picked.get("outlier_score")
    with core_db.write_tx(conn):
        storage.upsert_score(
            conn, cid, timeutil.epoch_to_iso(computed_at),
            significance=significance,
            axes_json=storage.jdump(axes) or "{}",
            parts_json=parts,
        )


def set_viral_indices(
    conn: sqlite3.Connection,
    updates: Iterable[tuple],
) -> int:
    """Обновить viral_index (и score_parts) у конкретных строк video_scores."""
    changed = 0
    with core_db.write_tx(conn):
        for item in updates:
            video_id, computed_at, viral_index = item[0], item[1], item[2]
            parts = item[3] if len(item) > 3 else None
            cid = _content_id(conn, video_id)
            if cid is None:
                continue
            computed_iso = timeutil.epoch_to_iso(computed_at)
            row = conn.execute(
                "SELECT axes_json FROM score WHERE content_id=? AND computed_at=?",
                (cid, computed_iso),
            ).fetchone()
            if row is None:
                continue
            axes = row["axes_json"] or "{}"
            if parts is None:
                conn.execute(
                    "UPDATE score SET axes_json=json_set(?, '$.viral_index', ?), "
                    "significance=COALESCE(?, significance) "
                    "WHERE content_id=? AND computed_at=?",
                    (axes, viral_index, viral_index, cid, computed_iso),
                )
            else:
                conn.execute(
                    "UPDATE score SET axes_json=json_set(?, '$.viral_index', ?), "
                    "parts_json=?, significance=COALESCE(?, significance) "
                    "WHERE content_id=? AND computed_at=?",
                    (axes, viral_index, parts, viral_index, cid, computed_iso),
                )
            changed += 1
    return changed


def get_unclassified(
    conn: sqlite3.Connection,
    limit: int | None = None,
    ids: Iterable[str] | None = None,
) -> list[sqlite3.Row]:
    """Видео без разбора, свежие первыми (published_at DESC)."""
    sql = """
        SELECT v.video_id AS video_id, v.title AS title, v.description AS description,
               v.duration_seconds AS duration_seconds, v.published_at AS published_at,
               v.channel_id AS channel_id, c.title AS channel_title
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        LEFT JOIN video_classification vc ON vc.video_id = v.video_id
        WHERE vc.video_id IS NULL
    """
    params: list[Any] = []
    if ids is not None:
        wanted = [str(i) for i in ids if i]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        sql += f" AND v.video_id IN ({marks})"
        params.extend(wanted)
    sql += " ORDER BY v.published_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Кандидаты (channel + query) — этап 9
# ---------------------------------------------------------------------------

CANDIDATE_COLUMNS = (
    "channel_id", "title", "handle", "description", "source", "evidence",
    "mentions", "subscriber_count", "score", "status", "reject_reason",
    "resolve_attempts", "discovered_at", "probed_at",
)


def upsert_channel_candidate(conn: sqlite3.Connection, data: Mapping[str, Any]) -> None:
    """Записать канал-кандидата. Повторная находка увеличивает mentions."""
    row = _pick(data, CANDIDATE_COLUMNS)
    channel_id = row.get("channel_id")
    if not channel_id:
        raise ValueError("upsert_channel_candidate: нужен channel_id")
    row.setdefault("source", "manual")
    row.setdefault("status", "new")
    row.setdefault("mentions", 1)
    if row.get("discovered_at") is None:
        row["discovered_at"] = timeutil.iso_now()
    conn.execute(
        "INSERT INTO channel_candidates(channel_id, title, handle, description, source, "
        "evidence, mentions, subscriber_count, score, status, reject_reason, "
        "resolve_attempts, discovered_at, probed_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(channel_id), row.get("title"), row.get("handle"), row.get("description"),
         row["source"], row.get("evidence"), row.get("mentions"), row.get("subscriber_count"),
         row.get("score"), row["status"], row.get("reject_reason"),
         row.get("resolve_attempts") or 0, row["discovered_at"], row.get("probed_at")),
    )
    conn.commit()


def bump_candidate_mention(
    conn: sqlite3.Connection,
    channel_id: str,
    evidence: str | None = None,
    source: str | None = None,
) -> None:
    """Учесть повторное упоминание уже известного кандидата."""
    conn.execute(
        "UPDATE channel_candidates SET mentions = COALESCE(mentions, 0) + 1, "
        "evidence = COALESCE(?, evidence), source = COALESCE(?, source) "
        "WHERE channel_id = ?",
        (evidence, source, channel_id),
    )
    conn.commit()


def set_candidate_status(
    conn: sqlite3.Connection,
    channel_id: str,
    status: str,
    reject_reason: str | None = None,
    probed_at: str | None = None,
) -> None:
    """Сменить статус кандидата (new | accepted | rejected | unresolved)."""
    conn.execute(
        "UPDATE channel_candidates SET status=?, reject_reason=?, "
        "probed_at=COALESCE(?, probed_at) WHERE channel_id=?",
        (status, reject_reason, probed_at, channel_id),
    )
    conn.commit()


def mark_candidate_unresolved(
    conn: sqlite3.Connection,
    channel_id: str,
    reason: str | None = None,
    max_attempts: int = 3,
) -> str:
    """Пометить кандидата unresolved и учесть попытку разрешения."""
    conn.execute(
        "UPDATE channel_candidates SET resolve_attempts = "
        "COALESCE(resolve_attempts, 0) + 1 WHERE channel_id=?",
        (channel_id,),
    )
    row = conn.execute(
        "SELECT resolve_attempts FROM channel_candidates WHERE channel_id=?",
        (channel_id,),
    ).fetchone()
    attempts = int(row["resolve_attempts"]) if row else max_attempts
    if attempts >= int(max_attempts):
        final = f"{reason or 'handle не разрешён'} (попыток: {attempts})"
        conn.execute(
            "UPDATE channel_candidates SET status='rejected', reject_reason=? "
            "WHERE channel_id=?", (final, channel_id))
        conn.commit()
        return "rejected"
    conn.execute(
        "UPDATE channel_candidates SET status='unresolved', reject_reason=? "
        "WHERE channel_id=?", (reason, channel_id))
    conn.commit()
    return "unresolved"


def get_candidates(
    conn: sqlite3.Connection,
    status: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Кандидаты (по умолчанию все), свежие/скоринговые первыми."""
    sql = "SELECT * FROM channel_candidates"
    params: list[Any] = []
    if status is not None:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY score DESC, mentions DESC, discovered_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def candidate_source_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько новых кандидатов по каждому источнику."""
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM channel_candidates GROUP BY source"
    ).fetchall()
    return {r["source"]: int(r["n"]) for r in rows}


def known_channel_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> set[str]:
    """Какие из id уже есть в channels (для дедупликации до траты)."""
    wanted = [str(i) for i in ids if i]
    known: set[str] = set()
    for i in range(0, len(wanted), 500):
        part = wanted[i:i + 500]
        marks = ",".join("?" for _ in part)
        if not marks:
            continue
        rows = conn.execute(
            f"SELECT channel_id FROM channels WHERE channel_id IN ({marks})", part
        ).fetchall()
        known.update(r["channel_id"] for r in rows)
    return known


def upsert_query_candidate(conn: sqlite3.Connection, data: Mapping[str, Any]) -> None:
    """Записать кандидата-запрос. Повторная находка увеличивает hits."""
    row = _pick(data, (
        "query", "source", "evidence", "hits", "score", "status", "discovered_at", "kind",
    ))
    query = row.get("query")
    if not query:
        raise ValueError("upsert_query_candidate: нужен query")
    row.setdefault("source", "term_mining")
    row.setdefault("status", "new")
    row.setdefault("hits", 1)
    if row.get("discovered_at") is None:
        row["discovered_at"] = timeutil.iso_now()
    conn.execute(
        "INSERT INTO query_candidates(query, source, evidence, hits, score, status, "
        "discovered_at, kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (str(query), row["source"], row.get("evidence"), row.get("hits"),
         row.get("score"), row["status"], row["discovered_at"],
         row.get("kind") or "query"),
    )
    conn.commit()


def get_query_candidates(
    conn: sqlite3.Connection, status: str | None = None, kind: str | None = None,
) -> list[sqlite3.Row]:
    """Кандидаты-запросы, по убыванию hits."""
    sql = "SELECT * FROM query_candidates"
    params: list[Any] = []
    clauses: list[str] = []
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY hits DESC, score DESC"
    return conn.execute(sql, params).fetchall()


def set_query_candidate_status(
    conn: sqlite3.Connection,
    query: str,
    status: str,
    reject_reason: str | None = None,
) -> None:
    """Сменить статус запроса-кандидата (new | accepted | rejected)."""
    conn.execute(
        "UPDATE query_candidates SET status=?, reject_reason=? WHERE query=?",
        (status, reject_reason, query),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Миграции-пересчёты (совместимость с прежним tuber/db.py)
# ---------------------------------------------------------------------------

def migrate_interval_quality(conn: sqlite3.Connection, cfg: Any = config) -> int:
    """Проставить interval_quality и обнулить скорость на коротких интервалах."""
    threshold = int(config.min_interval_for_speed_seconds(cfg))
    first = "interval_seconds IS NULL AND interval_quality IS NOT 'first'"
    short = ("interval_seconds IS NOT NULL AND interval_seconds < ? "
             "AND (interval_quality IS NOT 'short' OR views_per_day IS NOT NULL "
             "OR views_per_hour IS NOT NULL)")
    ok = ("interval_seconds IS NOT NULL AND interval_seconds >= ? "
          "AND interval_quality IS NOT 'ok'")
    # rowcount по представлению с INSTEAD OF-триггером не определён, поэтому
    # число изменяемых строк считаем заранее тем же условием.
    changed = conn.execute(f"SELECT COUNT(*) FROM snapshots WHERE {first}").fetchone()[0]
    changed += conn.execute(f"SELECT COUNT(*) FROM snapshots WHERE {short}", (threshold,)).fetchone()[0]
    changed += conn.execute(f"SELECT COUNT(*) FROM snapshots WHERE {ok}", (threshold,)).fetchone()[0]
    if changed:
        with core_db.write_tx(conn):
            conn.execute(f"UPDATE snapshots SET interval_quality='first' WHERE {first}")
            conn.execute("UPDATE snapshots SET interval_quality='short', views_per_day=NULL, "
                         f"views_per_hour=NULL WHERE {short}", (threshold,))
            conn.execute(f"UPDATE snapshots SET interval_quality='ok' WHERE {ok}", (threshold,))
    return int(changed)


def recompute_shorts(conn: sqlite3.Connection, cfg: Any = config) -> int:
    """Пересчитать is_shorts у всех видео по сохранённой длительности."""
    rows = conn.execute("SELECT video_id, duration_seconds, is_shorts FROM videos").fetchall()
    changed = 0
    with core_db.write_tx(conn):
        for row in rows:
            new_value = config.is_probable_shorts(row["duration_seconds"], cfg)
            old_value = row["is_shorts"]
            if old_value is None and new_value is None:
                continue
            if old_value != new_value:
                conn.execute(
                    "UPDATE content SET is_short=?, kind=? "
                    "WHERE platform='youtube' AND external_id=?",
                    (new_value, "short" if new_value else "video", row["video_id"]),
                )
                changed += 1
    return changed


_DEAD_QUERY_REASON = ("непригодна для поиска (служебный ключ, нет букв или "
                       "короче 3 знаков)")


def migrate_drop_dead_queries(conn: sqlite3.Connection) -> None:
    """Пометить «мёртвые» принятые фразы отсеянными (ТЗ-33). Идемпотентно."""
    rows = conn.execute(
        "SELECT query FROM query_candidates WHERE kind='query' AND runs=0 "
        "AND status IN ('accepted','new') AND COALESCE(source, '') <> 'playlist'"
    ).fetchall()
    dead = [str(r["query"]) for r in rows if not is_human_query(r["query"])]
    if not dead:
        return
    with core_db.write_tx(conn):
        conn.executemany(
            "UPDATE query_candidates SET status='dropped', reject_reason=? "
            "WHERE query=? AND kind='query' AND runs=0 "
            "AND COALESCE(source, '') <> 'playlist'",
            [(_DEAD_QUERY_REASON, q) for q in dead],
        )


def migrate_restore_usable_queries(conn: sqlite3.Connection) -> None:
    """Вернуть фразы, ошибочно закрытые миграцией ТЗ-33 (ТЗ-34). Идемпотентно."""
    rows = conn.execute(
        "SELECT query, source FROM query_candidates WHERE kind='query' "
        "AND status='dropped' AND reject_reason=?", (_DEAD_QUERY_REASON,),
    ).fetchall()
    restore = [str(r["query"]) for r in rows
               if str(r["source"] or "") == "playlist" or is_human_query(r["query"])]
    if not restore:
        return
    with core_db.write_tx(conn):
        conn.executemany(
            "UPDATE query_candidates SET status='accepted', reject_reason=NULL "
            "WHERE query=? AND kind='query' AND status='dropped' AND reject_reason=?",
            [(q, _DEAD_QUERY_REASON) for q in restore],
        )


def register_static_phrases(conn: sqlite3.Connection) -> None:
    """Зарегистрировать статические фразы конфига как accepted. Идемпотентно."""
    existing = {r[0] for r in conn.execute("SELECT query FROM query_candidates")}
    new = [p for p in getattr(config, "CHANNEL_SEARCH_QUERIES", []) if p not in existing]
    if not new:
        return
    with core_db.write_tx(conn):
        for phrase in new:
            conn.execute(
                "INSERT INTO query_candidates(query, source, evidence, hits, score, "
                "status, discovered_at, kind) "
                "VALUES(?, 'channel_search_static', NULL, 1, 0.0, 'accepted', ?, 'query')",
                (phrase, timeutil.iso_now()),
            )


def requeue_unresolved_handles(conn: sqlite3.Connection) -> int:
    """Вернуть handles, потерянные из-за лимита, обратно в unresolved."""
    pred = ("status='rejected' AND reject_reason='handle не разрешён'")
    n = conn.execute(f"SELECT COUNT(*) FROM channel_candidates WHERE {pred}").fetchone()[0]
    if n:
        with core_db.write_tx(conn):
            conn.execute(
                "UPDATE channel_candidates SET status='unresolved', "
                "resolve_attempts=MAX(COALESCE(resolve_attempts,0),1) WHERE " + pred)
    return int(n)


def drop_exhausted_phrases(conn: sqlite3.Connection) -> int:
    """Отсеять новые фразы после двух нулевых кругов. Идемпотентно."""
    pred = ("kind='query' AND runs >= 2 AND accepted = 0 AND status IN ('accepted','new')")
    n = conn.execute(f"SELECT COUNT(*) FROM query_candidates WHERE {pred}").fetchone()[0]
    if n:
        with core_db.write_tx(conn):
            conn.execute(
                "UPDATE query_candidates SET status='dropped', "
                "reject_reason='нулевой урожай после 2 кругов' WHERE " + pred)
    return int(n)


def is_human_query(value: Any) -> bool:
    """Похоже ли значение на человеческую поисковую фразу (ТЗ-33, ТЗ-34)."""
    text = str(value or "")
    if text != text.strip() or len(text) < 2:
        return False
    found_letter = False
    for ch in text:
        if ch.isalpha():
            found_letter = True
        elif ch != " ":
            return False
    return found_letter
