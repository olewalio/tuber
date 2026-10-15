"""Детектор рекламы/промо и агрегаторов-пересказов (ТЗ-8).

Зачем: главное число витрин и отчёта («кратность к норме канала/автора»)
СИСТЕМАТИЧЕСКИ награждает рекламу. Рекламный/промо-пост для канала — выброс
относительно его же нормы: канал постит рекламу редко, но продвигает её на
деньги, поэтому просмотры (а иногда и лайки) кратно выше обычного. Замер
боевой борды 08.10.2026: промо-канал Альфа-Банка ``alfa_vygodno`` занял 1-е и
2-е места Telegram (×51,8 и ×27,9), в YouTube всплыли «Реклама World Camera…» и
«Реклама Telegram-бота Буквица…».

Модуль сознательно НЕ зависит от БД и от LLM на первом (основном) проходе:
правила чистые, детерминированные и дешёвые. Это же делает функцию удобной
для юнит-тестов — :func:`is_promotional` проверяется на строках, без базы.

Пороги эвристики КАПСА/эмодзи вынесены в константы ниже с обоснованием. На
замере боевой борды 08.10.2026 эвристика по КАПСУ/эмодзи НЕ срабатывает ни на
одной настоящей виральной карточке: всю работу делают явные маркеры, а
эвристика остаётся страховкой для промо-креативов без слов-маркеров.
"""

from __future__ import annotations

import os
import re

# --------------------------------------------------------------------------- #
# Явные маркеры рекламы/промо (ТЗ-8 §1.1)
# --------------------------------------------------------------------------- #
#: Регулярки маркеров. Русские корни взяты в виде префиксов, чтобы ловить
#: падежи/формы (``реклам`` → «реклама/рекламный/рекламирую»), но не резать
#: середину слова: ``\b`` (Unicode-граница слова) не даёт совпасть внутри
#: другого слова («нереклама» не сработает).
PROMO_MARKER_PATTERNS: tuple[str, ...] = (
    r"\bпромокод",
    r"\bпромо(?:\b|акци|м)",          # промо, промоакция, промо-код
    r"\bскидк",                       # скидка, скидки, скидочный
    r"\bк[еэ]шб[еэ]к",                # кэшбэк, кешбэк
    r"\bрозыгрыш",
    r"\bподпишись\b",
    r"\bподписывайт",                 # подписывайтесь
    r"\bпереходи\s+по\s+ссылке",
    r"\bрегистрируйс",                # регистрируйся
    r"\bпо\s+ссылке\s+в\s+описании",
    r"\bтариф",                       # тариф, тарифа, тарифы
    r"\bспонсор",                     # спонсор, спонсорский, Sponsr
    r"\bпартн[её]рск",                # партнёрский / партнерский
    r"\bжми(?:те)?\b",
    # Английские маркеры (в базе есть англоязычные X/YouTube-посты).
    # Сужаем до реально рекламных форм, чтобы не ловить новости про рекламу/
    # «referral of Iran», «employer-sponsored insurance» и т.п.
    r"\bpromo\s*code\b",
    r"\bgiveaway\b",
    r"\breferral\s+(?:code|link|program|bonus)\b",
    r"\baffiliate\s+(?:link|program|code)\b",
    r"\bsponsored\s+(?:by|post|content)\b",
)

_PROMO_MARKER_RE = re.compile("|".join(PROMO_MARKER_PATTERNS), re.IGNORECASE)

#: «Реклама» как ЯРЛЫК (в начале строки/текста либо «Реклама:»), а не как слово
#: внутри текста. Иначе пост-разбор «Воркфлоу рекламы Firefox» ложно попадёт в
#: рекламу (замер 08.10.2026: 164 из 1082 X-срабатываний — именно такие).
_AD_LABEL_RE = re.compile(
    r"(?:^|\n)\s*(?:#?реклама|рекламный\s+(?:пост|материал|ролик))\b",
    re.IGNORECASE | re.MULTILINE,
)

