# Единая схема `tuber` (SCHEMA-UNIFIED)

Полная спецификация с картой legacy → core — в корневом `SCHEMA.md`. Здесь —
краткая справка по ядру и обоснование ключевых решений.

## Принципы

1. **Одно ядро на три платформы.** Общие таблицы `source`, `content`,
   `metric_snapshot`, `content_latest`, `classification`, `classify_cache`,
   `score`, `story`, `story_member`, `candidate` несут данные всех платформ;
   платформа указывается колонкой `platform`.
2. **Платформенная специфика — в JSON.** Оси, которых нет у других платформ,
   идут в `axes_json` (`score`), `meta_json` (`source`, `content`), `parts_json`
   (`score`). Это позволяет не плодить колонки под каждую платформу.
3. **Даты — только ISO-8601 UTC `TEXT`.** Конвертация централизована в
   `tuber/core/timeutil.py`.
4. **Идентичность — на естественных ключах.** `UNIQUE(platform, handle)` для
   `source` и `candidate`, `UNIQUE(platform, external_id)` для `content`,
   `UNIQUE(content_id, captured_at)` для `metric_snapshot`,
   `PRIMARY KEY(content_id, computed_at)` для `score`.
5. **Единый обмен.** Мост JSONL (канон `docs/EXCHANGE-FEED.md`) заменяется
   таблицей `candidate` в ТЗ-5; JSONL останется как экспорт/импорт совместимости.

## Ключевые решения и их основания

- **Составной `content.external_id` для Telegram.** `message_id` уникален только
  внутри канала (замер: 14 877 постов / 6 939 различных `message_id`), поэтому
  ключ — `<handle>/<message_id>`.
- **X: автор ≠ владелец ленты.** `content.author_handle` берётся из
  `posts.author_handle` (реальный автор), а `source_id` — из `posts.account_id`
  (владелец ленты, чью ленту парсили). Это подтверждено замером (в `owner_handle`
  лежит владелец ленты; 11 неверных авторств из 25 проверенных).
- **Тема YouTube — в `classification.topic`.** `videos.primary_topic` пуст у всех
  33 986 видео; реальная тема — `video_classification.topic` (33 873 строки).
  Пустая тема (`''`) нормализуется в `NULL`.
- **`content_latest` — производная.** Заполняется из последнего `metric_snapshot`
  (плюс для X — из метрик `posts`). В ядре `content_latest` не источник истины, а
  быстрый срез.

## Вспомогательные представления

`v_os_videos`, `v_os_snapshots`, `v_x_posts`, `v_x_scores`, `v_tg_posts`,
`v_tg_scores` повторяют колонками старые таблицы (данные — из ядра) и нужны для
постепенного переноса кода и для сверки. Наличие всех шести проверяется тестом.

## Денормализация идентификатора платформы (ТЗ-2c)

Представления совместимости читают `platform` и `external_id` НАПРЯМУЮ из
таблицы данных, без join к `content`: SQLite разворачивает (flatten) подзапрос
справа от `LEFT JOIN` только если он однотабличный, иначе представление
материализуется целиком на каждый join (долг D-22).

| Таблица | Денормализованные колонки | Откуда |
|---------|---------------------------|--------|
| `content` | `source_external_id` | `source.external_id` (канал/лента) |
| `content_latest`, `metric_snapshot`, `classification`, `score`, `seo_field`, `thumbnail_vision`, `content_comment`, `comment_check` | `platform`, `external_id` | `content` |

Значения поддерживают триггеры (`trg_<table>_denorm_ins/upd`,
`trg_content_source_ext_*`, `trg_source_ext_upd`, `trg_content_ext_upd`), поэтому
дублирование не нужно повторять в каждом пути записи. Бэкфилл уже заполненных
баз — `tuber.core.schema.ensure_denormalized` (идемпотентно, маркер
`schema_meta.denorm_version`, в `migrate_schema` выполняется один раз).

Индексы под access-path представлений: `idx_metric_ext(platform, external_id,
captured_at)`, `idx_score_ext(...)`, `idx_classification_ext(...)`,
`idx_seo_field_ext`, `idx_thumbnail_vision_ext`, `idx_content_comment_ext`,
`idx_comment_check_ext`, `idx_content_source_ext(platform, source_external_id)`.

Адаптер YouTube добавляет два своих индекса под access-path представлений
(в `store.py`, не в схеме ядра): `idx_youtube_score_vpd` — порядок по оси `vpd`
(`video_scores` по скорости), `idx_youtube_content_videos` — покрывающая проекция
представления `videos`.

Инвариант (тест `tests/youtube/test_denorm_views.py`): у каждой строки с
`content_id` значения `platform`/`external_id` равны соответствующим полям
`content`.

## `legacy_map`

Позволяет находить новый id по старому, не полагаясь на совпадение
автоинкрементов. Формат: `(legacy_db, legacy_table, legacy_id) →
(target_table, target_id, migrated_at)`. Нюансы (`target_id = 0` для составных
ключей, many→one для `quota_log`) — в `SCHEMA.md`, §5, и в `TECH-DEBT.md`.
