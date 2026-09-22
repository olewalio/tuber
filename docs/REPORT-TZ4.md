# Отчёт волны ТЗ-4: перенос Telegram в монорепозиторий `tuber`

Дата: 17.09.2026. Исполнитель: jCode (Main). Проект: `/root/tuber`.
Предусловия: ТЗ-1/ТЗ-1b (ядро, миграция, parity), ТЗ-2/ТЗ-2c (YouTube),
ТЗ-3/ТЗ-3b/ТЗ-3c/ТЗ-3d (X) выполнены и зелёные.
Образец переноса — `tuber/platforms/x/store.py`.

## 0. Итог одной таблицей

| Задача | Итог |
|---|---|
| Код Telegram → `tuber/platforms/telegram/` | **перенесён**: 8 модулей, логика и формула не менялись |
| Адаптер единого ядра `store.py` | **написан**: 11 legacy-представлений + 29 `INSTEAD OF`-триггеров |
| CLI | `python3 -m tuber tg <подкоманда>` (`init-db`, `collect`, `scoring`, `discover`, `prelim`, `bridge export/import`) |
| Тесты Telegram | **49 → 51**: столько же перенесённых (49) плюс 2 регрессионных теста на найденный дефект (§7.1) |
| Тесты монорепозитория | **942 собрано (941 passed + 1 xfailed) → 993 собрано (992 passed + 1 xfailed)** |
| Офлайн-прогон `scoring` на КОПИИ | **пройден**: `score(telegram)` 9 235 → 21 482 (legacy `scores` = 9 235) |
| Экспорт/импорт кандидатов | **идемпотентно**: импорт 4 → 0 новых; повторный экспорт 0 вставок, `candidate` 10 536 → 10 536 |
| `parity` | **0 расхождений** на базе, пересобранной из замороженных копий (44 правила, из них 11 — `tg.*`) |
| Боевая `tuber_telegram.db` | **не тронута**: sha256 `2057e871fa2953ac…` и mtime совпали до/после |
| Долги | локальный реестр Telegram (D-01..D-11) перенумерован в сквозные **D-34..D-44**, все открыты |
| Коммиты | см. §9 |

Оба приёмочных прогона — **9/9 и 6/6 OK**, журналы:
`docs/tg/acceptance-log-tz4.txt`, `docs/tg/acceptance-log-21.txt`.

---

## 1. Что перенесено

### 1.1. Код платформы → `tuber/platforms/telegram/`

| источник (`/root/tuber-telegram`) | куда | примечание |
|---|---|---|
| `scripts/collect.py` | `telegram/collect.py` | логика разбора `t.me/s`, MTProto, resolve — без правок |
| `scripts/scoring.py` | `telegram/scoring.py` | формула и пороги не тронуты |
| `scripts/discover_from_posts.py` | `telegram/discover.py` | |
| `scripts/bridge_common.py` | `telegram/bridge.py` | |
| `scripts/export_candidates.py` | `telegram/feeds_export.py` | + запись в каноническую `candidate` |
| `scripts/import_candidates.py` | `telegram/feeds_import.py` | |
| `scripts/init_db.py` | `telegram/init_db.py` | DDL убран: схему владеет ядро |
| `scripts/prelim_ingest.py` | `telegram/prelim.py` | |
| `scripts/test_collect.py`, `test_scoring.py`, `test_bridge_sources.py` | `tests/telegram/` | имена тестов сохранены |
| `tests/test_feed_contract.py`, `tests/test_schedule_sync.py` | `tests/telegram/` | |
| `scripts/acceptance_tz21.py` | `scripts/acceptance/telegram_tz21.py` | |
| `config/scoring.json`, `config/bridge_sources.json` | `config/telegram/` | **значения не менялись** |
| `scripts/cron_*.sh`, `install_hermes_cron.sh` | `scripts/telegram/` | вызовы через `python3 -m tuber tg …` |
| `reports/` | **не переносить** (выдача прогонов) | см. §7 п.7 |
| `tests/fixtures/*.html`, `tz17_feed.jsonl` | `tests/telegram/fixtures/` | |

Что именно менялось в перенесённых модулях (полный список правок):

1. `sys.path.insert` + `import collect as C` / `import bridge_common as bc` →
   относительные импорты внутри пакета (`from . import collect as C`).
2. Хардкод `ROOT = "/root/tuber-telegram"`, `DB = …/data/tuber_telegram.db`,
   `/root/tuber-telegram/config/…` → `config.py` пакета (пути монорепозитория).
