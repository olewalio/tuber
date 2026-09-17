"""Конфигурация Tuber_OS.

Здесь собраны все пути, параметры сбора и закрытые списки. Другие модули
не хардкодят пути, а берут их отсюда.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# --- Пути -----------------------------------------------------------------

# Корень монорепозитория: файл лежит в tuber/platforms/youtube/config.py.
# Платформенные пути (база, отчёты, обмен) выводятся из него, а не хардкодятся.
TUBER_DIR = Path(__file__).resolve().parents[3]
# Путь к базе. Перекрывается переменной окружения TUBER_DB: это нужно для
# прогонов на копии боевой базы, не трогая саму боевую базу.
DB_PATH = Path(os.environ.get("TUBER_DB") or (TUBER_DIR / "data" / "tuber.db"))
# D-06: отчёты суточного цикла складываются в подкаталог рядом с базой.
REPORT_SUBDIR = "reports"
# D-06: сколько дней файл отчёта лежит «на виду»; старше — уходит в archive/.
REPORT_KEEP_DAYS = 180
YT_KEYS = Path("/root/.hermes/data/yt_keys.json")
# Файл с секретами (DeepSeek и прочее). Подхватывается через load_env.
ENV_PATH = "/root/.hermes/.env"

# --- Квота YouTube Data API v3 -------------------------------------------

# Дневной лимит на один ключ (units). Это поведение по умолчанию для ключей
# без привязки к проекту (старый формат yt_keys.json без поля project): тогда
# ключ и есть отдельный проект. Для ключей с известным проектом работает
# QUOTA_LIMIT_PER_PROJECT ниже.
#
# Историческая справка (проверяется `git grep` по коммиту-предку): до этого
# изменения предохранителя суточной квоты в проекте НЕ существовало — обе
# константы (QUOTA_LIMIT_PER_KEY, QUOTA_SAFETY_RESERVE) были только объявлены и
# нигде не читались, а защита шла по ошибке API 403 quotaExceeded. Учёт расхода
# появился в этом изменении.
QUOTA_LIMIT_PER_KEY = 10000
# Суточный лимит Google на ПРОЕКТ (units), а не на ключ. Квота YouTube Data API
# выдаётся проекту: если два ключа принадлежат одному проекту, их расход
# складывается и лимит 10000 общий на оба. Именно поэтому учёт расхода считает
# сумму по проекту, а не по ключу.
#
# D-10 закрыт: разбор привязки «ключ → проект» живёт в
# config.load_key_projects(); для ключей с известным проектом и units, и
# вызовы поиска считаются суммой по проекту. Ключи без привязки работают в
# прежнем режиме «на ключ» (см. docs/TECH-DEBT.md, D-10).
QUOTA_LIMIT_PER_PROJECT = 10000
# Неприкосновенный запас на повторы и аварии, units. Рабочий порог остановки =
# лимит (проекта или ключа) минус этот запас: 10000 - 2000 = 8000 units.
# Запас оставляем, чтобы в конце суток хватило на: повторы после 429/сетевого
# сбоя, одну дорогую операцию (поиск стоит до 100 units) и хвост уже
# запущенного прогона, который не должен обрываться на середине.
QUOTA_SAFETY_RESERVE = 2000

# Отдельный жёсткий лимит Google именно на вызовы search.list: не больше 100
# вызовов в сутки на ПРОЕКТ (это дополняет лимит в 10 000 units на проект, а
# не заменяет его: Google считает и units, и число вызовов поиска отдельно).
# Считаются ПОПЫТКИ (строки quota_log с endpoint='search') за текущие сутки
# квоты по Pacific Time, а не units: Google ограничивает именно вызовы.
QUOTA_SEARCH_LIMIT_PER_PROJECT = 100
# Неприкосновенный запас вызовов поиска: рабочий порог = лимит минус запас =
# 100 - 15 = 85 вызовов на проект. Оставлен, чтобы после остановки поиска
# прогон мог доработать бесплатными механизмами (разбор имён, упоминания,
# обход плейлистов) и случайный всплеск не добил проектный лимит до отказа.
QUOTA_SEARCH_RESERVE = 15

# Стоимость вызовов в units (передаётся в api_call явно).
COST_SEARCH = 100
COST_VIDEOS = 1
COST_CHANNELS = 1
COST_PLAYLIST_ITEMS = 1
COST_COMMENT_THREADS = 1

# --- Формат видео: шортсы и полные ---------------------------------------

# Порог длительности вероятного шортса, секунды (по умолчанию 180).
# YouTube с 15.10.2024 разрешает шортсы до трёх минут. Надёжного API-признака
# шортса не существует (проверено тремя способами, docs/METHODOLOGY-EXPANSION-SHORTS.md),
# поэтому поле is_shorts — это «вероятный шортс по длительности», а не факт.
SHORTS_MAX_SECONDS = 180
# Переменная окружения, перекрывающая порог (читается в момент вызова).
SHORTS_MAX_SECONDS_ENV = "SHORTS_MAX_SECONDS"

# Ускорение слотов свежих шортсов: возраст видео умножается на этот множитель
# перед выбором слота. Шортс получает часовой слот раньше полного видео;
# при 1.5 первые слоты h0..h4 идут с шагом ровно 2 часа против 3 часов у полных.
SHORTS_FRESH_SLOT_SPEDUP = 1.5

# --- Расписание замеров ---------------------------------------------------

# Свежие видео (моложе FRESH_MAX_HOURS) — слоты h0..h6.
SLOTS_FRESH = ("h0", "h1", "h2", "h3", "h4", "h5", "h6")
# Остальные видео — один суточный замер.
SLOT_DAILY = "d"
# Все допустимые слоты.
SNAPSHOT_SLOTS = SLOTS_FRESH + (SLOT_DAILY,)

# Возрастные пороги (часы/дни).
FRESH_MAX_HOURS = 48
# Видео старше этого возраста — один замер в сутки.
DAILY_MIN_AGE_HOURS = 48
# Видео старше 10 дней в отчёт попадает только как «долгоживущий рост».
LONG_LIVED_MAX_DAYS = 10

# --- Композитный индекс виральности (ТЗ виральности, часть 1) --------------

# Период полураспада свежести, дней: чем старше видео, тем сильнее гасится
# индекс. Значение печатается в отчёте, чтобы распад не был скрытой настройкой.
VIRAL_HALF_LIFE_DAYS = 7.0
# Переменная окружения, перекрывающая период полураспада (читается в момент
# вызова), например для эксперимента на копии базы.
VIRAL_HALF_LIFE_DAYS_ENV = "VIRAL_HALF_LIFE_DAYS"
# Минимум осей, при котором индекс вообще считается. Меньше — NULL: среднее по
# одной оси выдало бы мусор за виральность (требование п.1.3).
VIRAL_MIN_AXES = 2
# Минимум видео канала (в окне), чтобы медиана канала считалась надёжной.
VIRAL_MIN_CHANNEL_BASE = 3
# Окно свежести базы сравнения: медиана показателя по видео канала за N дней.
VIRAL_CHANNEL_WINDOW_DAYS = 90
# Порог показов честного топа виральности, просмотры. Видео ниже порога в
# честный топ не идёт: на микровыборке (единицы просмотров) ось реакций
# раздувается (1 лайк на 3 просмотра даёт likes_per_1000 = 333 и отношение
# ×116), и такая позиция обгоняет реальные топа. Отсечённые не исчезают:
# отчёт показывает их отдельным блоком «МАЛАЯ ВЫБОРКА».
#
# Значение выбрано по ЗАМЕРУ на копии боевой базы (16.09.2026), а не придумано:
#   * шортсы: в честном топ-50 просмотры 104, 300, 308, 364, 376, 392, 559,
#     767, 940 — затем разрыв до 14 818. Порог 1000 отсекает ровно эти 9
#     микропозиций; пороги 300/500 режут 1/6, а 2000 не режет больше (тот же
#     разрыв 940 → 14 818);
#   * полные видео: в честном топ-50 просмотры 3, 196, 213, затем 2 704.
#     Порог от 300 до 2000 отсекает одни и те же 3 позиции (разрыв 213 → 2704).
# 1000 — наименьшее круглое значение, попадающее в «плато» обоих потоков
# (удаляет ровно микровыборку и не трогает реальные видео). Пороги потоков
# оставлены отдельными константами, чтобы их можно было развести по данным,
# но по замеру оба равны 1000.
VIRAL_MIN_VIEWS = 1000
VIRAL_MIN_VIEWS_SHORTS = VIRAL_MIN_VIEWS
VIRAL_MIN_VIEWS_LONG = VIRAL_MIN_VIEWS
# Переменные окружения, перекрывающие порог (читаются в момент вызова):
# общий VIRAL_MIN_VIEWS и уточняющие VIRAL_MIN_VIEWS_SHORTS / _LONG.
VIRAL_MIN_VIEWS_ENV = "VIRAL_MIN_VIEWS"
VIRAL_MIN_VIEWS_SHORTS_ENV = "VIRAL_MIN_VIEWS_SHORTS"
VIRAL_MIN_VIEWS_LONG_ENV = "VIRAL_MIN_VIEWS_LONG"
# Потолок отношения оси лайков/комментариев. Микровыборка не должна раздувать
# индекс: при 1 лайке на 3 просмотра отношение доходит до ×116. Потолок режет
# только хвост отношений, а не сами оси. Ось просмотров (outlier_score) НЕ
# ограничивается: большое отношение там означает реально большие просмотры.
#
# Значение 25 выбрано по ЗАМЕРУ (копия боевой базы, 16.09.2026): отношения
# выше потолка есть у 46 видео при 25, у 112 при 10 и у 19 при 50 — то есть 25
# лежит между «почти не трогаем» и «режем всё подряд». На позиции честного топа
# это влияет точечно: раздутые лайки микровыборки больше не обгоняют реальные
# видео, а крупные каналы остаются на месте (см. отчёт ТЗ).
VIRAL_AXIS_CAP = 25.0
VIRAL_AXIS_CAP_ENV = "VIRAL_AXIS_CAP"
# TODO(debt-D-15): оба числа подобраны на одном срезе базы 16.09.2026; потолок
# общий для лайков и комментариев, порог показов не учитывает размер канала —
# перемерить на следующих срезах, см. docs/TECH-DEBT.md.
# Минимальный интервал между замерами, чтобы считать пару валидной (секунды).
# При таком интервале считаются дельты (они честные), но НЕ скорость.
MIN_PAIR_INTERVAL_SECONDS = 600
# Минимальный интервал между замерами, при котором вообще считается скорость
# (views_per_day / views_per_hour), секунды. По умолчанию час: экстраполяция с
# 6-12 минут — это шум выборки, а не тренд, и в отчёт такие цифры не попадают.
# Короткий интервал даёт дельты как есть, но скорость = NULL.
MIN_INTERVAL_FOR_SPEED_SECONDS = 3600
# Опечатка из ТЗ сохранена алиасом, чтобы внешние ссылки на имя не ломались.
MIN_INTERVAL_FOR_SPED_SECONDS = MIN_INTERVAL_FOR_SPEED_SECONDS
# Переменные окружения, перекрывающие порог (читаются в момент вызова).
MIN_INTERVAL_FOR_SPEED_SECONDS_ENV = "MIN_INTERVAL_FOR_SPEED_SECONDS"
MIN_INTERVAL_FOR_SPED_SECONDS_ENV = "MIN_INTERVAL_FOR_SPED_SECONDS"

# Смещения слотов свежих видео от момента обнаружения, секунды (3-4 часа).
FRESH_SLOT_OFFSETS = {
    "h0": 0,
    "h1": 3 * 3600,
    "h2": 6 * 3600,
    "h3": 9 * 3600,
    "h4": 12 * 3600,
    "h5": 24 * 3600,
    "h6": 36 * 3600,
}
# Суточный слот — один раз в сутки.
DAILY_INTERVAL_SECONDS = 24 * 3600
# Видео 2-10 дней мерим в слот d дважды в сутки.
DAILY_TWICE_INTERVAL_SECONDS = 12 * 3600

# Окно поиска и обхода uploads: старше этого возраста видео не берём.
MAX_VIDEO_AGE_DAYS = 30
# Старше этого возраста видео выпадает из плана замеров полностью.
MAX_TRACK_DAYS = 30

# --- Параметры прогона сбора ---------------------------------------------

# Жёсткий лимит времени прогона, секунды: 15 минут. Живой прогон двух
# запросов занял 307 с, без лимита 30 запросов растянулись бы на часы.
COLLECT_TIME_BUDGET_SECONDS = 900
# Лимит обхода uploads-плейлиста канала: страниц по 50 видео.
MAX_UPLOAD_PAGES_PER_CHANNEL = 2
# Канал считается ИИ-каналом только при этом минимуме разобранных ИИ-видео.
MIN_AI_VIDEOS_PER_CHANNEL = 2
# D-03: доля ИИ-видео среди РАЗОБРАННЫХ, %. Применяется вместе с абсолютным
# минимумом выше, если разобрано не меньше MIN_AI_SHARE_MIN_VIDEOS видео.
MIN_AI_SHARE_PERCENT = 25
# Ниже этого числа разобранных видео доля статистически не работает: выборка
# мала, доля скачет от одного ролика, поэтому применяется прежнее абсолютное
# правило (>= MIN_AI_VIDEOS_PER_CANDIDATE ИИ-видео).
MIN_AI_SHARE_MIN_VIDEOS = 10

# Источники замеров (поле source в snapshots).
SOURCE_FRESH = "fresh"
SOURCE_DAILY = "daily"
SOURCE_MANUAL = "manual"

# --- Расширение поиска (этап 9, docs/METHODOLOGY-EXPANSION-SHORTS.md) -----

# Верхний предел расхода квоты на один прогон расширения, units.
EXPAND_BUDGET_UNITS_PER_RUN = 2000
# Сколько кандидатов максимум проверяем смыслом за прогон.
EXPAND_MAX_PROBES_PER_RUN = 40
# Сколько новых каналов-кандидатов максимум принимаем к созданию за прогон.
EXPAND_MAX_NEW_CHANNELS_PER_RUN = 30
# Сколько последних видео кандидата смотрим при проверке.
EXPAND_PROBE_VIDEOS = 15
# Сколько дорогих проверок через search?channelId (100 units) допускаем за прогон.
EXPAND_MAX_SEARCH_FALLBACKS = 3
# Сколько плейлистов максимум листаем у одного канала.
EXPAND_MAX_PLAYLISTS_PER_CHANNEL = 3
# Сколько каналов максимум просматриваем на плейлисты за прогон (защита квоты).
EXPAND_MAX_PLAYLIST_CHANNELS_PER_RUN = 20
# Сколько дорогих поисков каналов (100 units каждый) максимум за прогон.
EXPAND_MAX_CHANNEL_SEARCHES_PER_RUN = 2
# Сколько фраз максимум берём в пул поиска каналов за прогон (D-02). Пул шире
# лимита поисков сознательно: он определяет порядок, а не число дорогих вызовов.
EXPAND_MAX_CHANNEL_SEARCH_POOL = 60
# Сколько раз подряд фраза может упасть в поиске каналов, прежде чем временно
# уйдёт из пула (ТЗ-33). Иначе постоянно падающая фраза приоритета 1 каждый
# прогон занимает слоты и голодит остальной пул (репро /tmp/repro/repro_starve.py).
EXPAND_PHRASE_FAIL_LIMIT = 3
# Кулдаун для фраз, упавших EXPAND_PHRASE_FAIL_LIMIT раз подряд: 6 часов.
# Прогон расширения идёт раз в сутки, поэтому после кулдауна фраза снова
# пробуется — вдруг сбой был фразо-специфичным, а не общим по квоте/сети.
EXPAND_PHRASE_FAIL_COOLDOWN_SEC = 21600
# Сколько handle максимум резолвим за прогон (1 unit за handle: API принимает
# ровно одно значение forHandle на вызов, проверено живым вызовом).
#
# Порог выбран по ЗАМЕРУ ОЧЕРЕДИ, а не наугад (закрытие D-07, 13.09.2026):
#   * очередь к разбору на 13.09.2026 — 240 отложенных @handle (не разобрались
#     с первой попытки) плюс ~123 свежих упоминания, добавляемых самим прогоном,
#     итого ≈ 363 против прежней ёмкости 250; приток упоминаний 152–362 в сутки;
#   * прогон 13.09.2026 12:50 при лимите 250 встал по лимиту разбора
#     (stopped_reason в логе), а не по бюджету: calls=250, разобрано 145,
#     нераскрытых 289 (в базе осталось 240), израсходовано 675 units из
#     EXPAND_BUDGET_UNITS_PER_RUN = 2000, то есть четверть бюджета;
#   * 400 закрывает очередь 363 с запасом. Превышение над прежними 250 — это
#     +150 вызовов; разбор имени стоит 1 unit, то есть +150 units — это 1.5%
#     дневного лимита проекта (10 000); против поиска (100 units) разбор имени
#     дешёвый, поэтому 400 < бюджета прогона 2000.
# Пересматривать при росте очереди выше ёмкости: если прогон снова встаёт по
# лимиту разбора, а не по бюджету, — поднимать дальше, и следом приём каналов
# EXPAND_MAX_NEW_CHANNELS_PER_RUN (см. docs/TECH-DEBT.md, D-07).
EXPAND_MAX_HANDLE_RESOLVES_PER_RUN = 400
# Сколько новых добытых запросов максимум переводим в работу (accepted) за прогон.
# Защита квоты: один поисковый прогон стоит 100 units, а дневная квота проекта 10000.
EXPAND_MAX_QUERIES_PER_RUN = 20
# Сколько раз пробуем разрешить handle, прежде чем честно отклонить кандидата.
EXPAND_MAX_RESOLVE_ATTEMPTS = 3

# Русский кандидат при равном score проверяется первым.
EXPAND_RU_PRIORITY = True

# Вес источника кандидата (METHODOLOGY, часть A).
EXPAND_SOURCE_WEIGHTS: dict[str, int] = {
    "mention": 3,
    "channel_search": 3,
    "playlist": 2,
    "chart": 1,
}

# Регионы и категории для чарта популярного (videos?chart=mostPopular, 1 unit).
CHART_REGIONS: tuple[str, ...] = ("RU", "US", "GB", "DE", "IN", "KZ")
# 28 — Science & Technology (ядро ИИ-контента), 27 — Education.
CHART_CATEGORY_IDS: tuple[str, ...] = ("28", "27")

# Поисковые запросы для прямого поиска каналов (search?type=channel, 100 units).
CHANNEL_SEARCH_QUERIES: list[str] = [
    # Русские (приоритетный срез рунета).
    "ИИ агенты",
    "нейросети обзор",
    "нейросети для бизнеса",
    "искусственный интеллект канал",
    "локальные нейросети",
    # Английские.
    "AI agents",
    "artificial intelligence news",
    "AI tools review",
    # Расширение покрытия тем (задача о тематике вокруг ИИ).
    "нейросети для дизайна",
    "ИИ стартап",
    "новая нейросеть обзор",
    "ИИ влог",
    "обучение нейросетям",
    "нейросети для игр",
    "локальные нейросети на компьютер",
    "AI design",
    "AI startup",
    "AI vlog",
    "AI tutorial",
    "local AI",
]

# --- Поисковые запросы (не менее 130, английские + русские) --------------

SEARCH_QUERIES: list[str] = [
    # Английские (ядро мирового тренда).
    "artificial intelligence news",
    "AI model release",
    "OpenAI GPT",
    "Google Gemini AI",
    "Anthropic Claude",
    "AI agents automation",
    "AI coding assistant",
    "Nvidia AI chips",
    "AI datacenter energy",
    "AI robotics",
    "AI startup funding",
    "AI regulation safety",
    "AI science medicine",
    "AI video generation",
    "AI music generation",
    "open source AI model",
    "LLM benchmark",
    "AI tools productivity",
    "AI image generation",
    "machine learning research",
    # Русские (отдельный срез рунета). Короткие, как реально набирают.
    "ИИ агенты",
    "нейросети",
    "новости ИИ",
    "нейросеть видео",
    "ИИ для работы",
    "локальные нейросети",
    "ИИ кодинг",
    "нейросети для программирования",
    "роботы ИИ",
    "ИИ чипы",
    "заработок на ИИ",
    "ИИ бизнес",
    "обучение нейросетям",
    "ChatGPT нейросеть",
    "Grok ИИ",
    "Qwen ИИ",
    "генерация нейросетями",
    "искусственный интеллект новости",

    # --- дизайн и креатив ---
    "AI design tools", "AI graphic design", "Midjourney design", "AI UX UI design",
    "Figma AI", "AI 3D modeling", "AI animation", "AI photo editing", "AI logo design",
    "AI art tutorial",
    "нейросети для дизайна", "ИИ дизайн логотип", "нейросеть для анимации",
    "нейросеть обработка фото", "midjourney уроки", "нейросети для 3D", "ИИ арт нейросеть",

    # --- стартапы и бизнес ---
    "AI startup launch", "AI founder story", "build AI SaaS", "solo founder AI",
    "indie hacker AI", "AI startup pitch", "Y Combinator AI", "AI business automation",
    "AI marketing automation", "AI for small business",
    "ИИ стартап", "бизнес на нейросетях", "внедрение ИИ в бизнес", "ИИ для малого бизнеса",
    "ИИ автоматизация бизнеса", "ИИ маркетинг",

    # --- запуски и анонсы ---
    "new AI tool launch", "AI product demo", "AI announcements this week", "AI release update",
    "AI app launch", "AI feature update", "brand new AI tool",
    "новая нейросеть обзор", "запуск ИИ сервиса", "обзор новой нейросети",
    "новинки нейросетей", "что нового в нейросетях",

    # --- влоги и личный опыт ---
    "AI vlog", "day in the life AI engineer", "build in public AI", "I built with AI",
    "my AI workflow", "AI setup tour",
    "вайб кодинг влог", "делаю с ИИ", "мой опыт с нейросетями", "как я использую нейросети",
    "ИИ влог", "автоматизирую работу с ИИ",

    # --- обучение и навыки ---
    "AI tutorial beginner", "AI course free", "prompt engineering tutorial", "learn AI skills",
    "AI career path",
    "обучение нейросетям с нуля", "промпты для нейросетей", "профессия ИИ", "уроки по нейросетям",

    # --- игры и развлечения ---
    "AI game development", "AI in gaming", "AI NPC", "нейросети для игр", "ИИ в играх",

    # --- голос и дубляж ---
    "AI voice cloning", "AI dubbing", "AI music cover", "озвучка нейросетью", "ИИ дубляж",

    # --- безопасность и дипфейки ---
    "LLM jailbreak", "AI security risk", "AI deepfake", "AI scam warning",
    "дипфейк нейросеть", "мошенники нейросети",

    # --- локальный ИИ и железо ---
    "local LLM setup", "run AI locally", "self-hosted AI", "AI PC build",
    "локальные нейросети на компьютер", "нейросети на своём железе", "ИИ без интернета",
    "нейросеть на ноутбуке",

    # --- кодинг-агенты ---
    "Claude Code", "Cursor AI editor", "MCP protocol", "AI browser agent",
    "computer use agent", "AI coding agent review",
    "Claude Code обзор", "Cursor ИИ редактор", "ИИ агент для кода",

    # --- работа и деньги ---
    "AI jobs hiring", "AI freelancing", "make money with AI", "AI side hustle",
    "работа с ИИ", "фриланс с ИИ", "заработок на нейросетях 2026", "ИИ подработка",

    # --- финансы и ИИ ---
    "AI trading bot", "AI in finance", "algorithmic trading AI", "ИИ трейдинг",
    "нейросети в финансах",

    # --- виральное и хобби ---
    "AI pet video", "AI ASMR", "AI humor", "AI generated movie",
    "нейросети приколы", "ИИ видео приколы", "нейросеть сделала",
]


# --- Карта покрытия тем ---------------------------------------------------
# тема -> запросы этой темы (для контроля покрытия и тестов).
# Каждый запрос пула SEARCH_QUERIES входит ровно в одну группу, и наоборот:
# объединение групп в точности равно множеству пула (проверяется тестом).
QUERY_TOPIC_MAP: dict[str, list[str]] = {
    # Базовый пул прошлых версий: ядро мирового тренда и рунета.
    "ядро ИИ": [
        "artificial intelligence news",
        "AI model release",
        "OpenAI GPT",
        "Google Gemini AI",
        "Anthropic Claude",
        "AI agents automation",
        "AI coding assistant",
        "Nvidia AI chips",
        "AI datacenter energy",
        "AI robotics",
        "AI startup funding",
        "AI regulation safety",
        "AI science medicine",
        "AI video generation",
        "AI music generation",
        "open source AI model",
        "LLM benchmark",
        "AI tools productivity",
        "AI image generation",
        "machine learning research",
        "ИИ агенты",
        "нейросети",
        "новости ИИ",
        "нейросеть видео",
        "ИИ для работы",
        "локальные нейросети",
        "ИИ кодинг",
        "нейросети для программирования",
        "роботы ИИ",
        "ИИ чипы",
        "заработок на ИИ",
        "ИИ бизнес",
        "обучение нейросетям",
        "ChatGPT нейросеть",
        "Grok ИИ",
        "Qwen ИИ",
        "генерация нейросетями",
        "искусственный интеллект новости",
    ],
    "дизайн и креатив": [
        "AI design tools", "AI graphic design", "Midjourney design", "AI UX UI design",
        "Figma AI", "AI 3D modeling", "AI animation", "AI photo editing", "AI logo design",
        "AI art tutorial",
        "нейросети для дизайна", "ИИ дизайн логотип", "нейросеть для анимации",
        "нейросеть обработка фото", "midjourney уроки", "нейросети для 3D", "ИИ арт нейросеть",
    ],
    "стартапы и бизнес": [
        "AI startup launch", "AI founder story", "build AI SaaS", "solo founder AI",
        "indie hacker AI", "AI startup pitch", "Y Combinator AI", "AI business automation",
        "AI marketing automation", "AI for small business",
        "ИИ стартап", "бизнес на нейросетях", "внедрение ИИ в бизнес", "ИИ для малого бизнеса",
        "ИИ автоматизация бизнеса", "ИИ маркетинг",
    ],
    "запуски и анонсы": [
        "new AI tool launch", "AI product demo", "AI announcements this week", "AI release update",
        "AI app launch", "AI feature update", "brand new AI tool",
        "новая нейросеть обзор", "запуск ИИ сервиса", "обзор новой нейросети",
        "новинки нейросетей", "что нового в нейросетях",
    ],
    "влоги и личный опыт": [
        "AI vlog", "day in the life AI engineer", "build in public AI", "I built with AI",
        "my AI workflow", "AI setup tour",
        "вайб кодинг влог", "делаю с ИИ", "мой опыт с нейросетями", "как я использую нейросети",
        "ИИ влог", "автоматизирую работу с ИИ",
    ],
    "обучение и навыки": [
        "AI tutorial beginner", "AI course free", "prompt engineering tutorial", "learn AI skills",
        "AI career path",
        "обучение нейросетям с нуля", "промпты для нейросетей", "профессия ИИ", "уроки по нейросетям",
    ],
    "игры и развлечения": [
        "AI game development", "AI in gaming", "AI NPC", "нейросети для игр", "ИИ в играх",
    ],
    "голос и дубляж": [
        "AI voice cloning", "AI dubbing", "AI music cover", "озвучка нейросетью", "ИИ дубляж",
    ],
    "безопасность и дипфейки": [
        "LLM jailbreak", "AI security risk", "AI deepfake", "AI scam warning",
        "дипфейк нейросеть", "мошенники нейросети",
    ],
    "локальный ИИ и железо": [
        "local LLM setup", "run AI locally", "self-hosted AI", "AI PC build",
        "локальные нейросети на компьютер", "нейросети на своём железе", "ИИ без интернета",
        "нейросеть на ноутбуке",
    ],
    "кодинг-агенты": [
        "Claude Code", "Cursor AI editor", "MCP protocol", "AI browser agent",
        "computer use agent", "AI coding agent review",
        "Claude Code обзор", "Cursor ИИ редактор", "ИИ агент для кода",
    ],
    "работа и деньги": [
        "AI jobs hiring", "AI freelancing", "make money with AI", "AI side hustle",
        "работа с ИИ", "фриланс с ИИ", "заработок на нейросетях 2026", "ИИ подработка",
    ],
    "финансы и ИИ": [
        "AI trading bot", "AI in finance", "algorithmic trading AI", "ИИ трейдинг",
        "нейросети в финансах",
    ],
    "виральное и хобби": [
        "AI pet video", "AI ASMR", "AI humor", "AI generated movie",
        "нейросети приколы", "ИИ видео приколы", "нейросеть сделала",
    ],
}

# --- Ротация порций поисковых запросов ------------------------------------

# Сколько запросов берём за один прогон (40 x 100 units = 4000 units).
COLLECT_QUERIES_PER_RUN = 40
# Файл с курсором ротации (JSON: cursor / updated_at / full_len).
QUERY_ROTATION_FILE = "data/query_rotation.json"


def shorts_max_seconds(cfg: Any = None) -> int:
    """Действующий порог длительности вероятного шортса, секунды.

    Приоритет: переменная окружения SHORTS_MAX_SECONDS (читается в момент
    вызова, поэтому работает и после load_env), затем SHORTS_MAX_SECONDS из
    переданного cfg, затем константа модуля. Некорректное значение молча
    пропускается — порог не должен ломать сбор.
    """
    raw = os.environ.get(SHORTS_MAX_SECONDS_ENV)
    if raw is not None and str(raw).strip() != "":
        try:
            value = int(str(raw).strip())
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    if cfg is not None:
        fallback = getattr(cfg, "SHORTS_MAX_SECONDS", None)
        if fallback is not None:
            try:
                value = int(fallback)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
    return int(SHORTS_MAX_SECONDS)


def min_interval_for_speed_seconds(cfg: Any = None) -> int:
    """Действующий порог интервала для расчёта скорости, секунды.

    Скорость (views_per_day/views_per_hour) считается только если между
    замерами прошло не меньше этого порога. Приоритет: переменные окружения
    MIN_INTERVAL_FOR_SPEED_SECONDS / MIN_INTERVAL_FOR_SPED_SECONDS (читаются в
    момент вызова), затем одноимённый атрибут cfg, затем константа модуля.
    Некорректное значение молча пропускается — порог не должен ломать сбор.
    """
    for env_name in (MIN_INTERVAL_FOR_SPEED_SECONDS_ENV, MIN_INTERVAL_FOR_SPED_SECONDS_ENV):
        raw = os.environ.get(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    if cfg is not None:
        for attr in ("MIN_INTERVAL_FOR_SPEED_SECONDS", "MIN_INTERVAL_FOR_SPED_SECONDS"):
            fallback = getattr(cfg, attr, None)
            if fallback is None:
                continue
            try:
                value = int(fallback)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return int(MIN_INTERVAL_FOR_SPEED_SECONDS)


def viral_half_life_days(cfg: Any = None) -> float:
    """Действующий период полураспада свежести индекса, дней.

    Приоритет: переменная окружения VIRAL_HALF_LIFE_DAYS (читается в момент
    вызова), затем одноимённый атрибут cfg, затем константа модуля. Некорректное
    значение молча пропускается: настройка не должна ломать отчёт.
    """
    raw = os.environ.get(VIRAL_HALF_LIFE_DAYS_ENV)
    if raw is not None and str(raw).strip() != "":
        try:
            value = float(str(raw).strip())
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    if cfg is not None:
        fallback = getattr(cfg, "VIRAL_HALF_LIFE_DAYS", None)
        if fallback is not None:
            try:
                value = float(fallback)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
    return float(VIRAL_HALF_LIFE_DAYS)


def _env_positive_int(name: str) -> int | None:
    """Целое из переменной окружения; пустое/битое/отрицательное — None."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def viral_min_views(fmt: str | None = None, cfg: Any = None) -> int:
    """Действующий порог показов честного топа, просмотры.

    Приоритет: уточняющая переменная окружения потока (VIRAL_MIN_VIEWS_SHORTS /
    VIRAL_MIN_VIEWS_LONG), затем общая VIRAL_MIN_VIEWS, затем одноимённый
    атрибут cfg, затем константа модуля. ``fmt='short'/'long'`` выбирает
    поток; любое другое значение — общий порог. Некорректное значение молча
    пропускается: настройка не должна ломать отчёт.
    """
    env_names: list[str] = []
    attr_names: list[str] = []
    if fmt == "short":
        env_names.append(VIRAL_MIN_VIEWS_SHORTS_ENV)
        attr_names.append("VIRAL_MIN_VIEWS_SHORTS")
    elif fmt == "long":
        env_names.append(VIRAL_MIN_VIEWS_LONG_ENV)
        attr_names.append("VIRAL_MIN_VIEWS_LONG")
    env_names.append(VIRAL_MIN_VIEWS_ENV)
    attr_names.append("VIRAL_MIN_VIEWS")
    for env_name in env_names:
        value = _env_positive_int(env_name)
        if value is not None:
            return value
    if cfg is not None:
        for attr in attr_names:
            fallback = getattr(cfg, attr, None)
            if fallback is None:
                continue
            try:
                value = int(fallback)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
    return int(VIRAL_MIN_VIEWS)


