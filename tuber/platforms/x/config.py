"""Конфигурация X-платформы монорепозитория: пути, лимиты, пул инстансов, тиры.

Р3.1: пул инстансов расширяется правкой строки INSTANCES здесь, без изменения кода
брокера. Р3.2/Р3.3: лимитер и суточный потолок задаются здесь же.

Пути (ТЗ-3 §2). Проект переехал из ``/root/tuber-x`` в ``tuber/platforms/x``
монорепозитория, поэтому:

* ``ROOT`` — корень МОНОрепозитория (``/root/tuber``), а не каталог проекта:
  ``data/``, ``reports/`` и ``docs/`` теперь общие;
* ``DB_PATH`` — ЕДИНАЯ база ``data/tuber.db``. Прежние способы выбора базы
  сохранены: ``TUBER_X_DB`` (историческое имя приёмки) и ``TUBER_DB``
  (короткое имя ТЗ «починка виральности»), плюс ``TUBER_DB`` читается ядром
  (:func:`tuber.config.db_path`) — так работает и ``--db`` у CLI.
"""
import os

try:  # обычный импорт пакета
    from tuber import config as _core_config
except ImportError:  # запуск как скрипта из каталога платформы
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))
    from tuber import config as _core_config

# ---------------------------------------------------------------- пути (Р1)
# Файл лежит в tuber/platforms/x/config.py — корень репозитория на три уровня выше.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
DOCS_DIR = os.path.join(ROOT, "docs")

# Переменная окружения нужна тестам и параллельным прогонам: своя БД на прогон.
DB_PATH = os.environ.get("TUBER_X_DB") or _core_config.db_path()
RUN_LOG_PATH = os.path.join(LOG_DIR, "tuber_x.log")

# Таймаут ожидания блокировки записи, с: в legacy ``db.connect`` был ровно 30.
# В единой базе пишут несколько платформ, поэтому значение сохранено (ядро
# использует своё ``config.BUSY_TIMEOUT_MS``).
BUSY_TIMEOUT_SEC = 30.0

# --------------------------------------------------- пул инстансов Nitter (Р3.1)
# Строка ниже — единственное место расширения пула.
#
# Замер 17.09.2026 (перебор 56 публичных хостов Nitter: список инстансов из вики
# проекта + известные адреса; критерий «RSS одного аккаунта отдаёт посты» + поиск).
# Живых стабильных всего 2, они и оставлены в пуле. Отброшены:
#   * nitter.kareem.one — HTTP 502 (мёртв). Был аварийным инстансом, отсюда 683
#     отказа из 1 064 запросов ленты (39%) в журнале транспорта;
#   * twiiit.com — 403 при серии запросов (своя защита), НЕ включать;
#   * nitter.cz — 502/403/400 вперемешку, НЕ включать.
# Оставленные адреса: nitter.jaydenha.uk (8/8 успешных, 1.2 с, поиск 19 постов)
# и nitter.netbub.com (8/8 успешных, 1.1 с, поиск 19 постов).
INSTANCES = [
    "https://nitter.jaydenha.uk",
    "https://nitter.netbub.com",
]

# ------------------------------------------------------------- лимитер (Р3.2)
# Замеренный предел ~8 запросов / 30 с на инстанс. Работаем ниже предела.
RATE_MAX_REQUESTS = 6          # запросов на инстанс в окне
RATE_WINDOW_SEC = 60.0         # длина окна, с
MIN_REQUEST_INTERVAL_SEC = 2.0  # минимум между двумя запросами к одному инстансу

# --------------------------------------------------------- суточный учёт (Р3.3)
# Ёмкость канала (замер): 960 зап/ч = 23 040 зап/сутки. Потолок — 80%.
DAILY_REQUEST_CAP = 18_000

# ------------------------------------------------------------ транспорт
HTTP_TIMEOUT_SEC = 25.0
USER_AGENT = "tuber-x/1.0 (+rss collector)"
# Сколько раз job может переспросить инстанс (429/403/пусто), прежде чем сдаться
MAX_JOB_ATTEMPTS = 3

# --------------------------------------------------------- health-check (Р3.4)
HEALTH_PATH = "/nasa/rss"
HEALTH_MIN_ITEMS = 15          # HTTP 200 с меньшим числом item — не живой
HEALTH_TTL_SEC = 600           # не чаще одного раза в 10 минут на инстанс

# ----------------------------------------------------------------- кеш (Р3.6)
CACHE_TTL_SEC = 600            # один и тот же URL — не чаще раза в 10 минут

