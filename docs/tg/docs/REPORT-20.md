# Отчёт ТЗ-20. Расписание мостов источников (Tuber-Telegram)

Дата: 15.09.2026. Репозиторий: `/root/tuber-telegram`.
Область: `scripts/`, `docs/SCHEDULE.md`, `tests/test_schedule_sync.py`, планировщик Hermes.

## Кратко

Три скрипта моста из ТЗ-17 были написаны, но **не поставлены в планировщик**:
`docs/SCHEDULE.md` называл их «рекомендация, ставит владелец», заданий в Hermes не
было. Теперь у каждого механизма моста есть обёртка, шим и задание; документ и
планировщик сверяет тест.

- **Новые обёртки:** `scripts/cron_bridge_export.sh`, `scripts/cron_bridge_import.sh`,
  `scripts/cron_bridge_discover.sh`.
- **Единый источник расписания:** блок `JOBS=(...)` в
  `scripts/install_hermes_cron.sh` (формулировка «рекомендация, ставит владелец» в
  `docs/SCHEDULE.md` заменена ссылкой на установщик).
- **Тесты:** `scripts/test_cron_bridge.sh` (12 кейсов), `tests/test_schedule_sync.py`
  (3 теста). `pytest -q`: было 23 → стало **26**, все зелёные.
- **Задания в планировщике:** `tuber_telegram_feed_export.sh` 06:30,
  `tuber_telegram_feed_import.sh` 06:35, `tuber_telegram_discover.sh` 07:40
  (действующее `tuber_telegram_collect.sh` `*/30` не тронуто).

## Что было → что стало

| | Было | Стало |
|---|---|---|
| `docs/SCHEDULE.md` | «рекомендация, ставит владелец», без привязки к установщику | ссылка на `JOBS` установщика как единственный источник истины |
| Обёртки | нет | `cron_bridge_export.sh`, `cron_bridge_import.sh`, `cron_bridge_discover.sh` |
| Установщик | нет | `install_hermes_cron.sh` (шимы + `JOBS`) |
| Шимы | нет | `tuber_telegram_feed_export.sh`, `tuber_telegram_feed_import.sh`, `tuber_telegram_discover.sh` |
| Задания | нет | 06:30, 06:35, 07:40 (зарегистрированы владельцем) |
| Тесты | нет расписания/обёрток | `test_schedule_sync.py` (+3), `test_cron_bridge.sh` (12) |

## Часть A. Обёртки заданий

Обёртки — по образцу `scripts/cron_collect.sh`: запускают скрипт проекта через
venv-питон Hermes (`/usr/local/lib/hermes-agent/venv/bin/python3`, `timeout 900`),
пишут вывод в `logs/bridge-*.log`, тихие при норме и **всегда завершаются кодом 0**.
Одна понятная строка в stdout печатается только при аномалии:

- итог не-JSON/пусто;
- `error`/`errors` непусто;
- фид не найден или пуст;
- ни один кандидат не опознан при непустом фиде (признак разрыва формата — тот
  самый случай «87 строк → 0»);
- экспорт записал 0 строк.

Идемпотентность: повторный импорт/дискавери — норма с `imported=0`, если часть
кандидатов уже известна (`skipped_existing>0`) или отсеяна фильтром. Строка
печатается только когда из фида не опознано **ни одного** кандидата.

Тестовые хуки без сети: `TG_BRIDGE_TEST_JSON`, `TG_IMPORT_TEST_JSON`,
`TG_DISCOVER_TEST_JSON`, `*_CRON_LOG`, `TG_IMPORT_FED`, `TG_BRIDGE_DB`,
`TG_IMPORT_DB`, `TG_DISCOVER_DB`, `TG_BRIDGE_OUT`, `TG_IMPORT_LIMIT`.

**Важная деталь:** у `import_candidates.py` потолок `--limit` по умолчанию 200, а в
фиде tuber-os после фильтра ~881 новых кандидата. Чтобы фид разбирался за один
ночной прогон, обёртка явно передаёт `--limit 1000` (перекрывает `TG_IMPORT_LIMIT`).

## Часть B. Установщик и единый источник расписания

`scripts/install_hermes_cron.sh`:

- блок `JOBS=(...)` — единственный источник истины (новые задания моста + действующее
  `tuber_telegram_collect.sh` `*/30`);
- создаёт **настоящие файлы-шимы** в `/root/.hermes/scripts/` (не симлинки), внутри —
  `exec` обёртки проекта;
- идемпотентен, посторонние файлы не трогает;
- `--dry-run` / `HERMES_INSTALL_DRYRUN=1` — только печать;
- печатает таблицу «шим | расписание | что делает» для регистрации владельцем.
- Задания в планировщике **регистрирует владелец**; установщик их не создаёт и в
  crontab не пишет.

## Часть C. Тест синхронизации

`tests/test_schedule_sync.py` парсит блок `JOBS` установщика и сверяет его с
фактическими заданиями проекта в `/root/.hermes/cron/jobs.json` по паре «имя
скрипта-шима + расписание». Задания проекта — все с префиксом `tuber_telegram_`.
Отсутствие `jobs.json` — `skip` с причиной. Синтетическая проверка ловит случай «то
же задание, другое время». Пути переопределяются `TUBER_TG_INSTALLER` и
`TUBER_TG_HERMES_JOBS_JSON`.

## Приёмка

Полный журнал: `docs/acceptance-log-20.txt`. Ключевые факты (рабочая база — только
чтение, прогоны на снимке `/tmp/tz20/`):

- Обёртки: `test_cron_bridge.sh` → **12 passed, 0 failed**.
- Тесты: `pytest -q` → **26 passed** (было 23).
- Установщик `--dry-run` и боевой запуск → 3 шима, симлинков 0.
- Живой **экспорт**: 239 строк (`x` 152, `youtube` 87), stdout пуст, RC 0.
- Живой **импорт** фида tuber-os: `imported 881, skipped_existing 4, skipped_filter 211`;
  повтор → `imported 0, skipped_existing 885`; реестр `channels` 155 → 1036 (+881),
  дублей handle 0.
- Живой **дискавери**: `imported 84, skipped_existing 70, skipped_filter 212,
  scanned_handles 2616`; повтор → `imported 0`; `posts` 3573 не изменилось.
- Негативы: отсутствующий фид, пустой фид, битый JSON — по одной строке, RC 0.
- Рабочая база до/после: `channels=155, posts=3573, runs=61`. `yt_keys.json` не тронут.
- Задания в планировщике: `tuber_telegram_feed_export.sh` 30 6,
  `tuber_telegram_feed_import.sh` 35 6, `tuber_telegram_discover.sh` 40 7 —
  зарегистрированы; резервная копия `jobs.json` сделана заранее.

## Артефакты

- `scripts/cron_bridge_export.sh`, `scripts/cron_bridge_import.sh`,
  `scripts/cron_bridge_discover.sh`;
- `scripts/test_cron_bridge.sh`;
- `scripts/install_hermes_cron.sh`;
- `docs/SCHEDULE.md` (обновлён);
- `tests/test_schedule_sync.py`;
- `docs/REPORT-20.md`, `docs/acceptance-log-20.txt`.

## Рекомендации

1. При росте фида выше потолка `--limit 1000` импорт растянется на несколько ночей;
   поднять `TG_IMPORT_LIMIT`.
2. Держать блок `JOBS` единственным источником расписания; тест ловит расхождение с
   планировщиком.
3. Дискавери и импорт пишут только новые строки `channels(status='candidate')`;
   существующие статусы не понижаются — сохранять это при изменениях.
