# ТЗ-21. Договор формата фида: выровнять продюсеров, научить импорт терпеть оба вида

Дата: 15.09.2026. Проект: `tuber-x` (потребитель моста), затронуты также
`tuber-os` и `tuber-telegram` (продюсеры).

## 1. Что было (дефект, найденный приёмкой владельца 15.09.2026)

Два продюсера писали ОДИН и тот же файл-мост по-разному:

| продюсер | файл | `videos` | `examples` | `video_ids` |
|---|---|---|---|---|
| tuber-os (ТЗ-19) | `/root/tuber-os/data/exchange/external_candidates.jsonl` | **число** (368) | список строк | не было |
| tuber-telegram (ТЗ-17) | `/root/tuber-telegram/data/exchange/external_candidates.jsonl` | **список** (`[]`) | список **объектов** | не было |

Проверено на живых файлах на диске:

* tuber-telegram: 239 строк (152 `kind=x`, 87 `kind=youtube`), `videos` = list
  (152/152), `examples` = list объектов (152/152);
* tuber-os: 2182 строки (1086 `kind=x`, 1096 `kind=telegram`), `videos` = int
  (1086/1086), `examples` = list строк, `sources` отсутствует (есть `source`).

Последствие: приём X падал на живом фиде —
`TypeError: 'int' object is not iterable` в `tuber_x/feeds.py` (`_video_ids`
итерировался по `videos`, а там лежало число). Синтетические проверки ТЗ-18
дефект не поймали: тестовые строки были написаны по образцу одного продюсера
(`videos: []`), а второй вид (число) в тестах не встречался.

## 2. Что стало

Канон версии **2** закреплён в `/root/tuber-os/docs/EXCHANGE-FEED.md`
(копии с шапкой «канон — в tuber-os» — в `tuber-telegram/docs/` и
`tuber-x/docs/`):

| поле | тип | смысл |
|---|---|---|
| `kind` | строка | `telegram` / `x` / `youtube` |
| `handle` | строка | имя канала/аккаунта без `@`, нижний регистр |
| `video_id` | строка | только для `kind=youtube` |
| `mentions` | **число** | сколько раз встречался кандидат |
| `videos` | **число** | из скольких РАЗНЫХ видео/постов пришёл кандидат |
| `video_ids` | **список строк** | идентификаторы этих видео (у Telegram — реальные id, иначе `[]`) |
| `sources` | список строк | идентификаторы источников упоминаний |
| `source` | строка | метка продюсера (`tuber-os:video-descriptions`) |
| `examples` | **список строк** | цитаты-примеры (не объекты) |
| `ai_hint` | 0/1 | признак темы ИИ |
| `first_seen`, `last_seen` | ISO-8601 UTC | границы наблюдения |

Правила: лишние поля допустимы; неизвестный `kind` — строка считается и
пропускается; отсутствующий `handle` — строка битая; битая строка не отменяет
импорт остальных.

### Часть A. Продюсеры приведены к канону

* **tuber-telegram** `scripts/export_candidates.py`: `videos` — **число**
  (сколько разных постов дали упоминание, считается по множеству
  `message_id`), добавлено `video_ids` (для YouTube-строк — реальный id видео,
  иначе `[]`), `examples` — список **строк** (цитата с префиксом `message_id`
  для трассировки, ≤ 80 знаков), `mentions` — число. Заведён
  `docs/EXCHANGE-FEED.md`.
* **tuber-os** `tuber/candidates.py::_finalize`: добавлено `video_ids` —
  отсортированный список id видео, из которых пришёл кандидат. `videos`
  остаётся числом. Договор `docs/EXCHANGE-FEED.md` дополнен.

Экспорт продюсеров в tmp проверен на канон: tuber-os — 2182 строки, 0 нарушений;
tuber-telegram — 239 строк, 0 нарушений (проверено и на реальной рабочей базе
telegram, запись в tmp: `videos` = int у 239/239, `examples` — строки,
`video_ids` есть у всех строк).

### Часть B. Потребители терпят оба вида

