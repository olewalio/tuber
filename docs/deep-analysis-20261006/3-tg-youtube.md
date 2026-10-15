# 3. Платформы Telegram и YouTube

Глубокий анализ монорепозитория Tuber. Дата: 06.10.2026.
Метод: только чтение кода; база `/root/tuber/data/tuber.db` — read-only (`sqlite3 -readonly`).
Все цифры получены реальными командами; источники кода указаны как `путь:строка`.

---

## 1. Инвентаризация модулей

### 1.1 Telegram — `tuber/platforms/telegram/` (21 модуль, ≈9163 строк)

| Модуль | Строк | Байт | Назначение |
|---|---:|---:|---|
| `collect.py` | 1335 | 57711 | Сборщик постов. Режимы: `web` (t.me/s + пагинация `?before=`), `mtproto` (Telethon, инкремент), `resolve` (кандидаты через `get_entity` + `GetFullChannelRequest`). Предохранители: дедлайн, backoff 403/429, `flood_until`, ≤20 резолвов/сутки (`collect.py:1-12`). |
| `scoring.py` | 1356 | 70818 | Скоринг значимости. Собирает `posts.forwards` (бэкфилл), `channel_baselines` (медианы), `scores`. Формула `Significance = Fr × Wsrc × Xconf × Eng × (1−DupPenalty) × TopicWeight`, распад `decay=0.5**(age/half_life)` (`scoring.py:13-16`, `:213-220`). `Eng = sqrt(eng_channel × eng_global)` — геометрическое среднее двух баз (`docs/tg/docs/DESIGN.md:186-196`). |
| `store.py` | 1330 | 66114 | Адаптер хранения Telegram к единому ядру (`source/content/metric_snapshot/score`). |
| `classify.py` | 991 | 43885 | Смысловая классификация постов DeepSeek (ИИ-тематика, `title_ru`/`summary_ru`), батч 20, лимиты `TG_CLASSIFY_DAILY_CAP` (150) и `TG_CLASSIFY_USD_CAP` (0.40). |
| `comments.py` | 623 | 30837 | Тексты ответов через Telethon (`iter_messages(reply_to=...)` у САМОГО канала, не у группы — D-64); пишет в `content_comment` (`comments.py:1-30`). |
| `ads.py` | 519 | 21808 | Семантический детектор рекламы DeepSeek (D-37, ТЗ-B11): кэш `ad_cache` по `text_hash`, батч 20 (`ads.py:1-23`). |
| `exact_subs.py` | 461 | 21105 | Точные подписчики MTProto: `get_entity` + `GetFullChannelRequest` → `participants_count` (D-62/D-68), ≤1000/прогон, пауза 1.5 с (`exact_subs.py:1-27`). |
| `promote.py` | 401 | 19321 | Повышение кандидата → активный источник (ТЗ-51): измеримое правило, `vr` по фактическим постам (D-54/D-55) (`promote.py:1-30`). |
| `followers.py` | 376 | 16231 | Точные подписчики из превью `https://t.me/<handle>` (БЕЗ `/s/`): `tgme_page_extra`. Исходы `value`/`no_counter`/`gone`; округлённое значение ленты `/s/` НЕ пишется (ТЗ-45) (`followers.py:1-27`). |
| `bridge.py` | 345 | 14613 | Общий мост источников (ТЗ-17): import/export кандидатов, нормализация хендлов, извлечение t.me/x.com/youtube id. |
| `prelim.py` | 199 | 9419 | Исторический разовый разбор t.me/s (ТЗ-1), пишет через адаптер `store`. |
| `feeds_export.py` | 196 | 8568 | Экспорт кандидатов X/YouTube в фид соседних платформ. |
| `config.py` | 188 | 13052 | Пути, лимиты, защита боёвки (`--allow-production`), `PROMOTE_*` (`config.py:90-98`), `EXACT_SUBS_*` (`:110-113`). |
| `linking.py` | 179 | 6838 | Канонизация хендла (`@X`/`t.me/x`/`x/s`→`x`) + ключ `tg_id`; связь очередь ↔ реестр. |
| `cli.py` | 158 | 7263 | CLI `python3 -m tuber tg ...`. |
| `dedup.py` | 156 | 6947 | Кластеризация почти-дублей по simhash + LSH-блокировка (порог Хэмминга 6; D-38/D-39). |
| `owner.py` | 149 | 7068 | Автопостановка канала ВЛАДЕЛЬЦА в реестр как обычного источника (`source_kind='owner'`) — D-69 (`owner.py:1-21`). |
| `init_db.py` | 128 | 6060 | Разовая заливка реестра (`subs_at` заморожен на 2026-09-14). |
| `discover.py` | 112 | 4631 | Дискавери t.me-кандидатов из собственных постов (`source='post_mentions'`). |
| `feeds_import.py` | 89 | 3888 | Импорт кандидатов из фида tuber-os. |
| `prompts/is_ad.md` | 42 | 2760 | Промпт семантического детектора рекламы. |