# ------------------------------------------------------------- cooldown (Р3.5)
COOLDOWN_429_SEC = 180
COOLDOWN_429_HARD_SEC = 900    # второй 429 подряд
COOLDOWN_403_SEC = 86_400      # 403/451 — инстанс недоступен на сутки

# ---------------------------------------------------------- приоритеты (Р3.7)
PRIORITIES = {
    "critical": 0,   # сторожа
    "collect": 1,    # обход реестра
    "discover": 2,   # дискавери
    "backfill": 3,   # бэкфилл
}

# --------------------------------------------------------------- тиры (Р4.5, Р5.1)
TIERS = {
    "A": {"max_accounts": 200, "interval_hours": 1, "max_pages": 3},
    "B": {"max_accounts": 1000, "interval_hours": 3, "max_pages": 3},
    "C": {"max_accounts": None, "interval_hours": 24, "max_pages": 1},
}
DEFAULT_TIER = "C"

# ------------------------------------------------------------------ Р5.2
BACKFILL_MAX_PAGES = 3         # не более 3 страниц за прогон
CURSOR_LAG_POSTS = 20          # «отстаёт», если последний пост старше 20 постов назад

# ----------------------------------------------------- валидатор реестра (Р4.3)
HANDLE_RE = r"^[A-Za-z0-9_]{1,15}$"
TRUSTED_TIERS = ("A", "B")     # доверенное ядро для co-occurrence (Р4.3.2)
VERIFY_MIN_ITEMS = 15          # Р4.3.1
VERIFY_MIN_MENTIONS = 2        # Р4.3.2 — не менее 2 разных авторов ядра
VERIFY_POSTS_PER_DAY_MIN = 0.2  # Р4.3.3
VERIFY_POSTS_PER_DAY_MAX = 20.0
VERIFY_MIN_CV = 0.15           # Р4.3.4 (при >= 10 постах)
VERIFY_CV_MIN_POSTS = 10
VERIFY_MAX_LINK_RATIO = 0.7    # Р4.3.5
VERIFY_MAX_DUP_RATIO = 0.3
VERIFY_MAX_RT_RATIO = 0.8
DAILY_NEW_ACTIVE_CAP = 50      # Р4.4 / ТЗ-2 Р3.3
# ------------------------------------------------------------- ТЗ-2 дискавери
DISCOVERY_DAILY_BUDGET = 120   # Р1.3 (методология §10): суммарный бюджет запросов/сутки
DISCOVERY_MAX_PAGES_PER_QUERY = 2   # Р2.2: не более 2 страниц на запрос за прогон
DISCOVERY_MAX_VERIFY_DEFAULT = int(os.environ.get("TUBER_X_MAX_VERIFY") or 30)
                                                    # Р3.1: кандидатов в верификацию за сутки
# ТЗ-C Р2: боевое значение 60 задаётся средой TUBER_X_MAX_VERIFY в обёртке
# `scripts/x/tuber_x_discover.sh`; дефолт 30 сохранён как безопасный откат.

# ------------------------------------------------- трёхступенчатый статус (Р2-БИС)
# candidate -> provisional -> active
PROVISIONAL_MAX = 300          # Р2-БИС.3: не более 300 provisional одновременно
BOOTSTRAP_MIN_ACTIVE = 20      # Р2-БИС.2: пока active < 20 — фаза bootstrap
TENURE_DAYS = int(os.environ.get("TUBER_X_TENURE_DAYS") or 14)
# Р2-БИС.2(б): непрерывный сбор для промоушена. ТЗ-C Р2: боевое значение 4
# задаётся средой TUBER_X_TENURE_DAYS в обёртке scripts/x/tuber_x_discover.sh;
# дефолт 14 сохранён (откат = убрать переменную).
PROMOTE_MIN_AI_DENSITY = 0.5   # Р2-БИС / Р4
PROVISIONAL_MENTION_MIN_POSTS = 10      # Р2-БИС.1: provisional-упоминатель
PROVISIONAL_MENTION_MIN_AI_DENSITY = 0.5
TENURE_REJECT_FREE_DAYS = 30   # Р2-БИС.2: после bootstrap путь (б) требует чистоты
# ТЗ-C Р5: сколько раз подряд неудачная верификация по items_lt_15 допустима до
# окончательной отбраковки. Попытки 1..N-1 -> кандидат остаётся в очереди (retry),
# N-я -> rejected (прежнее поведение). Счётчик живёт в candidate.meta_json.
VERIFY_MAX_ATTEMPTS = 3

