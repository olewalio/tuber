"""Смысловая классификация Telegram-постов моделью DeepSeek (ТЗ-G).

Зачем модуль. Телеграм-ветка монорепозитория — самый релевантный источник для
Datapine Radar, но этапа ИИ-классификации у неё не было вовсе: в
``classification`` не было ни одной строки ``platform='telegram'``. Модуль
закрывает этот разрыв: решает, посвящён ли пост ИИ-тематике, и даёт короткий
русский заголовок и описание.

Особенности Telegram, из-за которых нельзя переиспользовать YouTube-классификатор
один-в-один (факты боевой базы 20.09.2026):

* у источников ``source.external_id`` ПУСТ у всех каналов, хендл лежит в
  ``source.handle``, а готовая ссылка на пост — в ``content.url``;
* постов за 7 суток — 12 316, поэтому классифицируется НЕ вся ветка, а только
  каналы ИИ-тематики (вайтлист по хендлу/названию + ручной список-надстройка);
* требование владельца: вся выдача канала — на русском, поэтому ``title_ru`` и
  ``summary_ru`` заполняются ВСЕГДА, даже для русских постов (сжатая
  переформулировка без эмодзи и без хвостов «подробнее…»).

Правила, зашитые в код и проверяемые тестами:

* батч 20 постов на один вызов, две попытки; при невалидном ответе —
  ``is_ai=NULL``, ``status='error'``, никаких выдуманных значений;
* тема — только из закрытого списка ``config.TOPICS``, иначе NULL;
* предохранители расхода: ``TG_CLASSIFY_DAILY_CAP`` (постов/сутки, дефолт 150) и
  ``TG_CLASSIFY_USD_CAP`` (долларов/сутки, дефолт 0.40); учёт — в общей
  ``classify_daily`` (``platform='telegram'``);
* кэш повторов по ``text_hash``: одинаковый текст не оплачивается дважды;
* ключ модели — только из окружения; при отсутствии — ``ALERT`` и код 0.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tuber.core import storage, timeutil
from . import config

log = logging.getLogger(__name__)

# --- параметры DeepSeek ----------------------------------------------------

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

# Батч: 20 постов на один вызов (ТЗ).
BATCH_SIZE = 20
# Попытки на батч: обычная + повтор с явным «верни только JSON».
MAX_ATTEMPTS = 2

# Цены deepseek-chat, USD за 1M токенов (как в youtube/classify.py).
PRICE_IN = 0.14
PRICE_OUT = 0.28
PRICE_CACHE_HIT = 0.0028

STAGE = "classify"
PLATFORM = "telegram"
PROMPT_VER = "tg-v1"

# Ограничения полей выдачи.
TEXT_LIMIT = 500
TITLE_LIMIT = 90
SUMMARY_LIMIT = 300
SUBTITLE_LIMIT = 120

# Предохранители расхода (перекрываются средой).
DEFAULT_DAILY_CAP = 150
DEFAULT_USD_CAP = 0.40

# Файл секретов контура (ключ DeepSeek). Значение нигде не печатается.
ENV_PATH = "/root/.hermes/.env"
# Ручной список-надстройка вайтлиста (по строке — хендл).
MANUAL_WHITELIST = os.path.join(config.DATA_DIR, "telegram_ai_channels.txt")

# --- вайтлист ИИ-каналов ---------------------------------------------------

#: Ключевые слова ИИ-тематики (ТЗ-G §1.2). Регистр не важен.
AI_KEYWORDS: tuple[str, ...] = (
    "ai", "ии", "gpt", "нейро", "вайб", "vibe", "claude", "llm", "агент",
    "agent", "deepseek", "qwen", "openai", "midjourney", "промпт", "prompt",
    "copilot", "codex", "cursor", "ml", "giga", "machinelearning",
    "data_analysis",
)

#: Символы, которые при нормализации считаются разделителями слов. Нижнее
#: подчёркивание тоже разделитель: хендлы Telegram пишутся через ``_``
#: (``ai_machinelearning_big_data``), и слово внутри хендла должно находиться.
_WORD_SPLIT_RE = re.compile(r"[^0-9a-zа-яё]+")

_KEYWORD_RE: re.Pattern[str] | None = None


def _normalize_text(value: str | None) -> str:
    """Нижний регистр + все разделители (``_``, ``-``, пробелы) → пробел."""
    text = (value or "").lower()
    return _WORD_SPLIT_RE.sub(" ", text).strip()


def _keyword_regex() -> re.Pattern[str]:
    """Собрать регулярку вайтлиста.

    Границы слов обязательны (иначе «ai» ловит «airfield»), но только их мало:
    пример ТЗ ``vibecoding_tg`` требует совпадения ``vibe`` с началом слова, а
    ``dailyprompts`` — ``prompt`` внутри слова. Поэтому:

    * ключ длиной ≤2 (``ai``, ``ml``, ``ии``) — строго слово целиком;
    * ключ длиной 3–4 (``vibe``, ``gpt``) — начало слова;
    * ключ длиной ≥5 — подстрока (``нейро``, ``prompt``, ``claude``).

    Так ``rian_ru`` и ``mash`` не попадают, а ``vibecoding_tg``,
    ``prog_ai`` и ``dailyprompts`` — попадают (тест T8).
    """
    global _KEYWORD_RE
    if _KEYWORD_RE is not None:
        return _KEYWORD_RE
    alts: list[str] = []
    for kw in AI_KEYWORDS:
        key = re.escape(_normalize_text(kw))
        if len(kw) <= 2:
            alts.append(r"(?<![0-9a-zа-яё])" + key + r"(?![0-9a-zа-яё])")
        elif len(kw) <= 4:
            alts.append(r"(?<![0-9a-zа-яё])" + key)
        else:
            alts.append(key)
    _KEYWORD_RE = re.compile("|".join(alts), re.IGNORECASE)
    return _KEYWORD_RE


def is_ai_channel(handle: str | None, title: str | None) -> bool:
    """Канал ИИ-тематики? Смотрим хендл и название."""
    hay = _normalize_text(handle) + " " + _normalize_text(title)
    return bool(_keyword_regex().search(hay))


def load_manual_whitelist(path: str = MANUAL_WHITELIST) -> set[str]:
    """Ручной список-надстройка: по строке — хендл; ``#`` — комментарий."""
    out: set[str] = set()
    p = Path(path)
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.lstrip("@").strip().lower())
    return {h for h in out if h}