### 1.2 YouTube — `tuber/platforms/youtube/` (15 модулей, ≈14568 строк)

| Модуль | Строк | Байт | Назначение |
|---|---:|---:|---|
| `seo.py` | 2262 | 99984 | SEO-разбор упаковки (ТОЛЬКО БД, без сети). Заполняет `seo_field`; обложки (`thumb_*`) здесь НЕ заполняются (`seo.py:1-20`). |
| `store.py` | 1751 | 87371 | Адаптер к единому ядру; TEMP-представления legacy-таблиц (`videos`, `snapshots`, `channel_candidates`) + INSTEAD OF-триггеры (`store.py:1-26`). |
| `expand.py` | 1435 | 67407 | Расширение поиска (этап 9): порядок трат от бесплатных к дорогим, `EXPAND_BUDGET_UNITS_PER_RUN=2000` (`expand.py:1-18`). |
| `cli.py` | 1359 | 62250 | CLI `python3 -m tuber yt ...` (`daily`, `snapshots`, `candidates-export/import`, …). |
| `report.py` | 1359 | 58951 | Витрина отчёта (только БД). Ранжирование по `views_per_day` (`report.py:1-12`). |
| `config.py` | 1111 | 64528 | Квоты (`config.py:48-83`), слоты (`:113-124`), `VIRAL_*` (`:130-193`), закрытые списки тем. |
| `collect.py` | 1036 | 45437 | Сбор видео/каналов: `search.list`=100 units → дешёвый предфильтр → `videos.list`=1/50 id (`collect.py:1-10`). |
| `api.py` | 943 | 46754 | Транспорт YouTube Data API v3 без SDK; ротация ключей, сброс квоты по Pacific Time, `quota_log` (`api.py:1-14`). |
| `thumbs.py` | 685 | 28186 | Разбор обложек ТОЛЬКО топ-видео (vision, `kimi-k2.6`), порог `THUMB_MIN_MULT=3.0`, ≤30/сутки. |
| `candidates.py` | 647 | 28235 | Экспорт TG/X из описаний + импорт YouTube-каналов. |
| `comments.py` | 583 | 25952 | Верхние комментарии + харвест комментаторов (likes ≥ 50) в очередь `candidate`. |
| `classify.py` | 568 | 23843 | Смысловой разбор видео (ИИ-тематика), батч ≤20, тема из `config.TOPICS`. |
| `schedule.py` | 447 | 22213 | Расписание замеров: слоты `h0..h6` + `d`. `metric_schedule` НЕ используется — пропуски пишутся в `youtube_slot_miss` (`schedule.py:1-8`, `:267-268`, `:431`). |
| `viral.py` | 382 | 18184 | Композитный индекс виральности (4 оси, база — медиана канала за 90 дней) (`viral.py:1-25`). |

### 1.3 Тесты и скрипты

- `tests/telegram/`: 20 тест-модулей (32 файла суммарно с `__init__`/`conftest`) + 12 HTML-фикстур t.me/s; покрывают форварды, flood-guard, linked-comments, cluster-dedup, promote, followers, LLM-journal.
- `tests/youtube/`: 29 тест-модулей, в т.ч. 4 по квоте (`test_quota_pacific`, `test_quota_project`, `test_quota_search`, `test_queries_rotation`), `test_schedule`, `test_seo`, `test_viral`, `test_denorm_views`.
- `scripts/telegram/`: 11 shell-обёрток (collect, discover, feed_export/import, score, ads, classify, followers, forwards_resume, resolve, install_hermes_cron).
- `scripts/youtube/`: 4 обёртки (daily, snapshots, os_feed_export, os_feed_import).