# ------------------------------------------------------- тиры по плотности (Р3.4)
TIER_A_MIN_AI_DENSITY = 0.7
TIER_A_MIN_POSTS_PER_DAY = 1.0
TIER_B_MIN_AI_DENSITY = 0.5
TIER_A_MAX = 200
TIER_B_MAX = 1000

# -------------------------------------------- эвристика ai_density (Р4.2, до ТЗ-3)
AI_DENSITY_POSTS = 20          # Р4.1: доля считается по последним 20 постам
AI_DENSITY_SRC_HEURISTIC = "heuristic"
# ТЗ-6 задача 1: предфильтр по этим словам ВЫКЛЮЧЕН по умолчанию. Список
# работает только при явно включённом режиме (`cli classify --prefilter`).
# Замер 15.09.2026: фильтр отсеивал 12.5% постов, из них половину — по делу,
# при экономии ~0.24 USD/мес. Поэтому по умолчанию к модели уходят все посты.
AI_TEXT_INDICATORS = (
    "ai", "llm", "gpt", "agent", "model", "prompt",
    "нейросет", "ии", "модель", "агент", "промпт",
)
AI_LINK_INDICATORS = (
    "arxiv.org", "huggingface.co", "github.com", "openai.com",
    "anthropic.com", "deepmind", "ai.google", "techcrunch.com",
)

# ------------------------------------------------------- стоп-лист / отбраковка (Р2.5)
SERVICE_HANDLES = ("search", "explore", "i", "home", "settings", "notifications",
                   "messages", "compose", "tos", "privacy")

# ------------------------------------------- ТЗ-18: новостники-гиганты в фидах
# Хендлы новостных изданий и агрегаторов БЕЗ тематики ИИ. Такой аккаунт в X
# даёт поток разноплановых новостей: тематическая плотность ИИ у него низкая,
# поэтому он не годится ни в реестр, ни в качестве источника-наблюдателя.
# Фильтр применяется ТОЛЬКО к приёму кандидатов из фидов (ТЗ-18), сбор/дискавери
# его не затрагивает.
#
# Факт (TG-фид 15.09.2026, 152 строки kind="x"): из фида реально отсеялись по
# этому списку `foxnews` (3 упоминания), `francenews24`, `news`,
# `fetchpakistan`, `bashareport` (по 1). Обоснование каждого — в docs/REPORT-18.md.
# Остальные записи — превентивные (глобальные гиганты того же класса), в
# текущем фиде не встречаются.
NEWS_GIANTS = (
    # фактические из живого TG-фида (проверено 15.09.2026)
    "foxnews", "francenews24", "fetchpakistan", "bashareport", "news",
    # ТЗ-21/D-4: фактические из живого фида tuber-os (kind=x, проверено
    # 15.09.2026). Новостные сети вверху фида, которых не хватало списку;
    # числа упоминаний — в docs/REPORT-21.md.
    "ndtv",          # 370 упоминаний, индийская новостная сеть
    "khabar_gaon",   # 249, хиндиязычный новостной канал
    "bloombergradio", "bloombergtv", "bsurveillance", "bpolitics",  # 146/88/88/87
    "foxbusiness",   # 137
    "firstpost",     # 105
    "bbgenespanol", "bbgoriginals",  # 105/87
    "ajenglish",     # 100
    # превентивно: глобальные новостные издания и агрегаторы
    "reuters", "nytimes", "washingtonpost", "theguardian", "wsj", "bloomberg",
    "apnews", "bbc", "bbcnews", "cnn", "nbcnews", "abcnews", "cbsnews",
    "aljazeera", "usatoday", "politico", "axios", "businessinsider",
    "forbes", "dwnews", "skynews", "independent", "huffpost", "vice",
)
NEWS_GIANTS_SET = frozenset(h.lower() for h in NEWS_GIANTS)

# ------------------------------------------------------------- ТЗ-18: приём фидов
# Сколько кандидатов принимает один прогон import_candidates по умолчанию.
FEED_IMPORT_LIMIT = 500
# Порог упоминаний НЕ заводится заново: берётся из конфига фильтра проекта
# (`VERIFY_MIN_MENTIONS`, Р4.3.2 — не менее 2 разных авторов ядра). Значение
# намеренно не меняется.

# ------------------------------------------------------------- метрики (Р5.4)
RECENT_POSTS_WINDOW = 100      # posts_per_day / cv_interval по последним 100 постам

# -------------------------------------------------- выборка даты (Р2, инвариант)
FUTURE_TOLERANCE_SEC = 2 * 3600  # «в будущем более чем на 2 часа» — брак
MIN_VALID_YEAR = 2000            # pubDate с годом < 2000 не берём, идём в snowflake

