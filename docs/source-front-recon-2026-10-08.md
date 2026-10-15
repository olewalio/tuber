# Разведка нового фронта сбора для Tuber

Дата: 2026-10-08. Хост: Main <server_host>. Проект: `/root/tuber`.
Метод: чтение кода и схемы БД (read-only) + реальные HTTP-запросы `curl`/`urllib` с фиксацией кода ответа.
Боевой код не изменялся, git-команды не запускались, в `data/tuber.db` записи не производились
(база открывалась только через `file:...?mode=ro`).

---

## 1. Таблица источников (проверено живыми запросами)

Легенда способа доступа: RSS/Atom — публичная лента; JSON — публичный JSON-эндпоинт;
ключ — требуется токен; платно — требуется платный доступ.

| Источник | URL проверки | Способ | HTTP | Отдаваемые поля | Метрика популярности | Лимиты / аутентификация |
|---|---|---|---|---|---|---|
| Product Hunt — лента | `https://www.producthunt.com/feed` | RSS (Atom) | **200**, `application/atom+xml`, 44 061 байт, 50 записей | `id`, `published`, `updated`, `link(alternate)`, `title`, `content` (HTML-описание), `author/name` | нет (в `<content>` — ссылка «Discussion») | Без ключа. Отдаётся через Cloudflare (`cf-cache-status: HIT`, `cache-control: max-age=30`). Лимит не документирован |
| Product Hunt — AI-категория | `https://www.producthunt.com/feed?category=artificial-intelligence` | RSS (Atom) | **200**, 43 907 байт | те же, но только AI-продукты (Griffin by Tavus, OpenSwarm, NOVA CLI, Cekura Bench, ClawCall) | нет | То же |
| Product Hunt — сайт | `https://www.producthunt.com/` | HTML | **403** | — | — | Cloudflare «Just a moment…». Парсинг HTML невозможен |
| Product Hunt — GraphQL API | `https://api.producthunt.com/v2/api/graphql` | JSON (POST) | **401** `invalid_oauth_token` | — (не получено) | — | **Нужен OAuth developer-токен**, без него доступа нет |
| arXiv — API cs.AI | `https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending` | RSS/Atom API | **200**, `application/atom+xml`, 14 428 байт | `id`, `updated`, `published`, `title`, `summary`, `author/name`, `link(alternate html + pdf)`, `arxiv:primary_category`, `category` | нет | Без ключа. Официальные ToU arXiv просят паузу между запросами (≈1 запрос / 3 с). Заголовков rate-limit не отдаёт |
| arXiv — API cs.CL | (то же, `cat:cs.CL`) | RSS/Atom API | **200**, 12 224 байта | те же | нет | То же |
| arXiv — RSS-поток | `https://rss.arxiv.org/rss/cs.AI` | RSS | **200**, `application/rss+xml`, 974 376 байт, `cache-control: max-age=86398` | `title`, `link`, `description`, `pubDate`, `dc:creator`, `arxiv:primary_category`, `arxiv:announce_type` | нет | Без ключа. Крупные суточные дампы |
| GitHub — Trending (HTML) | `https://github.com/trending/python?since=daily` | HTML-скрейп | **200**, 603 104 байта, 12 репозиториев | имя репозитория (`/owner/repo`), описание, язык | «stars today» (309, 162, 915, …) + общее число звёзд | Без ключа. Верстка может меняться |
| GitHub — поиск репозиториев | `https://api.github.com/search/repositories?q=created:>YYYY-MM-DD stars:>=10&sort=stars` | JSON API | **200**, `total_count: 1802` | `full_name`, `html_url`, `description`, `created_at`, `language`, `forks_count`, `stargazers_count`, `owner` | `stargazers_count` | **Без токена — 10 запросов/мин** (`X-RateLimit-Limit: 10`, `X-RateLimit-Resource: search`). С токеном — 30/мин |
| Hugging Face — трендовые модели | `https://huggingface.co/api/models?sort=trendingScore&direction=-1` | JSON API | **200**, `application/json` | `id`, `modelId`, `likes`, `trendingScore`, `downloads`, `tags`, `pipeline_tag`, `library_name`, `createdAt` | `likes` + `trendingScore` + `downloads` | Без ключа. **`ratelimit-policy: "fixed window";"api";q=500;w=300`** → 500 запросов / 300 с анонимно |
| Hugging Face — свежие модели | `https://huggingface.co/api/models?sort=createdAt&direction=-1` | JSON API | **200** | те же | — | То же |
| Hugging Face — Daily Papers | `https://huggingface.co/api/daily_papers` | JSON API | **200**, 273 831 байт | `paper.id`, `paper.title`, `paper.summary`, `paper.authors`, `paper.publishedAt`, `paper.upvotes`, `paper.githubRepo`, `paper.githubStars`, `numComments`, `submittedBy`, `publishedAt` | `paper.upvotes` | Без ключа. То же |
| Hacker News — Algolia | `https://hn.algolia.com/api/v1/search_by_date?tags=story` | JSON API | **200** | `objectID`, `title`, `url`, `points`, `num_comments`, `author`, `created_at`, `created_at_i`, `story_id` | `points`, `num_comments` | Без ключа. Заголовков лимита нет (документируется как ~10 000 запросов/час) |
| Hacker News — сайт | `https://news.ycombinator.com/` | HTML | **200**, 34 666 байт | — | — | Запасной путь |
| Papers with Code — API | `https://paperswithcode.com/api/v1/papers/?page=1` | JSON API (заявлено) | **200**, но `text/html`, 1 545 750 байт | JSON **не отдаётся** — возвращается HTML главной страницы (SPA) | — | Данные через API недоступны |
| Papers with Code — варианты | `/api/v1/papers/?format=json`, `/api/v1/` | JSON | **200**, тот же HTML 1 545 750 байт | — | — | То же. API фактически нерабочий (отдаёт оболочку сайта) |
| BetaList — нативные ленты | `/feed`, `/feed.xml`, `/rss`, `/atom.xml` | RSS | **404** | — | — | Нативных лент нет |
| BetaList — Atom (feedburner) | `https://feeds.feedburner.com/BetaList` | RSS (Atom) | **200**, `text/xml`, 38 776 байт, 25 записей | `id`, `published`, `updated`, `link`, `title`, `content`, `author/name` | нет | Без ключа. Лента размещена на feedburner (Google), не на betalist.com |
| BetaList — сайт | `https://betalist.com/` | HTML | **200**, 220 103 байта | — | — | В HTML `<link rel="alternate" type="application/atom+xml" href="https://feeds.feedburner.com/BetaList">` |
| Habr — вся лента | `https://habr.com/ru/rss/all/all/` | RSS | **200** (через 301-редирект), `text/xml`, 91 532 байта | `title`, `guid`, `link`, `description`, `pubDate`, `dc:creator`, `category` | нет (просмотры/рейтинг в ленте отсутствуют) | Без ключа. Требуется следование редиректам (`-L`) |
| Habr — хаб «ИИ» | `https://habr.com/ru/rss/hubs/artificial_intelligence/` | RSS | **200**, `text/xml`, 100 467 байт | те же | нет | Канонический URL — с «hubs» (мн. ч.); старый `.../rss/hub/.../all/` даёт 2 редиректа |
| vc.ru — вся лента | `https://vc.ru/rss` | RSS | **200**, `text/xml`, 20 142 байта | `title`, `description`, `link`, `guid`, `dc:creator`, `pubDate` | нет | Без ключа |
| vc.ru — тег AI | `https://vc.ru/rss/tag/ai` | RSS | **200**, `application/rss+xml`, 36 454 байта | те же | нет | Без ключа |