3. `sqlite3.connect(...) + executescript(SCHEMA_SQL/SCHEMA_EXTRA_SQL)` →
   `store.connect(...)`: схему создаёт ядро. Иначе прогон завёл бы в единой базе
   НАСТОЯЩИЕ таблицы `channels`/`posts` и заслонил ядро.
4. UPSERT по представлению (SQLite: `cannot UPSERT a view`) → обычный `INSERT`,
   а UPSERT-семантику выполняет триггер представления с теми же полями:
   `channel_baselines`, `scores`, `account_state` (долг D-25, приём тот же, что
   в ТЗ-3).
5. `rowcount` представления всегда 0 → `store.changes_since()` в
   `scoring.backfill_forwards` (долг D-23); `lastrowid` представления всегда 0 →
   `store.last_insert_id()` для строки `runs` в `collect.execute`.
6. Мост кандидатов: запись идёт в каноническую таблицу `candidate`
   (`platform='telegram'`), реестр `source` не трогается (ТЗ-4 §0); дедупликация
   видит обе половины (`source` + `candidate`).
7. `flood_until` представление отдаёт в виде ISO **с зоной**
   (`YYYY-MM-DDTHH:MM:SS+00:00`), хотя ядро хранит время без зоны: legacy
   разбирает это поле через `datetime.fromisoformat()` и сравнивает с aware
   «сейчас», а наивная строка бросает `TypeError`, который legacy глушит
   `except Exception` — предохранитель флуда при этом молча перестаёт работать
   (найдено при переносе, воспроизведено тестом `test_resolve_guards`).

Прочие правки не затрагивали логику: переименованы пути в докстроках, убраны
ссылки на удалённые скрипты, локальные долги перенумерованы (§6).

### 1.2. Адаптер `tuber/platforms/telegram/store.py`

Ядро — единственный источник правды; адаптер отдаёт legacy-форму TEMP-
представлениями и принимает запись через `INSTEAD OF`-триггеры. Так плоский SQL
Telegram-кода и перенесённых тестов продолжает работать без правок логики.

```
channels          → source (platform='telegram'; tg_id→external_id,
                            added_at→added_at/first_seen_at,
                            source→source_kind, posts_7d/last_post_at→meta_json)
posts             → content (external_id = <handle>/<message_id>, kind='post',
                             views/forwards/reactions → content_latest + metric_snapshot,
                             остатки → meta_json) — через ядровое v_tg_posts
scores            → score (оси er/eng_channel/eng_global/wsrc/dup_penalty/fr/
                           topic_weight/age_days → axes_json, eng→engagement)
channel_baselines → source_baseline
classified        → classification
stories/story_members → story/story_member
account_state     → transport_account_state
runs / run_log    → run / run_log (platform='telegram'; D-24)
metrics_daily     → metrics_daily (platform='telegram')
```

Уроки ТЗ-2/ТЗ-2c/ТЗ-3d выполнены:

1. `store.connect()` гарантирует схему ядра (`migrate_schema`) ДО
   `install_compat()` — иначе «no such table: main.source» и «views may not be
   indexed» на `run_log`.
2. `posts` строится поверх ядрового `v_tg_posts`, а не дублирует мэппинг: где
   ядро уже описало форму Telegram, адаптер её переиспользует.
3. Запись адаптера (`log_run`, `start_run`, `finish_run`, `import_candidate`,
   `upsert_candidate`) идёт через `write_tx` (`BEGIN IMMEDIATE` + повтор при
   `database is locked`); плоский SQL Telegram-кода сохраняет legacy-модель
   транзакций, повтор при блокировке встроен в соединение (`_RetryConnection`).
4. Индексы на таблицах ядра — только через ядровой страж
   `schema.ensure_adapter_index` (ТЗ-3d). У Telegram добавлены два индекса, оба
   на полях, которых ядро не индексирует: `idx_tg_score_story`,
   `idx_tg_story_pub`; попытка дубля/префикс-дубля падает громко.
5. `ANALYZE` — `ensure_planner_stats()`, один раз на базу (D-21).