# --------------------------------------------------------- идентификация агента
ADDED_BY = "jcode"

# Режим брокера: True — фоновый диспетчер (демон), False — синхронный drain
# (по умолчанию: прогоны по крону последовательны, приоритеты всё равно соблюдаются).
BROKER_ASYNC = False

# ==================================================================== ТЗ-4
# Каналы сбора (Р1 матрица каналов). Все сетевые запросы идут ТОЛЬКО через
# channels.py (роутер) и nitter_broker.py (транспорт).

# --------------------------------------------------- владение инстансом (5-БИС)
# Вариант (а) из ТЗ-4 5-БИС: за Tuber-x закреплён один инстанс, за боевым
# демоном CryptoGraph — другой. Это единственная правка, не требующая чужих правок.
#   "dedicated" — сначала свой инстанс, чужой оставлен аварийным fallback-ом;
#   "strict"    — ТОЛЬКО свой инстанс (чужой не трогаем вообще);
#   "pool"      — старое поведение ТЗ-1 (весь пул равноправно).
INSTANCE_OWNERSHIP = {
    "tuber_x": "https://nitter.jaydenha.uk",
    # У демона CryptoGraph свой инстанс мёртв (kareem.one → 502), поэтому он
    # закреплён за живым адресом пула (замер 17.09.2026).
    "cryptograph": "https://nitter.netbub.com",
}
INSTANCE_MODE = os.environ.get("TUBER_X_INSTANCE_MODE", "dedicated")

# Вариант (б), оставлен как шаг развития: общий файловый семафор с записью
# времени запросов (читает и демон CryptoGraph). None — выключен.
SHARED_SEMAPHORE_PATH = os.environ.get("TUBER_X_NITTER_LOCK") or None
SHARED_SEMAPHORE_MAX = 8       # запросов
SHARED_SEMAPHORE_WINDOW = 30.0  # за окно, с (замеренный предел инстанса)

# ------------------------------------------------------------- канал cdn_tweet
# CDN tweet-result: штатный ОБОГАТИТЕЛЬ (метрики + текст + точное UTC-время).
CDN_HOST = "cdn.syndication.twimg.com"
CDN_PATH = "/tweet-result"
CDN_RATE_MAX = 2               # не более 2 запросов...
CDN_RATE_WINDOW_SEC = 1.0      # ...в секунду
CDN_MIN_INTERVAL_SEC = 0.35    # пауза между запросами
CDN_429_PAUSE_SEC = 60         # при 429 — пауза 60 с
CDN_MAX_RETRIES = 3            # повторов не более 3 (60/120/240)
CDN_USER_AGENT = "Mozilla/5.0 (compatible; tuber-x/1.0)"

# -------------------------------------------------------- канал synd_timeline
# Ленточный syndication: ПРЕМИАЛЬНЫЙ и РАЗОВЫЙ. Блок переживает 15 минут тишины.
SYND_HOST = "syndication.twitter.com"
SYND_PATH = "/srv/timeline-profile/screen-name/"
SYND_REFERER = "https://platform.twitter.com/"   # без него 429 (питфолл 7.1)
SYND_WINDOW_BUDGET = 5         # не более 5 запросов в окне
SYND_WINDOW_PAUSE_SEC = 1200   # затем пауза 20 минут
SYND_DAILY_BUDGET = 5          # и не более 5 аккаунтов в сутки (2.3)

# -------------------------------------------------------------- канал x_ssr
# x.com/<handle>: аварийный дублёр, включается только при деградации Nitter.
XSSR_HOST = "x.com"
XSSR_RATE_MAX = 1              # не более 1 запроса в секунду
XSSR_RATE_WINDOW_SEC = 1.0
# ТЗ-10 2.2: пауза между запросами резерва — не менее 2 с (замер: x.com держит
# ~2,05 с на запрос, отказов нет).
XSSR_MIN_INTERVAL_SEC = 2.0
# ТЗ-10 2.1/2.2: деградация Nitter = суммарные ФАКТИЧЕСКИЕ отказы сбора
# (fail_streak/collect_fail_streak по всем инстансам) достигли порога. Резерв
# включается по факту отказа в ТОМ ЖЕ прогоне, а не через ~3 цикла health-проб.
XSSR_DEGRADED_STREAK = 3
# ТЗ-10 2.2: потолок обращений к резерву за один прогон сбора.
XSSR_MAX_PER_RUN = int(os.environ.get("TUBER_XSSR_MAX_PER_RUN") or 50)