def build_whitelist(conn: Any, manual_path: str = MANUAL_WHITELIST) -> set[str]:
    """Хендлы ИИ-каналов: совпавшие с регуляркой + ручной список.

    Возвращает множество хендлов в НИЖНЕМ регистре (хендлы в базе
    разнорегистровые: ``GPTMainNews`` против ``gpt_news``).
    """
    rows = conn.execute(
        "SELECT handle, title FROM source WHERE platform=?", (PLATFORM,)
    ).fetchall()
    handles = {
        str(r["handle"]).lower()
        for r in rows
        if r["handle"] and is_ai_channel(r["handle"], r["title"])
    }
    handles |= load_manual_whitelist(manual_path)
    return {h for h in handles if h}


def build_whitelist_file(conn: Any, path: str = MANUAL_WHITELIST) -> list[str]:
    """Пересобрать файл вайтлиста по текущим источникам. Вернуть хендлы."""
    handles = sorted(
        (r["handle"] for r in conn.execute(
            "SELECT handle, title FROM source WHERE platform=?", (PLATFORM,)
        ) if r["handle"] and is_ai_channel(r["handle"], r["title"])),
        key=str.lower,
    )
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Вайтлист ИИ-каналов Telegram (ТЗ-G).\n"
        "# Сгенерировано `python3 -m tuber tg classify --build-whitelist`.\n"
        "# Строки ниже — каналы, чей хендл/название совпал с ИИ-ключевыми\n"
        "# словами. Дополнительные каналы можно вписать руками: одна строка —\n"
        "# один хендл без '@', строки с '#' игнорируются.\n"
    )
    p.write_text(header + "".join(f"{h}\n" for h in handles), encoding="utf-8")
    return handles


# --- кэш повторов ----------------------------------------------------------


def text_hash_of(text: str | None) -> str | None:
    """Хэш текста — тот же алгоритм, что у X (межплатформенный кэш)."""
    if not text:
        return None
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


