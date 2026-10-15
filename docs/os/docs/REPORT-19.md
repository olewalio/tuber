# Отчёт ТЗ-19. Приёмка ТЗ-16: три дефекта + договор формата фида

Дата: 15.09.2026. Репозиторий: `/root/tuber-os`.
Область: `tuber/yt.py`, `tuber/candidates.py`, тесты, документация.

## Кратко

Найдены и починены три дефекта импорта/учёта из ТЗ-16. Заведены долги
**D-11**, **D-12**, **D-13** (все закрыты, с код-маркерами `TODO(debt-D-XX)`
в местах правок). Создан договор формата фида-моста `docs/EXCHANGE-FEED.md`.
Добавлено 15 тестов; прежние 458 остались зелёными (итого 473).
Все живые прогоны — на копии `/tmp/tz19.db`; рабочая база не изменялась.

## Дефект 1 (D-11). Счётчик падал на подключении без `row_factory`

**Где:** `tuber/yt.py` — `YouTubeClient.spent_today()` и `search_calls_today()`.

**Суть:** значение запроса читалось по имени колонки (`row["u"]`, `row["n"]`),
причём `int(...)` стоял ВНЕ `try/except`. На обычном `sqlite3.connect()` без
`row_factory = sqlite3.Row` строка — это `tuple`, и `row["u"]` поднимал
`TypeError: tuple indices must be integers or slices, not str`, который улетал
наружу и **ронял сбор**. Докстрока обещала обратное («сбой чтения учёта не
останавливает работу, пишем warning и считаем 0»).

**Как починено:** значение читается **позиционно** (`row[0]`), возврат перенесён
**внутрь `try`** — теперь любой сбой чтения (в т.ч. нечисловое значение) даёт
`log.warning` + 0 при любой форме подключения. Тот же приём применён к
`candidates._quota_total()` (та же ошибка `int(row["u"])` вне `try`).

**Маркер:** `# TODO(debt-D-11)` в `tuber/yt.py` (в обоих методах) и
`tuber/candidates.py`.

## Дефект 2 (D-12). Импорт не понимал поле `handle`

**Где:** `tuber/candidates.py` — `_video_id_from_entry()`.

**Суть:** id искался только в `video_id`/`id`/`url`/`link`/`video_url`. Экспорт
(`candidates-export`) кладёт ссылку в поле `handle`, поэтому фид, собранный нашим
же экспортом, при импорте давал `youtube_ids: 0` (проверено живьём на
`/tmp/feed_yt_ok.jsonl`).

**Как починено:** порядок поиска расширен полем `handle` последним:
`video_id` → `id` → `url` → `link` → `video_url` → `handle`. `handle`
принимается и как ссылка (`youtube.com/watch?v=…`, `youtu.be/…`), и как голый
11-символьный id.

**Маркер:** `# TODO(debt-D-12)` в `tuber/candidates.py`.

## Дефект 3 (D-13). Одна битая строка отменяла весь импорт

**Где:** `tuber/candidates.py` — `read_youtube_feed()`.

**Суть:** первая же не-JSON строка поднимала `CandidatesError`, и ни одна строка
не импортировалась. Для файла-моста, который пишет ДРУГОЙ проект, это хрупко:
оборванная запись (упал писатель) навсегда блокировала приём.

**Как починено:** разбор вынесен в `_parse_feed()`; `read_youtube_feed()` —
тонкая обёртка. Битые строки пропускаются и считаются (`bad_lines`), строки
чужого `kind` игнорируются со счётчиком (`skipped_kinds`), импорт продолжается.
Отмена с понятной ошибкой — **только** если файл не найден/нечитаем, пуст или
**все** строки битые. В итог импорта добавлены поля `bad_lines`, `skipped_kinds`,
`youtube_ids`. Строка `kind=youtube` без извлекаемого id считается битой.

**Маркер:** `# TODO(debt-D-13)` в `tuber/candidates.py`.

## Договор формата фида

