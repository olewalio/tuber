"""SEO-разбор YouTube-упаковки (этап 1).

Модуль читает ТОЛЬКО базу данных: сеть не используется ни разу.

Три входа:
- ``analyze``  — заполняет таблицу ``seo_fields`` разбором текста;
- ``score_video`` — оценка упаковки 0-100 с разбивкой и списком проблем;
- ``patterns`` — сравнение «выбросы против фона» по оформлению.

Жёсткое правило методики (docs/METHODOLOGY-SEO.md): по чужому видео CTR,
удержание, среднее время просмотра, источники трафика и конечные экраны
недоступны. Их не выдумываем, а честно перечисляем в ``not_available``.

Обложки (поля ``thumb_*``) здесь НЕ заполняются: их разбирает отдельный
визуальный проход по картинкам. Поэтому в разборе эти колонки отсутствуют
и остаются NULL (см. комментарий в ``_FIELDS``).
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Sequence

from . import config, store as db, report

# --- Константы правил (docs/METHODOLOGY-SEO.md, раздел 3) ------------------

TITLE_MIN_LEN = 30
TITLE_MAX_LEN = 60
TITLE_HARD_MAX_LEN = 70          # выше — грубое нарушение
TITLE_CAPS_MAX_RATIO = 0.5       # доля заглавных букв выше — «крик»
TITLE_EMOJI_MAX = 3
DESC_SNIPPET_LEN = 150           # первые знаки описания, которые индексируются
DESC_HASHTAG_MIN = 3
DESC_HASHTAG_MAX = 5
DESC_HASHTAG_IGNORED = 15        # свыше — YouTube игнорирует все хэштеги
TAGS_GOOD_MIN = 8
TAGS_GOOD_MAX = 12
TAGS_SPAM_RISK = 50
CHAPTERS_MIN_DURATION = 480      # 8 минут: главы обязательны

# Веса компонентов оценки упаковки (в сумме 100).
SCORE_WEIGHTS: dict[str, int] = {
    "title": 30,
    "description": 30,
    "hashtags": 10,
    "tags": 10,
    "chapters": 10,
    "topic": 10,
}

# Штраф к весу компонента за одно нарушение соответствующего уровня.
_PENALTY = {"high": 0.5, "mid": 0.25, "low": 0.1}

# Строка про недоступное обязательна в выдаче patterns.
NOT_AVAILABLE: tuple[str, ...] = (
    "CTR — недоступен: это метрика кабинета автора, по чужому видео не выводится",
    "удержание и средний процент просмотра — недоступны по чужому видео",
    "среднее время просмотра — недоступно по чужому видео",
    "источники трафика — недоступны по чужому видео",
    "возврат зрителей за 7 дней — недоступен напрямую по чужому видео",
    "конечные экраны и карточки — API не отдаёт",
)

MSK = report.MSK


# --- Стоп-слова и токенизация ----------------------------------------------

_RU_STOP = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех",
    "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть",
    "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего",
    "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем",
    "хорошо", "свою", "этой", "перед", "иногда", "лучше", "чуть", "том",
    "нельзя", "такой", "им", "более", "всегда", "конечно", "всю", "между",
    "это", "что", "какие", "как", "таки", "просто", "лишь", "ведь", "вон",
}

_EN_STOP = {
    "the", "and", "for", "you", "your", "with", "that", "this", "are", "was",
    "from", "have", "has", "will", "but", "not", "all", "can", "how", "why",
    "what", "when", "who", "its", "into", "out", "new", "his", "her", "she",
    "him", "they", "their", "our", "get", "got", "vs", "top", "about", "over",
    "more", "most", "than", "then", "them", "these", "those", "were", "had",
    "been", "being", "does", "did", "doing", "done", "just", "only", "also",
    "here", "there", "where", "which", "while", "would", "could", "should",
    "may", "might", "must", "one", "two", "now", "you're", "it's", "we're",
}

_STOPWORDS = _RU_STOP | _EN_STOP

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")

# Эмодзи: диапазоны Unicode, без внешних библиотек. Модификаторы тона кожи,
# ZWJ и селекторы начертания исключаются, чтобы не считать один эмодзи дважды.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # основные эмодзи-блоки (включая флаги 1F1E6-1F1FF)
    (0x2600, 0x27BF),    # символы и дингбаты
    (0x2300, 0x23FF),    # часы, песочные часы
    (0x25A0, 0x25FF),    # геометрические фигуры
    (0x2B00, 0x2BFF),    # звёзды, дополнительные символы
)
_EMOJI_EXCLUDE = {0x200D, 0xFE0E, 0xFE0F, 0x1F3FB, 0x1F3FC, 0x1F3FD,
                  0x1F3FE, 0x1F3FF}


def _is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    if cp in _EMOJI_EXCLUDE:
        return False
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def count_emoji(text: str) -> int:
    """Число эмодзи в строке (по диапазонам Unicode)."""
    return sum(1 for ch in (text or "") if _is_emoji_char(ch))


def caps_ratio(text: str) -> float:
    """Доля заглавных среди буквенных символов (0.0-1.0)."""
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return 0.0
    upper = sum(1 for ch in letters if ch.isupper())
    return upper / len(letters)


def _words(text: str) -> list[str]:
    """Слова строки в нижнем регистре (буквы/цифры)."""
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def significant_words(text: str) -> list[str]:
    """Значимые слова: не короче 3 знаков и не стоп-слова."""
    return [w for w in _words(text) if len(w) >= 3 and w not in _STOPWORDS]


def title_top_words(title: str, limit: int = 5) -> list[str]:
    """До limit значимых слов заголовка по убыванию частоты."""
    words = significant_words(title)
    if not words:
        return []
    counter: dict[str, int] = {}
    for w in words:
        counter[w] = counter.get(w, 0) + 1
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ordered[:limit]]


# --- Регексы разбора описания ----------------------------------------------

_LINK_RE = re.compile(r"https?://", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)
_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_CTA_RES = (
    re.compile(r"подпис|подпиш", re.IGNORECASE),
    re.compile(r"subscribe", re.IGNORECASE),
    re.compile(r"ставь(те)?\s+лайк", re.IGNORECASE),
    re.compile(r"ссылк\w*\s+в\s+описании", re.IGNORECASE),
    re.compile(r"link\s+below", re.IGNORECASE),
    re.compile(r"\bжми\b|\bнажми\b", re.IGNORECASE),
)


def _has_cta(description: str) -> int:
    text = description or ""
    return 1 if any(rx.search(text) for rx in _CTA_RES) else 0


# --- Соответствие заголовка теме -------------------------------------------


def _has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04FF" for ch in (text or ""))


def _match_single(topic_words: list[str], text_words: list[str],
                  topic: str) -> int:
    """Совпадение уже разобранной темы с разобранным текстом (0/1)."""
    if not topic_words or not text_words:
        return 0
    if _has_cyrillic(topic):
        text_roots = {w[:5] for w in text_words if len(w) >= 5}
        for w in topic_words:
            if len(w) >= 5:
                if w[:5] in text_roots:
                    return 1
            elif w in text_words:
                return 1
        return 0
    text_set = set(text_words)
    return 1 if any(w in text_set for w in topic_words) else 0


def matches_topic(topic: str | None, title: str | None,
                  title_ru: str | None = None) -> int | None:
    """Пересечение темы и заголовка значимыми словами.

    1 — есть совпадение, 0 — тема известна, но совпадения нет, None — темы нет.
    Русские темы сравниваются по корням (первые 5 знаков слова длиной ≥5),
    английские/смешанные — по словам целиком.

    У англоязычных видео оригинальный заголовок часто не пересекается с русской
    темой классификации — это ложный ноль. Поэтому, если есть русский перевод
    ``title_ru``, совпадение считается по заголовку ИЛИ по переводу.
    """
    if not topic or not str(topic).strip():
        return None
    topic_str = str(topic)
    topic_words = significant_words(topic_str)
    if _match_single(topic_words, significant_words(title or ""), topic_str):
        return 1
    if title_ru and str(title_ru).strip():
        if _match_single(topic_words, significant_words(title_ru),
                         topic_str):
            return 1
    return 0


# --- Метка паттерна заголовка ----------------------------------------------


def seo_pattern(title: str, caps: float, emoji: int, length: int) -> str:
    """Метка через '+' из набора признаков заголовка."""
    labels: list[str] = []
    if re.search(r"\d", title or ""):
        labels.append("цифра")
    if "?" in (title or ""):
        labels.append("вопрос")
    if ":" in (title or "") or "—" in (title or "") or "-" in (title or ""):
        labels.append("двоеточие")
    if emoji > 0:
        labels.append("эмодзи")
    if caps > TITLE_CAPS_MAX_RATIO:
        labels.append("капс")
    if length > TITLE_MAX_LEN:
        labels.append("длинный")
    elif length < TITLE_MIN_LEN:
        labels.append("короткий")
    if not labels:
        labels.append("базовый")
    return "+".join(labels)


# --- Сборка полей одной строки ---------------------------------------------


# Колонки, которые заполняет этот разбор. thumb_* сознательно отсутствуют:
# обложки разбирает отдельный визуальный проход, текстовый разбор их не
# трогает и оставляет NULL.
_FIELDS: tuple[str, ...] = (
    "video_id", "title_length", "title_words", "title_has_number",
    "title_has_question", "title_has_colon", "title_caps_ratio",
    "title_emoji_count", "title_top_words", "desc_length", "desc_links",
    "desc_hashtags", "desc_timestamps", "desc_cta", "tags_count",
    "tags_common", "published_hour_msk", "published_weekday",
    "published_hour_local", "title_matches_topic", "seo_pattern",
)


def build_fields(row: Any) -> dict[str, Any]:
    """Разобрать строку видео (sqlite3.Row) в значения колонок seo_fields."""
    title = row["title"] or ""
    description = row["description"] or ""
    tags = report._parse_tags(row["tags"])
    feats = report._title_features(title)
    caps = caps_ratio(title)
    emoji = count_emoji(title)
    length = feats["title_length"]
    hour, weekday, _ = report._msk_parts(row["published_at"])
    topic = row["topic"] if "topic" in row.keys() else None
    title_ru = row["title_ru"] if "title_ru" in row.keys() else None
    return {
        "video_id": row["video_id"],
        "title_length": length,
        "title_words": feats["title_words"],
        "title_has_number": 1 if feats["title_has_number"] else 0,
        "title_has_question": 1 if "?" in title else 0,
        "title_has_colon": 1 if (":" in title or "—" in title or "-" in title) else 0,
        "title_caps_ratio": round(caps, 4),
        "title_emoji_count": emoji,
        "title_top_words": json.dumps(title_top_words(title), ensure_ascii=False),
        "desc_length": len(description),
        "desc_links": len(_LINK_RE.findall(description)),
        "desc_hashtags": len(_HASHTAG_RE.findall(description)),
        "desc_timestamps": 1 if _TIMESTAMP_RE.search(description) else 0,
        "desc_cta": _has_cta(description),
        "tags_count": len(tags),
        "tags_common": json.dumps([t.strip().lower() for t in tags][:15],
                                  ensure_ascii=False),
        "published_hour_msk": hour,
        "published_weekday": weekday,
        # Таймзона канала через API неизвестна — выдумывать нельзя.
        "published_hour_local": None,
        "title_matches_topic": matches_topic(topic, title, title_ru),
        "seo_pattern": seo_pattern(title, caps, emoji, length),
    }


def _upsert_fields(conn, values: dict[str, Any]) -> None:
    """Идемпотентная запись строки, не трогающая чужие колонки (thumb_*).

    Запись идёт через адаптер (store): в единой базе таблицы ``seo_fields`` нет,
    есть ядро ``seo_field`` с ключом ``content_id``.
    """
    payload = {c: values.get(c) for c in _FIELDS if c != "video_id"}
    db.upsert_seo_field(conn, values["video_id"], **payload)


# --- 1. analyze ------------------------------------------------------------


def analyze(conn, force: bool = False, limit: int | None = None) -> dict[str, int]:
    """Заполнить seo_fields по видео из videos.

    По умолчанию берутся только видео, для которых строки ещё нет;
    ``force=True`` пересчитывает и перезаписывает все. Повторный прогон
    идемпотентен: дублей не появляется, значения не меняются.
    """
    existing: set[str] = set()
    if not force:
        existing = {
            r[0] for r in conn.execute("SELECT video_id FROM seo_fields").fetchall()
        }
    sql = """
        SELECT v.video_id, v.title, v.description, v.tags, v.published_at,
               vc.topic AS topic, vc.title_ru AS title_ru
        FROM videos v
        LEFT JOIN video_classification vc ON vc.video_id = v.video_id
        ORDER BY v.video_id
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(limit),)
    rows = conn.execute(sql, params).fetchall()

    videos = written = skipped = no_title = 0
    for row in rows:
        videos += 1
        title = (row["title"] or "").strip()
        if not title:
            no_title += 1
            continue
        if not force and row["video_id"] in existing:
            skipped += 1
            continue
        _upsert_fields(conn, build_fields(row))
        written += 1
    conn.commit()
    return {
        "videos": videos,
        "written": written,
        "skipped": skipped,
        "no_title": no_title,
    }


