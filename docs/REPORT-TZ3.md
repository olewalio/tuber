# Отчёт волны ТЗ-3: перенос X (Twitter) в монорепозиторий `tuber`

Дата: 16.09.2026. Исполнитель: jCode (Main). Предусловия: ТЗ-1 (ядро/схема/
миграция/parity) и ТЗ-2/ТЗ-2c (каркас переноса YouTube, адаптер `store.py`)
выполнены и зелёные.

Образец переноса — `tuber/platforms/youtube/store.py` (ТЗ-2). Схема та же:
логика X не переписывается, меняется только слой доступа к БД, импорты и пути.

---

## 1. Что перенесено

### 1.1. Код платформы → `tuber/platforms/x/`

| источник (`/root/tuber-x`) | куда | примечание |
|---|---|---|
| `tuber_x/nitter_broker.py` | `tuber/platforms/x/broker.py` | переименован; ссылки в коде и тестах обновлены |
| `tuber_x/collect.py` | `tuber/platforms/x/collect.py` | |
| `tuber_x/channels.py` | `tuber/platforms/x/channels.py` | |
| `tuber_x/discover.py` | `tuber/platforms/x/discover.py` | |
| `tuber_x/registry.py` | `tuber/platforms/x/registry.py` | |
| `tuber_x/enrich.py` | `tuber/platforms/x/enrich.py` | |
| `tuber_x/health.py` | `tuber/platforms/x/health.py` | |
| `tuber_x/scoring.py` | `tuber/platforms/x/scoring.py` | формулы не тронуты |
| `tuber_x/scores.py` | `tuber/platforms/x/scores.py` | |
| `tuber_x/stories.py` | `tuber/platforms/x/stories.py` | кластеризация не тронута |
| `tuber_x/report.py` | `tuber/platforms/x/report.py` | |
| `tuber_x/classify.py` | `tuber/platforms/x/classify.py` | |
| `tuber_x/feeds.py` | `tuber/platforms/x/feeds.py` | |
| `tuber_x/ai_filter.py` | `tuber/platforms/x/ai_filter.py` | |
| `tuber_x/blocklist.py` | `tuber/platforms/x/blocklist.py` | |
| `tuber_x/seeds.py` | `tuber/platforms/x/seeds.py` | |
| `tuber_x/config.py` | `tuber/platforms/x/config.py` | пути: `ROOT` = корень монорепо, `DB_PATH` = единая база |
| `tuber_x/cli.py` | `tuber/platforms/x/cli.py` | подкоманды `python3 -m tuber x …` |
| `tuber_x/db.py` | **не перенесён** | заменён адаптером `tuber/platforms/x/store.py` |

Что именно менялось в перенесённых модулях (полный список правок):

1. `from . import …, db, …` → `…, store as db, …` (адаптер играет роль прежнего
   `tuber_x/db.py`: тот же набор функций и констант).
2. `from .nitter_broker import …` → `from .broker import …` (файл переименован).
3. Пути в `config.py` (см. §2).
4. Пять мест, где код опирался на `cursor.rowcount` (обход представлений, долг
   D-23 в `TECH-DEBT.md`): `collect.insert_posts`, `enrich.apply_metrics`,
   `enrich.mark_deleted`, `blocklist.remove`, `scores._purge_stale` — теперь
   `store.changes_since()`.
5. Пять `INSERT … ON CONFLICT … DO UPDATE` по представлениям (SQLite: `cannot
   UPSERT a view`) — внешний `ON CONFLICT` убран, UPSERT-семантику выполняет
   триггер представления с теми же `excluded.*` (долг D-25).
6. `stories.run`: `story_id = cur.lastrowid` → `store.last_insert_id(con)`
   (`lastrowid` у представления всегда 0; настоящий id возвращает триггер через
   `temp.x_last_insert`).

### 1.2. Адаптер `tuber/platforms/x/store.py`

