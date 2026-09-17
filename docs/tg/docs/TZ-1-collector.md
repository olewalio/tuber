# ТЗ-1 Tuber-Telegram: сборщик постов с предохранителями квоты

Проект: `/root/tuber-telegram/` (Python 3.11, stdlib + httpx, без фреймворков).
База: `/root/tuber-telegram/data/tuber_telegram.db` (SQLite, WAL; схема уже создана — читай `scripts/init_db.py`, НЕ меняй её структуру).
Прочитай перед работой: `docs/DESIGN.md` (слои системы, лимиты Telegram), `scripts/init_db.py`.

## Что сделать

Написать `scripts/collect.py` — сборщик постов из публичных Telegram-каналов реестра, и `scripts/test_collect.py` — тесты.

### Р1. Режим `web` (по умолчанию) — основа
- Читает `https://t.me/s/<handle>` (httpx, UA браузера, timeout 25 с, follow_redirects).
- Пагинация: пока не дошли до `min(message_id)` этого канала в таблице `posts`, брать страницу `https://t.me/s/<handle>?before=<min_id>`; максимум 5 страниц на канал за прогон.
- Пауза между запросами 0.5–1.0 с (случайная в диапазоне). При HTTP 403/429 — пауза 30 с, до 3 попыток, затем канал в `run_log` как ошибка и переход к следующему.
- Разбор страницы: `message_id` (из `data-post="<channel>/<id>"`), дата (атрибут `<time datetime=...>`), текст (HTML → чистый текст, ссылки сохранять как `текст (url)`), просмотры (`tgme_widget_message_views`), реакции и форварды если есть, тип медиа, все внешние ссылки, хэштеги, `@упоминания`, признак и источник пересылки (`tgme_widget_message_forwarded_from`).
- Время в базу — UTC ISO (`date_utc`); в отчёте — МСК.
- Дедуп на запись: `INSERT ... ON CONFLICT(channel_id,message_id) DO UPDATE` — обновлять `views`, `views_checked_at` не чаще 1 раза в сутки на пост, текст не перезаписывать пустым.
- Мусор: если текста нет и медиа нет — пост не писать; реклама (метки ERID, «реклама», «промокод» в первых 200 знаках) — писать с `is_ad=1`.
- Каналы, которые отдали «Contact @…»/пустую страницу — помечать `channels.read_mode='unreadable'`, `status='private'`, не пытаться читать в следующих прогонах.

### Р2. Режим `mtproto` (флаг `--mtproto`)
- Только каналы со `status='active'` и `read_mode='mtproto'`.
- Telethon, сессия `/root/.hermes/swarm/sessions/tg_collector`, ключи `TG_API_ID`/`TG_API_HASH` из `/root/.hermes/.env`.
- Чтение инкрементально: `iter_messages(entity, min_id=<max message_id в БД>, reverse=True)`, **sleep 1.0 с** между вызовами (лимит Telegram: ~10 запросов/30 с).
- При `FloodWaitError`: записать `flood_until = now + e.seconds` в `account_state` (name='tg_collector') и в `channels.flood_until`, прекратить работу аккаунта в этом прогоне. **Никаких ретраев во время флуда.**

### Р3. Режим `resolve` (флаг `--resolve N`, по умолчанию 20)
- Берёт каналы со `status='candidate'` и `tg_id IS NULL`; резолвит через MTProto (`get_entity` + `GetFullChannelRequest`) → `tg_id`, `subs`, `subs_at`, `title`, `about`.
- Пауза 3–5 с между резолвами. Счётчик `account_state.resolves_today` + `day` (UTC): не более N за прогон и не более 20 за сутки.
- Если `account_state.flood_until` в будущем — режим не запускается, печатает в лог `skip: flood until ...`.
- Провал резолва (нет такого юзернейма) → `status='dead'`, `notes` с причиной.

### Р4. Общие требования
- CLI: `--mode web|mtproto|resolve|all` (по умолчанию `all` = web+mtproto, БЕЗ resolve), `--handle X` (отладка одного канала), `--limit-channels N`, `--deadline SEC` (дефолт 900; по истечении — корректно завершить текущий канал и выйти), `--dry-run`.
- Журнал: строка в `runs` (started_at, finished_at, mode, channels_ok, channels_fail, posts_new, posts_upd, errors, note) + строки в `run_log` по каждому каналу.
- stdout — РОВНО ОДНА JSON-строка с итогом (ключи: `mode, channels_ok, channels_fail, posts_new, posts_upd, errors, duration_sec, flood_until`). Остальное — в `run_log` и в `/root/tuber-telegram/logs/collect-<ГГГГ-ММ-ДД>.log`.
- Порядок обхода: сначала каналы с наибольшим числом подписчиков И непроверенные сегодня, затем остальные (поле `checked_at`).
- Запрещено: ssh на другие машины, любые записи вне `/root/tuber-telegram/`, изменение схемы БД, изменение чужих проектов (`/root/cryptostream`, `/root/signalstream`, `/root/data/analytical-network`), установка системных пакетов. Прокси не использовать.

### Р5. Тесты `scripts/test_collect.py`
Не менее 8 проверок на локальных HTML-фикстурах (`tests/fixtures/*.html`, положи туда 2–3 реальных сохранённых страницы `t.me/s` любых публичных каналов):
1. корректный разбор `message_id`, даты (UTC), просмотров, текста;
2. пагинация: при `min_id` в БД запрашивается `?before=`;
3. повторный прогон не создаёт дублей (upsert), но обновляет просмотры;
4. пост без текста и без медиа не пишется;
5. реклама по ERID помечается `is_ad=1`;
6. `--resolve` уважает дневной лимит 20 и `flood_until`;
7. `--deadline` завершает прогон и пишет строку в `runs`;
8. канал с пустой страницей → `read_mode='unreadable'`.

Тесты должны запускаться как `python3 scripts/test_collect.py` и печатать `PASS/FAIL` по каждой проверке; в конце — итог `N passed, M failed`. Работать на копии базы (`/tmp/tt_test.db`), боевую не трогать.

## Приёмка
- `python3 scripts/collect.py --mode web --dry-run` — без ошибок, печатает одну JSON-строку.
- `python3 scripts/collect.py --mode web --limit-channels 5` — реально наполняет `posts` (проверить `SELECT COUNT(*) FROM posts`).
- `python3 scripts/test_collect.py` — все проверки PASS.
- Отчёт в `/root/tuber-telegram/docs/TZ-1-report.md`: что сделано, какие фикстуры, вывод тестов, фактическое число постов в базе после прогона.