def cache_get(conn: Any, text_hash: str) -> Mapping[str, Any] | None:
    """Готовая запись кэша, если текст уже разобран."""
    if not text_hash:
        return None
    return conn.execute(
        "SELECT * FROM classify_cache WHERE text_hash=? AND status='classified'"
        " AND is_ai IS NOT NULL",
        (text_hash,),
    ).fetchone()


# --- отбор постов ----------------------------------------------------------


def _handle_placeholders(handles: Sequence[str]) -> tuple[str, list[str]]:
    return ", ".join("?" for _ in handles), list(handles)


def select_pending(conn: Any, handles: Sequence[str], limit: int | None = None,
                   only: str | None = None) -> list[Mapping[str, Any]]:
    """Неразобранные посты ИИ-каналов, свежие первыми.

    Исключаются посты с успешной строкой ``classification``; строки со статусом
    ``error`` попадают в повтор. Если задан ``only`` — только этот хендл (в
    пределах вайтлиста).
    """
    hs = sorted({h.lower() for h in handles if h})
    if only is not None:
        want = only.lstrip("@").lower()
        hs = [h for h in hs if h == want]
    if not hs:
        return []
    ph, params = _handle_placeholders(hs)
    sql = f"""
        SELECT c.id AS content_id, c.external_id, c.url, c.text, c.text_hash,
               c.lang AS content_lang, c.published_at, c.title AS content_title,
               s.handle AS handle, s.title AS channel_title
        FROM content c
        JOIN source s ON s.id = c.source_id
        LEFT JOIN classification cl ON cl.content_id = c.id
        WHERE c.platform = ?
          AND lower(s.handle) IN ({ph})
          AND c.text IS NOT NULL AND length(trim(c.text)) > 0
          AND (cl.content_id IS NULL OR cl.is_ai IS NULL)
        ORDER BY c.published_at DESC, c.id DESC
    """
    args = [PLATFORM, *params]
    rows = list(conn.execute(sql, args))
    if limit is not None:
        rows = rows[: int(limit)]
    return rows


# --- промпты ---------------------------------------------------------------

SYSTEM_PROMPT = """Ты классифицируешь Telegram-посты для аналитики ИИ-трендов.
Отвечай строго JSON-массивом, без пояснений и без markdown.

Что считается ИИ-тематикой (is_ai=true): сам предмет поста — искусственный
интеллект, ИИ-инструменты и сервисы, ИИ-индустрия, модели и их релизы, чипы и
железо для ИИ, дата-центры и энергия под ИИ, роботы и физический ИИ, ИИ-кодинг
и вайб-кодинг, сделки и деньги в ИИ, регулирование ИИ, ИИ в науке и медицине.

Что НЕ считается (is_ai=false): упоминание ИИ вскользь; общие IT-новости без
ИИ-сути; политика, происшествия, реклама и розыгрыши, посты про гаджеты и
подписки без ИИ. При сомнении ставь is_ai=false и понижай confidence.

Разграничение смежных тем (при нескольких подходящих выбирай самую узкую):
- «запуски и анонсы» — анонс/выход продукта, сервиса, новой функции. В
  «модели и релизы» — только сами модели и их версии.
- «агенты и автоматизация» — ИИ-агенты, автоматизация задач и пайплайнов.
- «кодинг и разработка» — ИИ-кодинг, IDE, SDK, инструменты разработчика.
- «деньги и сделки» — капитал и рынок; «стартапы и бизнес» — продукт, раунды,
  внедрение ИИ в компанию.
- «дизайн и креатив» — картинки, видео, 3D, промпты для картинок. В «медиа и
  творчество» — музыка, кино, тексты, голос.
- «обучение и навыки» — уроки, курсы, промпт-инженерия как навык.
Если ни одна тема не подходит, topic = null и is_ai=false.

Для каждого поста верни объект:
{"content_id": <как во входе>, "is_ai": true|false, "topic": "..."|null,
 "subtopic": "..."|null, "lang": "ru|en|...", "confidence": 0.0-1.0,
 "title_ru": "...", "summary_ru": "...", "reason": "..."}

topic — ТОЛЬКО из списка: __TOPICS__.
title_ru — короткий русский заголовок, до 90 знаков, без эмодзи.
summary_ru — 1-2 русских предложения, до 300 знаков. ТОЛЬКО факты из текста
поста, без выдуманных цифр.
ВАЖНО: title_ru и summary_ru заполняются ВСЕГДА, даже если пост уже на русском
(для русских — сжатая переформулировка без эмодзи и без хвостов «подробнее…»).
lang — язык исходного поста.
reason — коротко, почему решено, что это ИИ или не ИИ.
""".replace("__TOPICS__", ", ".join(config.TOPICS))