Ядро — единственный источник правды; адаптер отдаёт legacy-форму TEMP-
представлениями и принимает запись через `INSTEAD OF`-триггеры. Так плоский SQL
X-кода (и перенесённых тестов) продолжает работать без правок.

```
accounts              → source (platform='x'; x_id→external_id; added_at→first_seen_at;
                               source_type→source_kind; last_attempt_at/ai_density_src/
                               provisional_since/reject_reason/last_reject_at/promo_path → meta_json)
posts                 → content (tweet_id→external_id, kind='post', is_retweet→is_repost,
                                 likes/replies/metrics_at/metrics_src/pinned/… → meta_json,
                                 author_handle — отдельная колонка ядра, как и требует ТЗ)
post_metrics_history  → metric_snapshot
candidates            → candidate (sources/verified_at/feed_source → meta_json)
classified            → classify_cache (tweet_id → meta_json) + проекция в classification
classify_daily        → classify_daily (суммирующий UPSERT)
stories / story_posts → story / story_member (handle — колонка ядра, см. D-26)
scores                → score (специфичные оси → axes_json; significance/branch/anomaly/
                                metrics_missing/metrics_at/metrics_age_hours → колонки)
cursors               → cursor
instances             → transport_instance (collect_fail_streak/reserve_since → meta_json)
requests              → transport_request
runs / run_log        → run / run_log
metrics_daily         → metrics_daily (cdn_429_count/synd_429_count/ssr_used/
                                stale_lag_p95_min → extra_json)
report_texts          → report_text
blocklist             → blocklist (platform='x')
darks                 → source.meta_json.$.darks
```

Уроки ТЗ-2/ТЗ-2c выполнены:

1. `store.connect()` гарантирует схему ядра (`migrate_schema`) ДО
   `install_compat()` — иначе «no such table: main.source».
2. Представления **однотабличные**: `content`/`score`/`metric_snapshot`/… читаются
   напрямую (денормализованные `external_id`/`platform` уже есть в ядре).
   `LEFT JOIN`-планы с представлениями сняты, `MATERIALIZE` в типовых запросах
   отчёта нет (замер — §6).
3. Запись из функций адаптера (`log_run`, `start_run`, `finish_run`,
   `mark_reserve_active`, …) идёт через `write_tx` (`BEGIN IMMEDIATE` + повтор).
   Плоский SQL X-кода сохраняет legacy-модель транзакций (явные
   `commit()`/`rollback()`, в том числе `feeds.import_candidates(dry=True)`);
   повтор при `database is locked` встроен в соединение
   (`_RetryConnection`), `busy_timeout` = 30 с (как в legacy).
4. `ANALYZE` — `ensure_planner_stats()`, один раз на базу (D-21).
5. Порядок миграции ядра не менялся; адаптер перед `migrate_schema` снимает
   TEMP-слой (иначе TEMP-представление `run_log` затеняет одноимённую таблицу
   ядра и «views may not be indexed»).

Формат дат: ядро хранит `YYYY-MM-DD HH:MM:SS`, legacy X — `…T…`. Представления
отдают формат X, триггеры пишут формат ядра, поэтому диапазонные сравнения
внутри X-кода (`published_at_utc >= db.iso(...)`) остались корректными без
единой правки логики.

### 1.3. Тесты, инструменты, документация

* `tests/*.py` (34 файла, 298 тестов) → `tests/x/*.py`, **имена сохранены**;
  адаптированы: импорты (`store as db`, `tests.x.*`, `broker`), пути
  (`ROOT` = корень монорепо, `docs/x/docs/*`), четыре теста legacy-миграций
  схемы (`test_db_schema`, `test_db_migration_v3`, `test_tz5`, `test_tz18`,
  `test_viral_fix`) переписаны на наблюдаемые свойства адаптера, ожидания
  аргументов запускалок (`-m tuber x …`) и путь шимов (`scripts/x/…`).
* `tests/x/` лежит в пакете (`__init__.py`), помогает `tests/x/mocking.py`,
  `tests/x/story_eval.py`, фикстуры `tests/x/fixtures/*`.