Все обёртки запускают интерпретатор контура `/usr/local/lib/hermes-agent/venv/bin/python3` явно и пишут в `/root/.hermes/logs/`.

**Фактическое расписание в планировщике Hermes** (из `/root/.hermes/cron/jobs.json`, только активные):

- Telegram: `collect` — `*/30 * * * *`; `score` — `20 * * * *`; `classify` — `50 */2 * * *`; `followers` — `20 3 * * *`; `resolve` — `25 8-23/2 * * *`; `forwards_resume` — `40 8-23/3 * * *`; `ads` (D-37) — `15 4 * * *`; `discover` — `40 7 * * *`; `metric_queue` — `25 * * * *`; `comments_daily` — `30 8 * * *`.
- YouTube: `Tuber_OS замеры` (`tuber_snapshots.sh`) — `0 */3 * * *`; `Tuber_OS суточный цикл` (`tuber_daily.sh`) — `0 7 * * *`.

> Замечание: `install_hermes_cron.sh` регистрирует только 5 шимов (collect/score/discover/feed_export/feed_import), но в `/root/.hermes/scripts/` фактически лежат 11 telegram-шимов; остальные (ads/classify/followers/forwards_resume/resolve) зарегистрированы в планировщике отдельно. Обёртки `promote.sh` нет вовсе — **промоушен не стоит в расписании** (запускается вручную / разово).

---

## 2. Telegram

### 2.1 Путь сбора

Два режима (`collect.py:1-12`): `web` — чтение `https://t.me/s/<handle>` с пагинацией `?before=<id>` (без аккаунта, `WEB_MAX_PAGES=5`, `config.py:52`); `mtproto` — Telethon, инкрементальное чтение подписанных каналов. Резолв кандидатов — `resolve` через `get_entity` + `GetFullChannelRequest` (`RESOLVE_MAX_PER_RUN=20`, `config.py:53`).

Состояние реестра (`source`, platform='telegram', всего **1372**):

| Срез | Значения |
|---|---|
| `read_mode` | `web`=779, `unreadable`=593 |
| `status` | `active`=547, `private`=592, `candidate`=204, `dead`=29 |
| `checked_at` | заполнен у 1372 |
| `verified_at` | заполнен у 0 |

То есть 593 канала (43%) помечены `unreadable`, 592 (43%) — `private`: почти половина реестра недоступна веб-сбору.

### 2.2 Что собирается

- **Посты**: `content` platform='telegram' = **89358**, все `kind='post'`.
- **Просмотры/лайки/реакции/форварды/ответы** — в `metric_snapshot` (133392 строки tg) и денормализации `content_latest`:
  - всего в `content_latest` tg: **89357**;
  - с `forwards`: **36408** (40.7%);
  - с `reactions`: **69370** (77.6%);
  - с `replies`: **0** (в денормализации ответы не пишутся).
- **Ответы (тексты)**: `content_comment` platform='telegram' = **11972**, свежесть `captured_at` до 2026-10-06 05:32.
- Снимки по бакетам tg: `72h`=85334, `24h`=21210, `6h`=18681, `1h`=8175.

Свежесть постов tg: `last_seen` от 2026-09-17 06:49 до **2026-10-06 19:08** (данные живые).

### 2.3 Скоринг значимости (`scoring.py`)

Формула: `Significance = Fr × Wsrc × Xconf × Eng × (1−DupPenalty) × TopicWeight` с распадом по возрасту (`scoring.py:13-16`, `:220`). `Eng = sqrt(eng_channel × eng_global)` — геометрическое среднее двух баз сравнения: медианы ER канала (`eng_channel`) и общей медианы ER корпуса (`eng_global`) (`docs/tg/docs/DESIGN.md:176-207`).

Фактическое наполнение колонок `score` (platform='telegram', всего 89358):

| Колонка | Заполнено | Комментарий |
|---|---:|---|
| `significance` | 68488 | — |
| `engagement` | 68488 | — |
| `decay` | 89358 | заполнен у всех |
| `xconf` | 89358 | заполнен у всех |
| `anomaly` | 214 (=1) | — |
| `first_mover` | 33 | фактически мёртвая ось |
| `velocity` | **0** | не наполняется |
| `spread` | **0** | не наполняется |
| `freshness` | **0** | не наполняется |
| `branch` | 89358 (все пустые) | ветка не заполнена |