def build_user_prompt(rows: Sequence[Mapping[str, Any]]) -> str:
    """Батч постов компактным JSON."""
    items = []
    for row in rows:
        text = (row["text"] or "")[:TEXT_LIMIT]
        items.append({
            "content_id": row["content_id"],
            "channel": row["handle"],
            "channel_title": row["channel_title"],
            "date": row["published_at"],
            "text": text,
        })
    return (
        "Разбери эти посты. Верни JSON-массив с объектом на каждый пост в том же"
        " порядке.\n" + json.dumps(items, ensure_ascii=False)
    )


RETRY_INSTRUCTION = (
    "Предыдущий ответ не был валидным JSON. Верни ТОЛЬКО JSON-массив объектов,"
    " без markdown-обёртки и без пояснений."
)


# --- разбор ответа ---------------------------------------------------------


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def extract_json_array(text: str) -> list | None:
    """Первый валидный JSON-массив в тексте, иначе None."""
    if not text:
        return None
    cleaned = _strip_fences(text)
    try:
        loaded = json.loads(cleaned)
        if isinstance(loaded, list):
            return loaded
    except (ValueError, TypeError):
        pass
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        pos = cleaned.find("[", idx)
        if pos == -1:
            return None
        try:
            obj, _end = decoder.raw_decode(cleaned[pos:])
        except ValueError:
            idx = pos + 1
            continue
        if isinstance(obj, list):
            return obj
        idx = pos + 1


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "да"):
            return True
        if low in ("false", "0", "no", "нет"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, num))


def _as_text(value: Any, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if limit is not None:
        text = text[:limit]
    return text


def _normalize_lang(value: Any) -> str | None:
    text = _as_text(value)
    if text is None:
        return None
    token = re.split(r"[\s,;|/_-]", text.lower())[0]
    return token[:8] or None


def normalize_item(item: Mapping[str, Any], cfg: Any = config) -> dict[str, Any] | None:
    """Объект модели → поля БД. Нет content_id — None."""
    cid = item.get("content_id")
    if cid is None:
        cid = item.get("id")
    if cid is None:
        return None
    topic = _as_text(item.get("topic"))
    if topic is not None and not cfg.is_valid_topic(topic):
        topic = None
    is_ai = _as_bool(item.get("is_ai"))
    return {
        "content_id": cid,
        "is_ai": None if is_ai is None else int(is_ai),
        "topic": topic,
        "subtopic": _as_text(item.get("subtopic"), SUBTITLE_LIMIT),
        "confidence": _as_float(item.get("confidence")),
        "title_ru": _as_text(item.get("title_ru"), TITLE_LIMIT),
        "summary_ru": _as_text(item.get("summary_ru"), SUMMARY_LIMIT),
        "lang": _normalize_lang(item.get("lang")),
        "reason": _as_text(item.get("reason")),
    }


def parse_response(text: str, expected_ids: Sequence[Any] | None = None,
                   cfg: Any = config) -> dict[Any, dict[str, Any]] | None:
    """{content_id: поля} или None. Посторонние id отбрасываются."""
    arr = extract_json_array(text)
    if arr is None:
        return None
    allowed = {str(i) for i in expected_ids} if expected_ids is not None else None
    out: dict[Any, dict[str, Any]] = {}
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        norm = normalize_item(item, cfg)
        if norm is None:
            continue
        if allowed is not None and str(norm["content_id"]) not in allowed:
            continue
        out[str(norm["content_id"])] = norm
    return out


# --- сеть ------------------------------------------------------------------


def _chat(api_key: str, messages: list[dict], session: Any, model: str,
          timeout: float = 60.0) -> tuple[str, dict]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    resp = session.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=timeout)
    if hasattr(resp, "raise_for_status"):
        resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    text = ""
    if choices:
        text = (choices[0].get("message") or {}).get("content") or ""
    return text, (data.get("usage") or {})


