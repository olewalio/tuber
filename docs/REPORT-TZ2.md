# ТЗ-2 / ТЗ-2b — перенос YouTube в монорепо `tuber`, приёмка

Дата: 16.09.2026. Исполнитель: jCode (Main). Проект: `/root/tuber`.
Ревизия на момент отчёта: `fec66f7` (+ последний коммит этой волны, список в конце).

Отчёт отвечает на вопросы приёмки ТЗ-2b:

1. что перенесено (файлы, строки);
2. числа тестов до/после и построчная сверка с базой 518;
3. сверка отчёта «legacy против нового» на копиях боевой базы;
4. живой офлайн-прогон записи на копии единой базы;
5. `parity` на копиях;
6. неприкосновенность боевой базы;
7. медленные места и что с ними сделано;
8. что не получилось и почему; долги.

---

## 0. Сломанное состояние, с которого начали

Полный прогон на незакоммиченном состоянии: **50 failed, 192 passed, 335 errors**.

Причина у большинства — одна: `store.connect()` не создавал схему ядра, а
`install_compat()` сразу заводит представления по таблицам ядра. На пустом файле
это давало:

```
tests/youtube/test_yt.py:45: c = db.connect(tmp_path / "yt_test.db")
tuber/platforms/youtube/store.py:117: install_compat(conn)
sqlite3.OperationalError: no such table: main.source
```

Отсюда и 335 errors, и 32 падения `tests/youtube/test_cli.py` (тот же путь
`cli._connect()` → `db.connect()` → `install_compat`). В legacy `tuber.db.connect`
создавал схему сам, поэтому перенесённый код и тесты ждали от `connect()` рабочую
базу.

**Исправление** (коммит `fe0829c`): `store.connect()` зовёт `migrate_schema(conn)`
до `install_compat(conn)`. Идемпотентно, на прогретую базу ~0,2 мс
(`CREATE ... IF NOT EXISTS`). `init_db` остаётся точкой входа для справочников
(темы, пул фраз).

Результат: **578 passed, 0 failed, 0 errors** (17,8-19,9 с).

---

## 1. Что перенесено

### 1.1. Модули (коммит `64dfe84`, 14 файлов, 13 106 строк)

| Файл | Строк |
|------|-------|
| `tuber/platforms/youtube/seo.py` | 2 262 |
| `tuber/platforms/youtube/store.py` (новый адаптер хранения) | 1 530 |
| `tuber/platforms/youtube/expand.py` | 1 435 |
| `tuber/platforms/youtube/report.py` | 1 357 |
| `tuber/platforms/youtube/cli.py` | 1 302 |
| `tuber/platforms/youtube/config.py` | 946 |
| `tuber/platforms/youtube/api.py` | 891 |
| `tuber/platforms/youtube/thumbs.py` | 685 |
| `tuber/platforms/youtube/collect.py` | 683 |
| `tuber/platforms/youtube/candidates.py` | 642 |
| `tuber/platforms/youtube/classify.py` | 568 |
| `tuber/platforms/youtube/viral.py` | 370 |
| `tuber/platforms/youtube/comments.py` | 275 |
| `tuber/platforms/youtube/schedule.py` | 160 |
| **итого** | **13 106** |

`store.py` — не копия legacy `tuber/db.py`: это адаптер к ядру. Он отдаёт те же
функции (`upsert_video`, `insert_snapshot`, `save_score`, …), пишет в единое ядро
(`source`/`content`/`metric_snapshot`/`score`/…), а на соединении заводит
`TEMP`-представления с именами legacy-таблиц, поэтому сырой SQL YouTube-кода и
перенесённые тесты работают без правок. Запись — через `write_tx`
(`BEGIN IMMEDIATE` + повтор при `database is locked`).

### 1.2. Тесты (коммит `dc022c2`, 22 файла, 9 649 строк)

`tests/youtube/test_*.py`, имена файлов сохранены. Плюс `tests/youtube/test_debt_registry.py`
(инвариант нумерации долга).

### 1.3. Точка входа (коммит `12dc235`, +13 строк в `tuber/cli.py`)

