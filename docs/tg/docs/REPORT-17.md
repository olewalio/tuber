# ТЗ-17. Отчёт: мост источников (импорт кандидатов из фида + дискавери из постов + экспорт для X)

Дата: 15.09.2026, ~23:05 MSK. Репозиторий: `/root/tuber-telegram`.
Рабочая база: `data/tuber_telegram.db` — только чтение. Все замеры и тесты — на копиях
(`sqlite3 .backup` в `/tmp/tz17/`).

## Предусловие: планировщик на паузе

Задание `fd9f54e2b9bd` («Tuber-Telegram: сбор постов (web)», `*/30`) на момент прогона:
`state=paused`, `enabled=false`, `paused_at=2026-09-15T22:55:18+03:00`,
`last_run_at=2026-09-15T22:33:48+03:00`, `last_status=ok`. Условие выполнено.

## Что было → что стало

**Было:** 3 573 поста, 155 каналов (85 active, 28 candidate, 14 private, 28 dead). Упоминания
чужих каналов, аккаунты X и видео YouTube лежали в текстах/ссылках постов мёртвым грузом:
не было способа пополнить реестр кандидатами из этих данных и не было формата обмена с
`tuber-os`/`tuber-x`.

**Стало:** три скрипта и общий модуль:

| Файл | Роль |
|---|---|
| `scripts/bridge_common.py` | Общий модуль: конфиг фильтра, нормализация, извлечение t.me/x/youtube, дедуплицирующий импорт (единый код для A и B, без копипасты) |
| `scripts/import_candidates.py` | **Часть A**: импорт `kind="telegram"` из фида tuber-os |
| `scripts/discover_from_posts.py` | **Часть B**: дискавери t.me-хендлов из `posts.text`/`posts.links` |
| `scripts/export_candidates.py` | **Часть C**: экспорт фида X/YouTube для tuber-x/os |
| `config/bridge_sources.json` | Конфиг фильтра: `handle_regex`, запретные пути, боты, список гигантов-новостников, порог |

Плюс: `docs/SCHEDULE.md` (регламент суточного расписания), тесты
`scripts/test_bridge_sources.py` (13 кейсов), фикстура `tests/fixtures/tz17_feed.jsonl`,
`.gitignore` дополнен `data/exchange/`.

Логику сбора (`scripts/collect.py`), расписание и cron-задания не трогали. Поля
`is_author`, `vr`, `antifraud_flag` существующих каналов не трогали.

## Формат и контракт

- `stdout` каждого скрипта — ровно одна JSON-строка. Ошибка входа — понятная строка в
  `stderr` и код 2, без трейсбека в лицо владельцу.
- Дедупликация по нормализованному `handle` (нижний регистр, без `@`) **во всех статусах**:
  существующие записи не перезаписываются и не понижаются, только счётчик
  `skipped_existing`.
- Порядок обработки: фильтр → дедуп → лимит. Поэтому статистика причин отсева честная.
- Импорт пишет только новые строки `channels(handle,status='candidate',source,notes,read_mode='web')`.

### Конфиг фильтра (`config/bridge_sources.json`)

- `handle_regex": "^[a-z0-9_]{5,32}$"` — служебное/мусор (короче 5, длиннее 32, крив.
  символы) отбрасывается;
- `reserved_paths` — служебные пути t.me (`joinchat`, `addstickers`, `share`, `s`,
  `durov`, `premium`, …) с пояснением;
- `bot_suffix="bot"` — хендлы `*bot` отбрасываются, КРОМЕ явного ИИ-признака `ai_hint=1`;
- `news_giants` — список новостников-гигантов (`ndtv`, `skynews`, `foxnews`, `cnn`,
  `bbc`, `reuters`, `bloomberg`, `wsj`, `nytimes`, `guardian`, `forbes`, `dw`, `rt`,
  `tass`, `ria` и т.п.) с пояснением;
- `min_mentions_default=2` — ниже порога не берём, если `ai_hint != 1`.

Порядок проверок в фильтре: служебное → бот → гигант → паттерн → порог. Гиганты
проверяются **раньше** паттерна намеренно: короткие хендлы (`cnn`, `bbc`, `rt`) иначе
попали бы в «паттерн», и статистика «сколько гигантов отсеяно» была бы неверной
(найдено при первом прогоне тестов, исправлено).

## Таблица тестов (фактический вывод, 13 passed / 0 failed)

Запуск: `python3 scripts/test_bridge_sources.py`. Сеть не используется, временные БД в /tmp.

