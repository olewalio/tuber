# ТЗ-21 (tuber-telegram). Договор формата фида: экспорт приведён к канону

Дата: 15.09.2026. Роль проекта: **продюсер** фида-моста (`kind=x` и
`kind=youtube`) для `tuber-x`/`tuber-os`. Канон договора — в
`tuber-os/docs/EXCHANGE-FEED.md`; здесь лежит копия с шапкой «канон — в tuber-os»
(`docs/EXCHANGE-FEED.md`).

## 1. Что было

`scripts/export_candidates.py` (ТЗ-17, писался ДО появления договора) выгружал:

* `videos` — **список** (для X-строк всегда `[]`, для YouTube — `[video_id]`),
  тогда как договор и второй продюсер (tuber-os) пишут `videos` **числом**;
* `examples` — список **объектов** `{"message_id": .., "text": ..}`, а договор
  требует список **строк** (цитат);
* `video_ids` отсутствовал вообще.

Именно из-за `videos`-списка/`examples`-объектов приём X падал на живом фиде
(`TypeError: 'int' object is not iterable`) — эталонным был формат tuber-os.

## 2. Что стало

`scripts/export_candidates.py` приведён к канону v2:

* `videos` — **число** (сколько РАЗНЫХ постов/каналов дали упоминание, считается
  по множеству `message_id`);
* `video_ids` — **список строк**: для своих YouTube-строк реальный id видео,
  иначе `[]`;
* `examples` — список **строк** (цитата с префиксом `message_id` для
  трассировки, ≤ 80 знаков);
* `mentions` — число (как и было).

Живой файл `data/exchange/external_candidates.jsonl` пока записан в СТАРОМ
формате — его обновит плановый экспорт (ТЗ-20, 06:30 МСК). Приёмка это
фиксирует честно: импорт обязан терпеть оба вида, и терпит.

**Договор.** Заведён `docs/EXCHANGE-FEED.md` — копия канона с ссылкой на
`tuber-os`.

**Потребитель.** `scripts/bridge_common.py` (`read_feed`, `feed_examples`,
`feed_to_candidates`) уже принимал `examples` и объектами, и строками, и
`mentions` в любом виде; подтверждено приёмкой на обоих видах.

## 3. Приёмка (факты)

Инструмент: `scripts/acceptance_tz21.py` → `docs/acceptance-log-21.txt`
(**6 проверок, все OK**).

* П.2 реальный экспорт в tmp: 2 строки (`x`: `videos=2 (int)`, `video_ids=[]`,
  `examples` — строки; `youtube`: `videos=1 (int)`,
  `video_ids=['dQw4w9WgXcQ']`), нарушений канона 0;
* дополнительно прогон на РЕАЛЬНОЙ рабочей базе (только чтение, запись в tmp):
  **239 строк** (152 `x` + 87 `youtube`), `videos` = int у 239/239,
  элементы `examples` — только `str`, `video_ids` есть у всех строк, у
  YouTube-строк `video_ids == [handle]`;
* П.3 потребитель терпит оба вида: старый (`videos` списком, `examples`
  объектами) и новый (`videos` числом, `examples` строками, `sources` строкой)
  разобраны без исключения;
* П.4 импорт из живого фида tuber-os (`--dry`) не падает:
  `{"imported": 200, "skipped_existing": 4, "skipped_filter": 211,
  "skipped_limit": 681, "dry": true}`;
* П.5 рабочая база `tuber_telegram.db` не изменилась:
  `{channels: 155, posts: 3573, classified: 0}` до и после;
* П.6 `pytest -q` — **29 passed** (было 26; +3 теста договора). Обновлён
  `scripts/test_bridge_sources.py` (проверки 12) под новый формат.

Сквозная приёмка на снимке боевой базы X с числами —
`tuber-x/docs/REPORT-21.md` и `tuber-x/docs/acceptance-log-21.txt`.

## 4. Запреты соблюдены

Порог `VERIFY_MIN_MENTIONS=2`, формула `priority`, пороги регистрации, cron и
расписание (ТЗ-20) не менялись; `/root/.hermes/data/yt_keys.json` не тронут;
секреты и токены в отчёт не вынесены.