`python3 -m tuber yt <подкоманда>`, путь к базе — `--db PATH` или `TUBER_DB`.
Подкоманды прежние: `report`, `collect`, `snapshots`, `classify`, `comments`,
`viral-refresh`, `migrate-shorts`, `migrate-speed`, `expand-migrate`, `expand`,
`daily`, `seo`, `candidates-export`, `candidates-import`.

---

## 2. Числа тестов: до и после, построчная сверка с 518

| Где / когда | Тестов | Падений |
|-------------|--------|---------|
| `/root/tuber-os` (база для сверки, замер 16.09.2026) | 518 | 0 |
| Монорепо до переноса YouTube (ядро ТЗ-1) | 70 | 0 |
| Ожидание ТЗ-2 (70 + 518) | 588 | 0 |
| Монорепо после переноса, до фикса `connect` | 577 собрано | 50 failed + 335 errors |
| Монорепо после фикса `connect` | **577** | 0 |
| Монорепо после возврата одного теста и правок ТЗ-2b | **578** | 0 |

Разбор: 70 = `tests/core` 43 + `tests/migration` 22 + `tests/parity` 5.
518 в `tuber-os` = 515 в перенесённых файлах + 3 в `test_schedule_sync.py`.
Итого 70 + 578-70 = 578 против ожидаемых 588 → **недостаёт 10 тестов**:

* 3 — `tests/test_schedule_sync.py` (Hermes-крон tuber-os) не переносился;
* 7 — из трёх перенесённых файлов (`test_db.py` 31→26, `test_quota_pacific.py`
  8→7, `test_quota_project.py` 12→11).

Построчно (тест → что с ним):

| Legacy-тест | Итог | Причина |
|-------------|------|---------|
| `test_db.py::test_init_db_idempotent` | заменён | `test_init_db_idempotent_and_schema_complete`: проверяет не имена legacy-таблиц, а `schema.verify_schema(c) == []` |
| `test_db.py::test_video_scores_schema_without_undefined_columns` | заменён | `test_video_scores_view_columns`: те же 11 колонок без `freshness`/`viral_score`, но у представления `video_scores` |
| `test_db.py::test_interval_748_seconds_is_short_speed_null_delta_kept` | переименован | `test_interval_748_seconds_short_speed_null_delta_kept` |
| `test_db.py::test_interval_3600_seconds_is_ok_and_speed_computed` | покрыт | граница 3600 с проверяется в `test_views_per_day_two_snapshots_known_interval` (интервал 3600 → `ok`, скорость считается) |
| `test_db.py::test_comment_checks_schema` | покрыт | колонки `comment_checks` проверяются в `tests/youtube/test_comments.py` (2 теста) |
| `test_db.py::test_comment_checks_migration_on_legacy_db` | не переносился | legacy-хелпер `db._migrate_comment_checks` (DDL по legacy-таблице) в адаптере отсутствует: колонки comment_checks живут в ядре, миграция — в `tuber/tools/migrate_legacy.py` |
| `test_db.py::test_migrate_drops_undefined_score_columns` | не переносился | удаление `freshness`/`viral_score` — операция над legacy-таблицей `video_scores`; в ядре этих колонок нет вовсе (проверяет `test_video_scores_view_columns`) |
| `test_db.py::test_query_kind_migration_on_legacy_db` | не переносился | D-04-миграция колонки `kind` у legacy `query_candidates`; действие ушло в `migrate_legacy.py`, покрыто `tests/migration/*` |
| `test_db.py::test_query_runs_migration_on_legacy_db` | не переносился | бэкфилл `runs/accepted` в legacy `query_candidates`; в ядре это поля `candidate`, а сам бэкфилл — в миграции |
| `test_db.py::test_query_fail_counts_migration_on_legacy_db` | не переносился | то же, колонка `fail_counts` legacy `query_candidates` |
| `test_db.py::test_init_db_drops_unused_empty_tables` | не переносился | **поведение утрачено** — см. долг D-19 |
| `test_db.py::test_init_db_keeps_unused_table_with_rows` | не переносился | **поведение утрачено** — см. долг D-19 |
| `test_quota_pacific.py::test_ts_migration_on_legacy_db` | не переносился | миграция колонки `ts` у legacy `quota_log`; в ядре `quota_log` — представление над агрегатом `quota_usage`, мигрировать нечего |
| `test_quota_project.py::test_quota_project_migration_on_legacy_db` | не переносился | миграция колонки `project` у legacy `quota_log`, та же причина |
| `test_quota_pacific.py::test_missing_zoneinfo_falls_back_without_crash` | **возвращён** | поведение (`_quota_tzinfo`, фикс. PST −8, одно предупреждение) живёт в `tuber/platforms/youtube/api.py`; тест перенесён в этой волне, отличие от legacy одно — имя логгера |

