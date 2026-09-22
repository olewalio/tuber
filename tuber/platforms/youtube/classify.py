"""Смысловой разбор видео (этап 5).

Задача модуля — по заголовку, каналу, описанию и длительности решить,
посвящено ли видео ИИ-тематике, и дать короткое русское описание.

Ключевые правила (METHODOLOGY.md, раздел про темы):
- «ИИ-тематика» — это предмет видео, а не вскользь упомянутый ИИ;
- тема выбирается только из config.TOPICS, иначе NULL и is_ai=false;
- один вызов модели — один батч до 20 видео;
- при невалидном ответе — повтор, после двух неудач is_ai=NULL (не выдумываем).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Mapping, Sequence

from . import config, store as db

log = logging.getLogger(__name__)

# --- параметры DeepSeek ----------------------------------------------------

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

# Батч: 20 видео на один вызов (ТЗ).
BATCH_SIZE = 20
# Максимум попыток на батч (1 обычная + 1 с явным «верни только JSON»).
MAX_ATTEMPTS = 2

# Цены deepseek-chat, USD за 1M токенов.
PRICE_IN = 0.14
PRICE_OUT = 0.28
PRICE_CACHE_HIT = 0.0028

STAGE = "classify"

# Сколько знаков описания отдавать модели.
DESCRIPTION_LIMIT = 500

# --- промпты ---------------------------------------------------------------

SYSTEM_PROMPT = """Ты классифицируешь YouTube-видео для аналитики ИИ-трендов.
Отвечай строго JSON-массивом, без пояснений и без markdown.

Что считается ИИ-тематикой (is_ai=true): сам предмет видео — искусственный
интеллект, ИИ-инструменты, ИИ-индустрия, чипы и железо для ИИ, дата-центры и
энергия под ИИ, роботы и физический ИИ, ИИ-кодинг, сделки и деньги в ИИ,
регулирование ИИ, ИИ в науке и медицине.

Что НЕ считается (is_ai=false): упоминание ИИ вскользь (например, новость про
акции, где ИИ упомянут один раз); общие IT-курсы (DevOps, AWS-сертификация);
новости политики и происшествий; финансовые сводки без ИИ-сути. При сомнении
ставь is_ai=false и понижай confidence.

Разграничение смежных тем (при нескольких подходящих выбирай самую узкую):
- «запуски и анонсы» — анонс или выход продукта, сервиса, приложения, новой
функции, демонстрация только что вышедшего инструмента. В «модели и релизы»
идут только сами модели и их версии (веса, бенчмарки, архитектура).
- «стартапы и бизнес» — основатели, раунды, юнит-экономика, внедрение ИИ в
компанию, построение продукта, маркетинг. В «деньги и сделки» — только
капитал и рынок: инвестиции в чипы и дата-центры, котировки, отчёты корпораций.
- «дизайн и креатив» — визуал и креативные инструменты: графика, 3D, анимация,
монтаж, генерация изображений, промпты для картинок. В «медиа и творчество» —
музыка, кино, тексты, голос, озвучка.
- «влоги и личный опыт» — личный формат: «я сделал», «мой опыт», «день из
жизни», обзор своей сборки или настройки без новостного повода.
- «обучение и навыки» — уроки, курсы, разборы «с нуля», обучение профессии,
промпт-инженерия как навык.
Если видео подходит под несколько тем — выбрать самую узкую. Если ни одна не
подходит, topic = null (правило сохраняется). Решение принимается по смыслу,
а не по ключевым словам.

Для каждого видео верни объект:
{"id": "...", "is_ai": true|false, "topic": "..."|null,
 "confidence": 0.0-1.0, "title_ru": "...", "summary_ru": "...",
 "lang": "ru|en|...", "reason": "..."}

