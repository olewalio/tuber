# Расширение поиска видео и каналов (этап 9)

Документ фиксирует решения этапа 9. Ссылка на
`docs/METHODOLOGY-EXPANSION-SHORTS.md` из ТЗ не подтвердилась: файла в
`docs/` нет (как и на этапе 8), поэтому обоснования собраны здесь.

## Проблема

Сбор шёл по фиксированному списку из 38 запросов, пул каналов не рос сам.
`search.list?relatedToVideoId` («похожие видео») Google удалил 07.08.2023,
поэтому расширение построено на других вызовах.

## Новые вызовы (`tuber/yt.py`)

| Функция | Вызов | Стоимость |
|---------|-------|-----------|
| `search_channels` | `search?type=channel` | 100 units |
| `chart_videos` | `videos?chart=mostPopular` | 1 unit за 50 видео |
| `playlists_by_channel` | `playlists?channelId` | 1 unit |
| `channels_by_handle` | `channels?forHandle` | 1 unit за handle |
| `search_channel_videos` | `search?channelId` (fallback probe) | 100 units |

### Отклонения от ТЗ (проверено живой выдачей 2026-09-10)

1. **`forHandle` не принимает пачку.** ТЗ просило «пачками по 50», но это
   фильтр ровно на один handle: список через запятую возвращает
   `totalResults: 0` без ошибки. Проверено вживую. Поэтому
   `channels_by_handle` честно делает один вызов на handle (1 unit) и не
   теряет данные молча.
2. **Категории чарта 27, 25, 22 мертвы.** Пары регион+категория с
   `videoCategoryId` 27 (Education), 25 (News), 22 (People) дают
   `404 notFound` во всех проверенных регионах. В `CHART_CATEGORY_IDS`
   оставлены работающие 28 (Science&Technology), 26 (Howto&Style),
   24 (Entertainment).
3. **403 «IP address restriction»** теперь штатно помечает ключ мёртвым и
   продолжает ротацию (`yt._classify`), а не роняет вызов. Это реализует
   ограничение ТЗ «два живых ключа из четырёх».

## Схема (`tuber/db.py`)

Добавлены `channel_candidates` и `query_candidates` (DDL из ТЗ). Единственное
отступление от инварианта «все моменты — целые unixtime»: колонки
`discovered_at`/`probed_at` в ТЗ объявлены `TEXT`, поэтому туда пишется
ISO-8601 UTC (`db.now_iso`, `2026-09-10T16:00:00Z`). Формат единая точка —
`db.now_iso`.

Модуль `tuber/expand.py`:

* `mine_mentions` — regex по описаниям ИИ-видео и каналов
  (`youtube.com/@handle`, `/channel/UC…`, `/c/`, `/user/`), 0 units,
  самоупоминания и известные каналы отсекаются;
* `mine_queries` — униграммы и биграммы из заголовков и тегов ИИ-видео,
  порог `EXPAND_MINING_MIN_HITS` (3) разных видео, 0 units;
* `scan_charts`, `scan_playlists`, `search_new_channels` — платные источники;
* `probe_candidates` — проверка кандидата: последние ~15 видео через
  `playlistItems` по `uploads_playlist_id`, разбор существующим
  классификатором, приём при `>= EXPAND_ACCEPT_MIN_AI_VIDEOS` (2)
  подтверждённых ИИ-видео.

Порядок фаз — от бесплатных к дорогим (`EXPANSION_PHASES`, проверяется
тестом на неубывание стоимости). Транспортный сбой при проверке **не
отклоняет** кандидата навсегда: статус остаётся `new`, причина пишется
только за содержательный вердикт.

Скоринг: `вес источника (mention 3, channel_search 3, playlist 2, chart 1,
query_mining 3) + log10(подписчики+1) + 0.5×(упоминаний−1)`. При равном
score русскоязычный кандидат выше (тайбрейк, `candidate_sort_key`).

## Бюджет

`EXPAND_BUDGET_UNITS_PER_RUN=2000`, `EXPAND_MAX_PROBES_PER_RUN=40`,
`EXPAND_MAX_NEW_CHANNELS_PER_RUN=30`. Потраченное считается по `quota_log`,
поэтому учитывает и неудачи. Дедупликация (канал из `channels` или уже
проверенный кандидат) — до вызова API.

## Команды

* `python3 -m tuber expand [--budget N] [--max-probes N] [--dry-run]`;
* `daily` вызывает `expand` после сбора и разбора, до замеров.

## Живая проверка (боевая база, 2026-09-10)

`python3 -m tuber expand --budget 400 --max-probes 8`:

* кандидаты: chart 965 new (+3 rejected), channel_search 115 new
  (+15 accepted, 1 rejected), mention 1 (уже известный канал);
* принято 15 каналов, из них 8 — в финальном прогоне;
* потрачено 301 unit в финальном прогоне (661 unit за все три прогона);
* русских каналов: `is_russian=1` 0 → 6, `default_language=ru` 0 → 2,
  кириллица в title/source_query 71 → 90, `country=RU` 0 → 5;
* подтверждённых ИИ-видео в базе: 12 → 203;
* `mine_mentions` и `mine_queries` — 0 units, 0 вызовов.

## Ограничение живой базы

На старте этапа в базе было всего 12 подтверждённых ИИ-видео, поэтому
бесплатный источник упоминаний дал один кандидат (и тот уже известный канал).
Основной вклад дал платный `search_channels` по русским запросам. Это не
дефект механизма: после того как probe пополнил базу ИИ-видео (203), добыча
терминов заработала (164 запроса-кандидата) и в следующих прогонах
упоминаний станет больше.
