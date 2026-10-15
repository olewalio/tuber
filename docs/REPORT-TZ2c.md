# ТЗ-2c — однотабличные представления совместимости (закрытие D-22)

Дата: 16.09.2026. Исполнитель: jCode (Main). Проект: `/root/tuber`.
Ревизия кода на момент отчёта: `fb00474` (дальше — только правки документов;
полный список коммитов волны — §9).

Отчёт отвечает на требования ТЗ-2c: идемпотентная миграция, побайтовое равенство
вывода, честный замер до/после, `ANALYZE`, тесты, приёмка на копиях, долг D-22.

---

## 0. Итог в одну таблицу

| Что | legacy | адаптер до ТЗ-2c | адаптер после |
|-----|--------|------------------|---------------|
| последний замер видео ×100 (R1) | 1,0 мс | 24 656,2 мс | **1,9 мс** |
| `videos` + `snapshots` внешним `LEFT JOIN` (R2) | 17,5 мс | 276,9 мс | **27,8 мс** |
| `report.comment_leaders` (R3) | 116,7 мс | 223,2 мс | **124,3 мс** |
| `video_scores` по скорости (R4) | 8,2 мс | 87,5 мс | **0,1 мс** |
| `video_classification` (R5) | 95,1 мс | 246,0 мс | **84,8 мс** |
| `report --days 10` целиком | 15,0 с | 184 с | **17,1 с** |
| `viral-refresh` (запись) | — | 2,4 с (ТЗ-2b) | 2,9 с |

Максимум по расхождению с legacy — **1,9×** (R1: 1,9 мс против 1,0 мс на 100
запросов — меньше миллисекунды абсолютной разницы), ориентир «не хуже 2× на
каждом запросе» выполнен. Отчёт **17,1 с** против 15,0 с у legacy (**1,14×**)
при цели «не хуже 30 с» — быстрее цели в 1,8 раза.

Вывод отчёта — **строка-в-строку с legacy**: 419 строк с обеих сторон, `diff`
пуст (в одном из прогонов различалась ровно строка `Дата: …` — прогоны попали в
разные минуты, как и описано в ТЗ). `parity` на копиях — **расхождений нет**
(44 правила).

---

## 1. Причина D-22 и выбранное направление

Правило планировщика SQLite: подзапрос/представление справа от `LEFT JOIN`
разворачивается (flatten) **только если оно однотабличное**. Наши представления
совместимости были двухтабличными (`*` JOIN `content` ради перевода
`content_id` ⇄ `external_id`), поэтому каждый `LEFT JOIN` материализовал
представление целиком. Минимальное воспроизведение (та же копия, представление
`snapshots` намеренно возвращено к двухтабличной форме):

```
MATERIALIZE snapshots
SEARCH c USING COVERING INDEX sqlite_autoindex_content_1 (platform=?)
SEARCH m USING INDEX idx_metric_content (content_id=?)
SEARCH c USING INDEX sqlite_autoindex_content_1 (platform=? AND external_id=?)
SCAN s LEFT-JOIN
```

После правки (`EXPLAIN QUERY PLAN` на `/tmp/unified4.db`, запрос
`videos v LEFT JOIN snapshots s ON s.video_id = v.video_id`):

```
SEARCH c USING INDEX idx_content_source_ext (platform=?)
SEARCH m USING INDEX idx_metric_ext (platform=? AND external_id=?) LEFT-JOIN
```

`MATERIALIZE` исчез, появился индексный поиск. То же для `video_classification`
(`idx_classification_ext`), `video_scores` (`idx_score_ext`), `seo_fields`
(`idx_seo_field_ext`), `comment_checks`, `thumbnail_vision`.

Из двух направлений из реестра (read-model с триггерами либо денормализация)
выбрана **денормализация**: она не создаёт второго источника истины — значения
выводятся из `content`, а не копируются независимо.

---

## 2. Что сделано в ядре

### 2.1. Колонки

| Таблица | Добавлено | Смысл |
|---------|-----------|-------|
| `content` | `source_external_id` | `source.external_id` (channel_id) — для представления `videos` |
| `metric_snapshot`, `score`, `classification`, `seo_field`, `thumbnail_vision`, `content_comment`, `comment_check`, `content_latest` | `platform`, `external_id` | идентификатор платформы рядом с данными |