# --- 2. score_video --------------------------------------------------------


def _load_fields(conn, video_id: str) -> dict[str, Any] | None:
    """Поля seo_fields; если строки нет — считаем разбор на лету (без записи)."""
    row = conn.execute(
        "SELECT * FROM seo_fields WHERE video_id = ?", (video_id,)
    ).fetchone()
    if row is not None:
        return dict(row)
    vrow = conn.execute(
        """
        SELECT v.video_id, v.title, v.description, v.tags, v.published_at,
               vc.topic AS topic, vc.title_ru AS title_ru
        FROM videos v
        LEFT JOIN video_classification vc ON vc.video_id = v.video_id
        WHERE v.video_id = ?
        """,
        (video_id,),
    ).fetchone()
    if vrow is None:
        return None
    return build_fields(vrow)


def _score_from(fields: dict[str, Any], duration: int | None) -> dict[str, Any]:
    """Собрать оценку из готовых полей (без обращения к БД)."""
    issues: list[dict[str, str]] = []
    not_applicable: list[str] = []
    na_parts: set[str] = set()
    parts: dict[str, float] = {}
    checks = 0

    def add(code: str, level: str, text: str) -> None:
        issues.append({"code": code, "level": level, "text": text})

    # --- заголовок (30) ---
    title_pen = 0.0
    length = fields.get("title_length")
    if length is None:
        length = 0
    checks += 1
    if length > TITLE_HARD_MAX_LEN:
        add("title_too_long", "high",
            f"Заголовок {length} знаков — грубо длиннее {TITLE_HARD_MAX_LEN}; "
            f"YouTube обрежет его в выдаче.")
        title_pen += _PENALTY["high"]
    elif length > TITLE_MAX_LEN:
        add("title_too_long", "mid",
            f"Заголовок {length} знаков — длиннее цели {TITLE_MAX_LEN}; "
            f"основной ключ может не попасть в видимую часть.")
        title_pen += _PENALTY["mid"]
    elif length < TITLE_MIN_LEN:
        add("title_too_short", "low",
            f"Заголовок {length} знаков — короче {TITLE_MIN_LEN}; "
            f"мало места под ключи и смысл.")
        title_pen += _PENALTY["low"]
    checks += 1
    caps = fields.get("title_caps_ratio") or 0.0
    if caps > TITLE_CAPS_MAX_RATIO:
        add("title_caps", "mid",
            f"Заглавных {round(caps * 100)}% — выглядит как крик; "
            f"порог {round(TITLE_CAPS_MAX_RATIO * 100)}%.")
        title_pen += _PENALTY["mid"]
    checks += 1
    emoji = fields.get("title_emoji_count") or 0
    if emoji > TITLE_EMOJI_MAX:
        add("title_emoji", "low",
            f"Эмодзи {emoji} — больше {TITLE_EMOJI_MAX}; лишний шум в заголовке.")
        title_pen += _PENALTY["low"]
    parts["title"] = SCORE_WEIGHTS["title"] * max(0.0, 1.0 - title_pen)

    # --- описание (30) ---
    desc_len = fields.get("desc_length") or 0
    checks += 1
    desc_pen = 0.0
    if desc_len < DESC_SNIPPET_LEN:
        add("description_no_snippet", "high",
            f"Описание {desc_len} знаков — короче сниппета "
            f"({DESC_SNIPPET_LEN}); первые знаки не индексируются.")
        desc_pen += _PENALTY["high"]
    parts["description"] = SCORE_WEIGHTS["description"] * max(0.0, 1.0 - desc_pen)

    # --- хэштеги (10) ---
    hashtags = fields.get("desc_hashtags") or 0
    if desc_len == 0:
        not_applicable.append(
            "хэштеги: описания нет, ставить их негде (проверка пропущена)")
        na_parts.add("hashtags")
        parts["hashtags"] = 0.0
    else:
        hpen = 0.0
        checks += 1
        if hashtags == 0:
            add("hashtags_missing", "low",
                "Хэштегов нет; 3-5 штук помогают навигации по теме.")
            hpen += _PENALTY["low"]
        elif hashtags < DESC_HASHTAG_MIN:
            add("hashtags_few", "low",
                f"Хэштегов {hashtags} — меньше {DESC_HASHTAG_MIN}.")
            hpen += _PENALTY["low"]
        elif hashtags > DESC_HASHTAG_IGNORED:
            add("hashtags_over_limit", "high",
                f"Хэштегов {hashtags} — больше {DESC_HASHTAG_IGNORED}; "
                f"YouTube игнорирует их все.")
            hpen += _PENALTY["high"]
        elif hashtags > DESC_HASHTAG_MAX:
            add("hashtags_many", "low",
                f"Хэштегов {hashtags} — больше рекомендуемых "
                f"{DESC_HASHTAG_MIN}-{DESC_HASHTAG_MAX}.")
            hpen += _PENALTY["low"]
        parts["hashtags"] = SCORE_WEIGHTS["hashtags"] * max(0.0, 1.0 - hpen)

    # --- теги (10) ---
    tags_count = fields.get("tags_count") or 0
    tpen = 0.0
    checks += 1
    if tags_count == 0:
        add("tags_missing", "low",
            "Тегов нет. По официальной справке YouTube влияние тегов "
            "минимально, но опечатки и синонимы ключа закрыть полезно.")
        tpen += _PENALTY["low"]
    elif tags_count < TAGS_GOOD_MIN:
        add("tags_few", "low",
            f"Тегов {tags_count} — меньше {TAGS_GOOD_MIN}; "
            f"влияние тегов минимально, но запас вариантов не помешает.")
        tpen += _PENALTY["low"]
    elif tags_count > TAGS_SPAM_RISK:
        checks += 1
        add("tags_spam_risk", "mid",
            f"Тегов {tags_count} — больше {TAGS_SPAM_RISK}; риск спам-фильтра.")
        tpen += _PENALTY["mid"]
    elif tags_count > TAGS_GOOD_MAX:
        add("tags_many", "low",
            f"Тегов {tags_count} — больше рекомендуемых "
            f"{TAGS_GOOD_MIN}-{TAGS_GOOD_MAX}.")
        tpen += _PENALTY["low"]
    parts["tags"] = SCORE_WEIGHTS["tags"] * max(0.0, 1.0 - tpen)

    # --- главы/структура (10) ---
    if duration is None:
        not_applicable.append(
            "главы: длительность видео неизвестна, проверить нельзя")
        na_parts.add("chapters")
        parts["chapters"] = 0.0
    else:
        parts["chapters"] = float(SCORE_WEIGHTS["chapters"])
        if duration >= CHAPTERS_MIN_DURATION:
            checks += 1
            if not (fields.get("desc_timestamps") or 0):
                add("description_no_chapters", "mid",
                    f"Видео длится {duration} с (≥ {CHAPTERS_MIN_DURATION}) "
                    f"без таймкодов в описании; главы обязательны.")
                parts["chapters"] = SCORE_WEIGHTS["chapters"] * (
                    1.0 - _PENALTY["mid"])
        else:
            checks += 1  # короткое видео: главы не требуются

    # --- соответствие теме (10) ---
    topic_match = fields.get("title_matches_topic")
    if topic_match is None:
        not_applicable.append(
            "соответствие теме: смысловой классификации нет, поле честно NULL")
        na_parts.add("topic")
        parts["topic"] = 0.0
    else:
        checks += 1
        if topic_match == 0:
            add("topic_mismatch", "mid",
                "Заголовок не пересекается значимыми словами с темой канала "
                "по классификации.")
            parts["topic"] = SCORE_WEIGHTS["topic"] * (1.0 - _PENALTY["mid"])
        else:
            parts["topic"] = float(SCORE_WEIGHTS["topic"])

    # Вес неприменимых компонентов в знаменатель не входит.
    na_weights = sum(SCORE_WEIGHTS[name] for name in na_parts)
    total_weight = sum(SCORE_WEIGHTS.values()) - na_weights
    raw = sum(parts.values())
    score = round(100.0 * raw / total_weight, 1) if total_weight else 0.0
    return {
        "video_id": fields.get("video_id"),
        "score": score,
        "parts": {k: round(v, 1) for k, v in parts.items()},
        "issues": issues,
        "checks": checks,
        "not_applicable": not_applicable,
    }