* `tools/acceptance_tz*.py`, `tools/audit_writeback.py`, `tools/measure_tz10.py`
  → `scripts/acceptance/x_*.py` (+ новый `x_compare_reports.py` — приёмка ТЗ-3).
* `scripts/backfill_authors.py` → `scripts/x_backfill_authors.py`.
* Обёртки расписания (`tuber_x_*.sh`, `install_hermes_cron.sh`) → `scripts/x/`
  (пути и вызов CLI обновлены; единый установщик для трёх платформ — ТЗ-5).
* Документация проекта X перенесена в `docs/x/` (в `docs/x/docs/` — отчёты и
  расписание, `docs/x/acceptance-log-*.txt` — журналы приёмок).

---

## 2. CLI и база

```
python3 -m tuber x <подкоманда>        # все прежние команды tuber_x.cli с теми же именами/флагами
python3 -m tuber x report --date 2026-09-15 --stdout-only
```

База по умолчанию — единая `/root/tuber/data/tuber.db`
(`tuber.config.db_path()`). Переопределение сохранено полностью: `--db PATH`,
`TUBER_DB`, историческое `TUBER_X_DB` (порядок: `TUBER_X_DB` → `TUBER_DB` →
`default.ini` → `data/tuber.db`).

---

## 3. Числа тестов: до и после

| Набор | Собрано | Прогнано |
|---|---|---|
| `/root/tuber-x` (legacy, замер 16.09.2026) | **298** | 297 passed, 1 failed |
| `tuber` до волны ТЗ-3 (ТЗ-2c) | 598 | 598 passed |
| `tuber` после ТЗ-3 — только `tests/x` | **298** | 297 passed, 1 failed |
| `tuber` после ТЗ-3 — весь монорепозиторий | **896** | 895 passed, 1 failed |

Требование «не меньше 298» выполнено: 298 = 298, ни один legacy-тест X не
выброшен.

**Единственное падение — legacy-дефект, воспроизведённый 1:1.**
`tests/x/test_story_pairs.py::test_required_different_pairs_not_glued` падает
одинаково в обоих проектах на одном и том же наборе:

```
AssertionError: 2099679599542382696 и 2099793800147390574 склеены, а это разные события
  assert 4 != 4
```

Это не регрессия переноса: то же сообщение, те же id, то же место
(`test_story_pairs.py:80`) воспроизводится в `/root/tuber-x`. Набор
`tests/x/data/*.jsonl` собран из живого корпуса и лежит в `.gitignore`
(`data/`), поэтому на чистом клоне тесты этого модуля **скипаются с явной
причиной** (проверено: 4 skipped, причина печатается через `-rs`), а не падают
трейсбеком — требование §3.1 выполнено.

Дополнительно в волне закрыт доступ к реестру долгов: маркеры
`TODO(debt-D-XX)` перенесённого X ссылались на номера ЛОКАЛЬНОГО X-реестра
(D-09/D-10). Они перенумерованы в сквозные монорепозиторные (D-28, D-29), иначе
падал инвариант `tests/youtube/test_debt_registry.py` (номер занят другим долгом).

---

## 4. Сверка отчётов: legacy ↔ монорепозиторий

Методика — `scripts/acceptance/x_compare_reports.py` (приёмка §3.2/§3.3/§3.5):

1. обе базы копируются `sqlite3 .backup` (боевые файлы только читаются);
2. X-часть копии ядра подмигрируется тем же инструментом ТЗ-1 (иначе сравнение
   шло бы с устаревшим срезом: единая база снята 16.09.2026 21:15, а боевой X
   писал ещё и после — 810 постов против 768);
3. `parity` по правилам X на копиях;
4. отчёт legacy-кодом и новым кодом за одну дату, построчный диф;
5. замер времени.