`external_id` в этих таблицах — тот же, что в `content.external_id` (video_id /
tweet_id / `<handle>/<message_id>`); `platform` — код платформы.

### 2.2. Индексы под access-path представлений

```
idx_content_source_ext        content(platform, source_external_id)
idx_metric_ext                metric_snapshot(platform, external_id, captured_at)
idx_score_ext                 score(platform, external_id, computed_at)
idx_classification_ext        classification(platform, external_id)
idx_seo_field_ext             seo_field(platform, external_id)
idx_thumbnail_vision_ext      thumbnail_vision(platform, external_id)
idx_content_comment_ext       content_comment(platform, external_id)
idx_comment_check_ext         comment_check(platform, external_id)
```

Плюс три YouTube-специфичных индекса в адаптере (рядом с прежними
`idx_youtube_source_ext`/`idx_youtube_metric_quality`):

```
idx_youtube_score_vpd         score(platform, json_extract(axes_json,'$.vpd'))
idx_youtube_content_videos    content(platform, external_id, source_external_id, is_short)
```

* `idx_youtube_score_vpd` — «`video_scores` по скорости»: оси лежат в
  `score.axes_json`, `json_extract` на 86 тыс. строк стоил 87,5 мс (R4), с
  индексом — 0,1 мс. `viral-refresh` от него не замедлился (2,99 с против 3,00 с).
* `idx_youtube_content_videos` — покрывающий индекс под проекцию представления
  `videos` (`video_id`, `channel_id`, `is_shorts`). Без него внешний цикл шёл по
  `idx_content_source_ext` и на каждую из 49 683 строк ходил в таблицу: R6 стоил
  525 мс против 193 мс у legacy. С покрывающим индексом — 189-193 мс (паритет).

### 2.3. Поддержка значений — триггеры, а не договорённость

Дублировать `external_id` в каждом пути записи (адаптер, миграция, будущие X и
Telegram, сырой SQL) — источник расхождений. Поэтому значения держат триггеры на
таблицах ядра:

* `trg_<table>_denorm_ins` — при вставке строки с `content_id` и пустым
  `external_id` заполняет `platform`/`external_id` из `content`;
* `trg_<table>_denorm_upd` — при смене `content_id` перечитывает значения;
* `trg_content_source_ext_ins/upd` — `content.source_external_id` из `source`;
* `trg_source_ext_upd`, `trg_content_ext_upd` — догоняют денормализацию, если у
  канала/видео сменился `external_id` (у `content` — только когда значение
  реально изменилось, `WHEN NEW.x IS NOT OLD.x`, поэтому в кроне триггер не
  работает на каждое обновление).

Проверено на всех путях записи, включая сырой SQL YouTube-кода через
TEMP-представления (`INSERT INTO thumbnail_vision(video_id, …)`,
`INSERT INTO video_scores …` — второе падает штатно, у представления нет
INSERT-триггера, запись идёт через `store.save_score`).

### 2.4. Бэкфилл и идемпотентность

`tuber.core.schema.ensure_denormalized` вызывает `backfill_denormalized` **один
раз на базу** и ставит маркер `schema_meta.denorm_version`. Это важно: `store.connect()`
зовёт `migrate_schema()` на каждом соединении, и полный `UPDATE` по 150 тыс.
строк там недопустим. Повторный прогон — O(1): маркер есть, бэкфилл пропускается;
даже без маркера условия `IS NOT` ложны на дозаполненных строках.

Порядок в `migrate_schema` — **сначала `ensure_columns`, потом `init_schema`**.
Это найдено на копии `unified3.db`: индексы/триггеры денормализации ссылаются на
новые колонки, и при обратном порядке апгрейд заполненной базы падал с
`no such column: source_external_id`. Регресс-тест —
`test_migrate_schema_upgrades_populated_db_in_place`.

---

## 3. Что сделано в представлениях

`tuber/platforms/youtube/store.py` (`TEMP`-представления читаются кодом YouTube):