# ------------------------------------------------------------- обогащение (2.2)
ENRICH_BATCH = 400             # LIMIT batch
ENRICH_MIN_TEXT_LEN = 40       # текст из CDN пишем, только если в базе короче
ENRICH_LONG_TEXT_MIN = 200     # сторож: is_long=1 и текст короче — обрезан

# ------------------------------------------------------------------ скоринг (4)
# Веса стартовые, калибруемая часть (зафиксировано в METHODOLOGY.md).
SCORE_W_ENGAGE = 0.45
SCORE_W_SPREAD = 0.35
SCORE_W_FIRST = 0.20
SPREAD_AUTHOR_WEIGHT = 1.5     # вес одного независимого автора в графе
SPREAD_VERIFIED_BONUS = 0.10   # +10% к весу аккаунта при verified=1 (не фильтр!)
VELOCITY_TARGET_HOURS = 6.0    # фиксированный возраст для velocity_6h
FIRST_MOVER_WINDOW_HOURS = 72  # окно поиска второго независимого автора

# ------------------------------------------------------------------- сторож (6)
HEALTH_STALE_HOURS = 6         # нет новых постов за 6 ч
HEALTH_FAIL_RATE_MAX = 0.50    # доля отказов канала за час > 50% (ТЗ-5 задача 2)
HEALTH_CDN_429_PER_HOUR = 20   # cdn_tweet 429 > 20 за час
HEALTH_LONG_TEXT_GAP_RATIO = 0.10  # доля is_long=1 с текстом < 200 больше 10%
# ТЗ-3 Р5: дополнительные пороги сторожа
HEALTH_TIER_A_STALE_HOURS = 3      # свежесть TIER-A
HEALTH_DATE_VALID_MIN = 0.99       # валидность дат за сутки
HEALTH_QUOTA_MAX = 0.80            # доля суточного потолка квоты
HEALTH_REGISTRY_GROWTH_HOURS = 48  # прежнее окно роста реестра (справочно, без WARN)
# ТЗ-11 задача 3: рост реестра — осмысленные пороги. Ноль новых `active` за
# 48 ч НЕ является сигналом: путь candidate -> provisional -> active занимает до
# 14 суток (ТЗ-2 Р2-БИС), поэтому проверяем реальную поломку расширения.
HEALTH_DISCOVER_STALE_HOURS = 36     # дискавери не запускался дольше -> ALERT
HEALTH_REGISTRY_STALL_DAYS = 7       # 7 суток без новых кандидатов и provisional -> WARN
HEALTH_MIN_REQUESTS_FAIL = 10      # меньше запросов — доля отказов не показательна
# ТЗ-5 задача 1: провенанс текста в расписании сторожа
HEALTH_TEXT_SHORT_RATIO_MAX = 0.05  # доля is_long=1 с текстом <= 300 за сутки
HEALTH_CDN_TEXT_RATIO_MAX = 0.05    # доля text_src='cdn' за сутки
HEALTH_COOLDOWN_MAX_MIN = 30        # инстанс Nitter в cooldown дольше — ALERT
# ТЗ-10 2.3: резерв x_ssr активен дольше 6 ч -> Nitter лежит долго (ALERT).
HEALTH_XSSR_ACTIVE_HOURS = 6
# ТЗ-10 2.3: фактических отказов сбора Nitter за последний час больше порога.
HEALTH_COLLECT_FAIL_PER_HOUR = 20

# ТЗ-5 задача 1: порог «подозрительно короткого» текста длинного поста.
# Замер CDN tweet-result 15.09.2026: текст обрезается на ~279–280 символах,
# поэтому всё, что не длиннее 300 при is_long=1, считается неполным.
FULLTEXT_CDN_LIMIT = 300