#: Второе правило: призыв к действию (CTA) + «промо-ссылка» (реферальная,
#: бот-стартовая, акционная). Ловит промо без слов-маркеров, но НЕ трогает
#: обычные посты со ссылкой на событие/источник (у них нет реф-токена в URL).
_CTA_RE = re.compile(
    r"(регистрируйс|подпиш|переход[ии]?\s+по|жми|оформля|оформи|закаж"
    r"|купи|покупай|успей|дедлайн|скачай|забирай)",
    re.IGNORECASE,
)
_PROMO_LINK_RE = re.compile(
    r"(https?://[^\s/]+/[^\s]*(?:\?|&)(?:start|ref|promo|bonus|gift|sale"
    r"|discount|action)=|t\.me/[^\s]*bot)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Эвристика КАПСА/эмодзи для промо-креативов (ТЗ-8 §1.1)
# --------------------------------------------------------------------------- #
#: Доля заглавных среди букв в «заголовке». Замер 08.10.2026: у настоящих
#: виральных заголовков — до 0,35 («МАГИЧЕСКАЯ БИТВА» ditarogram); у промо-креативов
#: выше. Но высокий КАПС сам по себе бывает у новостных раундапов/шуток, поэтому
#: правило требует ДОПОЛНИТЕЛЬНОГО сигнала (ссылка или ≥ 2 эмодзи) — только
#: связка «кричащий заголовок + промо-атрибут» считается промо.
CAPS_RATIO_THRESHOLD = 0.6
#: Минимальное число букв, чтобы доля КАПСА вообще считалась осмысленной.
CAPS_MIN_LETTERS = 15
#: Эмодзи на короткий фрагмент. Виральные новости/шутки несут 2–4 эмодзи
#: («Ахахахаха 🤣🤣🤣🤣»); порог 5 + дополнительный сигнал (ссылка или КАПС ≥ 0,35)
#: оставляет эвристику только для «нарядных» промо-креативов.
EMOJI_COUNT_THRESHOLD = 5
#: Окно «заголовка»: КАПС/эмодзи считаем по первым N знакам (первая строка/
#: лид поста), а не по всему тексту — иначе длинный пост размывает долю.
HEADLINE_CHARS = 120

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # эмодзи, пиктограммы, транспорт, лица и т.п.
    "\U00002600-\U000027BF"   # символы и дингбаты
    "\U00002B00-\U00002BFF"   # стрелки/звёзды
    "\U0001F1E6-\U0001F1FF"   # региональные индикаторы (флаги)
    "\u2764\u2665\u2b50\u2705\u2714\u274c\u2757\u2753"
    "\ufe0f"                   # variation selector
    "]"
)


