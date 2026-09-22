"""Единая схема ядра (ТЗ-1, §3).

Здесь лежит ровно DDL из спецификации — имена таблиц, колонок, индексов и
представлений менять нельзя. Платформенные расширения из legacy
(``seo_fields``, ``thumbnail_vision``, ``video_comments``, ``comment_checks``)
перенесены как есть; изменилось только имя ссылочной колонки на ``content``:
``video_id``/``post_id``/``tweet_id`` → ``content_id``.

Все даты — TEXT ISO-8601 UTC (см. :mod:`tuber.core.timeutil`).
"""

from __future__ import annotations

import hashlib
import sqlite3

from tuber.core import urls

SCHEMA_VERSION = "6"

# ``legacy_map`` вынесен отдельной константой: тип ``target_id`` менялся с
# INTEGER на TEXT (см. TECH-DEBT D-02), и при домиграции таблицу приходится
# пересобирать (SQLite не умеет ``ALTER COLUMN``).
LEGACY_MAP_DDL = """
    CREATE TABLE IF NOT EXISTS legacy_map (
      legacy_db TEXT NOT NULL, legacy_table TEXT NOT NULL, legacy_id TEXT NOT NULL,
      target_table TEXT NOT NULL, target_id TEXT NOT NULL, migrated_at TEXT,
      PRIMARY KEY (legacy_db, legacy_table, legacy_id)
    )
    """

# ---------------------------------------------------------------------------
# Таблицы
# ---------------------------------------------------------------------------

