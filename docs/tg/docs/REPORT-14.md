# ТЗ-14 — отчёт: разделены «единичная повторённая ошибка» и «сбой сбора»

Дата: 15.09.2026 (МСК). Исполнитель: jCode. Проект: `/root/tuber-telegram/`.
Задание планировщика: `fd9f54e2b9bd` (Tuber-Telegram, сбор постов web), расписание `*/30 * * * *`.

## 0. Предусловие (проверено до начала работ)

`/root/.hermes/cron/jobs.json`, задание `fd9f54e2b9bd`:

```json
"enabled": false,
"state": "paused",
"paused_at": "2026-09-15T22:11:21.373471+03:00",
"last_run_at": "2026-09-15T22:05:14.758522+03:00",
"last_status": "ok"
```

Задание действительно на паузе, живого сбора нет — работа начата. Рабочая база
`data/tuber_telegram.db` использовалась только для чтения (`?mode=ro`), писать в неё запрещено
и не пришлось. Состояние cron-заданий не менялось.

## 1. Что было

Итоговая JSON-строка прогона 15.09.2026 22:05 (из `logs/cron-2026-09-15.log`) — успех:
60 из 60 каналов, 0 сбоев, 5 новых постов, 2645 обновлений, одна транзиентная сетевая ошибка:

```json
{"mode": "web", "channels_ok": 60, "channels_fail": 0, "posts_new": 5, "posts_upd": 2645, "errors": 1, "duration_sec": 281.79, "flood_until": null}
```

Единственное событие — `denissexy`, повтор через 8 секунд успешен (`logs/collect-2026-09-15.log`):

```
2026-09-15 19:01:36 [error] denissexy: request error (RemoteProtocolError): Server disconnected without sending a response.
2026-09-15 19:01:44 [info]  denissexy: ok pages=5 new=0 upd=87
```

Владельцу же ушло (output задания):

```
Tuber-Telegram: сбой сбора — каналов ок 60, сбоев 0, ошибок 1, новых 5, обновлено 2645.
```

Причина — в старой обёртке `~/.hermes/scripts/tuber_telegram_collect.sh` условие `if err or fail`:
любая единичная повторённая ошибка (`errors=1`) печаталась словами «сбой сбора», хотя `channels_fail=0`.
За двое суток таких ложных тревог было ровно две и обе — транзиентные сетевые:

| Дата | Канал | Ошибка | Повтор |
|---|---|---|---|
| 15.09.2026 | denissexy | RemoteProtocolError (19:01:36) | ok через 8 с (19:01:44) |
| 14.09.2026 | ai4svoi | ReadTimeout (17:51:52) | ok через 5 с (17:51:57) |

В обоих случаях канал после повтора собран полностью — сбоя сбора не было.

## 2. Что стало

### 2.1. Логика переехала в проект, в `~/.hermes/scripts` остался шим

- `scripts/cron_collect.sh` — вся логика обёртки (запуск `scripts/collect.py`, разбор JSON, правила вывода).
- `~/.hermes/scripts/tuber_telegram_collect.sh` — ровно две строки, исполняемый, без аргументов:

  ```bash
  #!/usr/bin/env bash
  exec /root/tuber-telegram/scripts/cron_collect.sh
  ```

  Это настоящий файл, не симлинк (планировщик Hermes симлинки не принимает).

### 2.2. Правила вывода (stdout уходит владельцу дословно; пустой stdout = тишина)

| Условие | Вывод |
|---|---|
| `channels_fail > 0` | `ALERT Tuber-Telegram: сбор не удался — каналов ок <ok>, сбоев <fail>, ошибок <err>, новых <new>, обновлено <upd>.` |
| `errors > 0` и `channels_fail == 0` и `errors < 3` | пустой stdout (единичная ошибка, повтор успешен) |
| `errors >= 3` или (`channels_ok > 0` и `errors*100/channels_ok >= 5`) | `Tuber-Telegram: повторные ошибки сети при сборе — каналов ок <ok>, ошибок <err>, новых <new>, обновлено <upd>.` (без слова «сбой») |
| `flood_until` заполнен | `Tuber-Telegram: аккаунт под флудом до <flood_until>. Web-сбор продолжается.` (дополнительно к правилам выше) |
| JSON не разобран | `Tuber-Telegram: сборщик не вернул итог (см. /root/tuber-telegram/logs/)` |

Скрипт всегда завершается кодом 0 (проверено на всех кейсах).

Решения по неоднозначностям (зафиксированы в коде):
- при `channels_fail > 0` выводится только строка `ALERT` (она уже содержит `ошибок <err>`);
  строка «повторные ошибки» в этом случае не дублируется — сбой и фон не смешиваются;
- строка про флуд независима и может идти вместе с любой из строк выше;
- числовой разбор защищён от `null`/нечисловых значений (`int(... or 0)`).