def viral_axis_cap(cfg: Any = None) -> float:
    """Действующий потолок отношения осей лайков/комментариев.

    Приоритет: переменная окружения VIRAL_AXIS_CAP (читается в момент вызова),
    затем одноимённый атрибут cfg, затем константа модуля. Значение <= 0
    означает «без потолка». Некорректное значение молча пропускается.
    """
    raw = os.environ.get(VIRAL_AXIS_CAP_ENV)
    if raw is not None and str(raw).strip() != "":
        try:
            value = float(str(raw).strip())
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    if cfg is not None:
        fallback = getattr(cfg, "VIRAL_AXIS_CAP", None)
        if fallback is not None:
            try:
                value = float(fallback)
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                pass
    return float(VIRAL_AXIS_CAP)


def is_probable_shorts(duration: int | None, cfg: Any = None) -> int | None:
    """1 — вероятный шортс по длительности, 0 — полное видео, None — нет данных.

    Ровно на пороге (например 180 с) — ещё шортс; на секунду больше — уже нет.
    """
    if duration is None:
        return None
    try:
        seconds = int(duration)
    except (TypeError, ValueError):
        return None
    return int(0 < seconds <= shorts_max_seconds(cfg))


def query_language(query: str) -> str:
    """Вернуть язык запроса: 'ru' при кириллице, иначе 'en'."""
    for ch in query:
        if "а" <= ch.lower() <= "я" or ch.lower() == "ё":
            return "ru"
    return "en"


