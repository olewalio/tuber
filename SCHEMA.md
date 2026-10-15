# Единая схема ядра `tuber` и карта legacy → core

Волна **ТЗ-1**. Здесь описан фундамент: единая схема, в которой данные всех трёх
платформ лежат в **общем ядре**, а не в трёх параллельных наборах таблиц.
DDL — в `tuber/core/schema.py` (единственный источник истины; имена таблиц,
колонок, индексов и представлений из ТЗ не переименовываются).

## 1. Инварианты

- **Даты.** Все даты в ядре — `TEXT` ISO-8601 UTC вида `YYYY-MM-DD HH:MM:SS`.
  Legacy-разнородность (epoch в `tuber-os`, ISO-строки в `tuber-x`/`tuber-telegram`)
  снимается при миграции через `tuber/core/timeutil.py` (`parse_any`,
  `epoch_to_iso`, `iso_now`). Все конвертации идут только через него.
- **PRAGMA.** `journal_mode=WAL`, `busy_timeout=15000`, `synchronous=NORMAL`,
  `foreign_keys=ON` (см. `tuber/core/db.py`).
- **Версия схемы.** В таблице `schema_meta` ключ `version` (сейчас `1`).
- **Идентичность.**
  - `source` уникален по `(platform, handle)`.
  - `content` уникален по `(platform, external_id)`.
    - YouTube: `external_id = video_id`;
    - X: `external_id = tweet_id`;
    - Telegram: `external_id = <handle>/<message_id>` — **составной**, потому что
      `message_id` уникален только внутри канала (проверено: 14 877 постов, но
      всего 6 939 различных `message_id`).
  - `candidate` уникален по `(platform, handle)`; `handle` — КЛЮЧ (`channel_id`
    для YouTube-каналов, `handle` для X/Telegram), а человекочитаемое имя — в
    `display_handle` (см. §5 и D-01).
  - `cursor` уникален по `(platform, kind, ref)`; `classify_daily` — по `(day, platform)`.
  - `score` — по `(content_id, computed_at)`; `metric_snapshot` — по
    `(content_id, captured_at)`.

## 2. Общие таблицы ядра

| Таблица | Назначение |
|---|---|
| `schema_meta` | версия схемы и прочие мета-ключи |
| `platform` | справочник платформ: `youtube`, `x`, `telegram` |
| `source` | канал/аккаунт/лента (объединяет `channels`(os) + `accounts`(x) + `channels`(tg)) |
| `source_baseline` | базовые медианы по источнику (обобщение `channel_baselines`(tg)) |
| `content` | единый элемент контента: видео/шортс/пост/сообщение |
| `metric_snapshot` | ряд замеров метрик (объединяет `snapshots`(os) + `post_metrics_history`(x)) |
| `content_latest` | денормализованное «последнее известное» состояние (производное) |
| `classification` | результат классификации по контенту |
| `classify_cache` | кэш классификации по `text_hash` (межплатформенный) |
| `score` | единая оценка; платформенные оси — в `axes_json` |
| `story`, `story_member` | сюжеты и их участники |
| `candidate` | единый пул кандидатов (канон вместо JSONL-моста, ТЗ-5) |
| `quota_usage` | расход квот API (обобщение `quota_log`(os)) |
| `cursor` | курсоры пагинации по `(platform, kind, ref)` — канонический источник курсоров (D-05) |
| `classify_daily` | дневная статистика LLM-классификации (X `classify_daily`, D-04) |
| `transport_instance`, `transport_request`, `transport_account_state` | транспортный слой (X-инстансы, запросы, состояние MTProto) |
| `run`, `run_log` | журналы прогонов |
| `metrics_daily` | дневная панель качества |
| `llm_usage` | расход LLM |
| `topic`, `report_text`, `blocklist` | справочник тем, кэш текстов отчётов, блок-лист |
| `seo_field`, `thumbnail_vision`, `content_comment`, `comment_check` | платформенные расширения OS (ключ — `content_id`) |
| `legacy_map` | карта переноса: `(legacy_db, legacy_table, legacy_id)` → `(target_table, target_id)` |