topic — ТОЛЬКО из списка: __TOPICS__. Если ни одна тема не подходит, topic=null
и is_ai=false.
title_ru — короткое описание ролика по-русски, 8-16 слов.
summary_ru — 1-2 предложения по-русски, «что за видео».
lang — язык самого видео (не перевода).
reason — коротко, почему решено, что это ИИ или не ИИ.
""".replace("__TOPICS__", ", ".join(config.TOPICS))


def build_user_prompt(rows: Sequence[Mapping[str, Any]]) -> str:
    """Собрать входной батч как компактный JSON."""
    items = []
    for row in rows:
        desc = row["description"] if "description" in row.keys() else None
        desc = (desc or "")[:DESCRIPTION_LIMIT]
        items.append({
            "id": row["video_id"],
            "title": row["title"],
            "channel": row["channel_title"],
            "description": desc,
            "duration_seconds": row["duration_seconds"],
        })
    return (
        "Разбери эти видео. Верни JSON-массив с объектом на каждое видео "
        "в том же порядке.\n" + json.dumps(items, ensure_ascii=False)
    )


RETRY_INSTRUCTION = (
    "Предыдущий ответ не был валидным JSON. Верни ТОЛЬКО JSON-массив "
    "объектов, без markdown-обёртки и без пояснений."
)


# --- разбор ответа ---------------------------------------------------------


def _strip_fences(text: str) -> str:
    """Убрать markdown-обёртку ```json ... ```."""
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def extract_json_array(text: str) -> list | None:
    """Найти первый валидный JSON-массив в тексте. Иначе None."""
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
    """Привести значение модели к bool. Непонятное — None."""
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
    """Confidence: float 0..1, иначе None."""
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 0.0:
        return 0.0
    if num > 1.0:
        return 1.0
    return num


def _as_text(value: Any, limit: int | None = None) -> str | None:
    """Строка или None; длинные обрезаем."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if limit is not None:
        text = text[:limit]
    return text


def _normalize_lang(value: Any) -> str | None:
    """Язык: короткий код в нижнем регистре ('ru', 'en', ...)."""
    text = _as_text(value)
    if text is None:
        return None
    token = re.split(r"[\s,;|/_-]", text.lower())[0]
    return token[:8] or None


def normalize_item(item: Mapping[str, Any], cfg: Any = config) -> dict[str, Any] | None:
    """Привести объект модели к полям БД. Нет id — None."""
    video_id = item.get("id")
    if not video_id:
        return None
    topic = _as_text(item.get("topic"))
    # Тема только из закрытого списка, иначе отбрасывается.
    if topic is not None and not cfg.is_valid_topic(topic):
        topic = None
    return {
        "video_id": str(video_id),
        "is_ai": None if _as_bool(item.get("is_ai")) is None else int(_as_bool(item.get("is_ai"))),
        "topic": topic,
        "confidence": _as_float(item.get("confidence")),
        "title_ru": _as_text(item.get("title_ru")),
        "summary_ru": _as_text(item.get("summary_ru")),
        "lang": _normalize_lang(item.get("lang")),
        "reason": _as_text(item.get("reason")),
    }


def parse_response(text: str, expected_ids: Sequence[str] | None = None,
                   cfg: Any = config) -> dict[str, dict[str, Any]] | None:
    """Разобрать ответ модели в словарь {video_id: поля}.

    None — если валидного массива нет. Объекты без id отбрасываются,
    id вне батча игнорируются (защита от выдуманных роликов).
    """
    arr = extract_json_array(text)
    if arr is None:
        return None
    allowed = set(expected_ids) if expected_ids is not None else None
    out: dict[str, dict[str, Any]] = {}
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        norm = normalize_item(item, cfg)
        if norm is None:
            continue
        if allowed is not None and norm["video_id"] not in allowed:
            continue
        out[norm["video_id"]] = norm
    return out


# --- сеть ------------------------------------------------------------------


def _chat(api_key: str, messages: list[dict], session: Any, model: str,
          timeout: float = 60.0) -> tuple[str, dict]:
    """Один вызов DeepSeek. Возвращает (текст, usage)."""
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
    usage = data.get("usage") or {}
    return text, usage


def _cost_usd(usage: Mapping[str, Any]) -> float:
    """Стоимость вызова по тарифам deepseek-chat."""
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


# --- основной проход -------------------------------------------------------