* **tuber-x** `tuber_x/feeds.py`:
  * `videos` — принимается и числом, и списком (список трактуется как перечень
    id, число — как счётчик);
  * `video_ids` — читается, если есть, и приоритетно;
  * `sources` — принимается списком, строкой (через запятую) или отсутствующим
    (тогда берётся `source`);
  * `examples` — принимается списком строк, списком объектов (у объекта берётся
    `text`/`quote`/`snippet`), а также одиночной строкой;
  * негодное значение поля не роняет строку: она считается в новом счётчике
    `bad_fields` (общий в отчёте и по каждому фиду), импорт продолжается.
* **tuber-os** `tuber/candidates.py`: `_id_str` терпимо достаёт id из списка,
  объекта или строки; `_bad_field_count` считает поля с негодным типом;
  `bad_fields` добавлен в результат `_parse_feed` и в итог
  `import_youtube_feed`. (Функции `_feed_youtube_ids` в коде нет; её роль
  выполняют `_parse_feed` / `_video_id_from_entry`.)

Импортёры не перезаписывают уже опубликованные файлы: терпимость нужна в том
числе ради архивных фидов старого образца.

### Часть C. Тесты, ловящие именно этот класс дефектов

1. `tests/test_feed_contract.py` в КАЖДОМ из трёх проектов: генерируется свой
   РЕАЛЬНЫЙ экспорт в tmp и проверяются типы полей по таблице канона, с
   падением «поле X — ожидался int, получен list».
   * tuber-telegram: запускает `scripts/export_candidates.py` на временной БД;
   * tuber-os: `candidates.export_external` на временной БД;
   * tuber-x: свой экспорт очереди `feeds.export_queue` (новая функция + команда
     `tuber_x.cli export_candidates --out`), т.к. `tuber-x` обязан отдавать
     наружу данные по тому же договору.
2. Регрессия на НАСТОЯЩИХ файлах с диска (в `tuber-x` и `tuber-os`): если файл
   есть — импорт обязан пройти без исключения; если нет — `skip` с причиной.
3. `tuber-x/tools/acceptance_tz18.py`: добавлена проверка `[17]`, что импорт
   ОБОИХ реальных фидов не падает, с числами по каждому; CLI-разбор стал
   защищённым (`run_cli_json`): при сбое печатается код возврата, stderr и
   причина, а не необработанный трейсбек. Заодно исправлена скрытая ошибка:
   `giants_from_feed` вызывал `.get("text")` у строки `examples` и падал на
   фиде tuber-os.

### Часть D. Приёмка (факты)

Инструмент: `tools/acceptance_tz21.py` → `docs/acceptance-log-21.txt` (12
проверок, все OK).

**Настоящий прогон на снимке боевой базы X** (`VACUUM INTO
/tmp/.../prod_x_snapshot.db`):

```
TUBER_X_DB=<снимок> python3 -m tuber_x.cli import_candidates \
  --feed /root/tuber-telegram/data/exchange/external_candidates.jsonl \
  --feed /root/tuber-os/data/exchange/external_candidates.jsonl
```

полный JSON:

```json
{"feeds": [
  {"path": ".../tuber-telegram/external_candidates.jsonl", "feed_tag": "tuber-telegram",
   "rows": 239, "x_rows": 152, "skipped_kinds": 87, "bad_lines": 0, "bad_fields": 0},
  {"path": ".../tuber-os/external_candidates.jsonl", "feed_tag": "tuber-os",
   "rows": 2182, "x_rows": 1086, "skipped_kinds": 1096, "bad_lines": 0, "bad_fields": 0}],
 "feeds_missing": [],
 "imported_new": 367,
 "merged": 24,
 "skipped_filter": {"bad_handle": 0, "service_handle": 0, "blocklist": 0,
   "already_registered": 14, "bot": 2, "news_giant": 22,
   "below_mention_threshold": 760},
 "bad_fields": 0,
 "skipped_limit": 0,
 "queue_total": 1774,
 "dry": false}
```

Исключений нет. 239 строк Telegram-фида и 2182 строки фида tuber-os (из них
1086 `kind=x`) предъявлены в `feeds`.

**Разбор притока** (чистые базы, слияние по `handle`):

| прогон | imported_new | merged | queue_total |
|---|---|---|---|
| только tuber-telegram | 39 | 0 | 39 |
| только tuber-os | 359 | 0 | 359 |
| оба вместе | **401** | 0 | 401 |