def _cost_usd(usage: Mapping[str, Any]) -> float:
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    billed_in = max(tokens_in - cache_hit, 0)
    return (
        billed_in * PRICE_IN / 1_000_000
        + cache_hit * PRICE_CACHE_HIT / 1_000_000
        + tokens_out * PRICE_OUT / 1_000_000
    )


def _usage_counts(usage: Mapping[str, Any]) -> tuple[int, int]:
    return (
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )


def load_env(path: str = ENV_PATH) -> int:
    """Подхватить переменные из приватного .env, не перетирая окружение."""
    src = Path(path)
    if not src.exists():
        return 0
    count = 0
    for raw in src.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def resolve_api_key(api_key: str | None = None) -> str:
    """Ключ DeepSeek: аргумент → окружение → /root/.hermes/.env."""
    if api_key:
        return api_key
    if not os.environ.get("DEEPSEEK_API_KEY"):
        load_env()
    return os.environ.get("DEEPSEEK_API_KEY", "")


# --- предохранители --------------------------------------------------------


def daily_cap() -> int:
    try:
        return int(os.environ.get("TG_CLASSIFY_DAILY_CAP", DEFAULT_DAILY_CAP))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def usd_cap() -> float:
    try:
        return float(os.environ.get("TG_CLASSIFY_USD_CAP", DEFAULT_USD_CAP))
    except (TypeError, ValueError):
        return DEFAULT_USD_CAP


def _daily_row(conn: Any, day: str) -> Mapping[str, Any] | None:
    return conn.execute(
        "SELECT * FROM classify_daily WHERE day=? AND platform=?", (day, PLATFORM)
    ).fetchone()


def daily_used(conn: Any, day: str) -> tuple[int, float]:
    """(постов за сутки, долларов за сутки) по Telegram-классификации."""
    row = _daily_row(conn, day)
    if not row:
        return 0, 0.0
    return int(row["posts"] or 0), float(row["cost_usd"] or 0.0)


def _bump_daily(conn: Any, day: str, *, posts: int = 0, model_calls: int = 0,
                failed: int = 0, prompt_tokens: int = 0,
                completion_tokens: int = 0, cost_usd: float = 0.0) -> None:
    """Прибавить расход к суточной строке (суммирование, не перезапись)."""
    row = _daily_row(conn, day)
    storage.upsert_classify_daily(
        conn, day, PLATFORM,
        posts=int(row["posts"] or 0) + posts if row else posts,
        model_calls=int(row["model_calls"] or 0) + model_calls if row else model_calls,
        failed=int(row["failed"] or 0) + failed if row else failed,
        prompt_tokens=(int(row["prompt_tokens"] or 0) + prompt_tokens) if row else prompt_tokens,
        completion_tokens=(int(row["completion_tokens"] or 0) + completion_tokens) if row else completion_tokens,
        cost_usd=round((float(row["cost_usd"] or 0.0) if row else 0.0) + cost_usd, 8),
    )


def _cap_message(conn: Any, day: str) -> str:
    posts, usd = daily_used(conn, day)
    return (f"TG-CAP: достигнут суточный потолок (постов {posts}, "
            f"${usd:.6f}) — остальное в следующий прогон")


# --- запись ----------------------------------------------------------------


def _classification_fields(item: Mapping[str, Any], model: str, *,
                           status: str, attempts: int, usage: Mapping[str, Any],
                           classified_at: str, method: str = "deepseek",
                           reason: str | None = None) -> dict[str, Any]:
    return {
        "is_ai": item.get("is_ai"),
        "topic": item.get("topic"),
        "subtopic": item.get("subtopic"),
        "lang": item.get("lang"),
        "confidence": item.get("confidence"),
        "method": method,
        "model": model,
        "prompt_ver": PROMPT_VER,
        "status": status,
        "attempts": int(attempts),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cost_usd": float(usage.get("cost_usd") or 0.0),
        "classified_at": classified_at,
        "title_ru": item.get("title_ru"),
        "summary_ru": item.get("summary_ru"),
        "reason": reason if reason is not None else item.get("reason"),
    }