def _chunks(items: Sequence[Any], size: int):
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def _run_model_batches(conn: Any, rows: Sequence[Any], key: str, session: Any,
                       use_model: str, cfg: Any,
                       save_failures: bool = True) -> dict[str, Any]:
    """Прогнать видео батчами через DeepSeek и записать разбор.

    Общий проход для разбора новых видео и переразбора. Ошибка одного батча
    не валит прогон: сообщение попадает в errors, остальные батчи идут дальше.

    save_failures — писать ли строку parse_error для неразобранных видео
    (для новых — да, для переразбора — нет, чтобы не портить старый разбор).
    """
    stats: dict[str, Any] = {
        "batches": 0,
        "done": 0,
        "failed": 0,
        "ai": 0,
        "not_ai": 0,
        "topics": {},
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "errors": [],
    }

    for batch in _chunks(rows, BATCH_SIZE):
        stats["batches"] += 1
        ids = [r["video_id"] for r in batch]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(batch)},
        ]

        parsed: dict[str, dict[str, Any]] | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                text, usage = _chat(key, messages, session, use_model)
            except Exception as exc:
                stats["errors"].append(f"api: {exc}")
                log.warning("DeepSeek вызов не удался: %s", exc)
                break

            tin, tout = _usage_counts(usage)
            cost = _cost_usd(usage)
            stats["tokens_in"] += tin
            stats["tokens_out"] += tout
            stats["cost_usd"] += cost
            db.log_llm_usage(conn, STAGE, use_model, tin, tout, cost)

            parsed = parse_response(text, ids, cfg)
            if parsed is not None:
                break
            if attempt + 1 < MAX_ATTEMPTS:
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(batch)},
                    {"role": "assistant", "content": (text or "")[:2000]},
                    {"role": "user", "content": RETRY_INSTRUCTION},
                ]

        if parsed is None:
            for vid in ids:
                if save_failures:
                    db.save_classification(
                        conn, vid, is_ai=None, topic=None, confidence=None,
                        title_ru=None, summary_ru=None, lang=None,
                        reason="parse_error", model=use_model,
                    )
                stats["failed"] += 1
            continue

        for vid in ids:
            item = parsed.get(vid)
            if item is None:
                if save_failures:
                    db.save_classification(
                        conn, vid, is_ai=None, topic=None, confidence=None,
                        title_ru=None, summary_ru=None, lang=None,
                        reason="parse_error", model=use_model,
                    )
                stats["failed"] += 1
                continue
            db.save_classification(
                conn, vid,
                is_ai=item["is_ai"], topic=item["topic"],
                confidence=item["confidence"], title_ru=item["title_ru"],
                summary_ru=item["summary_ru"], lang=item["lang"],
                reason=item["reason"], model=use_model,
            )
            stats["done"] += 1
            if item["is_ai"] == 1:
                stats["ai"] += 1
                topic = item["topic"] or "без темы"
                stats["topics"][topic] = stats["topics"].get(topic, 0) + 1
            elif item["is_ai"] == 0:
                stats["not_ai"] += 1

    stats["cost_usd"] = round(stats["cost_usd"], 6)
    return stats


def _resolve_credentials(api_key: str | None, session: Any) -> tuple[str, Any]:
    """Ключ и HTTP-сессия для обращения к DeepSeek."""
    if api_key is not None:
        key = api_key
    else:
        # D-01 закрыт: подхватываем /root/.hermes/.env, не перетирая окружение.
        config.load_env()
        key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY не задан в окружении")
    if session is None:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("requests не установлен") from exc
        session = requests.Session()
    return key, session