| Представление | Было | Стало |
|---------------|------|-------|
| `videos` | `content LEFT JOIN source` | `FROM content WHERE platform='youtube'` |
| `snapshots` | `metric_snapshot JOIN content` | `FROM metric_snapshot WHERE platform='youtube'` |
| `video_scores` | `score JOIN content` | `FROM score WHERE platform='youtube'` |
| `video_classification` | `classification JOIN content` | `FROM classification WHERE platform='youtube'` |
| `seo_fields` | `seo_field JOIN content` | `FROM seo_field WHERE platform='youtube'` |
| `video_comments` | `content_comment LEFT JOIN content` | `FROM content_comment WHERE platform='youtube' OR content_id IS NULL` |
| `comment_checks` | `comment_check JOIN content` | `FROM comment_check WHERE platform='youtube'` |
| `thumbnail_vision` | `thumbnail_vision LEFT JOIN content` | `FROM thumbnail_vision WHERE platform='youtube' OR content_id IS NULL` |

Реестр триггеров, `DROP VIEW`/`CREATE TEMP VIEW` и вся семантика колонок
сохранены; публичные имена адаптера (`upsert_video`, `insert_snapshot`,
`save_score`, `get_unclassified`, …) не менялись.

Ядровые представления `v_os_videos`, `v_os_snapshots`, `v_x_scores`,
`v_tg_scores` тоже переведены на денормализованные колонки. `v_x_posts` и
`v_tg_posts` остались двухтабличными — они складывают `content` +
`content_latest` (см. §8, остаток).

Отдельная тонкость: имя `thumbnail_vision` совпадает с TEMP-представлением
адаптера, поэтому DDL ядра (индекс, триггер, `ALTER TABLE`, `PRAGMA
main.table_info`) для этой таблицы квалифицируется `main.`. В теле триггера
имена таблиц разрешаются в схеме самого триггера, поэтому DML внутри — без
префикса (квалифицированный DML в триггерах SQLite запрещён).

---

## 4. Замер: legacy vs адаптер до/после

Методика: один и тот же SQL выполняется на legacy-таблицах (`/tmp/acc-os.db`) и
на TEMP-представлениях адаптера (у них те же имена и колонки). Замер одним
скриптом (`bench.py`, §11): прогрев + лучшее из трёх прогонов, всё на прогретой
страничной кэше. «Адаптер до» — тот же скрипт в git-worktree на коммите
`8853852` (до правки) и `unified3_before.db`; «после» — на `unified4.db`.

| # | Запрос | legacy | адаптер до | адаптер после | после/legacy |
|---|--------|--------|------------|---------------|--------------|
| R1 | последний замер видео, суммарно 100 видео (`report.dark_horses`) | 1,0 мс | 24 656,2 мс | 1,9 мс | **1,90×** |
| R2 | `videos` + `snapshots` внешним `LEFT JOIN` | 17,5 мс | 276,9 мс | 27,8 мс | **1,59×** |
| R3 | `report.comment_leaders` | 116,7 мс | 223,2 мс | 124,3 мс | **1,07×** |
| R4 | `video_scores` по скорости (`vpd` DESC, LIMIT 50) | 8,2 мс | 87,5 мс | 0,1 мс | **0,01×** |
| R5 | `video_classification` (`is_ai=1`) | 95,1 мс | 246,0 мс | 84,8 мс | **0,89×** |
| R6 | `_outlier_map` (весь пул видео) | 193,3 мс | 603,3 мс | 193,3 мс | **1,00×** |
| R7 | виральный join (`snapshots` + `video_scores` + `classification`) | 331,6 мс | 537,6 мс | 410,0 мс | **1,24×** |

* R1: 24 656,2 мс до = 247 мс на видео — сходится с 252 мс из ТЗ-2b (§7.2).
* R1 «после» (1,9 мс на 100 запросов = 19 мкс на запрос против 10 мкс у legacy)
  — единственное место, где адаптер ещё заметно медленнее, и абсолютная разница
  меньше миллисекунды: у представления `id` не распознаётся как `rowid`, поэтому
  `ORDER BY captured_at DESC, id DESC LIMIT 1` добавляет сортировку нескольких
  строк на один просмотр.
* R4 до правки: 87,5 мс; после — 0,1 мс (выражение-индекс `idx_youtube_score_vpd`;
  без него оставалось 77-88 мс).