def search_params(query: str) -> dict:
    """Дополнительные параметры search.list для запроса."""
    if query_language(query) == "ru":
        return {"relevanceLanguage": "ru", "regionCode": "RU"}
    return {}


# --- Закрытый список тем (SCHEMA.md, раздел 2.8) --------------------------

TOPICS: tuple[str, ...] = (
    "модели и релизы",
    "запуски и анонсы",
    "агенты и автоматизация",
    "кодинг и разработка",
    "чипы и железо",
    "дата-центры и энергия",
    "роботы и физический ИИ",
    "деньги и сделки",
    "стартапы и бизнес",
    "регулирование и безопасность",
    "наука и медицина",
    "медиа и творчество",
    "дизайн и креатив",
    "влоги и личный опыт",
    "обучение и навыки",
    "ИИ-инструменты для обычных людей",
    "прочее",
)

# Быстрая проверка «тема из закрытого списка».
TOPIC_SET = frozenset(TOPICS)


def is_valid_topic(topic: str | None) -> bool:
    """True, если тема входит в закрытый список."""
    return topic in TOPIC_SET


# --- Разбор обложек топ-видео (vision) ------------------------------------

# Модель и точка входа OpenAI-совместимого API Moonshot (Kimi).
THUMB_MODEL = "kimi-k2.6"
THUMB_ENDPOINT = "https://api.moonshot.ai/v1/chat/completions"
THUMB_PROMPT_VERSION = "v2"   # v2: main_text вместо простыни, objects<=6, запрет OCR скриншотов
THUMB_MIN_MULT = 3.0            # порог выброса: просмотры / медиана канала
THUMB_MIN_CHANNEL_N = 5         # минимум видео канала (того же формата, только ИИ) с известными
                                # просмотрами: при меньшем числе медиана недостоверна и кратность врёт
                                # (живой случай: x995 у канала с 2 видео)