→ Из 7 спроектированных осей Telegram фактически живы только `engagement`, `decay`, `significance`; `velocity`/`spread`/`freshness` — нули, `first_mover` — 33 из 89358. `branch` пуст у всех.

Дополнительно в `score.axes_json` (заполнен у **89358/89358**): `er`, `eng_channel`, `eng_global`, `wsrc`, `dup_penalty`, `fr`, `topic_weight`, `age_days`.

Проверка распределений в `axes_json` (важно — вопреки оговоркам докстринга):
- `topic_weight`: нейтральный 1.0 у 83701, но **не-нейтральный у ~5600** (0.9=1111, 1.4=1104, 1.05=963, 1.2=704, 1.3=647, 0.6=402 и др.) — то есть тематический вес фактически считается (`scoring.py:873`, `_config.topic_weight_of`), а не зафиксирован в 1.0.
- `wsrc`: 1.0 у 82924, дробные значения у тысяч постов (0.5=3690, 0.33=678, 0.25=343, … до 0.02).
- `dup_penalty`: 0.0 у 85687, ненулевой у ~3671.

Базы сравнения: `source_baseline` — всего **820 строк, все platform='telegram'**; `median_views` заполнен у 820, `median_reactions`/`median_er` — у 767, а **`median_likes` пуст у ВСЕХ 820** (0 заполненных) — базы по лайкам нет ни у одного канала (совпадает с замером `docs/TZ-53-tehnicheskoe-prilozhenie.md:100-104`).

Порог честного рейтинга: `views >= min_views` И `reactions >= min_reactions` (по умолчанию 100 и 5, `config/scoring.json`); NULL трактуется как «порог не пройден».

### 2.4 Промоушен кандидатов (`promote.py`, `config.py:90-98`)

Канал становится `active`, когда: (1) `content` канала ≥ `PROMOTE_MIN_POSTS` (=20); (2) `antifraud_flag == PROMOTE_MAX_ANTIFRAUD` (=0); (3) `vr` посчитана ПО ФАКТИЧЕСКИМ ПОСТАМ (медиана просмотров / подписчики × 100) и ≥ `PROMOTE_MIN_VR` (=15.0, Q1 распределения). `vr` НЕ берётся из `source.vr` (заполнена только у 5 из 766 кандидатов, D-54). Предохранитель вырождения: при ≥ `PROMOTE_DEGENERATE_MIN_CHECKED` (=50) кандидатах и повышении ВСЕХ — боевой прогон отменяется.

Состояние очереди `candidate` (telegram, всего **2564**): `promoted`=1516, `rejected`=988, `hold`=60. По комментарию `config.py:85-89` правило пропускает 307 каналов (41% очереди). **В расписании промоушен отсутствует** — результат достигнут разовыми прогонами.

### 2.5 Детектор рекламы `is_ad` (`ads.py`)

Написан (519 строк + промпт `prompts/is_ad.md`), НЕ расширяет список ключевых слов (решение владельца), решает смыслом через DeepSeek. Кэш `ad_cache` по `text_hash`.

**Факт: детектор РАБОТАЕТ.** `ad_cache` = **450 строк** (`classified`=449, `error`=1; sample: `is_ad=0`, `method=deepseek`, `confidence` 0.85-0.95). `classify_daily` platform='telegram-ad': прогоны 2026-10-04/05/06, по 150 постов/8 батчей в сутки, cost_usd=0.0 (заниженный учёт — не выяснено, почему). В `run_log` — 53 упоминания. Параллельно keyword-эвристика даёт `content.is_promo=1` у **806** постов (из 89358). В планировщике — ежедневно `15 4 * * *`.

→ Вопреки гипотезе «написан, но не запущен»: **написан И запущен**, но охват ограничен лимитом 150 постов/сутки.

### 2.6 Точные подписчики