Новые тесты, которых в legacy не было (7): `test_get_unclassified`,
`test_install_compat_is_reentrant`, `test_mark_candidate_unresolved_then_rejected`,
`test_save_classification_and_score` (проверки адаптера) плюс три
переименования/замены из таблицы выше.

### Итоговая арифметика

```
/root/tuber-os                                  518 тестов
  из них перенесённые файлы (22 файла)          515
  из них tests/test_schedule_sync.py              3   (не переносили)

tests/youtube сейчас                            508 = 515 - 14 (не перенесены)
                                                     +  7 (новые/переименованные)
ядро ТЗ-1                                        70 = tests/core 43 + tests/migration 22
                                                     + tests/parity 5
итого монорепо                                  578 = 70 + 508

ожидание ТЗ-2 «>= 588»                          588 = 70 + 518
недостача                                        10 = 3 (schedule_sync)
                                                     + 7 (см. таблицу выше)
```

То есть каждое из 10 расхождений названо и объяснено: расхождений с базой 518
без объяснения не осталось.

---

## 3. Сверка отчёта: legacy против нового (п. 1.1 ТЗ-2b)

Все прогоны — на копиях, боевая база не трогалась.

Копии (сняты `sqlite3.backup`, 16.09.2026 ~22:38):

* `/tmp/acc-os.db` — legacy YouTube (135 946 240 байт, совпадает с боевой базой);
* `/tmp/acc-x.db`, `/tmp/acc-tg.db` — legacy X и Telegram;
* `/tmp/unified3.db` — единая база, собранная ИЗ ЭТИХ ЖЕ копий:
  `python3 -m tuber migrate --target /tmp/unified3.db --os /tmp/acc-os.db --x /tmp/acc-x.db --tg /tmp/acc-tg.db`
  (44,5 с, включая финальный `ANALYZE`).

Прогоны:

```
cd /root/tuber-os && TUBER_DB=/tmp/acc-os.db python3 -m tuber report --days 10
cd /root/tuber    && TUBER_DB=/tmp/unified3.db python3 -m tuber yt report --days 10
```

* legacy: **15 с**, 419 строк вывода;
* новое: **3 мин 4 с**, 419 строк вывода;
* `diff`: **0 расхождений**. Числа совпадают полностью, включая шапку, топы,
  счётчики, темы, комментарии, SEO.

Контрольные числа (одинаковые с обеих сторон):

* замеров в базе 93 802; видео с замером 19 636; пригодны для скорости 74 121;
  слишком короткие 45; без пары 19 636; видео со скоростью 18 352;
* «ЧТО РАСТЁТ»: видео на окне 11 702, индекс виральности посчитан у 0 (в базе
  нет `viral_index` — не запускался `viral-refresh`), лайки не собраны у 398,
  реакций нет у 837; топ пуст, отчёт штатно возвращает код 1 (пустой топ не
  должен выглядеть как «видео нет», требование п. 2.4 legacy);
* темы: «модели и релизы» — 572 видео, сумма скоростей 537 817 просмотров/сутки;
  топ-1 — «Сценарии использования GPT-6 Astra становятся абсурдными»
  (105 627 просмотров/сутки);
* «ЧТО ОБСУЖДАЮТ»: 20 позиций, совпадают; «ТЁМНЫЕ ЛОШАДКИ»: 20 позиций;
  «РУССКИЙ ЮТУБ»: 20 позиций.