def _write_classification(conn: Any, content_id: Any, external_id: Any, fields: dict) -> None:
    storage.set_classification(
        conn, content_id, platform=PLATFORM, external_id=external_id, **fields
    )


def _write_cache(conn: Any, text_hash: str, fields: dict, *, content_id: Any,
                 classified_at: str) -> None:
    storage.set_classify_cache(
        conn, text_hash,
        is_ai=fields.get("is_ai"), topic=fields.get("topic"),
        subtopic=fields.get("subtopic"), lang=fields.get("lang"),
        title_ru=fields.get("title_ru"), summary_ru=fields.get("summary_ru"),
        method=fields.get("method"), model=fields.get("model"),
        status="classified" if fields.get("is_ai") is not None else "error",
        attempts=fields.get("attempts"),
        error=None if fields.get("is_ai") is not None else fields.get("reason"),
        prompt_tokens=fields.get("prompt_tokens"),
        completion_tokens=fields.get("completion_tokens"),
        cost_usd=fields.get("cost_usd"),
        classified_at=classified_at, first_seen_at=classified_at,
        meta_json=json.dumps({"platform": PLATFORM, "content_id": content_id}),
    )


# --- основной проход -------------------------------------------------------


def _chunks(items: Sequence[Any], size: int):
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def classify(conn: Any, *, limit: int | None = None, dry_run: bool = False,
             only: str | None = None, session: Any = None,
             api_key: str | None = None, model: str | None = None,
             now: datetime | None = None,
             manual_path: str = MANUAL_WHITELIST) -> dict[str, Any]:
    """Разобрать неразобранные посты ИИ-каналов Telegram.

    Идемпотентен по кэшу ``text_hash``. ``dry_run`` — только показать объём, без
    обращений к модели и без записи. Возвращает сводку прогона.
    """
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    use_model = model or DEFAULT_MODEL

    handles = build_whitelist(conn, manual_path=manual_path)
    pending = select_pending(conn, handles, limit=limit, only=only)

    summary: dict[str, Any] = {
        "whitelist_channels": len(handles),
        "whitelist_posts": len(pending),
        "selected": len(pending),
        "classified": 0, "ai": 0, "not_ai": 0, "failed": 0, "cached": 0,
        "batches": 0, "model_calls": 0,
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
        "cap_posts": daily_cap(), "cap_usd": usd_cap(),
        "daily_posts_used": 0, "daily_usd_used": 0.0,
        "capped": False, "no_key": False, "dry_run": dry_run,
        "model": use_model, "errors": [],
    }

    used_posts, used_usd = daily_used(conn, day)
    summary["daily_posts_used"] = used_posts
    summary["daily_usd_used"] = used_usd

    if dry_run:
        print(f"TG-DRY-RUN: вайтлист {len(handles)} каналов, "
              f"к разбору {len(pending)} постов "
              f"(суточный потолок {summary['cap_posts']} постов, "
              f"${summary['cap_usd']:.2f}; израсходовано {used_posts} постов, "
              f"${used_usd:.6f})")
        return summary

    key = resolve_api_key(api_key)
    if not key:
        print("ALERT: нет ключа DeepSeek — классификация Telegram пропущена")
        summary["no_key"] = True
        return summary

    if session is None:
        import requests
        session = requests.Session()

    left_posts = daily_cap() - used_posts
    if left_posts <= 0:
        print(_cap_message(conn, day))
        summary["capped"] = True
        return summary
    if usd_cap() > 0 and used_usd >= usd_cap():
        print(_cap_message(conn, day))
        summary["capped"] = True
        return summary

    if not pending:
        print(f"TG: вайтлист {len(handles)} каналов, неразобранных постов нет")
        return summary

    print(f"TG: вайтлист {len(handles)} каналов, к разбору {len(pending)} постов")

    # Группировка по text_hash: одинаковый текст к модели один раз (кэш).
    by_hash: dict[str, list[Mapping[str, Any]]] = {}
    for row in pending:
        th = row["text_hash"] or text_hash_of(row["text"])
        by_hash.setdefault(th or f"__row{row['content_id']}", []).append(row)

    # Копия из кэша — без вызова модели.
    to_model: list[Mapping[str, Any]] = []
    for th, group in by_hash.items():
        cached = cache_get(conn, th)
        if cached is not None:
            item = {
                "is_ai": cached["is_ai"], "topic": cached["topic"],
                "subtopic": cached["subtopic"], "lang": cached["lang"],
                "confidence": None, "title_ru": cached["title_ru"],
                "summary_ru": cached["summary_ru"], "reason": "cache",
            }
            for row in group:
                fields = _classification_fields(
                    item, use_model, status="classified", attempts=1,
                    usage={"cost_usd": 0.0}, classified_at=timeutil.iso_now())
                _write_classification(conn, row["content_id"], row["external_id"], fields)
                summary["classified"] += 1
                summary["cached"] += 1
                if item["is_ai"] == 1:
                    summary["ai"] += 1
                elif item["is_ai"] == 0:
                    summary["not_ai"] += 1
        else:
            to_model.append(group[0])

    if summary["cached"]:
        _bump_daily(conn, day, posts=summary["cached"])
        conn.commit()

    # Прогон батчами. Постов за прогон не больше остатка суточного потолка.
    room = left_posts
    for batch in _chunks(to_model, BATCH_SIZE):
        # Потолок постов: если за сутки уже набрано — аккуратно останавливаемся.
        posts_now, usd_now = daily_used(conn, day)
        if posts_now >= daily_cap():
            summary["capped"] = True
            print(_cap_message(conn, day))
            break
        if usd_cap() > 0 and usd_now >= usd_cap():
            summary["capped"] = True
            print(_cap_message(conn, day))
            break
        room_now = max(min(len(batch), room), 0)
        if room_now <= 0:
            summary["capped"] = True
            print(_cap_message(conn, day))
            break
        batch = batch[:room_now]
        room -= room_now

        summary["batches"] += 1
        ids = [str(r["content_id"]) for r in batch]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(batch)},
        ]

        parsed: dict[str, dict[str, Any]] | None = None
        attempts = 0
        batch_tin = batch_tout = 0
        batch_cost = 0.0
        for attempt in range(MAX_ATTEMPTS):
            attempts += 1
            try:
                text, usage = _chat(key, messages, session, use_model)
            except Exception as exc:  # noqa: BLE001 — канал/сеть могут отказать
                summary["errors"].append(f"api: {exc}")
                log.warning("DeepSeek вызов не удался: %s", exc)
                break
            tin, tout = _usage_counts(usage)
            cost = _cost_usd(usage)
            batch_tin += tin
            batch_tout += tout
            batch_cost += cost
            summary["model_calls"] += 1
            summary["tokens_in"] += tin
            summary["tokens_out"] += tout
            summary["cost_usd"] += cost
            _bump_daily(conn, day, model_calls=1, prompt_tokens=tin,
                        completion_tokens=tout, cost_usd=cost)
            conn.commit()
            parsed = parse_response(text, ids)
            if parsed is not None:
                break
            if attempt + 1 < MAX_ATTEMPTS:
                messages = messages + [
                    {"role": "assistant", "content": (text or "")[:2000]},
                    {"role": "user", "content": RETRY_INSTRUCTION},
                ]

        # Доля вызова на пост: одна цена батча делится между постами, чтобы
        # сумма по classification не превышала реальный расход (ТЗ-G §1.1).
        share_usage = {
            "prompt_tokens": batch_tin // len(batch),
            "completion_tokens": batch_tout // len(batch),
            "cost_usd": round(batch_cost / len(batch), 8),
        }

        if parsed is None:
            # Невалидный ответ: две попытки исчерпаны — честные NULL + error.
            for row in batch:
                fields = _classification_fields(
                    {"is_ai": None, "topic": None, "subtopic": None, "lang": None,
                     "confidence": None, "title_ru": None, "summary_ru": None,
                     "reason": "invalid_json"},
                    use_model, status="error", attempts=attempts,
                    usage=share_usage, classified_at=timeutil.iso_now())
                _write_classification(conn, row["content_id"], row["external_id"], fields)
                summary["failed"] += 1
            _bump_daily(conn, day, posts=len(batch), failed=len(batch))
            conn.commit()
            continue

        classified_at = timeutil.iso_now()
        for row in batch:
            item = parsed.get(str(row["content_id"]))
            if item is None:
                fields = _classification_fields(
                    {"is_ai": None, "topic": None, "subtopic": None, "lang": None,
                     "confidence": None, "title_ru": None, "summary_ru": None,
                     "reason": "missing_in_response"},
                    use_model, status="error", attempts=attempts,
                    usage=share_usage, classified_at=classified_at)
                _write_classification(conn, row["content_id"], row["external_id"], fields)
                summary["failed"] += 1
                continue
            fields = _classification_fields(
                item, use_model, status="classified", attempts=attempts,
                usage=share_usage, classified_at=classified_at)
            _write_classification(conn, row["content_id"], row["external_id"], fields)
            th = row["text_hash"] or text_hash_of(row["text"])
            if th:
                _write_cache(conn, th, fields, content_id=row["content_id"],
                             classified_at=classified_at)
            summary["classified"] += 1
            if item["is_ai"] == 1:
                summary["ai"] += 1
            elif item["is_ai"] == 0:
                summary["not_ai"] += 1
        _bump_daily(conn, day, posts=len(batch))
        conn.commit()

        # Денежный предохранитель проверяем после каждого батча.
        _, usd_after = daily_used(conn, day)
        if usd_cap() > 0 and usd_after >= usd_cap():
            if room > 0:
                summary["capped"] = True
                print(_cap_message(conn, day))
            break

    summary["cost_usd"] = round(summary["cost_usd"], 6)
    posts_now, usd_now = daily_used(conn, day)
    summary["daily_posts_used"] = posts_now
    summary["daily_usd_used"] = usd_now
    return summary