def classify_videos(conn: Any, cfg: Any = config, limit: int | None = None,
                    session: Any = None, api_key: str | None = None,
                    model: str | None = None,
                    video_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """Разобрать неразобранные видео батчами по 20 через DeepSeek.

    Возвращает сводку: сколько разобрано, сколько признано ИИ, распределение
    по темам, расход токенов и денег.

    session — объект с методом post (requests.Session или мок в тестах).
    api_key — если не задан, читается из окружения DEEPSEEK_API_KEY.
    video_ids — разобрать только эти видео (проверка кандидатов).
    """
    key, session = _resolve_credentials(api_key, session)
    use_model = model or DEFAULT_MODEL
    rows = db.get_unclassified(conn, limit, ids=video_ids)
    summary: dict[str, Any] = {
        "requested": len(rows),
        "batches": 0,
        "classified": 0,
        "ai": 0,
        "not_ai": 0,
        "failed": 0,
        "topics": {},
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "errors": [],
    }
    if not rows:
        return summary

    stats = _run_model_batches(conn, rows, key, session, use_model, cfg)
    summary.update({
        "batches": stats["batches"],
        "classified": stats["done"] + stats["failed"],
        "ai": stats["ai"],
        "not_ai": stats["not_ai"],
        "failed": stats["failed"],
        "topics": stats["topics"],
        "tokens_in": stats["tokens_in"],
        "tokens_out": stats["tokens_out"],
        "cost_usd": stats["cost_usd"],
        "errors": stats["errors"],
    })
    return summary


# --- переразбор размеченных видео -----------------------------------------


def _sync_topics(conn: Any) -> list[str]:
    """Досинхронизировать справочник topics из config.TOPICS.

    Возвращает список тем, которые появились в БД впервые (отсортированный).
    """
    try:
        before = {r["name"] for r in conn.execute("SELECT name FROM topics")}
    except Exception:
        before = set()
    db.init_db(conn)
    after = {r["name"] for r in conn.execute("SELECT name FROM topics")}
    return sorted(after - before)


def _select_for_reclassify(conn: Any, limit: int | None,
                           only_ai: bool) -> list[Any]:
    """Видео с уже готовым разбором, стабильный порядок обхода.

    only_ai=True — только строки с is_ai = 1 (видео с is_ai = 0 не трогаем:
    их смысл не изменится, а деньги будут потрачены). Порядок
    ORDER BY classified_at, video_id даёт идемпотентную пагинацию: обновлённые
    строки получают свежий classified_at и уходят в конец очереди.
    """
    sql = """
        SELECT v.video_id, v.title, v.description, v.duration_seconds,
               v.published_at, v.channel_id,
               c.title AS channel_title
        FROM video_classification vc
        JOIN videos v ON v.video_id = vc.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
    """
    params: list[Any] = []
    if only_ai:
        sql += " WHERE vc.is_ai = 1"
    sql += " ORDER BY vc.classified_at, vc.video_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def reclassify(conn: Any, limit: int | None = None, dry_run: bool = False,
              only_ai: bool = True, cfg: Any = config, session: Any = None,
              api_key: str | None = None,
              model: str | None = None) -> dict[str, Any]:
    """Переразобрать видео, у которых уже есть строка в video_classification.

    Обновляет topic, title_ru, is_ai, model, classified_at (по video_id).
    only_ai=True (по умолчанию) — только is_ai = 1. dry_run — только показать,
    сколько видео попадёт под переразбор, без обращений к модели.

    Идемпотентность: обход стабильный (ORDER BY classified_at, video_id),
    поэтому повторный вызов с limit продолжает с того же места.

    Возвращает {"selected", "done", "errors", "skipped", "cost_usd"} плюс
    "topics_added" — темы, появившиеся в справочнике после синхронизации.

    Темы синхронизируются до отбора (init_db заодно гарантирует схему), чтобы
    topics_added честно показал темы, которых в справочнике не было.
    """
    topics_added = _sync_topics(conn)
    rows = _select_for_reclassify(conn, limit, only_ai)
    result: dict[str, Any] = {
        "selected": len(rows),
        "done": 0,
        "errors": 0,
        "skipped": 0,
        "cost_usd": 0.0,
        "topics_added": topics_added,
    }

    if dry_run or not rows:
        return result

    key, session = _resolve_credentials(api_key, session)
    use_model = model or DEFAULT_MODEL
    stats = _run_model_batches(conn, rows, key, session, use_model, cfg,
                               save_failures=False)
    result.update({
        "done": stats["done"],
        "errors": len(stats["errors"]),
        "skipped": stats["failed"],
        "cost_usd": stats["cost_usd"],
        "batches": stats["batches"],
        "topics": stats["topics"],
        "tokens_in": stats["tokens_in"],
        "tokens_out": stats["tokens_out"],
        "errors_list": stats["errors"],
    })
    return result