Дата 2026-09-15 (полные сутки: сюжеты, оценки, выдача). Снимок боевой X-базы — `data/acceptance/x_legacy_copy.db` от 00:06:05 MSK (21:06:05 UTC), 810 постов.

**Результат сверки (полный вывод — `docs/x/acceptance-log-tz3.txt`):**

| Пункт | Legacy | Монорепо | Итог |
|---|---|---|---|
| строк отчёта | 62 | 62 | совпало |
| расхождение построчно | — | 1 строка из 62 | причина — долг D-27 (тайбрейк) |
| `parity` (17 правил X) | — | 17 × `+0`, расхождений нет | **0 расхождений** |
| время отчёта | 0.13 с | 0.17–0.18 с | **1.32–1.41×** (< 2×) |

Единственное расхождение (дословно):

```
-   Запуск: инференс-сервис; кто: @Cointelegraph; 2026-09-15 13:25 UTC;
    https://x.com/Cointelegraph/status/2099851980843925762
+   Запуск: GPU-аренда; кто: @Cointelegraph; 2026-09-15 13:25 UTC;
    https://x.com/Cointelegraph/status/2099851981557035395
```

Таблица расхождений с причинами:

| # | Блок отчёта | Суть | Причина | Статус |
|---|---|---|---|---|
| 1 | 3. Деньги и запуски | выбран `echo`-пост сюжета 87 вместо `primary` | у двух постов сюжета ОДНА секунда публикации (`13:25:00`), а запрос отчёта сортирует только по времени (`ORDER BY p.published_at_utc ASC LIMIT 1`) — выбор диктует план; legacy-план шёл по `story_posts(tweet_id)`, план адаптера — по `story_member(story_id, content_id)` | долг **D-27**, рекомендация: второй ключ сортировки (`tweet_id`) |
| 2 | 6. Тёмные лошадки | `@iamlukethedev` 6 сюжетов вместо 5, лишняя строка `@melvininvests` | автор поста брался из ТЕКУЩЕЙ строки `content`, а legacy фиксировал автора в момент кластеризации; CDN-обогащение переписывает `author_handle` POST ФАКТУМ | **исправлено в волне**: аддитивная `story_member.handle`, миграция её переносит (долг **D-26**, закрыт) |
| 3 | 7. Служебный блок (постов за сутки) | 168 против 167 | единая база снята до последнего прогона сбора X; при домиграции из боевой копии расхождение исчезает | снято методикой (домиграция копии) |

Что ещё проверено в волне на копиях (все — `+0`): `x.accounts` 72, `x.posts` 810,
`x.post_metrics_history` 809, `x.scores` 103, `x.stories` 100, `x.story_posts` 123,
`x.classified` 598, `x.classify_daily` 2, `x.cursors` 85, `x.candidates` 1407,
`x.requests` 2693, `x.instances` 4, `x.runs` 166, `x.run_log` 1610,
`x.metrics_daily` 2, `x.report_texts` 1, `x.blocklist` 0.

Примечание про ожидаемый расхождения «до переноса»: в отчёте 05/09 `spread`
(ось «распространение» в X мертва: `xconf=1` у 91 из 103 оценок,
`spread = max(0, xconf-1)`, ретвиты не приходят) — числа до и после совпали
(`x.scores` 103, суммы по `significance` те же), дефект по ТЗ не «чинился».

---

## 5. Боевые базы: что именно с ними происходило

| Файл | mtime на старте волны | mtime на конец | Кто изменил |
|---|---|---|---|
| `/root/tuber/data/tuber.db` | `2026-09-16 21:15:28 +0300` | **тот же** | никто (md5 `5fea7703…` совпал) |
| `/root/tuber-x/data/tuber_x.db` | `2026-09-17 00:06:05 +0300` | `2026-09-17 00:15:59 +0300` | **боевой крон проекта `tuber-x`**, не волна ТЗ-3 |

Про X-базу — честно и с доказательствами, потому что требование §3.4 звучит как
«mtime до/после» и одним «не менялся» тут не отделаться:

1. В 00:15:59 MSK в базу реально добавились данные: `posts` 810 → **811**,
   `requests` 2693 → **2700**, свежий пост опубликован `2026-09-16T21:03:33Z`.
   Волна ТЗ-3 ничего в неё не пишет: все замеры, отчёты и миграции шли на
   копиях (`sqlite3 .backup`, `data/acceptance/*.db`, `$JCODE_SCRATCH_DIR`), а
   боевой файл открывался только на чтение.
2. Время совпадает с боевым заданием Hermes: `/root/.hermes/cron/jobs.json`
   содержит `tuber_x_collect_b.sh` с расписанием `15 */4 * * *` — то есть ровно
   00:15 MSK. Это сбор тира B, он и записал 1 пост и 7 запросов.
3. Контрольный опыт на копии (безопасный): запись → закрытие соединения →
   повторное открытие `read-write` и `SELECT` → `close()` — mtime и md5 НЕ
   меняются (чекпоинт WAL происходит на закрытии соединения, которое что-то
   писало, а не на читающем открытии). Значит, read-write-открытие само по себе
   файл не портит.

**Методологическая правка по ходу приёмки.** Первый прогон `EXPLAIN QUERY PLAN`
для legacy-базы открывал боевой файл соединением по умолчанию (read-write, без
URI `mode=ro`) — запросы были только читающие, файл не изменился (см. п. 3), но
это неправильная методика. Инструмент исправлен: legacy-база открывается как
`file:…?mode=ro`, замеры сняты повторно на КОПИИ
(`data/acceptance/x_legacy_copy.db`); планы и времена совпали с первым замером.
То же требование теперь соблюдает и `install_compat()` адаптера: на read-only
соединении аддитивные индексы пропускаются (TEMP-представления создаются).

Снимок для приёмки: `data/acceptance/x_legacy_copy.db` от 00:06:05 MSK
(21:06:05 UTC) — 810 постов; все числа §4 относятся к нему.

---

## 6. Скорость и планы запросов (урок D-22)

Отчёт целиком (тот же корпус, та же дата): **legacy 0.13 с → монорепо
0.17–0.18 с, отношение 1.32–1.41×** (порог ТЗ — 2×; вердикт `OK`).

`EXPLAIN QUERY PLAN` трёх типовых запросов отчёта (боевые копии,
`story_id=87`, окно суток):

| Запрос | План монорепо | `MATERIALIZE` | Строк | Время |
|---|---|---|---|---|
| `_primary_post` (первый пост сюжета) | `SEARCH sm USING COVERING INDEX sqlite_autoindex_story_member_1` + `SEARCH content USING INDEX sqlite_autoindex_content_1` + скалярные подзапросы | **нет** | 1 | 0.7 мс (legacy 0.2 мс) |
| окно сюжетов (`posts × accounts × classified`) | `SEARCH c USING INDEX idx_x_content_pub (platform=?)` + `SEARCH classify_cache USING INDEX` | **нет** | 57 | 4.5 мс (legacy 3.3 мс) |
| «Деньги и запуски» (`stories × story_posts × posts × classified`) | `SEARCH s USING INDEX idx_x_story_xconf` + `SEARCH sm USING COVERING INDEX` + скалярные подзапросы | **нет** | 7 | 0.9 мс (legacy 0.3 мс) |

Ни один план не содержит `MATERIALIZE` (требование §3.5). Однотабличные
представления + аддитивные индексы `idx_x_*` дают `SEARCH`, а не полный перебор.
Абсолютные времена запросов — доли миллисекунды; различие в 1.3–3× на уровне
шумов одного прогона (полный отчёт уложился в 1.4×).

Полный текст планов — в журнале приёмки `docs/x/acceptance-log-tz3.txt`.

---

## 7. Что не вышло / что осталось открытым

1. **Одна строка отчёта расходится** (блок 3, долг D-27): тайбрейк в SQL отчёта.
   Правка — в слое логики, поэтому вынесена в долг с рекомендацией, а не сделана
   молча в волне переноса.
