# Расписание заданий Tuber-Telegram (ТЗ-17, ТЗ-20)

Регламент суточного обмена кандидатами между `tuber-telegram`, `tuber-os` и
`tuber-x`, а также действующего сбора постов.

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
| `tuber_telegram_feed_export.sh` | `30 6 * * *` | Экспорт фида X/YouTube (`scripts/export_candidates.py`) → `data/exchange/external_candidates.jsonl` для tuber-os/tuber-x |
| `tuber_telegram_feed_import.sh` | `35 6 * * *` | Импорт каналов (`scripts/import_candidates.py --feed /root/tuber-os/data/exchange/external_candidates.jsonl`), строки `kind="telegram"` |
| `tuber_telegram_discover.sh` | `40 7 * * *` | Дискавери t.me-хендлов из своих постов (`scripts/discover_from_posts.py`) |
| `tuber_telegram_collect.sh` | `*/30 * * * *` | Плановый сбор постов (`scripts/collect.py`, ТЗ-1) — действующее задание, не трогаем |

Порядок обязателен: **экспорт → импорт**, иначе импорт читает вчерашний фид.
Полный суточный конвейер по МСК:

1. 06:10 — `tuber-os`: экспорт фида (`tuber_os_feed_export.sh`);
2. 06:30 — `tuber-telegram`: экспорт своего фида;
3. 06:35 — `tuber-telegram`: импорт фида tuber-os (ТЗ-17/A);
4. 06:50 — `tuber-os`: импорт фида tuber-telegram;
5. 07:00 — `tuber-os`: суточный цикл (действующее задание);
6. 07:40 — `tuber-telegram`: дискавери из своих постов (ТЗ-17/B).

## Обёртки и их поведение

Каждое задание запускает обёртку проекта через venv-питон Hermes
(`/usr/local/lib/hermes-agent/venv/bin/python3`, `timeout 900`):

| Обёртка | Журнал |
|---|---|
| `scripts/cron_bridge_export.sh` | `logs/bridge-export.log` |
| `scripts/cron_bridge_import.sh` | `logs/bridge-import.log` |
| `scripts/cron_bridge_discover.sh` | `logs/bridge-discover.log` |

Обёртки тихие при норме: в stdout одна понятная строка только при аномалии (итог не
разобран, ошибка команды, фид не найден/пуст, ни один кандидат не опознан, экспорт
записал 0 строк). Всегда завершаются кодом 0: аномалия — не «сбой задания».
Тесты обёрток: `scripts/test_cron_bridge.sh`.

## Предохранители

- Рабочая база (`data/tuber_telegram.db`) — только чтение для экспорта, импорта и
  дискавери; запись идёт исключительно новыми строками `channels` со
  `status='candidate'`.
- Существующие записи во всех статусах (`active`/`private`/`dead`/`candidate`) не
  перезаписываются и не понижаются.
- Сеть и MTProto не используются: кандидаты читаются web-режимом t.me/s.
- `data/exchange/` и `logs/*.log` исключены из git (`.gitignore`).