def score_video(conn, video_id: str, fmt: str | None = None) -> dict[str, Any]:
    """Оценка упаковки видео 0-100 с разбивкой и списком проблем.

    ``fmt`` принимается для совместимости с отчётными вызовами; оценка
    текстовой упаковки от формата не зависит.
    """
    fields = _load_fields(conn, video_id)
    if fields is None:
        raise ValueError(f"video_id не найден: {video_id}")
    row = conn.execute(
        "SELECT duration_seconds, is_shorts FROM videos WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    duration = row["duration_seconds"] if row is not None else None
    result = _score_from(fields, duration)
    result["video_id"] = video_id
    return result


# --- 3. patterns -----------------------------------------------------------


# D-05: медиана vpd канала считается не меньше чем по этому числу видео с
# валидной скоростью просмотров. Меньше — медиана недостоверна, vpd_ratio NULL.
MIN_VPD_RATIO_VIDEOS = 3


def _snapshot_vpd(snap: Any) -> float | None:
    """Скорость просмотров из последнего замера.

    Берётся готовое ``views_per_day`` (формула замера: delta_views за сутки) и
    только при ``interval_quality='ok'`` и ``is_anomaly=0``: на коротком
    интервале скорость не считается, а аномальный замер (просмотры уменьшились)
    не даёт известной скорости. Иначе NULL — неизвестное остаётся неизвестным.
    """
    if snap is None or snap["interval_quality"] != "ok" or snap["is_anomaly"] != 0:
        return None
    value = snap["views_per_day"]
    return float(value) if value is not None else None


def _snapshot_comment_velocity(snap: Any) -> float | None:
    """Скорость комментариев из последнего замера, комментариев в сутки.

    Формула: ``delta_comments / (interval_seconds / 86400)``. Только при
    ``interval_quality='ok'`` и ``is_anomaly=0`` (аномальный замер не даёт ни
    скорости просмотров, ни скорости комментариев) и положительном интервале.
    Отрицательная дельта (чистка или скрытие комментариев) сводится к 0:
    скорость роста не бывает отрицательной. Пустая дельта — NULL.
    """
    if snap is None or snap["interval_quality"] != "ok" or snap["is_anomaly"] != 0:
        return None
    delta = snap["delta_comments"]
    interval = snap["interval_seconds"]
    if delta is None or interval is None or int(interval) <= 0:
        return None
    delta_value = float(delta)
    if delta_value < 0:
        delta_value = 0.0
    return delta_value / (float(interval) / 86400)


def _channel_vpd_medians(
    latest_snap: dict[str, Any],
    min_videos: int = MIN_VPD_RATIO_VIDEOS,
) -> dict[str, dict[str, float]]:
    """Медианы ``vpd`` по каналам и форматам видео (ТЗ-33).

    Медиана канала из одних видео смешивает шортсы и полные ролики: у канала с
    шортсами по 50 тыс. просмотров медиана завышена для полного видео и занижена
    для шортса. Поэтому сначала считается медиана по видео ТОГО ЖЕ формата
    (``videos.is_shorts``), и лишь если видео этого формата меньше ``min_videos``
    — берётся медиана по каналу целиком, как раньше.

    Учитываются только нормальные замеры (``interval_quality='ok'`` и
    ``is_anomaly=0``): аномальные, у которых просмотры уменьшились, скорости не
    дают и в медиану не попадают. Возвращает ``channel_id -> {формат|'all':
    медиана}``; в словаре форматов остаются только надёжные медианы (>0 и не
    меньше ``min_videos`` видео).
    """
    per_channel: dict[str, list[float]] = {}
    per_channel_fmt: dict[tuple[str, str], list[float]] = {}
    for snap in latest_snap.values():
        vpd = _snapshot_vpd(snap)
        channel_id = snap["channel_id"]
        if vpd is None or not channel_id:
            continue
        fmt = report._video_format(snap["is_shorts"])
        per_channel.setdefault(channel_id, []).append(vpd)
        per_channel_fmt.setdefault((channel_id, fmt), []).append(vpd)
    medians: dict[str, dict[str, float]] = {}
    for channel_id, values in per_channel.items():
        entry: dict[str, float] = {}
        for fmt in ("short", "long"):
            fmt_values = per_channel_fmt.get((channel_id, fmt), [])
            if len(fmt_values) < int(min_videos):
                continue
            med = median(fmt_values)
            if med > 0:
                entry[fmt] = float(med)
        if len(values) >= int(min_videos):
            med = median(values)
            if med > 0:
                entry["all"] = float(med)
        if entry:
            medians[channel_id] = entry
    return medians


def _vpd_ratio(vpd: float | None, channel_id: Any, is_shorts: Any,
               medians: dict[str, dict[str, float]]) -> float | None:
    """``vpd`` видео к медиане его формата; без медианы формата — к каналу.

    Если у канала набралось меньше ``MIN_VPD_RATIO_VIDEOS`` видео того же
    формата, база сравнения — медиана канала целиком; если нет и её — NULL.
    """
    if vpd is None or not channel_id:
        return None
    entry = medians.get(channel_id)
    if not entry:
        return None
    fmt = report._video_format(is_shorts)
    med = entry.get(fmt) or entry.get("all")
    if not med:
        return None
    return vpd / med


def _latest_snapshot_map(conn) -> dict[str, Any]:
    """Последний замер каждого видео: просмотры/лайки/комментарии и скорость.

    Помимо самих счётчиков берутся поля, из которых считаются скоры
    ``video_scores`` (D-05): ``views_per_day`` (скорость просмотров, только при
    ``interval_quality='ok'`` и ``is_anomaly=0``), ``delta_comments`` и
    ``interval_seconds`` для скорости комментариев, а также ``channel_id`` — для
    медианы канала.
    """
    rows = conn.execute(
        """
        SELECT v.video_id AS video_id, v.channel_id AS channel_id,
               v.is_shorts AS is_shorts,
               s.views AS views, s.likes AS likes,
               s.comments AS comments, s.views_per_day AS views_per_day,
               s.interval_quality AS interval_quality,
               s.is_anomaly AS is_anomaly,
               s.delta_comments AS delta_comments,
               s.interval_seconds AS interval_seconds
        FROM videos v
        JOIN snapshots s ON s.id = (
            SELECT id FROM snapshots
            WHERE video_id = v.video_id
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
        )
        """
    ).fetchall()
    return {r["video_id"]: r for r in rows}


def refresh_scores(conn, limit: int | None = None, force: bool = False,
                   fmt: str | None = None, now: int | None = None) -> dict[str, int]:
    """Сохранить скор упаковки видео в video_scores. Бесплатно, без сети.

    Берутся видео, у которых уже есть строка в ``seo_fields`` (иначе
    ``score_video`` нечего оценивать), по убыванию свежести.

    - ``packaging_score`` — из ``score_video(...)["score"]``, разбивка — в
      ``score_parts`` (JSON);
    - ``outlier_score`` — переиспользован ``report._outlier_map``;
    - ``likes_per_1000``/``comments_per_1000`` — формула ``report._per_1000``;
    - ``vpd`` — ``views_per_day`` последнего замера только при
      ``interval_quality='ok'`` и ``is_anomaly=0`` (аномальный замер скорости не
      даёт);
    - ``comment_velocity`` — ``delta_comments`` за сутки того же нормального
      замера (``interval_quality='ok'``, ``is_anomaly=0``; рост комментариев,
      отрицательная дельта сводится к 0);
    - ``vpd_ratio`` — ``vpd`` видео к медиане ``vpd`` видео его канала ТОГО ЖЕ
      формата (``is_shorts``); если видео этого формата меньше
      ``MIN_VPD_RATIO_VIDEOS``, база сравнения — медиана канала целиком (ТЗ-33).

    При ``force=False`` видео, у которых уже есть строка на последний
    ``computed_at``, пропускаются; ``force=True`` пишет новый срез.
    """
    computed_at = int(now) if now is not None else config.now_ts()
    done: set[str] = set()
    if not force:
        latest = conn.execute(
            "SELECT MAX(computed_at) FROM video_scores"
        ).fetchone()[0]
        if latest is not None:
            done = {
                r[0] for r in conn.execute(
                    "SELECT video_id FROM video_scores WHERE computed_at = ?",
                    (latest,),
                )
            }

    where = report._format_where(fmt, "v")
    sql = """
        SELECT v.video_id
        FROM seo_fields sf
        JOIN videos v ON v.video_id = sf.video_id
    """
    params: tuple[Any, ...] = ()
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY v.published_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(limit),)
    candidates = [r[0] for r in conn.execute(sql, params).fetchall()]

    omap = report._outlier_map(conn, fmt)
    latest_snap = _latest_snapshot_map(conn)
    vpd_medians = _channel_vpd_medians(latest_snap)
    scored = skipped = 0
    for video_id in candidates:
        if not force and video_id in done:
            skipped += 1
            continue
        result = score_video(conn, video_id)
        snap = latest_snap.get(video_id)
        views = snap["views"] if snap is not None else None
        likes = snap["likes"] if snap is not None else None
        comments = snap["comments"] if snap is not None else None
        vpd = _snapshot_vpd(snap)
        channel_id = snap["channel_id"] if snap is not None else None
        is_shorts = snap["is_shorts"] if snap is not None else None
        db.save_score(
            conn,
            video_id,
            computed_at,
            packaging_score=result["score"],
            score_parts=json.dumps(result["parts"], ensure_ascii=False),
            outlier_score=omap.get(video_id),
            likes_per_1000=report._per_1000(likes, views),
            comments_per_1000=report._per_1000(comments, views),
            vpd=vpd,
            vpd_ratio=_vpd_ratio(vpd, channel_id, is_shorts, vpd_medians),
            comment_velocity=_snapshot_comment_velocity(snap),
        )
        scored += 1

    return {
        "scored": scored,
        "skipped": skipped,
        "total": len(candidates),
    }


# --- 3. patterns -----------------------------------------------------------