# ==================================================================== ТЗ-3
# ------------------------------------------------------- классификация (Р1)
# Клиент DeepSeek: ключ ТОЛЬКО из окружения, в коде ключей нет.
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_THINKING = {"type": "disabled"}   # thinking:disabled (проверено на живом API)
DEEPSEEK_TEMPERATURE = 0.0
DEEPSEEK_MAX_TOKENS = 4000
DEEPSEEK_TIMEOUT_SEC = 120.0
DEEPSEEK_MIN_INTERVAL_SEC = 0.3    # мягкий темп, чтобы не злить API
DEEPSEEK_RETRY_PAUSE_SEC = 5.0
CLASSIFY_BATCH = 20                # Р1.3: батчи по 20 постов
CLASSIFY_RETRIES = 1               # Р1.3: при невалидном ответе один повтор
CLASSIFY_RETRY_TEMPERATURE = 0.4   # повтор при невалидном JSON: при t=0 повтор бессмыслен
# Р1.5: дневной потолок Tuber-x, постов. ТЗ-B: значение по умолчанию 400 —
# безопасный откат; боевое значение задаётся средой TUBER_X_CLASSIFY_DAILY_CAP
# (обёртка расписания поднимает его на фазу разбора хвоста). Шаблон env-константы
# рядом: XSSR_MAX_PER_RUN выше.
CLASSIFY_DAILY_CAP = int(os.environ.get("TUBER_X_CLASSIFY_DAILY_CAP") or 400)
# ТЗ-B (G2): денежный предохранитель стадии, USD/сутки. Мягкий стоп без падения:
# прогон завершается, если фактический расход дня в `classify_daily.cost_usd`
# уже достиг порога. 0 (или отрицательное) отключает предохранитель.
CLASSIFY_DAILY_USD_CAP = float(os.environ.get("TUBER_X_CLASSIFY_DAILY_USD_CAP") or 1.0)
CLASSIFY_RECENT_CLAIMS = 25        # сколько свежих утверждений даём модели для novelty
CLASSIFY_MAX_TEXT_CHARS = 1200     # обрезка длинного текста в промпте
BUDGET_SCRIPT = "/root/.hermes/scripts/deepseek_budget.py"
BUDGET_HALT_FLAG = "/root/.hermes/.deepseek_halt"
# Замеренные 22.08.2026 тарифы DeepSeek (USD/токен) — для локальной оценки стоимости.
DEEPSEEK_PRICE_IN = 0.41384e-6
DEEPSEEK_PRICE_OUT = 1.69928e-6
DEEPSEEK_PRICE_CACHE = 0.01566e-6

# Тематические рубрики ТЗ-2 Р1.1, среди которых выбирает модель.
RUBRIC_TOPICS = (
    "релизы моделей",
    "агенты и автоматизация",
    "вайб-кодинг",
    "инструменты разработчика",
    "инфраструктура и железо",
    "инвестиции и раунды",
    "регулирование",
    "исследования и бенчмарки",
    "кейсы внедрения",
    "скандалы и риски",
)
CLAIM_TYPES = ("release", "funding", "research", "opinion", "news", "howto",
               "incident")
# Слаги рубрик ТЗ-2 (для приёма ответов модели вида slug, а не названия).
RUBRIC_SLUGS = (
    "model-releases",
    "agents-automation",
    "vibe-coding",
    "devtools",
    "infra-hardware",
    "funding",
    "regulation",
    "research-benchmarks",
    "adoption-cases",
    "incidents-risks",
)

# ------------------------------------------------------------- сюжеты (Р2)
# Пересборка кластеризации 16.09.2026 (закрывает D-08). Замер на боевом
# корпусе показал: пороги simhash 6/8/10/12/16 из 64 бит давали ОДИН результат
# (наблюдаемые расстояния Хэмминга 23-42), склейку определяло равенство НАБОРОВ
# сущностей. Теперь решение — взвешенное пересечение: редкость сущностей (IDF),
# текстовое подтверждение токенами и ограничение по времени.
# Старая константа оставлена только для совместимости старых снимков БД;
# в решении не участвует.
SIMHASH_THRESHOLD = 12         # не используется в решении (см. D-08), историческое
STORIES_WINDOW_HOURS = 72      # Р2.1: окно кластеризации
# Время как ограничение кандидатов (ТЗ п.1.5): измеренный максимум разрыва
# внутри подтверждённых пар «одно событие» — 26.2 ч (ByteDance $29.6B), поэтому
# 30 ч покрывает живые пары и отсекает длинные ложные связи.
STORY_TIME_GATE_HOURS = 30
# «Гигант» — сущность, встречающаяся в доле постов корпуса не ниже порога.
# Замер (окно 72 ч, 121 пост, 16.09.2026): при 0.10 гигантами ровно те слова,
# что названы в D-08, — openai (0.30), anthropic (0.32), nvidia (0.20),
# google (0.19), claude (0.17), meta (0.13), microsoft (0.12); следующее
# значение deepseek = 0.09 уже не гигант. Они не могут быть достаточным
# признаком сюжета. Порог масштабируется долей, а не числом постов.
STORY_GIANT_ENTITY_DOC_FREQ = 0.10
# Балл склейки = IDF-взвешенное пересечение токенов (доля от короткого поста)
#            + вес редких общих сущностей.
# Порог подобран замером на tests/data/story_pairs.jsonl (74 пары) со средней
# связью: при 0.10 F1 максимален (0.667), P=0.79/R=0.58; при 0.15 точность
# растёт, но recall падает (F1 0.615). Свип — в ответе и docs/REPORT-*.md.
STORY_MATCH_MIN = 0.10
STORY_ENTITY_IDF_W = 0.10      # максимальный вклад редких сущностей в балл
STORY_ENTITY_IDF_NORM = 3.0    # нормировка суммы IDF редких общих сущностей
# «Сильное» текстовое подтверждение: доля IDF-взвешенных общих токенов от
# короткого поста, при которой пара склеивается даже без общей редкой сущности.
# Замер: подтверждённые пары-перепечатки дают 0.45-1.00 (Glass Imaging 0.57,
# ByteDance 0.83), а ложные связи через слово-гигант — 0.04-0.20, поэтому
# 0.15 разделяет их и не даёт цепочке «одна компания — один сюжет» (T=0.05-0.08
# ещё склеивает обязательную тройку SpaceX/Nvidia/CrowdStrike, T>=0.15 — нет).
STORY_TEXT_STRONG = 0.15
# Даже при сильном текстовом совпадении нужно не меньше двух общих значимых
# слов: одна общая сущность-гигант не событие (замер: пара «openai + ...»
# на двух постах даёт 1 общее слово и не должна склеиваться).
STORY_MIN_SHARED_TOKENS = 2
STORY_MIN_TEXT_LEN = 20        # слишком короткий текст не кластеризуем по simhash
STORY_ROLE_PRIMARY = "primary"
STORY_ROLE_ECHO = "echo"
STORY_ROLE_AMPLIFIER = "amplifier"
STORY_ROLE_EXTENDER = "extender"

