"""Разбор обложек ТОЛЬКО у топ-видео (vision, kimi-k2.6).

Пул из нескольких тысяч видео через зрение гонять бессмысленно и дорого.
Обложки нужны у выбросов — видео, чьи просмотры не ниже ``THUMB_MIN_MULT``
медианы своего канала (раздельно для шортсов и полных). Логику выбросов
модуль НЕ дублирует: она берётся из :func:`report._outlier_map`.

Правила честности (METHODOLOGY.md, RESEARCH-MECHANICS.md):
- ничего не выдумываем: полей, которых нет в ответе модели, не создаём;
- если ответ получен, но не распарсился — ``ok=True``, ``parsed=None``,
  сырой текст сохранён, причина честно записана;
- если обложка не скачалась или запрос упал — в ``thumbnail_vision`` НЕ
  пишем, чтобы повторный прогон сработал (идемпотентность через повтор);
- повторный прогон не платит второй раз: уже разобранные ``video_id``
  исключаются из отбора.

Сеть здесь есть, но только по явному вызову ``run``/``analyze``. Отбор,
``cost`` и ``save`` сети не касаются.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from . import config, report

log = logging.getLogger(__name__)

# Дословный промпт версии v2. Менять только вместе с THUMB_PROMPT_VERSION.
# v2: main_text вместо простыни, objects<=6, запрет OCR скриншотов.
PROMPT = (
    "Ты разбираешь обложку YouTube-видео. Опиши ТОЛЬКО саму обложку, "
    "а не кадр или картинку внутри неё.\n"
    "Правила:\n"
    "- не перечисляй текст со скриншотов и интерфейсов внутри картинки, "
    "не расшифровывай мелкие подписи интерфейса;\n"
    "- main_text — самое крупное и читаемое на обложке, то, что видно с первого "
    "взгляда (заголовок), до 120 знаков; если крупного текста нет — пустая строка;\n"
    "- small_text — мелкий текст, только если он является частью оформления "
    "обложки, до 80 знаков;\n"
    "- objects — не больше 6 главных объектов, только значимые;\n"
    "- отвечай ТОЛЬКО JSON, без пояснений.\n"
    "Ответ — JSON строго такой схемы:\n"
    '{"main_text": "крупный текст обложки — заголовок, до 120 знаков", '
    '"small_text": "мелкий текст оформления, до 80 знаков", '
    '"objects": ["до 6 главных объектов"], "people_count": 0, '
    '"has_arrows_or_circles": false, "dominant_colors": ["до 3 цветов"], '
    '"style": "краткий стиль оформления"}'
)

# Максимальная длина thumb_text в seo_fields, знаков (обрезаем с «…»).
THUMB_TEXT_MAX = 200
# Максимум объектов в thumb_objects.
THUMB_OBJECTS_MAX = 6

# Сколько попыток скачивания картинки: первый запрос + два ретрая.
IMAGE_ATTEMPTS = 3

# Сколько попыток запроса к модели (повтор только для не-400 сбоев).
REQUEST_ATTEMPTS = 2

_FENCE_HEAD_RE = re.compile(r"^```[a-zA-Z]*\s*")
_FENCE_TAIL_RE = re.compile(r"\s*```$")


# --- ключ ------------------------------------------------------------------


def get_api_key() -> str | None:
    """Ключ KIMI_API_KEY из окружения (после config.load_env)."""
    config.load_env()
    key = (os.environ.get("KIMI_API_KEY") or "").strip()
    return key or None


# --- отбор топ-видео -------------------------------------------------------


def _existing_ids(conn) -> set[str]:
    """video_id, по которым уже есть строка в thumbnail_vision."""
    rows = conn.execute("SELECT video_id FROM thumbnail_vision").fetchall()
    return {r[0] for r in rows if r[0]}


def _channel_titles(conn) -> dict[str, str]:
    """Названия каналов по channel_id (для читаемого вывода)."""
    try:
        rows = conn.execute("SELECT channel_id, title FROM channels").fetchall()
    except Exception:  # noqa: BLE001 — вывод не должен падать из-за схемы
        return {}
    out: dict[str, str] = {}
    for r in rows:
        cid = r["channel_id"]
        if cid:
            out[cid] = r["title"] or ""
    return out


def _latest_views_map(conn) -> dict[str, float]:
    """Последние непустые просмотры каждого видео."""
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


def _channel_ai_n(conn) -> dict[tuple[str, int], int]:
    """Число ИИ-видео с известными просмотрами по паре (канал, формат).

    Состав базы сравнения ровно тот же, что у :func:`report._outlier_map`:
    последний непустой замер, ``is_ai`` NULL или 1 (видео без классификации
    считаются ИИ-контентом, как и в медиане), непустой ``channel_id``.
    Считается одним SQL-запросом, а не в цикле по кандидатам.
    """
    rows = conn.execute(
        """
        SELECT v.channel_id AS channel_id,
               CASE WHEN v.is_shorts = 1 THEN 1 ELSE 0 END AS fmt,
               COUNT(*) AS n
        FROM videos v
        JOIN snapshots s ON s.id = (
            SELECT id FROM snapshots
            WHERE video_id = v.video_id AND views IS NOT NULL
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
        )
        LEFT JOIN video_classification vc ON vc.video_id = v.video_id
        WHERE s.views IS NOT NULL
          AND v.channel_id IS NOT NULL
          AND (vc.is_ai IS NULL OR vc.is_ai = 1)
        GROUP BY v.channel_id, fmt
        """
    ).fetchall()
    return {
        (r["channel_id"], int(r["fmt"])): int(r["n"])
        for r in rows
        if r["channel_id"]
    }


def _candidates(conn, min_mult: float,
                min_channel_n: int = config.THUMB_MIN_CHANNEL_N
                ) -> tuple[list[dict[str, Any]], int]:
    """Все выбросы с кратностью ≥ min_mult и непустой обложкой, по убыванию.

    Кратность считается внутри формата канала: логика выбросов берётся из
    ``report._outlier_map`` и здесь не повторяется.

    Кандидаты с числом видео канала (того же формата) меньше ``min_channel_n``
    отсеиваются как недостоверные; их число возвращается вторым элементом.
    ``min_channel_n <= 0`` выключает фильтр.
    """
    omap = report._outlier_map(conn)
    views_map = _latest_views_map(conn)
    channel_n = _channel_ai_n(conn)
    rows = conn.execute(
        "SELECT video_id, channel_id, title, is_shorts, thumbnail_url FROM videos"
    ).fetchall()

    items: list[dict[str, Any]] = []
    skipped_low_n = 0
    for row in rows:
        vid = row["video_id"]
        mult = omap.get(vid)
        if mult is None or mult < float(min_mult):
            continue
        url = (row["thumbnail_url"] or "").strip()
        if not url:
            continue
        if int(min_channel_n) > 0:
            flag = 1 if row["is_shorts"] == 1 else 0
            n = channel_n.get((row["channel_id"], flag), 0)
            if n < int(min_channel_n):
                skipped_low_n += 1
                continue
        views = views_map.get(vid)
        items.append({
            "video_id": vid,
            "channel_id": row["channel_id"],
            "title": row["title"],
            "views": views,
            "mult": float(mult),
            "is_shorts": row["is_shorts"] == 1,
            "thumbnail_url": url,
        })
    items.sort(key=lambda x: (-x["mult"], x["video_id"]))
    return items, skipped_low_n


def _apply_selection(cands: list[dict[str, Any]], existing: set[str],
                     max_per_channel: int, limit: int | None) -> list[dict[str, Any]]:
    """Исключить разобранные, ограничить по каналу и по общему числу."""
    if limit is not None and int(limit) <= 0:
        return []
    out: list[dict[str, Any]] = []
    per_channel: dict[str, int] = {}
    for it in cands:
        if it["video_id"] in existing:
            continue
        channel = it["channel_id"]
        if max_per_channel and channel:
            if per_channel.get(channel, 0) >= max_per_channel:
                continue
            per_channel[channel] = per_channel.get(channel, 0) + 1
        out.append(it)
        if limit is not None and len(out) >= int(limit):
            break
    return out


def select_top_videos(conn, min_mult: float = config.THUMB_MIN_MULT,
                      max_per_channel: int = config.THUMB_MAX_PER_CHANNEL,
                      limit: int | None = None,
                      min_channel_n: int = config.THUMB_MIN_CHANNEL_N
                      ) -> list[dict[str, Any]]:
    """Отобрать топ-видео (выбросы) для разбора обложек.

    Возвращает список словарей: ``video_id``, ``channel_id``, ``title``,
    ``views``, ``mult``, ``is_shorts``, ``thumbnail_url``.

    ``min_channel_n`` — минимум видео канала (того же формата) с известными
    просмотрами: при меньшем числе медиана недостоверна и кратность врёт.
    ``0`` выключает фильтр.
    """
    cands, _skipped_low_n = _candidates(conn, min_mult, min_channel_n)
    return _apply_selection(cands, _existing_ids(conn), max_per_channel, limit)


# --- сеть ------------------------------------------------------------------


def _content_type(data: bytes) -> str:
    """Тип картинки по сигнатуре (по умолчанию jpeg)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def fetch_image(url: str, timeout: int = config.THUMB_IMAGE_TIMEOUT_SEC) -> bytes | None:
    """Скачать обложку: первый запрос плюс два ретрая. Провал — честный None."""
    if not url:
        return None
    last_error: Exception | None = None
    for _attempt in range(IMAGE_ATTEMPTS):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "TuberOS/1.0"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                data = resp.read()
            if data:
                return data
            last_error = ValueError("пустой ответ")
        except Exception as exc:  # noqa: BLE001 — наружу не отдаём
            last_error = exc
            log.warning("обложка не скачалась (%s): %s", url, exc)
    if last_error is not None:
        log.warning("обложка недоступна после %d попыток (%s)", IMAGE_ATTEMPTS, url)
    return None