2. **Приёмочные инструменты `scripts/acceptance/x_tz*.py` не прогоняются
   сквозняком** на единой базе (долг D-30): они читают боевую базу сырым
   `sqlite3` и ждут legacy-форму, которую даёт только адаптер на соединении.
   Все 14 файлов перенесены, разбираются и получили новые импорты/пути; сквозной
   прогон — после переключения контура X на единую базу (ТЗ-5).
3. **`run_log` в ядре не имеет `platform`** (долг D-24): строки X с
   `run_id IS NULL` показываются в представлении X; при переносе Telegram их надо
   будет различать.
4. **Ось `spread` в X не работает** (ретвиты не приходят) — известный дефект
   проекта, по ТЗ не чинился; перенос числа не изменил.
5. **Долги, перенесённые вместе с кодом:** D-28 (граф распространения — запрос
   на каждый пост) и D-29 (бэкфилл автора требует CDN).
6. `test_story_pairs.py::test_required_different_pairs_not_glued` падает на
   размеченном наборе — воспроизведённый legacy-дефект кластеризации (D-08 в
   реестре X), не регрессия.

---

## 8. Замечание про боевой контур

Боевая `/root/tuber/data/tuber.db` живёт на **версии схемы 1** (`schema_meta`:
`version=1`; нет `cursor`, `classify_daily`, денормализованных `*_ext`-колонок).
Это не дефект адаптера: `store.connect()` поднимает схему до текущей
(`migrate_schema`, путь ТЗ-2c) при первом соединении. Волна ТЗ-3 сознательно НЕ
делала этого для боевой базы: она пишется живым кроном Telegram, а переключение
X-крона на единый контур — отдельная волна (ТЗ-5, маркер
`TODO(debt-hermes-cron)`). До переключения боевой X продолжает работать в
`/root/tuber-x` на своей базе, как и раньше.

---

## 9. Коммиты волны

| Коммит | Содержание |
|---|---|
| `3ea30b5` | ТЗ-3: перенос X в `tuber/platforms/x` + адаптер `store.py` поверх ядра (слой БД, CLI, аддитивная `classify_cache.meta_json`) |
| `c648af1` | ТЗ-3: тесты X → `tests/x` (298), обёртки → `scripts/x`, инструменты → `scripts/acceptance`, документация X в `docs/x` |
| `ff906cf` | ТЗ-3: `story_member.handle` (D-26) + реестр долгов D-23..D-30 + отчёт `docs/REPORT-TZ3.md` |
| `9b64dba` | ТЗ-3: глобальный `--db` у подкоманд X (путь к единой базе в любой позиции) |

---

## 10. Итог приёмки ТЗ-3

| Требование | Итог |
|---|---|
| §3.1 тесты: не меньше 298, `story_pairs` скипается без набора | ✅ 298 собрано, 297 passed + 1 legacy-failed; skip проверен |
| §3.2 отчёты на копиях, числа совпадают, расхождения — таблицей | ✅ 62/62 строки, 1 строка расходится (D-27, причина показана), блок «Тёмные лошадки» исправлен (D-26) |
| §3.3 `parity` — ноль расхождений | ✅ 17 правил X, 0 расхождений |
| §3.4 боевые базы не тронуты (mtime) | ✅ единая база — байт-в-байт без изменений; X-базу изменил боевой крон (`tuber_x_collect_b.sh`, 00:15:59 MSK, +1 пост/+7 запросов), волна к ней не писала (разбор и доказательства — §5) |
| §3.5 скорость + `EXPLAIN` без `MATERIALIZE` | ✅ 1.32–1.41× (< 2×), `MATERIALIZE` нет ни в одном плане |
| §3.6 коммиты по частям (≥ 3) | ✅ три коммита (см. §9) |
| §3.7 офлайн-тесты | ✅ сеть в тестах не используется (транспорт подменяется; `test_no_direct_network` — инвариант) |