Примечания к таблице:
- Все проверки делались с публичного IP Main прямым запросом, без прокси. Ни один из
  требуемых источников не потребовал прокси/зеркала (в отличие от Product Hunt homepage — 403 CF).
- Product Hunt homepage (403) и GraphQL (401) — единственные отказы среди обязательного минимума.
- Papers with Code API формально отвечает 200, но тело — HTML главной страницы (тот же размер,
  1 545 750 байт, что и у `/`), т.е. структурированных данных нет.

---

## 2. Откуда в таблице `novelty` взялись `producthunt` и `arxiv`

**Ответ: оба собираются НАПРЯМУЮ отдельным коллектором внешнего контура. Это не ссылки,
пойманные классификатором внутри постов X/Telegram.** Код существует и работает в бою.

Найденные факты:

1. Модуль-коллектор: `/root/tuber/tuber/analysis/external.py`.
   - `PLATFORMS = {hackernews, github, producthunt, arxiv}`.
   - Прямые адреса: `HN_SEARCH_URL = https://hn.algolia.com/api/v1/search_by_date`,
     `GITHUB_SEARCH_URL = https://api.github.com/search/repositories`,
     `PRODUCT_HUNT_FEED_URL = https://www.producthunt.com/feed`,
     `ARXIV_API_URL = https://export.arxiv.org/api/query`.
   - Каждый источник изолирован: отказ одного попадает в `sources[name]` и не роняет прогон.
   - Константы: `PH_MAX_ENTRIES = 60`, `ARXIV_MAX_RESULTS = 100`, окно по умолчанию 48 ч.