def _latest_views_map(conn) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT v.video_id AS video_id, s.views AS views
        FROM videos v
        JOIN snapshots s ON s.id = (
            SELECT id FROM snapshots
            WHERE video_id = v.video_id AND views IS NOT NULL
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
        )
        WHERE s.views IS NOT NULL
        """
    ).fetchall()
    return {r["video_id"]: float(r["views"]) for r in rows}


def _quote_share(part: int, whole: int) -> float | None:
    if not whole:
        return None
    return round(part / whole, 3)


def _mean(values: Sequence[float]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return round(mean(clean), 1)


def _med(values: Sequence[float]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return round(float(median(clean)), 1)


def _title_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    if not n:
        return {"n": 0, "avg_length": None, "median_length": None,
                "share_number": None, "share_question": None,
                "share_exclamation": None, "share_caps": None}
    lengths = [len(it["title"] or "") for it in items]
    number = sum(1 for it in items
                 if report._title_features(it["title"] or "")["title_has_number"])
    question = sum(1 for it in items if "?" in (it["title"] or ""))
    exclam = sum(1 for it in items if "!" in (it["title"] or ""))
    caps = sum(1 for it in items
               if caps_ratio(it["title"] or "") > TITLE_CAPS_MAX_RATIO)
    return {
        "n": n,
        "avg_length": _mean(lengths),
        "median_length": _med(lengths),
        "share_number": _quote_share(number, n),
        "share_question": _quote_share(question, n),
        "share_exclamation": _quote_share(exclam, n),
        "share_caps": _quote_share(caps, n),
    }


def _description_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    described = [it for it in items if (it["description"] or "").strip()]
    base = len(described)
    return {
        "n": n,
        "share_with_description": _quote_share(base, n),
        "base_with_description": base,
        "share_timestamps": _quote_share(
            sum(1 for it in described
                if _TIMESTAMP_RE.search(it["description"] or "")), base),
        "share_hashtags": _quote_share(
            sum(1 for it in described
                if _HASHTAG_RE.search(it["description"] or "")), base),
        "avg_hashtags": _mean(
            [len(_HASHTAG_RE.findall(it["description"] or "")) for it in described]),
    }


def _tags_stats(items: list[dict[str, Any]],
                with_top: bool = False) -> dict[str, Any]:
    n = len(items)
    tagged = [it for it in items if it["tags"]]
    base = len(tagged)
    counter: dict[str, int] = {}
    for it in tagged:
        for tag in it["tags"]:
            key = tag.strip().lower()
            if key:
                counter[key] = counter.get(key, 0) + 1
    top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    out: dict[str, Any] = {
        "n": n,
        "share_with_tags": _quote_share(base, n),
        "base_with_tags": base,
        "without_tags": n - base,
        "avg_tags": _mean([len(it["tags"]) for it in tagged]),
    }
    if with_top:
        out["top_tags"] = [{"tag": t, "count": c} for t, c in top]
    return out


def _timing_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    by_hour: dict[int, int] = {}
    by_weekday: dict[int, int] = {}
    for it in items:
        hour, weekday, _ = report._msk_parts(it["published_at"])
        if hour is not None:
            by_hour[hour] = by_hour.get(hour, 0) + 1
        if weekday is not None:
            by_weekday[weekday] = by_weekday.get(weekday, 0) + 1
    peak_hours = sorted(by_hour.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    peak_days = sorted(by_weekday.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return {
        "n": n,
        "base_with_time": sum(
            1 for it in items if report._msk_parts(it["published_at"])[0] is not None),
        "by_hour_msk": dict(sorted(by_hour.items())),
        "by_weekday": dict(sorted(by_weekday.items())),
        "peak_hours": [{"hour": h, "count": c} for h, c in peak_hours],
        "peak_weekdays": [
            {"weekday": w, "name": report.WEEKDAYS_RU[w], "count": c}
            for w, c in peak_days
        ],
    }


_DURATION_BUCKETS: tuple[tuple[int, int | None, str], ...] = (
    (0, 300, "до 5 мин"),
    (300, 600, "5-10 мин"),
    (600, 1200, "10-20 мин"),
    (1200, 2400, "20-40 мин"),
    (2400, None, "свыше 40 мин"),
)


def _duration_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    def group(flag: int) -> list[dict[str, Any]]:
        if flag == 1:
            return [it for it in items if it["is_shorts"] == 1]
        return [it for it in items if it["is_shorts"] != 1]

    def summary(g: list[dict[str, Any]]) -> dict[str, Any]:
        durs = [it["duration"] for it in g if it["duration"] is not None]
        return {"n": len(g), "with_duration": len(durs),
                "median_seconds": _med(durs), "avg_seconds": _mean(durs)}

    out = {"short": summary(group(1)), "long": summary(group(0)),
           "long_buckets": []}
    longs = group(0)
    for lo, hi, label in _DURATION_BUCKETS:
        members = []
        for it in longs:
            d = it["duration"]
            if d is None or d < lo:
                continue
            if hi is not None and d >= hi:
                continue
            members.append(it)
        views = [it["views"] for it in members if it["views"] is not None]
        out["long_buckets"].append({
            "label": label,
            "n": len(members),
            "median_views": _med(views),
        })
    return out


def _top_words_stats(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: dict[str, int] = {}
    for it in items:
        for w in significant_words(it["title"] or ""):
            counter[w] = counter.get(w, 0) + 1
    top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    return [{"word": w, "count": c} for w, c in top]


def _word_presence(items: list[dict[str, Any]]) -> dict[str, int]:
    """Сколько РАЗНЫХ видео группы содержат слово в заголовке.

    Слово внутри одного заголовка считается один раз: доля считается по
    видео, а не по вхождениям.
    """
    counter: dict[str, int] = {}
    for it in items:
        for w in set(significant_words(it["title"] or "")):
            counter[w] = counter.get(w, 0) + 1
    return counter


def discriminative_words(outlier_items: list[dict[str, Any]],
                         background_items: list[dict[str, Any]],
                         min_count: int = 5,
                         limit: int = 15) -> list[dict[str, Any]]:
    """Слова, отличающие заголовки выбросов от фона.

    Для каждого слова берётся доля ВИДЕО, в заголовке которых оно есть:
    ``p_out`` у выбросов и ``p_bg`` у фона. ``diff = p_out - p_bg`` — на сколько
    долей слово чаще у выбросов. ``z`` — значимость разницы двух долей на
    объединённой (пул) оценке ``p``:

        z = (p_out - p_bg) / sqrt(p * (1 - p) * (1/n_out + 1/n_bg))

    Слова с суммарным числом видео в объединённой выборке меньше ``min_count``
    отбрасываются. Сортировка по ``diff`` по убыванию. Пустая группа — пустой
    список: долю на пустой выборке не построить.
    """
    n_out = len(outlier_items)
    n_bg = len(background_items)
    if n_out == 0 or n_bg == 0:
        return []
    out_have = _word_presence(outlier_items)
    bg_have = _word_presence(background_items)
    result: list[dict[str, Any]] = []
    for word in set(out_have) | set(bg_have):
        c_out = out_have.get(word, 0)
        c_bg = bg_have.get(word, 0)
        if c_out + c_bg < int(min_count):
            continue
        p_out = c_out / n_out
        p_bg = c_bg / n_bg
        diff = p_out - p_bg
        p = (c_out + c_bg) / (n_out + n_bg)
        se = math.sqrt(p * (1.0 - p) * (1.0 / n_out + 1.0 / n_bg))
        z = diff / se if se > 0 else 0.0
        result.append({
            "word": word,
            "p_out": p_out,
            "p_bg": p_bg,
            "diff": diff,
            "z": z,
            "count_out": c_out,
            "count_bg": c_bg,
            "n_out": n_out,
            "n_bg": n_bg,
        })
    result.sort(key=lambda x: (-x["diff"], x["word"]))
    return result[:limit]


def _format_mix(items: list[dict[str, Any]]) -> dict[str, int]:
    """Число шортсов и полных в группе (плюс её размер)."""
    shorts = sum(1 for it in items if it["is_shorts"] == 1)
    return {"n": len(items), "shorts": shorts, "long": len(items) - shorts}


def patterns(conn, min_outlier: float = 3.0, fmt: str | None = None,
             lang: str | None = None, days: int | None = None,
             channel: str | None = None) -> dict[str, Any]:
    """Сравнить оформление выбросов (≥ min_outlier) с фоном.

    Выброс — видео с ``report.outlier_score`` не ниже порога; фон — остальные
    видео с известной медианой канала. Видео без медианы честно считаются
    пропущенными: без базы сравнения outlier не существует. При заданном
    ``channel`` выборки сужаются до этого канала: его выбросы против его фона.
    """
    now = int(config.now_ts())
    where: list[str] = []
    params: list[Any] = []
    fmt_where = report._format_where(fmt, "v")
    if fmt_where:
        where.append(fmt_where)
    if lang == "ru":
        where.append(report._is_russian_expr())
    elif lang == "world":
        where.append(f"NOT {report._is_russian_expr()}")
    if days is not None:
        where.append("v.published_at IS NOT NULL AND v.published_at >= ?")
        params.append(now - int(days) * 86400)
    if channel:
        where.append("v.channel_id = ?")
        params.append(channel)

    sql = """
        SELECT v.video_id AS video_id, v.title AS title,
               v.description AS description, v.tags AS tags,
               v.duration_seconds AS duration_seconds,
               v.is_shorts AS is_shorts, v.published_at AS published_at,
               vc.topic AS topic
        FROM videos v
        LEFT JOIN video_classification vc ON vc.video_id = v.video_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql, params).fetchall()

    omap = report._outlier_map(conn, fmt)
    views_map = _latest_views_map(conn)
    format_of = {r["video_id"]: report._video_format(r["is_shorts"]) for r in rows}

    outliers: list[dict[str, Any]] = []
    background: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        vid = row["video_id"]
        o = omap.get(vid)
        item = {
            "video_id": vid,
            "title": row["title"] or "",
            "description": row["description"] or "",
            "tags": report._parse_tags(row["tags"]),
            "duration": row["duration_seconds"],
            "is_shorts": row["is_shorts"],
            "published_at": row["published_at"],
            "views": views_map.get(vid),
            "outlier": o,
            "topic": row["topic"],
        }
        if o is None:
            skipped += 1
            continue
        if o >= float(min_outlier):
            outliers.append(item)
        else:
            background.append(item)

    sample: list[dict[str, Any]] = []
    for it in sorted(outliers, key=lambda x: -(x["outlier"] or 0))[:20]:
        try:
            pack = score_video(conn, it["video_id"], fmt)["score"]
        except ValueError:
            pack = None
        date = None
        if it["published_at"] is not None:
            date = datetime.fromtimestamp(
                int(it["published_at"]), tz=MSK).strftime("%Y-%m-%d")
        sample.append({
            "video_id": it["video_id"],
            "title": it["title"],
            "views": it["views"],
            "outlier_score": round(it["outlier"], 2) if it["outlier"] else None,
            "pack_score": pack,
            "format": format_of.get(it["video_id"], "long"),
            "published_date": date,
        })

    return {
        "filters": {
            "min_outlier": float(min_outlier),
            "format": fmt,
            "lang": lang,
            "days": days,
            "channel": channel,
        },
        "channel": channel,
        "population": len(rows),
        "outliers": {"n": len(outliers)},
        "background": {"n": len(background)},
        "skipped_no_median": skipped,
        "title": {
            "outlier": _title_stats(outliers),
            "background": _title_stats(background),
        },
        "description": {
            "outlier": _description_stats(outliers),
            "background": _description_stats(background),
        },
        "tags": {
            "outlier": _tags_stats(outliers, with_top=True),
            "background": _tags_stats(background),
        },
        "timing": {
            "outlier": _timing_stats(outliers),
            "background": _timing_stats(background),
        },
        "formats": {
            "outlier": _format_mix(outliers),
            "background": _format_mix(background),
        },
        "duration": _duration_stats(outliers),
        "top_words": _top_words_stats(outliers),
        "discriminative_words": discriminative_words(outliers, background),
        "sample": sample,
        "not_available": list(NOT_AVAILABLE),
    }


