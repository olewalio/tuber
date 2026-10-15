# ТЗ-1 — Tuber-x, ядро сбора

Проект: `/root/tuber-x`. Язык: Python 3, только стандартная библиотека + `requests` при
наличии (не обязателен). БД: SQLite (`/root/tuber-x/data/tuber_x.db`), WAL.
Исполнитель: jCode. Работать строго по пунктам; ничего не додумывать сверх ТЗ.

Источник истины по механикам — `/root/tuber-x/METHODOLOGY.md` (прочитать перед началом).

## Р1. Структура проекта

```
/root/tuber-x/
  METHODOLOGY.md
  docs/TZ-1-core.md
  tuber_x/__init__.py
  tuber_x/config.py        # пути, лимиты, пул инстансов, тиры — всё в одном месте
  tuber_x/nitter_broker.py # единственный владелец квоты Nitter
  tuber_x/db.py            # схема, миграции, хелперы
  tuber_x/registry.py      # реестр аккаунтов + валидатор кандидатов
  tuber_x/collect.py       # обход реестра, курсоры, запись постов
  tuber_x/cli.py           # команды
  tests/                   # pytest
  data/                    # БД, логи
```

Команды CLI (все — идемпотентные, повторный запуск не ломает данные):

```
python3 -m tuber_x.cli init                 # создать БД
python3 -m tuber_x.cli instances --check    # проверить живость пула инстансов
python3 -m tuber_x.cli registry add <handle> --tier A --source <запрос>
python3 -m tuber_x.cli registry add-file <путь.csv>
python3 -m tuber_x.cli registry list [--status active] [--tier A]
python3 -m tuber_x.cli collect --tier A|B|C --max-accounts N [--dry-run]
python3 -m tuber_x.cli backfill <handle> --pages 3
python3 -m tuber_x.cli budget               # расход квоты по инстансам за сутки
```

## Р2. Схема БД

`accounts` — реестр:

```
id INTEGER PK, handle TEXT UNIQUE NOT NULL,          -- без @, валиден по ^[A-Za-z0-9_]{1,15}$
x_id TEXT,                                            -- если удалось достать из lede/ссылки
tier TEXT NOT NULL DEFAULT 'C',                       -- A|B|C
status TEXT NOT NULL DEFAULT 'candidate',             -- candidate|active|dead|blocked|rejected
lang TEXT, topic_guess TEXT,
is_author INTEGER,                                    -- 1 автор, 0 усилитель/провод, NULL неизвестно
ai_density REAL,                                     -- доля постов про ИИ (0..1)
cv_interval REAL, posts_per_day REAL, link_ratio REAL, rt_ratio REAL, dup_ratio REAL,
first_mover_score REAL DEFAULT 0,                     -- накопительно
posts_collected INTEGER DEFAULT 0,
last_success_at TEXT, last_attempt_at TEXT, fail_streak INTEGER DEFAULT 0,
last_error TEXT, cursor TEXT,                         -- курсор min-id — страховка от потери
added_at TEXT DEFAULT (datetime('now')), added_by TEXT, source_type TEXT, notes TEXT
```

`posts`:

```
id INTEGER PK, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
tweet_id TEXT NOT NULL,                              -- из guid (snowflake)
published_at_utc TEXT NOT NULL,                       -- ISO, UTC
published_src TEXT NOT NULL,                          -- rss | snowflake | unknown
text TEXT, text_hash TEXT,
lang TEXT, links TEXT, mentions TEXT, hashtags TEXT,
is_retweet INTEGER DEFAULT 0, is_quote INTEGER DEFAULT 0, is_reply INTEGER DEFAULT 0,
owner_handle TEXT,                                    -- кто реально опубликовал (при ретвите ≠ dc:creator)
orig_handle TEXT,                                     -- автор оригинала, если это ретвит/цитата
media_kind TEXT,
first_seen_at TEXT DEFAULT (datetime('now')),
UNIQUE(tweet_id)
```

Инвариант: `published_at_utc` не может быть пустым, `1970-*` или в будущем более чем
на 2 часа; иначе — восстановление из snowflake, при неудаче статус `unknown` и пост
НЕ попадает в выборки отчёта.