THUMB_MAX_PER_CHANNEL = 2       # не больше двух обложек с одного канала (0 = без ограничения)
THUMB_MAX_PER_RUN = 30          # сколько обложек разбирать за суточный цикл
THUMB_DAILY_BUDGET_USD = 0.25   # денежный предохранитель на прогон
THUMB_TIMEOUT_SEC = 120         # таймаут запроса к модели
THUMB_IMAGE_TIMEOUT_SEC = 25    # таймаут скачивания обложки
THUMB_PRICE_IN_MISS = 0.95      # $ за 1M входных токенов (cache miss)
THUMB_PRICE_IN_CACHE = 0.16     # $ за 1M входных токенов (cache hit)
THUMB_PRICE_OUT = 4.00          # $ за 1M выходных токенов


# --- Сбор верхних комментариев (video_comments) ---------------------------

# Сколько видео разбирать за прогон команды comments.
COMMENT_MAX_VIDEOS_PER_RUN = 20
# Сколько верхних комментариев брать с одного видео (отбор по лайкам).
COMMENT_MAX_PER_VIDEO = 20
# Сколько комментариев запрашивать у API за один вызов. Стоимость вызова не
# зависит от maxResults (1 unit), поэтому берём максимум: при order=relevance
# API отдаёт до 100, а «лучший по лайкам» может быть внутри этой сотни, а не
# среди первых 20. Сохраняем всё равно только COMMENT_MAX_PER_VIDEO лучших.
COMMENT_FETCH_MAX = 100
# Свежесть: если по видео уже есть строки моложе стольких суток — пропуск.
COMMENT_REFRESH_DAYS = 7


