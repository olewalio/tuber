"""Канонические имена сущностей для контура 5 (подтемы и новинки).

Одно место нормализации: подтемы (:mod:`tuber.analysis.subtopics`) и новинки
(:mod:`tuber.analysis.novelties`) обязаны сравнивать сущности ОДИНАКОВО.
Извлечение самих сущностей здесь не изобретается — используется существующее
(:func:`tuber.platforms.x.stories.extract_entities`); токены добавляются только
там, где существующее извлечение не видит новое ИМЯ (``ZCode``, ``Supacut`` —
их нет в словаре моделей/организаций).

Правила :func:`normalize_entity`:

* нижний регистр, обрезка пробелов и пунктуации по краям;
* ``@handle`` → ``handle`` (упоминание — тоже именованная сущность);
* домен (``sakana.ai``, ``z.ai``) → метка до первой точки (``sakana``, ``z``):
  так одна и та же новинка, упомянутая доменом на одной платформе и словом на
  другой, не теряется при подсчёте «≥ 2 платформ»;
* пустое/мусорное (``…``, ``www``, ``%d1%81``) → ``None``.

Границы честности: общий список служебных слов (:data:`STOPWORDS`) — это
HEURISTIC, а не словарь английского. Он отсекает обычные слова заголовков
(``before``, ``other``, ``models``), чтобы «новинкой» не стала английская
лексика. Ошибка в сторону пропуска допустима и видна в отчёте (не хватает
примеров) — заглушки не подставляются.
"""

from __future__ import annotations

import re

#: Токены заголовков/текстов.
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
#: Единица измерения (``8gb``, ``4k``) — не имя продукта.
_UNIT_RE = re.compile(r"^\d+[a-z]{1,3}$")

#: Названия платформ/ОС — не новинка (пусть и camelCase: ``macOS``, ``iOS``).
PLATFORM_WORDS: frozenset[str] = frozenset({
    "macos", "ios", "ipados", "android", "windows", "linux", "ubuntu",
    "chrome", "safari", "firefox", "vscode", "webgl", "http", "https",
})

#: Служебные слова и частая лексика заголовков: не могут быть именем новинки.
STOPWORDS: frozenset[str] = frozenset(["a", "an", "the", "and", "or", "but", "if", "then", "else", "for", "of", "to", "in", "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its", "this", "that", "these", "those", "i", "you", "he", "she", "we", "they", "them", "his", "her", "their", "our", "your", "me", "my", "us", "not", "no", "do", "does", "did", "done", "have", "has", "had", "will", "would", "can", "could", "should", "may", "might", "must", "about", "into", "over", "under", "out", "up", "down", "more", "most", "very", "just", "so", "than", "too", "also", "via", "per", "new", "now", "today", "latest", "full", "one", "two", "all", "any", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "zero", "first", "second", "third", "use", "used", "using", "show", "ask", "best", "top", "how", "why", "what", "when", "who", "whose", "without", "release", "released", "launched", "launch", "open", "source", "built", "build", "based", "tool", "tools", "app", "apps", "platform", "system", "systems", "framework", "library", "free", "local", "world", "next", "week", "year", "daily", "review", "vs", "github", "com", "www", "org", "net", "io", "ai", "hn", "ways", "way", "get", "gets", "got", "getting", "make", "makes", "made", "making", "take", "takes", "took", "taking", "give", "gives", "gave", "giving", "go", "goes", "went", "going", "come", "comes", "came", "coming", "see", "sees", "saw", "seen", "seeing", "know", "knows", "knew", "known", "knowing", "think", "thinks", "thought", "want", "wants", "wanted", "need", "needs", "needed", "like", "likes", "liked", "look", "looks", "looked", "first", "second", "third", "time", "times", "people", "person", "man", "woman", "men", "women", "day", "days", "night", "life", "world", "things", "thing", "part", "parts", "place", "places", "case", "cases", "point", "points", "number", "numbers", "group", "groups", "company", "companies", "state", "states", "fact", "facts", "back", "still", "even", "much", "many", "well", "only", "own", "same", "right", "left", "new", "old", "good", "great", "big", "small", "long", "short", "high", "low", "could", "would", "should", "must", "shall", "may", "might", "can", "will", "about", "above", "after", "again", "against", "because", "before", "below", "between", "both", "during", "each", "few", "further", "here", "once", "other", "some", "such", "there", "through", "until", "while", "with", "within", "without", "being", "having", "doing", "data", "code", "coding", "machine", "learning", "model", "models", "agent", "agents", "llm", "llms", "text", "image", "images", "video", "videos", "audio", "voice", "users", "user", "customer", "customers", "product", "products", "service", "services", "project", "projects", "team", "teams", "work", "works", "working", "build", "building", "created", "create", "creates", "created", "creating", "new", "open", "closed", "secure", "privacy", "security", "free", "paid", "price", "pricing", "cost", "costs", "value", "values", "best", "better", "worse", "fast", "slow", "simple", "complex", "powerful", "intelligent", "smart", "native", "light", "dark", "full", "empty", "easy", "hard", "quick", "real", "fake", "true", "false", "public", "private", "global", "local", "internal", "external", "cloud", "server", "client", "mobile", "desktop", "web", "website", "websites", "internet", "online", "offline", "email", "phone", "store", "market", "marketing", "start", "started", "starting", "run", "runs", "running", "test", "tests", "testing", "debug", "bugs", "issue", "issues", "feature", "features", "support", "update", "updates", "updated", "version", "versions", "v2", "beta", "alpha", "now", "today", "tomorrow", "yesterday", "week", "month", "year", "hours", "minutes", "seconds", "happened", "happens", "happen", "happening", "wrong", "correct", "error", "errors", "fix", "fixed", "camera", "room", "cars", "car", "house", "home", "school", "health", "money", "business", "science", "tech", "guide", "tutorial", "tips", "list", "lists", "news", "article", "blog", "post", "posts", "story", "introducing", "announcement", "announce", "announces"])