# --- CLI -------------------------------------------------------------------


def _format_summary(summary: Mapping[str, Any]) -> str:
    if summary.get("no_key"):
        return "нет ключа DeepSeek — прогон пропущен"
    if summary.get("dry_run"):
        return (f"тестовый режим: вайтлист {summary['whitelist_channels']} "
                f"каналов, к разбору {summary['whitelist_posts']} постов")
    return (
        f"разбор: к разбору {summary['selected']}, разобрано "
        f"{summary['classified']} (из них из кэша {summary['cached']}), "
        f"ИИ {summary['ai']}, не ИИ {summary['not_ai']}, "
        f"сбоев {summary['failed']}, вызовов модели {summary['model_calls']}, "
        f"стоимость ${summary['cost_usd']}, "
        f"за сутки {summary['daily_posts_used']} постов "
        f"(${summary['daily_usd_used']:.6f})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m tuber tg classify [--limit N] [--dry-run] [--only H]``.

    ``--build-whitelist`` пересобирает ``data/telegram_ai_channels.txt``.
    """
    import argparse

    from . import store as db

    parser = argparse.ArgumentParser(
        prog="tuber tg classify",
        description="смысловая классификация Telegram-постов ИИ-каналов (ТЗ-G)")
    parser.add_argument("--limit", type=int, default=None,
                        help="максимум постов за прогон")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать объём, без модели и без записи")
    parser.add_argument("--only", default=None,
                        help="ограничить одним хендлом (для отладки)")
    parser.add_argument("--build-whitelist", action="store_true",
                        help="пересобрать data/telegram_ai_channels.txt и выйти")
    args = parser.parse_args(list(argv) if argv is not None else None)

    conn = db.connect(config.resolve_db())
    try:
        if args.build_whitelist:
            handles = build_whitelist_file(conn)
            print(f"вайтлист пересобран: {len(handles)} каналов -> "
                  f"{MANUAL_WHITELIST}")
            return 0
        if args.dry_run:
            classify(conn, limit=args.limit, dry_run=True, only=args.only)
            return 0
        summary = classify(conn, limit=args.limit, only=args.only)
        print(_format_summary(summary))
        for err in summary.get("errors", []):
            print(f"  ошибка: {err}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