`cursors` (аккаунт/запрос → курсор и история обхода):

```
id INTEGER PK, kind TEXT NOT NULL,                    -- account|search
ref TEXT NOT NULL,                                    -- handle или строка запроса
cursor TEXT, last_page_at TEXT, pages_total INTEGER DEFAULT 0,
items_total INTEGER DEFAULT 0, UNIQUE(kind, ref)
```

`instances` и `requests`:

```
instances: host TEXT PK, healthy INTEGER, rss_ok INTEGER, items_last_test INTEGER,
           last_check_at TEXT, fail_streak INTEGER, cooldown_until TEXT,
           requests_today INTEGER, day TEXT, version TEXT, last_error TEXT
requests:  id INTEGER PK, host TEXT, ts TEXT DEFAULT (datetime('now')), kind TEXT,
           url TEXT, status INTEGER,
           items INTEGER, latency_ms INTEGER, run_id INTEGER
```

`candidates` (очередь роста реестра, наполняется в ТЗ-2, но таблица создаётся здесь):

```
handle TEXT PK, found_via TEXT, found_in_account TEXT, seen_count INTEGER DEFAULT 1,
distinct_sources INTEGER DEFAULT 1, first_seen_at TEXT, last_seen_at TEXT,
validated TEXT, reject_reason TEXT, llm_checked INTEGER DEFAULT 0
```

`runs`, `run_log`, `metrics_daily` — по образцу `/root/tuber-telegram/scripts/init_db.py`
(`runs`: mode, accounts_ok, accounts_fail, posts_new, posts_upd, errors, note;
`metrics_daily`: day, posts_ingested, dup_rate, coverage, fail_rate, latency_p95_min,
valid_date_ratio, instances_alive).

## Р3. Брокер доступа к Nitter (`nitter_broker.py`)

Единственная точка, откуда проект ходит в Nitter. Прямые `urlopen` в других модулях
запрещены (проверяется тестом: grep по проекту не должен находить `urlopen` вне
`nitter_broker.py`).

Р3.1. Пул инстансов из конфига, по умолчанию:

```
https://nitter.kareem.one
https://nitter.jaydenha.uk
```

Пул расширяется строкой в `config.py`, без правки кода брокера.

Р3.2. Лимитер: не более **6 запросов / 60 с на инстанс** (значение меняется конфигом;
замеренный предел — 8/30 с, работать ниже предела). Между двумя запросами к одному
инстансу — не менее 2 с.

Р3.3. Учёт расхода: каждый запрос пишется в `requests` (host, kind, url, status, items,
latency_ms). Суточный потолок на инстанс — 18 000 запросов (80% от 23 040). При
достижении потолка инстанс исключается до конца суток, запись в `run_log` уровня WARN.

Р3.4. Health-проверка: `GET /nasa/rss` → инстанс живой, только если HTTP 200 **и**
`items >= 15`. HTTP 200 с нулём item (заглушка Anubis) — не живой. Проверка перед
первым использованием и далее не чаще одного раза в 10 минут на инстанс.

Р3.5. Обработка отказа: при 429 — cooldown инстанса 180 с и переход на второй;
второй 429 подряд — cooldown 900 с; при 403/451 — инстанс помечается недоступным на
сутки. Если живых нет — брокер не долбит сеть, а возвращает ошибку `NoLiveInstance`
с записью в `run_log`.

Р3.6. Кеш: один и тот же URL не запрашивать чаще, чем раз в 600 с (кеш инстанса —
10 минут), кроме явного `force=True` (нужен только тестам).

Р3.7. Приоритеты: `critical` (сторожа) > `collect` > `discover` > `backfill`.
Реализация — приоритетная очередь; при перегрузке сначала обслуживаются старшие.

Р3.8. Публичный интерфейс:

```
fetch_feed(handle, cursor=None, priority='collect', force=False) -> list[dict]
fetch_search(query, cursor=None, priority='discover', force=False) -> list[dict]
check_instance(host) -> dict
stats() -> dict   # по инстансам: запросов за сутки, cooldown, живой/нет
```