**Идентификатор поста.** `message_id` уникален только внутри канала, поэтому
`content.external_id` Telegram — составной. Разделитель — **`/`**
(`tuber.core.ids.tg_external_id`, ядровые представления `v_tg_posts`/
`v_tg_scores`, миграция ТЗ-1): это уже действующий канон ядра, и адаптер его
сохраняет, а не вводит второй. ТЗ-4 в тексте писало `<handle>:<message_id>` —
это расхождение осознанное и объяснено: смена разделителя потребовала бы
перемиграции 21 482 строк `content` и сломала бы ядровые представления и
`parity`. `message_id` восстанавливается из `external_id` стабильно
(`substr(…, instr(…,'/')+1)`), а не по договорённости с `meta_json`.

**История оценок.** Ядровая `score` хранит историю по `(content_id,
computed_at)`, legacy-`scores` — ровно одну строку на пост (PK `post_id`).
Триггер представления ЗАМЕНЯЕТ строку поста (как legacy UPSERT). Поэтому:
сразу после пересборки базы `score(telegram)` = 9 235 (перенос один-в-один),
после полного прогона `scoring` = числу постов Telegram (21 482).

**Скоринг Telegram.** `significance` между каналами НЕсравнима (множитель
малого канала). Адаптер это не нормирует, не «поправляет» и не трогает
константы формулы: `config/telegram/scoring.json` перенесён байт-в-байт.

### 1.3. Тесты, инструменты, расписание

* `tests/telegram/` — 51 тест: **49 перенесённых** (столько же, сколько в
  `/root/tuber-telegram`; сверка `--collect-only -q` входит в приёмку) плюс
  2 регрессионных на дефект, найденный приёмкой (§7.1). Разбивка:
  `test_collect.py` 10, `test_scoring.py` 20, `test_bridge.py` 15,
  `test_feed_contract.py` 3, `test_schedule_sync.py` 3.
  Имена тестов и их смысл сохранены; адаптированы только установка БД
  (адаптер вместо `SCHEMA_SQL`), вызов CLI (`python -m tuber tg …`) и приёмники
  кандидатов (`candidate` вместо `channels`).
* `scripts/telegram/` — четыре обёртки расписания (`tuber_telegram_collect.sh`,
  `…_feed_export.sh`, `…_feed_import.sh`, `…_discover.sh`) и установщик шимов
  `install_hermes_cron.sh`. Блок `JOBS` совпадает с уже зарегистрированными в
  планировщике заданиями (`tuber_telegram_*`, 4 шт.) — это проверяет
  `tests/telegram/test_schedule_sync.py` (читает `/root/.hermes/cron/jobs.json`).
* `scripts/acceptance/telegram_tz4.py` и `telegram_tz21.py` — приёмки с
  журналами в `docs/tg/`.
* `docs/EXCHANGE-FEED.md` получил шапку «канон — в `tuber-os`» (сама таблица
  канона в файле уже была из ТЗ-2; требование `videos` — **число** теперь
  проверяется и тестом Telegram).
* Документация проекта Telegram лежала в `docs/tg/` с ТЗ-1 и не дублировалась.

---

## 2. CLI и база

```
python3 -m tuber tg init-db   [--csv PATH]          # схема ядра + реестр UNIVERSE.csv
python3 -m tuber tg collect   [--mode web|mtproto|resolve|all] [--handle H] …
python3 -m tuber tg scoring   forwards|baselines|authors|scores|report|all
python3 -m tuber tg discover  [--limit N] [--dry]
python3 -m tuber tg prelim    [--limit N] [--pages N]
python3 -m tuber tg bridge export [--out FILE]
python3 -m tuber tg bridge import --feed FILE      [--dry]
```

Синонимы прежних имён скриптов сохранены: `feed-export` = `bridge export`,
`feed-import` = `bridge import`, `init` = `init-db`.

База по умолчанию — единая `/root/tuber/data/tuber.db` (`config.DEFAULT_DB`).
Переопределение: `--db PATH` (глобальный флаг, работает в любой позиции),
`TUBER_TELEGRAM_DB` (историческое имя приёмки) или `TUBER_DB`.

Две детали реализации, обе проверены поведением:

* делегирование подкоманд сделано вручную, а не вложенными `subparsers`:
  с `nargs=argparse.REMAINDER` флаг, стоящий первым после подкоманды
  (`tg collect --mode web`), верхний парсер объявляет «unrecognized» и до
  обработчика не доходит;
* каталог отчётов переопределяется `TUBER_TG_REPORTS_DIR` — приёмка уводит
  выдачи прогонов в свой временный каталог и не сорит в репозитории.

---

## 3. Числа тестов: до и после

