# Расписание заданий Tuber-OS (ТЗ-20)

Регламент суточного обмена кандидатами между `tuber-os`, `tuber-telegram` и
`tuber-x`, а также действующих заданий сбора.

**Единственный источник истины — блок `JOBS=(...)` в
`scripts/install_hermes_cron.sh`.** Таблица ниже обязана совпадать с ним; расхождение
ловит тест `tests/test_schedule_sync.py` (сверяет установщик с фактическими заданиями
планировщика Hermes `/root/.hermes/cron/jobs.json`).

Файлы-шимы в каталоге планировщика создаёт установщик
`scripts/install_hermes_cron.sh` (настоящие файлы, не симлинки; `--dry-run` — только
печать). **Регистрацию заданий в планировщике Hermes выполняет владелец** отдельно:
установщик печатает готовую таблицу «шим | расписание | что делает», но сами задания
не создаёт и в crontab не пишет. Репозиторий не трогает действующий crontab.

## Задания (время МСК)

| Шим в `/root/.hermes/scripts/` | Расписание | Что делает |
|---|---|---|
| `tuber_os_feed_export.sh` | `10 6 * * *` | Экспорт фида внешних кандидатов (`python -m tuber candidates-export`) → `data/exchange/external_candidates.jsonl` для tuber-telegram/tuber-x |
| `tuber_os_feed_import.sh` | `50 6 * * *` | Импорт YouTube-кандидатов из фида tuber-telegram (`python -m tuber candidates-import --feed /root/tuber-telegram/data/exchange/external_candidates.jsonl`) |
| `tuber_daily.sh` | `0 7 * * *` | Суточный цикл: сбор, разбор, замеры, отчёт (действующее задание, не трогаем) |
| `tuber_snapshots.sh` | `0 */3 * * *` | Замеры просмотров (действующее задание, не трогаем) |

Порядок обязателен: **экспорт → импорт**, иначе импорт читает вчерашний фид.
Полный суточный конвейер по МСК:

1. 06:10 — `tuber-os`: экспорт фида (`tuber_os_feed_export.sh`);
2. 06:30 — `tuber-telegram`: экспорт своего фида;
3. 06:35 — `tuber-telegram`: импорт фида tuber-os;
4. 06:50 — `tuber-os`: импорт фида tuber-telegram (`tuber_os_feed_import.sh`);
5. 07:00 — `tuber-os`: суточный цикл (действующее задание);
6. 07:40 — `tuber-telegram`: дискавери из своих постов.

## Обёртки и их поведение

Каждое задание запускает обёртку проекта через venv-питон Hermes
(`/usr/local/lib/hermes-agent/venv/bin/python3`, `timeout 900`):

| Обёртка | Журнал |
|---|---|
| `scripts/cron_bridge_export.sh` | `logs/bridge-export.log` |
| `scripts/cron_bridge_import.sh` | `logs/bridge-import.log` |

Обёртки тихие при норме: в stdout одна понятная строка только при аномалии (итог не
разобран, ошибка команды, фид не найден/пуст, ни один канал не разрешён, экспорт
записал 0 строк). Всегда завершаются кодом 0: аномалия — не «сбой задания».
Тесты обёрток: `scripts/test_cron_bridge.sh`.

## Предохранители

- Рабочая база (`data/tuber.db`) — только чтение для экспорта; импорт изменяет лишь
  таблицу `channel_candidates` (новые строки и `mentions`).
- Импорт — единственная сетевая операция моста (YouTube `videos.list`, 1 unit на
  видео, не больше `--limit` за прогон) и останавливается общим предохранителем квоты.
- `data/exchange/` и `logs/` исключены из git (`.gitignore`).