Расхождений, требующих таблицы «показатель | legacy | новое | причина», нет.

Про медленную часть — раздел 7.

## 4. `parity` на копиях (п. 1.3 ТЗ-2b)

```
python3 -m tuber parity --target /tmp/unified3.db --os /tmp/acc-os.db --x /tmp/acc-x.db --tg /tmp/acc-tg.db
```

Итог: **расхождений нет** (все правила OK).

Отдельно зафиксировано: `parity` против ЖИВЫХ legacy-баз даёт 8 «расхождений»
(`x.posts` −21, `x.post_metrics_history` −21, `x.requests` −71, `x.runs` −5,
`x.run_log` −42, `tg.posts` −22, `tg.runs` −2, `tg.run_log` −124). Причина не в
переносе: боевые коллекторы X и Telegram пишут в эти базы каждую минуту, а
единая база собрана раньше. На замороженных копиях, снятых и перенесённых из
одного снимка, расхождений 0. Для приёмки сверять нужно именно копии.

## 5. Живой офлайн-прогон записи (п. 1.2 ТЗ-2b)

Копия единой базы: `/tmp/write-test.db` (снята с `/tmp/unified3.db`).

До прогона: `score` 85 771, `content` 49 683, `metric_snapshot` 109 484,
`source` 8 780, строк со `viral_index` — **0**.

```
TUBER_DB=/tmp/write-test.db python3 -m tuber yt viral-refresh
```

Вывод: видео 33 986, индекс посчитан у 13 774, NULL у 20 212 (меньше двух осей),
обновлено строк 33 986. Время 2,4 с, сеть не использовалась.

После прогона (`SELECT` по ядру):

```sql
SELECT COUNT(*) FROM score;                                                     -- 85 771 (не выросло: обновление, не вставка)
SELECT COUNT(*) FROM score WHERE json_extract(axes_json,'$.viral_index') IS NOT NULL;  -- 13 774
SELECT COUNT(*) FROM score WHERE parts_json LIKE '%"viral"%';                   -- 33 986
SELECT ROUND(MAX(json_extract(axes_json,'$.viral_index')),3) FROM score;        -- 7.862
```

Пять верхних строк (video_id, viral_index, computed_at):
`sGULyAry3-4` 7.862 (15.09 04:35), `DoNoCfb0R3U` 7.144 (16.09 04:34),
`F83KcCeHkXg` 5.165, `HrR0nVgASuM` 4.334, `2_SnQ5txHfw` 4.281.

`content`/`metric_snapshot`/`source` не изменились (запись шла только в `score`).

Копия legacy `/tmp/acc-os.db` после всех прогонов: размер 135 946 240 байт и
`mtime` 1789587486 — те же, что до; строки на месте: `videos` 33 986,
`snapshots` 93 802, `video_scores` 76 433. Ничего не менялось и не запускалось
в `/root/tuber-os`.

## 6. Неприкосновенность боевой базы (п. 1.4 ТЗ-2b)

```
до  всех прогонов: 2026-09-16 21:00:55.493858167 +0300 135946240 /root/tuber-os/data/tuber.db
после всех прогонов: 2026-09-16 21:00:55.493858167 +0300 135946240 /root/tuber-os/data/tuber.db
```

`mtime` и размер совпадают байт-в-байт. Все прогоны этого ТЗ шли на копиях в
`/tmp`; в `/root/tuber-os` не менялся ни один файл и не запускался ни один
коллектор.

## 7. Медленные места (п. 1.5 ТЗ-2b)

### 7.1. Починено: `ANALYZE` (долг D-21)

Первый прогон нового отчёта на копии единой базы: **5 мин 14 с** против 15 с у
legacy. cProfile: 311 с из 314 — в `sqlite3.Connection.execute` (1 273 вызова);
крупнейшие — `report.comment_leaders` (62 с на вызов) и `report.dark_horses`
(11 с).