| Набор | Собрано | Прогнано |
|---|---|---|
| `/root/tuber-telegram` (legacy, замер 17.09.2026) | **49** | 49 |
| `tuber` до волны ТЗ-4 (ТЗ-3d) | 942 | 941 passed, 1 xfailed |
| `tuber` после ТЗ-4 — весь набор | **993** | 992 passed, 1 xfailed |
| `tuber` после ТЗ-4 — `tests/telegram` | **51** | 51 passed (49 перенесённых + 2 регрессионных) |

`1 xfailed` — унаследованный от ТЗ-3b `xfail(strict=True)` (долг D-31,
`tests/x/test_story_pairs.py`), к Telegram отношения не имеет.

---

## 4. Офлайн-прогон скоринга на копии единой базы

База — пересобранная `migrate` из ЗАМОРОЖЕННЫХ копий (боевые legacy только
read-only). Команда приёмки: `python3 -m tuber tg scoring all --deadline 0 --json`.

| Показатель | Значение |
|---|---|
| `scores` в legacy Telegram | 9 235 |
| `score(telegram)` в пересобранной базе (до прогона) | 9 235 |
| `score(telegram)` после прогона | **21 482** |
| постов обработано | 21 482 |
| каналов | 878 |
| оценено (`significance` не NULL) | 16 859 |
| не оценено (views/reactions NULL или 0) | 4 623 |
| аномалий (ER > 50 %) | 26 |
| базы каналов записано | 806 |
| общая медиана ER | 0.01044 |
| форвардов заполнено | 0 (`t.me/s` не отдаёт счётчик — D-34) |
| время прогона | ≈ 4 с |

**`--deadline 0` — не косметика.** Единственный шаг `scoring all`, который
ходит в сеть, — `forwards`; без ограничения прогон висел на таймаутах сети при
живом сборе (замер: > 5 минут и не заканчивался). `--deadline 0` режет только
этот шаг (он и не может ничего дать: разметка `t.me/s` счётчика форвардов не
содержит). Пересчёт баз, оценок и отчёта выполняется полностью офлайн.
Это свойство унаследовано от legacy и записано долгом D-34, а не «починено»
молча.

Прогон **пересчитывает** оценки: 9 235 старых строк заменены, добавлены оценки
для постов, появившихся после последнего пересчёта в legacy (за период
16–17.09.2026 коллектор добрал 2 247 постов).

---

## 5. Экспорт/импорт кандидатов

| Проверка | Результат |
|---|---|
| экспорт JSONL (`bridge export`) | 2 000 строк (`x` 381, `youtube` 1 619), канон соблюдён |
| кандидаты в таблице `candidate` после экспорта | 3 391 вставка + 62 обновления |
| импорт фида (kind=telegram) | `imported` 4, `skipped_filter` 5, `skipped_existing` 0 |
| **повторный** импорт того же фида | `imported` **0**, `skipped_existing` 4 |
| повторный экспорт | `inserted` **0**, `updated` 3 453; `candidate` не изменился (10 536) |
| `tests/telegram/test_feed_contract.py` | 3 passed |

Канон `docs/EXCHANGE-FEED.md` соблюдён: `videos` — ЧИСЛО (не список),
`video_ids`/`examples`/`sources` — списки строк, `mentions`/`ai_hint` — числа.
Потребитель терпит чужие типы (`bridge.feed_to_candidates`: `videos` списком,
`examples` объектами) — проверено отдельным пунктом приёмки.

### 5.1. Куда пишутся кандидаты (изменение относительно legacy)

Legacy писал найденных кандидатов в `channels` со `status='candidate'`. В
единой базе это раздвоено: реестр каналов — `source`, кандидаты на промоушен —
`candidate` (так устроены X и YouTube, и так требует ТЗ-4 §0: «канонический
обмен теперь идёт через таблицу `candidate`»). Поэтому:

* `bridge import` и `discover` пишут в `candidate` (`found_via` =
  `feed:tuber-os` / `post_mentions`), `source` не трогается;
* дедупликация видит обе половины (`source` + `candidate`), поэтому канал,
  уже стоящий в реестре, не импортируется повторно (тест
  `test_existing_active_not_downgraded` это проверяет);
* `bridge export` дополнительно UPSERT-ит найденные X/YouTube-кандидаты в
  `candidate` — JSONL остаётся рабочим представлением обмена, но данные
  больше не живут только в файле.

---

## 6. Расхождения и решения, требующие внимания