2. Модуль сопоставления: `/root/tuber/tuber/analysis/novelties.py`.
   `external_index()` берёт имена из заголовков внешних записей и формирует
   `external_platforms`; `build()` собирает запись с полем `external_platforms`.

3. Запись в БД: `/root/tuber/tuber/analysis/trends.py`, функция `save_novelties()` —
   `INSERT INTO novelty (... external_platforms ...)`, где
   `external_platforms = ",".join(rec["external_platforms"])`.
   Подкоманда CLI: `python3 -m tuber trends novelties --save` (гейт `--allow-production`).

4. Обёртка: `/root/tuber/scripts/common/tuber_trends_daily.sh` — вызывает
   `python3 -m tuber trends novelties --save --allow-production`.

5. Крон: `/root/.hermes/cron/jobs.json`, job `id 3e2a8198a45d`
   «Tuber: подтемы и новинки (07:55)», `script: tuber_trends_daily.sh`,
   `expr: "55 7 * * *"`, `enabled: true`, `last_status: "ok"`, отработал 17 раз,
   `last_run_at: 2026-10-08T07:57:42+03:00`.

6. Журнал прогона `/root/.hermes/logs/tuber_trends_daily.log`, запись за
   2026-10-08T04:57:42Z:
   ```
   Hacker News: ok, записей 200
   GitHub: ok, записей 50
   Product Hunt: ok, записей 17
   arXiv: ok, записей 100
   ... записано строк в novelty: 51
   ```
   То есть во внешнем разрезе за 48 ч реально подтянуты 17 записей Product Hunt
   и 100 записей arXiv.

7. Состояние таблицы `novelty` (read-only запрос): 798 строк, `day` с 2026-09-22
   по 2026-10-08, tier `strict` — 520, `external` — 278. Строк с `producthunt`
   или `arxiv` в `external_platforms` — **198**. Встречающиеся значения
   `external_platforms`: `producthunt`, `arxiv`, `hackernews`, `github`,
   `hackernews,producthunt`, `arxiv,hackernews`, пусто.

**Вывод:** для Product Hunt и arXiv (и для HN/GitHub) фронт уже поднят с нуля
давно — контур 5 (ТЗ-48) работает, гейтуется флагом `--allow-production` и стоит
в боевом расписании. Поэтому вопрос «с нуля ли поднимать фронт» для этих четырёх
источников закрыт: поднимать не нужно, нужен только ДОБОР новых источников.

---

## 3. Предложение по интеграции

### 3.1. Какие 3–5 источников брать первыми