Создан `docs/EXCHANGE-FEED.md` (версия 1, обязателен для tuber-os /
tuber-telegram / tuber-x): формат JSONL UTF-8; обязательные `kind`, `handle`;
необязательные `mentions`, `videos`, `ai_hint`, `source`, `first_seen`,
`last_seen`, `examples`, `video_id`; правила производителя (`handle` без `@`, в
нижнем регистре, без завершающего слэша) и потребителя (чужой `kind` —
игнорировать, но считать; нет `handle` — строка негодная; порядок поиска id как
в D-12); пример из 3 строк, по одной на каждый `kind`; явное указание: формат
ломать нельзя без правки обоих проектов.

## Тесты

Добавлено 15 тестов (было 458 → стало **473**, все зелёные):

- `tests/test_quota_search.py::test_counts_read_on_connection_without_row_factory`
  — `spent_today`/`search_calls_today` читаются без `row_factory`, предохранитель
  не падает (Д1).
- `tests/test_candidates.py`:
  - `test_quota_total_reads_on_connection_without_row_factory` (Д1);
  - `test_read_youtube_feed_accepts_each_id_field` (6 параметров: каждое поле,
    включая `handle`) (Д2);
  - `test_read_youtube_feed_accepts_bare_id_in_handle` (голый 11-символьный id) (Д2);
  - `test_parse_feed_counts_skipped_kinds` (Д3);
  - `test_parse_feed_skips_bad_line_and_continues` (`bad_lines=1`, продолжение) (Д3);
  - `test_parse_feed_empty_file_raises` (пустой файл и файл из пустых строк) (Д3);
  - `test_parse_feed_all_bad_lines_raises` (все строки битые → ошибка) (Д3);
  - `test_import_skips_bad_line_and_reports_count` (Д3);
  - `test_import_repeat_no_duplicates` (повтор → `new=0`, дублей нет) (Д3).

## Приёмка

Полный журнал: `docs/acceptance-log-19.txt`. Ключевые числа (всё — на копии
`/tmp/tz19.db`, рабочая база только на чтение):

- Снимок: `VACUUM INTO '/tmp/tz19.db'`.
- Фид из 4 строк (3 youtube + 1 telegram): прогон 1 → `new=3, known=0, calls=3`;
  прогон 2 → `new=0, known=3, calls=3`, дублей нет (3 строки на 3 видео).
- Фид с битой 5-й строкой → импорт выполнен, `bad_lines=1`, `resolved=3`, `calls=3`.
- Пустой файл → `error: фид пуст`, exit 1, без трейсбека.
- Файл из одних битых строк → `error: все строки битые (3 из 3)`, exit 1, без трейсбека.
- Рабочая база до и после: `videos=30780, channels=7090, snapshots=71992,
  channel_candidates=2152`, `quota_log 8959/96673` — не изменилась; mtime не менялся.
- Копия израсходовала 18 units (`quota_log 8977/96691`): 3 сетевых прогона ×
  (3 `videos.list` + 3 ленивые проверки ключа, ротация берёт 3 разных ключа).

## Артефакты

- `docs/REPORT-19.md` (этот файл);
- `docs/EXCHANGE-FEED.md` — договор формата;
- `docs/TECH-DEBT.md` — D-11, D-12, D-13 (закрыты) + код-маркеры;
- `docs/acceptance-log-19.txt` — журнал приёмки;
- правки: `tuber/yt.py`, `tuber/candidates.py`, `tests/test_candidates.py`,
  `tests/test_quota_search.py`.

## Рекомендации

1. Ключи `yt_keys.json`: заполнено project у 5 из 6 ключей. Привязать оставшийся
   (и указать note, где нужно) — тогда предохранитель по units/поиску будет
   считаться строго по проекту и для него.
2. Экспорт внешних кандидатов (`candidates-export`) пишет не-YouTube записи в
   поле `handle` как ИМЯ канала, а потребитель YouTube ждёт ссылку. Договор
   `EXCHANGE-FEED.md` это фиксирует; при изменениях синхронизировать оба проекта.
3. Держать фид от других проектов строго по договору (JSONL, обязательные
   `kind`/`handle`) — тогда импорт не отменяется из-за одиночных битых строк.
