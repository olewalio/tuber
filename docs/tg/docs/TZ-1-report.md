# ТЗ-1 — отчёт: сборщик постов Tuber-Telegram с предохранителями квоты

Дата: 14.09.2026 (МСК). Исполнитель: jCode. Проект: `/root/tuber-telegram/`.

## 1. Что сделано

Написаны два файла (новый код, старые контуры не копировались):

- `scripts/collect.py` — сборщик. Разбор HTML на `html.parser` (stdlib), сеть — `httpx`,
  схема БД только `CREATE ... IF NOT EXISTS` (структура из `scripts/init_db.py` не менялась).
- `scripts/test_collect.py` — 10 проверок на локальных фикстурах, работает на копии `/tmp/tt_test.db`.
- `tests/fixtures/*.html` — фикстуры (см. §3).

### Р1. Режим `web` (основа)
- Читает `https://t.me/s/<handle>` (UA браузера, timeout 25 с, `follow_redirects`).
- Пагинация: база → пока `min(message_id)` страницы > `MIN(message_id)` канала в `posts`,
  берётся `?before=<наименьший id на странице>` (совпадает с `data-before` в разметке), максимум 5 страниц.
  При первом контакте (нет постов в БД) читается 1 страница — якоря для пагинации ещё нет.
- Пауза между HTTP-запросами 0.5–1.0 с (случайная, между страницами и между каналами).
- HTTP 403/429 → пауза 30 с, до 3 попыток, затем канал в `run_log` как ошибка и переход дальше.
  Сетевые ошибки/5xx → до 3 попыток с коротким backoff.
- Разбор: `message_id` из `data-post`, дата из `<time datetime>`, текст HTML→plain
  (ссылки как `текст (url)`, `<br>`→перевод строки), просмотры (`K/M/тыс/млн/млрд`),
  реакции (сумма), форварды (если класс есть), тип медиа (photo/video/document/sticker/voice/poll/…),
  внешние ссылки, хэштеги, `@упоминания`, признак и источник пересылки (`tgme_widget_message_forwarded_from`).
- `date_utc` — UTC ISO `2026-09-13T09:10:18+00:00`; в `runs.note` пишется время МСК.
- Upsert `ON CONFLICT(channel_id,message_id) DO UPDATE`: `views`/`views_checked_at` обновляются
  не чаще 1 раза в сутки, пустым текстом существующий текст не затирается, медиа/ссылки дозаполняются.
- Мусор (нет текста и нет медиа) не пишется. Реклама (`erid`, «реклама», «промокод» в первых 200 знаках) → `is_ad=1`.
- Пустая/приватная страница (нет ни одного сообщения) → `read_mode='unreadable'`, `status='private'`,
  канал исключается из будущих web-прогонов.

### Р2. Режим `mtproto` (`--mtproto` / `--mode mtproto`)
- Только `status='active' AND read_mode='mtproto'`.
- Telethon, сессия `/root/.hermes/swarm/sessions/tg_collector`, ключи `TG_API_ID`/`TG_API_HASH` из `/root/.hermes/.env`.
- Инкрементально `iter_messages(entity, min_id=<MAX(message_id) в БД>, reverse=True, limit=200)`, sleep 1.0 с между сообщениями.
- `FloodWaitError` → `flood_until = now + e.seconds` в `account_state(name='tg_collector')` и в `channels.flood_until`,
  работа аккаунта прекращается, ретраев нет.
- Сейчас в реестре нет ни одного канала с `read_mode='mtproto'`, поэтому режим — no-op до назначения таких каналов.

### Р3. Режим `resolve` (`--resolve [N]`, по умолчанию 20)
- Кандидаты `status='candidate' AND tg_id IS NULL`, `get_entity` + `GetFullChannelRequest`
  → `tg_id`, `subs`, `subs_at`, `title`, `about` (в `notes`, колонки `about` в схеме нет).
- Пауза 3–5 с. Дневной лимит `account_state.resolves_today` + `day` (UTC): не более N за прогон и не более 20 за сутки.
- Если `flood_until` в будущем — режим не стартует, в лог идёт `skip: flood until <МСК>`.
- Провал резолва → `status='dead'` + причина в `notes`.

### Р4. Общие требования
- CLI: `--mode web|mtproto|resolve|all` (default `all` = web+mtproto, без resolve), `--handle X`,
  `--limit-channels N`, `--deadline SEC` (default 900), `--dry-run`, `--mtproto`, `--resolve [N]`, `--db PATH`.
- Журнал: строка в `runs` за прогон + строки в `run_log` по каждому каналу; файл `logs/collect-<дата>.log`.
- stdout — ровно одна JSON-строка с ключами `mode, channels_ok, channels_fail, posts_new, posts_upd,
  errors, duration_sec, flood_until`. Проверено: 1 строка, stderr пуст.
- Порядок обхода: непроверенные сегодня по убыванию подписчиков, затем остальные по `checked_at`.
- Ограничения соблюдены: без ssh, без прокси, без установки пакетов, схема не менялась,
  боевой код и файлы вне `/root/tuber-telegram/` не трогались (единственное исключение —
  `/tmp/tt_test.db` и `/tmp/*` для фонового вывода, прямо разрешено самим ТЗ для тестов).

## 2. Приёмка (фактические прогоны)

```
$ python3 scripts/collect.py --mode web --dry-run
{"mode": "web", "channels_ok": 122, "channels_fail": 5, "posts_new": 1586,
 "posts_upd": 737, "errors": 1, "duration_sec": 147.75, "flood_until": null}
```
— одна JSON-строка, stderr пуст, дедлайн 900 с не достигнут (147 с). 5 fail — это пустые
(закрытые) страницы, 1 error — один `ReadTimeout`, снятый ретраем.