# ------------------------------------------------------------- оценки (Р3)
SIGNIFICANCE_AGE_EXP = 1.8     # степень затухания по возрасту
SIGNIFICANCE_SPREAD_W = 0.8    # вес ln(1+spread)
SIGNIFICANCE_ENGAGE_W = 1.0    # вес ln(1+velocity)
SIGNIFICANCE_AGE_24H = 24.0    # знаменатель (1 + часы/24)
SIGNIFICANCE_SNAPSHOT_HOURS = 6.0   # целевой возраст снимка метрик
SIGNIFICANCE_SNAPSHOT_TOL = 1.5     # допуск, ч
METRICS_STALE_HOURS = 24.0     # старше — метрики не считаем актуальными
FIRST_MOVER_HALFLIFE_DAYS = 30.0
DARKS_WINDOW_DAYS = 7
DARKS_MIN_STORIES = 5
DARKS_TOP_N = 20
SUSPECT_DUP_RATIO = 0.5        # Р3.4: все авторы с dup_ratio выше — suspect
SUSPECT_MIN_XCONF = 3
HEALTH_SUSPECT_MIN_XCONF = 5   # Р5: подозрительное совпадение

# ------------------------------------------------------------- отчёты (Р4)
# ТЗ-8 задача 2: каталог выдачи отчёта переопределяется переменной окружения
# TUBER_X_REPORT_DIR (нужна приёмке и параллельным прогонам — они обязаны писать
# во временный каталог и НЕ трогать рабочий `reports/`). Обёртка и CLI читают
# значение отсюда, хардкода `reports/` в коде больше нет.
REPORT_DIR = (os.environ.get("TUBER_X_REPORT_DIR")
              or os.path.join(ROOT, "reports"))
# Прежнее имя оставлено алиасом для совместимости; источник истины — REPORT_DIR.
REPORTS_DIR = REPORT_DIR
REPORT_MAIN_LIMIT = 10         # блок 1: до 10 сюжетов
REPORT_RU_LIMIT = 10           # блок 5: 5–10 позиций
REPORT_FIRST_MOVERS = 5        # блок 6: топ-5 first_mover_score
REPORT_TRANSLATE = True        # перевод в выдаче (Р1.6); False — только шаблоны
REPORT_TRANSLATE_BUDGET = 40   # максимум переводов за формирование отчёта


# ============================================ свой транспорт чтения X (ТЗ-43A/44)
# Внутренние GraphQL-операции x.com на сессии нашего выделенного аккаунта.
# Первый по приоритету канал платформы `x`; Nitter остаётся фолбэком.
CONFIG_DIR = os.path.join(ROOT, "config")
# Файл сессии: {"auth_token": "...", "ct0": "..."}, права строго 600.
# Путь переопределяется средой (приёмка и параллельные прогоны не трогают боевой).
X_SESSION_FILE = (os.environ.get("TUBER_X_SESSION_FILE")
                  or os.path.join(CONFIG_DIR, "x_session.json"))