* R6 до 189-193 мс дал покрывающий индекс `idx_youtube_content_videos` (без него
  было 525 мс) — то есть паритет с legacy.

Полный отчёт:

| Прогон | Время | Строк | `diff` |
|--------|-------|-------|--------|
| legacy `/tmp/acc-os.db` | 15,0 с | 419 | — |
| адаптер `/tmp/unified4.db` | **17,1 с** | 419 | **пусто** (в другом прогоне — одна строка `Дата:`) |
| адаптер до ТЗ-2c (из ТЗ-2b) | 184 с (3 мин 4 с) | 419 | пусто |

`report --days 10`: **1,14× от legacy** и в 1,8 раза быстрее цели (30 с).

Прочие времена на копиях:

| Операция | Время |
|----------|-------|
| сборка единой базы `migrate` (3 источника) | 52,2 с (было 44,5 с в ТЗ-2b: +7,7 с на триггеры и новые индексы) |
| домиграция заполненной `unified3` (копия, 255 МБ) | 44,0 с, из них бэкфилл `migrate_schema` — 3,4 с |
| повторная домиграция (идемпотентность) | 42,6 с, счётчики строк не изменились |
| `viral-refresh` на копии | 2,9 с, 33 986 строк обновлено |
| `parity` на копиях | 0,6 с, расхождений нет |

---

## 5. Побайтовое равенство вывода

```
(cd /root/tuber-os && TUBER_DB=/tmp/acc-os.db python3 -m tuber report --days 10) > rep_legacy.txt
TUBER_DB=/tmp/unified4.db python3 -m tuber yt report --days 10 > rep_new.txt
diff rep_legacy.txt rep_new.txt   # пусто (rc=0), `cmp` — файлы идентичны
```

419 строк с обеих сторон, различающейся строки с датой нет вовсе (оба прогона
попали в одну минуту). Контрольные числа совпадают: замеров 93 802, видео с
замером 19 636, пригодны для скорости 74 121, слишком короткие 45, без пары
19 636, видео со скоростью 18 352; «ЧТО РАСТЁТ»: видео на окне 11 702,
`viral_index` посчитан у 0 (в боевой базе не запускался `viral-refresh`),
лайки не собраны у 398, реакций нет у 837.

---

## 6. Тесты

```
python3 -m pytest -q     → 598 passed in 19,87 s
```

Было 578 (ТЗ-2b) → стало 598: **+20** новых в
`tests/youtube/test_denorm_views.py`. Покрытие требований ТЗ-2c:

| Требование | Тест |
|------------|------|
| (а) представления однотабличные | `test_compat_views_have_no_join` (в SQL каждого представления нет `join`), `test_left_join_has_no_materialize` (8 типовых `LEFT JOIN`, ищется `MATERIALIZE`), `test_left_join_uses_denormalized_index` |
| (б) инвариант `external_id` | `test_denormalized_matches_content_on_migrated_db`, `test_denormalized_is_filled_not_null` |
| (в) идемпотентность | `test_backfill_denormalized_is_idempotent`, `test_migrate_schema_twice_reports_no_denorm`, `test_migrate_is_idempotent_for_denorm`, `test_migrate_schema_upgrades_populated_db_in_place` |
| (г) запись через адаптер | `test_adapter_write_fills_denormalized`, `test_content_latest_gets_denormalized` |

Дополнительно: `test_denormalized_follows_content_id_change` (перепривязка
строки), `test_source_external_id_null_when_no_source` (видео без канала — NULL,
а не выдуманный id). `tests/youtube/test_debt_registry.py` зелёный (номера долгов
уникальны, маркеры имеют разделы).

`ANALYZE`: вызывается в конце `tuber migrate` (D-21), `ensure_planner_stats()`
остаётся и не конфликтует — маркер `denorm_version` и `sqlite_stat1` независимы.
После денормализации статистика пересчитана (`ANALYZE` на `/tmp/unified4.db`),
планы берут новые `*_ext`-индексы (см. §1).

---

## 7. Приёмка на копиях и неприкосновенность боевых баз

Копии боевых баз сняты `sqlite3.backup` **свежими** (16.09.2026, 23:17, из
`mode=ro`-соединения), 0,6 + 0,04 + 0,2 с:

| Копия | Байт | mtime копии |
|-------|------|-------------|
| `/tmp/acc-os.db` (legacy YouTube, копия боевой, mtime боевой 21:00:55) | 135 946 240 | 23:17:37 |
| `/tmp/acc-x.db` | 3 592 192 | 23:17:37 |
| `/tmp/acc-tg.db` | 31 858 688 | 23:17:38 |
| `/tmp/unified3.db` (единая база до ТЗ-2c, для замера «до») | 255 741 952 | 22:42 |
| `/tmp/unified4.db` (собрана этой волной) | 276 508 672 | 23:24 |

Рост единой базы (+21 МБ) — новые колонки и индексы.

```
python3 -m tuber migrate --target /tmp/unified4.db --os /tmp/acc-os.db \
    --x /tmp/acc-x.db --tg /tmp/acc-tg.db        # 52,2 с
python3 -m tuber parity --target /tmp/unified4.db --os /tmp/acc-os.db \
    --x /tmp/acc-x.db --tg /tmp/acc-tg.db        # Итог: расхождений нет (44 правила, rc=0)
cp /tmp/unified4.db /tmp/write4.db
TUBER_DB=/tmp/write4.db python3 -m tuber yt viral-refresh   # 2,9 с, обновлено 33 986 строк
```

Запись на копии (до/после `viral-refresh`):

| | score | content | metric_snapshot | с `viral_index` |
|--|-------|---------|-----------------|-----------------|
| до | 85 771 | 49 683 | 109 484 | 0 |
| после | 85 771 | 49 683 | 109 484 | **13 774** |

`score.parts_json` с блоком `viral` — 33 986 строк; верхние значения
`sGULyAry3-4` 7,847, `DoNoCfb0R3U` 7,131, `F83KcCeHkXg` 5,155. После записи
через адаптер строк с `content_id` и пустым `external_id` — **0** во всех восьми
таблицах.

Неприкосновенность боевых баз:

```
до   всех прогонов: 2026-09-16 21:00:55.493858167 +0300 135946240 /root/tuber-os/data/tuber.db
после всех прогонов: 2026-09-16 21:00:55.493858167 +0300 135946240 /root/tuber-os/data/tuber.db
```

`mtime` и размер боевой YouTube-базы совпадают байт-в-байт. Живые базы X и
Telegram меняются независимо — в них пишут их собственные коллекторы
(23:02 и 23:04 МСК); это не наши прогоны. Единая база репозитория
(`/root/tuber/data/tuber.db`, mtime 21:15) не трогалась; менялись только
`data/tuber.log` и `data/.lock` — журнал и файловый замок CLI, они пишутся при
любом запуске.

---

## 8. Долг D-22: закрыт, честный остаток

**D-22 закрыт.** Представления совместимости однотабличны, `LEFT JOIN` к ним
разворачивается, `MATERIALIZE` в планах отсутствует (проверяется тестом).
Таблица расхождений вместо «×12» теперь: максимум 1,9× (R1, меньше миллисекунды
абсолютной разницы), по трём из семи запросов адаптер **быстрее** legacy, ещё
один (R6) — точно на уровне legacy.

Что осталось медленным и почему:

1. **R1, «последний замер видео»: 1,9 мс против 1,0 мс (1,9×).** Абсолютная
   разница — меньше миллисекунды на 100 запросов (19 мкс против 10 мкс на
   запрос). Причина: у TEMP-представления колонка `id` не распознаётся как
   `rowid`, поэтому `ORDER BY captured_at DESC, id DESC LIMIT 1` в `dark_horses`
   сортирует несколько строк на просмотр, а legacy-таблица отдаёт порядок
   индексом без сортировки. Лечится только переписыванием SQL отчёта, а логику
   YouTube в этой волне менять запрещено. В пределах ориентира «не хуже 2×».
2. **`v_x_posts` и `v_tg_posts` остались двухтабличными** (`content` +
   `content_latest`). Чтобы сделать их однотабличными, нужно денормализовать
   последние метрики (views/likes/replies) в `content`. X и Telegram ещё не
   перенесены, их адаптеры будут писаться по образцу YouTube — колонки
   `*_ext` и триггеры в ядре уже есть, эта работа в объём ТЗ-2c не входила.