## 3. Представления

| Представление | Смысл |
|---|---|
| `v_score_current` | текущая (последняя) оценка по каждому контенту |
| `v_os_videos` | совместимость с `tuber-os.videos` (колонки как в legacy, данные из ядра) |
| `v_os_snapshots` | совместимость с `tuber-os.snapshots` |
| `v_x_posts` | совместимость с `tuber-x.posts` |
| `v_x_scores` | совместимость с `tuber-x.scores` |
| `v_tg_posts` | совместимость с `tuber-telegram.posts` |
| `v_tg_scores` | совместимость с `tuber-telegram.scores` |

В представлениях «эпоховые» поля (`published_at`, `captured_at`, `first_seen`) отдаются
обратно в epoch (`strftime('%s', ...)`), чтобы старый код читал их как раньше.

## 4. Карта legacy → core (обязательный раздел)

Колонка «примечание» фиксирует решения, где маппинг не однозначен.

### 4.1 `tuber-os` (`legacy_db = os`)

| legacy-таблица | target | примечание |
|---|---|---|
| `channels` | `source` (`platform='youtube'`) | `channel_id→external_id`, `handle` (пустой → `channel_id`), `subscriber_count→subs`, `default_language→lang`, `first_seen→first_seen_at`, `last_synced_at→last_synced_at`; `video_count`, `view_count`, `topic_categories`, `uploads_playlist_id`, `is_russian` → `meta_json` |
| `videos` | `content` (`kind∈{video,short}`) | `video_id→external_id`; `is_shorts→is_short` и `kind`; `description→text`; `published_at/first_seen/last_seen` epoch→ISO; `tags`, `thumbnail_*`, `live_broadcast`, `caption_available`, `primary_topic`, `topic_confidence` → `meta_json` |
| `snapshots` | `metric_snapshot` | `captured_at` epoch→ISO, `prev_captured_at` → `raw_json`; `(video_id,captured_at)` уникальны |
| `video_scores` | `score` | `significance=COALESCE(viral_index,outlier_score)`; `outlier_score`, `vpd`, `vpd_ratio`, `likes_per_1000`, `comments_per_1000`, `comment_velocity`, `packaging_score`, `viral_index` → `axes_json`; `score_parts→parts_json` |
| `video_classification` | `classification` | `topic=''` → `NULL`; `title_ru`, `summary_ru`, `reason` переносятся один-в-один в одноимённые колонки (D-03 закрыт) |
| `seo_fields` | `seo_field` | `video_id→content_id`, остальные 27 колонок как есть |
| `thumbnail_vision` | `thumbnail_vision` | `video_id→content_id`, `created_at` epoch→ISO |
| `video_comments` | `content_comment` | `video_id→content_id`, `platform='youtube'` |
| `comment_checks` | `comment_check` | `video_id→content_id` |
| `channel_candidates` | `candidate` (`kind='channel'`) | `handle` целевого ключа = `channel_id`; человекочитаемый `handle` → `display_handle` (нормализован, без `@`, пусто → NULL); `source→found_via`, `mentions→seen_count`, `score→score_priority`, `discovered_at→first_seen_at`, `probed_at→last_seen_at`; `title/description/evidence/subscriber_count/resolve_attempts/probed_at` → `meta_json` (D-01/D-06 закрыты) |
| `query_candidates` | `candidate` (`kind='query'`) | `query→handle/external_id`; `source→found_via`, `hits→seen_count`, `score→score_priority`; `evidence/kind/runs/accepted/fail_count/last_fail_at` → `meta_json` |
| `quota_log` | `quota_usage` | агрегируется по `(key_id, day, endpoint)` (`day` = дата из epoch), `SUM(calls)`/`SUM(units)`; `project`, `ts` → колонки |
| `llm_usage` | `llm_usage` (`platform='youtube'`) | `created_at` epoch→ISO |
| `topics` | `topic` (`platform='youtube'`) | |

### 4.2 `tuber-x` (`legacy_db = x`)