# --- 4. Отбор образцов и статистика для брифа -------------------------------

# Меньше этого числа образцов — статистики нет, рекомендации не выдаём.
MIN_BRIEF_SAMPLE = 5

# ТЗ-22: каталог отчётов пакетного аудита каналов. Тесты подменяют его.
AUDIT_DIR = config.TUBER_DIR / "docs" / "audits"

# Меньше этого числа видео с данными в канале — предупреждаем о слабой выборке.
MIN_CHANNEL_DATA = 10

# Признак «личный результат» в заголовке: «я …», «мой …», «за N дней/долларов»,
# «$N». Это регексы методики, а не выдуманные правила.
_PERSONAL_RES = (
    re.compile(r"(?:^|\s)я(?:\s|$)", re.IGNORECASE),
    re.compile(r"(?:^|\s)мо[йяеи](?:\s|$)", re.IGNORECASE),
    re.compile(
        r"\bза\s+\d+[-\s]?(?:дней|дня|день|долларов|доллара|доллар|"
        r"часов|часа|час)",
        re.IGNORECASE,
    ),
    re.compile(r"\$\s?\d"),
)


def has_personal_result(title: str | None) -> int:
    """1, если в заголовке есть личный результат (я/мой/за N дней/$N)."""
    text = title or ""
    return 1 if any(rx.search(text) for rx in _PERSONAL_RES) else 0


def _percentile(values: Sequence[float], q: float) -> float | None:
    """Линейная интерполяция процентиля (q в долях: 0.25, 0.75)."""
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return round(clean[0], 1)
    pos = (len(clean) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(clean) - 1)
    frac = pos - lo
    return round(clean[lo] * (1.0 - frac) + clean[hi] * frac, 1)


def _top_words_n(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    counter: dict[str, int] = {}
    for it in items:
        for w in significant_words(it["title"] or ""):
            counter[w] = counter.get(w, 0) + 1
    top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"word": w, "count": c} for w, c in top]


def _hashtags_in(description: str) -> list[str]:
    return [h[1:].lower() for h in _HASHTAG_RE.findall(description or "")]


def _hashtag_top(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: dict[str, int] = {}
    for it in items:
        for h in set(_hashtags_in(it["description"] or "")):
            if h and not h.isdigit():
                counter[h] = counter.get(h, 0) + 1
    top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"tag": t, "count": c} for t, c in top]


def _typo_variants(keyword: str | None) -> list[str]:
    """Опечатки основного ключа: перестановка соседних букв."""
    if not keyword or not str(keyword).strip():
        return []
    chars = list(str(keyword).strip())
    variants: set[str] = set()
    for i in range(len(chars) - 1):
        if chars[i] == " " or chars[i + 1] == " ":
            continue
        swapped = list(chars)
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
        variants.add("".join(swapped))
    return sorted(variants)


def _title_templates(top_words: list[dict[str, Any]]) -> list[str]:
    """3-5 шаблонов-заготовок из реальных слов топа. Слоты в {…} — не выдумка."""
    words = [w["word"] for w in top_words]
    if len(words) < 2:
        return []
    a, b = words[0], words[1]
    c = words[2] if len(words) > 2 else b
    candidates = [
        f"{a}: {b} — {{итог одной фразой}}",
        f"Как {a} меняет {b}: {{разбор за N минут}}",
        f"{{N}} фактов о {a} и {b}",
        f"{a} против {b}: {{честное сравнение}}",
        f"Почему {c} без {a} не работает",
    ]
    count = min(5, max(3, len(words)))
    return candidates[:count]