| # | Кейс | Факт |
|---|---|---|
| 1 | Базовый импорт: счётчики, `status`/`source`/`notes` | PASS. imported=3, skipped_existing=1, skipped_filter=5; notes содержат `mentions=5`, `ai_hint=1`, не более 2 примеров |
| 2 | Идемпотентность: повтор = 0 новых | PASS. run1 imported=4; run2 imported=0, skipped_existing=4 |
| 3 | Существующий `active` не понижаем/не переписываем | PASS. imported=3, skipped_existing=1; статус `active`, source не переписан |
| 4 | Бот без `ai_hint` отсеян, с `ai_hint=1` пропущен | PASS (`spamnewsbot`→bot, `aibot`→pass) |
| 5 | Гигант-новостник отсеян | PASS (`cnn`,`reuters`,`bbc`,`nytimes`,`tass`→news_giant) |
| 6 | Служебное/паттерн + порог и `ai_hint` | PASS (`joinchat`→reserved, `s`→reserved, `ab`→pattern, `has.dot`→pattern, `rare_chan`→low_mentions, при `ai_hint` проходит) |
| 7 | `--limit`: превышение в `skipped_limit` | PASS. imported=1, skipped_limit=3 |
| 8 | `--dry`: сводка без записи | PASS. imported=4, dry=true, в БД 0 строк |
| 9 | Отсутствующий фид: код 2, без трейсбека | PASS. `exit=2`, stderr «фид не найден…», stdout пуст |
| 10 | Битый фид: код 2, без записи, без трейсбека | PASS. `exit=2`, stderr «фид битый: … строка 2», в БД 0 строк |
| 11 | Дискавери из постов (`source=post_mentions`) | PASS. imported=1, skipped_existing=2, skipped_filter=3 (s/spamnewsbot/cnn) |
| 12 | Экспорт X/YouTube: поля, сортировка, examples | PASS. kind_counts {x:1,youtube:1}; mentions=2; first/last_seen ISO; ≤3 examples с message_id, ≤80 знаков |
| 13 | Извлечение t.me/x/youtube (регрессы) | PASS; включая `t.me/+invite` не берётся, `x.com/i/…` не берётся, `norm_ts` нормализует 'ДД.MM' формат |

Регресс сбора: `python3 scripts/test_collect.py` → 10 passed / 0 failed (не задет).

## Замеры на КОПИИ рабочей базы

Копия: `sqlite3 data/tuber_telegram.db ".backup '/tmp/tz17/…/run_*.db'"`.

### Часть B — дискавери из своих постов (реальные данные)

```
python3 scripts/discover_from_posts.py --db <копия> --dry
{"imported": 86, "skipped_existing": 68, "skipped_filter": 212, "skipped_limit": 0,
 "dry": true, "source": "post_mentions", "scanned_handles": 2616}
```

- Уникальных t.me-хендлов в постах: **366** (вхождений 2 616 — один пост = одно упоминание).
- Разбор фильтра по 366 кандидатам: прошли **154**; отсеяно **212**: `low_mentions` 155,
  `bot` 51, `reserved` 4, `pattern` 2. **Гигантов-новостников в своих упоминаниях — 0**
  (список в конфиге есть, но в постах этих каналов нет).
- Из 154 прошедших **80 уже есть в реестре** — но 12 из них отсеяны фильтром раньше дедупа
  (боты-агрегаторы реестра), поэтому `skipped_existing=68`.
- Итог: добавлено **86 новых кандидатов** (`source='post_mentions'`).
- Повторный запуск: `imported=0, skipped_existing=154` (идемпотентно).
- `--limit 3`: `imported=3, skipped_existing=68, skipped_limit=83`.
- Контроль неприкосновенности: `posts=3573`; `channels` в статусах active/private/dead
  без новых источников = 127 (85+28+14) — не изменились.

Топ по упоминаниям (для наглядности): `ai_machinelearning_big_data` 99, `cgevent` 80,
`deeptechnet` 74, `xor_journal` 73, `gpt_news` 66.

### Часть A — импорт фида

Реальный фид tuber-os `/root/tuber-os/data/exchange/external_candidates.jsonl`
**отсутствует** (`ls` → No such file or directory). Приёмка части A честно шла на
фикстуре `tests/fixtures/tz17_feed.jsonl` (формат ТЗ-16, 10 строк). Проверка запуска на
реальном пути зафиксирована как негативный кейс «фид не найден» (код 2).

```
python3 scripts/import_candidates.py --feed tests/fixtures/tz17_feed.jsonl --db <копия>
{"imported": 4, "skipped_existing": 0, "skipped_filter": 5, "skipped_limit": 0, "dry": false, ...}
# повтор:
{"imported": 0, "skipped_existing": 4, "skipped_filter": 5, "skipped_limit": 0, ...}
```