Два механизма: `followers.py` (превью `t.me/<handle>`, точное число — основной, ежедневно `20 3 * * *`) и `exact_subs.py` (MTProto `participants_count` — для случаев D-62/D-68). Состояние `source` tg: `subs` заполнено у **1002/1372**, `subs_at` — у 1002. Распределение `subs_at`: **898** каналов обновлены 2026-10-06, 90 — 2026-10-05, 5 — 2026-10-03, остальные единичные (от 2026-09-14 из `init_db`). Ряд подписчиков реален и наполняется.

---

## 3. YouTube

### 3.1 Квота

Константы (`config.py:48-83`): `QUOTA_LIMIT_PER_KEY=10000`, `QUOTA_LIMIT_PER_PROJECT=10000`, `QUOTA_SAFETY_RESERVE=2000` (эффективно ≤8000 на проект), `QUOTA_SEARCH_LIMIT_PER_PROJECT=100`, `QUOTA_SEARCH_RESERVE=15`, `COST_SEARCH=100`, `COST_VIDEOS=1`, `COST_CHANNELS=1`, `COST_PLAYLIST_ITEMS=1`, `COST_COMMENT_THREADS=1`.

Расход за 2026-10-06 (`quota_usage`, сумма units):

| Проект | units | calls |
|---|---:|---:|
| <gcp-project-number> | 1675 | 1378 |
| <gcp-project-number-2> | 988 | 691 |
| <gcp-project-number-3> | 694 | 694 |
| <gcp-project-number-4> | 14 | 14 |
| (без проекта) | 14 | 14 |

По ключам (сегодня): `<key-id-4>`=988, `<key-id-2>`=884, `<key-id-1>`=791, `<key-id-3>`=694, `<key-id-6>`=14, `<key-id-5>`=14. Итого ≈ **3385 units/сутки** при лимите 10000/ключ (резерв 2000) — запас огромный, риск исчерпания квоты отсутствует. `search` сегодня: 3+1+2 = 6 вызовов (600 units) при лимите 100.

### 3.2 План снимков по возрастным корзинам (`schedule.py`, `config.py:113-223`)

Слоты свежих: `SLOTS_FRESH=("h0".."h6")` при `FRESH_MAX_HOURS=48`, шаг из `FRESH_SLOT_OFFSETS` (fallback `MIN_INTERVAL_FOR_SPEED_SECONDS=3600`). Суточный слот `d`: для видео 2-10 дней (`LONG_LIVED_MAX_DAYS=10`) — 2×/сутки (`DAILY_TWICE_INTERVAL_SECONDS=12h`), для более старых — 1×/сутки (`DAILY_INTERVAL_SECONDS=24h`, `DAILY_MIN_AGE_HOURS=48`). `MAX_TRACK_DAYS=30` — старше видео выпадает из плана. Батч `videos.list` = 50 id / 1 unit.

**Важно:** YouTube намеренно НЕ кладёт свои стадии в `metric_schedule` («в неё его стадии намеренно не кладутся», `schedule.py:431`). В `metric_schedule` — только telegram (153780) и x (150904), строк youtube — **0**. Пропущенные слоты фиксируются отдельно в `youtube_slot_miss` — **262366 строк** (пропуск не растворяется, `schedule.py:267-268`).

**Расхождение план↔факт:** в планировщике замеры идут каждые 3 часа (`0 */3 * * *`) — то есть 8 точек/сутки, тогда как план объявляет 7 свежих слотов `h0..h6` + суточный `d`. 3-часовая сетка физически не может попасть в `h1..h6`; это и есть источник 262366 «пропущенных слотов».

### 3.3 Оси виральности (`viral.py`, `config.py:130-193`)

4 оси, все с ОДНОЙ базой сравнения — медиана того же показателя по видео канала за `VIRAL_CHANNEL_WINDOW_DAYS=90` дней (`docs/TZ-53-tehnicheskoe-prilozhenie.md:69`):
1. выброс по просмотрам (`outlier_score` / `vpd_ratio`);
2. выброс по лайкам (`likes_per_1000`);
3. выброс по комментариям (`comment_velocity`);
4. выброс по упаковке (`packaging_score`).

`VIRAL_MIN_AXES=2` (меньше 2 осей → `viral_index=NULL`), `VIRAL_MIN_CHANNEL_BASE=3`, `VIRAL_HALF_LIFE_DAYS=7.0`, `VIRAL_AXIS_CAP=20.0`, `VIRAL_MIN_VIEWS=1000`.