| legacy-таблица | target | примечание |
|---|---|---|
| `accounts` | `source` (`platform='x'`) | `x_id→external_id`, `tier/status/lang/topic_guess/is_author/*_ratio/...` в одноимённые; `source_type→source_kind`; `last_success_at/last_attempt_at/ai_density_src/provisional_since/reject_reason/last_reject_at/promo_path` → `meta_json` |
| `posts` | `content` (`kind='post'`) | `author_handle` → `content.author_handle` (реальный автор); `source_id` — владелец ленты через `account_id` (**не автор**); `is_retweet/is_quote/is_reply→is_repost/is_quote/is_reply`; `owner_handle/orig_handle/published_src/metrics_*/pinned/retweet_count/author_verified/spread_src/text_src` → `meta_json`; метрики `likes/replies/metrics_at` дополнительно пишутся в `content_latest` |
| `post_metrics_history` | `metric_snapshot` | `taken_at→captured_at`, `src→source` |
| `scores` | `score` (`platform='x'`) | `significance/branch/engagement/velocity/spread/xconf/metrics_*/story_id` в колонки; `likes_at_6h/replies_at_6h/score_*` → `axes_json` |
| `stories` | `story` (`platform='x'`) | `first_tweet_id→first_content_id`, `first_mover→first_mover_source_id`, `published_at→first_pub_at`, `post_count→content_count` |
| `story_posts` | `story_member` | `role`, `is_canonical = (role='primary')`; `handle` (автор на момент кластеризации) → `story_member.handle` (аддитивная колонка, ТЗ-3/D-26); `sim` — нет в legacy → NULL |
| `classified` | `classify_cache` | ключ `text_hash`; `tweet_id` → `classify_cache.meta_json` (аддитивная колонка, ТЗ-3); дополнительно проекция в `classification` для постов, найденных в `content` |
| `classify_daily` | `classify_daily` (`platform='x'`) | колонки как в legacy + `platform`; ключ `(day, platform)` (D-04 закрыт) |
| `candidates` | `candidate` (`kind='handle'`) | `handle→handle` (ключ), человекочитаемый handle → `display_handle`; `priority→score_priority`; `feed_source→found_via` при пустом `found_via`; `verified_at`, `sources` → `meta_json`; `found_in_account→found_in_handle`; `validated→validated`, `reject_reason→reject_reason` (D-06 закрыт) |
| `cursors` | `cursor` (+ `source.cursor` для `kind='account'`) | переносятся ВСЕ курсоры (`account` и `search`); канонический источник — `cursor(platform,kind,ref)`, `source.cursor` оставлен для совместимости (D-05 закрыт) |
| `instances` | `transport_instance` (`platform='x'`) | `collect_fail_streak`, `reserve_since` → `meta_json` |
| `requests` | `transport_request` (`platform='x'`) | `run_id` резолвится через `legacy_map` |
| `runs` | `run` (`platform='x'`) | `accounts_ok→ok_count`, `posts_new→items_new` и т.д. |
| `run_log` | `run_log` | `handle→ref` |
| `metrics_daily` | `metrics_daily` (`platform='x'`) | `cdn_429_count/synd_429_count/ssr_used/stale_lag_p95_min` → `extra_json` |
| `report_texts` | `report_text` | |
| `blocklist` | `blocklist` (`platform='x'`) | |
| `darks` | `source.meta_json.darks` | `status` источника **не меняется** |

### 4.3 `tuber-telegram` (`legacy_db = tg`)

