"""Синтетические legacy-базы для тестов миграции (ТЗ-1 §7).

Схемы взяты из живых legacy-баз (той же формы, что в проде), но строк мало и
они подобраны осознанно, включая битые случаи:

* ``os.snapshots`` со ссылкой на несуществующее видео (сирота);
* ``x.posts.owner_handle`` ≠ реального автора (важно: ``source_id`` — владелец
  ленты, а ``content.author_handle`` — реальный автор);
* ``tg.posts`` без просмотров;
* кандидат OS с пустым handle и дублирующимся handle.

Функция :func:`build_legacy` создаёт три файла и возвращает пути. При
``with_broken=False`` битые строки не добавляются — такой набор нужен для
проверки нулевого parity.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# DDL (форма legacy-баз)
# ---------------------------------------------------------------------------

OS_DDL = """
CREATE TABLE channels (
    channel_id TEXT PRIMARY KEY, title TEXT, handle TEXT, subscriber_count INTEGER,
    video_count INTEGER, view_count INTEGER, country TEXT, default_language TEXT,
    topic_categories TEXT, uploads_playlist_id TEXT, is_russian INTEGER,
    first_seen INTEGER, last_synced_at INTEGER
);
CREATE TABLE videos (
    video_id TEXT PRIMARY KEY, channel_id TEXT REFERENCES channels(channel_id),
    title TEXT, description TEXT, tags TEXT, category_id INTEGER, default_language TEXT,
    duration_seconds INTEGER, is_shorts INTEGER, published_at INTEGER, thumbnail_url TEXT,
    thumbnail_width INTEGER, thumbnail_height INTEGER, thumbnail_checked_at INTEGER,
    live_broadcast TEXT, caption_available INTEGER, primary_topic TEXT, topic_confidence REAL,
    first_seen INTEGER, last_seen INTEGER
);
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL, captured_at INTEGER NOT NULL,
    bucket TEXT NOT NULL, views INTEGER, likes INTEGER, comments INTEGER, prev_captured_at INTEGER,
    interval_seconds INTEGER, delta_views INTEGER, delta_likes INTEGER, delta_comments INTEGER,
    views_per_day REAL, views_per_hour REAL, age_hours INTEGER, source TEXT,
    is_anomaly INTEGER NOT NULL DEFAULT 0, interval_quality TEXT
);
CREATE TABLE video_scores (
    video_id TEXT, computed_at INTEGER, outlier_score REAL, vpd REAL, vpd_ratio REAL,
    likes_per_1000 REAL, comments_per_1000 REAL, comment_velocity REAL, score_parts TEXT,
    packaging_score REAL, viral_index REAL, PRIMARY KEY (video_id, computed_at)
);
CREATE TABLE seo_fields (
    video_id TEXT PRIMARY KEY, title_length INTEGER, title_words INTEGER,
    title_has_number INTEGER, title_top_words TEXT, desc_length INTEGER, tags_count INTEGER,
    published_hour_msk INTEGER, title_matches_topic INTEGER, seo_pattern TEXT
);
CREATE TABLE thumbnail_vision (
    id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT, model TEXT, prompt_version TEXT,
    description_raw TEXT, extracted_text TEXT, cost_usd REAL, latency_ms INTEGER, created_at INTEGER
);
CREATE TABLE topics (name TEXT PRIMARY KEY);
CREATE TABLE video_comments (
    comment_id TEXT PRIMARY KEY, video_id TEXT, author TEXT, text TEXT, likes INTEGER,
    published_at INTEGER, captured_at INTEGER
);
CREATE TABLE quota_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date INTEGER, key_id TEXT, calls INTEGER, units INTEGER,
    endpoint TEXT, project TEXT, ts INTEGER
);
CREATE TABLE llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, model TEXT, tokens_in INTEGER,
    tokens_out INTEGER, cost_usd REAL, created_at INTEGER
);
CREATE TABLE video_classification (
    video_id TEXT PRIMARY KEY, is_ai INTEGER, topic TEXT, confidence REAL, lang TEXT,
    title_ru TEXT, summary_ru TEXT, reason TEXT,
    model TEXT, classified_at INTEGER
);
CREATE TABLE channel_candidates (
    channel_id TEXT PRIMARY KEY, title TEXT, handle TEXT, description TEXT, source TEXT NOT NULL,
    evidence TEXT, mentions INTEGER DEFAULT 1, subscriber_count INTEGER, score REAL,
    status TEXT NOT NULL DEFAULT 'new', reject_reason TEXT, discovered_at TEXT NOT NULL,
    probed_at TEXT, resolve_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE query_candidates (
    query TEXT PRIMARY KEY, source TEXT NOT NULL, evidence TEXT, hits INTEGER DEFAULT 1,
    score REAL, status TEXT NOT NULL DEFAULT 'new', discovered_at TEXT, reject_reason TEXT,
    kind TEXT NOT NULL DEFAULT 'query', runs INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0, last_run_at TEXT, fail_count INTEGER NOT NULL DEFAULT 0,
    last_fail_at INTEGER
);
CREATE TABLE comment_checks (
    video_id TEXT PRIMARY KEY, checked_at INTEGER, status TEXT, error TEXT
);
"""

X_DDL = """
CREATE TABLE accounts (
  id INTEGER PRIMARY KEY, handle TEXT UNIQUE NOT NULL, x_id TEXT, tier TEXT NOT NULL DEFAULT 'C',
  status TEXT NOT NULL DEFAULT 'candidate', lang TEXT, topic_guess TEXT, is_author INTEGER,
  ai_density REAL, cv_interval REAL, posts_per_day REAL, link_ratio REAL, rt_ratio REAL,
  dup_ratio REAL, first_mover_score REAL DEFAULT 0, posts_collected INTEGER DEFAULT 0,
  last_success_at TEXT, last_attempt_at TEXT, fail_streak INTEGER DEFAULT 0, last_error TEXT,
  cursor TEXT, added_at TEXT DEFAULT (datetime('now')), added_by TEXT, source_type TEXT,
  notes TEXT, verified_at TEXT, ai_density_src TEXT, provisional_since TEXT, reject_reason TEXT,
  last_reject_at TEXT, promo_path TEXT
);
CREATE TABLE posts (
  id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  tweet_id TEXT NOT NULL, published_at_utc TEXT NOT NULL, published_src TEXT NOT NULL, text TEXT,
  text_hash TEXT, lang TEXT, links TEXT, mentions TEXT, hashtags TEXT, is_retweet INTEGER DEFAULT 0,
  is_quote INTEGER DEFAULT 0, is_reply INTEGER DEFAULT 0, owner_handle TEXT, orig_handle TEXT,
  media_kind TEXT, first_seen_at TEXT DEFAULT (datetime('now')), likes INTEGER, replies INTEGER,
  has_quote INTEGER, is_long INTEGER, metrics_at TEXT, metrics_src TEXT, pinned INTEGER,
  retweet_count INTEGER, author_verified INTEGER, spread_src TEXT, deleted_at TEXT, text_src TEXT,
  author_handle TEXT, UNIQUE(tweet_id)
);
CREATE TABLE post_metrics_history (
  id INTEGER PRIMARY KEY, tweet_id TEXT NOT NULL, taken_at TEXT NOT NULL, age_hours REAL,
  likes INTEGER, replies INTEGER, src TEXT, UNIQUE(tweet_id, taken_at)
);
CREATE TABLE scores (
  tweet_id TEXT PRIMARY KEY, story_id INTEGER, computed_at TEXT, significance REAL, branch TEXT,
  metrics_missing INTEGER DEFAULT 0, metrics_at TEXT, metrics_age_hours REAL, likes_at_6h INTEGER,
  replies_at_6h INTEGER, engagement REAL, velocity REAL, xconf INTEGER, spread INTEGER,
  score_engage REAL, score_spread REAL, score_first REAL
);
CREATE TABLE stories (
  id INTEGER PRIMARY KEY, created_at TEXT, window_hours INTEGER, threshold INTEGER,
  first_tweet_id TEXT, first_mover TEXT, published_at TEXT, xconf INTEGER DEFAULT 0,
  post_count INTEGER DEFAULT 0, lead_time_min REAL, topics TEXT, entities TEXT,
  is_new_entity INTEGER DEFAULT 0, is_single INTEGER DEFAULT 0, suspect INTEGER DEFAULT 0,
  claimed_at TEXT
);
CREATE TABLE story_posts (
  story_id INTEGER NOT NULL, tweet_id TEXT NOT NULL, handle TEXT, role TEXT, added_at TEXT,
  PRIMARY KEY (story_id, tweet_id)
);
CREATE TABLE classified (
  text_hash TEXT PRIMARY KEY, tweet_id TEXT, is_ai INTEGER, topic TEXT, subtopic TEXT,
  claim_type TEXT, novelty REAL, lang TEXT, status TEXT NOT NULL DEFAULT 'classified',
  method TEXT, model TEXT, attempts INTEGER DEFAULT 0, error TEXT, prompt_tokens INTEGER,
  completion_tokens INTEGER, cost_usd REAL, classified_at TEXT, first_seen_at TEXT
);
CREATE TABLE candidates (
  handle TEXT PRIMARY KEY, found_via TEXT, found_in_account TEXT, seen_count INTEGER DEFAULT 1,
  distinct_sources INTEGER DEFAULT 1, first_seen_at TEXT, last_seen_at TEXT, validated TEXT,
  reject_reason TEXT, llm_checked INTEGER DEFAULT 0, sources TEXT, priority REAL DEFAULT 0,
  rubric TEXT, lang_guess TEXT, spam INTEGER DEFAULT 0, promoted_by TEXT, verified_at TEXT,
  ai_hint INTEGER DEFAULT 0, feed_source TEXT
);
CREATE TABLE instances (
  host TEXT PRIMARY KEY, healthy INTEGER, rss_ok INTEGER, items_last_test INTEGER,
  last_check_at TEXT, fail_streak INTEGER DEFAULT 0, cooldown_until TEXT,
  requests_today INTEGER DEFAULT 0, day TEXT, version TEXT, last_error TEXT,
  rate_limited_429 INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, collect_fail_streak INTEGER DEFAULT 0,
  reserve_since TEXT
);
CREATE TABLE requests (
  id INTEGER PRIMARY KEY, host TEXT, ts TEXT DEFAULT (datetime('now')), kind TEXT, url TEXT,
  status INTEGER, items INTEGER, latency_ms INTEGER, run_id INTEGER
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, accounts_ok INTEGER,
  accounts_fail INTEGER, posts_new INTEGER, posts_upd INTEGER, errors INTEGER, note TEXT
);
CREATE TABLE run_log (
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  ts TEXT DEFAULT (datetime('now')), level TEXT, handle TEXT, msg TEXT
);
CREATE TABLE metrics_daily (
  day TEXT PRIMARY KEY, posts_ingested INTEGER, dup_rate REAL, coverage REAL, fail_rate REAL,
  latency_p95_min REAL, valid_date_ratio REAL, instances_alive INTEGER, likes_median REAL,
  enriched_ratio REAL, cdn_429_count INTEGER, synd_429_count INTEGER, ssr_used INTEGER,
  stale_lag_p95_min REAL
);
CREATE TABLE blocklist (handle TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
CREATE TABLE report_texts (
  text_hash TEXT PRIMARY KEY, ru TEXT, model TEXT, created_at TEXT, src TEXT
);
CREATE TABLE cursors (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, cursor TEXT, last_page_at TEXT,
  pages_total INTEGER DEFAULT 0, items_total INTEGER DEFAULT 0, UNIQUE(kind, ref)
);
CREATE TABLE classify_daily (
  day TEXT PRIMARY KEY, posts INTEGER DEFAULT 0, model_calls INTEGER DEFAULT 0,
  failed INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0
);
CREATE TABLE darks (
  handle TEXT PRIMARY KEY, computed_at TEXT, stories_cur INTEGER DEFAULT 0,
  stories_prev INTEGER DEFAULT 0, growth REAL, in_top INTEGER DEFAULT 0
);
"""

TG_DDL = """
CREATE TABLE channels (
  id INTEGER PRIMARY KEY, handle TEXT UNIQUE NOT NULL, tg_id INTEGER, title TEXT, subs INTEGER,
  subs_at TEXT, last_post_at TEXT, posts_7d REAL, avg_views INTEGER, vr REAL, lang TEXT,
  topic_guess TEXT, status TEXT DEFAULT 'candidate', read_mode TEXT DEFAULT 'web',
  is_author INTEGER DEFAULT 0, antifraud_flag INTEGER DEFAULT 0, flood_until TEXT,
  added_at TEXT DEFAULT (datetime('now')), checked_at TEXT, source TEXT, notes TEXT
);
CREATE TABLE posts (
  id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  message_id INTEGER NOT NULL, date_utc TEXT NOT NULL, text TEXT, text_hash TEXT, views INTEGER,
  forwards INTEGER, reactions INTEGER, media_kind TEXT, links TEXT, hashtags TEXT, mentions TEXT,
  fwd_from TEXT, is_forward INTEGER DEFAULT 0, has_own_media INTEGER DEFAULT 0, is_ad INTEGER DEFAULT 0,
  first_seen_at TEXT DEFAULT (datetime('now')), views_checked_at TEXT, UNIQUE(channel_id, message_id)
);
CREATE TABLE classified (
  post_id INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE, is_ai INTEGER, topic TEXT,
  source_type TEXT, claim_type TEXT, entity_tier TEXT, novelty TEXT, lang TEXT, confidence REAL,
  model TEXT, prompt_ver TEXT, classified_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE stories (
  id INTEGER PRIMARY KEY, canonical_post_id INTEGER, title TEXT, topic TEXT, channel_count INTEGER,
  xconf INTEGER, first_pub_at TEXT, last_pub_at TEXT, significance REAL, rank_score REAL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE story_members (
  story_id INTEGER, post_id INTEGER, sim REAL, is_canonical INTEGER DEFAULT 0, role TEXT,
  PRIMARY KEY (story_id, post_id)
);
CREATE TABLE account_state (
  name TEXT PRIMARY KEY, flood_until TEXT, last_error TEXT, resolves_today INTEGER DEFAULT 0,
  day TEXT, updated_at TEXT
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, channels_ok INTEGER,
  channels_fail INTEGER, posts_new INTEGER, posts_upd INTEGER, errors INTEGER, note TEXT
);
CREATE TABLE run_log (
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE, ts TEXT,
  level TEXT, handle TEXT, msg TEXT
);
CREATE TABLE metrics_daily (
  day TEXT PRIMARY KEY, posts_ingested INTEGER, dup_rate REAL, coverage_est REAL,
  latency_p90_min REAL, enrich_cov REAL, errors INTEGER
);
CREATE TABLE channel_baselines (
  channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE, window_days INTEGER,
  posts_in_window INTEGER, median_views REAL, median_reactions REAL, median_er REAL,
  is_author_data INTEGER, hashed_posts INTEGER, dup_posts INTEGER, dup_ratio REAL, computed_at TEXT
);
CREATE TABLE scores (
  post_id INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE, er REAL,
  eng_channel REAL, eng_global REAL, eng REAL, xconf INTEGER, wsrc REAL, dup_penalty REAL,
  fr REAL, topic_weight REAL, age_days REAL, decay REAL, significance REAL,
  anomaly INTEGER DEFAULT 0, computed_at TEXT
);
"""


def _exec_script(conn: sqlite3.Connection, script: str) -> None:
    conn.executescript(script)


# ---------------------------------------------------------------------------
# Заполнение
# ---------------------------------------------------------------------------

def _seed_os(conn: sqlite3.Connection, broken: bool) -> None:
    conn.executemany(
        "INSERT INTO channels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("UC1", "Chan One", "@chanone", 1000, 2, 5000, "US", "en", "[]", "UU1", 0, 1700000000, 1700000100),
            ("UC2", "Chan Two", "", 500, 1, 100, "RU", "ru", "[]", "UU2", 1, 1700000200, 1700000300),
        ],
    )
    conn.executemany(
        "INSERT INTO videos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("v1", "UC1", "T1", "D1", "[\"a\"]", 22, "en", 100, 0, 1700001000,
             "http://t/1", 1280, 720, 1700001100, "none", 1, "", None, 1700001200, 1700001300),
            ("v2", "UC1", "T2 short", "D2", "[]", 22, "en", 30, 1, 1700001400,
             "http://t/2", 1280, 720, None, "none", 0, None, None, 1700001500, 1700001600),
            ("v3", "UC2", "T3", "D3", "[]", 28, "ru", 200, 0, 1700002000,
             None, None, None, None, None, 0, None, None, 1700002100, 1700002200),
        ],
    )
    snaps = [
        (1, "v1", 1700003000, "d", 100, 10, 1, None, None, None, None, None, None, None, 1, "daily", 0, "first"),
        (2, "v1", 1700006000, "d", 300, 20, 2, 1700003000, 3000, 200, 10, 1, 300.0, 12.5, 2, "daily", 0, "ok"),
        (3, "v2", 1700003100, "h4", 50, 5, 0, None, None, None, None, None, None, None, 1, "fresh", 0, "first"),
    ]
    conn.executemany(
        "INSERT INTO snapshots (id,video_id,captured_at,bucket,views,likes,comments,prev_captured_at,"
        "interval_seconds,delta_views,delta_likes,delta_comments,views_per_day,views_per_hour,"
        "age_hours,source,is_anomaly,interval_quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        snaps,
    )
    if broken:
        # Снапшот без видео — сирота.
        conn.execute(
            "INSERT INTO snapshots (id,video_id,captured_at,bucket,views,likes,comments,age_hours,source,is_anomaly,interval_quality)"
            " VALUES (99,'missing',1700009999,'d',1,1,1,1,'daily',0,'first')"
        )
    conn.executemany(
        "INSERT INTO video_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("v1", 1700007000, 3.5, 100.0, 1.2, 10.0, 1.0, 0.5, '{"title": 30}', 97.8, None),
            ("v2", 1700007000, None, None, None, None, None, None, '{"title": 1}', 10.0, 55.5),
            ("v3", 1700007000, 1.0, 50.0, 0.5, 5.0, 0.2, 0.1, None, 1.0, None),
        ],
    )
    conn.executemany(
        "INSERT INTO video_classification "
        "(video_id,is_ai,topic,confidence,lang,title_ru,summary_ru,reason,model,classified_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("v1", 1, "модели и релизы", 0.9, "en", "Заголовок RU", "Резюме RU", "причина", "deepseek-chat", 1700004000),
            ("v3", 0, "", 0.8, "ru", None, None, "причина v3", "deepseek-chat", 1700004000),
        ],
    )
    conn.executemany(
        "INSERT INTO seo_fields (video_id,title_length,seo_pattern) VALUES (?,?,?)",
        [("v1", 2, "базовый"), ("v3", 2, "базовый")],
    )
    conn.executemany("INSERT INTO topics (name) VALUES (?)", [("модели и релизы",), ("агенты",)])
    conn.executemany(
        "INSERT INTO quota_log (id,date,key_id,calls,units,endpoint,project,ts) VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, 1700000000, "k1", 1, 1, "videos/probe", None, 1700000000),
            (2, 1700000000, "k1", 1, 100, "search", None, 1700000000),
            (3, 1700086400, "k1", 1, 1, "videos/probe", None, 1700086400),
        ],
    )
    conn.executemany(
        "INSERT INTO llm_usage (id,stage,model,tokens_in,tokens_out,cost_usd,created_at) VALUES (?,?,?,?,?,?,?)",
        [(1, "classify", "deepseek-chat", 10, 5, 0.001, 1700004000)],
    )
    conn.executemany(
        "INSERT INTO thumbnail_vision (id,video_id,model,prompt_version,description_raw,extracted_text,cost_usd,latency_ms,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(1, "v1", "kimi", "v2", "desc", "TEXT", 0.002, 100, 1700005000)],
    )
    conn.executemany(
        "INSERT INTO video_comments (comment_id,video_id,author,text,likes,published_at,captured_at)"
        " VALUES (?,?,?,?,?,?,?)",
        [("c1", "v1", "@u", "hi", 5, 1700008000, 1700009000)],
    )
    conn.executemany(
        "INSERT INTO comment_checks (video_id,checked_at,status,error) VALUES (?,?,?,?)",
        [("v1", 1700009000, "ok", None)],
    )
    conn.executemany(
        "INSERT INTO channel_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("UCX", "Resolved", "", "", "mention", "video:v1", 10, None, 7.5, "rejected", "r", "1700000000", "1700000100", 0),
            ("@dupe", "", "dupe", "", "mention", "video:v2", 5, None, 3.0, "new", None, "1700000001", None, 0),
            ("@dupe2", "T", "dupe", "", "mention", "video:v3", 1, None, 1.0, "new", None, "1700000002", None, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO query_candidates (query,source,evidence,hits,score,status,discovered_at,reject_reason,kind,runs,accepted,last_run_at,fail_count,last_fail_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("chart:RU:28", "chart", "chart:RU:28", 1, 0.0, "accepted", "1700000000", None, "chart", 0, 0, None, 0, None)],
    )


def _seed_x(conn: sqlite3.Connection, broken: bool) -> None:
    conn.executemany(
        "INSERT INTO accounts (id,handle,x_id,tier,status,lang,posts_collected,added_at,source_type,verified_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "feedowner", "x1", "A", "active", "en", 2, "1700000000", "seed", "1700000100"),
            (2, "other", "x2", "C", "candidate", "ru", 1, "1700000200", "seed", None),
        ],
    )
    conn.executemany(
        "INSERT INTO posts (id,account_id,tweet_id,published_at_utc,published_src,text,text_hash,lang,"
        "is_retweet,is_quote,is_reply,owner_handle,orig_handle,media_kind,first_seen_at,likes,replies,"
        "has_quote,is_long,metrics_at,metrics_src,author_handle)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # владелец ленты = feedowner, реальный автор поста = realauthor
            (1, 1, "t1", "2026-09-14T10:00:00", "rss", "hello", "h1", "en", 0, 0, 0,
             "someoneelse", None, "photo", "2026-09-14 10:01:00", 10, 2, 0, 0,
             "2026-09-14 11:00:00", "cdn", "realauthor"),
            (2, 1, "t2", "2026-09-14T12:00:00", "rss", "quote msg", "h2", "en", 1, 0, 0,
             None, "orig", "photo", "2026-09-14 12:01:00", 5, 1, 1, 0,
             None, None, "feedowner"),
            (3, 2, "t3", "2026-09-14T13:00:00", "rss", "ru post", "h3", "ru", 0, 0, 1,
             None, None, None, "2026-09-14 13:01:00", 1, 0, 0, 1,
             "2026-09-14 14:00:00", "cdn", "other"),
        ],
    )
    pmh = [
        (1, "t1", "2026-09-14T11:00:00", 1.0, 10, 2, "cdn"),
        (2, "t1", "2026-09-14T12:00:00", 2.0, 20, 3, "cdn"),
    ]
    conn.executemany("INSERT INTO post_metrics_history VALUES (?,?,?,?,?,?,?)", pmh)
    if broken:
        conn.execute(
            "INSERT INTO post_metrics_history VALUES (99,'nope','2026-09-14T12:00:00',1.0,1,1,'cdn')"
        )
    conn.executemany(
        "INSERT INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("t1", 1, "2026-09-14T15:00:00", 5.5, "popularity", 0, "2026-09-14T11:00:00", 1.1,
             10, 2, 100.0, 20.0, 1, 0, 3.0, 0.0, 0.0),
            ("t2", 1, "2026-09-14T15:00:00", 2.0, "popularity", 0, "2026-09-14T12:00:00", 1.0,
             5, 1, 50.0, 10.0, 1, 0, 1.0, 0.0, 0.0),
        ],
    )
    conn.executemany(
        "INSERT INTO stories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(1, "2026-09-15T00:00:00", 72, 12, "t1", "feedowner", "2026-09-14T10:00:00", 1, 2, None,
          '["topic"]', '["ent"]', 0, 0, 0, "2026-09-15T00:00:00")],
    )
    conn.executemany(
        "INSERT INTO story_posts VALUES (?,?,?,?,?)",
        [
            (1, "t1", "feedowner", "primary", "2026-09-15T00:00:00"),
            (1, "t2", "feedowner", "echo", "2026-09-15T00:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO classified (text_hash,tweet_id,is_ai,topic,subtopic,claim_type,novelty,lang,status,method,model,attempts,error,prompt_tokens,completion_tokens,cost_usd,classified_at,first_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("h1", "t1", 1, "релизы", "sub", "claim", 0.5, "en", "classified", "model", "m1", 1, None, 10, 5, 0.01, "2026-09-14T16:00:00", "2026-09-14 16:00:00")],
    )
    conn.executemany(
        "INSERT INTO candidates (handle,found_via,found_in_account,seen_count,distinct_sources,first_seen_at,last_seen_at,validated,reject_reason,llm_checked,sources,priority,rubric,lang_guess,spam,promoted_by,verified_at,ai_hint,feed_source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("candx", None, "acc1", 3, 2, "2026-09-14T00:00:00", "2026-09-14T01:00:00", "provisional", None, 0,
          '["s1","s2"]', 10.0, "релизы", "ru", 0, None, "2026-09-14T02:00:00", 0, "feed:src")],
    )
    conn.executemany(
        "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("https://nitter.example", 1, 1, 20, "2026-09-14T00:00:00", 0, None, 5, "2026-09-14", "v1", None, 0, 0, 0, None)],
    )
    conn.executemany(
        "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?)",
        [(1, "https://nitter.example", "2026-09-14T00:00:00", "health", "u", 200, 20, 100, None)],
    )
    conn.executemany(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(1, "2026-09-14T00:00:00", "2026-09-14T00:01:00", "collect", 1, 0, 2, 0, 0, "n")],
    )
    conn.executemany(
        "INSERT INTO run_log VALUES (?,?,?,?,?,?)",
        [(1, 1, "2026-09-14T00:00:30", "INFO", "feedowner", "msg")],
    )
    conn.executemany(
        "INSERT INTO metrics_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("2026-09-14", 3, 0.0, 1.0, 0.0, 1.0, 1.0, 1, 10.0, 0.5, 0, 0, 0, 1.0)],
    )
    conn.executemany(
        "INSERT INTO report_texts VALUES (?,?,?,?,?)",
        [("rt1", "текст", "m", "2026-09-14T00:00:00", "model")],
    )
    conn.executemany(
        "INSERT INTO cursors VALUES (?,?,?,?,?,?,?)",
        [(1, "account", "feedowner", "CUR1", "2026-09-14T00:00:00", 5, 100),
         (2, "search", "q", "CUR2", "2026-09-14T00:00:00", 1, 10)],
    )
    conn.executemany(
        "INSERT INTO classify_daily VALUES (?,?,?,?,?,?,?)",
        [("2026-09-14", 3, 1, 0, 100, 50, 0.01)],
    )
    conn.executemany(
        "INSERT INTO darks VALUES (?,?,?,?,?,?)",
        [("feedowner", "2026-09-14T00:00:00", 3, 1, 2.0, 0)],
    )


def _seed_tg(conn: sqlite3.Connection, broken: bool) -> None:
    conn.executemany(
        "INSERT INTO channels (id,handle,tg_id,title,subs,subs_at,last_post_at,posts_7d,avg_views,vr,lang,topic_guess,status,read_mode,is_author,antifraud_flag,flood_until,added_at,checked_at,source,notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "chan1", 111, "Chan 1", 1000, "2026-09-14", "2026-09-14 17:35", 9.0, 500, 0.1,
             "RU", None, "active", "web", 1, 0, None, "2026-09-14 17:44:52", "2026-09-16 17:30:39", "list", "note"),
            (2, "chan2", 222, "Chan 2", 500, "2026-09-14", "2026-09-13 09:16", 0.1, 100, None,
             "RU", None, "candidate", "web", None, 0, None, "2026-09-14 17:44:52", "2026-09-16 17:30:43", "list", None),
        ],
    )
    posts = [
        # message_id=100 повторяется в разных каналах — проверка составного
        # внешнего ключа Telegram (<handle>/<message_id>).
        (1, 1, 100, "2026-09-13 05:40:11", "p1", "th1", 1000, 5, 10, "video", "[]", "[]", "[]", None, 0, 1, 0,
         "2026-09-14 17:46:39", "2026-09-15 18:00:39"),
        (2, 1, 101, "2026-09-13 09:10:18", "p2", "th2", 2000, 3, 4, "photo", "[]", "[]", "[]", None, 1, 1, 1,
         "2026-09-14 17:46:39", None),
        (3, 2, 100, "2026-09-13 10:00:00", "p3", "th3", None, None, None, None, None, None, None, None, 0, 0, 0,
         "2026-09-14 17:50:00", None),
    ]
    conn.executemany(
        "INSERT INTO posts (id,channel_id,message_id,date_utc,text,text_hash,views,forwards,reactions,"
        "media_kind,links,hashtags,mentions,fwd_from,is_forward,has_own_media,is_ad,first_seen_at,views_checked_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        posts,
    )
    if broken:
        conn.execute(
            "INSERT INTO posts (id,channel_id,message_id,date_utc,text,text_hash,views,first_seen_at)"
            " VALUES (99, 999, 1, '2026-09-13 11:00:00', 'orphan', 'th99', 1, '2026-09-14 18:00:00')"
        )
    conn.executemany(
        "INSERT INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, 0.003, 0.63, 0.33, 0.46, 1, 1.0, 0.0, 1.0, 1.0, 3.0, 0.93, 0.43, 0, "2026-09-16 07:44:22"),
            (2, 0.004, 0.78, 0.41, 0.56, 1, 1.0, 0.0, 1.0, 1.0, 2.9, 0.93, 0.53, 0, "2026-09-16 07:44:22"),
        ],
    )
    conn.executemany(
        "INSERT INTO channel_baselines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(1, 90, 68, 29150.0, 124.5, 0.005, 1, 40, 0, 0.0, "2026-09-16 07:44:22")],
    )
    conn.executemany(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(1, "2026-09-14 17:48:23", "2026-09-14 17:48:23", "web", 1, 0, 3, 0, 0, "n")],
    )
    conn.executemany(
        "INSERT INTO run_log VALUES (?,?,?,?,?,?)",
        [(1, 1, "2026-09-14 17:48:23", "INFO", "chan1", "msg")],
    )


def build_legacy(directory: str | Path, *, with_broken: bool = True) -> dict[str, str]:
    """Создать три синтетические legacy-базы и вернуть пути к ним."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "os": str(directory / "os.db"),
        "x": str(directory / "x.db"),
        "tg": str(directory / "tg.db"),
    }
    os_conn = sqlite3.connect(paths["os"])
    try:
        _exec_script(os_conn, OS_DDL)
        _seed_os(os_conn, with_broken)
        os_conn.commit()
    finally:
        os_conn.close()

    x_conn = sqlite3.connect(paths["x"])
    try:
        _exec_script(x_conn, X_DDL)
        _seed_x(x_conn, with_broken)
        x_conn.commit()
    finally:
        x_conn.close()

    tg_conn = sqlite3.connect(paths["tg"])
    try:
        _exec_script(tg_conn, TG_DDL)
        _seed_tg(tg_conn, with_broken)
        tg_conn.commit()
    finally:
        tg_conn.close()
    return paths
