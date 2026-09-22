# tuber — единый монорепозиторий трёх платформ

`tuber` объединяет три ранее независимых проекта в один репозиторий с **одной
общей базой** `data/tuber.db`, где данные YouTube, X и Telegram лежат в общем
ядре (`source`, `content`, `metric_snapshot`, `score`, `story`, `candidate`), а
не в трёх параллельных наборах таблиц.

Составлен из (read-only, эти проекты продолжают работать в проде):
- `/root/tuber-os` — YouTube;
- `/root/tuber-x` — X (Twitter);
- `/root/tuber-telegram` — Telegram.

## Что уже сделано

* **ТЗ-1 / ТЗ-1b** — фундамент: структура репозитория, единая схема, слой
  доступа к БД, миграция данных из трёх legacy-баз, инструмент сверки `parity`.
* **ТЗ-2 / ТЗ-2b / ТЗ-2c** — YouTube: код в `tuber/platforms/youtube/`
  (адаптер `store.py` поверх ядра), команды `python3 -m tuber yt …`, тесты в
  `tests/youtube/`.
* **ТЗ-3** — X (Twitter): код в `tuber/platforms/x/` (адаптер `store.py` поверх
  ядра), команды `python3 -m tuber x …`, тесты в `tests/x/` (298 — столько же,
  сколько в `tuber-x`), обёртки расписания в `scripts/x/`, приёмка
  `scripts/acceptance/x_compare_reports.py`, отчёт `docs/REPORT-TZ3.md`.
* **ТЗ-4** — Telegram: код в `tuber/platforms/telegram/`, команды
  `python3 -m tuber tg …`, тесты в `tests/telegram/`, обёртки расписания в
  `scripts/telegram/`, отчёт `docs/REPORT-TZ4.md`.
* **ТЗ-5** — единое расписание и объединённый анализ:
  * `scripts/install_hermes_cron.sh` — **единственный** источник списка заданий
    (`JOBS` / `NEW_JOBS`), пишет шимы в `~/.hermes/scripts/`, `--dry-run`,
    идемпотентен, печатает блок «требуется регистрация владельцем» (реестр
    `jobs.json` правит владелец); тест `tests/test_schedule_sync.py`;
  * `python3 -m tuber report` — объединённая выдача из одной базы: YouTube (топ
    по просмотрам/сутки, порог 50 000 / 10 000 для ru), X (без ретвитов, лайки
    и лайки/час), Telegram (внутриканальное ранжирование), сквозной сюжет по
    `story_member` + `platform`; честные оговорки в подвале;
  * `python3 -m tuber db backup` — суточный бэкап единой базы + `PRAGMA
    integrity_check` с записью результата в `data/backups/backup.log`;
  * `scripts/cutover_prepare.sh` — подготовка переключения (снимки legacy,
    страховочная копия, пересборка из снимков, `--schema-only`, VACUUM);
  * отчёт `docs/REPORT-TZ5.md`, инструкция `docs/CUTOVER.md`.

Задачи ТЗ-1…ТЗ-5 закрыты.

## Быстрый старт

```bash
cd /root/tuber

# 1. Миграция: legacy ТОЛЬКО читаются (mode=ro). Копии для прогона — в /tmp.
python3 -m tuber migrate --target /root/tuber/data/tuber.db \
    --os /root/tuber-os/data/tuber.db \
    --x  /root/tuber-x/data/tuber_x.db \
    --tg /root/tuber-telegram/data/tuber_telegram.db

# 2. Сверка чисел legacy ↔ ядро (код 0 — сошлось, 2 — расхождение).
python3 -m tuber parity --target /root/tuber/data/tuber.db \
    --os /root/tuber-os/data/tuber.db \
    --x  /root/tuber-x/data/tuber_x.db \
    --tg /root/tuber-telegram/data/tuber_telegram.db

# 3. Отчёт X на копии и сверка с legacy-кодом (приёмка ТЗ-3).
python3 scripts/acceptance/x_compare_reports.py --date 2026-09-15 \
    --legacy-db /root/tuber-x/data/tuber_x.db --core-db /root/tuber/data/tuber.db

# 4. Telegram: приёмка ТЗ-4 (копии legacy, офлайн-скоринг, round-trip фида, parity).
python3 scripts/acceptance/telegram_tz4.py

# 5. Единое расписание: список заданий без записи и сверка с реестром Hermes.
bash scripts/install_hermes_cron.sh --dry-run
python3 -m pytest -q tests/test_schedule_sync.py

# 6. Объединённая выдача и бэкап единой базы (путь к базе — всегда явно).
python3 -m tuber report --db data/tuber.db --days 10
python3 -m tuber db backup --db data/tuber.db --keep 14

# 7. Подготовка переключения (снимки legacy, страховка, пересборка, VACUUM).
bash scripts/cutover_prepare.sh --dry-run

# 8. Тесты.
python3 -m pytest -q
```

Подкоманды платформ: `python3 -m tuber yt| x| tg <команда>`; путь к базе —
`--db PATH` (в любой позиции), `TUBER_DB` или историческое имя платформы
(`TUBER_X_DB`, `TUBER_TELEGRAM_DB`).

Команду `migrate` можно вызывать и как `python3 -m tuber tools migrate ...`.

## Структура

```
tuber/
  __init__.py __main__.py
  config.py                 # пути/настройки БД (платформенные — в следующих волнах)
  core/                     # db, timeutil, schema, storage, legacy, ids
  platforms/youtube|x/telegram/  # перенесённые коллекторы (ТЗ-2, ТЗ-3, ТЗ-4)
  analysis/report.py        # объединённая выдача по трём платформам (ТЗ-5)
  feeds/                    # каркас обмена (ТЗ-5)
  tools/migrate_legacy.py parity_report.py db_backup.py
  cli.py
config/telegram/            # scoring.json, bridge_sources.json
config/                     # наполняется в следующих волнах
docs/                       # SCHEMA-UNIFIED, MIGRATION-LEGACY, REPORT-TZ3..TZ5, CUTOVER + os/ x/ tg/
scripts/install_hermes_cron.sh   # ЕДИНОЕ расписание (ТЗ-5)
scripts/x|telegram|youtube|common/  # обёртки расписания по платформам
scripts/cutover_prepare.sh  # подготовка переключения (ТЗ-5)
scripts/acceptance/         # приёмки платформ (x_*.py, telegram_*.py)
tests/core|migration|parity|youtube|x|telegram + test_schedule_sync.py test_report_unified.py
data/                       # gitignored; здесь живёт tuber.db
```

Документация: `SCHEMA.md` (схема + карта legacy→core), `REVIEW.md` (как читать
проект), `docs/SCHEMA-UNIFIED.md`, `docs/MIGRATION-LEGACY.md`, `TECH-DEBT.md`.

## Гарантии

- Боевые базы legacy **не изменяются**: соединения только `mode=ro` +
  `PRAGMA query_only`, миграция их не копирует и не перезаписывает.
- Миграция **идемпотентна**: повторный прогон не создаёт дублей.
- Ноль сетевых вызовов и LLM: волна полностью офлайновая и воспроизводимая.