```
$ python3 scripts/collect.py --mode web --limit-channels 5
{"mode": "web", "channels_ok": 5, "channels_fail": 0, "posts_new": 0,
 "posts_upd": 150, "errors": 0, "duration_sec": 10.4, "flood_until": null}
```
Новых не было потому, что боевая БД оказалась уже наполнена сторонней задачей
(`runs.mode='web-preliminary'`, 737 постов) — обход шёл по уже собранным каналам.
Чтобы показать реальное наполнение, тот же сборщик пущен во второй раз на большую пачку:

```
$ python3 scripts/collect.py --mode web --limit-channels 40
{"mode": "web", "channels_ok": 38, "channels_fail": 2, "posts_new": 233,
 "posts_upd": 737, "errors": 0, "duration_sec": 72.27, "flood_until": null}
```

**Фактическое число постов в базе после прогонов: 970** (было 737, добавлено 233;
SQLite: `SELECT COUNT(*) FROM posts` → 970). Отдельно проверено на чистой БД
(`/tmp/tt_demo.db`, только 2 канала без постов): `posts_new=31`, то есть сборщик реально пишет данные.

Журнал `runs` после прогонов:

| id | mode | ok | fail | new | upd | errors | finished_at |
|----|------|----|------|-----|-----|--------|-------------|
| 1 | web-preliminary (не наш прогон) | 25 | 0 | 737 | 0 | 0 | 17:48:23 |
| 2 | web | 5 | 0 | 0 | 150 | 0 | 17:49:37 |
| 3 | web | 38 | 2 | 233 | 737 | 0 | 17:53:49 |

Каналы, помеченные как нечитаемые в ходе прогонов: `iskustvennyy_intelekt_gpt_ai`, `sbn_ai`
(`read_mode='unreadable'`, `status='private'`). Ещё 5 пустых страниц в dry-run не помечались (dry-run не пишет).

## 3. Фикстуры (`tests/fixtures/`)

| Файл | Что это |
|---|---|
| `chatgptv.html` | реальная сохранённая страница `t.me/s/chatgptv` (14 постов, медиа, ссылки, реакции) |
| `chatgptv_before.html` | реальная страница `t.me/s/chatgptv?before=11900` (для проверки пагинации) |
| `deeptechnet.html` | реальная страница `t.me/s/deeptechnet` (17 постов) |
| `ai_machinelearning_big_data.html` | реальная страница с пересланным постом (`tgme_widget_message_forwarded_from`) |
| `empty_private.html` | реальный ответ `t.me/s/<несуществующий>` (пустая страница, без сообщений) |
| `edgechan_synthetic.html` | синтетика для краев: пост без текста и медиа, реклама ERID/промокод, нормальный пост, пост-медиа с пересылкой |

## 4. Вывод тестов

```
$ python3 scripts/test_collect.py
PASS  1. разбор message_id/даты UTC/просмотров/текста
PASS  2. пагинация: ?before= при min_id в БД
PASS  3. upsert: нет дублей, обновляются просмотры
PASS  4. мусор (нет текста и медиа) не пишется
PASS  5. реклама ERID/промокод -> is_ad=1
PASS  6. resolve: лимит 20/сутки и flood_until
PASS  7. --deadline: завершение и запись в runs
PASS  8. пустая страница -> unreadable/private
PASS  9. формат счётчиков (K/M/тыс)
PASS  10. ссылки «текст (url)», пересылка, медиа

10 passed, 0 failed
```

## 5. Найденные проблемы и наблюдения

1. **Боевая БД была наполнена извне во время работы.** В 17:48:23 появилась строка
   `runs(mode='web-preliminary', posts_new=737)` — не мой прогон (запущен сторонней задачей/агентом).
   Из-за этого приёмочный `--limit-channels 5` дал `posts_new=0`: каналы уже были собраны.
   На данные мой сборщик не повлиял (upsert идемпотентен), но чистого «0 → N» на боевой БД не получилось;
   наполнение подтверждено прогоном `--limit-channels 40` (+233) и отдельно на чистой БД (+31).
2. **17 каналов отдают закрытое веб-превью** (DESIGN §5.8). Из них в реальном прогоне
   подтвердились пустыми: `iskustvennyy_intelekt_gpt_ai`, `sbn_ai`, `neirosetij`, `MLhandbook`,
   `truetechsprint_prompt`. Они помечаются `unreadable/private` и больше не опрашиваются web-режимом;
   для них нужен путь через MTProto-подписку.
3. **`channels` не имеет колонки `about`** (Р3 требует писать `about`). Схему менять нельзя, поэтому
   `about` сохраняется в `notes`. Рекомендация: при следующей ревизии схемы добавить `about TEXT`.
4. **Один `ReadTimeout`** на `ai4svoi` (снят ретраем). Это нормальный сетевой шум; backoff работает.
5. **Первичный контакт читает 1 страницу**, а не 5: пока нет `min_id`, пагинация смысла не имеет
   (не к чему «дойти»), а 5 страниц × 155 каналов = перегрузка `t.me`. Если нужна глубинная ретроспектива,
   это отдельный прогон с явным флагом.

## 6. Рекомендации

1. Назначить `read_mode='mtproto'` для каналов с закрытым веб-превью и опрашивать их только подписанной сессией.
2. Прогнать `--mode resolve` для оставшихся кандидатов (не более 20/сутки), затем перевести живых в `active`.
3. Добавить в схему (в рамках отдельного ТЗ) колонку `about` в `channels` и, возможно, `reactions_json`.
4. Поставить cron: `web` каждые 30–60 мин (dry-run не нужен), `resolve` — 1 раз в сутки, с учётом `flood_until`.
5. Мониторить `flood_until` в `account_state` и не трогать аккаунт до полного спада (уже в логике).