`fetch_*` возвращает распарсенные посты, каждый — словарь с ключами:
`tweet_id, owner_handle, orig_handle, published_at_utc, published_src, text, links,
mentions, hashtags, is_retweet, is_quote, is_reply, media_kind, cursor_next`.

Р3.9. Разбор RSS:

- `tweet_id` — из `guid` (там числовой snowflake-id; если guid не число — из `link`);
- `owner_handle` — из заголовка `RT by @X:` (это владелец ленты), иначе из `dc:creator`;
- `orig_handle` — из `dc:creator`, если это ретвит/цитата (иначе пусто);
- `published_at_utc` — из `pubDate`, источник помечается `rss`; если `pubDate` пуст или
  год < 2000 — восстановить из snowflake (`tweet_id >> 22` + 1288834974657 мс) и
  пометить источник `snowflake`; иначе `unknown`;
- `text` — из `description` с удалением HTML-тегов и декодированием сущностей;
- `mentions`, `hashtags`, `links` — регулярками, без дублей;
- `cursor_next` — из заголовка ответа `min-id` (нижний регистр!);
- max 5 запросов в тесте, реальные запросы в тестах не выполняются (сеть подменяется
  фикстурой RSS, файл `tests/fixtures/sample_feed.xml`).

## Р4. Реестр (`registry.py`)

Р4.1. Валидация хендла: `^[A-Za-z0-9_]{1,15}$`, приведение к нижнему регистру, `@`
отбрасывается. Невалидный — отказ с причиной `bad_handle` (не исключение).

Р4.2. Добавление в реестр без сетевой проверки (статус `candidate`) — команда `registry add`.

Р4.3. Проверка кандидата перед переводом в `active` (команда `registry verify <handle>`,
в ТЗ-2 вызывается автоматически из дискавери):

1. инстанс отдал ≥ 15 item по этому аккаунту;
2. не менее 2 разных авторов из доверенного ядра упоминали его за всё время
   (co-occurrence считается по таблице `posts.mentions` и `orig_handle`);
3. посты/сутки в диапазоне 0.2–20;
4. CV интервалов ≥ 0.15 (при ≥ 10 постах);
5. доля ссылок ≤ 0.7, доля дублей ≤ 0.3, доля ретвитов ≤ 0.8;
6. иначе — `rejected` с причиной.

Результат проверки пишется в `candidates.validated` / `reject_reason` и в
`accounts.status`, все измеренные признаки — в поля `accounts`.

Р4.4. Дневной потолок прироста: не более 50 новых `active` в сутки; при превышении
остальные остаются `candidate` до следующего дня (запись в `run_log`).

Р4.5. Тиры: `A` — до 200 аккаунтов, `B` — до 1000, `C` — остальные. Назначение тира —
вручную или по `first_mover_score`/`ai_density` (в ТЗ-2).

## Р5. Сбор (`collect.py`)

Р5.1. Обход по тирам: A — раз в час, B — раз в 3 часа, C — раз в сутки. Модуль не
хранит расписание сам: период передаётся аргументом/конфигом, расписание — в кроне
(ТЗ-4).

Р5.2. На каждый аккаунт — один запрос ленты. Если у аккаунта есть сохранённый `cursor`
и он «отстаёт» (последний пост в БД старше 20 постов назад) — добрать страницы
курсором, но не более 3 страниц за прогон и не более 1 страницы для TIER-C.

Р5.3. Дедупликация: `INSERT OR IGNORE` по `UNIQUE(tweet_id)`; счётчики `posts_new` /
`posts_upd` в `runs` обязаны быть корректными (новые = реально вставленные).

Р5.4. После обхода аккаунта: обновить `last_success_at`, `fail_streak=0`,
`cursor = cursor_next`, `posts_collected`, пересчитать `posts_per_day` и `cv_interval`
по последним 100 постам.

Р5.5. Отказ аккаунта (404/нет item/ошибка брокера): `fail_streak += 1`,
`last_error`; при 5 подряд — `status='dead'` и запись в `run_log` уровня WARN.
Ни один отказ не должен прерывать обход остальных аккаунтов.