Приоритет — то, чего в проекте сейчас НЕТ и что даёт метрику популярности:

1. **Hugging Face — трендовые модели** (`/api/models?sort=trendingScore`) —
   прямое попадание в тему «релизы моделей», есть числа (`likes`, `trendingScore`,
   `downloads`), анонимный доступ 500 запросов / 5 мин.
2. **Hugging Face — Daily Papers** (`/api/daily_papers`) — замена нерабочего
   Papers with Code: те же новые статьи, но с `upvotes`, `githubRepo`,
   `githubStars` и авторами.
3. **vc.ru — тег AI** (`https://vc.ru/rss/tag/ai`) — русскоязычный фронт,
   стартапы и ИИ-новости, чистый RSS без ключа.
4. **Habr — хаб ИИ** (`https://habr.com/ru/rss/hubs/artificial_intelligence/`) —
   русскоязычные технические статьи по ИИ.
5. **BetaList** (`https://feeds.feedburner.com/BetaList`) — суточная лента новых
   стартапов, fields `title/link/published/author`.

Резерв/улучшение (не новый фронт, а доработка существующего): GitHub Trending HTML
даёт сигнал «stars today», которого нет у поискового API; Product Hunt имеет
AI-категорийную ленту `?category=artificial-intelligence` (200) — можно сузить шум.

### 3.2. Какие поля писать в существующие таблицы

Схему БД МЕНЯТЬ НЕ НУЖНО. Внешние новинки не являются постами `content` — они живут
только в `novelty` (поле `evidence_json`), поэтому поток данных такой:

- новый фитчер возвращает список словарей той же формы, что `_item()` в `external.py`:
  `platform, title, url, score, comments, published_at, name_text`;
- `novelties.external_index()` строит `external_platforms`;
- `trends.save_novelties()` пишет строку в `novelty`
  (`external_sources`, `external_platforms`, `external_url`, `evidence_json`).

То есть поля новых источников автоматически ложатся в уже существующие колонки
таблицы `novelty`; писать в `content`, `source`, `classification`, `metric_snapshot`
не требуется и нельзя (это сломало бы контур сбора).

Соответствие полей → `_item()`:
- HF models: `title=id/modelId`, `url=https://huggingface.co/<id>`,
  `score=trendingScore` (или `likes`), `published_at=createdAt`, `name_text=id`;
- HF daily_papers: `title=paper.title`, `url=https://huggingface.co/papers/<paper.id>`,
  `score=paper.upvotes`, `published_at=paper.publishedAt`;
- vc.ru / Habr / BetaList (RSS/Atom): `title`, `url=link`, `published_at`,
  `author` (dc:creator / author/name), `score=None`.

### 3.3. Как не сломать текущий пайплайн

- **Только аддитивно.** Добавить ключи в `external.PLATFORMS` и `external.FETCHERS`;
  сигнатура `collect()` и формат отчёта не меняются.
- **Изоляция отказов сохраняется.** В `collect()` на каждый источник свой `try/except`
  (HTTPError / URLError / parse) — новый источник не уронит прогон.
- **Никаких миграций схемы и записей вне `novelty`.** Запись остаётся под
  `--save --allow-production` (боевой гейт уже реализован).
- **Уважать лимиты:** GitHub search — 10/мин (уже 1 запрос/сутки); HF — 500/5мин;
  arXiv — пауза между запросами; к хостам RSS ходить раз в сутки.
- **Технические грабли, найденные при разведке:**
  - Habr RSS отдаёт 301 — нужно следовать редиректу (в `urllib` редиректы
    следуются по умолчанию; в `curl` — флаг `-L`).
  - BetaList: `betalist.com/feed` → 404; рабочая лента только на
    `feeds.feedburner.com/BetaList`.
  - Product Hunt: использовать `/feed` (200), НЕ парсить HTML главной (403 CF)
    и НЕ закладываться на GraphQL (401).
  - Papers with Code API отдаёт HTML — не подключать.