def _caps_ratio(text: str) -> float:
    """Доля заглавных букв среди всех букв (0.0, если букв нет)."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    upper = sum(1 for ch in letters if ch.isupper())
    return upper / len(letters)


def _emoji_count(text: str) -> int:
    return len(_EMOJI_RE.findall(text))


def promo_signals(text: str) -> dict:
    """Диагностика: какие правила сработали (для отладки и тестов).

    Не влияет на решение :func:`is_promotional`, но объясняет его.
    """
    t = text or ""
    head = t[:HEADLINE_CHARS]
    caps_ratio = _caps_ratio(head)
    emoji_count = _emoji_count(head)
    letters = sum(1 for ch in head if ch.isalpha())
    #: У промо-креатива обычно есть и ссылка, и призыв к действию. Без этой пары
    #: кричащий заголовок/обилие эмодзи — ещё не реклама (новостной раундап,
    #: шутка); проверка замером 08.10.2026 убрала ложные X-срабатывания.
    has_url = bool(re.search(r"https?://|t\.me/", t, re.IGNORECASE))
    has_cta = bool(_CTA_RE.search(t))
    creative = has_url and has_cta
    caps_hit = (caps_ratio >= CAPS_RATIO_THRESHOLD
                and letters >= CAPS_MIN_LETTERS and creative)
    emoji_hit = (emoji_count >= EMOJI_COUNT_THRESHOLD and creative)
    return {
        "marker": bool(_PROMO_MARKER_RE.search(t) or _AD_LABEL_RE.search(t)),
        "cta_link": bool(_CTA_RE.search(t) and _PROMO_LINK_RE.search(t)),
        "caps_ratio": caps_ratio,
        "emoji_count": emoji_count,
        "has_url": has_url,
        "caps_hit": caps_hit,
        "emoji_hit": emoji_hit,
    }


def is_promotional(text: str) -> bool:
    """True, если текст похож на рекламу/промо (ТЗ-8 §1.1).

    Чистые правила, без БД и без LLM. Порядок:

    1. явный маркер из :data:`PROMO_MARKER_PATTERNS` (реклама, промокод,
       кэшбэк, тариф, спонсор…);
    2. призыв к действию + промо-ссылка (реф-токен, бот-стартовая ссылка);
    3. промо-креатив: высокая доля КАПСА ИЛИ много эмодзи в заголовке.

    Неуверенности здесь нет: функция возвращает ``bool``. Второй проход через
    ``deepseek-chat`` (см. :func:`refine_promotional`) — ОПЦИОНАЛЬНЫЙ и по
    умолчанию выключен: на замере 08.10.2026 правил достаточно.
    """
    t = (text or "").strip()
    if not t:
        return False
    # Единый расчёт сигналов: :func:`promo_signals` уже прогоняет те же
    # регулярки (маркеры, CTA+ссылка, КАПС/эмодзи-креатив), поэтому второй раз
    # их здесь не гоняем — решение берётся из его результатов (ТЗ-10 §3).
    sig = promo_signals(t)
    return bool(sig["marker"] or sig["cta_link"]
                or sig["caps_hit"] or sig["emoji_hit"])


# --------------------------------------------------------------------------- #
# Агрегаторы и пересказы (ТЗ-8 §1.3)
# --------------------------------------------------------------------------- #
#: Маркеры перепечатки/агрегации. Это ПОКАЗ, а не фильтр: карточка остаётся в
#: выдаче, но получает ``origin='retelling'`` (ТЗ-8 §1.3).
AGGREGATOR_MARKERS: tuple[str, ...] = (
    "по материалам",
    "по данным издания",
    "источник:",
    "источник —",
    "источник -",
    "via @",
    "переслано из",
    "forwarded from",
    "читайте также",
    "подробнее:",
    "оригинал:",
    "перевод:",
    "смотрите в источнике",
    "ссылка на первоисточник",
)
_AGGREGATOR_RE = re.compile(
    "|".join(re.escape(m) for m in AGGREGATOR_MARKERS), re.IGNORECASE
)
#: Ссылка в самом начале текста (первая строка — только цитата + ссылка).
_LEADING_URL_RE = re.compile(r"^\s*(?:https?://|t\.me/|@\w+\s+https?://)")


def is_aggregator(text: str, url: str | None = None) -> bool:
    """True, если текст похож на агрегат/пересказ чужого материала (ТЗ-8 §1.3).

    Правила (консервативные, без выдумывания первоисточника):

    * явные маркеры перепечатки (``по материалам``, ``источник:``, ``via @``,
      ``forwarded from``, ``читайте также``…);
    * текст начинается со ссылки/цитаты-ссылки, а своего текста почти нет
      (ссылка в первом абзаце без собственного комментария).

    ``url`` — необязательный адрес самого материала; сейчас он не меняет
    решение, но сигнатура расширяема (ТЗ-8 §1.3).
    """
    t = (text or "").strip()
    if not t:
        return False
    if _AGGREGATOR_RE.search(t):
        return True
    # Ссылка в первом абзаце при почти полном отсутствии своего текста:
    # первый абзац состоит из ссылки и короткого служебного обрамления.
    first_para = re.split(r"\n\s*\n", t, maxsplit=1)[0].strip()
    if _LEADING_URL_RE.match(first_para):
        stripped = re.sub(r"https?://\S+|t\.me/\S+", "", first_para)
        stripped = re.sub(r"[\s\-—:|()\[\]«»\"']+", "", stripped)
        if len(stripped) <= 40:
            return True
    return False


def classify_origin(text: str, url: str | None = None) -> str:
    """``"retelling"`` для агрегатов/пересказов, иначе ``"original"`` (ТЗ-8 §1.3)."""
    return "retelling" if is_aggregator(text, url) else "original"


# --------------------------------------------------------------------------- #
# ОПЦИОНАЛЬНЫЙ второй проход через LLM (ТЗ-8 §1.1)
# --------------------------------------------------------------------------- #
#: Модель второго прохода — дешёвая, БЕЗ reasoning (ТЗ-8 §1.1). Запускается
#: ТОЛЬКО при неуверенности правил и только при включённом флаге окружения.
ADS_LLM_MODEL = "deepseek-chat"
#: Флаг окружения. Пусто/0 — LLM не вызывается вовсе (замеры и тесты дешёвы).
ADS_LLM_ENV_FLAG = "TUBER_ADS_LLM"
#: Адрес API — тот же, что у остальных стадий DeepSeek проекта.
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

_LLM_PROMPT = (
    "Ты — детектор рекламы. Ответь РОВНО одним словом: YES, если пост — реклама, "
    "промо, спонсорский или партнёрский материал; NO, если это обычная новость, "
    "авторское мнение или анонс без спонсорской метки.\n\nПост:\n"
)


def ads_llm_enabled() -> bool:
    """Включён ли второй проход через LLM (по умолчанию ВЫКЛЮЧЕН)."""
    return (os.environ.get(ADS_LLM_ENV_FLAG) or "").strip().lower() in (
        "1", "on", "true")


def _cache_get(conn, text_hash: str) -> bool | None:
    """Вердикт из ``ad_cache`` (если уже считали) — иначе ``None``.

    Кэш — существующая таблица семантического детектора рекламы Telegram
    (``tuber/platforms/telegram/ads.py``): она уже хранит ``is_ad`` по
    ``text_hash`` и наполняется 750+ строк. Для рекламы это семантически
    точнее, чем общий ``classify_cache`` (темы/ИИ), поэтому второй проход
    пишет и читает именно её. ``classify_cache`` остаётся кэшем классификации
    тем и не смешивается с рекламным решением.
    """
    if not text_hash or conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT is_ad, status FROM ad_cache WHERE text_hash = ?",
            (text_hash,)).fetchone()
    except Exception:  # noqa: BLE001 — нет таблицы в тестовой БД → без кэша
        return None
    if row is None:
        return None
    try:  # sqlite3.Row
        is_ad, status = row["is_ad"], row["status"]
    except (TypeError, KeyError, IndexError):
        is_ad, status = row[0], row[1]  # обычный кортеж
    if status not in (None, "ok"):
        return None
    if is_ad is None:
        return None
    return bool(is_ad)


def _cache_put(conn, text_hash: str, verdict: bool) -> None:  # pragma: no cover
    """Сохранить вердикт в ``ad_cache`` (только при включённом LLM-проходе)."""
    if not text_hash or conn is None:
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ad_cache(text_hash, is_ad, confidence,"
            " reason, method, model, prompt_ver, status, classified_at,"
            " first_seen_at) VALUES (?,?,?,?,?,?,?, 'ok', ?, ?)",
            (text_hash, 1 if verdict else 0, 1.0 if verdict else 0.0,
             "analysis.ads rule+llm", "llm", ADS_LLM_MODEL, "ads-v1", now, now))
    except Exception:  # noqa: BLE001, S110 — тестовая БД без таблицы → молча без кэша
        pass


def refine_promotional(conn, text: str, *, text_hash: str | None = None,
                       first_pass: bool | None = None) -> bool:
    """Второй проход (опционально): спорный случай через ``deepseek-chat``.

    Вызывается ТОЛЬКО если первый проход неуверен. «Неуверенность» — нет
    явного маркера, но есть слабый сигнал (призыв к действию в тексте).
    При выключенном флаге возвращает результат первого прохода — никаких
    сетевых вызовов и затрат.

    Затраты (обоснование): один короткий запрос ``deepseek-chat`` без reasoning
    на спорный пост; кэш — по ``text_hash``. На замере 08.10.2026 флаг
    ВЫКЛЮЧЕН: явные маркеры покрывают все найденные рекламные карточки,
    поэтому LLM-проход не оплачивается (0 вызовов, 0 $).
    """
    result = is_promotional(text) if first_pass is None else first_pass
    if not ads_llm_enabled():
        return result
    if result or not text:
        return result  # явный маркер или пусто — первый проход уверен
    if not _CTA_RE.search(text or ""):
        return result  # нет даже слабого сигнала — не платим
    cached = _cache_get(conn, text_hash)
    if cached is not None:
        return cached
    verdict = _llm_classify_promo(text)
    if verdict is None:
        return result
    _cache_put(conn, text_hash, verdict)  # pragma: no cover
    return verdict


def _llm_classify_promo(text: str) -> bool | None:  # pragma: no cover
    """Реальный сетевой вызов — вынесен, чтобы тесты его не трогали."""
    import json
    import urllib.request
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        return None
    payload = json.dumps({
        "model": ADS_LLM_MODEL, "temperature": 0, "stream": False,
        "messages": [{"role": "user", "content": _LLM_PROMPT
                      + (text or "")[:800]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL, data=payload,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    choices = data.get("choices") or []
    answer = ((choices[0].get("message") or {}).get("content")
              if choices else "") or ""
    if not answer.strip():
        return None
    return "yes" in answer.strip().lower()