**Наполнение осей** (`v_score_current`, platform='youtube', всего 110223; оси лежат в `score.axes_json`, а `significance` = `viral_index`/`outlier_score`):

| Показатель | Не заполнено (NULL) из 110223 |
|---|---:|
| `axes_json.viral_index` | 43862 нет (заполнено **66361**) |
| `significance` | 43142 нет (заполнено **67081**) |
| `axes_json.outlier_score` | 50104 |
| `axes_json.likes_per_1000` | 37428 |
| `axes_json.comment_velocity` | **67290** |
| `axes_json.packaging_score` | 0 (заполнено у всех) |
| `axes_json.vpd` / `vpd_ratio` | **67008** |

То есть оси `comment_velocity` и `vpd`(скорость просмотров) — почти пусты (61% NULL); `likes_per_1000` пуста у 34%. Эти оси без базы сравнения фактически не дают вклада.

### 3.4 SEO-поля (`seo.py`, таблица `seo_field` = 110223 строк)

Заполнено у 110223 из 110234 видео:
- `title_length` / `desc_length` / `tags_count` / `seo_pattern` / `published_hour_msk` — 110223 (текстовые поля наполнены полностью);
- `thumb_text` / `thumb_objects` — только **302** (обложки разбирает отдельный vision-проход `thumbs.py`: `THUMB_MODEL=kimi-k2.6`, `THUMB_MAX_PER_RUN=30`, `THUMB_MIN_MULT=3.0`, `THUMB_DAILY_BUDGET_USD=0.25`).

`thumbnail_vision` = **302 строки** (совпадает с числом обложек).

### 3.5 Состояние данных

- `content` youtube = **110234**: `short`=70105, `video`=40129.
- `source` youtube = **12748**.
- `metric_snapshot` youtube = **691559**; видео с хотя бы одним снимком — **75510** из 110234 (≈31.5% видео без снимков).
- Снимки по бакетам: `d`=667050, `h6`=9739, `h5`=6678, `h4`=4894, `h3`=1352, `h2`=965, `h1`=627, `h0`=254.
- `score` youtube = 862299; `v_score_current` = 110223, `significance` заполнена у 67081.
- `classification` youtube = **110113** (почти 100%).
- `content_comment` youtube = **153819**, свежесть до 2026-10-06 05:30.
- Свежесть контента: `last_seen` 2026-09-10 → **2026-10-06 05:07**.
- Выгрузка наружу: `v_os_videos`, `v_os_snapshots` — представления legacy-форм для отчёта.

### 3.6 Что не наполняется

- `significance`/`viral_index` пусты у ≈39% видео (43142 из 110223) — либо ниже `VIRAL_MIN_AXES`, либо нет базы сравнения.
- Оси `comment_velocity` (67% NULL) и `vpd` (61% NULL) — фактически мёртвые.
- `thumb_*` в `seo_field` — практически пусто (302 из 110223).
- У 34724 видео нет ни одного снимка.
- `metric_schedule` для youtube не ведётся.

---

## 4. Открытые разрывы

