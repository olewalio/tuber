# Ядро, архитектура и инварианты — глубокий анализ монорепо Tuber

_Файл дополняется инкрементально. Все факты — с указанием `путь:строка`._

- Дата анализа: 2026-10-06.
- Репозиторий: `/root/tuber`, ветка `master`, HEAD `0fec101`.
- Масштаб: 329 `.py`-файлов; 169 тестовых файлов; `pytest --collect-only` → **1897 тестов**; суммарно строк в тестах — 37 924.
- Интерпретатор: Python 3.11.15.
- Боевая БД `data/tuber.db` (2 ГБ) в рамках этой работы НЕ изменяется (только чтение).

## Оглавление

1. [Инвентаризация модулей ядра](#1-инвентаризация-модулей-ядра)
2. [Слои и связи](#2-слои-и-связи)
3. [Архитектурные инварианты](#3-архитектурные-инварианты)
4. [Дублирование и мёртвый код](#4-дублирование-и-мёртвый-код)
5. [Тесты](#5-тесты)
6. [Выводы](#6-выводы)

---

## 1. Инвентаризация модулей ядра

Каталог `tuber/core/` — 16 `.py`-файлов, **6 841 строка**, **338 672 байта**.
Замеры: `wc -l` / `wc -c` по `tuber/core/*.py`.

| Файл | Строк | Байт | Назначение (докстринг/код) |
|------|------:|-----:|----------------------------|
| `tuber/core/__init__.py` | 1 | 125 | «Ядро единой базы: схема, соединение, хранилище, время, legacy-чтение.» |
| `tuber/core/db.py` | 120 | 4 849 | Слой соединения (§4 ТЗ-1): `connect`, PRAGMA, read-only legacy, `write_tx`. |
| `tuber/core/schema.py` | 1822 | 98 542 | Единая схема ядра (DDL из ТЗ-1 §3). Самый крупный модуль ядра. |
| `tuber/core/storage.py` | 806 | 36 022 | Функции доступа к ядру — «единственное место с SQL-записями ядра» (`storage.py:1`). |
| `tuber/core/graph.py` | 1455 | 72 090 | Граф источников (ТЗ-8): рёбра «пост→цель», `consume`, `verify_feed`. |
| `tuber/core/metrics.py` | 897 | 44 105 | Общий расчёт дельт и стадий замеров (ТЗ-43). |
| `tuber/core/firstmovers.py` | 609 | 30 078 | Граф первопроходцев (ТЗ-47, контур 4 «Сливки»). |
| `tuber/core/daily_metrics.py` | 270 | 14 067 | Суточная сводка `metrics_daily` по платформам (ТЗ-53, часть 2). |
| `tuber/core/sqlcompat.py` | 236 | 11 912 | Совместимость SQL: legacy-имена vs таблицы ядра (ТЗ-3c), `CompatConnection`. |
| `tuber/core/health_all.py` | 163 | 7 806 | Кросс-платформенный сторож (часть 2 «волна 2»). |
| `tuber/core/timeutil.py` | 121 | 4 211 | Единый разбор/формат дат (ISO-8601 UTC). |
| `tuber/core/health_dedup.py` | 106 | 4 523 | Дедуп алертов сторожа. |
| `tuber/core/urls.py` | 83 | 4 182 | Синтез прямой ссылки на материал (долг D-45). |
| `tuber/core/textsim.py` | 70 | 3 082 | Общие функции близости текстов (simhash, Хэмминг, Jaccard, D-38). |
| `tuber/core/legacy.py` | 51 | 1 795 | Чтение legacy-баз в режиме read-only. |
| `tuber/core/ids.py` | 31 | 1 283 | Идентификаторы внешних сущностей в ядре. |
| **Итого core** | **6 841** | **338 672** | |

Опорные файлы вне `core/`:

| Файл | Строк | Байт | Назначение |
|------|------:|-----:|------------|
| `tuber/cli.py` | 265 | 13 578 | Диспетчер CLI: `migrate/parity/schema/doctor/report/db/graph/rating/queue/trends/comments/digest/metrics/usage` + ссылки на платформенные подкоманды `x/tg/yt`. |
| `tuber/config.py` | 60 | 2 095 | Путь к единой БД (приоритет `--target` → `TUBER_DB` → `default.ini` → `data/tuber.db`), PRAGMA-константы. |
| `tuber/__main__.py` | 6 | 140 | Точка входа `python3 -m tuber`. |

`tuber/tools/` (4 `.py` + `__init__.py`): `migrate_legacy.py` (78 960 б — крупнейший файл репо), `parity_report.py` (15 688), `db_backup.py` (11 955), `graph_cmd.py` (9 364).

Платформенные подсистемы (`tuber/platforms/`): `x/` — 22 файла, `telegram/` — 21, `youtube/` — 15. Аналитический слой (`tuber/analysis/`) — 18 модулей, **10 191 строка**.

---

## 2. Слои и связи

Архитектура — четырёхслойная, границы заданы соглашением и проверяются тестами.

### 2.1 Кто владеет схемой БД

- **Владелец схемы — ядро.** DDL живёт только в `tuber/core/schema.py`: **39** `CREATE TABLE`, **26** `CREATE INDEX`, **7** `CREATE VIEW` (замер `grep -c`). Версия схемы — `SCHEMA_VERSION = "7"` (`schema.py:19`), версия денормализации — `DENORM_VERSION = "1"` (`schema.py:761`).
- Полный перечень обязательных объектов — `REQUIRED_TABLES` из **37** имён (`schema.py:1597`), `REQUIRED_INDEXES` (`schema.py:1608`), `REQUIRED_VIEWS` = `v_score_current, v_os_videos, v_os_snapshots, v_x_posts, v_x_scores, v_tg_posts, v_tg_scores` (`schema.py:1632`).
- Докстринг `tuber/tools/migrate_legacy.py:13`: «Боевые базы не копируются и не перезаписываются»; `schema.py:1068`, `schema.py:1080`, `schema.py:1575` — миграции дореклассификации/URL гейтуются по `schema_meta`, чтобы не крутить тяжёлый проход на каждом соединении.
- Идемпотентность гарантируется гейтом: `tuber/platforms/telegram/store.py:212` (`connect`) при несовпадении версии вызывает `migrate_schema` (`store.py:250`), затем ставит слой совместимости `install_compat` (`store.py:252`).

### 2.2 Кто пишет / читает

- **Канонический слой записи ядра — `tuber/core/storage.py`.** Докстринг `storage.py:1`: «Функции доступа к ядру (единственное место с SQL-записями ядра)». 41 функция записи (из 45 публичных и 51 всего `def` в файле): `upsert_source` (`storage.py:147`), `upsert_content` (`storage.py:228`), `add_snapshot` (`storage.py:288`), `recompute_content_latest` (`storage.py:312`), `add_llm_usage` (`storage.py:581`), `upsert_metrics_daily` (`storage.py:509`) и др.
- **Слой соединения — `tuber/core/db.py`**: `connect(path, readonly=False)` (`db.py:28`), `apply_pragmas` (`db.py:61`), `write_tx` (`db.py:73`), `is_readonly` (`db.py:114`).
- **Платформы** владеют сбором и адаптацией: `x/store.py:287` (`connect`), `telegram/store.py:212`, `youtube/store.py:138`. Они вызывают ядро через `storage.*` (напр. `x/store.py:842` — `storage.set_follower_snapshot`, `youtube/store.py:1002` — `storage.upsert_source`, `telegram/classify.py:505` — `storage.add_llm_usage`).
- **Границы-разрывы зафиксированы численно.** Докстринг `storage.py:1` декларирует единственность слоя записи, однако фактически в `tuber/platforms/` **156 из 206** прямых DML-инструкций адресуют таблицы ядра напрямую (`INSERT INTO source`, `UPDATE candidate SET`, `INSERT INTO candidate` и т.п.) — замер `grep -oE "INSERT INTO [a-z_]+|UPDATE [a-z_]+ SET"` по `tuber/platforms/**/*.py` с классификацией по списку таблиц ядра. Примеры: `telegram/promote.py:275` (`UPDATE source SET`), `telegram/promote.py:324` (`INSERT INTO candidate`), `telegram/owner.py:103` (`INSERT INTO source`). Это расхождение докстринга и кода (см. §4). Прямые записи мотивированы legacy-адаптерами: незаквалифицированные имена транслирует `CompatConnection` (`sqlcompat.py`), а часть платформенного кода пишет имена ядра напрямую.
- **Анализ** (`tuber/analysis/`) — преимущественно читатель/выдача (`report.py`, `readable.py`, `coverage.py`, `names.py`). Часть подкоманд пишет снимки и гейтуется production-флагом: `slivki.py`, `queue.py`, `trends.py`, `comments.py`, `digest.py`, `metric_queue.py`.
- **Инструменты** (`tuber/tools/`) — миграция (`migrate_legacy.py`), сверка (`parity_report.py`), бэкап (`db_backup.py`), граф-CLI (`graph_cmd.py`).

### 2.3 Поток данных

```
legacy-БД (tuber-os/x/telegram)  ──read-only (file:?mode=ro)──►  tools/migrate_legacy
        │                                                                  │
        └──────────────────────────────────────────────────────────────────┘
                                    ▼
                     ядро: core/schema.py (DDL) + core/storage.py (запись)
                                    ▲
     platforms/{x,telegram,youtube}  │ storage.*          analysis/*  (чтение+выдача)
     (сбор, сеть — только через broker X)                    (гейт --allow-production на запись)
```

---

## 3. Архитектурные инварианты

### И1. Все даты — TEXT ISO-8601 UTC
Заявлен в `tuber/core/timeutil.py:1-12` («Инвариант §3 ТЗ-1: **все** даты в единой базе — TEXT ISO-8601 UTC вида `YYYY-MM-DD HH:MM:SS`»). Конвертации legacy (epoch-секунды tuber-os; ISO-строки с `T`/`Z` в x/tg) идут только через этот модуль. Единая схема подтверждает: `schema.py:1` («Все даты — TEXT ISO-8601 UTC»).

### И2. Legacy-базы открываются ТОЛЬКО read-only
Закреплено в `db.py:40-52`: `readonly=True` открывает `file:<path>?mode=ro` + `PRAGMA query_only=ON`; для readonly не применяются PRAGMA записи. Обёртка `legacy.open_legacy` (`legacy.py:18-20`) жёстко зовёт `db.connect(path, readonly=True)`. Докстринг `legacy.py:3-7`: «любой случайный INSERT/UPDATE/DELETE получит ошибку драйвера, а не тихо испортит прод». Проверка: `db.is_readonly` (`db.py:114`) читает `PRAGMA query_only`.

### И3. Сеть — только через брокер (П9/Р3)
- Модуль-владелец квоты: `tuber/platforms/x/broker.py:1-5` — «Единственный владелец квоты Nitter… Прямые `urlopen` вне этого файла запрещены — проверяется тестом `test_no_direct_network`».
- Единственное место ядра, ходящее в сеть, — `graph.verify_feed` (`graph.py:1088`); докстринг `graph.py:14`: «единственное место графа, которое ходит в сеть (и только через переданный `broker`)». Внутри — `broker.get(...)` (`graph.py:943,963`) и импорт `raw_http_get` из брокера (`graph.py:936`).
- **Как проверяется:** `tests/x/test_no_direct_network.py:22` (`test_no_urlopen_outside_broker`) запрещает `urlopen` вне `broker.py`; `tests/x/test_no_direct_network.py:40-41` требует ровно **1** вызов `urllib.request.urlopen(` в брокере. Дублирующая проверка транспорта — `tests/x/test_session_transport.py:331`.

### И4. Production-гейт (`--allow-production`)
Запись/отправка в боевую единую базу запрещена без явного флага. Реализация:
- `tuber/platforms/telegram/store.py:349` — `production_gate_error(path, allow_production)`; `is_production_db` (`telegram/store.py:321`).
- `tuber/platforms/youtube/store.py:224` — `production_gate_error`; `is_production_db` (`youtube/store.py:213`).
- `tuber/platforms/x/store.py:426` — только `is_production_db`.
- Гейт применяется в подкомандах: `telegram/{owner,discover,classify,prelim,feeds_export,feeds_import,ads}.py`, `youtube/cli.py:196`, `analysis/{digest,metric_queue,trends,slivki,comments,queue}.py`, `tools/{db_backup,graph_cmd}.py`.
- Причина (урок ТЗ-45F) зафиксирована в `tuber/analysis/digest.py:40-41`: «Отправка гейтуется боевой базой… иначе боевой ночной прогон молча отказывает».
- Проверка тестом: `tests/telegram/test_b9_gate_owner_incremental.py` (по реестру долга D-43).

### И5. Единый писатель снимков + монотонность просмотров (D-70)
- Клэмп монотонности перенесён в уровень хранилища: `storage.add_snapshot` (`storage.py:288`) в докстринге прямо называет это «единой точкой записи снимков» и не даёт записать убывание просмотров; факт клэмпа считает `views_clamp_count` (`storage.py:263`).
- Базовая функция — `storage.monotonic_views` (`storage.py:268`); `metrics.monotonic_views` (`metrics.py:617`) оставлена «тонкой обёрткой», чтобы очередь не дублировала правило (докстринг функции прямо ссылается на ТЗ-B6/D-70).
- Проверка: `tests/core/test_d70_views_clamp.py`.

### И6. Один писатель WAL на транзакцию (`BEGIN IMMEDIATE` + retry)
`db.write_tx` (`db.py:73-111`): `BEGIN IMMEDIATE`, до `WRITE_RETRIES = 5` (`db.py:24`) попыток с паузой 0.5→1→2→4 с при `database is locked`. Обоснование в докстринге `db.py:8-11`: три платформы пишут из разных процессов, WAL допускает одного писателя. Telegram-адаптер оборачивает своё `write_tx` поверх ядрового (`telegram/store.py:292-299`).

### И7. Идемпотентность апсертов
`storage.py:6-8`: «Каждый upsert идемпотентен: при повторном прогоне те же строки дают те же идентификаторы и не создают дублей (опора на `UNIQUE`/`PRIMARY KEY` и `ON CONFLICT DO UPDATE`)». Примеры: `daily_metrics.compute_daily` (`daily_metrics.py` докстринг — `ON CONFLICT(day, platform) DO UPDATE`), `metrics._write_snapshot` (`ON CONFLICT(content_id, captured_at) DO UPDATE`).

### И8. Журнал прогонов не оставляет `finished_at IS NULL`
`tuber/cli.py:249-265` (`main`): при необработанном исключении зовёт `storage.close_open_runs_now("error: unhandled")`. Штатный путь — `storage.run_guard` (`storage.py:749`) + `close_run`; фон-сторож `_RunDeadlineWatchdog` (`storage.py:715`). Проверка: `tests/core/test_run_reliability.py`.

### И9. Единственный источник simhash (D-38)
`textsim.py:1-9`: «Единственный источник simhash в проекте (D-38): и кластеризация сюжетов X, и кластер почти-дублей Telegram берут его ОТСЮДА, а не заводят вторую реализацию».

---

## 4. Дублирование и мёртвый код

### 4.1 Дублирование логики

| Что дублируется | Где | Кол-во копий |
|---|---|---|
| `def _is_production(path)` — идентичные тела, сравнение `realpath(path) == realpath(config.DEFAULT_DB_PATH)` | `tuber/tools/graph_cmd.py:31`, `tuber/analysis/digest.py:908`, `tuber/analysis/metric_queue.py:45`, `tuber/analysis/trends.py:47`, `tuber/analysis/slivki.py:1112`, `tuber/analysis/comments.py:48`, `tuber/analysis/queue.py:1371` | **7** (проверено построчным сравнением тел) |
| `def is_production_db(path)` | `tuber/platforms/telegram/store.py:321`, `tuber/platforms/youtube/store.py:213`, `tuber/platforms/x/store.py:426` | **3** |
| `def production_gate_error(path, allow_production)` | `tuber/platforms/telegram/store.py:349`, `tuber/platforms/youtube/store.py:224` | **2** (+`assert_can_write` в `telegram/scoring.py:231`) |

Тела `_is_production` в 5 из 7 файлов совпадают символ-в-символ; в `comments.py:50-51` отличается лишь локальным алиасом `core_config`. Это классический кандидат на вынос в `tuber/core` или `tuber/config.py` — сейчас инвариант «боевая база» размазан по 12 точкам.

### 4.2 Мёртвый код (проверено grep по вызовам `имя(`)

- `text_similarity(a, b)` — определена в `tuber/core/textsim.py:64`, **0 реальных вызовов** во всём `tuber/ tests/ scripts/ tools/` (единственное вхождение — сама строка `def`).
- `read_ledger(con)` — определена в `tuber/core/firstmovers.py:427`, **0 вызовов**.
- `compat_sql_enabled` — property-геттер `CompatConnection` (`tuber/core/sqlcompat.py:133-135`), **0 обращений** к публичному свойству (внутри используется приватное `self._compat_sql_enabled`, `sqlcompat.py:139`).

_(Ложно-мёртвыми выглядят `repl_intro`/`repl_pragma` — это вложенные callback'и, передаваемые в `re.sub` внутри `_translate` (`sqlcompat.py:86-87`); они живые.)_

### 4.3 TODO/FIXME с номерами долгов

Маркеров `# TODO(debt-...)` в `tuber/` — **31** (весь репо). В самом ядре — **4**:

| Маркер | Файл:строка | Долг | Статус |
|---|---|---|---|
| `TODO(debt-D-73)` | `tuber/core/daily_metrics.py:54` | журнал прогонов YT/TG неполон → часть полей `metrics_daily` = NULL | **открыт** |
| `TODO(debt-D-57)` | `tuber/core/firstmovers.py:464` | размер мелкой стороны пары | закрыт (ТЗ-52) |
| `TODO(debt-D-51)` | `tuber/core/graph.py:1145` | редиректы кандидата-URL фида | закрыт (ТЗ-11 W3-3) |
| `TODO(debt-D-67)` | `tuber/core/metrics.py:586` | стадия, закрытая без данных, неотличима от измеренной | закрыт (ТЗ-53) |

Реестр `TECH-DEBT.md` (202 672 б): **54 строки-долга**, из них **открытых — 5**: `D-37` (семантический детектор рекламы TG), `D-52` (фильтр TG по теме), `D-71` (база сравнения каналов YouTube — остаток D-47), `D-73` (журналы прогонов YT/TG), `D-74` (смешанные форматы `story.created_at`). Остальные закрыты.

---

## 5. Тесты

Всего **169** тестовых файлов, **1897** тестов (`pytest --collect-only -q`), 37 924 строки. Конфиг: `pytest.ini:testpaths = tests`, блокируется сторонний плагин `pytest_ethereum` (`addopts = -p no:pytest_ethereum`).

Разбивка по подсистемам (замер `find -name 'test_*.py'`):

| Каталог | Тестовых файлов |
|---|---|
| `tests/x/` | 53 |
| `tests/youtube/` | 29 |
| `tests/analysis/` | 26 |
| `tests/telegram/` | 22 |
| **`tests/core/`** | **17** |
| `tests/graph/` | 11 |
| `tests/parity/` | 2 |
| `tests/migration/` | 1 |
| `tests/audit/` | 1 |

**Покрытие именно ядра** (`tests/core/`, 17 файлов, 2 312 строк):

| Файл | Строк | Что покрывает |
|---|---:|---|
| `test_schema.py` | 106 | полнота схемы (`verify_schema`) |
| `test_storage.py` | 99 | апсерты/идемпотентность/снимки |
| `test_db.py` | 109 | соединение, readonly, `write_tx` |
| `test_timeutil.py` | 46 | форматы дат |
| `test_urls.py` | 281 | синтез ссылок (D-45) |
| `test_ids.py` | 23 | идентификаторы (составной TG) |
| `test_daily_metrics.py` | 108 | суточная сводка |
| `test_health_dedup.py` | 168 | дедуп алертов |
| `test_d70_views_clamp.py` | 83 | монотонность просмотров (D-70) |
| `test_run_reliability.py` | 134 | журнал прогонов, `run_guard` |
| `test_run_log_platform.py` | 153 | платформенные `run_log` |
| `test_tz3c_sqlite_compat.py` | 218 | `CompatConnection`, TEMP-триггеры |
| `test_tz11_feeds.py` | 125 | `graph.verify_feed` |
| `test_index_hygiene.py` | 453 | индексы, дубли, конфликты |
| `test_cli.py` | 41 | диспетчер CLI |
| `test_db_backup.py` | 86 | бэкап + integrity |
| `test_llm_usage_report.py` | 79 | журнал `llm_usage` |

Граф-ядро дополнительно покрывается в `tests/graph/` (11 файлов). Проверки инвариантов: `tests/x/test_no_direct_network.py` (сеть только через брокер), `tests/telegram/test_b9_gate_owner_incremental.py` (production-гейт), `tests/core/test_d70_views_clamp.py` (единый писатель снимков).

---

## 6. Выводы

### Что крепко
1. **Владение схемой централизовано.** Единственный DDL-источник — `tuber/core/schema.py` (39 таблиц / 26 индексов / 7 вью), версия в `schema_meta` (`schema.py:19,901`), полнота проверяется `verify_schema` (`schema.py:1638`) и `tests/core/test_schema.py`. Дублирования DDL между платформами нет.
2. **Read-only legacy — жёсткая механическая гарантия, а не соглашение.** `db.py:40-52` использует URI `mode=ro` + `PRAGMA query_only`; нарушить нельзя случайным `INSERT`. Это ровно то, что исключает порчу боевых баз.
3. **Сетевой инвариант под тестом.** «Сеть только через брокер» (`broker.py:1-5`, `graph.py:14`) закреплён исполняемым тестом `test_no_direct_network.py` (ровно 1 `urlopen` во всём репо), а не только словами.
4. **Монотонность метрик сведена в одну точку.** Клэмп просмотров живёт в `storage.add_snapshot` (`storage.py:288`) — D-70 закрыт, все пути записи (X-сессия, prelim, миграции) проходят через него; есть счётчик и отдельный тест.
5. **WAL-безопасность записи.** `write_tx` (`db.py:73`) с `BEGIN IMMEDIATE` и ретраями учитывает многопроцессную запись трёх платформ — редкая для аналитического репо зрелость.
6. **Дисциплина технического долга.** Единый реестр `TECH-DEBT.md` (54 записи, `D-01…D-74`) с правилом «увидел — замаркируй, не правь молча»; закрытые помечены с замерами. Открытых — всего 5.
7. **Ядро хорошо покрыто юнит-тестами** (17 файлов, 2312 строк), включая совместимость SQLite (`test_tz3c_sqlite_compat.py`, 218 строк) и гигиену индексов (`test_index_hygiene.py`, 453 строки).

### Что хрупко
1. **Докстринг `storage.py` расходится с кодом.** Декларация «единственное место с SQL-записями ядра» (`storage.py:1`) опровергается **156** прямыми DML к таблицам ядра в `tuber/platforms/`. Границы слоя «ядро vs платформы» держатся на дисциплине, механического барьера нет.
2. **Production-гейт размножен.** `_is_production` — **7** копий, `is_production_db` — **3**, `production_gate_error` — **2**. Инвариант «боевая база» размазан по 12 точкам; правка порога/правила требует синхронного изменения всех копий, иначе одна подкоманда «протечёт» мимо гейта.
3. **Мёртвый код не вычищается.** `text_similarity` (`textsim.py:64`) и `read_ledger` (`firstmovers.py:427`) не вызываются нигде; `compat_sql_enabled` (property, `sqlcompat.py:134`) не читается. Накопление неиспользуемых публичных функций в ядре — риск дрейфа API.
4. **Незакрытый D-73 бьёт по достоверности сводок.** YouTube не пишет `run`/`run_log`, Telegram не пишет `transport_request`, поэтому `coverage`/`fail_rate`/`dup_rate`/`latency_p95_min` в `metrics_daily` честно `NULL` — «ничего не теряется молча» выполнено лишь частично, половина метрик недоступна.
5. **Незакрытый D-74 — смешанные форматы времени.** `story.created_at` пишется с `T` для `platform='cross'` и с пробелом для `'x'`, из-за чего `MAX(created_at)` по сырому столбцу врёт (замер 02.10.2026: `MAX` = `03:50`, реальный последний = `11:40`). Это подрыв инварианта И1 (единый формат дат) в конкретном столбце.
6. **`migrate_schema` вызывается на каждом соединении** из ~10 точек входа (`store.connect` всех трёх платформ, `analysis/*`, `tools/*`) — корректно благодаря гейтам по `schema_meta`, но каждая точка — потенциальный источник рассинхрона порядка «схема ДО слоя совместимости» (см. комментарий `telegram/store.py:246-248` о затенении `run_log`).
7. **Крупные файлы-монолиты.** `schema.py` (1822 стр.), `graph.py` (1455), `storage.py` (806) и особенно `tools/migrate_legacy.py` (78 960 б) концентрируют много ответственности в одном модуле — риск конфликтов при правках и сложность точечной верификации.

---

_Все числовые данные получены реальными командами (`wc`, `grep -c`, `find`, `pytest --collect-only`, `ast`-обход) 2026-10-06. Боевая база `data/tuber.db` только читалась; в код и БД изменений не вносилось._