Причина — не адаптер, а статистика планировщика. Представления совместимости
SQLite материализует (правило разворачивания, см. 7.2), и соединение с
материалом идёт либо автоматическим индексом, либо полным перебором. Без
`sqlite_stat1` оценка мощности неверна и планировщик берёт перебор:
`MATERIALIZE video_classification` … `SCAN vc LEFT-JOIN` = 18 260 кандидатов ×
34 471 строку разбора. С статистикой — `SEARCH vc USING AUTOMATIC COVERING INDEX`.

Замер на копии: самый тяжёлый запрос 37,7 с → **0,22 с**; отчёт целиком
5 мин 14 с → 3 мин 4 с.

Сделано: `tuber migrate` в конце гоняет `ANALYZE` (флаг `--no-analyze`), а
`store.init_db` зовёт `ensure_planner_stats()` — один раз, только если
`sqlite_stat1` в базе нет, поэтому в кроне повторно не срабатывает.

### 7.2. Не починено, записано (долг D-22): `LEFT JOIN` материализует представления

Проверено на SQLite 3.45.1 (минимальный воспроизводимый опыт): подзапрос справа
от `LEFT JOIN` разворачивается только если он ОДНОтабличный. Наши `snapshots`,
`videos`, `video_classification`, `video_scores`, `seo_fields`, `video_comments`
двухтабличные (`*` + `content` ради перевода `content_id` ⇄ `external_id`),
поэтому КАЖДЫЙ `LEFT JOIN` к ним материализует всё представление заново.

Цена, замеры на копии боевой базы:

| Операция | legacy | адаптер | отношение |
|----------|--------|---------|-----------|
| Запрос «последний замер видео» (`report.dark_horses`) | 0,070 мс | 252 мс | ×3 600 |
| `report --days 10` целиком | 15 с | 3 мин 4 с | ×12 |
| cProfile отчёта | — | 196 с, из них `dark_horses` 167 с (4 вызова: 2 потока × текст+JSON) | — |

Вывод отчёта при этом совпадает строка-в-строку, то есть это чистая
производительность. Индексами это не лечится (план `MATERIALIZE ... + SCAN`), а
переделка адаптера (read-model таблица с тригерами либо денормализация
`external_id` в ядро) — отдельная задача: в этой волне запрещено переписывать
адаптер с нуля. Направления закрытия описаны в `TECH-DEBT.md` (D-22).

До закрытия D-22 отчёт в кроне должен остаться на legacy-контуре (он там и живёт:
ТЗ-5 не начат), либо придётся мириться с 3 мин на прогон.

> **Обновление 16.09.2026 (ТЗ-2c).** D-22 закрыт денормализацией `platform` +
> `external_id` в таблицы ядра: представления стали однотабличными и
> разворачиваются планировщиком. Тот же отчёт `report --days 10` на копиях:
> **15,0 с legacy / 17,1 с адаптер** (было 3 мин 4 с), запросы «последний замер
> видео» и `report.dark_horses` — на уровне десятков микросекунд (было 252 мс).
> Замер, планы, приёмка и остаток — `docs/REPORT-TZ2c.md`.

## 8. Что не получилось и почему

1. **Полный паритет по числу тестов (588) не достигнут: 578.** 10 тестов не
   перенесены или заменены; каждый назван в разделе 2. Причины: (а) две утраты
   поведения (`init_db` больше не чистит выведенные из схемы таблицы) — заведены
   долгом D-19; (б) семь тестов проверяли миграции колонок legacy-таблиц,
   которых в ядре нет (действия уехали в `tuber/tools/migrate_legacy.py` и
   покрыты `tests/migration/*`); (в) три теста — про Hermes-крон tuber-os,
   который намеренно не переносился до ТЗ-5.
2. **Отчёт медленнее legacy в 12 раз** (3 мин 4 с против 15 с) — D-22, см. 7.2.
   Первый фактор (неучёт статистики) закрыт, второй (правило разворачивания
   `LEFT JOIN`) требует переделки адаптера.
3. **`init_db` не воспроизводит legacy-гигиену**: удаление пустых legacy-таблиц
   (`channel_baseline`, `digest_runs`, `digest_items`) и предупреждение о
   непустых больше не выполняется (D-19). На числа и отчёты не влияет, но
   поведение утрачено сознательно: чистить чужие (X/TG) таблицы в единой базе
   не должен никто до ТЗ-5.