Р5.6. Прогон пишет `runs` (mode, accounts_ok, accounts_fail, posts_new, posts_upd,
errors) и по завершении — строку в `metrics_daily`.

Р5.7. `--dry-run` — обход без записи в БД (только чтение лент и отчёт в stdout).

## Р6. Полнота и свежесть (критерии, реализуемые в коде)

Р6.1. Команда `collect --verify-completeness --tier A` — после обхода выполняет
повторный обход тех же аккаунтов и печатает число новых id. Норма: **0 новых**.
Не ноль — предупреждение с рекомендацией участить обход (правило §5 методологии).

Р6.2. Отчёт `budget` печатает: запросов за сутки по инстансам, долю от потолка,
число 429, число инстансов в cooldown.

## Р7. Тесты (pytest, без сети)

Обязательные:

1. `test_broker_parse` — разбор фикстуры RSS: ретвит (владелец vs оригинал), цитата,
   медиа, ссылки, упоминания, хештеги, snowflake-фолбэк даты.
2. `test_broker_limit` — лимитер: серия из 20 «запросов» с подменённым транспортом
   не превышает 6/60 с и не превышает лимит на инстанс.
3. `test_broker_health` — HTTP 200 с `items=0` не считается живым; 429 даёт cooldown;
   403/451 — недоступность на сутки.
4. `test_db_schema` — все таблицы создаются, повторный `init` не падает, WAL включён.
5. `test_date_validation` — пустая дата, 1970 и дата в будущем отбраковываются.
6. `test_registry_validate` — все 6 порогов Р4.3 (по одному тесту на отказ).
7. `test_collect_dedup` — повторный обход даёт 0 новых; `posts_upd` считается верно.
8. `test_fail_streak` — 5 отказов переводят аккаунт в `dead`, обход других аккаунтов
   не прерывается.
9. `test_no_direct_network` — в проекте нет `urlopen` вне `nitter_broker.py`.

## Р8. Приёмка (выполняется на живых данных, не в тестах)

| # | Проверка | Команда | Порог |
|---|---|---|---|
| П1 | Схема и инициализация | `python3 -m tuber_x.cli init && sqlite3 data/tuber_x.db ".tables"` | таблицы есть, повторный `init` без ошибок |
| П2 | Пул инстансов | `cli instances --check` | ≥ 2 живых по замеру «items ≥ 15» |
| П3 | Живой сбор | `cli registry add nasa --tier C` затем `cli collect --tier C --max-accounts 1` | в БД появились посты, `last_success_at` заполнен |
| П4 | Полнота | `cli collect --tier C --max-accounts 1 --verify-completeness` | 0 новых id при повторном обходе |
| П5 | Валидность дат | SQL: доля `published_at_utc` валидных | ≥ 99% (пустых/1970 — нет) |
| П6 | Курсор и бэкфилл | `cli backfill <handle> --pages 3` | ≥ 50 уникальных постов, прирост страниц без дублей |
| П7 | Квота | `cli budget` | расход ≤ 80% потолка, 429 — не более 10% запросов |
| П8 | Тесты | `python3 -m pytest tests -q` | все зелёные |
| П9 | Изоляция транспорта | `grep -rn "urlopen" tuber_x/` | только `nitter_broker.py` |

## Р9. Запрещено

- Ходить в Nitter из модулей, кроме брокера.
- Использовать платный X API, прокси, креды (их нет и заводить не нужно).
- Писать `published_at` из «сегодня», если дата не разобралась (лучше `unknown`).
- Считать «популярность»: метрик вовлечённости в канале нет.
- Менять что-либо вне `/root/tuber-x` (CryptoGraph и его конфиги в этой задаче не трогать).

## Р10. Что сдать

1. Работающий код по Р1–Р6, тесты Р7 зелёные.
2. Лог прогона приёмки П1–П9 с фактическими цифрами (stdout команд).
3. Короткая записка `docs/REPORT-1.md`: что сделано, какие цифры получены, что не
   получилось и почему.
