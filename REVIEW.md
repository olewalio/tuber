# REVIEW — «что за проект и как читать»

Этот файл — для внешнего ревьюера. Он объясняет, зачем проект, что уже сделано,
где смотреть и как проверить за 10 минут.

## Что это

`tuber` — единый монорепозиторий для аналитики трёх платформ (YouTube, X,
Telegram) на **одной общей базе**. До волны ТЗ-1 данные лежали в трёх отдельных
проектах с тремя отдельными базами. ТЗ-1 строит фундамент: одну схему, один слой
доступа к БД, миграцию данных и сверку чисел.

**Границы волн.** ТЗ-1…ТЗ-5 закрыты: коллекторы всех трёх платформ перенесены
(`tuber yt|x|tg …`), расписание сведено в один установщик, выдача объединена
(`tuber report`), бэкап единой базы — `tuber db backup`. Старые проекты и их
базы остаются на диске как страховка, переключение кронов выполняет владелец
(см. `docs/CUTOVER.md`).

## Порядок чтения

1. `README.md` — быстрый старт.
2. `SCHEMA.md` — единая схема и **карта legacy → core** (ключевой раздел).
3. `tuber/core/schema.py` — DDL ядра (источник истины).
4. `tuber/core/db.py` — соединение + `write_tx` (транзакции с повтором при блокировке).
5. `tuber/core/storage.py` — идемпотентные upsert-функции ядра.
6. `tuber/tools/migrate_legacy.py` — миграция (legacy read-only).
7. `tuber/tools/parity_report.py` — сверка чисел.
8. `tuber/analysis/report.py` — объединённая выдача по трём платформам (ТЗ-5).
9. `tuber/tools/db_backup.py` — бэкап единой базы + integrity (ТЗ-5).
10. `scripts/install_hermes_cron.sh` — единое расписание (ТЗ-5).
11. `tests/` — как заявленные свойства проверяются.
12. `TECH-DEBT.md` — осознанные компромиссы и пробелы (D-01..D-32).
13. `docs/CUTOVER.md` — что переключено и как откатиться.

## Как проверить (10 минут)

```bash
cd /root/tuber
python3 -m pytest -q                     # ожидается: все тесты проходят
bash scripts/install_hermes_cron.sh --dry-run   # список заданий без записи

# Копии боевых баз (read-only .backup), миграция и сверка на копиях:
bash scripts/acceptance_tz1.sh           # см. docs/MIGRATION-LEGACY.md
```

Ключевые утверждения и где они проверяются:

| Утверждение | Где проверяется |
|---|---|
| Схема ядра полна (таблицы/индексы/вью/колонки), включая негативный тест | `tests/core/test_schema.py` |
| `write_tx` коммитит, откатывает при исключении, повторяет при `database is locked` | `tests/core/test_db.py` |
| Даты: epoch/ISO/`Z`/пусто | `tests/core/test_timeutil.py` |
| Миграция идемпотентна, `legacy_map` заполнена, флаги маппятся явно | `tests/migration/test_migration.py` |
| Домерживание новых колонок и `display_handle`/`meta_json`/`cursor`/`classify_daily` | `tests/migration/test_migration.py` (ТЗ-1b) |
| Legacy-базы открыты только на чтение; файлы не меняются | `tests/migration/test_migration.py` |
| `parity` молчит на верных данных и ловит расхождение (код 2) | `tests/parity/test_parity.py` |
| Единое расписание совпадает с реестром; новое задание = ожидание, не ошибка | `tests/test_schedule_sync.py` |
| Установщик пишет реальные файлы, идемпотентен, удаляет устаревшие, dry-run ничего не пишет | `tests/test_schedule_sync.py` |
| Секции `tuber report`: порог YouTube, отсев ретвитов X, внутриканальный Telegram, сквозной сюжет | `tests/test_report_unified.py` |
| `tuber db backup`: копия, integrity в journal, ошибка на отсутствующую базу, ротация | `tests/core/test_db_backup.py` |

## Известные ограничения (честно)

- Долги ТЗ-1 D-01..D-06 **закрыты** в ТЗ-1b: `display_handle` (D-01),
  `legacy_map.target_id` TEXT (D-02), `title_ru/summary_ru/reason` (D-03),
  `classify_daily` (D-04), `cursor` (D-05), `candidate.meta_json` (D-06).
  Подробности и проверки — `TECH-DEBT.md`, карта — `SCHEMA.md`.
- Поля без колонки в ядре уезжают в `meta_json` (не теряются): например
  `candidate.meta_json`, `content.meta_json`, `source.meta_json`.
- `x.cursors(kind='account')` записывается и в `cursor`, и в `source.cursor`
  (совместимость); канонический источник — `cursor` (см. `SCHEMA.md`).