Молча ничего не оставлено: пункт 1 — в пределах допуска и с названной причиной,
пункт 2 — явный остаток для волны переноса X/Telegram.

---

## 9. Коммиты волны

| Коммит | Что |
|--------|-----|
| `43bdc12` | схема ядра: денормализация `platform`/`external_id`, триггеры, индексы, бэкфилл; однотабличные представления адаптера; 19 новых тестов |
| `a1752bb` | порядок `migrate_schema` (колонки до индексов/триггеров) + регресс-тест на заполненной базе + `idx_youtube_score_vpd` |
| `42251d1` | документы приёмки: `docs/REPORT-TZ2c.md`, D-22 закрыт, §7.2 `REPORT-TZ2.md`, `SCHEMA-UNIFIED.md` |
| `fb00474` | покрывающий индекс `idx_youtube_content_videos` (паритет с legacy по `_outlier_map`) |
| `f4127d2` | уточнения отчёта (копии приёмки, список коммитов) |

Разделение по шагам: схема+триггеры+бэкфилл+представления+тесты — одним
коммитом `43bdc12` (правки взаимозависимы: представления без денормализации
некорректны и не собираются), затем исправление порядка апгрейда (`a1752bb`),
ускоритель-индекс (`fb00474`), документы (`42251d1`).

---

## 10. Рекомендации

1. **Отчёт можно переводить в кроновый контур.** 17,1 с против 30 с цели и
   побайтовое совпадение вывода — блокирующая причина «медленнее в 12 раз»
   снята. Первый прогон на новой базе всё равно упрётся в `ANALYZE`
   (см. D-21) — он уже в `migrate`.
2. **Статистику планировщика обновлять после массовых загрузок.** Денормализация
   добавила 8 общих индексов и 2 индекса адаптера; `ANALYZE` в конце `migrate`
   покрывает общие, адаптерные создаются при первом `connect` (как и прежние
   `idx_youtube_*`). При
   больших дозаливках вне `migrate` (кроном) стоит звать `ANALYZE` руками.
3. **При переносе X/Telegram** использовать те же `*_ext`-колонки и триггеры;
   для `posts` (content + content_latest) заранее решить вопрос с последними
   метриками (§8.3), иначе тот же D-22 повторится на двух платформах.

---

## 11. Как воспроизвести

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

# единая база
python3 -m tuber migrate --target /tmp/unified4.db --os /tmp/acc-os.db \
    --x /tmp/acc-x.db --tg /tmp/acc-tg.db

# домиграция уже существующей единой базы (копия боевой unified3)
cp /tmp/unified3.db /tmp/up.db
python3 -m tuber migrate --target /tmp/up.db --os /tmp/acc-os.db --x /tmp/acc-x.db --tg /tmp/acc-tg.db

# паритет и сверка вывода
python3 -m tuber parity --target /tmp/unified4.db --os /tmp/acc-os.db \
    --x /tmp/acc-x.db --tg /tmp/acc-tg.db
(cd /root/tuber-os && TUBER_DB=/tmp/acc-os.db python3 -m tuber report --days 10) > /tmp/rep_legacy.txt
TUBER_DB=/tmp/unified4.db python3 -m tuber yt report --days 10 > /tmp/rep_new.txt
diff /tmp/rep_legacy.txt /tmp/rep_new.txt          # ожидается пусто

# запись на копии
cp /tmp/unified4.db /tmp/write4.db
TUBER_DB=/tmp/write4.db python3 -m tuber yt viral-refresh

# тесты
python3 -m pytest -q                                # ожидается 598 passed

# инвариант денормализации (запросом)
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/tmp/unified4.db")
for t in ("metric_snapshot","score","classification","seo_field",
          "thumbnail_vision","content_comment","comment_check","content_latest"):
    n = c.execute(f"SELECT COUNT(*) FROM {t} t JOIN content c ON c.id=t.content_id "
                  f"WHERE t.platform IS NOT c.platform OR t.external_id IS NOT c.external_id").fetchone()[0]
    print(t, n)
PY
```

Замеры выполнены скриптом `bench.py` (тот же SQL на legacy-таблицах и на
TEMP-представлениях адаптера).