### 2.3. Сохранено без изменений (п.3 ТЗ)

`timeout 900`; `PY=/usr/local/lib/hermes-agent/venv/bin/python3` (в `/usr/bin/python3` нет httpx —
кейс 14.09.2026 давал молчаливый `errors=1` за 0.01 с); запись итоговой строки в
`logs/cron-$(date +%F).log`; аргументы `--mode web --limit-channels 60 --deadline 780`; хвост `| tail -1`.

Добавлены два тестовых хука, в бою не задействованы: `TG_TEST_JSON` (взять готовый JSON вместо сбора)
и `TG_CRON_LOG` (альтернативный путь журнала). Без них поведение идентично прежнему.

## 3. Тесты (`scripts/test_cron_collect.sh`)

Bash, без сети и без боевого сбора: готовые JSON-строки через `TG_TEST_JSON`, журнал — во временный
файл. Фактический прогон:

```
Обёртка: /root/tuber-telegram/scripts/cron_collect.sh
Шим:     /root/.hermes/scripts/tuber_telegram_collect.sh

Case (a) инцидент errors=1 fail=0 -> пустой stdout ... OK
Case (b) errors=0 -> пустой stdout ... OK
Case (c) channels_fail=2 -> ALERT со сбоев 2 ... OK
Case (d) errors=5 fail=0 -> «повторные ошибки», нет «сбой» ... OK
Case (e) flood_until заполнен -> есть «флудом» ... OK
Case (f) мусор вместо JSON -> есть «не вернул итог» ... OK
Case (g) шим Hermes, инцидент errors=1 -> пустой stdout ... OK
Case (h) errors=3 -> «повторные ошибки» ... OK

8 passed, 0 failed
```

Таблица кейсов с фактическим выводом (дословно):

| # | Вход | stdout | exit |
|---|---|---|---|
| a | `{"channels_ok": 60, "channels_fail": 0, "errors": 1, "posts_new": 5, "posts_upd": 2645}` | *(пусто)* | 0 |
| b | `{"channels_ok": 60, "channels_fail": 0, "errors": 0, "posts_new": 10, "posts_upd": 100}` | *(пусто)* | 0 |
| c | `{"channels_ok": 58, "channels_fail": 2, "errors": 0, "posts_new": 3, "posts_upd": 50}` | `ALERT Tuber-Telegram: сбор не удался — каналов ок 58, сбоев 2, ошибок 0, новых 3, обновлено 50.` | 0 |
| d | `{"channels_ok": 60, "channels_fail": 0, "errors": 5, "posts_new": 4, "posts_upd": 60}` | `Tuber-Telegram: повторные ошибки сети при сборе — каналов ок 60, ошибок 5, новых 4, обновлено 60.` | 0 |
| e | `{"channels_ok": 60, "channels_fail": 0, "errors": 0, "posts_new": 1, "posts_upd": 2, "flood_until": "2026-09-15T23:00:00+03:00"}` | `Tuber-Telegram: аккаунт под флудом до 2026-09-15T23:00:00+03:00. Web-сбор продолжается.` | 0 |
| f | `not-a-json-at-all` | `Tuber-Telegram: сборщик не вернул итог (см. /root/tuber-telegram/logs/)` | 0 |

Дополнительно (g) — вызов через боевой шим `~/.hermes/scripts/tuber_telegram_collect.sh` с JSON инцидента
даёт пустой stdout (шим корректно делегирует); (h) — порог `errors >= 3` срабатывает. Кейс (d)
дополнительно проверяет отсутствие подстроки `сбой`.

## 4. Доказательство, что боевой сбор не задет

Числа рабочей базы `data/tuber_telegram.db` (чтение через `?mode=ro`):

| Момент | posts | channels |
|---|---|---|
| до работ (22:12) | 3571 | 155 |
| после работ (22:13) | 3571 | 155 |

Изменений нет. Журнал `logs/cron-2026-09-15.log` не тронут: последняя строка осталась
`2026-09-15 22:05:14 {...}`, md5 `73f222080148f378ed968da29540bcf3`; тесты писали журнал во временный файл
через `TG_CRON_LOG`. `scripts/collect.py` не менялся (`git status`/`git diff` по нему пусты).
Cron-задания планировщика не трогались.

## 5. Файлы

- `scripts/cron_collect.sh` — новый, вся логика обёртки.
- `scripts/test_cron_collect.sh` — новый, тесты правил (без сети).
- `~/.hermes/scripts/tuber_telegram_collect.sh` — заменён на шим из двух строк (вне git-репозитория проекта).
- `docs/REPORT-14.md`, `docs/acceptance-log-14.txt` — этот отчёт и журнал приёмки.

`scripts/collect.py` не менялся; правка обёртки не потребовала изменений сборщика.