4. **`viral_index` в боевой базе не посчитан** ни у одного видео: отчёт штатно
   возвращает код 1 с явным предупреждением. Это состояние данных, не дефект;
   на копии `viral-refresh` его считает (раздел 5).

## 9. Долги (все — в `TECH-DEBT.md`)

| ID | Суть | Статус |
|----|------|--------|
| D-14 | ось просмотров виральности без окна, среднее по наличным осям | открыт (перенесён) |
| D-15 | порог показов и потолок оси подобраны на одном срезе | открыт (перенесён) |
| D-16 | `quota_log` в ядре агрегирован, число вызовов восстанавливается разворотом | открыт (ТЗ-2) |
| D-17 | `content.is_short` не различает «неизвестно» и «полное видео» | открыт (ТЗ-2) |
| D-18 | сырой SQL YouTube-кода читает TEMP-представления адаптера | открыт (ТЗ-2) |
| D-19 | `init_db` больше не убирает выведенные из схемы таблицы | открыт (ТЗ-2b) |
| D-20 | 11 legacy-тестов не перенесено (разбор — раздел 2) | открыт (ТЗ-2b) |
| D-21 | планировщик без `sqlite_stat1` выбирает перебор | закрыт (ТЗ-2b): `ANALYZE` в `migrate` + `ensure_planner_stats()`; остаток — статистика устаревает после массовой загрузки |
| D-22 | `LEFT JOIN` с многотабличными представлениями материализует их | открыт (ТЗ-2b), замерен |

## 10. Коммиты волны ТЗ-2b

| Коммит | Что |
|--------|-----|
| `8cc7897` | ТЗ-2: аддитивные индексы под access-path представлений совместимости |
| `fe0829c` | ТЗ-2b: `connect()` гарантирует схему ядра до `install_compat()` |
| `3ed5ac6` | ТЗ-2b: вернуть тест фолбэка без tzdata |
| `c7cf405` | ТЗ-2b: реестр долгов YouTube/адаптера, сверка переноса тестов |
| `6bcd7b9` | ТЗ-2b: `ANALYZE` один раз (D-21) |
| `fec66f7` | ТЗ-2b: документы приёмки — `docs/REPORT-TZ2.md` |
| последний | ТЗ-2b: долг D-22 (материализация `LEFT JOIN`) в реестр |

## 11. Как воспроизвести приёмку

```bash
# копии боевых баз (read-only по боевым)
python3 - <<'PY'
import sqlite3
for src, dst in [("/root/tuber-os/data/tuber.db","/tmp/acc-os.db"),
                 ("/root/tuber-x/data/tuber_x.db","/tmp/acc-x.db"),
                 ("/root/tuber-telegram/data/tuber_telegram.db","/tmp/acc-tg.db")]:
    c = sqlite3.connect(src); o = sqlite3.connect(dst)
    with o: c.backup(o)
PY

# единая база из копий
python3 -m tuber migrate --target /tmp/unified3.db --os /tmp/acc-os.db \
    --x /tmp/acc-x.db --tg /tmp/acc-tg.db

# паритет: ожидается «расхождений нет»
python3 -m tuber parity --target /tmp/unified3.db --os /tmp/acc-os.db \
    --x /tmp/acc-x.db --tg /tmp/acc-tg.db

# сверка отчётов: вывод должен совпасть строка-в-строку
(cd /root/tuber-os && TUBER_DB=/tmp/acc-os.db python3 -m tuber report --days 10) > /tmp/rep_legacy.txt
TUBER_DB=/tmp/unified3.db python3 -m tuber yt report --days 10 > /tmp/rep_new.txt
diff /tmp/rep_legacy.txt /tmp/rep_new.txt

# запись на копии
cp /tmp/unified3.db /tmp/write-test.db
TUBER_DB=/tmp/write-test.db python3 -m tuber yt viral-refresh

# тесты
python3 -m pytest -q          # ожидается 578 passed
```