| legacy-таблица | target | примечание |
|---|---|---|
| `channels` | `source` (`platform='telegram'`) | `tg_id→external_id`, `posts_7d` и `last_post_at` → `meta_json` (в ядре `posts_per_day` семантически иное — не подставляем); `source→source_kind` |
| `posts` | `content` (`kind='post'`) + `metric_snapshot` | `external_id = <handle>/<message_id>`; `is_forward→is_repost`, `is_ad→is_promo`; снапшот: `views/forwards/reactions`, `captured_at = views_checked_at`, иначе `first_seen_at` (столбцы просмотров могут быть NULL — 256 таких постов) |
| `scores` | `score` (`platform='telegram'`) | `eng→engagement`, `significance/decay/xconf/anomaly` в колонки; `er/eng_channel/eng_global/wsrc/dup_penalty/fr/topic_weight/age_days` → `axes_json` |
| `channel_baselines` | `source_baseline` | `posts_in_window→items_in_window`, `hashed_posts→hashed_items`, `dup_posts→dup_items`; `median_likes` в legacy нет → NULL |
| `classified` | `classification` | по `post_id`→`content_id` (в базе 0 строк) |
| `stories` | `story` (`platform='telegram'`) | `canonical_post_id→canonical_content_id`, `channel_count→source_count` (0 строк) |
| `story_members` | `story_member` | по `post_id`→`content_id` (0 строк) |
| `account_state` | `transport_account_state` (`platform='telegram'`) | |
| `runs` | `run` (`platform='telegram'`) | `channels_ok→ok_count` |
| `run_log` | `run_log` | `handle→ref` |
| `metrics_daily` | `metrics_daily` (`platform='telegram'`) | `coverage_est→coverage`, `latency_p90_min→latency_p95_min`, `enrich_cov→enriched_ratio` |

## 5. `legacy_map`: как читать

Каждая переехавшая строка получает запись `(legacy_db, legacy_table, legacy_id)
→ (target_table, target_id)`. `target_id` объявлен `TEXT` (D-02 закрыт): для
целочисленных целей в нём лежит число строкой, для составных/текстовых ключей —
сам ключ.

- Для таблиц с целочисленным PK (`source`, `content`, `story`, `run`,
  `metric_snapshot`, `thumbnail_vision`, `llm_usage`, `transport_request`) `target_id` —
  настоящий новый id.
- Для таблиц с составным/текстовым ключом (`seo_field`, `content_comment`,
  `comment_check`, `source_baseline`, `classification`, `score`, `candidate`,
  `topic`, `blocklist`, `report_text`, `metrics_daily`, `transport_instance`,
  `transport_account_state`, `cursor`, `classify_daily`) `target_id` — ближайший
  целочисленный id (`content_id`/`source_id`) либо составной ключ строкой.
  `storage.get_legacy_map` возвращает числовые значения как `int`, а составные —
  как строку.
- `quota_log` агрегируется many→one: каждой legacy-строке соответствует агрегат,
  а `target_id` — реальный составной ключ `platform|key_id|day|endpoint` (D-02).
- `cursor`: `target_id` — `platform|kind|ref`.
- Одна legacy-строка может дать более одной целевой (например `x.posts` →
  `content` + `content_latest`, `tg.posts` → `content` + `metric_snapshot`,
  `x.cursors(kind='account')` → `cursor` + `source.cursor`). В `legacy_map`
  пишется основная цель; производные записи идут по своим естественным ключам.

## 6. Человекочитаемый handle кандидата (`display_handle`)

Единственное каноническое место человекочитаемого handle кандидата —
`candidate.display_handle` (нормализовано: ведущие `@` сняты, пустое → `NULL`).
`candidate.handle` — КЛЮЧ пула (`channel_id` для YouTube-каналов, `handle` для
X/Telegram). `found_in_handle` сохраняет своё исходное значение «в каком handle
кандидат найден» (для X — `found_in_account`) и человекочитаемым handle больше
не дублируется; для YouTube-каналов он `NULL` (D-01).

## 7. Что ядро НЕ получает из legacy (и почему)

Ни одно значение не выдумывается: если поля нет в legacy — остаётся `NULL`.
Расхождения, которые нельзя закрыть, перечислены в `TECH-DEBT.md`.

Долги D-01..D-06 закрыты в ТЗ-1b:
`title_ru`/`summary_ru`/`reason` (D-03), `cursor` (D-05), `classify_daily` (D-04),
`display_handle` (D-01), `candidate.meta_json` (D-06), `legacy_map.target_id` TEXT (D-02),
`classify_cache.meta_json` и `story_member.handle` (ТЗ-3, перенос X).