Слияние: 13 хендлов есть в обоих фидах (`artificialanlys`, `chatgpt`,
`claudedevs`, `deepseek_ai`, `elonmusk`, `evanhub`, `hilbertspaess`, `openai`,
`polynoamial`, `sama`, `sharifshameem`, `thsottiaux`, `ylecun`) — они дают одну
запись, а не две. 385 кандидатов проходят каждый фид по отдельности, ещё 16
перешли порог `VERIFY_MIN_MENTIONS=2` только когда упоминания двух фидов
сложились (например `runwayml`, `grummz`, `peterwildeford`). Отсюда 401 > 385.

**Идемпотентность**: повторный прогон на том же снимке —
`imported_new=0`, `merged=391`, `queue_total=1774`.

**Новостники-гиганты (по фактам живого фида tuber-os, `kind=x`).**
`ndtv` в списке `NEWS_GIANTS` ОТСУТСТВОВАЛ. Добавлены фактические гиганты
вверху живого фида (число упоминаний — из фида):

| handle | упоминаний | обоснование |
|---|---|---|
| `ndtv` | 370 | индийская новостная сеть |
| `khabar_gaon` | 249 | хиндиязычный новостной канал |
| `bloombergradio` | 146 | Bloomberg (в списке был только `bloomberg`) |
| `foxbusiness` | 137 | Fox Business (был только `foxnews`) |
| `firstpost` | 105 | новостное издание |
| `bbgenespanol` | 105 | BBC (был только `bbc`/`bbcnews`) |
| `ajenglish` | 100 | Al Jazeera English (была `aljazeera`) |
| `bloombergtv` | 88 | Bloomberg |
| `bsurveillance` | 88 | Bloomberg |
| `bbgoriginals` | 87 | BBC |
| `bpolitics` | 87 | Bloomberg |

Уже были и реально отсеиваются: `skynews` (259), `foxnews` (150), `cbsnews`
(114), `dwnews` (105), `forbes` (86), `reuters` (82), `businessinsider` (3),
`cnn` (1). Итого `news_giant` = 22 при прогоне обоих фидов вместе.

Сознательно НЕ добавлялись персональные/тематические аккаунты (`rajshamani`,
`vottak_tv`, `carolinehydetv`, `edludlow`, `technology`, `analyticsindiam` и
т.п.) — это не новостные сети, а персоны/рубрики; `business` (179) не добавлен,
т.к. это обобщённый хендл без подтверждаемой новостной принадлежности.

**Рабочие базы не изменились** (COUNT до = COUNT после, приёмка ведёт все
прогоны на копиях):

* `tuber_x.db`: accounts 72, candidates 1407, posts 602, stories 100;
* `tuber.db`: videos 30780, channel_candidates 2152, channels 7090, quota_log 8959;
* `tuber_telegram.db`: channels 155, posts 3573, classified 0.

**Тесты `pytest -q`** (было → стало):

| проект | было | стало |
|---|---|---|
| tuber-x | 273 passed | **282 passed** |
| tuber-os | 476 passed | **484 passed** |
| tuber-telegram | 26 passed | **29 passed** |

## 3. Что НЕ менялось (запреты соблюдены)

* `VERIFY_MIN_MENTIONS=2` не менялся;
* формула `priority` и пороги регистрации не менялись (реестр сам не растёт:
  импорт только наполняет очередь `candidates`, аккаунты не регистрируются —
  подтверждено приёмкой);
* cron-задания и расписание (ТЗ-20) не тронуты;
* `/root/.hermes/data/yt_keys.json` не тронут;
* секреты и токены в отчёт не вынесены.

## 4. Рекомендации

1. Плановый экспорт сохранён в канон v2; живой файл tuber-telegram обновится
   ближайшим заданием (06:30 МСК) и станет `videos`-числом. До этого момента
   импорт работает за счёт терпимости — это и есть требуемое поведение.
2. Любая правка состава полей — сначала в канон
   (`/root/tuber-os/docs/EXCHANGE-FEED.md`), затем в оба продюсера и
   потребителя, и только потом выкатка.
3. При добавлении новых фидов держать `tests/test_feed_contract.py` зелёным:
   он специально проверяет ТИПЫ полей, а не только успешный импорт.