- **Риск качества для RU-источников (нужна проверка перед внедрением):**
  сопоставление внешних имён с внутренним корпусом идёт через
  `names.normalize_entity()` / `names.distinctive_names()`, рассчитанные на
  латиницу/`Show HN`/Product Hunt. Для русскоязычных заголовков Habr/vc.ru
  качество извлечения имён не проверялось (нельзя запускать код пайплайна);
  рекомендуется приёмка на копии базы с `--dry-run`.

---

## 4. Что проверить не удалось и почему

1. **Product Hunt GraphQL API** — реальные поля и квоты не получены: без OAuth
   developer-токена возвращает 401 `invalid_oauth_token`. Требуется регистрация
   приложения и ключ; в этой разведке ключа не было. Статус: «нужен ключ».
2. **Papers with Code API** — структурированные данные получить не удалось.
   Все проверенные варианты (`/api/v1/papers/`, `?format=json`, `/api/v1/`)
   отдают HTML главной страницы (1 545 750 байт, `text/html`), а не JSON. API в
   текущем состоянии непригоден; причина (закрытие/редизайн сервиса) по HTTP не
   устанавливается — можно лишь констатировать факт. Альтернатива — Hugging Face
   Daily Papers (работает).
3. **Метрики популярности русскоязычных источников** — RSS Habr и vc.ru не содержат
   просмотров/лайков/рейтинга, только `title/description/link/pubDate/author`.
   Ранжировать по популярности из RSS нельзя; не проверялось, извлекаемы ли эти
   метрики со страниц статей (не входило в задачу и потребовало бы парсинга HTML).
4. **Точный лимит Hugging Face HN-подобных и HN Algolia** — у HN Algolia вообще нет
   заголовков rate-limit, у HF лимит есть (`q=500; w=300`). Точный часовой порог
   HN Algolia из заголовков не выводится.
5. **BetaList API** — в HTML главной внутренних API-эндпоинтов не найдено; проверялись
   только публичные RSS-пути (404) и feedburner-лента (200). Существование иных
   машинных интерфейсов не подтверждено.
6. **Исполнение изменённого коллектора** — НЕ проверялось по условию задачи (запрет
   на правки кода и запуск пайплайна). Все выводы по интеграции сделаны чтением
   кода (`external.py`, `novelties.py`, `trends.py`) и схемы `tuber.db`, а не
   прогоном нового фитчера.
7. **Боевая БД** — записи не делались; `novelty` и схема читались только в режиме
   `file:/root/tuber/data/tuber.db?mode=ro`.

---

## Приложение. Команды воспроизведения проверок

```bash
# Product Hunt (лента), arXiv, GitHub, HF, HN, BetaList, Habr, vc.ru — коды ответов
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' -A 'Mozilla/5.0' -L \
  https://www.producthunt.com/feed
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' -A 'Mozilla/5.0' -L \
  'https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=5'
curl -sS -o /dev/null -w '%{http_code}\n' -A 'Mozilla/5.0' -L https://github.com/trending
curl -sS -o /dev/null -w '%{http_code}\n' -A 'Mozilla/5.0' -L \
  'https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=5'
curl -sS -o /dev/null -w '%{http_code}\n' -A 'Mozilla/5.0' -L https://hn.algolia.com/api/v1/search_by_date
curl -sS -o /dev/null -w '%{http_code}\n' -A 'Mozilla/5.0' -L https://feeds.feedburner.com/BetaList
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' -A 'Mozilla/5.0' -L https://vc.ru/rss/tag/ai
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' -A 'Mozilla/5.0' -L \
  https://habr.com/ru/rss/hubs/artificial_intelligence/

# Схема и состояние novelty — только чтение
python3 -c "import sqlite3;c=sqlite3.connect('file:/root/tuber/data/tuber.db?mode=ro',uri=True);\
print(c.execute('SELECT tier,COUNT(*) FROM novelty GROUP BY tier').fetchall())"
```

Отчёт подготовлен read-only разведкой; боевой код, расписание и база не изменялись.
