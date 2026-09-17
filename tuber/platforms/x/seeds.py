"""Р1. Ядро поисковых запросов дискавери: таксономия рубрик (методология §8).

Каждая рубрика — словарь:
    {'рубрика', 'queries_ru', 'queries_en', 'priority', 'quota_per_day'}

Операторы X (Р1.2), проверенные на живом RSS Nitter:
    since:<дата> until:<дата>   окно по времени
    min_faves:N min_retweets:N  порог (осторожно: молча режет выборку)
    lang:ru lang:en             язык
    -filter:replies             без ответов
    from:<handle>               лента аккаунта через поиск

`filter:media` НЕ используем (в замере отдаёт устаревшее). Даты подставляются
в момент прогона через `expand()` — статические даты в коде быстро устаревают,
а окно по времени обязано быть свежим.

Бюджет: сумма `quota_per_day` по рубрикам <= config.DISCOVERY_DAILY_BUDGET (120, §10).
Русские запросы есть в КАЖДОЙ рубрике (требование продукта, Р1.4), английские
дают основной объём.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Окно по времени по умолчанию: последние N дней.
DEFAULT_WINDOW_DAYS = 14

# --------------------------------------------------------------- таксономия (Р1.1)
RUBRICS = [
    {
        "рубрика": "релизы моделей",
        "slug": "model-releases",
        "queries_ru": [
            'ИИ релиз since:{since} until:{until} lang:ru -filter:replies',
            'нейросеть модель since:{since} until:{until} lang:ru -filter:replies',
            'нейросеть релиз since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'new LLM release since:{since} until:{until} lang:en -filter:replies',
            '"model release" AI since:{since} until:{until} lang:en -filter:replies',
            'launch new model OpenAI Anthropic since:{since} until:{until} lang:en -filter:replies',
            'open weights model released since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 1,
        "quota_per_day": 20,
    },
    {
        "рубрика": "агенты и автоматизация",
        "slug": "agents-automation",
        "queries_ru": [
            'ИИ агент since:{since} until:{until} lang:ru -filter:replies',
            'ИИ автоматизация since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'AI agent automation since:{since} until:{until} lang:en -filter:replies',
            'agentic workflow LLM since:{since} until:{until} lang:en -filter:replies',
            'multi-agent framework since:{since} until:{until} lang:en -filter:replies',
            'tool use function calling agent since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 1,
        "quota_per_day": 16,
    },
    {
        "рубрика": "вайб-кодинг",
        "slug": "vibe-coding",
        "queries_ru": [
            'вайб-кодинг since:{since} until:{until} lang:ru -filter:replies',
            'ИИ код since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'vibe coding since:{since} until:{until} lang:en -filter:replies',
            'vibecoding Cursor Claude Code since:{since} until:{until} lang:en -filter:replies',
            'AI writes code agent since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 2,
        "quota_per_day": 10,
    },
    {
        "рубрика": "инструменты разработчика",
        "slug": "devtools",
        "queries_ru": [
            'ИИ инструмент since:{since} until:{until} lang:ru -filter:replies',
            'нейросеть инструмент since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'MCP server AI since:{since} until:{until} lang:en -filter:replies',
            'new dev tool LLM SDK since:{since} until:{until} lang:en -filter:replies',
            'IDE AI assistant launch since:{since} until:{until} lang:en -filter:replies',
            'open source AI devtool since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 2,
        "quota_per_day": 12,
    },
    {
        "рубрика": "инфраструктура и железо",
        "slug": "infra-hardware",
        "queries_ru": [
            'ИИ чип since:{since} until:{until} lang:ru -filter:replies',
            'ИИ вычисления since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'AI chip GPU datacenter since:{since} until:{until} lang:en -filter:replies',
            'inference hardware TPU since:{since} until:{until} lang:en -filter:replies',
            'AI compute cluster since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 2,
        "quota_per_day": 10,
    },
    {
        "рубрика": "инвестиции и раунды",
        "slug": "funding",
        "queries_ru": [
            'ИИ стартап since:{since} until:{until} lang:ru -filter:replies',
            'ИИ раунд since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'AI startup raises funding round since:{since} until:{until} lang:en -filter:replies',
            'Series A AI company since:{since} until:{until} lang:en -filter:replies',
            'valuation AI startup since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 1,
        "quota_per_day": 12,
    },
    {
        "рубрика": "регулирование",
        "slug": "regulation",
        "queries_ru": [
            'ИИ закон since:{since} until:{until} lang:ru -filter:replies',
            'ИИ регулирование since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'AI regulation law since:{since} until:{until} lang:en -filter:replies',
            'EU AI Act since:{since} until:{until} lang:en -filter:replies',
            'AI policy ruling since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 2,
        "quota_per_day": 10,
    },
    {
        "рубрика": "исследования и бенчмарки",
        "slug": "research-benchmarks",
        "queries_ru": [
            'ИИ бенчмарк since:{since} until:{until} lang:ru -filter:replies',
            'нейросеть исследование since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'new AI paper benchmark since:{since} until:{until} lang:en -filter:replies',
            'LLM eval results since:{since} until:{until} lang:en -filter:replies',
            'arxiv AI model since:{since} until:{until} lang:en -filter:replies',
            'benchmark SOTA model since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 1,
        "quota_per_day": 14,
    },
    {
        "рубрика": "кейсы внедрения",
        "slug": "adoption-cases",
        "queries_ru": [
            'ИИ внедрение since:{since} until:{until} lang:ru -filter:replies',
            'ИИ бизнес since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'enterprise AI adoption case since:{since} until:{until} lang:en -filter:replies',
            'deployed LLM production since:{since} until:{until} lang:en -filter:replies',
            'AI in business results since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 3,
        "quota_per_day": 8,
    },
    {
        "рубрика": "скандалы и риски",
        "slug": "incidents-risks",
        "queries_ru": [
            'ИИ сбой since:{since} until:{until} lang:ru -filter:replies',
            'ИИ риск since:{since} until:{until} lang:ru -filter:replies',
        ],
        "queries_en": [
            'AI incident failure since:{since} until:{until} lang:en -filter:replies',
            'AI safety risk since:{since} until:{until} lang:en -filter:replies',
            'model leak security issue since:{since} until:{until} lang:en -filter:replies',
        ],
        "priority": 2,
        "quota_per_day": 8,
    },
]

# Рубрики по приоритету (1 — самые ценные), порядок внутри приоритета сохраняется.
RUBRIC_ORDER = sorted(range(len(RUBRICS)),
                      key=lambda i: (RUBRICS[i]["priority"], i))

PRIORITY_SCORE_BONUS = {1: 2, 2: 0, 3: 0}  # Р3: +2 за рубрику приоритета 1


def total_quota_per_day() -> int:
    return sum(r["quota_per_day"] for r in RUBRICS)


def window(now=None, days=DEFAULT_WINDOW_DAYS):
    """(since, until) для операторов окна по времени — ISO-даты."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    until = now.strftime("%Y-%m-%d")
    return since, until


def expand(query, now=None, days=DEFAULT_WINDOW_DAYS) -> str:
    """Подставить свежее окно времени в шаблон запроса."""
    since, until = window(now=now, days=days)
    return query.format(since=since, until=until)


def queries_for(lang="en", priority=None, now=None, days=DEFAULT_WINDOW_DAYS):
    """Список (rubric_dict, query) по рубрикам в порядке приоритета.

    lang: 'ru' | 'en' | 'all' (ru + en).
    priority: фильтр по минимальному приоритету? Нет — точному значению, если задан.
    """
    out = []
    for i in RUBRIC_ORDER:
        r = RUBRICS[i]
        if priority is not None and r["priority"] != priority:
            continue
        langs = ("ru", "en") if lang in (None, "all", "any") else (lang,)
        for lg in langs:
            for q in r.get(f"queries_{lg}", []):
                out.append((r, expand(q, now=now, days=days)))
    return out