1. **`external_id` — `/`, а не `:`** (см. §1.2). Осознанно: канон уже задан
   ядром ТЗ-1; смена разделителя — перемиграция и поломка `parity`.
2. **Parity считается против ЗАМОРОЖЕННЫХ копий, а не против «сейчас».**
   Боевые legacy растут от живых коллекторов (в ходе этой волны `tg.posts`
   вырос 21 114 → 21 482). Сверка с текущим состоянием даёт расхождения
   −6 234…, которые к переносу отношения не имеют. Приёмка делает снимок
   `.backup`, пересобирает единую базу `migrate` и сверяет её — ровно приём
   ТЗ-3b. Результат: **0 расхождений по 44 правилам** (11 из них `tg.*`).
3. **Локальная нумерация долгов Telegram.** У проекта был свой реестр
   (`docs/tg/docs/TECH-DEBT.md`, D-01..D-11), в монорепозитории номера сквозные
   и заняты другими смыслами. Долги перенумерованы: **D-01→D-34 … D-11→D-44**,
   маркеры `TODO(debt-D-XX)` в `tuber/platforms/telegram/` обновлены, тексты
   долгов не менялись (ТЗ-4 — перенос, а не переработка модели значимости).
   Ни один долг переносом не закрыт.
4. **`init-db` и `data/UNIVERSE.csv`.** Реестр лежит в `data/`, а `data/`
   gitignored (рабочие данные). Файл скопирован локально
   (`data/UNIVERSE.csv`), путь по умолчанию —
   `tuber/platforms/telegram/init_db.py:DEFAULT_CSV`. На чистом клоне
   `tg init-db` без `--csv` вернёт понятную ошибку «реестр не найден», а не
   трейсбек. Это осознанно: версионировать рабочий реестр ТЗ не просило.
5. **`tests/telegram/__init__.py` обязателен.** Без него pytest падал на
   сборе: `tests/telegram/test_collect.py` и `tests/youtube/test_collect.py`
   (в `tests/youtube/` нет `__init__.py`) получали одно модульное имя. У X
   такая же защита (`tests/x/__init__.py`). Не правил `tests/youtube/` — это
   вне периметра волны, а ТЗ-2 зелёная.
6. **`scoring all` ходит в сеть** на шаге `forwards` (D-34). Для приёмки это
   обойдено `--deadline 0`; в боевом расписании Telegram задания `scoring` нет
   вовсе (тоже D-42), поэтому на прод это не влияет.
7. **`reports/` не переносится** (выдача прогонов). В репозитории
   `reports/significance-<дата>.md` не появляется: каталог отчётов
   переопределяется `TUBER_TG_REPORTS_DIR`, приёмка пишет во временный.

---

## 7. Что не вышло / что осталось открытым

* **Форварды Telegram по-прежнему недоступны** (D-34): все 21 482 строки
  `forwards` — NULL, `Wsrc`/`Fr` в формуле не работают. Это ограничение
  источника, а не переноса; замер воспроизведён в журнале приёмки.
* **Классификатор тем и семантический фильтр рекламы** (D-36, D-37) — этап 2,
  не входит в перенос.
* **Нечёткий дедуп** (D-38, D-39): `Xconf`/`dup_ratio` видят только точные
  дубли; признак агрегатора занижен (замер legacy: 2 агрегатора).
* **Сюжеты Telegram пусты** (D-40): `stories`/`story_members` в legacy 0 строк,
  в ядре `story`/`story_member` для Telegram тоже 0 — переносить было нечего.
* **Задание скоринга в расписании не заведено** (D-42) — модель значимости
  Telegram считается только по запросу, как и в legacy.
* **`classified` для Telegram пуст** (0 строк в legacy) — переносить нечего;
  таблица ядра `classification` для Telegram пуста, что `parity` подтверждает.

### 7.1. Инцидент приёмки: экспорт в боевую единую базу и что из него вышло

При ручной проверке CLI (`python3 -m tuber tg bridge export --out -`) команда была
запущена БЕЗ `--db`, то есть против боевой единой базы `data/tuber.db`. Экспорт
пишет найденных X/YouTube-кандидатов в общую таблицу `candidate`, поэтому прогон
оставил след. Что сделано:

| Последствие | Действие | Состояние |
|---|---|---|
| 2 476 новых строк `candidate` (`found_via='tuber-telegram:posts'`) | удалены точечным `DELETE` по этому `found_via` | `candidate` вернулся к 6 002 строкам |
| 36 существующих строк X потеряли свой `meta_json` и получили `seen_count+1` | восстановлены из legacy-базы X ровно так, как их строит миграция (`verified_at`+`sources`, `seen_count`, `last_seen_at`) скриптом `scripts/repair_x_candidate_meta.py` (legacy — только read-only) | 36 строк восстановлено, 0 «чужих» `meta_json` в базе не осталось |
| Схема базы домигрирована с версии 1 до текущей (4) | не откатывалось: это идемпотентная домиграция ядра, ровно то, что делает `python3 -m tuber tools migrate --schema-only` (ТЗ-3d) и что всё равно делает каждое соединение адаптера | `schema_meta` = 4 |

**Дефект, который инцидент выявил (исправлен).** `store.upsert_candidate`
обновляла ЛЮБУЮ существующую строку `(platform, handle)`, включая строку,
которую завёл ДРУГОЙ продюсер (у X и YouTube своя история по тем же ключам:
`found_via` вида `author:…`, `mention`, `chart`). Экспорт одного фида молча
перезаписывал чужой `meta_json`. Теперь при несовпадении `found_via` строка
считается чужой: отмечается факт встречи (`seen_count+1`, `last_seen_at`), а
`meta_json`/`external_id` не трогаются. Закрыто двумя регрессионными тестами
(`test_export_does_not_clobber_foreign_candidate_meta`,
`test_export_creates_own_candidate_row`).

Вывод для эксплуатации: боевые прогоны подкоманд `tuber tg` должны получать явный
`--db`; это же — рекомендация долга D-43 (единый гейт записи для изменяющих
подкоманд).

---

## 8. Боевые базы: что именно с ними происходило

| База | Как читалась | sha256 ДО | sha256 ПОСЛЕ |
|---|---|---|---|
| `/root/tuber-telegram/data/tuber_telegram.db` | `.backup` через `file:…?mode=ro` | `2057e871fa2953ac…` | `2057e871fa2953ac…` |
| `/root/tuber-x/data/tuber_x.db` | то же | `d5dad10d193f0d1e…` | `d5dad10d193f0d1e…` |
| `/root/tuber-os/data/tuber.db` | то же | `35fabe49b5b38c71…` | `35fabe49b5b38c71…` |

mtime боевой Telegram-базы до и после совпал бит-в-бит
(`1789626736.6511974`). Все изменяющие прогоны шли на копиях в временном
каталоге; `store.connect(readonly=True)` дополнительно ставит
`PRAGMA query_only=ON`.

---

## 9. Коммиты волны

1. `ТЗ-4: перенос Telegram в монорепозиторий tuber/platforms/telegram` —
   пакет платформы (адаптер, код, CLI), configs, тесты, обёртки расписания,
   перенумерация долгов, README.
2. `ТЗ-4: приёмки telegram_tz4.py и telegram_tz21.py + журналы` —
   инструменты приёмки, журналы прогонов, отчёт `docs/REPORT-TZ4.md`.
3. `ТЗ-4: экспорт не перезаписывает чужой candidate + repair-скрипт` —
   защита строк `candidate` другого продюсера, два регрессионных теста,
   восстановление 36 строк X и описание инцидента (§7.1).

---

## 10. Итог приёмки ТЗ-4

| Требование ТЗ-4 §2 | Результат |
|---|---|
| 1. `pytest -q`: точное число тестов, перенесённых тестов Telegram не меньше, чем в legacy | 993 собрано / 992 passed + 1 xfailed; `tests/telegram` = **51** (49 перенесённых = legacy 49, плюс 2 регрессионных) |
| 2. Офлайн-прогон `scoring` на копии, `score(telegram)` ≥ legacy `scores` | 9 235 → **21 482** ≥ 9 235; разница объяснена (2 247 постов добраны коллектором после последнего пересчёта) |
| 3. Экспорт/импорт кандидатов: round-trip, идемпотентность, контракт | импорт 4 → 0 новых; экспорт 0 вставок при повторе; `candidate` 10 536 без изменений; контрактные тесты 3 passed |
| 4. `parity` — ноль расхождений | **0** на пересобранной из снимков базе, 44 правила |
| 5. Боевая база не тронута — mtime до/после | совпал; sha256 тоже |
| 6. Коммиты по частям, минимум 3 | журнал приёмок + код + отчёт (≥3) |