# --- Загрузка переменных окружения ----------------------------------------


def load_env(path: str = ENV_PATH) -> int:
    """Прочитать .env и проставить переменные через os.environ.setdefault.

    Построчно разбирает KEY=VALUE, пропускает пустые строки и комментарии,
    снимает обрамляющие кавычки у значения. Уже заданные извне переменные не
    перетираются. Возвращает число реально проставленных переменных; если файла
    нет — 0 (не падаем).
    """
    src = Path(path)
    if not src.exists():
        return 0
    count = 0
    with src.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            already = key in os.environ
            os.environ.setdefault(key, value)
            if not already:
                count += 1
    return count


# --- Загрузка ключей YouTube ----------------------------------------------


def load_keys(path: str | Path | None = None) -> list[str]:
    """Прочитать только ключи YouTube (обратно совместимая обёртка).

    Разбор формата живёт в одном месте — load_key_projects(); здесь остаётся
    прежний контракт «список строк» для вызывающего кода и внешних тестов.
    """
    return [key for key, _project in load_key_projects(path)]


def load_key_projects(
    path: str | Path | None = None,
) -> list[tuple[str, str | None]]:
    """Прочитать ключи YouTube и их проекты из JSON-файла.

    Поддерживаются оба формата, выбор формата не ломает прежнее поведение:

    * старый: ``{"keys": ["AIza...", "AIza..."]}`` — каждый ключ считается
      отдельным проектом, проект неизвестен (None);
    * с привязкой: ``{"keys": [{"key": "AIza...", "project": "<gcp-project-number>"},
      ...]}`` — ключи с одинаковым ``project`` считаются одним проектом.

    Дополнительно принимаются верхнеуровневый список и поле ``api_key``.
    Битый или неизвестный элемент списка пропускается с предупреждением в лог,
    а не роняет клиент. Ключи не логируются и не печатаются.
    """
    src = Path(path) if path is not None else YT_KEYS
    if not src.exists():
        return []
    try:
        with src.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("yt_keys: не удалось прочитать %s: %s", src, exc)
        return []

    if isinstance(raw, dict):
        items = raw.get("keys", [])
    else:
        items = raw
    if not isinstance(items, (list, tuple)):
        log.warning(
            "yt_keys: ожидался список ключей в %s, получено %s",
            src,
            type(items).__name__,
        )
        return []

    out: list[tuple[str, str | None]] = []
    for idx, item in enumerate(items):
        if isinstance(item, str):
            key = item
            project: str | None = None
        elif isinstance(item, dict):
            key = item.get("key") or item.get("api_key") or ""
            raw_project = item.get("project")
            if raw_project is None or raw_project == "":
                project = None
            elif isinstance(raw_project, (str, int)):
                project = str(raw_project).strip() or None
            else:
                log.warning(
                    "yt_keys: элемент %d: поле project не строка/число — "
                    "считаю проект неизвестным",
                    idx,
                )
                project = None
        else:
            log.warning(
                "yt_keys: элемент %d имеет неизвестный тип %s — пропускаю",
                idx,
                type(item).__name__,
            )
            continue
        key = str(key).strip()
        if not key:
            log.warning("yt_keys: элемент %d без непустого ключа — пропускаю", idx)
            continue
        out.append((key, project))
    return out


def now_ts() -> int:
    """Текущий unixtime (целые секунды)."""
    return int(time.time())


# Московское время (UTC+3, без перехода на летнее с 2014 года). Суточные
# отчёты и имя файла отчёта считаются по МСК, чтобы совпадать с сутками базы.
MSK_TZ = datetime.timezone(datetime.timedelta(hours=3), "MSK")


def now_msk() -> datetime.datetime:
    """Текущее время по МСК (UTC+3)."""
    return datetime.datetime.now(MSK_TZ)