# Карта queryId + набор фич из родного бандла X (кэш, TTL 24 ч).
X_QIDS_FILE = (os.environ.get("TUBER_X_QIDS_FILE")
               or os.path.join(CONFIG_DIR, "x_qids.json"))
# Общий выключатель транспорта (аварийный откат на Nitter).
X_SESSION_ENABLED = (os.environ.get("TUBER_X_SESSION_ENABLED", "1").lower()
                     not in ("0", "false", "no", "off"))
# Ограничитель под контролем: замер 21.09.2026 — 20 зап/мин аккаунт не держит
# (429 на 104-м запросе), 4 зап/мин держит уверенно. Берём замеренное значение
# и интервал 10 с; это внутрипроцессное сглаживание, а общий по всем процессам
# бюджет считает JOURNAL (см. ниже).
X_SESSION_RATE_MAX = int(os.environ.get("TUBER_X_SESSION_RATE_MAX") or 4)
X_SESSION_RATE_WINDOW_SEC = 60.0
X_SESSION_MIN_INTERVAL = float(os.environ.get("TUBER_X_SESSION_MIN_INTERVAL") or 10.0)
# Бюджет запросов сессии X считается ПО ЖУРНАЛУ transport_request, а не по памяти:
# тогда он общий для всех процессов (кронов) по построению.
# Окно: не больше X_SESSION_WINDOW_CAP запросов за X_SESSION_WINDOW_SEC секунд.
X_SESSION_WINDOW_CAP = int(os.environ.get("TUBER_X_SESSION_WINDOW_CAP") or 50)
X_SESSION_WINDOW_SEC = int(os.environ.get("TUBER_X_SESSION_WINDOW_SEC") or 900)
# Суточный потолок запросов за последние 24 ч: при исчерпании — запись в run_log
# и откат на Nitter.
X_SESSION_DAILY_CAP = int(os.environ.get("TUBER_X_SESSION_DAILY_CAP") or 800)
# ТЗ-43C: ленты через сессию по умолчанию ВЫКЛЮЧЕНЫ. Именно сплошной поток
# лент (20 зап/мин) и родил 429. По умолчанию ленты идут прежним путём (Nitter),
# а сессия обслуживает ТОЧЕЧНЫЕ задачи: followers (ТЗ-44) и метрики постов.
X_SESSION_FEDS_ENABLED = (
    os.environ.get("TUBER_X_SESSION_FEDS_ENABLED", "0").lower()
    not in ("0", "false", "no", "off"))
# Хост записи состояния транспорта в transport_instance (platform='x').
X_SESSION_HOST = "x.com"
# TTL кэша queryId/фич, с (сутки).
X_SESSION_QIDS_TTL_SEC = 86400
# Таймаут одного запроса к x.com, с (GraphQL отвечает быстрее, HTML — дольше).
X_SESSION_TIMEOUT_SEC = 45.0
# Паузы при отказах: 429 — cooldown с нарастанием; 401/403 — блокировка на сутки.
X_SESSION_429_COOLDOWN_SEC = 900
X_SESSION_429_COOLDOWN_HARD_SEC = 3600
X_SESSION_403_COOLDOWN_SEC = 86400
# Сколько постов брать из ленты по умолчанию и потолок одного вызова.
X_SESSION_TIMELINE_COUNT = 20
X_SESSION_TIMELINE_MAX = 40
# ТЗ-43B: сколько записей метрик сессии X писать за один прогон сбора.
# Защита metric_snapshot от роста на ровном месте: при упоре в потолок в
# run_log пишется понятная строка, дальше метрики прогона не пишутся.
X_SESSION_METRICS_MAX_PER_RUN = int(
    os.environ.get("TUBER_X_SESSION_METRICS_MAX_PER_RUN") or 3000)


def nitter_instances():
    """Порядок инстансов Nitter по режиму владения (ТЗ-4 5-БИС).

    dedicated — свой инстанс первым, чужой остаётся аварийным fallback-ом;
    strict    — только свой инстанс (чужой не трогаем вообще);
    pool      — весь пул ТЗ-1 равноправно.
    """
    own = INSTANCE_OWNERSHIP["tuber_x"].rstrip("/")
    mode = (INSTANCE_MODE or "dedicated").lower()
    if mode == "strict":
        return [own]
    if mode == "pool":
        return list(INSTANCES)
    rest = [h for h in INSTANCES if h.rstrip("/") != own]
    return [own] + rest