1. **Telegram: оси скоринга.** Заявлены `velocity`, `spread`, `freshness`, `first_mover`; фактически `velocity`/`spread`/`freshness` = **0**, `first_mover` = 33 из 89358. `branch` пуст у всех.
2. **Telegram: `replies` в `content_latest` = 0**, при этом `content_comment` tg = 11972 — тексты ответов собираются, но денормализованный счётчик ответов пуст.
3. **Telegram: база по лайкам отсутствует** — `source_baseline.median_likes` NULL у ВСЕХ 820 строк; ось лайков в Telegram-скоринге не имеет базы сравнения (совпадает с `docs/TZ-53-tehnicheskoe-prilozhenie.md:100-104`).
4. **Telegram: 593 канала `unreadable` + 592 `private`** — почти половина реестра недоступна веб-сбору.
5. **Telegram: `metric_schedule`** — `closed_no_data`=85531 из 153780 (55.6% запланированных замеров закрыты без данных).
6. **Telegram: `forwards`** заполнены только у 36408 из 89357 (40.7%); округл./веб-превью счётчик форвардов не отдаёт (`scoring.py:5-6`), добор идёт через `forwards_resume` (D-34).
7. **Telegram: `topic_weight`** — вопреки докстрингу `scoring.py` («1.0, D-03»), в базе ~5600 не-нейтральных значений: документация отстала от кода.
8. **YouTube: `metric_schedule` не ведётся** (0 строк) — единая очередь охвата стадии YouTube не видит; 262366 пропущенных слота в `youtube_slot_miss` не попадают в общий знаменатель охвата.
9. **YouTube: план↔факт по слотам** — cron `0 */3` (8 точек/сутки) против объявленных `h0..h6`+`d`.
10. **YouTube: снимки неполны** — 34724 из 110234 видео без снимков; `significance` пуста у 39%; `comment_velocity`/`vpd` пусты у ~61-67%.
11. **YouTube: обложки** разобраны лишь у 302/110234 (лимит `THUMB_MAX_PER_RUN=30`/сутки).
12. **Детектор рекламы Telegram** ограничен 150 постами/сутки — при 89358 постах охват 450 `ad_cache` (0.5%).
13. **Промоушен Telegram не в расписании** — обёртки `promote.sh` нет; результат достигнут разовыми прогонами.

---

## 5. Выводы

1. Обе платформы перенесены в монорепо через адаптеры `store.py`, пишущие в единое ядро (`source/content/metric_snapshot/score/candidate`); legacy-таблицы эмулируются TEMP-представлениями и INSTEAD OF-триггерами (`tuber/platforms/youtube/store.py:1-26`).
2. Telegram-сбор живой: 89358 постов, свежесть до 2026-10-06 19:08; но вовлечённость неполна — `forwards` 40.7%, `replies`=0.
3. Telegram-скоринг работает ЧАСТИЧНО: `engagement`/`decay`/`xconf`/`significance`/`axes_json` наполнены (68488/89358/89358/68488/89358), а `velocity`/`spread`/`freshness` — нули, `branch` пуст.
4. Тематический вес и `wsrc` фактически считаются (≈5600 не-нейтральных `topic_weight`), хотя докстринг `scoring.py` заявляет фиксацию в 1.0 — **документация отстала от кода**.
5. Промоушен Telegram измерим (правило `PROMOTE_*`, `config.py:90-98`): 1516 `promoted` из 2564 кандидатов, но в расписании его нет (ручные прогоны).
6. Детектор рекламы `ads.py` **написан И запущен** (вопреки гипотезе «написан, но не запущен»): `ad_cache`=450, `classify_daily platform='telegram-ad'` за 04-06.10.2026, ежедневно `15 4`; охват ограничен 150 постами/сутки.
7. Точные подписчики Telegram реально наполняются двумя механизмами: `subs` у 1002/1372, ряд обновлён 06.10.2026 (898 каналов) через превью `t.me/<handle>`.
8. YouTube-квота расходуется экономно: ≈3385 units/сутки при лимите 10000/ключ (резерв 2000) — риска исчерпания нет; `search`=6 вызовов (600 units) при лимите 100.
9. YouTube-виральность строится на 4 осях с единой базой (медиана канала, окно 90 дней), требует ≥2 осей; но `viral_index`/`significance` пусты у ≈39% видео, а оси `comment_velocity` и `vpd` почти мертвы (61-67% NULL) — «оси без базы сравнения».
10. YouTube-снимки неполны (34724 видео без снимков), их расписание вне единой очереди `metric_schedule`, а cron-сетка `0 */3` не бьётся с объявленными слотами `h0..h6`+`d` → 262366 пропущенных слота, невидимых в общем охвате.
11. YouTube-обложки — узкое место: vision разобран лишь у 302 видео (`THUMB_MIN_MULT=3.0`, 30/сутки), поля `thumb_*` в `seo_field` почти пусты, тогда как текстовый SEO заполнен на 100% (110223).

---

_Невясненное:_ (1) почему `classify_daily platform='telegram-ad'` показывает `cost_usd=0.0` при реальных токенах — «не выяснено»; (2) точная причина 43142 видео без `viral_index` (порог осей vs отсутствие базы) без построчного разбора — «не выяснено»; (3) почему `metric_schedule` не ведёт youtube, кроме мотива «намеренно» из `schedule.py:431`, — «не выяснено».