_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)
    """,
    """
    CREATE TABLE IF NOT EXISTS platform (code TEXT PRIMARY KEY, title TEXT, meta_json TEXT)
    """,
    """
    CREATE TABLE IF NOT EXISTS source (
      id INTEGER PRIMARY KEY,
      platform TEXT NOT NULL REFERENCES platform(code),
      external_id TEXT,
      handle TEXT NOT NULL,
      title TEXT, url TEXT, lang TEXT, country TEXT, topic_guess TEXT,
      is_author INTEGER, status TEXT, tier TEXT,
      subs INTEGER, subs_at TEXT,
      posts_per_day REAL, avg_views INTEGER, vr REAL,
      ai_density REAL, cv_interval REAL, link_ratio REAL, rt_ratio REAL, dup_ratio REAL,
      first_mover_score REAL, posts_collected INTEGER DEFAULT 0,
      read_mode TEXT, antifraud_flag INTEGER DEFAULT 0, flood_until TEXT,
      fail_streak INTEGER DEFAULT 0, last_error TEXT, cursor TEXT,
      added_at TEXT, added_by TEXT, source_kind TEXT, notes TEXT, verified_at TEXT, checked_at TEXT,
      first_seen_at TEXT, last_synced_at TEXT, meta_json TEXT,
      UNIQUE(platform, handle)
    )
    """,
    # ТЗ-3d: индекс на ``source(platform, external_id)`` принадлежит ядру.
    # Оба адаптера (X и YouTube) ходили в ``source`` по этой паре через свои
    # представления, и каждый создавал СВОЙ индекс на таблице ядра
    # (``idx_x_source_ext`` и ``idx_youtube_source_ext``) — точный дубль одного
    # и того же. Теперь индекс один и создаёт его ядро (D-33).
    "CREATE INDEX IF NOT EXISTS main.idx_source_ext ON source(platform, external_id)",
    """
    CREATE TABLE IF NOT EXISTS source_baseline (
      source_id INTEGER PRIMARY KEY REFERENCES source(id) ON DELETE CASCADE,
      window_days INTEGER, items_in_window INTEGER,
      median_views REAL, median_likes REAL, median_reactions REAL, median_er REAL,
      is_author_data INTEGER, hashed_items INTEGER, dup_items INTEGER, dup_ratio REAL,
      computed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS content (
      id INTEGER PRIMARY KEY,
      platform TEXT NOT NULL REFERENCES platform(code),
      source_id INTEGER REFERENCES source(id) ON DELETE CASCADE,
      source_external_id TEXT,
      external_id TEXT NOT NULL,
      kind TEXT,
      url TEXT, title TEXT, text TEXT, text_hash TEXT, lang TEXT,
      author_handle TEXT,
      published_at TEXT NOT NULL,
      duration_seconds INTEGER, category TEXT, media_kind TEXT,
      is_repost INTEGER DEFAULT 0, is_quote INTEGER DEFAULT 0, is_reply INTEGER DEFAULT 0,
      is_promo INTEGER DEFAULT 0, is_short INTEGER DEFAULT 0,
      links TEXT, mentions TEXT, hashtags TEXT,
      first_seen_at TEXT, last_seen_at TEXT, deleted_at TEXT, meta_json TEXT,
      UNIQUE(platform, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_content_pub ON content(platform, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_content_source ON content(source_id, published_at)",
    "CREATE INDEX IF NOT EXISTS main.idx_content_source_ext ON content(platform, source_external_id)",
    "CREATE INDEX IF NOT EXISTS idx_content_hash ON content(text_hash)",
    "CREATE INDEX IF NOT EXISTS idx_content_author ON content(author_handle)",
    """
    CREATE TABLE IF NOT EXISTS metric_snapshot (
      id INTEGER PRIMARY KEY,
      content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
      platform TEXT,
      external_id TEXT,
      captured_at TEXT NOT NULL,
      bucket TEXT, source TEXT, age_hours REAL, interval_seconds INTEGER, interval_quality TEXT,
      views INTEGER, likes INTEGER, comments INTEGER, replies INTEGER,
      reposts INTEGER, forwards INTEGER, reactions INTEGER, quotes INTEGER,
      delta_views INTEGER, delta_likes INTEGER, delta_comments INTEGER,
      delta_replies INTEGER, delta_reposts INTEGER,
      views_per_day REAL, views_per_hour REAL,
      is_anomaly INTEGER NOT NULL DEFAULT 0, raw_json TEXT,
      UNIQUE(content_id, captured_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_metric_content ON metric_snapshot(content_id, captured_at)",
    "CREATE INDEX IF NOT EXISTS main.idx_metric_ext ON metric_snapshot(platform, external_id, captured_at)",
    """
    CREATE TABLE IF NOT EXISTS content_latest (
      content_id INTEGER PRIMARY KEY REFERENCES content(id) ON DELETE CASCADE,
      platform TEXT,
      external_id TEXT,
      captured_at TEXT, views INTEGER, likes INTEGER, comments INTEGER, replies INTEGER,
      reposts INTEGER, forwards INTEGER, reactions INTEGER,
      views_per_day REAL, views_per_hour REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classification (
      content_id INTEGER PRIMARY KEY REFERENCES content(id) ON DELETE CASCADE,
      platform TEXT,
      external_id TEXT,
      is_ai INTEGER, topic TEXT, subtopic TEXT, claim_type TEXT,
      source_type TEXT, entity_tier TEXT, novelty REAL, lang TEXT, confidence REAL,
      method TEXT, model TEXT, prompt_ver TEXT, status TEXT,
      attempts INTEGER DEFAULT 0, error TEXT,
      prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, classified_at TEXT,
      title_ru TEXT, summary_ru TEXT, reason TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS main.idx_classification_ext ON classification(platform, external_id)",
    """
    CREATE TABLE IF NOT EXISTS classify_cache (
      text_hash TEXT PRIMARY KEY,
      is_ai INTEGER, topic TEXT, subtopic TEXT, claim_type TEXT, novelty REAL, lang TEXT,
      title_ru TEXT, summary_ru TEXT,
      method TEXT, model TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT,
      prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL,
      classified_at TEXT, first_seen_at TEXT,
      -- Дополнительные поля платформ: у X в legacy-таблице ``classified`` есть
      -- ``tweet_id`` (пост-первоисточник текста), в ядре колонки нет. Кладём
      -- платформенные остатки в ``meta_json`` — тот же приём, что для
      -- ``candidate.meta_json`` (ТЗ-1, D-01/D-06; ТЗ-3 §1).
      meta_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS score (
      content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
      platform TEXT,
      external_id TEXT,
      computed_at TEXT NOT NULL,
      significance REAL, branch TEXT,
      engagement REAL, velocity REAL, spread REAL, first_mover REAL, decay REAL, freshness REAL,
      xconf INTEGER, anomaly INTEGER DEFAULT 0,
      metrics_missing INTEGER DEFAULT 0, metrics_at TEXT, metrics_age_hours REAL,
      axes_json TEXT, parts_json TEXT, story_id INTEGER,
      PRIMARY KEY (content_id, computed_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_score_sig ON score(significance)",
    "CREATE INDEX IF NOT EXISTS main.idx_score_ext ON score(platform, external_id, computed_at)",
    """
    CREATE VIEW IF NOT EXISTS v_score_current AS
      SELECT s.* FROM score s
      JOIN (SELECT content_id, MAX(computed_at) AS c FROM score GROUP BY content_id) m
        ON m.content_id=s.content_id AND m.c=s.computed_at
    """,
    """
    CREATE TABLE IF NOT EXISTS story (
      id INTEGER PRIMARY KEY,
      platform TEXT REFERENCES platform(code),
      created_at TEXT, window_hours INTEGER, threshold INTEGER,
      title TEXT, topic TEXT,
      canonical_content_id INTEGER REFERENCES content(id),
      first_content_id INTEGER REFERENCES content(id),
      first_mover_source_id INTEGER REFERENCES source(id),
      first_pub_at TEXT, last_pub_at TEXT,
      source_count INTEGER DEFAULT 0, content_count INTEGER DEFAULT 0, xconf INTEGER DEFAULT 0,
      lead_time_min REAL, topics TEXT, entities TEXT,
      is_new_entity INTEGER DEFAULT 0, is_single INTEGER DEFAULT 0, suspect INTEGER DEFAULT 0,
      claimed_at TEXT, similarity_threshold REAL, significance REAL, rank_score REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS story_member (
      story_id INTEGER NOT NULL REFERENCES story(id) ON DELETE CASCADE,
      content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
      role TEXT, sim REAL, is_canonical INTEGER DEFAULT 0, added_at TEXT,
      -- Автор поста на момент кластеризации (legacy ``story_posts.handle``).
      -- Колонка аддитивная: у баз, перенесённых до её появления, handle
      -- выводится адаптером из ``content`` (см. TECH-DEBT D-26).
      handle TEXT,
      PRIMARY KEY (story_id, content_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate (
      id INTEGER PRIMARY KEY,
      platform TEXT NOT NULL REFERENCES platform(code),
      kind TEXT, handle TEXT NOT NULL, external_id TEXT,
      score_priority REAL DEFAULT 0,
      found_via TEXT, found_in_handle TEXT, found_in_source_id INTEGER REFERENCES source(id),
      seen_count INTEGER DEFAULT 1, distinct_sources INTEGER DEFAULT 1, sources_json TEXT,
      display_handle TEXT, meta_json TEXT,
      first_seen_at TEXT, last_seen_at TEXT,
      validated TEXT, reject_reason TEXT, llm_checked INTEGER DEFAULT 0,
      ai_hint INTEGER DEFAULT 0, spam INTEGER DEFAULT 0, lang_guess TEXT, rubric TEXT,
      promoted_by TEXT, promoted_at TEXT, status TEXT DEFAULT 'new',
      UNIQUE(platform, handle)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate(status)",
    """
    CREATE TABLE IF NOT EXISTS quota_usage (
      platform TEXT NOT NULL, project TEXT, key_id TEXT, day TEXT NOT NULL,
      endpoint TEXT NOT NULL, calls INTEGER, units INTEGER, ts TEXT,
      PRIMARY KEY (platform, key_id, day, endpoint)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transport_instance (
      platform TEXT NOT NULL, host TEXT NOT NULL,
      healthy INTEGER, rss_ok INTEGER, items_last_test INTEGER, last_check_at TEXT,
      fail_streak INTEGER DEFAULT 0, cooldown_until TEXT, requests_today INTEGER, day TEXT,
      version TEXT, last_error TEXT, rate_limited_429 INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0,
      meta_json TEXT, PRIMARY KEY (platform, host)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transport_request (
      id INTEGER PRIMARY KEY, platform TEXT, host TEXT, ts TEXT, kind TEXT, url TEXT,
      status INTEGER, items INTEGER, latency_ms INTEGER, run_id INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_treq_host_ts ON transport_request(host, ts)",
    """
    CREATE TABLE IF NOT EXISTS transport_account_state (
      platform TEXT NOT NULL, name TEXT NOT NULL, flood_until TEXT, last_error TEXT,
      resolves_today INTEGER DEFAULT 0, day TEXT, updated_at TEXT, PRIMARY KEY (platform, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run (
      id INTEGER PRIMARY KEY, platform TEXT, started_at TEXT, finished_at TEXT, mode TEXT,
      ok_count INTEGER, fail_count INTEGER, items_new INTEGER, items_upd INTEGER,
      errors INTEGER, note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_log (
      id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES run(id) ON DELETE CASCADE,
      platform TEXT,
      ts TEXT, level TEXT, ref TEXT, msg TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_log_run ON run_log(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_run_log_platform ON run_log(platform)",
    """
    CREATE TABLE IF NOT EXISTS metrics_daily (
      day TEXT NOT NULL, platform TEXT NOT NULL,
      items_ingested INTEGER, dup_rate REAL, coverage REAL, fail_rate REAL,
      latency_p95_min REAL, valid_date_ratio REAL, instances_alive INTEGER,
      likes_median REAL, enriched_ratio INTEGER, errors INTEGER, extra_json TEXT,
      PRIMARY KEY (day, platform)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_usage (
      id INTEGER PRIMARY KEY, platform TEXT, stage TEXT, model TEXT,
      tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, created_at TEXT
    )
    """,
    "CREATE TABLE IF NOT EXISTS topic (name TEXT PRIMARY KEY, platform TEXT, weight REAL)",
    """
    CREATE TABLE IF NOT EXISTS report_text (
      text_hash TEXT PRIMARY KEY, ru TEXT, model TEXT, created_at TEXT, src TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS blocklist (
      platform TEXT NOT NULL, handle TEXT NOT NULL, reason TEXT, added_at TEXT,
      PRIMARY KEY (platform, handle)
    )
    """,
    # --- Платформенные расширения tuber-os (видеоядро), video_id → content_id ---
    """
    CREATE TABLE IF NOT EXISTS seo_field (
      content_id           INTEGER PRIMARY KEY REFERENCES content(id) ON DELETE CASCADE,
      platform             TEXT,
      external_id          TEXT,
      title_length         INTEGER,
      title_words          INTEGER,
      title_has_number     INTEGER,
      title_has_question   INTEGER,
      title_has_colon      INTEGER,
      title_caps_ratio     REAL,
      title_emoji_count    INTEGER,
      title_top_words      TEXT,
      thumb_text           TEXT,
      thumb_text_words     INTEGER,
      thumb_objects        TEXT,
      thumb_face_count     INTEGER,
      thumb_arrows         INTEGER,
      thumb_colors         TEXT,
      thumb_style          TEXT,
      desc_length          INTEGER,
      desc_links           INTEGER,
      desc_hashtags        INTEGER,
      desc_timestamps      INTEGER,
      desc_cta             INTEGER,
      tags_count           INTEGER,
      tags_common          TEXT,
      published_hour_msk   INTEGER,
      published_weekday    INTEGER,
      published_hour_local INTEGER,
      title_matches_topic  INTEGER,
      seo_pattern          TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS main.idx_seo_field_ext ON seo_field(platform, external_id)",
    """
    CREATE TABLE IF NOT EXISTS thumbnail_vision (
      id              INTEGER PRIMARY KEY,
      content_id      INTEGER REFERENCES content(id) ON DELETE CASCADE,
      platform        TEXT,
      external_id     TEXT,
      model           TEXT,
      prompt_version  TEXT,
      description_raw TEXT,
      extracted_text  TEXT,
      cost_usd        REAL,
      latency_ms      INTEGER,
      created_at      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS main.idx_thumbnail_vision_ext ON thumbnail_vision(platform, external_id)",
    """
    CREATE TABLE IF NOT EXISTS content_comment (
      comment_id   TEXT PRIMARY KEY,
      content_id   INTEGER REFERENCES content(id) ON DELETE CASCADE,
      platform     TEXT,
      external_id  TEXT,
      author       TEXT,
      text         TEXT,
      likes        INTEGER,
      published_at TEXT,
      captured_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_check (
      content_id INTEGER PRIMARY KEY REFERENCES content(id) ON DELETE CASCADE,
      platform   TEXT,
      external_id TEXT,
      checked_at TEXT,
      status     TEXT,
      error      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS main.idx_comment_check_ext ON comment_check(platform, external_id)",
    "CREATE INDEX IF NOT EXISTS main.idx_content_comment_ext ON content_comment(platform, external_id)",
    """
    CREATE TABLE IF NOT EXISTS cursor (
      platform TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
      cursor TEXT, last_page_at TEXT, pages_total INTEGER, items_total INTEGER,
      updated_at TEXT, meta_json TEXT,
      PRIMARY KEY (platform, kind, ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classify_daily (
      day TEXT NOT NULL, platform TEXT NOT NULL,
      posts INTEGER DEFAULT 0, model_calls INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
      prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
      PRIMARY KEY (day, platform)
    )
    """,
    # ------------------------------------------------------------------ edges
    # ТЗ-8 §1: рёбра «пост → внешний источник». Таблица отдельная: реестр
    # ``source`` заточен под аккаунт с handle, а цель ребра может быть доменом
    # (веб-фид), каналом Telegram/YouTube или X-аккаунтом. ``from_content_id``
    # может быть NULL для разовых постов, поэтому UNIQUE с NULL не мешает
    # нескольким таким рёбрам (в SQLite NULL не конфликтует).
    #
    # ``competitor`` — флаг Р1.4: ребро на домен-платформу-конкурента
    # (max.ru/vk.com/…) СОЗДАЁТСЯ (не теряется молча), но помечается, чтобы
    # выдача «на подключение» могла отфильтровать его одним условием.
    """
    CREATE TABLE IF NOT EXISTS edge (
      id INTEGER PRIMARY KEY,
      from_platform TEXT NOT NULL,
      from_source_id INTEGER REFERENCES source(id) ON DELETE SET NULL,
      from_content_id INTEGER REFERENCES content(id) ON DELETE CASCADE,
      from_handle TEXT,
      kind TEXT NOT NULL,
      target_type TEXT NOT NULL,
      target_platform TEXT,
      target_value TEXT NOT NULL,
      target_url TEXT,
      weight REAL NOT NULL DEFAULT 0,
      evidence TEXT,
      origin TEXT,
      competitor INTEGER NOT NULL DEFAULT 0,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      seen_count INTEGER NOT NULL DEFAULT 1,
      UNIQUE(from_content_id, kind, target_value)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_edge_target "
    "ON edge(target_type, target_platform, target_value)",
    "CREATE INDEX IF NOT EXISTS idx_edge_from ON edge(from_platform, from_source_id)",
    # Реестр первопроходцев (ТЗ-47, контур 4): агрегаты по источникам.
    # ``trusted_indegree_30d`` — сколько разных крупных авторов сослались;
    # ``lead_time_median`` — медианный лаг автора внутри сюжетов (минуты,
    # отрицательное = раньше остальных); ``first_mover_share`` — доля сюжетов,
    # где автор был первым (≥ 30 минут до второго автора).
    """
    CREATE TABLE IF NOT EXISTS first_mover (
      source_id INTEGER PRIMARY KEY REFERENCES source(id) ON DELETE CASCADE,
      platform TEXT, handle TEXT,
      first_moves INTEGER NOT NULL DEFAULT 0,
      stories INTEGER NOT NULL DEFAULT 0,
      first_mover_share REAL,
      lead_time_median REAL,
      lead_time_posts INTEGER NOT NULL DEFAULT 0,
      trusted_indegree_30d INTEGER NOT NULL DEFAULT 0,
      computed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_first_mover_indeg "
    "ON first_mover(trusted_indegree_30d)",
    # Ежедневный снимок метрик источника (ТЗ-45, контур 3 «Сливки»).
    # Нужен, чтобы считать рост подписчиков ``g7`` и восстанавливать динамику
    # осей breakout, которой в базе нет: до этой волны ``subs`` хранился одним
    # значением (``source.subs``) без ряда. Гранулярность — один снимок на
    # источник в сутки (UTC), повторный прогон в тот же день перезаписывает
    # строку (идемпотентность). Даты — TEXT ``YYYY-MM-DD``.
    """
    CREATE TABLE IF NOT EXISTS source_metric_history (
      source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
      day TEXT NOT NULL,
      subs INTEGER,
      posts_7d INTEGER,
      median_likes_24h REAL,
      viral_posts_14d INTEGER,
      trusted_indegree_30d INTEGER,
      lead_time_median REAL,
      captured_at TEXT,
      PRIMARY KEY (source_id, day)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_smh_day ON source_metric_history(day)",
    # Новинки внешнего контура (ТЗ-48, контур 5). Уникальный ключ — день+имя:
    # повторный прогон в тот же день перезаписывает строку. ``tier`` — "strict"
    # (формула ТЗ только на внутренних источниках) или "external" (объединённый
    # счёт внутренних и внешних источников). ``evidence_json`` — внешние ссылки
    # с очками/комментариями (для аудита и офлайн-разбора).
    """
    CREATE TABLE IF NOT EXISTS novelty (
      day TEXT NOT NULL,
      entity TEXT NOT NULL,
      tier TEXT,
      new_internal INTEGER DEFAULT 0,
      internal_sources INTEGER, internal_platforms TEXT,
      external_sources INTEGER, external_platforms TEXT,
      total_sources INTEGER,
      internal_url TEXT, external_url TEXT,
      evidence_json TEXT, computed_at TEXT,
      PRIMARY KEY (day, entity)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_novelty_day ON novelty(day)",
    # Очередь замеров метрик (ТЗ-43, контур 1 «Скорость»). Каждая строка —
    # запланированный замер поста X/Telegram на стадии ``1h/6h/24h/72h`` от
    # ``published_at`` (ТЗ-43), ``due_at`` — когда замер полагается, ``done_at`` — когда
    # сделан (NULL = ещё нет), ``attempt`` — число попыток. ``status`` (ТЗ-53,
    # D-67) различает состояния: ``pending`` (ещё не измерено), ``measured``
    # (измерено, снимок со стадией есть), ``closed_no_data`` (стадия закрыта без
    # данных: просрочена безнадёжно или исчерпаны попытки). Без статуса
    # «закрыто без данных» было неотличимо от «измерено» по одному ``done_at``.
    # Первичный ключ
    # ``(content_id, stage)`` делает планирование идемпотентным: повторный
    # прогон не плодит строк. Стадии YouTube живут отдельно (слоты h0..h6/d
    # планирует ``youtube/schedule.py``), эту таблицу не трогают.
    """
    CREATE TABLE IF NOT EXISTS metric_schedule (
      content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
      platform TEXT,
      stage TEXT NOT NULL,
      due_at TEXT NOT NULL,
      done_at TEXT,
      attempt INTEGER NOT NULL DEFAULT 0,
      status TEXT DEFAULT 'pending',
      PRIMARY KEY (content_id, stage)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_metric_schedule_due ON metric_schedule(done_at, due_at)",
    LEGACY_MAP_DDL,
]

# ---------------------------------------------------------------------------
# Представления совместимости со старыми формами (§3).
# Колонки — ровно как в legacy-таблицах, данные — из ядра.
# ---------------------------------------------------------------------------

_VIEWS: list[str] = [
    """
    CREATE VIEW IF NOT EXISTS v_os_videos AS
    SELECT
      c.external_id                                              AS video_id,
      c.source_external_id                                       AS channel_id,
      c.title                                                    AS title,
      c.text                                                     AS description,
      json_extract(c.meta_json, '$.tags')                        AS tags,
      c.category                                                 AS category_id,
      c.lang                                                     AS default_language,
      c.duration_seconds                                         AS duration_seconds,
      c.is_short                                                 AS is_shorts,
      CAST(strftime('%s', c.published_at) AS INTEGER)            AS published_at,
      json_extract(c.meta_json, '$.thumbnail_url')               AS thumbnail_url,
      json_extract(c.meta_json, '$.thumbnail_width')             AS thumbnail_width,
      json_extract(c.meta_json, '$.thumbnail_height')            AS thumbnail_height,
      json_extract(c.meta_json, '$.thumbnail_checked_at')        AS thumbnail_checked_at,
      json_extract(c.meta_json, '$.live_broadcast')              AS live_broadcast,
      json_extract(c.meta_json, '$.caption_available')           AS caption_available,
      json_extract(c.meta_json, '$.primary_topic')               AS primary_topic,
      json_extract(c.meta_json, '$.topic_confidence')            AS topic_confidence,
      CAST(strftime('%s', c.first_seen_at) AS INTEGER)           AS first_seen,
      CAST(strftime('%s', c.last_seen_at) AS INTEGER)            AS last_seen
    FROM content c
    WHERE c.platform = 'youtube'
    """,
    """
    CREATE VIEW IF NOT EXISTS v_os_snapshots AS
    SELECT
      m.id                                                        AS id,
      m.external_id                                               AS video_id,
      CAST(strftime('%s', m.captured_at) AS INTEGER)              AS captured_at,
      m.bucket                                                    AS bucket,
      m.views                                                     AS views,
      m.likes                                                     AS likes,
      m.comments                                                  AS comments,
      json_extract(m.raw_json, '$.prev_captured_at')              AS prev_captured_at,
      m.interval_seconds                                          AS interval_seconds,
      m.delta_views                                               AS delta_views,
      m.delta_likes                                               AS delta_likes,
      m.delta_comments                                            AS delta_comments,
      m.views_per_day                                             AS views_per_day,
      m.views_per_hour                                            AS views_per_hour,
      m.age_hours                                                 AS age_hours,
      m.source                                                    AS source,
      m.is_anomaly                                                AS is_anomaly,
      m.interval_quality                                          AS interval_quality
    FROM metric_snapshot m
    WHERE m.platform = 'youtube'
    """,
    """
    CREATE VIEW IF NOT EXISTS v_x_posts AS
    SELECT
      c.id                                                        AS id,
      c.source_id                                                 AS account_id,
      c.external_id                                               AS tweet_id,
      c.published_at                                              AS published_at_utc,
      json_extract(c.meta_json, '$.published_src')                AS published_src,
      c.text                                                      AS text,
      c.text_hash                                                 AS text_hash,
      c.lang                                                      AS lang,
      c.links                                                     AS links,
      c.mentions                                                  AS mentions,
      c.hashtags                                                  AS hashtags,
      c.is_repost                                                 AS is_retweet,
      c.is_quote                                                  AS is_quote,
      c.is_reply                                                  AS is_reply,
      json_extract(c.meta_json, '$.owner_handle')                 AS owner_handle,
      json_extract(c.meta_json, '$.orig_handle')                  AS orig_handle,
      c.media_kind                                                AS media_kind,
      c.first_seen_at                                             AS first_seen_at,
      cl.likes                                                    AS likes,
      cl.replies                                                  AS replies,
      json_extract(c.meta_json, '$.has_quote')                    AS has_quote,
      json_extract(c.meta_json, '$.is_long')                      AS is_long,
      json_extract(c.meta_json, '$.metrics_at')                   AS metrics_at,
      json_extract(c.meta_json, '$.metrics_src')                  AS metrics_src,
      json_extract(c.meta_json, '$.pinned')                       AS pinned,
      json_extract(c.meta_json, '$.retweet_count')                AS retweet_count,
      json_extract(c.meta_json, '$.author_verified')              AS author_verified,
      json_extract(c.meta_json, '$.spread_src')                   AS spread_src,
      c.deleted_at                                                AS deleted_at,
      json_extract(c.meta_json, '$.text_src')                     AS text_src,
      c.author_handle                                             AS author_handle
    FROM content c
    LEFT JOIN content_latest cl ON cl.content_id = c.id
    WHERE c.platform = 'x'
    """,
    """
    CREATE VIEW IF NOT EXISTS v_x_scores AS
    SELECT
      sc.external_id                                              AS tweet_id,
      sc.story_id                                                 AS story_id,
      sc.computed_at                                              AS computed_at,
      sc.significance                                             AS significance,
      sc.branch                                                   AS branch,
      sc.metrics_missing                                          AS metrics_missing,
      sc.metrics_at                                               AS metrics_at,
      sc.metrics_age_hours                                        AS metrics_age_hours,
      json_extract(sc.axes_json, '$.likes_at_6h')                 AS likes_at_6h,
      json_extract(sc.axes_json, '$.replies_at_6h')               AS replies_at_6h,
      sc.engagement                                               AS engagement,
      sc.velocity                                                 AS velocity,
      sc.xconf                                                    AS xconf,
      sc.spread                                                   AS spread,
      json_extract(sc.axes_json, '$.score_engage')                AS score_engage,
      json_extract(sc.axes_json, '$.score_spread')                AS score_spread,
      json_extract(sc.axes_json, '$.score_first')                 AS score_first
    FROM score sc
    WHERE sc.platform = 'x'
    """,
    """
    CREATE VIEW IF NOT EXISTS v_tg_posts AS
    SELECT
      c.id                                                        AS id,
      c.source_id                                                 AS channel_id,
      CAST(substr(c.external_id, instr(c.external_id, '/') + 1) AS INTEGER) AS message_id,
      c.published_at                                              AS date_utc,
      c.text                                                      AS text,
      c.text_hash                                                 AS text_hash,
      cl.views                                                    AS views,
      cl.forwards                                                 AS forwards,
      cl.reactions                                                AS reactions,
      c.media_kind                                                AS media_kind,
      c.links                                                     AS links,
      c.hashtags                                                  AS hashtags,
      c.mentions                                                  AS mentions,
      json_extract(c.meta_json, '$.fwd_from')                     AS fwd_from,
      c.is_repost                                                 AS is_forward,
      json_extract(c.meta_json, '$.has_own_media')                AS has_own_media,
      c.is_promo                                                  AS is_ad,
      c.first_seen_at                                             AS first_seen_at,
      json_extract(c.meta_json, '$.views_checked_at')             AS views_checked_at
    FROM content c
    LEFT JOIN content_latest cl ON cl.content_id = c.id
    WHERE c.platform = 'telegram'
    """,
    """
    CREATE VIEW IF NOT EXISTS v_tg_scores AS
    SELECT
      sc.content_id                                               AS post_id,
      json_extract(sc.axes_json, '$.er')                          AS er,
      json_extract(sc.axes_json, '$.eng_channel')                 AS eng_channel,
      json_extract(sc.axes_json, '$.eng_global')                  AS eng_global,
      sc.engagement                                               AS eng,
      sc.xconf                                                    AS xconf,
      json_extract(sc.axes_json, '$.wsrc')                        AS wsrc,
      json_extract(sc.axes_json, '$.dup_penalty')                 AS dup_penalty,
      json_extract(sc.axes_json, '$.fr')                          AS fr,
      json_extract(sc.axes_json, '$.topic_weight')                AS topic_weight,
      json_extract(sc.axes_json, '$.age_days')                    AS age_days,
      sc.decay                                                    AS decay,
      sc.significance                                             AS significance,
      sc.anomaly                                                  AS anomaly,
      sc.computed_at                                              AS computed_at
    FROM score sc
    WHERE sc.platform = 'telegram'
    """,
]

# ---------------------------------------------------------------------------
# Денормализация идентификаторов платформы (ТЗ-2c, долг D-22).
#
# Представления совместимости читают `external_id` платформы и `platform`
# НАПРЯМУЮ из таблицы данных, без join к `content`. Только однотабличное
# представление SQLite разворачивает (flatten) на правой стороне `LEFT JOIN`;
# многотабличное материализуется целиком на каждый такой join — это и был
# долг D-22 (`MATERIALIZE snapshots` = 283 мс вместо `SEARCH` = 50 мс).
#
# Значения поддерживаются триггерами на таблицах ядра, поэтому любой путь
# записи (адаптер, миграция, будущие X/TG) получает их автоматически, а не
# по договорённости. Триггеры идемпотентны: заполняют колонку только если она
# ещё NULL, и срабатывают при смене `content_id`.
# ---------------------------------------------------------------------------

#: Версия денормализации. Меняется, когда нужно перезалить значения
#: (например, добавился новый столбец) — тогда `migrate_schema` прогонит
#: бэкфилл ещё раз.
DENORM_VERSION = "1"

#: Версия бэкфилла ``content.url`` (долг D-45). Меняется, когда правила синтеза
#: ссылки меняются настолько, что нужно перезалить уже заполненные строки.
URL_BACKFILL_VERSION = "1"

#: Таблицы ядра, отдающие представления совместимости через `content_id`.
DENORM_TABLES: tuple[str, ...] = (
    "content_latest",
    "metric_snapshot",
    "classification",
    "score",
    "seo_field",
    "thumbnail_vision",
    "content_comment",
    "comment_check",
)


def _denorm_child_triggers() -> list[str]:
    out: list[str] = []
    for table in DENORM_TABLES:
        # Таблица в `ON` — через `main.`: имя ``thumbnail_vision`` совпадает с
        # TEMP-представлением адаптера YouTube, и без квалификации триггер
        # создался бы на представлении. В теле триггера имена таблиц
        # разрешаются в схеме самого триггера (main), поэтому DML без `main.`.
        out.append(f"""
        CREATE TRIGGER IF NOT EXISTS trg_{table}_denorm_ins
        AFTER INSERT ON main.{table}
        WHEN NEW.content_id IS NOT NULL AND NEW.external_id IS NULL
        BEGIN
          UPDATE {table}
             SET platform = (SELECT platform FROM content WHERE id = NEW.content_id),
                 external_id = (SELECT external_id FROM content WHERE id = NEW.content_id)
           WHERE rowid = NEW.rowid;
        END
        """)
        out.append(f"""
        CREATE TRIGGER IF NOT EXISTS trg_{table}_denorm_upd
        AFTER UPDATE OF content_id ON main.{table}
        WHEN NEW.content_id IS NOT NULL AND NEW.content_id IS NOT OLD.content_id
        BEGIN
          UPDATE {table}
             SET platform = (SELECT platform FROM content WHERE id = NEW.content_id),
                 external_id = (SELECT external_id FROM content WHERE id = NEW.content_id)
           WHERE rowid = NEW.rowid;
        END
        """)
    return out


_TRIGGERS: list[str] = [
    # content.source_external_id ← source.external_id (для представления videos).
    """
    CREATE TRIGGER IF NOT EXISTS trg_content_source_ext_ins
    AFTER INSERT ON main.content
    WHEN NEW.source_id IS NOT NULL AND NEW.source_external_id IS NULL
    BEGIN
      UPDATE content
         SET source_external_id = (SELECT external_id FROM source WHERE id = NEW.source_id)
       WHERE id = NEW.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_content_source_ext_upd
    AFTER UPDATE OF source_id ON main.content
    WHEN NEW.source_id IS NOT OLD.source_id
    BEGIN
      UPDATE content
         SET source_external_id = (SELECT external_id FROM source WHERE id = NEW.source_id)
       WHERE id = NEW.id;
    END
    """,
    # Канал переименовался (external_id сменился) — догнать денормализацию.
    """
    CREATE TRIGGER IF NOT EXISTS trg_source_ext_upd
    AFTER UPDATE OF external_id ON main.source
    WHEN NEW.external_id IS NOT OLD.external_id
    BEGIN
      UPDATE content SET source_external_id = NEW.external_id WHERE source_id = NEW.id;
    END
    """,
    # Видео/пост получил новый external_id — догнать дочерние таблицы.
    """
    CREATE TRIGGER IF NOT EXISTS trg_content_ext_upd
    AFTER UPDATE OF external_id, platform ON main.content
    WHEN NEW.external_id IS NOT OLD.external_id OR NEW.platform IS NOT OLD.platform
    BEGIN
      UPDATE content_latest SET platform = NEW.platform, external_id = NEW.external_id
       WHERE content_id = NEW.id;
      UPDATE metric_snapshot SET platform = NEW.platform, external_id = NEW.external_id
       WHERE content_id = NEW.id;
      UPDATE classification SET platform = NEW.platform, external_id = NEW.external_id
       WHERE content_id = NEW.id;
      UPDATE score SET platform = NEW.platform, external_id = NEW.external_id
       WHERE content_id = NEW.id;
      UPDATE seo_field SET platform = NEW.platform, external_id = NEW.external_id
       WHERE content_id = NEW.id;
      UPDATE thumbnail_vision SET platform = NEW.platform, external_id = NEW.external_id
       WHERE content_id = NEW.id;
      UPDATE content_comment SET platform = NEW.platform, external_id = NEW.external_id
       WHERE content_id = NEW.id;
      UPDATE comment_check SET platform = NEW.platform, external_id = NEW.external_id
       WHERE content_id = NEW.id;
    END
    """,
] + _denorm_child_triggers()

CORE_STATEMENTS: list[str] = _TABLES + _VIEWS + _TRIGGERS

PLATFORM_SEED: list[tuple[str, str]] = [
    ("youtube", "YouTube"),
    ("x", "X (Twitter)"),
    ("telegram", "Telegram"),
    # Сквозной сюжет (связывание материалов разных платформ, ТЗ «сквозной
    # сюжет»): не отдельная платформа-источник, а признак строки ``story``,
    # собранной из ``content`` нескольких платформ. Строка добавляется
    # идемпотентно (``ON CONFLICT DO UPDATE`` в :func:`init_schema`), поэтому
    # существующие значения ``platform`` не переписываются.
    ("cross", "Сквозной сюжет (несколько платформ)"),
    # ТЗ-8 Р3.3: веб-фид/RSS — четвёртый тип цели реестра. Ребро на домен
    # (``edge.target_platform='web'``) порождает кандидата ``platform='web'``,
    # а прошедший верификацию фид — источник с ``handle=<домен>``.
    ("web", "Веб-фид/RSS"),
]


def init_schema(conn: sqlite3.Connection) -> None:
    """Создать всю схему и заполнить ``platform``/``schema_meta``."""
    for stmt in CORE_STATEMENTS:
        conn.execute(stmt)
    for code, title in PLATFORM_SEED:
        conn.execute(
            "INSERT INTO platform(code, title) VALUES (?, ?) "
            "ON CONFLICT(code) DO UPDATE SET title=excluded.title",
            (code, title),
        )
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SCHEMA_VERSION,),
    )


# Аддитивные колонки, которых не было в версии 1 схемы. Домиграция на уже
# заполненную базу добавляет их через ``ALTER TABLE ... ADD COLUMN`` (с проверкой
# наличия), не пересоздавая таблицы — см. TECH-DEBT D-01/D-03/D-06.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "classification": (
        ("title_ru", "TEXT"),
        ("summary_ru", "TEXT"),
        ("reason", "TEXT"),
    ),
    "candidate": (
        ("display_handle", "TEXT"),
        ("meta_json", "TEXT"),
    ),
    # Денормализация идентификаторов платформы (ТЗ-2c, долг D-22).
    "content": (("source_external_id", "TEXT"),),
    "content_latest": (("platform", "TEXT"), ("external_id", "TEXT")),
    # Дельта по ответам/репостам (ТЗ-43): YouTube-путь писал только
    # views/likes/comments; X и Telegram заполняют ещё replies/reposts.
    "metric_snapshot": (("platform", "TEXT"), ("external_id", "TEXT"),
                        ("delta_replies", "INTEGER"), ("delta_reposts", "INTEGER")),
    "classification": (
        ("platform", "TEXT"),
        ("external_id", "TEXT"),
        ("title_ru", "TEXT"),
        ("summary_ru", "TEXT"),
        ("reason", "TEXT"),
    ),
    "score": (("platform", "TEXT"), ("external_id", "TEXT")),
    "classify_cache": (("meta_json", "TEXT"), ("title_ru", "TEXT"),
                       ("summary_ru", "TEXT")),
    "story_member": (("handle", "TEXT"),),
    "seo_field": (("platform", "TEXT"), ("external_id", "TEXT")),
    "thumbnail_vision": (("platform", "TEXT"), ("external_id", "TEXT")),
    "content_comment": (("external_id", "TEXT"),),
    "comment_check": (("platform", "TEXT"), ("external_id", "TEXT")),
    # Принадлежность платформе у журнала прогонов (ТЗ-3b, долг D-24): аддитивная
    # колонка, чтобы строки X/Telegram различались без догадки по ``run``.
    "run_log": (("platform", "TEXT"),),
    # Различимое состояние стадии очереди замеров (ТЗ-53, долг D-67).
    "metric_schedule": (("status", "TEXT"),),
}


def _table_info(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """``{имя колонки: объявленный тип}`` для таблицы (пусто, если её нет)."""
    try:
        rows = conn.execute(f"PRAGMA main.table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {r[1]: (r[2] or "").upper() for r in rows}


def ensure_columns(conn: sqlite3.Connection) -> list[str]:
    """Добавить недостающие аддитивные колонки. Возвращает список добавленных."""
    added: list[str] = []
    for table, columns in _ADDED_COLUMNS.items():
        present = _table_info(conn, table)
        if not present:
            continue
        for name, decl in columns:
            if name in present:
                continue
            conn.execute(f"ALTER TABLE main.{table} ADD COLUMN {name} {decl}")
            added.append(f"{table}.{name}")
    return added


def ensure_legacy_map_target_id_text(conn: sqlite3.Connection) -> bool:
    """Пересобрать ``legacy_map``, если ``target_id`` всё ещё INTEGER (D-02).

    Возвращает ``True``, если пересборка выполнена. SQLite не умеет менять тип
    существующей колонки, поэтому таблица пересоздаётся и данные копируются
    один-в-один.
    """
    info = _table_info(conn, "legacy_map")
    if not info:
        return False
    if info.get("target_id") == "TEXT":
        return False
    conn.execute("ALTER TABLE legacy_map RENAME TO legacy_map_old_int")
    conn.execute(LEGACY_MAP_DDL)
    conn.execute(
        "INSERT INTO legacy_map "
        "(legacy_db, legacy_table, legacy_id, target_table, target_id, migrated_at) "
        "SELECT legacy_db, legacy_table, legacy_id, target_table, target_id, migrated_at "
        "FROM legacy_map_old_int"
    )
    conn.execute("DROP TABLE legacy_map_old_int")
    return True


def migrate_schema(conn: sqlite3.Connection, *,
                   backfill_url: bool = False) -> dict[str, object]:
    """Идемпотентно привести существующую базу к текущей версии схемы.

    Порядок: домигрировать аддитивные колонки → создать недостающие
    таблицы/индексы/представления/триггеры (:func:`init_schema`) → при
    необходимости сменить тип ``legacy_map.target_id`` → дозаполнить
    денормализацию (ТЗ-2c). Повторный запуск ничего не меняет.

    ``backfill_url=True`` дополнительно дозаполняет ``content.url`` (D-45,
    ТЗ-6). Флаг ЯВНЫЙ и по умолчанию выключен: ``store.connect()`` зовёт
    ``migrate_schema`` на каждом соединении, в том числе из ЖИВЫХ заданий
    расписания, и табличный ``UPDATE`` по всему ``content`` не должен
    срабатывать как побочный эффект обычного сбора. Бэкфилл запускают
    осознанно — ``tuber migrate`` (перенос) или ``tuber db backfill-urls``.

    Колонки добавляются ДО ``init_schema``: индексы и триггеры денормализации
    (ТЗ-2c) ссылаются на ``content.source_external_id`` и ``*.external_id``, и на
    старой базе этих колонок ещё нет. На пустой базе ``ensure_columns`` не находит
    таблиц и ничего не делает — их создаёт ``init_schema`` уже с новыми колонками.
    """
    before = schema_objects(conn)
    added = ensure_columns(conn)
    init_schema(conn)
    # ТЗ-3d: снести лишние адаптерные индексы на таблицах ядра. ПОСЛЕ
    # init_schema — покрывающий ядровой индекс к этому моменту уже создан.
    dropped_indexes = drop_redundant_indexes(conn)
    rebuilt_legacy_map = ensure_legacy_map_target_id_text(conn)
    denormalized = ensure_denormalized(conn)
    run_log_platform = backfill_run_log_platform(conn)
    # ТЗ-53 (D-67): дозаполнить состояние стадий очереди замеров. Идемпотентно.
    schedule_status = backfill_metric_schedule_status(conn)
    # D-45: заполнение content.url — только по явному запросу (см. докстроку).
    url_backfill = ensure_content_url(conn) if backfill_url else {}
    after = schema_objects(conn)
    return {
        "added_columns": added,
        "dropped_indexes": dropped_indexes,
        # Фактически появившиеся объекты (для сводки ``migrate --schema-only``):
        # не догадка по DDL, а diff ``sqlite_master`` до/после. Автоиндексы
        # ``UNIQUE``/``PK`` новые таблицы приносят с собой, но в счёт «созданных
        # индексов» не идут — иначе число не совпало бы с числом индексов,
        # которые реально создаёт прогон.
        "created_tables": sorted(after["tables"] - before["tables"]),
        "created_indexes": sorted(
            n for n in after["indexes"] - before["indexes"]
            if not n.startswith("sqlite_autoindex")),
        "created_triggers": sorted(after["triggers"] - before["triggers"]),
        "legacy_map_rebuilt": rebuilt_legacy_map,
        "denormalized": denormalized,
        "run_log_platform": run_log_platform,
        "schedule_status": schedule_status,
        "url_backfill": url_backfill,
    }


# ``legacy_map.legacy_db`` → код платформы ядра (см. backfill_run_log_platform).


def backfill_run_log_platform(conn: sqlite3.Connection) -> dict[str, int]:
    """Заполнить ``run_log.platform`` у уже существующих строк (ТЗ-3b, D-24).

    Порядок и обоснование способа для строк БЕЗ связи с прогоном
    (``run_id IS NULL`` — их писал плоский код X вне прогона: предупреждения
    брокера, потолки, cooldown):

    1. **по ``run_id``** — строка внутри прогона: берём ``run.platform``
       напрямую (точная связь);
    2. **по ``legacy_map``** — провенанс переноса знает, из какой legacy-базы
       пришла строка (``legacy_map(legacy_table='run_log').legacy_db``), и это
       надёжнее догадки по времени: строка могла быть записана между прогонами;
    3. остаток, который не покрыт ни тем, ни другим, честно остаётся ``NULL``
       (возвращается числом ``remaining``) — не выдумываем платформу.

    Идемпотентно: второй вызов меняет 0 строк (условие ``platform IS NULL``).
    """
    counts = {"total": 0, "by_run": 0, "by_legacy_map": 0, "remaining": 0}
    if not _table_info(conn, "run_log"):
        return counts
    counts["by_run"] = conn.execute(
        "UPDATE run_log SET platform = "
        "  (SELECT r.platform FROM run r WHERE r.id = run_log.run_id) "
        " WHERE platform IS NULL AND run_id IS NOT NULL"
    ).rowcount
    counts["by_legacy_map"] = conn.execute(
        "UPDATE run_log SET platform = ("
        "  SELECT CASE lm.legacy_db WHEN 'x' THEN 'x' WHEN 'tg' THEN 'telegram'"
        "         WHEN 'os' THEN 'youtube' ELSE lm.legacy_db END"
        "  FROM legacy_map lm WHERE lm.target_table='run_log'"
        "    AND lm.target_id = CAST(run_log.id AS TEXT) LIMIT 1) "
        " WHERE platform IS NULL AND EXISTS ("
        "  SELECT 1 FROM legacy_map lm WHERE lm.target_table='run_log'"
        "    AND lm.target_id = CAST(run_log.id AS TEXT))"
    ).rowcount
    counts["total"] = conn.execute("SELECT COUNT(*) FROM run_log").fetchone()[0]
    counts["remaining"] = conn.execute(
        "SELECT COUNT(*) FROM run_log WHERE platform IS NULL").fetchone()[0]
    return counts


def backfill_metric_schedule_status(conn: sqlite3.Connection) -> int:
    """Дозаполнить ``metric_schedule.status`` (ТЗ-53, D-67). Идемпотентно.

    Заполняет только строки с пустым статусом — после первого прогона второй
    вызов возвращает 0. Значения совпадают с семантикой :mod:`tuber.core.metrics`:

    * ``pending`` — ``done_at IS NULL`` (замер ещё не сделан);
    * ``measured`` — есть снимок со стадией в ``bucket`` (замер сделан по данным);
    * ``closed_no_data`` — ``done_at`` стоит, но снимка со стадией нет.

    Выполняется автоматически в :func:`migrate_schema` при каждом соединении —
    ручного шага не требует, на большом ``metric_schedule`` трогает лишь NULL-строки.
    """
    if "status" not in _table_info(conn, "metric_schedule"):
        return 0
    cur = conn.execute(
        """
        UPDATE metric_schedule SET status = CASE
          WHEN done_at IS NULL THEN 'pending'
          WHEN EXISTS (SELECT 1 FROM metric_snapshot m
                        WHERE m.content_id = metric_schedule.content_id
                          AND m.bucket = metric_schedule.stage) THEN 'measured'
          ELSE 'closed_no_data'
        END
        WHERE status IS NULL OR status = ''
        """
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# ТЗ-3d: гигиена индексов на таблицах ядра
# ---------------------------------------------------------------------------

#: Индексы, которые платформенные адаптеры исторически создавали на таблицах
#: ЯДРА, в обход ядрового DDL (ТЗ-2c/ТЗ-3). Каждый — точный дубль, строгий
#: префикс-дубль или заведомо покрыт ядровым индексом, то есть либо бесполезен,
#: либо (в случае ``idx_x_metric_ext``) делает план отчёта YouTube
#: патологическим. Удаляются идемпотентно на каждом ``migrate_schema``.
#:
#: Почему это ДАННЫЙ список, а не эвристика: часть индексов совпадает не с
#: ядровым индексом, а с автоиндексом ``UNIQUE``/``PRIMARY KEY`` (он неудаляем),
#: поэтому механический «префикс любого индекса» тут не подходит — он задел бы
#: и ядровые индексы из спецификации (например ``idx_metric_content`` дублирует
#: автоиндекс ``UNIQUE(content_id, captured_at)``, но имя закреплено ТЗ-1).
#: Список закрыт тестом ``tests/core/test_index_hygiene.py``: он ловит как
#: возврат этих индексов адаптерами, так и появление НОВЫХ дублей.
REDUNDANT_ADAPTER_INDEXES: tuple[str, ...] = (
    # строгий префикс ядрового idx_metric_ext (platform, external_id, captured_at);
    # перехватывает план: планировщик берёт idx_x_metric_ext вместо ядрового
    # idx_metric_ext (замеры приёмки — 16–20 с на всех доступных копиях,
    # деградации не измерено).
    "idx_x_metric_ext",
    # точный дубль ядрового idx_source_ext (см. DDL ядра выше); создавали и X,
    # и YouTube — каждый свой.
    "idx_x_source_ext",
    "idx_youtube_source_ext",
    # точный дубль автоиндекса UNIQUE(platform, handle) таблицы source.
    "idx_x_source_handle",
    # строгий префикс автоиндекса PRIMARY KEY(story_id, content_id) таблицы story_member.
    "idx_x_member_story",
    # перекрытие: ядровой idx_score_sig(significance) + фильтр platform='x'
    # селективнее, чем (platform, significance) — платформа одна на адаптер.
    "idx_x_score_sig",
    # перекрытие: ядровой idx_content_hash(text_hash) даёт точное попадание,
    # платформа при этом лишь отсеивается.
    "idx_x_content_hash",
    # перекрытие: ядровой idx_content_pub(platform, published_at) держит нужный
    # порядок (равенство + диапазон); is_repost остаётся фильтром.
    "idx_x_content_pub",
    # перекрытие: ядровой idx_content_author(author_handle) (легаси-запросы по
    # автору идут через ``lower(COALESCE(author_handle, owner_handle, ''))`` и
    # индекс не используют вовсе).
    "idx_x_content_author",
)


def drop_redundant_indexes(conn: sqlite3.Connection) -> list[str]:
    """Удалить лишние адаптерные индексы на таблицах ядра (ТЗ-3d).

    Идемпотентно: если индекса нет — ничего не делает. Возвращает имена
    реально удалённых индексов (пусто — база уже чистая). Вызывается из
    :func:`migrate_schema` после :func:`init_schema`, чтобы к моменту удаления
    покрывающий ядровой индекс уже существовал.
    """
    present = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    dropped: list[str] = []
    for name in REDUNDANT_ADAPTER_INDEXES:
        if name in present:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
            dropped.append(name)
    return dropped


def index_columns(conn: sqlite3.Connection, name: str) -> tuple[str, ...]:
    """Колонки индекса ``name`` в виде кортежа (выражения — отдельными тегами).

    ``PRAGMA index_info`` отдаёт ``NULL`` для выражений, из-за чего ЛЮБЫЕ два
    expression-индекса выглядели бы одинаково и детектор считал бы их дублем.
    Здесь используется ``index_xinfo`` + хеш определения индекса: одинаковые
    определения совпадают (это правда дубль), разные — различаются.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    sql = (row[0] if row else "") or ""
    expr_tag = "<expr:%s>" % hashlib.sha1(sql.encode("utf-8")).hexdigest()[:8]
    out: list[str] = []
    for seqno, _cid, cname, _desc, _coll, key in conn.execute(
            f"PRAGMA main.index_xinfo({name})").fetchall():
        if not key:  # rowid-колонки и прочий хвост — не часть ключа
            continue
        out.append(cname if cname is not None else f"{expr_tag}:{seqno}")
    return tuple(out)


def table_indexes(
    conn: sqlite3.Connection,
    table: str,
    *,
    include_auto: bool = True,
) -> dict[str, tuple[str, ...]]:
    """Индексы таблицы: ``{имя: колонки}``.

    ``include_auto=True`` добавляет автоиндексы ``UNIQUE``/``PRIMARY KEY`` — они
    неудаляемы и обязаны учитываться при проверке «а нет ли уже эквивалента».
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
    ).fetchall()
    out: dict[str, tuple[str, ...]] = {}
    for (name,) in rows:
        if not include_auto and name.startswith("sqlite_autoindex"):
            continue
        cols = index_columns(conn, name)
        if cols:
            out[name] = cols
    return out


def duplicate_index_report(conn: sqlite3.Connection) -> list[str]:
    """Найти дубли и префикс-дубли среди ЯВНЫХ индексов таблиц ядра.

    Сравниваются только явно созданные (именованные) индексы: автоиндексы
    ``UNIQUE``/``PRIMARY KEY`` удалить нельзя, и ядровой DDL спецификации
    (ТЗ-1) сознательно держит рядом с ними ``idx_metric_content``.

    Возвращает список человекочитаемых описаний (пусто — чисто). Используется
    тестом-детектором: любая НОВАЯ платформа, создавшая дубль, попадёт сюда.
    """
    by_table: dict[str, dict[str, tuple[str, ...]]] = {}
    for table in REQUIRED_TABLES:
        explicit = table_indexes(conn, table, include_auto=False)
        if explicit:
            by_table[table] = explicit

    problems: list[str] = []
    for table, indexes in sorted(by_table.items()):
        names = sorted(indexes)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                ca, cb = indexes[a], indexes[b]
                if ca == cb:
                    problems.append(f"{table}: {a} и {b} — точный дубль {list(ca)}")
                elif len(ca) < len(cb) and cb[:len(ca)] == ca:
                    problems.append(f"{table}: {a} {list(ca)} — префикс {b} {list(cb)}")
                elif len(cb) < len(ca) and ca[:len(cb)] == cb:
                    problems.append(f"{table}: {b} {list(cb)} — префикс {a} {list(ca)}")
    return problems


# ---------------------------------------------------------------------------
# ТЗ-3d: правило владения индексами на таблицах ядра
# ---------------------------------------------------------------------------

class DuplicateIndexError(RuntimeError):
    """Индекс адаптера на таблице ядра дублирует (или префиксует) существующий.

    Это ошибка ОКРУЖЕНИЯ, а не стиля: лишний узкий индекс перехватывает план —
    планировщик берёт ``idx_x_metric_ext`` вместо покрывающего ядрового
    ``idx_metric_ext`` (замеры приёмки — 16–20 с на всех доступных копиях,
    деградации не измерено). Поэтому попытка создать такой индекс обязана быть
    ГРОМКОЙ, а не молчаливой: см. :func:`ensure_adapter_index`.
    """


def index_conflict(
    indexes: dict[str, tuple[str, ...]],
    columns: tuple[str, ...],
    *,
    exclude: str | None = None,
) -> tuple[str, str] | None:
    """Есть ли среди ``indexes`` дубль/префикс-дубль для ``columns``.

    Возвращает ``(вид, имя)``. Виды (важно: сравнение идёт по ЗАПРАШИВАЕМЫМ
    колонкам ``columns``, существующий индекс — ``existing``):

    * ``exact`` — колонки совпадают;
    * ``prefix`` — запрашиваемые колонки являются строгим префиксом
      существующего индекса, то есть существующий ПОКРЫВАЕТ наш запрос, а наш
      индекс был бы лишним (это дефект ТЗ-3d: ``(platform, external_id)`` при
      живом ``(platform, external_id, captured_at)``);
    * ``superset`` — запрашиваемый индекс шире существующего (существующий —
      его префикс), то есть наш индекс делает существующий лишним.

    ``exclude`` — имя индекса, который не нужно учитывать (обычно это тот, что
    мы и собираемся создавать).
    """
    cols = tuple(columns)
    for name, existing in indexes.items():
        if name == exclude:
            continue
        if existing == cols:
            return ("exact", name)
        if len(cols) < len(existing) and existing[:len(cols)] == cols:
            return ("prefix", name)
        if len(existing) < len(cols) and cols[:len(existing)] == existing:
            return ("superset", name)
    return None


def adapter_index_sql(name: str, table: str, columns: tuple[str, ...]) -> str:
    """DDL индекса адаптера (колонки — уже готовые SQL-выражения)."""
    return f"CREATE INDEX IF NOT EXISTS {name} ON {table}({', '.join(columns)})"


def ensure_adapter_index(
    conn: sqlite3.Connection,
    name: str,
    table: str,
    columns: tuple[str, ...],
    *,
    apply: bool = True,
) -> bool:
    """Создать индекс адаптера, не породив дубль на таблице ядра (ТЗ-3d).

    Правило владения: индекс на таблице ядра заводит либо ЯДРО, либо адаптер —
    и адаптер только тогда, когда эквивалента у ядра нет. Поведение:

    * колонки совпадают с существующим ЯВНЫМ индексом ядра → индекс НЕ
      создаётся, возвращается ``False``: это и есть «брать дубли у ядра»;
    * колонки — строгий префикс существующего явного индекса или наоборот →
      бросается :class:`DuplicateIndexError` с указанием обоих индексов (молча
      такая попытка не проходит — ни у новой платформы, ни у старой при
      регрессии);
    * автоиндексы ``UNIQUE``/``PRIMARY KEY``: эквивалент или наш индекс — строгий
      префикс автоиндекса → ``False`` (автоиндекс неудаляем и уже держит этот
      путь; узкий индекс рядом с ним — мёртвый груз). Обратный случай (наш индекс
      шире автоиндекса, т.е. покрывающий) — создаём: он реально полезен;
    * на таблице, которой нет в ядре, проверок нет — это платформенная таблица;
    * иначе индекс создаётся и возвращается ``True``.

    ``apply=False`` — только проверка, без создания (используется тестами и
    приёмочными сценариями на read-only базе).
    """
    if table in REQUIRED_TABLES:
        cols = tuple(columns)
        explicit = table_indexes(conn, table, include_auto=False)
        conflict = index_conflict(explicit, cols, exclude=name)
        if conflict is not None:
            kind, other = conflict
            if kind == "exact":
                return False
            raise DuplicateIndexError(
                f"{name} {list(cols)} на таблице ядра {table!r} "
                + ("перекрыт" if kind == "prefix" else "перекрывает")
                + f" индекс {other} {list(explicit[other])}; "
                "ядро уже держит этот access-path — уберите индекс из адаптера "
                "или расширьте ядровой (ТЗ-3d, долг D-33)"
            )
        auto = {n: c for n, c in table_indexes(conn, table).items()
                if n.startswith("sqlite_autoindex")}
        auto_conflict = index_conflict(auto, cols, exclude=name)
        if auto_conflict is not None and auto_conflict[0] in ("exact", "prefix"):
            # Автоиндекс ``UNIQUE``/``PRIMARY KEY`` удалить нельзя и он ПОКРЫВАЕТ
            # наш запрос — отдельный индекс рядом с ним мёртвый груз.
            # Обратный случай (наш индекс шире автоиндекса, т.е. покрывающий)
            # создаётся: он реально полезен, а автоиндекс остаётся неудаляемым.
            return False
    if not apply:
        return False
    conn.execute(adapter_index_sql(name, table, tuple(columns)))
    return True


def _denorm_marker(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='denorm_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row[0] if row is not None else None


def backfill_denormalized(conn: sqlite3.Connection) -> dict[str, int]:
    """Заполнить денормализованные колонки уже существующих строк.

    Возвращает ``{объект: сколько строк изменено}``. Каждое действие — один
    ``UPDATE``; повторный вызов на дозаполненной базе меняет 0 строк (условия
    ``IS NOT`` ложны, когда значение уже совпадает). Сироты (``content_id``
    без ``content``) не трогаются: скалярный подзапрос даёт NULL, а
    ``NULL IS NOT NULL`` = ложь.
    """
    counts: dict[str, int] = {}
    cur = conn.execute(
        """
        UPDATE content
           SET source_external_id = (SELECT external_id FROM source WHERE id = content.source_id)
         WHERE source_id IS NOT NULL
           AND source_external_id IS NOT (SELECT external_id FROM source WHERE id = content.source_id)
        """
    )
    counts["content.source_external_id"] = cur.rowcount
    for table in DENORM_TABLES:
        cur = conn.execute(
            f"""
            UPDATE main.{table}
               SET platform = (SELECT platform FROM content WHERE id = {table}.content_id),
                   external_id = (SELECT external_id FROM content WHERE id = {table}.content_id)
             WHERE content_id IS NOT NULL
               AND (platform IS NOT (SELECT platform FROM content WHERE id = {table}.content_id)
                    OR external_id IS NOT (SELECT external_id FROM content WHERE id = {table}.content_id))
            """
        )
        counts[table] = cur.rowcount
    return counts


def ensure_denormalized(conn: sqlite3.Connection) -> dict[str, int]:
    """Дозаполнить денормализацию один раз на базу (ТЗ-2c, долг D-22).

    Идемпотентно и дёшево: если в ``schema_meta`` стоит текущая
    :data:`DENORM_VERSION`, работа не делается. Так ``store.connect()``
    (он зовёт :func:`migrate_schema` на каждом соединении) не гоняет
    табличный ``UPDATE``. Возвращает счётчики бэкфилла (пусто — не требовалось).
    """
    if _denorm_marker(conn) == DENORM_VERSION:
        return {}
    counts = backfill_denormalized(conn)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('denorm_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (DENORM_VERSION,),
    )
    return counts


# ---------------------------------------------------------------------------
# D-45: заполнение content.url прямой ссылкой
# ---------------------------------------------------------------------------

def backfill_content_url(conn: sqlite3.Connection) -> dict[str, int]:
    """Заполнить ``content.url`` у строк, где он пуст, а компоненты известны.

    Правила синтеза — в :func:`tuber.core.urls.content_url` (одно место на весь
    репозиторий). Handle берётся из ``content.author_handle``, иначе из
    ``source.handle`` (X/Telegram). Строки, у которых компонента нет, остаются
    ``NULL`` — ссылка не выдумывается.

    Идемпотентно: второй вызов меняет 0 строк, потому что после первого в
    ``content.url`` уже стоит значение, а ``WHERE url IS NULL`` ложно.
    """
    counts = {"youtube": 0, "x": 0, "telegram": 0}
    rows = conn.execute(
        """
        SELECT c.id, c.platform, c.external_id,
               COALESCE(NULLIF(TRIM(c.author_handle), ''), s.handle) AS handle
        FROM content c
        LEFT JOIN source s ON s.id = c.source_id
        WHERE c.url IS NULL OR c.url = ''
        """
    ).fetchall()
    for row in rows:
        link = urls.content_url(row["platform"], row["external_id"], row["handle"])
        if not link:
            continue
        conn.execute("UPDATE content SET url=? WHERE id=?", (link, row["id"]))
        counts[row["platform"]] = counts.get(row["platform"], 0) + 1
    counts["filled"] = sum(v for k, v in counts.items() if k != "filled")
    counts["remaining"] = conn.execute(
        "SELECT COUNT(*) FROM content WHERE url IS NULL OR url=''").fetchone()[0]
    return counts


def ensure_content_url(conn: sqlite3.Connection) -> dict[str, int]:
    """Один раз на базу дозаполнить ``content.url`` (долг D-45, ТЗ-6 §3).

    Гейт по ``schema_meta.url_version``: ``store.connect()`` зовёт
    :func:`migrate_schema` на каждом соединении, и без гейта полный проход по
    ``content`` шёл бы каждый раз. Возвращает счётчики бэкфилла (пусто — не
    требовалось).
    """
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='url_version'").fetchone()
    if row is not None and row[0] == URL_BACKFILL_VERSION:
        return {}
    counts = backfill_content_url(conn)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('url_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (URL_BACKFILL_VERSION,),
    )
    return counts


# ---------------------------------------------------------------------------
# Список обязательных объектов — используется тестами и инструментом схемы.
# ---------------------------------------------------------------------------

REQUIRED_TABLES: tuple[str, ...] = (
    "schema_meta", "platform", "source", "source_baseline", "content", "metric_snapshot",
    "content_latest", "classification", "classify_cache", "score", "story", "story_member",
    "candidate", "quota_usage", "transport_instance", "transport_request",
    "transport_account_state", "run", "run_log", "metrics_daily", "llm_usage", "topic",
    "report_text", "blocklist", "seo_field", "thumbnail_vision", "content_comment",
    "comment_check", "legacy_map", "cursor", "classify_daily", "edge",
    "first_mover", "source_metric_history", "novelty", "metric_schedule",
)

REQUIRED_INDEXES: tuple[str, ...] = (
    "idx_content_pub", "idx_content_source", "idx_content_hash", "idx_content_author",
    "idx_content_source_ext", "idx_source_ext",
    "idx_metric_content", "idx_metric_ext",
    "idx_score_sig", "idx_score_ext",
    "idx_classification_ext", "idx_seo_field_ext", "idx_thumbnail_vision_ext",
    "idx_comment_check_ext",
    "idx_candidate_status", "idx_treq_host_ts", "idx_run_log_run", "idx_run_log_platform",
    "idx_edge_target", "idx_edge_from",
    "idx_first_mover_indeg",
    "idx_smh_day",
    "idx_novelty_day",
    "idx_metric_schedule_due",
)

REQUIRED_TRIGGERS: tuple[str, ...] = tuple(
    f"trg_{table}_denorm_{kind}"
    for table in DENORM_TABLES
    for kind in ("ins", "upd")
) + (
    "trg_content_source_ext_ins", "trg_content_source_ext_upd",
    "trg_source_ext_upd", "trg_content_ext_upd",
)

REQUIRED_VIEWS: tuple[str, ...] = (
    "v_score_current", "v_os_videos", "v_os_snapshots", "v_x_posts", "v_x_scores",
    "v_tg_posts", "v_tg_scores",
)


def verify_schema(conn: sqlite3.Connection) -> list[str]:
    """Проверить, что схема ядра полна. Возвращает список проблем.

    Пустой список — всё на месте. Используется тестами (в том числе
    негативным: удалили колонку — проверка это видит).
    """
    problems: list[str] = []

    present_tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    present_indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    present_views = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()
    }
    present_triggers = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }

    for table in REQUIRED_TABLES:
        if table not in present_tables:
            problems.append(f"нет таблицы: {table}")
    for index in REQUIRED_INDEXES:
        if index not in present_indexes:
            problems.append(f"нет индекса: {index}")
    for view in REQUIRED_VIEWS:
        if view not in present_views:
            problems.append(f"нет представления: {view}")
    for trigger in REQUIRED_TRIGGERS:
        if trigger not in present_triggers:
            problems.append(f"нет триггера: {trigger}")

    for table, cols in required_columns().items():
        if table not in present_tables:
            continue
        actual = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})").fetchall()]
        for col in cols:
            if col not in actual:
                problems.append(f"{table}: нет колонки {col}")
    return problems


def schema_objects(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Фактические объекты схемы (для диагностики)."""
    return {
        "tables": {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()},
        "indexes": {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()},
        "views": {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'").fetchall()},
        "triggers": {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()},
    }


def required_columns() -> dict[str, tuple[str, ...]]:
    """Ожидаемые колонки для ключевых таблиц (для теста схемы)."""
    return {
        "source": (
            "id", "platform", "external_id", "handle", "title", "url", "lang", "country",
            "topic_guess", "is_author", "status", "tier", "subs", "subs_at", "posts_per_day",
            "avg_views", "vr", "ai_density", "cv_interval", "link_ratio", "rt_ratio",
            "dup_ratio", "first_mover_score", "posts_collected", "read_mode", "antifraud_flag",
            "flood_until", "fail_streak", "last_error", "cursor", "added_at", "added_by",
            "source_kind", "notes", "verified_at", "checked_at", "first_seen_at",
            "last_synced_at", "meta_json",
        ),
        "content": (
            "id", "platform", "source_id", "source_external_id", "external_id", "kind", "url",
            "title", "text",
            "text_hash", "lang", "author_handle", "published_at", "duration_seconds",
            "category", "media_kind", "is_repost", "is_quote", "is_reply", "is_promo",
            "is_short", "links", "mentions", "hashtags", "first_seen_at", "last_seen_at",
            "deleted_at", "meta_json",
        ),
        "metric_snapshot": (
            "id", "content_id", "platform", "external_id", "captured_at", "bucket", "source",
            "age_hours",
            "interval_seconds", "interval_quality", "views", "likes", "comments", "replies",
            "reposts", "forwards", "reactions", "quotes", "delta_views", "delta_likes",
            "delta_comments", "delta_replies", "delta_reposts",
            "views_per_day", "views_per_hour", "is_anomaly", "raw_json",
        ),
        "score": (
            "content_id", "platform", "external_id", "computed_at", "significance", "branch",
            "engagement", "velocity",
            "spread", "first_mover", "decay", "freshness", "xconf", "anomaly",
            "metrics_missing", "metrics_at", "metrics_age_hours", "axes_json", "parts_json",
            "story_id",
        ),
        "candidate": (
            "id", "platform", "kind", "handle", "external_id", "score_priority", "found_via",
            "found_in_handle", "found_in_source_id", "seen_count", "distinct_sources",
            "sources_json", "display_handle", "meta_json", "first_seen_at", "last_seen_at",
            "validated", "reject_reason", "llm_checked", "ai_hint", "spam", "lang_guess",
            "rubric", "promoted_by", "promoted_at", "status",
        ),
        "classification": (
            "content_id", "platform", "external_id", "is_ai", "topic", "subtopic",
            "claim_type", "source_type",
            "entity_tier", "novelty", "lang", "confidence", "method", "model", "prompt_ver",
            "status", "attempts", "error", "prompt_tokens", "completion_tokens", "cost_usd",
            "classified_at", "title_ru", "summary_ru", "reason",
        ),
        "content_latest": (
            "content_id", "platform", "external_id", "captured_at", "views", "likes",
            "comments", "replies", "reposts", "forwards", "reactions",
            "views_per_day", "views_per_hour",
        ),
        "seo_field": (
            "content_id", "platform", "external_id",
        ),
        "thumbnail_vision": (
            "id", "content_id", "platform", "external_id", "model", "prompt_version",
            "description_raw", "extracted_text", "cost_usd", "latency_ms", "created_at",
        ),
        "content_comment": (
            "comment_id", "content_id", "platform", "external_id", "author", "text", "likes",
            "published_at", "captured_at",
        ),
        "comment_check": (
            "content_id", "platform", "external_id", "checked_at", "status", "error",
        ),
        "cursor": (
            "platform", "kind", "ref", "cursor", "last_page_at", "pages_total",
            "items_total", "updated_at", "meta_json",
        ),
        "classify_daily": (
            "day", "platform", "posts", "model_calls", "failed", "prompt_tokens",
            "completion_tokens", "cost_usd",
        ),
        "story": (
            "id", "platform", "created_at", "window_hours", "threshold", "title", "topic",
            "canonical_content_id", "first_content_id", "first_mover_source_id", "first_pub_at",
            "last_pub_at", "source_count", "content_count", "xconf", "lead_time_min",
            "topics", "entities", "is_new_entity", "is_single", "suspect", "claimed_at",
            "similarity_threshold", "significance", "rank_score",
        ),
        "legacy_map": (
            "legacy_db", "legacy_table", "legacy_id", "target_table", "target_id", "migrated_at",
        ),
        "source_metric_history": (
            "source_id", "day", "subs", "posts_7d", "median_likes_24h", "viral_posts_14d",
            "trusted_indegree_30d", "lead_time_median", "captured_at",
        ),
        "metric_schedule": (
            "content_id", "platform", "stage", "due_at", "done_at", "attempt", "status",
        ),
        "run": (
            "id", "platform", "started_at", "finished_at", "mode", "ok_count", "fail_count",
            "items_new", "items_upd", "errors", "note",
        ),
        "run_log": (
            "id", "run_id", "platform", "ts", "level", "ref", "msg",
        ),
        "metrics_daily": (
            "day", "platform", "items_ingested", "dup_rate", "coverage", "fail_rate",
        ),
        "edge": (
            "id", "from_platform", "from_source_id", "from_content_id", "from_handle",
            "kind", "target_type", "target_platform", "target_value", "target_url",
            "weight", "evidence", "origin", "competitor", "first_seen_at",
            "last_seen_at", "seen_count",
        ),
        "first_mover": (
            "source_id", "platform", "handle", "first_moves", "stories",
            "first_mover_share", "lead_time_median", "lead_time_posts",
            "trusted_indegree_30d", "computed_at",
        ),
    }
