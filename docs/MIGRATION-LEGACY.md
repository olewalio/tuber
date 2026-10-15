# MIGRATION-LEGACY — перенос данных из трёх баз в единое ядро

## Как запускать

```bash
cd /root/tuber

# Боевой прогон (legacy читаются только mode=ro):
python3 -m tuber migrate \
    --target /root/tuber/data/tuber.db \
    --os /root/tuber-os/data/tuber.db \
    --x  /root/tuber-x/data/tuber_x.db \
    --tg /root/tuber-telegram/data/tuber_telegram.db
```

Допустимы подмножества источников (`--os`/`--x`/`--tg` в любой комбинации).
Эквивалентная форма — `python3 -m tuber tools migrate ...`.

## Правила (обязательные)

1. **Legacy read-only.** Соединения открываются как `file:<path>?mode=ro` и
   дополнительно получают `PRAGMA query_only=ON`. Любая запись по legacy
   завершается ошибкой драйвера (проверено тестом).
2. **Идемпотентность.** Повторный прогон не создаёт дублей: upsert по
   естественным ключам + пропуск уже смаппленных строк в `legacy_map`. Числа
   после второго прогона не меняются (тест `test_idempotent_second_run`).
3. **`legacy_map`.** Каждая переехавшая строка пишет `старый id → новый id`.
4. **Порядок.** platform → source → content → metric_snapshot →
   classification/classify_cache → score → story/story_member → остальное.
5. **Даты.** epoch → ISO через `timeutil`; ISO-строки нормализуются.
6. **Без выдумывания.** Чего нет в legacy — остаётся `NULL`.
7. **Сводка.** В конце печатается таблица «legacy-таблица → target-таблица: строк».

## Домиграция на уже заполненную базу (ТЗ-1b)

Схема расширяется аддитивно. `tuber.core.schema.migrate_schema` (вызывается
`migrate` перед переносом) приводит существующую базу к текущей версии:

- `ALTER TABLE ... ADD COLUMN` для новых колонок (`classification.title_ru/
  summary_ru/reason`, `candidate.display_handle/meta_json`) — с проверкой наличия,
  повторный запуск ничего не меняет;
- создание новых таблиц `cursor`, `classify_daily` (`CREATE TABLE IF NOT EXISTS`);
- пересборка `legacy_map` c `target_id INTEGER` на `TEXT` (SQLite не умеет
  `ALTER COLUMN`), данные копируются один-в-один.

Данные **домерживаются**: повторный прогон миграции заполняет новые поля в уже
существующих строках (upsert по естественным ключам), а не только расширяет
схему. Проверка — раздел `4b` в `scripts/acceptance_tz1.sh` (повторная миграция
на том же файле: числа не меняются, новые колонки непусты).

## Копии для прогона (рекомендуется)

Боевые базы живут и меняются (кроны коллекторов), поэтому для приёмки и
экспериментов берите согласованный снимок через `sqlite3 .backup` (открывает
источник read-only и пишет копию в `/tmp`):

```bash
sqlite3 "file:/root/tuber-os/data/tuber.db?mode=ro" -readonly ".backup /tmp/copy-os.db"
sqlite3 "file:/root/tuber-x/data/tuber_x.db?mode=ro" -readonly ".backup /tmp/copy-x.db"
sqlite3 "file:/root/tuber-telegram/data/tuber_telegram.db?mode=ro" -readonly ".backup /tmp/copy-tg.db"
```

Затем мигрируйте и сверяйте на копиях — так `было`/`стало` считаются по одному и
тому же срезу. Готовый сценарий: `scripts/acceptance_tz1.sh`.

## Сверка (parity)

```bash
python3 -m tuber parity --target /tmp/tuber-unified-test.db \
    --os /tmp/copy-os.db --x /tmp/copy-x.db --tg /tmp/copy-tg.db
```

Коды возврата: `0` — всё сошлось, `2` — есть необъяснённое расхождение.
Объяснимые расхождения можно зафиксировать файлом `--known-diffs known.json`
(`{"правило": "причина"}`); по умолчанию список пуст и ожидается ноль расхождений.

## Известные источники расхождений

- Сироты в legacy: снапшот без видео или метрики без поста — такие строки
  пропускаются (в боевых базах на 16.09.2026 их 0). В отчёте они видны как
  дельта у `os.snapshots` / `x.post_metrics_history`.
- `os.quota_log` сравнивается по `SUM(calls)`, потому что строки агрегируются по
  `(key_id, day, endpoint)`.

## Приёмка ТЗ-1 (что предъявляется)

`scripts/acceptance_tz1.sh` выполняет и печатает:

1. снимки копий боевых баз (`sha256` + `mtime` до/после — доказательство, что
   боевые базы не тронуты);
2. миграцию копий в `/tmp/tuber-unified-test.db` со сводкой;
3. `parity` на этом файле (ожидается код 0);
4. повторную миграцию на том же файле (идемпотентность + домерживание новых полей);
5. числа закрытия долгов: `title_ru`/`summary_ru`/`reason` ядро = legacy,
   `cursor` = 85 (21 account + 64 search), `classify_daily` = 2;
6. три показательных SQL-запроса к единой базе (доказывают единое ядро);
7. число строк `legacy_map` и число строк ядра по платформам.