def _brief_items(conn, topic: str | None, channel_id: str | None,
                 fmt: str | None, lang: str | None,
                 min_outlier: float) -> list[dict[str, Any]]:
    """Видео-образцы с выбросом не ниже порога (общий отбор брифа и аудита)."""
    where: list[str] = []
    params: list[Any] = []
    fmt_where = report._format_where(fmt, "v")
    if fmt_where:
        where.append(fmt_where)
    if lang == "ru":
        where.append(report._is_russian_expr())
    elif lang == "world":
        where.append(f"NOT {report._is_russian_expr()}")
    sql = """
        SELECT v.video_id AS video_id, v.channel_id AS channel_id,
               v.title AS title, v.description AS description, v.tags AS tags,
               v.duration_seconds AS duration_seconds, v.is_shorts AS is_shorts,
               v.published_at AS published_at,
               vc.topic AS topic, vc.title_ru AS title_ru
        FROM videos v
        LEFT JOIN video_classification vc ON vc.video_id = v.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql, params).fetchall()
    omap = report._outlier_map(conn, fmt)
    views_map = _latest_views_map(conn)

    channel_topics: set[str] = set()
    if channel_id:
        for row in rows:
            if row["channel_id"] == channel_id and row["topic"]:
                channel_topics.add(row["topic"])

    items: list[dict[str, Any]] = []
    for row in rows:
        vid = row["video_id"]
        o = omap.get(vid)
        if o is None or o < float(min_outlier):
            continue
        if topic and matches_topic(topic, row["topic"]) != 1:
            continue
        if channel_id:
            same_channel = row["channel_id"] == channel_id
            same_topic = bool(row["topic"]) and row["topic"] in channel_topics
            if not (same_channel or same_topic):
                continue
        items.append({
            "video_id": vid,
            "title": row["title"] or "",
            "description": row["description"] or "",
            "tags": report._parse_tags(row["tags"]),
            "duration": row["duration_seconds"],
            "is_shorts": row["is_shorts"],
            "published_at": row["published_at"],
            "views": views_map.get(vid),
            "outlier": o,
            "topic": row["topic"],
            "own_channel": (row["channel_id"] == channel_id) if channel_id else None,
        })
    return items


# --- 4a. brief --------------------------------------------------------------


def _brief_title(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    lengths = [len(it["title"] or "") for it in items]
    cnt_number = sum(1 for it in items
                     if report._title_features(it["title"] or "")["title_has_number"])
    cnt_question = sum(1 for it in items if "?" in (it["title"] or ""))
    cnt_personal = sum(1 for it in items if has_personal_result(it["title"]))
    top_words = _top_words_n(items, 10)
    return {
        "n": n,
        "avg_length": _mean(lengths),
        "median_length": _med(lengths),
        "share_number": _quote_share(cnt_number, n),
        "share_question": _quote_share(cnt_question, n),
        "share_personal": _quote_share(cnt_personal, n),
        "top_words": top_words,
        "templates": _title_templates(top_words),
    }


def _brief_description(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    described = [it for it in items if (it["description"] or "").strip()]
    base = len(described)
    desc_lengths = [len(it["description"] or "") for it in described]
    cnt_ts = sum(1 for it in described
                 if _TIMESTAMP_RE.search(it["description"] or ""))
    cnt_ht = sum(1 for it in described
                 if _HASHTAG_RE.search(it["description"] or ""))
    hashtags = [len(_HASHTAG_RE.findall(it["description"] or "")) for it in described]
    avg_len = _mean(desc_lengths)
    share_links = _quote_share(
        sum(1 for it in described if _LINK_RE.search(it["description"] or "")),
        base,
    )
    structure = [
        {"part": "Сниппет (первые 150 знаков)",
         "hint": f"основной ключ и суть; у образцов средняя длина описания "
                 f"{_fmt_num(avg_len)} знаков (n={base})"},
        {"part": "Длинные вариации ключа (2-3)",
         "hint": "естественные формулировки ключа в теле, без переспама"},
        {"part": "Тело описания",
         "hint": f"раскрыть содержание; у образцов средняя длина "
                 f"{_fmt_num(avg_len)} знаков (n={base})"},
        {"part": "Таймкоды",
         "hint": f"есть у {_fmt_pct(_quote_share(cnt_ts, base))} образцов "
                 f"(n={base})"},
        {"part": "Хэштеги (3-5)",
         "hint": f"у образцов в среднем {_fmt_num(_mean(hashtags))} "
                 f"хэштегов (n={base})"},
        {"part": "Ссылка",
         "hint": f"ссылка встречается у {_fmt_pct(share_links)} образцов "
                 f"(n={base})"},
    ]
    return {
        "n": n,
        "base_with_description": base,
        "share_with_description": _quote_share(base, n),
        "avg_length": avg_len,
        "share_timestamps": _quote_share(cnt_ts, base),
        "share_hashtags": _quote_share(cnt_ht, base),
        "avg_hashtags": _mean(hashtags),
        "structure": structure,
    }


def _brief_duration(items: list[dict[str, Any]]) -> dict[str, Any]:
    def group(is_short: int) -> list[dict[str, Any]]:
        return [it for it in items
                if (it["is_shorts"] == 1) == bool(is_short)]

    def summary(g: list[dict[str, Any]]) -> dict[str, Any]:
        durs = [it["duration"] for it in g if it["duration"] is not None]
        return {
            "n": len(g),
            "with_duration": len(durs),
            "median_seconds": _med(durs),
            "p25_seconds": _percentile(durs, 0.25),
            "p75_seconds": _percentile(durs, 0.75),
        }

    best = None
    for bucket in _duration_stats(items)["long_buckets"]:
        if bucket["n"] and bucket["median_views"] is not None:
            if best is None or bucket["median_views"] > best["median_views"]:
                best = bucket
    return {"short": summary(group(1)), "long": summary(group(0)),
            "best_zone": best}


def _brief_checklist(title: dict[str, Any], description: dict[str, Any],
                     tags: dict[str, Any], duration: dict[str, Any],
                     timing: dict[str, Any]) -> list[dict[str, Any]]:
    """Пункты к публикации: каждый с ориентиром и числом из данных."""
    n = title["n"]
    checklist: list[dict[str, Any]] = [
        {"item": "Длина заголовка",
         "benchmark": f"цель {TITLE_MIN_LEN}-{TITLE_MAX_LEN} знаков",
         "evidence": f"у залетевших медиана {_fmt_num(title['median_length'])} "
                     f"знаков (n={n})"},
        {"item": "Цифра в заголовке",
         "benchmark": "добавить число, если его нет",
         "evidence": f"с цифрой {_fmt_pct(title['share_number'])} залетевших (n={n})"},
        {"item": "Вопрос в заголовке",
         "benchmark": "по желанию",
         "evidence": f"с вопросом {_fmt_pct(title['share_question'])} залетевших "
                     f"(n={n})"},
        {"item": "Личный результат",
         "benchmark": "«я/мой/за N дней/$N», если уместно",
         "evidence": f"у {_fmt_pct(title['share_personal'])} залетевших (n={n})"},
        {"item": "Описание до 150 знаков",
         "benchmark": "ключ в первых 150 знаках",
         "evidence": f"описание есть у {_fmt_pct(description['share_with_description'])} "
                     f"(n={description['n']}), средняя длина "
                     f"{_fmt_num(description['avg_length'])} знаков"},
        {"item": "Таймкоды",
         "benchmark": "обязательны для видео от 8 минут",
         "evidence": f"таймкоды у {_fmt_pct(description['share_timestamps'])} "
                     f"образцов (n={description['base_with_description']})"},
        {"item": "Хэштеги",
         "benchmark": f"{DESC_HASHTAG_MIN}-{DESC_HASHTAG_MAX} штук",
         "evidence": f"хэштеги у {_fmt_pct(description['share_hashtags'])} образцов, "
                     f"в среднем {_fmt_num(description['avg_hashtags'])} "
                     f"(n={description['base_with_description']})"},
        {"item": "Теги",
         "benchmark": f"{TAGS_GOOD_MIN}-{TAGS_GOOD_MAX} штук плюс опечатки ключа",
         "evidence": f"теги есть у {_fmt_pct(tags['share_with_tags'])} залетевших, "
                     f"среднее {_fmt_num(tags['avg_tags'])} "
                     f"(n={tags['base_with_tags']})"},
        {"item": "Длительность",
         "benchmark": "под запрос; шортс — до минуты",
         "evidence": f"медиана полных {_fmt_num(duration['long']['median_seconds'])} с, "
                     f"25-й-75-й процентили "
                     f"{_fmt_num(duration['long']['p25_seconds'])}-"
                     f"{_fmt_num(duration['long']['p75_seconds'])} с "
                     f"(n={duration['long']['with_duration']})"},
    ]
    if timing["peak_hours"]:
        peaks = ", ".join(f"{p['hour']}:00 ({p['count']})"
                          for p in timing["peak_hours"])
        checklist.append({
            "item": "Время публикации",
            "benchmark": "попасть в пик",
            "evidence": f"пики по МСК: {peaks} (n={timing['n']})",
        })
    return checklist


def brief(conn, topic: str | None = None, keyword: str | None = None,
          channel_id: str | None = None, fmt: str | None = None,
          lang: str | None = None, min_outlier: float = 3.0) -> dict[str, Any]:
    """SEO-бриф на новое видео по образцам залетевших из боевой базы.

    Рекомендации строятся только на наблюдениях: у каждой цифры есть ``n``.
    Меньше ``MIN_BRIEF_SAMPLE`` образцов — честный ``enough=False`` без
    выдуманных шаблонов и советов.
    """
    items = _brief_items(conn, topic, channel_id, fmt, lang, min_outlier)
    n = len(items)
    result: dict[str, Any] = {
        "enough": n >= MIN_BRIEF_SAMPLE,
        "sample_size": n,
        "filters": {
            "topic": topic, "keyword": keyword, "channel_id": channel_id,
            "format": fmt, "lang": lang, "min_outlier": float(min_outlier),
        },
        "not_available": list(NOT_AVAILABLE),
    }
    if channel_id is not None:
        own_n = sum(1 for it in items if it.get("own_channel"))
        result["sample_own_channel"] = own_n
        result["sample_by_topic"] = n - own_n
        data_n = _videos_with_data_by_channel(conn).get(channel_id, 0)
        result["channel_data_size"] = data_n
        result["low_data"] = data_n < MIN_CHANNEL_DATA
    else:
        result["sample_own_channel"] = None
        result["sample_by_topic"] = None
    if n < MIN_BRIEF_SAMPLE:
        result["reason"] = (
            f"образцов {n}, нужно минимум {MIN_BRIEF_SAMPLE}: "
            f"статистики на такой выборке нет"
        )
        return result

    title = _brief_title(items)
    description = _brief_description(items)
    tags_stats = _tags_stats(items, with_top=True)
    duration = _brief_duration(items)
    timing = _timing_stats(items)
    hashtags = _hashtag_top(items)
    tags = {
        "n": n,
        "base_with_tags": tags_stats["base_with_tags"],
        "share_with_tags": tags_stats["share_with_tags"],
        "without_tags": tags_stats["without_tags"],
        "avg_tags": tags_stats["avg_tags"],
        "top_tags": tags_stats["top_tags"],
        "keyword": keyword,
        "typo_variants": _typo_variants(keyword),
    }
    result.update({
        "title": title,
        "description": description,
        "tags": tags,
        "duration": duration,
        "timing": timing,
        "hashtags": {
            "recommended": [h["tag"] for h in hashtags[:5]],
            "top": hashtags[:15],
        },
        "checklist": _brief_checklist(title, description, tags, duration, timing),
    })
    return result


# --- 4b. audit_channel ------------------------------------------------------


def _channel_title(conn, channel_id: str) -> str:
    """Название канала из базы; если его нет — сам ID (не выдумываем)."""
    row = conn.execute(
        "SELECT title FROM channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    if row is not None and row["title"]:
        return str(row["title"])
    return channel_id


def _videos_with_data_by_channel(conn) -> dict[str, int]:
    """Сколько видео канала уже разобрано в ``seo_fields`` (есть данные)."""
    rows = conn.execute(
        """
        SELECT v.channel_id AS channel_id, COUNT(*) AS n
        FROM videos v
        JOIN seo_fields sf ON sf.video_id = v.video_id
        WHERE v.channel_id IS NOT NULL
        GROUP BY v.channel_id
        """
    ).fetchall()
    return {str(r["channel_id"]): int(r["n"]) for r in rows}


def top_channels_by_outliers(conn, limit: int,
                             min_outlier: float = 3.0) -> list[str]:
    """ID каналов с наибольшим числом выбросов (outlier ≥ ``min_outlier``).

    Сортировка: по числу выбросов вниз, при равенстве — по ID для
    детерминированности. Только каналы, у которых выбросы реально есть.
    """
    omap = report._outlier_map(conn)
    rows = conn.execute(
        "SELECT video_id, channel_id FROM videos WHERE channel_id IS NOT NULL"
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        o = omap.get(r["video_id"])
        if o is not None and o >= float(min_outlier):
            cid = str(r["channel_id"])
            counts[cid] = counts.get(cid, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [cid for cid, _ in ordered[: int(limit)]]


def audit_channel(conn, channel_id: str,
                  min_outlier: float = 3.0) -> dict[str, Any]:
    """SEO-аудит канала по всем его видео в базе.

    Считаем доли от видео канала, агрегируем проблемы по частоте и показываем
    лучшие/худшие по упаковке. ``min_outlier`` помечает, сколько видео канала
    реально залетели. CTR и удержание по чужим видео недоступны.
    """
    rows = conn.execute(
        """
        SELECT v.video_id AS video_id, v.channel_id AS channel_id,
               v.title AS title, v.description AS description, v.tags AS tags,
               v.duration_seconds AS duration_seconds, v.is_shorts AS is_shorts,
               v.published_at AS published_at
        FROM videos v
        WHERE v.channel_id = ?
        ORDER BY v.video_id
        """,
        (channel_id,),
    ).fetchall()
    views_map = _latest_views_map(conn)
    omap = report._outlier_map(conn)
    items = [{
        "video_id": r["video_id"],
        "title": r["title"] or "",
        "description": r["description"] or "",
        "tags": report._parse_tags(r["tags"]),
        "duration": r["duration_seconds"],
        "is_shorts": r["is_shorts"],
        "published_at": r["published_at"],
        "views": views_map.get(r["video_id"]),
        "outlier": omap.get(r["video_id"]),
    } for r in rows]

    n = len(items)
    result: dict[str, Any] = {
        "channel_id": channel_id,
        "channel_title": _channel_title(conn, channel_id),
        "videos": n,
        "min_outlier": float(min_outlier),
        "not_available": list(NOT_AVAILABLE),
    }
    if not n:
        result["enough"] = False
        result["reason"] = "у канала нет видео в базе"
        return result

    with_tags = sum(1 for it in items if it["tags"])
    described = [it for it in items if it["description"].strip()]
    base_desc = len(described)
    lengths = [len(it["title"]) for it in items]
    scores_by_video: dict[str, float] = {}
    issues_counter: dict[str, dict[str, Any]] = {}
    for it in items:
        rated = score_video(conn, it["video_id"])
        scores_by_video[it["video_id"]] = rated["score"]
        for issue in rated["issues"]:
            entry = issues_counter.setdefault(issue["code"], {
                "code": issue["code"], "level": issue["level"],
                "count": 0, "text": issue["text"],
            })
            entry["count"] += 1

    def fmt(is_short: int) -> list[dict[str, Any]]:
        return [it for it in items if (it["is_shorts"] == 1) == bool(is_short)]

    def median_views(group: list[dict[str, Any]]) -> float | None:
        return _med([it["views"] for it in group if it["views"] is not None])

    def avg_score(group: list[dict[str, Any]]) -> float | None:
        return _mean([scores_by_video[it["video_id"]] for it in group])

    all_scores = [scores_by_video[it["video_id"]] for it in items]
    ordering = sorted(items, key=lambda it: (-scores_by_video[it["video_id"]],
                                             it["video_id"]))
    worst = sorted(items, key=lambda it: (scores_by_video[it["video_id"]],
                                          it["video_id"]))

    def pack(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "video_id": it["video_id"],
            "title": it["title"],
            "views": it["views"],
            "score": scores_by_video[it["video_id"]],
        } for it in group[:3]]

    result.update({
        "enough": True,
        "with_tags": with_tags,
        "share_with_tags": _quote_share(with_tags, n),
        "without_tags": n - with_tags,
        "title_length": {
            "avg": _mean(lengths),
            "median": _med(lengths),
            "share_over_60": _quote_share(
                sum(1 for x in lengths if x > TITLE_MAX_LEN), n),
            "share_under_30": _quote_share(
                sum(1 for x in lengths if x < TITLE_MIN_LEN), n),
        },
        "description": {
            "share_with_description": _quote_share(base_desc, n),
            "base_with_description": base_desc,
            "share_timestamps": _quote_share(
                sum(1 for it in described
                    if _TIMESTAMP_RE.search(it["description"])), base_desc),
            "share_hashtags": _quote_share(
                sum(1 for it in described
                    if _HASHTAG_RE.search(it["description"])), base_desc),
        },
        "distribution": _timing_stats(items),
        "format_mix": {
            "short": {"n": len(fmt(1)), "median_views": median_views(fmt(1))},
            "long": {"n": len(fmt(0)), "median_views": median_views(fmt(0))},
        },
        "scores": {
            "avg": _mean(all_scores),
            "short_avg": avg_score(fmt(1)),
            "long_avg": avg_score(fmt(0)),
        },
        "problems": sorted(
            issues_counter.values(),
            key=lambda e: (-e["count"], e["code"]),
        ),
        "best": pack(ordering),
        "worst": pack(worst),
        "outliers": sum(
            1 for it in items
            if it["outlier"] is not None and it["outlier"] >= float(min_outlier)),
    })
    return result


def audit_channels(conn, channel_ids: Sequence[str],
                   min_outlier: float = 3.0,
                   out_dir: Any = None) -> dict[str, Any]:
    """Аудит списка каналов: сводка + файл отчёта на каждый канал (ТЗ-22).

    Канал без единого видео с ``seo_fields`` пропускается (пустой отчёт не
    пишется) и честно перечисляется в ``skipped``. Отчёты сохраняются в
    ``out_dir`` (по умолчанию ``docs/audits/<channel_id>.md``).
    """
    directory = Path(out_dir) if out_dir is not None else AUDIT_DIR
    with_data = _videos_with_data_by_channel(conn)
    reports: list[dict[str, Any]] = []
    skipped: list[str] = []
    for cid in channel_ids:
        n_data = with_data.get(cid, 0)
        if not n_data:
            skipped.append(cid)
            continue
        res = audit_channel(conn, cid, min_outlier=min_outlier)
        path = save_audit_report(res, out_dir=directory)
        reports.append({
            "channel_id": cid,
            "channel_title": res.get("channel_title", cid),
            "videos_in_analysis": n_data,
            "avg_score": res["scores"]["avg"] if res.get("enough") else None,
            "top_problems": [p["code"] for p in res["problems"][:3]],
            "report_path": str(path),
            "report_size": path.stat().st_size,
        })
    return {
        "reports": reports,
        "skipped": skipped,
        "out_dir": str(directory),
        "min_outlier": float(min_outlier),
    }


# --- Человекочитаемый вывод -------------------------------------------------


def _fmt_num(value: Any, digits: int = 1) -> str:
    if value is None:
        return "нет данных"
    if isinstance(value, float):
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "нет данных"
    return f"{round(value * 100, 1)}%"


def _fmt_seconds(value: Any) -> str:
    """Секунды; при отсутствии данных единица измерения не печатается вовсе."""
    if value is None:
        return "нет данных"
    return f"{_fmt_num(value, 0)} с"


def _fmt_len(value: Any) -> str:
    """Знаки; при отсутствии данных единица измерения не печатается вовсе."""
    if value is None:
        return "нет данных"
    return f"{_fmt_num(value)} знаков"


def format_analyze(summary: dict[str, int]) -> str:
    return (
        f"SEO-разбор: видео {summary['videos']}, записано {summary['written']}, "
        f"пропущено (уже есть) {summary['skipped']}, "
        f"без заголовка {summary['no_title']}."
    )


def format_score_refresh(summary: dict[str, int]) -> str:
    return (
        f"Скор упаковки: записано {summary['scored']}, "
        f"пропущено (уже есть) {summary['skipped']}, "
        f"всего кандидатов {summary['total']}."
    )


def format_score(result: dict[str, Any]) -> str:
    lines = [f"Видео {result['video_id']}: упаковка {result['score']}/100 "
             f"(проверок применено: {result['checks']})."]
    lines.append(
        "  Компоненты: " + ", ".join(
            f"{k} {_fmt_num(v)}" for k, v in result["parts"].items()))
    if result["issues"]:
        lines.append("  Проблемы:")
        for issue in result["issues"]:
            lines.append(f"    [{issue['level']}] {issue['text']}")
    else:
        lines.append("  Проблем нет.")
    if result["not_applicable"]:
        lines.append("  Не проверялось:")
        for note in result["not_applicable"]:
            lines.append(f"    - {note}")
    return "\n".join(lines)


def _group_line(name: str, stats: dict[str, Any]) -> str:
    return f"  {name}: n={stats['n']}"


def _discriminative_line(x: dict[str, Any]) -> str:
    sign = "+" if x["diff"] >= 0 else ""
    return (
        f"{x['word']} — {_fmt_pct(x['p_out'])} у выбросов против "
        f"{_fmt_pct(x['p_bg'])} у фона "
        f"({sign}{round(x['diff'] * 100, 1)} п.п., z={x['z']:.1f}, "
        f"n={x['n_out']}/{x['n_bg']})"
    )


def _patterns_channel_lines(res: dict[str, Any]) -> list[str]:
    """Срез по одному каналу: часы, дни, форматы, длительности, слова."""
    channel = res.get("channel")
    if not channel:
        return []
    n_out = res["outliers"]["n"]
    n_bg = res["background"]["n"]
    lines = [f"Срез по каналу {channel}: выбросов {n_out}, фона {n_bg}."]
    if n_out < MIN_BRIEF_SAMPLE:
        lines.append(
            f"  Мало данных (n={n_out}): проценты и отличающие слова не строю."
        )
        return lines
    for name, label in (("outlier", "Выбросы"), ("background", "Фон")):
        tm = res["timing"][name]
        hours = ", ".join(
            f"{h}:00 ({c})" for h, c in sorted(
                tm["by_hour_msk"].items(), key=lambda kv: (-kv[1], kv[0])
            )[:3]
        )
        days = ", ".join(
            f"{report.WEEKDAYS_RU[w]} ({c})" for w, c in sorted(
                tm["by_weekday"].items(), key=lambda kv: (-kv[1], kv[0])
            )[:3]
        )
        fm = res["formats"][name]
        lines.append(
            f"  {label} (n={tm['n']}): часы: {hours or 'нет данных'}; "
            f"дни: {days or 'нет данных'}; "
            f"формат: шортс={fm['shorts']}, полное={fm['long']}."
        )
    dur = res["duration"]
    lines.append(
        f"  Длительности выбросов канала: шортсы n={dur['short']['n']}, "
        f"медиана {_fmt_seconds(dur['short']['median_seconds'])}; полные "
        f"n={dur['long']['n']}, медиана {_fmt_seconds(dur['long']['median_seconds'])}."
    )
    words = res.get("discriminative_words") or []
    if words:
        lines.append(f"  Отличающие слова канала (n={n_out}/{n_bg}):")
        lines.extend(f"    {_discriminative_line(x)}" for x in words[:15])
    else:
        lines.append("  Отличающие слова канала: нет данных.")
    return lines


def format_patterns(res: dict[str, Any]) -> str:
    f = res["filters"]
    lines: list[str] = []
    lines.append(
        f"Паттерны упаковки: порог выброса {f['min_outlier']}x, "
        f"формат {f['format'] or 'все'}, язык {f['lang'] or 'все'}, "
        f"окно {str(f['days']) + ' дней' if f['days'] else 'всё время'}.")
    lines.append(
        f"Популяция {res['population']}: выбросов {res['outliers']['n']}, "
        f"фона {res['background']['n']}, без медианы канала "
        f"(пропущено) {res['skipped_no_median']}.")
    if res["outliers"]["n"] == 0:
        lines.append("Выбросов не найдено: разбивки пусты, фон показан как есть.")

    for name, label in (("outlier", "Выбросы"), ("background", "Фон")):
        t = res["title"][name]
        lines.append(
            f"{label} — заголовок: n={t['n']}, средняя длина {_fmt_len(t['avg_length'])}, "
            f"медиана {_fmt_len(t['median_length'])}, "
            f"с цифрой {_fmt_pct(t['share_number'])}, "
            f"с вопросом {_fmt_pct(t['share_question'])}, "
            f"с восклицанием {_fmt_pct(t['share_exclamation'])}, "
            f"капс {_fmt_pct(t['share_caps'])}.")
        d = res["description"][name]
        lines.append(
            f"{label} — описание: n={d['n']}, с описанием "
            f"{_fmt_pct(d['share_with_description'])}, с таймкодами "
            f"{_fmt_pct(d['share_timestamps'])}, с хэштегами "
            f"{_fmt_pct(d['share_hashtags'])}, среднее число хэштегов "
            f"{_fmt_num(d['avg_hashtags'])} (база с описанием "
            f"{d['base_with_description']}).")
        g = res["tags"][name]
        lines.append(
            f"{label} — теги: n={g['n']}, с тегами {_fmt_pct(g['share_with_tags'])}, "
            f"среднее число тегов {_fmt_num(g['avg_tags'])} от тех, у кого они есть "
            f"(база {g['base_with_tags']}); без тегов {g['without_tags']}.")
        if g.get("top_tags"):
            top = ", ".join(f"{x['tag']} ({x['count']})" for x in g["top_tags"][:10])
            lines.append(f"{label} — топ тегов: {top}.")

    tm = res["timing"]["outlier"]
    lines.append(
        f"Тайминг выбросов (МСК): n={tm['n']}, с известным временем "
        f"{tm['base_with_time']}, по часам {tm['by_hour_msk']}, "
        f"по дням недели {tm['by_weekday']}.")
    if tm["peak_hours"]:
        peaks = ", ".join(f"{p['hour']}:00 ({p['count']})" for p in tm["peak_hours"])
        lines.append(f"  Пик часов: {peaks}.")
    if tm["peak_weekdays"]:
        peaks = ", ".join(f"{p['name']} ({p['count']})" for p in tm["peak_weekdays"])
        lines.append(f"  Пик дней: {peaks}.")

    dur = res["duration"]
    lines.append(
        f"Длительность: шортсы: n={dur['short']['n']}, медиана "
        f"{_fmt_seconds(dur['short']['median_seconds'])}; полные: n={dur['long']['n']}, "
        f"медиана {_fmt_seconds(dur['long']['median_seconds'])}, среднее "
        f"{_fmt_seconds(dur['long']['avg_seconds'])}.")
    for b in dur["long_buckets"]:
        lines.append(
            f"  Полные {b['label']}: n={b['n']}, медиана просмотров "
            f"{_fmt_num(b['median_views'])}.")

    if res["top_words"]:
        words = ", ".join(f"{x['word']} ({x['count']})" for x in res["top_words"])
        lines.append(
            f"Топ слов заголовков выбросов (частоты ВНУТРИ выбросов, "
            f"не отличие от фона): {words}.")
    else:
        lines.append("Топ слов заголовков выбросов: нет данных.")

    disc = res.get("discriminative_words") or []
    if disc:
        lines.append("Отличающие слова (выбросы против фона):")
        lines.extend(f"  {_discriminative_line(x)}" for x in disc)
    else:
        lines.append("Отличающие слова (выбросы против фона): нет данных.")

    if res["sample"]:
        lines.append(f"Примеры выбросов (до 20, {len(res['sample'])}):")
        for s in res["sample"]:
            lines.append(
                f"  {s['video_id']} [{s['format']}] {s['published_date'] or '?'} "
                f"views={_fmt_num(s['views'], 0)} outlier="
                f"{_fmt_num(s['outlier_score'])} упаковка={_fmt_num(s['pack_score'])} "
                f"— {s['title']}")
    else:
        lines.append("Примеры выбросов: нет.")

    lines.extend(_patterns_channel_lines(res))

    lines.append("Недоступно (по чужим видео не выводится):")
    for note in res["not_available"]:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def format_brief(res: dict[str, Any]) -> str:
    """Бриф по-русски: компактно, с числами и n."""
    f = res["filters"]
    scope = []
    if f["topic"]:
        scope.append(f"тема «{f['topic']}»")
    if f["channel_id"]:
        scope.append(f"канал {f['channel_id']}")
    if f["format"]:
        scope.append(f"формат {f['format']}")
    if f["lang"]:
        scope.append(f"язык {f['lang']}")
    scope_txt = ", ".join(scope) if scope else "все залетевшие"
    lines: list[str] = [
        f"SEO-бриф: образцы — залетевшие с выбросом ≥ {f['min_outlier']}x "
        f"({scope_txt})."
    ]
    if res.get("low_data"):
        lines.append(
            f"мало данных (n={res['channel_data_size']}), выводы слабые."
        )
    if not res["enough"]:
        lines.append(
            f"Образцов найдено {res['sample_size']} — этого мало "
            f"(нужно минимум {MIN_BRIEF_SAMPLE})."
        )
        lines.append(
            "Рекомендаций не даю: на такой выборке статистики нет, "
            "любой шаблон был бы выдумкой."
        )
        lines.append("Недоступно (по чужим видео не выводится): "
                     + "; ".join(n.split(" — ")[0] for n in res["not_available"][:3])
                     + ".")
        return "\n".join(lines)

    t = res["title"]
    d = res["description"]
    g = res["tags"]
    dur = res["duration"]
    tm = res["timing"]
    if res.get("sample_own_channel") is not None:
        lines.append(
            f"Образцов: {res['sample_size']} "
            f"(свой канал {res['sample_own_channel']} + "
            f"{res['sample_by_topic']} по темам канала)."
        )
    else:
        lines.append(f"Образцов: {res['sample_size']}.")
    lines.append(
        f"Заголовок: медиана {_fmt_num(t['median_length'])} знаков, "
        f"с цифрой {_fmt_pct(t['share_number'])}, "
        f"с вопросом {_fmt_pct(t['share_question'])}, "
        f"с личным результатом {_fmt_pct(t['share_personal'])} (n={t['n']})."
    )
    if t["top_words"]:
        words = ", ".join(f"{w['word']} ({w['count']})" for w in t["top_words"])
        lines.append(f"  Топ слов: {words}.")
    if t["templates"]:
        lines.append("  Шаблоны-заготовки (слоты в {…}):")
        for tmpl in t["templates"]:
            lines.append(f"    - {tmpl}")
    lines.append(
        f"Описание: есть у {_fmt_pct(d['share_with_description'])} (n={d['n']}), "
        f"средняя длина {_fmt_num(d['avg_length'])} знаков, "
        f"таймкоды {_fmt_pct(d['share_timestamps'])}, "
        f"хэштеги {_fmt_pct(d['share_hashtags'])}, "
        f"в среднем {_fmt_num(d['avg_hashtags'])} хэштегов "
        f"(база с описанием {d['base_with_description']})."
    )
    for row in d["structure"]:
        lines.append(f"  - {row['part']}: {row['hint']}")
    if g["top_tags"]:
        tags_txt = ", ".join(f"{x['tag']} ({x['count']})" for x in g["top_tags"])
        lines.append(
            f"Теги (n={g['base_with_tags']}): {tags_txt}."
        )
    else:
        lines.append(f"Теги: нет данных (n={g['base_with_tags']}).")
    if g["keyword"]:
        lines.append(
            f"  Ключ «{g['keyword']}»; опечатки (перестановка букв): "
            + (", ".join(g["typo_variants"]) if g["typo_variants"] else "нет")
            + "."
        )
    lines.append(
        f"Длительность: полные медиана "
        f"{_fmt_seconds(dur['long']['median_seconds'])}, "
        f"25-й-75-й процентили "
        f"{_fmt_seconds(dur['long']['p25_seconds'])}-"
        f"{_fmt_seconds(dur['long']['p75_seconds'])} "
        f"(n={dur['long']['with_duration']}); шортсы медиана "
        f"{_fmt_seconds(dur['short']['median_seconds'])} "
        f"(n={dur['short']['with_duration']})."
    )
    if dur["best_zone"]:
        b = dur["best_zone"]
        lines.append(
            f"  Лучшая зона полных: «{b['label']}» — медиана просмотров "
            f"{_fmt_num(b['median_views'], 0)} (n={b['n']})."
        )
    if tm["peak_hours"]:
        peaks = ", ".join(f"{p['hour']}:00 ({p['count']})" for p in tm["peak_hours"])
        lines.append(f"Тайминг (МСК): пики {peaks} (n={tm['n']}).")
    if tm["peak_weekdays"]:
        days = ", ".join(f"{p['name']} ({p['count']})" for p in tm["peak_weekdays"])
        lines.append(f"  Дни недели: {days}.")
    rec = res["hashtags"]["recommended"]
    lines.append(
        "Хэштеги: " + (", ".join("#" + h for h in rec) if rec else "нет данных") + "."
    )
    lines.append("Чеклист к публикации:")
    for row in res["checklist"]:
        lines.append(f"  - {row['item']}: {row['benchmark']} — {row['evidence']}.")
    lines.append("Недоступно (по чужим видео не выводится): "
                 + "; ".join(n.split(" — ")[0] for n in res["not_available"][:3])
                 + ".")
    return "\n".join(lines)


def format_audit(res: dict[str, Any]) -> str:
    """Аудит канала по-русски: доли, проблемы, лучшие и худшие."""
    lines: list[str] = []
    if not res.get("enough"):
        lines.append(
            f"SEO-аудит канала {res['channel_id']}: видео {res['videos']}. "
            f"{res.get('reason', 'данных мало')}."
        )
        return "\n".join(lines)

    t = res["title_length"]
    d = res["description"]
    dist = res["distribution"]
    mix = res["format_mix"]
    lines.append(
        f"SEO-аудит канала {res['channel_id']}: видео {res['videos']}, "
        f"с тегами {res['with_tags']} ({_fmt_pct(res['share_with_tags'])}), "
        f"залетевших (≥ {res['min_outlier']}x) {res['outliers']}."
    )
    lines.append(
        f"Заголовки: средняя {_fmt_num(t['avg'])} знаков, "
        f"медиана {_fmt_num(t['median'])}, длиннее {TITLE_MAX_LEN} — "
        f"{_fmt_pct(t['share_over_60'])}, короче {TITLE_MIN_LEN} — "
        f"{_fmt_pct(t['share_under_30'])} (n={res['videos']})."
    )
    lines.append(
        f"Описание: есть у {_fmt_pct(d['share_with_description'])}, "
        f"таймкоды {_fmt_pct(d['share_timestamps'])}, "
        f"хэштеги {_fmt_pct(d['share_hashtags'])} "
        f"(база с описанием {d['base_with_description']})."
    )
    if dist["peak_hours"]:
        peaks = ", ".join(f"{p['hour']}:00 ({p['count']})" for p in dist["peak_hours"])
        lines.append(f"Публикации по часам (МСК): {peaks} (n={dist['n']}).")
    if dist["peak_weekdays"]:
        days = ", ".join(f"{p['name']} ({p['count']})" for p in dist["peak_weekdays"])
        lines.append(f"Публикации по дням недели: {days}.")
    lines.append(
        f"Форматы: шортсы {mix['short']['n']} (медиана просмотров "
        f"{_fmt_num(mix['short']['median_views'], 0)}), полные "
        f"{mix['long']['n']} (медиана просмотров "
        f"{_fmt_num(mix['long']['median_views'], 0)})."
    )
    lines.append(
        f"Скор упаковки: средний {_fmt_num(res['scores']['avg'])} "
        f"(шортсы {_fmt_num(res['scores']['short_avg'])}, "
        f"полные {_fmt_num(res['scores']['long_avg'])})."
    )
    if res["problems"]:
        lines.append("Проблемы (по частоте):")
        for p in res["problems"]:
            lines.append(
                f"  - {p['code']} [{p['level']}]: {p['count']} видео — {p['text']}"
            )
    else:
        lines.append("Проблем не найдено.")
    if res["best"]:
        lines.append("Лучшая упаковка:")
        for r in res["best"]:
            lines.append(
                f"  {r['video_id']} — {_fmt_num(r['score'])}/100, "
                f"views {_fmt_num(r['views'], 0)} — {r['title']}"
            )
    if res["worst"]:
        lines.append("Худшая упаковка:")
        for r in res["worst"]:
            lines.append(
                f"  {r['video_id']} — {_fmt_num(r['score'])}/100, "
                f"views {_fmt_num(r['views'], 0)} — {r['title']}"
            )
    lines.append("Недоступно (по чужим видео не выводится): "
                 + "; ".join(n.split(" — ")[0] for n in res["not_available"][:3])
                 + ".")
    return "\n".join(lines)


def format_audit_markdown(res: dict[str, Any]) -> str:
    """Отчёт аудита канала в markdown для ``docs/audits/<channel_id>.md``."""
    title = res.get("channel_title") or res["channel_id"]
    return "\n".join([
        f"# SEO-аудит канала {title} ({res['channel_id']})",
        "",
        "```",
        format_audit(res),
        "```",
        "",
    ])


def save_audit_report(res: dict[str, Any], out_dir: Any = None) -> Path:
    """Сохранить markdown-отчёт аудита канала, создав каталог при нужде."""
    directory = Path(out_dir) if out_dir is not None else AUDIT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{res['channel_id']}.md"
    path.write_text(format_audit_markdown(res), encoding="utf-8")
    return path


def format_audit_batch(batch: dict[str, Any]) -> str:
    """Сводная таблица пакетного аудита каналов (ТЗ-22).

    Колонки только из данных: канал с названием, видео в разборе, средний
    скор упаковки, топ-3 проблемы по частоте. Пропущенные каналы — в конце.
    """
    reports = batch["reports"]
    lines: list[str] = [
        f"SEO-аудит каналов: с данными {len(reports)}, "
        f"порог выброса ≥ {batch['min_outlier']}x, "
        f"отчёты в {batch['out_dir']}."
    ]
    if reports:
        lines.append("канал | видео в разборе | ср. скор упаковки | топ-3 проблемы")
        for r in reports:
            problems = ", ".join(r["top_problems"]) if r["top_problems"] else "нет"
            lines.append(
                f"{r['channel_title']} ({r['channel_id']}) | "
                f"{r['videos_in_analysis']} | "
                f"{_fmt_num(r['avg_score'])} | {problems}"
            )
    else:
        lines.append("каналов с данными не найдено")
    if batch["skipped"]:
        lines.append("пропущены (нет видео с seo_fields): "
                     + ", ".join(batch["skipped"]))
    return "\n".join(lines)