def _strip_fences(text: str) -> str:
    """Снять markdown-обёртку ```json ... ```."""
    t = (text or "").strip()
    t = _FENCE_HEAD_RE.sub("", t)
    t = _FENCE_TAIL_RE.sub("", t)
    return t.strip()


def _usage_counts(usage: dict[str, Any]) -> tuple[int, int, int]:
    """(prompt_tokens, completion_tokens, cached_tokens) из ответа модели."""
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    cached = usage.get("cached_tokens")
    if cached is None:
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
    return tokens_in, tokens_out, int(cached or 0)


def cost(prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
    """Стоимость вызова в долларах по тарифам kimi-k2.6.

    Cache-hit входные токены считаются дешевле; остальные входные — по
    обычному тарифу. Отрицательный кэш не создаёт отрицательной стоимости.
    """
    tokens_in = int(prompt_tokens or 0)
    tokens_out = int(completion_tokens or 0)
    cached = max(int(cached_tokens or 0), 0)
    cached = min(cached, tokens_in)
    miss = tokens_in - cached
    return (
        miss * config.THUMB_PRICE_IN_MISS / 1_000_000
        + cached * config.THUMB_PRICE_IN_CACHE / 1_000_000
        + tokens_out * config.THUMB_PRICE_OUT / 1_000_000
    )


def _post_chat(api_key: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    """POST в Moonshot, вернуть разобранный JSON ответа."""
    request = urllib.request.Request(
        config.THUMB_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def analyze(video_id: str, url: str) -> dict[str, Any]:
    """Один вызов vision-модели по обложке.

    Возвращает словарь с ключами ``ok``, ``parsed``, ``raw``, ``error``,
    ``tokens_in``, ``tokens_out``, ``cached_tokens``, ``cost_usd``,
    ``latency_ms``, ``model``, ``prompt_version``.
    """
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": False,
        "parsed": None,
        "raw": "",
        "error": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "cached_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
        "model": config.THUMB_MODEL,
        "prompt_version": config.THUMB_PROMPT_VERSION,
    }

    image = fetch_image(url, timeout=config.THUMB_IMAGE_TIMEOUT_SEC)
    if image is None:
        result["error"] = "не удалось скачать обложку"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        return result

    api_key = get_api_key()
    if not api_key:
        result["error"] = "KIMI_API_KEY не задан"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        return result

    encoded = base64.b64encode(image).decode("ascii")
    data_url = f"data:{_content_type(image)};base64,{encoded}"
    payload = {
        "model": config.THUMB_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        "thinking": {"type": "disabled"},
        "temperature": 0.6,
        "max_tokens": 800,
    }

    response: dict[str, Any] | None = None
    last_error: str | None = None
    for _attempt in range(REQUEST_ATTEMPTS):
        try:
            response = _post_chat(api_key, payload, config.THUMB_TIMEOUT_SEC)
            break
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                detail = ""
            if exc.code == 400:
                # 400 — не транзиентная ошибка (например, неверная temperature).
                last_error = f"HTTP 400: {detail or exc.reason}"
                log.warning("kimi 400 на %s: %s", video_id, last_error)
                break
            last_error = f"HTTP {exc.code}: {detail or exc.reason}"
            log.warning("kimi HTTP %s на %s: %s", exc.code, video_id, last_error)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.warning("kimi вызов не удался на %s: %s", video_id, exc)

    if response is None:
        result["error"] = last_error or "запрос к модели не удался"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        return result

    choices = response.get("choices") or []
    raw = ""
    if choices:
        raw = ((choices[0].get("message") or {}).get("content")) or ""
    usage = response.get("usage") or {}
    tokens_in, tokens_out, cached = _usage_counts(usage)

    result.update({
        "ok": True,
        "raw": raw,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cached_tokens": cached,
        "cost_usd": cost(tokens_in, tokens_out, cached),
        "latency_ms": int((time.monotonic() - started) * 1000),
    })

    cleaned = _strip_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError):
        parsed = None
    if not isinstance(parsed, dict):
        result["parsed"] = None
        note = "ответ получен, но не распарсился как JSON-объект"
        result["error"] = f"{note}: {cleaned[:200]}" if cleaned else note
    else:
        result["parsed"] = parsed
    return result


# --- запись ----------------------------------------------------------------


def _json_or_none(value: Any) -> str | None:
    """JSON-строка значения или None, если значения нет."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


# --- разбор полей ----------------------------------------------------------


def _first(parsed: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Первое непустое по существованию значение среди ключей (новые → старые)."""
    for key in keys:
        if key in parsed and parsed[key] is not None:
            return parsed[key]
    return None


def _as_int(value: Any) -> int | None:
    """Привести к int (None при неудаче)."""
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip_text(text: Any, limit: int = THUMB_TEXT_MAX) -> str:
    """Обрезать текст до ``limit`` знаков; при обрезке добавить «…»."""
    if text is None:
        return ""
    t = str(text).strip()
    if len(t) <= limit:
        return t
    return t[: max(limit - 1, 0)] + "…"


def _cap_objects(value: Any) -> list[str] | None:
    """Список объектов, не больше ``THUMB_OBJECTS_MAX``; иначе None."""
    if isinstance(value, list):
        return [str(x) for x in value][:THUMB_OBJECTS_MAX]
    return None


def normalize_parsed(parsed: Any) -> dict[str, Any] | None:
    """Привести ответ модели к схеме v2, читая и старые ключи (v1).

    Соответствие: ``main_text`` ← ``text_on_image``, ``people_count`` ←
    ``face_count``, ``has_arrows_or_circles`` ← ``arrows_or_circles``,
    ``dominant_colors`` ← ``colors``. Неизвестные ключи игнорируются.
    """
    if not isinstance(parsed, dict):
        return None
    main = _first(parsed, ("main_text", "text_on_image"))
    return {
        "main_text": "" if main is None else str(main).strip(),
        "small_text": _first(parsed, ("small_text",)) or "",
        "objects": _cap_objects(_first(parsed, ("objects",))),
        "people_count": _as_int(_first(parsed, ("people_count", "face_count"))),
        "has_arrows_or_circles": _first(
            parsed, ("has_arrows_or_circles", "arrows_or_circles")
        ),
        "dominant_colors": _first(parsed, ("dominant_colors", "colors")),
        "style": _first(parsed, ("style",)),
    }


def save(conn, video_id: str, res: dict[str, Any]) -> None:
    """Записать результат разбора: thumbnail_vision, seo_fields, llm_usage.

    ``thumb_text`` — только ``main_text``, обрезанный до ``THUMB_TEXT_MAX``
    знаков (простыни не пишем никогда); ``extracted_text`` — ``main_text`` без
    обрезки; ``thumb_objects`` — JSON-строка не больше ``THUMB_OBJECTS_MAX``.
    """
    parsed = res.get("parsed")
    norm = normalize_parsed(parsed)
    if norm is None:
        main_text: str | None = None
        clipped = ""
        objects = None
        people_count = None
        arrows = None
        colors = None
        style = None
    else:
        main_text = norm["main_text"]
        clipped = _clip_text(main_text)
        objects = norm["objects"]
        people_count = norm["people_count"]
        arrows = norm["has_arrows_or_circles"]
        colors = norm["dominant_colors"]
        style = norm["style"]
    words = len(clipped.split()) if clipped else 0
    obj_n = len(objects) if objects else 0

    conn.execute(
        """
        INSERT INTO thumbnail_vision
            (video_id, model, prompt_version, description_raw, extracted_text,
             cost_usd, latency_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            video_id,
            res.get("model"),
            res.get("prompt_version"),
            res.get("raw"),
            main_text,
            res.get("cost_usd"),
            res.get("latency_ms"),
            config.now_ts(),
        ),
    )
    conn.execute(
        "INSERT INTO llm_usage (stage, model, tokens_in, tokens_out, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "thumbnail_vision",
            res.get("model"),
            int(res.get("tokens_in") or 0),
            int(res.get("tokens_out") or 0),
            float(res.get("cost_usd") or 0.0),
            config.now_ts(),
        ),
    )
    log.info("обложка %s: main_text %d знаков, объектов %d",
             video_id, len(clipped), obj_n)

    if norm is not None:
        thumb_arrows = None if arrows is None else (1 if arrows else 0)
        # UPDATE, а не INSERT: строки seo_fields может не быть — не создаём её.
        conn.execute(
            """
            UPDATE seo_fields SET
                thumb_text = ?, thumb_text_words = ?, thumb_objects = ?,
                thumb_face_count = ?, thumb_arrows = ?, thumb_colors = ?,
                thumb_style = ?
            WHERE video_id = ?
            """,
            (
                clipped,
                words,
                _json_or_none(objects),
                people_count,
                thumb_arrows,
                _json_or_none(colors),
                style,
                video_id,
            ),
        )
    conn.commit()


# --- прогон ----------------------------------------------------------------


def run(conn, limit: int | None = None, min_mult: float = config.THUMB_MIN_MULT,
        max_per_channel: int = config.THUMB_MAX_PER_CHANNEL,
        dry_run: bool = False, budget_usd: float | None = None,
        min_channel_n: int = config.THUMB_MIN_CHANNEL_N) -> dict[str, Any]:
    """Разобрать обложки топ-видео (выбросов).

    ``dry_run=True`` — только отбор и печать первых 10, без сети и записи.
    Останавливается при достижении ``budget_usd`` (по умолчанию
    ``THUMB_DAILY_BUDGET_USD``). Возвращает сводку:
    ``selected``, ``done``, ``errors``, ``skipped_existing``,
    ``skipped_low_n``, ``cost_usd``.

    ``min_channel_n`` — минимум видео канала (того же формата) с известными
    просмотрами; меньше — медиана недостоверна, кандидат отсеивается
    (``skipped_low_n``). ``0`` выключает фильтр.
    """
    budget = config.THUMB_DAILY_BUDGET_USD if budget_usd is None else float(budget_usd)
    cands, skipped_low_n = _candidates(conn, min_mult, min_channel_n)
    existing = _existing_ids(conn)
    skipped_existing = sum(1 for c in cands if c["video_id"] in existing)
    selected = _apply_selection(cands, existing, max_per_channel, limit)

    if dry_run:
        print(f"Обложки (сухой прогон): к разбору {len(selected)}, "
              f"отсеяно по достоверности {skipped_low_n}")
        titles = _channel_titles(conn)
        for it in selected[:10]:
            views = it["views"]
            views_text = f"{int(views):,}".replace(",", " ") if views is not None else "?"
            fmt = "shorts" if it["is_shorts"] else "long"
            channel = titles.get(it["channel_id"], "")
            print(
                f"{it['mult']:.1f}x | {views_text} просмотров | {fmt} | "
                f"{channel} — {it['title']}"
            )
        return {
            "selected": len(selected),
            "done": 0,
            "errors": 0,
            "skipped_existing": skipped_existing,
            "skipped_low_n": skipped_low_n,
            "cost_usd": 0.0,
        }

    done = errors = 0
    spent = 0.0
    for i, it in enumerate(selected, 1):
        if spent >= budget:
            log.info("обложки: остановка по бюджету $%.4f", budget)
            break
        res = analyze(it["video_id"], it["thumbnail_url"])
        if not res.get("ok"):
            errors += 1
            log.warning("обложка %s: %s", it["video_id"], res.get("error"))
            continue
        save(conn, it["video_id"], res)
        done += 1
        spent += float(res.get("cost_usd") or 0.0)
        if i % 10 == 0:
            log.info("обложки: обработано %d из %d", i, len(selected))

    return {
        "selected": len(selected),
        "done": done,
        "errors": errors,
        "skipped_existing": skipped_existing,
        "skipped_low_n": skipped_low_n,
        "cost_usd": spent,
    }


def format_run(res: dict[str, Any]) -> str:
    """Строка итога на русском."""
    return (
        f"обложки: разобрано {res['done']}, ошибок {res['errors']}, "
        f"потрачено ${res['cost_usd']:.4f}, уже было {res['skipped_existing']}, "
        f"отсеяно (мало видео у канала) {res.get('skipped_low_n', 0)}"
    )