#: Доменные метки, сами по себе не имя продукта.
_GENERIC_LABELS = frozenset({"www", "com", "org", "net", "io", "ai", "co", "ru"})


def normalize_entity(raw) -> str | None:
    """Каноническая форма сущности или ``None``, если имени нет."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    text = text.removeprefix("@")
    if not text or text.startswith("%"):
        return None
    if "." in text and not any(ch.isspace() for ch in text):
        text = text.split(".", 1)[0]
    text = text.strip("._-+")
    if len(text) < 2 or text in _GENERIC_LABELS or text in STOPWORDS:
        return None
    if not any(ch.isalpha() for ch in text):
        return None
    return text


def title_tokens(text) -> set[str]:
    """Канонические токены текста (без служебных слов)."""
    out: set[str] = set()
    for token in _TOKEN_RE.findall(str(text or "")):
        name = normalize_entity(token)
        if name and len(name) >= 3:
            out.add(name)
    return out


def is_product_like(name: str) -> bool:
    """Похоже ли имя на инструмент/модель/стартап (а не на общее слово)."""
    if not name or name in STOPWORDS:
        return False
    if len(name) < 2 or len(name) > 40:
        return False
    return not name.isdigit()


def distinctive_names(title, *, capitalized: str = "none", extra=None) -> set[str]:
    """Имена-кандидаты из внешнего ЗАГОЛОВКА (название, а не лексика).

    Берутся группы:

    1. сущности существующего извлечения (:func:`extract_entities`) — словарь
       моделей/организаций, домены, ``@упоминания``;
    2. токены, которые ЯВНО похожи на имя: camelCase (``ZCode``, ``CometixCode``)
       или имя с цифрой (``gpt-4``, ``qwen3.5``);
    3. ``extra`` — явно переданные имена (например, имя репозитория).

    ``capitalized`` управляет заглавной буквой: ``"none"`` (по умолчанию) — не
    берём (иначе первое слово предложения становится новинкой), ``"first"`` —
    берём только ПЕРВЫЙ токен (продуктовые ленты: ``Supacut``, ``Show HN:
    Radius``), ``"all"`` — все заглавные.
    """
    from tuber.platforms.x.stories import extract_entities

    tokens = _TOKEN_RE.findall(str(title or ""))
    out: set[str] = set()
    for raw in extract_entities(str(title or "")):
        name = normalize_entity(raw)
        if name:
            out.add(name)
    for index, token in enumerate(tokens):
        name = normalize_entity(token)
        if not name or name in STOPWORDS or len(name) < 3:
            continue
        low = token.lower()
        has_lower = any(ch.islower() for ch in token)
        # ВНУТРЕННЯЯ заглавная (после первой буквы): ZCode, CometixCode, macOS.
        # Обычное слово с заглавной буквы (Dynamic, Three) camelCase НЕ является.
        inner_upper = any(ch.isupper() for ch in token[1:])
        has_digit = any(ch.isdigit() for ch in token)
        has_alpha = any(ch.isalpha() for ch in token)
        camel = has_lower and inner_upper
        number_name = has_digit and has_alpha and token[0].isalpha() and not _UNIT_RE.match(low)
        if camel or number_name or capitalized == "all" and token[0].isupper() or capitalized == "first" and index == 0 and token[0].isupper():
            out.add(name)
    for raw in extra or ():
        name = normalize_entity(raw)
        if name:
            out.add(name)
    return out
