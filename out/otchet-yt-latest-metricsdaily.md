# Отчёт: YouTube → «последнее известное состояние» + суточная сводка metrics_daily

ТЗ-53, ветка `master`. Начат 2026-09-29 ~19:27 (+03:00).

## Статус

- [x] Верификация фактов ТЗ (все воспроизвелись на боевой базе, см. ниже).
- [x] Часть 1. YouTube возвращается в `content_latest` — коммит `e70a119`.
  Боевой бэкфилл: `content_latest` youtube 21 504 → 68 210 строк, свежих (≤2 суток) 0 → 30 489,
  максимум `captured_at` 2026-09-29 18:00:50.
- [x] Часть 2. Суточная сводка `metrics_daily` по трём площадкам — коммит `d848fab`.
  Бэкфилл 14 суток на боевой: telegram 14 строк, youtube 14, x 15. Сверка X с копией до бэкфилла:
  14 из 15 дней совпали побайтно, отличается только 29.09 (строка пересчитана позже, материалов
  стало на 5 больше). Тесты: 1782 passed, 1 xfailed.

## Верификация фактов ТЗ (боевая `/root/tuber/data/tuber.db`, только чтение)

| Факт ТЗ | Проверка | Итог |
|---|---|---|
| 1. `metrics.py:48 PLATFORMS=("x","telegram")` | прочитано | подтв. |
| 2. `recompute_content_latest` зовётся только X/TG путями | прочитано | подтв. |
| 3. YouTube: 91112 видео, 21504 в `content_latest`, max captured_at `2026-09-17 06:00:49` | запрос | подтв. (91112 / 21504 / 2026-09-17 06:00:49) |
| 3. YouTube `metric_snapshot` 459221, ok 390899, max `2026-09-29 18:00:50` | запрос | подтв. (459221 / 390899 / 2026-09-29 18:00:50) |
| 4. путь замера YouTube `yt snapshots` → `run_snapshots`/`plan_snapshots`, `recompute` не зовёт | прочитано | подтв. |
| 5. `digest.py:422` JOIN content_latest без фильтра платформы | прочитано | подтв. |
| 6. `metrics_daily` 15 строк, все x, 2026-09-15..29 | запрос | подтв. (`x|15|2026-09-15|2026-09-29`) |
| 7. схема `metrics_daily` | PRAGMA | подтв. (13 колонок как в ТЗ) |
| 8. следующий долг D-73 | grep | подтв. (max D-72) |

Дополнительно выяснено (важно для части 2):

- `metrics_daily` боевой базы — схема ЯДРА (`day, platform, items_ingested, ..., errors,
  extra_json`), а не «легаси» X-схема. X-коллектор пишет через TEMP-представление
  `x_metrics_daily` и `CompatConnection` (переписывает `metrics_daily`→`x_metrics_daily`);
  `INSTEAD OF INSERT` раскладывает легаси-поля по колонкам ядра, а X-специфичные
  (`cdn_429_count`, `synd_429_count`, `ssr_used`, `stale_lag_p95_min`) — в `extra_json`.
- `run`/`run_log`: есть `x` (2278) и `telegram` (746), **`youtube` — 0 строк**.
- `transport_request`: только `x` (124932); у Telegram/YouTube строк нет.
- `transport_instance`: только `x` (5, healthy 2).
- `content.meta_json` у Telegram/YouTube не содержит `metrics_at`/`likes` (X-специфичные ключи).
- `metric_snapshot`: `x` 169381, `telegram` 93597, `youtube` 459221.
- `content`: `x` 28676, `telegram` 72041, `youtube` 91112.

## Часть 1

Реализация:
- `tuber/platforms/youtube/schedule.py::run_snapshots` — в конце прогона один раз зовёт
  `core_storage.recompute_content_latest(conn, platforms=("youtube",))` + `commit()`.
  Точка одна на оба входа (`yt snapshots` CLI и `yt daily`), попадает и в cron-обёртку
  `scripts/youtube/tuber_snapshots.sh`. В сводку добавлено поле `content_latest`.
- `tuber/core/storage.py::recompute_content_latest` — при конфликте просмотры берутся
  монотонно: `views = CASE WHEN excluded.views>content_latest.views THEN excluded.views
  ELSE content_latest.views END` (с учётом NULL). Понижения нет (ч.1.2).
- `tuber/analysis/metric_queue.py` — в `tuber metrics backfill` добавлен `--platform`
  (повторяемый / через запятую). По умолчанию — прежнее поведение (`x,telegram`).
  Сам `backfill_snapshots` уже принимал `platforms`; для YouTube `bucket` всегда
  заполнен, поэтому «снимков заполнено 0», а `recompute_content_latest` в хвосте
  делает разовый бэкфилл `content_latest`.
- Тесты `tests/youtube/test_schedule.py`: 3 новых (один снимок; два снимка — берётся
  последний; монотонность — не понижает завышенное значение).

Замеры (боевая база — до; копия `/tmp/tuber-copy.db` — прогон; затем боевая):

| Метрика | Боевая ДО | Копия ПОСЛЕ | Боевая ПОСЛЕ |
|---|---|---|---|
| строк `content_latest` `platform='youtube'` | 21504 | 68210 | 68210 |
| из них свежих (≤2 суток) | 0 | 30489 | 30489 |
| MIN(captured_at) | 2026-09-16 06:00:49* | 2026-09-10 16:01:37 | 2026-09-10 16:01:37 |
| MAX(captured_at) | 2026-09-17 06:00:49 | 2026-09-29 18:00:50 | 2026-09-29 18:00:50 |

\* до прогона MAX был 2026-09-17 06:00:49.

Видео без строки `content_latest`: **22902** = ровно те, у кого **нет ни одного**
`metric_snapshot` (`SELECT COUNT(*) FROM content c WHERE c.platform='youtube' AND NOT
EXISTS (SELECT 1 FROM metric_snapshot m WHERE m.content_id=c.id)` → 22902; при этом
`COUNT(DISTINCT content_id)` снимков YouTube = 68210, 91112 − 68210 = 22902). Причина —
факт: нет ни одного замера, «последнее известное» строить не из чего.

Контрольная выборка (достать просмотры ДО из боевой, ПОСЛЕ — из копии, и последний снимок):

| content_id | external_id | views ДО | views ПОСЛЕ | последний metric_snapshot.views |
|---|---|---|---|---|
| 2 | --Fao64JcWc | 6557 | 6909 | 6909 |
| 3 | --Slg4R6b7g | 281 | 281 | 281 |
| 5 | --r6aWpwwH8 | 387051 | 392711 | 392711 |

Проверка по всей таблице (копия vs боевая ДО): `downgraded=0`, `increased=17071`,
`unchanged=4433`, общих строк 21504. Понижений нет.

Тесты: `python3 -m pytest -q` → **1777 passed, 1 xfailed** (до правки — 1776 passed и
1 падавший `test_debt_markers::test_markers_reference_registry_ids` из-за прошлого
коммита, см. «Прочее»).

### Отдельно: починен предсуществующий падавший тест

На чистом HEAD `tests/test_debt_markers.py::test_markers_reference_registry_ids` падал:
строки реестра `D-34/D-35/D-36` зачёркнуты (закрыты предыдущим коммитом), а маркеры
`TODO(debt-D-34/35/36)` в `tuber/platforms/telegram/{collect,scoring}.py` остались.
Переоформил 4 маркера в обычные комментарии `# D-34 (закрыт 24.09.2026): …` — тест
позеленел. Это не долг, а гигиена реестра.

## Часть 2

(заполняется)