Фикстура: 1 строка `kind="youtube"` (игнорируется), 9 `kind="telegram"`. Из них:
4 импортированы (`ai_channel_one`, `existing_active`, `aibot`, `rare_ai`), 5 отсеяны
(`cnn`→гигант, `spamnewsbot`→бот, `joinchat`→служебное, `ab`→паттерн, `rare_chan`→порог).
`--limit 1` → `imported=1, skipped_limit=3`. Попытка импорта существующего `active`:
`skipped_existing=1`, статус остался `active`, `source` не переписан.

### Часть C — экспорт фида

```
python3 scripts/export_candidates.py --db <копия> --out data/exchange/external_candidates.jsonl
{"kind_counts": {"x": 152, "youtube": 87}, "written": 239, "out": "...external_candidates.jsonl",
 "dry": false, "top": [{"kind":"x","handle":"thsottiaux","mentions":13}, ...]}
```

- **152** аккаунта X, **87** видео YouTube, всего **239** строк JSONL.
- Все строки — валидный JSON с полями ТЗ-16 (`kind`,`handle`,`mentions`,`videos`/`sources`,
  `ai_hint`,`first_seen`,`last_seen`,`source`,`examples`,`exported_at`);
  `source="tuber-telegram:posts"`; `examples` — до 3 цитат ≤80 знаков с `message_id`;
  сортировка по `mentions` desc.
- Найден и исправлен дефект формата дат: в `posts.date_utc` встречаются два формата
  (`…T11:16:20+00:00` и `…11:16:20` без 'T'), из-за чего `last_seen` мог быть не-ISO и
  строковое сравнение min/max ломалось. Добавлен `bridge_common.norm_ts`; после фикса
  не-ISO дат в фиде — **0**.
- `--limit 10` → written=10 (x:9, youtube:1).

## Числа рабочей базы до/после (доказательство, что сбор не задет)

| Метрика | До | После |
|---|---|---|
| posts | 3573 | 3573 |
| channels | 155 | 155 |
| — candidate | 28 | 28 |
| — active / private / dead | 85 / 14 / 28 | 85 / 14 / 28 |
| runs | 60 | 60 |
| sources в channels | свои_MTProto+t.me/s 108, список_субагента 47 | без изменений |

Все операции шли на копиях (`/tmp/tz17/…`). Рабочая база открывалась только на чтение.
`feed:tuber-os` и `post_mentions` в рабочей базе отсутствуют.

## Негативные проверки (все пройдены)

| Проверка | Результат |
|---|---|
| Повторный импорт того же фида | `imported=0` (идемпотентность), тест 2 |
| Импорт существующего `active` | `skipped_existing=1`, статус `active` сохранён, тест 3 |
| Бот-хендл (`spamnewsbot`) | отсеян как `bot`, тест 4 |
| Гигант-новостник (`cnn`,`reuters`,`bbc`,`nytimes`,`tass`) | отсеян как `news_giant`, тест 5 |
| Битый JSON-файл | код 2, «фид битый: … строка 2», без записи, без трейсбека, тест 10 |
| Отсутствующий файл | код 2, «фид не найден», тест 9 |
| Превышение `--limit` | `skipped_limit>0`, лишнее не пишется, тест 7 |
| Служебный путь (`joinchat`,`s`) и мусор-паттерн (`ab`,`has.dot`) | отсеяны, тест 6 |
| Ниже `--min-mentions` без `ai_hint` | отсеяно (`low_mentions`); с `ai_hint=1` проходит, тест 6 |

## Выводы и рекомендации

1. Мост работает на реальных данных: из постов извлечено 86 новых telegram-кандидатов
   и 239 X/YouTube-кандидатов для обмена. Повторные прогоны ничего не дублируют.
2. Фильтр требует внимания к порядку проверок (гигант раньше паттерна) — иначе короткие
   новостные хендлы маскируются под «паттерн»; это зафиксировано тестом 5.
3. Реальный фид tuber-os ещё не появился на диске: до его появления часть A приёмки
   опирается на фикстуру. Рекомендуется после появления реального фида прогнать
   `import_candidates.py --dry` и сверить счётчики с отчётом tuber-os.
4. `reserved_paths` и `news_giants` — живые списки в конфиге; при появлении новых
   служебных путей/гигантов править только `config/bridge_sources.json`.
5. Задания в планировщик ставит владелец после приёмки (см. `docs/SCHEDULE.md`).
