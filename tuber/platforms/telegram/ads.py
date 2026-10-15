"""Семантический детектор рекламы Telegram-постов моделью DeepSeek (D-37, ТЗ-B11).

Зачем модуль. Прежнее решение ``collect.is_ad_text`` ставит ``is_ad`` по
подстрокам первых 200 знаков (``erid``/``реклам``/``промокод``). Решением
владельца расширять список ключевых слов ЗАПРЕЩЕНО, поэтому нужен смысловой
детектор: модель читает текст и решает, реклама ли это (прямая интеграция,
промокод, партнёрская ссылка, розыгрыш), а не ищет слова.

Что важно и зашито в код/тесты:

* кэш по ``text_hash`` в отдельной таблице ``ad_cache`` — одинаковый текст не
  оплачивается дважды; повторный прогон на том же входе делает **0 вызовов**
  модели (проверяется тестом);
* батчи по 20 постов, строгий JSON, две попытки; при невалидном ответе —
  ``is_ad = NULL`` и ``status='error'`` (значения не выдумываются);
* ключевое слово НЕ выбрасывается: оно остаётся тем, с чем сравниваем
  (``keyword_is_ad``), и считается в сводке рядом с семантическим решением;
* расход пишется в общий журнал DeepSeek (``tuber_tg_ads``) и в ``classify_daily``
  (``platform='telegram-ad'``), поэтому цена — факт, а не оценка;
* ключ — только из окружения/``.env``; при отсутствии — ``ALERT`` и код 0.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tuber.core import storage, timeutil

from . import config

log = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

BATCH_SIZE = 20
MAX_ATTEMPTS = 2

PROMPT_VER = "tg-ad-v1"
PLATFORM_TAG = "telegram-ad"
SCRIPT_NAME = "tuber_tg_ads"

TEXT_LIMIT = 500
REASON_LIMIT = 200

DEFAULT_DAILY_CAP = 150
DEFAULT_USD_CAP = 0.40

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "prompts", "is_ad.md")

_FALLBACK_PROMPT = """Ты — детектор рекламы в Telegram-постах.
Отвечай строго JSON-массивом, без пояснений и markdown.
Реклама (is_ad=true): прямая интеграция, спонсорский пост, промокод/скидка по
ссылке, партнёрская ссылка, розыгрыш приза, реклама курса/канала за деньги.
НЕ реклама (is_ad=false): обычная новость, авторское мнение, анонс своего
продукта без спонсорской метки, пост о своём канале.
Для каждого поста верни объект:
{"content_id": <как во входе>, "is_ad": true|false, "confidence": 0.0-1.0,
 "reason": "коротко, до 200 знаков, почему"}
"""


def system_prompt() -> str:
    """Промпт детектора: из ``prompts/is_ad.md``, иначе встроенный запасной."""
    try:
        text = Path(PROMPT_PATH).read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return _FALLBACK_PROMPT


# --- хэш/кэш ---------------------------------------------------------------


def text_hash_of(text: str | None) -> str | None:
    """Тот же алгоритм, что у тематического классификатора (межмодульный кэш)."""
    from .classify import text_hash_of as _th
    return _th(text)


def cache_get(conn: Any, text_hash: str) -> Mapping[str, Any] | None:
    if not text_hash:
        return None
    return conn.execute(
        "SELECT * FROM ad_cache WHERE text_hash=? AND status='classified'"
        " AND is_ad IS NOT NULL", (text_hash,)).fetchone()


def _write_cache(conn: Any, text_hash: str, item: Mapping[str, Any], *,
                 model: str, usage: Mapping[str, Any], classified_at: str,
                 status: str, attempts: int, error: str | None = None) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO ad_cache
           (text_hash, is_ad, confidence, reason, method, model, prompt_ver,
            status, attempts, error, prompt_tokens, completion_tokens, cost_usd,
            classified_at, first_seen_at, meta_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (text_hash, item.get("is_ad"), item.get("confidence"), item.get("reason"),
         "deepseek", model, PROMPT_VER, status, int(attempts), error,
         int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
         float(usage.get("cost_usd") or 0.0), classified_at, classified_at,
         json.dumps({"platform": "telegram"})))


# --- вердикт модели → content.is_promo (D-37, задача A) ---------------------


def verdict_for_hash(conn: Any, text_hash: str | None) -> int | None:
    """Вердикт модели (1/0) из ``ad_cache`` по ``text_hash`` или ``None``.

    ``None`` означает «модель этот текст не видела» — тогда в сборе работает
    ключевое правило как fallback (D-37, A3). Ошибка ``'error'``/``is_ad IS
    NULL`` тоже даёт ``None``: выдумывать вердикт запрещено.
    """
    if not text_hash:
        return None
    row = conn.execute(
        "SELECT is_ad FROM ad_cache WHERE text_hash=? AND status='classified'"
        " AND is_ad IS NOT NULL", (text_hash,)).fetchone()
    if row is None:
        return None
    return int(row["is_ad"])


def verdict_for_text(conn: Any, text: str | None,
                     text_hash: str | None = None) -> int | None:
    """Вердикт модели по тексту (хеш считается, если не передан)."""
    th = text_hash or text_hash_of(text)
    return verdict_for_hash(conn, th)


def resolve_is_ad(conn: Any, text: str | None, *, text_hash: str | None = None,
                  keyword: bool | None = None) -> int:
    """Вердикт модели, иначе ключевое правило (D-37, A3).

    Порядок ровно такой: (1) успешная строка ``ad_cache`` по ``text_hash``
    авторитетна; (2) нет вердикта — прежнее ключевое правило. ``keyword`` можно
    передать, чтобы не звать ``collect.is_ad_text`` второй раз.
    """
    verdict = verdict_for_text(conn, text, text_hash)
    if verdict is not None:
        return verdict
    if keyword is None:
        keyword = keyword_is_ad(text)
    return 1 if keyword else 0


def apply_verdict_to_content(conn: Any, content_id: Any, is_ad: Any) -> None:
    """Проставить ``content.is_promo`` по вердикту модели (D-37, A1).

    Пишем в ОБЕ стороны: ``is_ad=true`` → 1, ``is_ad=false`` → 0. Вердикт модели
    авторитетнее ключевого правила для постов, которые она видела.
    """
    conn.execute(
        "UPDATE content SET is_promo=? WHERE platform='telegram' AND id=?",
        (int(bool(is_ad)), content_id))


def backfill_content_promo(conn: Any, *, commit: bool = True) -> dict[str, int]:
    """Догнать ``content.is_promo`` по ``ad_cache`` (D-37, A2). Без сети.

    Join — по ``text_hash`` для ``platform='telegram'``. Пишем в ОБЕ стороны.
    Идемпотентно: повторный прогон даёт нули (``0→1`` = ``1→0`` = 0).

    Возвращает ``{"to_promo": n, "to_normal": m, "matched": k}``, где
    ``to_promo`` — сколько строк изменилось ``0→1``, ``to_normal`` — ``1→0``,
    ``matched`` — сколько строк content вообще имеют вердикт в кэше.
    """
    verdict = ("SELECT a.is_ad FROM ad_cache a WHERE a.text_hash = c.text_hash"
               " AND a.status='classified' AND a.is_ad IS NOT NULL")
    has_verdict = ("EXISTS (SELECT 1 FROM ad_cache a WHERE a.text_hash = c.text_hash"
                   " AND a.status='classified' AND a.is_ad IS NOT NULL)")
    base = f"FROM content c WHERE c.platform='telegram' AND c.text_hash IS NOT NULL AND {has_verdict}"
    matched = conn.execute(f"SELECT COUNT(*) {base}").fetchone()[0] or 0
    to_promo = conn.execute(
        f"SELECT COUNT(*) {base} AND COALESCE(c.is_promo,0)=0 AND ({verdict})=1"
    ).fetchone()[0] or 0
    to_normal = conn.execute(
        f"SELECT COUNT(*) {base} AND c.is_promo=1 AND ({verdict})=0"
    ).fetchone()[0] or 0
    if to_promo or to_normal:
        # В SQLite у UPDATE нет псевдонима c — ссылаемся на саму таблицу content.
        upd_verdict = ("SELECT a.is_ad FROM ad_cache a"
                       " WHERE a.text_hash = content.text_hash"
                       " AND a.status='classified' AND a.is_ad IS NOT NULL")
        upd_exists = ("EXISTS (SELECT 1 FROM ad_cache a"
                      " WHERE a.text_hash = content.text_hash"
                      " AND a.status='classified' AND a.is_ad IS NOT NULL)")
        conn.execute(
            f"UPDATE content SET is_promo = ({upd_verdict})"
            f" WHERE platform='telegram' AND text_hash IS NOT NULL AND {upd_exists}"
            f" AND COALESCE(is_promo,-1) != ({upd_verdict})")
        if commit:
            conn.commit()
    return {"to_promo": int(to_promo), "to_normal": int(to_normal),
            "matched": int(matched)}



# --- сравнение с ключевым правилом -----------------------------------------


def keyword_is_ad(text: str | None) -> bool:
    """ТЕКУЩЕЕ правило (ключевые слова) — с ним сравниваем семантику (D-37)."""
    from .collect import is_ad_text
    return bool(is_ad_text(text))


# --- отбор -----------------------------------------------------------------


def select_pending(conn: Any, *, handles: Sequence[str] | None = None,
                   only: str | None = None, limit: int | None = None
                   ) -> list[Mapping[str, Any]]:
    """Посты с текстом, для которых нет успешной строки ``ad_cache``."""
    where = ["c.platform = ?", "c.text IS NOT NULL", "length(trim(c.text)) > 0",
             "c.deleted_at IS NULL"]
    args: list[Any] = ["telegram"]
    if only is not None:
        where.append("lower(s.handle) = ?")
        args.append(only.lstrip("@").lower())
    elif handles:
        hs = sorted({h.lower() for h in handles if h})
        if not hs:
            return []
        where.append("lower(s.handle) IN (" + ",".join("?" for _ in hs) + ")")
        args.extend(hs)
    sql = f"""
        SELECT c.id AS content_id, c.external_id, c.url, c.text, c.text_hash,
               c.published_at, s.handle AS handle
        FROM content c JOIN source s ON s.id = c.source_id
        LEFT JOIN ad_cache a ON a.text_hash = c.text_hash
        WHERE {' AND '.join(where)}
          AND (a.text_hash IS NULL OR a.is_ad IS NULL)
        ORDER BY c.published_at DESC, c.id DESC
    """
    rows = list(conn.execute(sql, args))
    return rows[:int(limit)] if limit else rows


# --- промпт ----------------------------------------------------------------


def build_user_prompt(rows: Sequence[Mapping[str, Any]]) -> str:
    items = []
    for r in rows:
        items.append({
            "content_id": r["content_id"],
            "channel": r["handle"],
            "text": (r["text"] or "")[:TEXT_LIMIT],
        })
    return ("Определи рекламу. Верни JSON-массив, объект на каждый пост в том же"
            " порядке.\n" + json.dumps(items, ensure_ascii=False))


RETRY_INSTRUCTION = (
    "Предыдущий ответ не был валидным JSON. Верни ТОЛЬКО JSON-массив объектов,"
    " без markdown и пояснений.")


# --- разбор ответа ---------------------------------------------------------


def extract_json_array(text: str) -> list | None:
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        loaded = json.loads(t)
        if isinstance(loaded, list):
            return loaded
    except (ValueError, TypeError):
        pass
    dec = json.JSONDecoder()
    idx = 0
    while True:
        pos = t.find("[", idx)
        if pos == -1:
            return None
        try:
            obj, _ = dec.raw_decode(t[pos:])
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
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def normalize_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    cid = item.get("content_id", item.get("id"))
    if cid is None:
        return None
    is_ad = _as_bool(item.get("is_ad"))
    if is_ad is None:
        return None
    reason = item.get("reason")
    reason = " ".join(str(reason).split())[:REASON_LIMIT] if reason else None
    return {"content_id": str(cid), "is_ad": int(is_ad),
            "confidence": _as_float(item.get("confidence")), "reason": reason}


def parse_response(text: str, expected_ids: Sequence[Any] | None = None
                   ) -> dict[str, dict[str, Any]] | None:
    arr = extract_json_array(text)
    if arr is None:
        return None
    allowed = {str(i) for i in expected_ids} if expected_ids is not None else None
    out: dict[str, dict[str, Any]] = {}
    for item in arr:
        if not isinstance(item, Mapping):
            continue
        norm = normalize_item(item)
        if norm is None:
            continue
        if allowed is not None and norm["content_id"] not in allowed:
            continue
        out[norm["content_id"]] = norm
    return out


# --- сеть ------------------------------------------------------------------


def _usage(conn: Any, model, usage: Mapping[str, Any]) -> None:
    """Запись расхода в общий журнал DeepSeek (никогда не бросает).

    P0-3: вызов дополнительно попадает в единый журнал ``llm_usage``
    (``platform='telegram'``, ``stage='ads'``) через ядровый писатель.
    """
    rec = None
    try:
        from tuber.platforms.x.channels import log_deepseek_usage
        rec = log_deepseek_usage(
            SCRIPT_NAME, model,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            int(usage.get("prompt_cache_hit_tokens") or 0))
    except Exception as exc:  # noqa: BLE001 — учёт не должен ронять прогон
        log.debug("ads: запись расхода не удалась: %s", exc)
    try:
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost_usd")
        if cost is None and rec is not None:
            # Та же цена, что попала в jsonl: журналы обязаны сходиться.
            cost = rec.get("cost_usd")
        storage.add_llm_usage(
            conn, "telegram", stage="ads", model=model,
            tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=float(cost or 0.0), created_at=timeutil.iso_now())
    except Exception as exc:  # noqa: BLE001 — учёт не должен ронять прогон
        log.debug("ads: запись в llm_usage не удалась: %s", exc)


def _chat(api_key: str, messages: list[dict], session: Any, model: str,
          timeout: float = 60.0) -> tuple[str, dict]:
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": 0,
               "stream": False}
    resp = session.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=timeout)
    if hasattr(resp, "raise_for_status"):
        resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    text = (choices[0].get("message") or {}).get("content") if choices else ""
    return text or "", (data.get("usage") or {})


# --- предохранители --------------------------------------------------------


def daily_cap() -> int:
    try:
        return int(os.environ.get("TG_AD_DAILY_CAP", DEFAULT_DAILY_CAP))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def usd_cap() -> float:
    try:
        return float(os.environ.get("TG_AD_USD_CAP", DEFAULT_USD_CAP))
    except (TypeError, ValueError):
        return DEFAULT_USD_CAP


def _bump_daily(conn: Any, day: str, *, posts: int = 0, model_calls: int = 0,
                failed: int = 0, prompt_tokens: int = 0,
                completion_tokens: int = 0, cost_usd: float = 0.0) -> None:
    """Учёт стадии рекламы в общей ``classify_daily`` (platform='telegram-ad')."""
    row = conn.execute(
        "SELECT * FROM classify_daily WHERE day=? AND platform=?",
        (day, PLATFORM_TAG)).fetchone()
    storage_day = {
        "posts": (int(row["posts"] or 0) if row else 0) + posts,
        "model_calls": (int(row["model_calls"] or 0) if row else 0) + model_calls,
        "failed": (int(row["failed"] or 0) if row else 0) + failed,
        "prompt_tokens": (int(row["prompt_tokens"] or 0) if row else 0) + prompt_tokens,
        "completion_tokens": (int(row["completion_tokens"] or 0) if row else 0) + completion_tokens,
        "cost_usd": round((float(row["cost_usd"] or 0.0) if row else 0.0) + cost_usd, 8),
    }
    conn.execute(
        "INSERT OR REPLACE INTO classify_daily "
        "(day, platform, posts, model_calls, failed, prompt_tokens,"
        " completion_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?)",
        (day, PLATFORM_TAG, storage_day["posts"], storage_day["model_calls"],
         storage_day["failed"], storage_day["prompt_tokens"],
         storage_day["completion_tokens"], storage_day["cost_usd"]))
    conn.commit()


# --- основной проход -------------------------------------------------------


def run(conn: Any, *, handles: Sequence[str] | None = None, only: str | None = None,
        limit: int | None = None, dry_run: bool = False, session: Any = None,
        api_key: str | None = None, model: str | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Разметка рекламы. Идемпотентна по кэшу ``ad_cache`` (``text_hash``)."""
    now = now or datetime.now(timezone.utc)
    use_model = model or DEFAULT_MODEL
    pending = select_pending(conn, handles=handles, only=only, limit=limit)

    summary: dict[str, Any] = {
        "selected": len(pending), "classified": 0, "ad": 0, "not_ad": 0,
        "failed": 0, "cached": 0, "batches": 0, "model_calls": 0,
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
        "keyword_ad": 0, "dry_run": dry_run, "model": use_model,
        "no_key": False, "errors": [],
    }
    # Ключевое правило считается на ТОМ ЖЕ входе — это и есть сравнение D-37.
    for r in pending:
        if keyword_is_ad(r["text"]):
            summary["keyword_ad"] += 1

    if dry_run:
        return summary

    from .classify import resolve_api_key
    key = resolve_api_key(api_key)
    if not key:
        summary["no_key"] = True
        return summary
    if session is None:
        import requests
        session = requests.Session()
        log_usage = True   # только реальные сетевые вызовы (см. classify.run)
    else:
        log_usage = False

    # Дедуп по text_hash: одинаковый текст — один вызов модели.
    by_hash: dict[str, list] = {}
    for r in pending:
        th = r["text_hash"] or text_hash_of(r["text"])
        by_hash.setdefault(th or f"__row{r['content_id']}", []).append(r)

    to_model = []
    for th, group in by_hash.items():
        cached = cache_get(conn, th)
        if cached is not None:
            summary["classified"] += len(group)
            summary["cached"] += len(group)
            # Вердикт из кэша тоже авторитетен: догоняем content.is_promo и для
            # уже размеченных строк (D-37, A1/A2 — поведение в обе стороны).
            for r in group:
                apply_verdict_to_content(conn, r["content_id"], cached["is_ad"])
            if int(cached["is_ad"]) == 1:
                summary["ad"] += len(group)
            else:
                summary["not_ad"] += len(group)
        else:
            to_model.append((th, group[0]))

    if not to_model:
        return summary

    day = now.strftime("%Y-%m-%d")
    left = daily_cap()
    for start in range(0, len(to_model), BATCH_SIZE):
        if left <= 0:
            break
        chunk = to_model[start:start + BATCH_SIZE]
        chunk = chunk[:left]
        left -= len(chunk)
        summary["batches"] += 1
        rows = [c[1] for c in chunk]
        ids = [str(r["content_id"]) for r in rows]
        messages = [{"role": "system", "content": system_prompt()},
                    {"role": "user", "content": build_user_prompt(rows)}]
        parsed = None
        attempts = 0
        for attempt in range(MAX_ATTEMPTS):
            attempts += 1
            try:
                text, usage = _chat(key, messages, session, use_model)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"api: {exc}")
                break
            summary["model_calls"] += 1
            tin = int(usage.get("prompt_tokens") or 0)
            tout = int(usage.get("completion_tokens") or 0)
            summary["tokens_in"] += tin
            summary["tokens_out"] += tout
            _usage(conn, use_model, usage) if log_usage else None
            _bump_daily(conn, day, model_calls=1, prompt_tokens=tin,
                        completion_tokens=tout)
            parsed = parse_response(text, ids)
            if parsed is not None:
                break
            if attempt + 1 < MAX_ATTEMPTS:
                messages = messages + [
                    {"role": "assistant", "content": (text or "")[:2000]},
                    {"role": "user", "content": RETRY_INSTRUCTION}]
        classified_at = timeutil.iso_now()
        for th, row in chunk:
            item = (parsed or {}).get(str(row["content_id"]))
            if item is None:
                summary["failed"] += 1
                _write_cache(conn, th, {"is_ad": None}, model=use_model,
                             usage={}, classified_at=classified_at,
                             status="error", attempts=attempts,
                             error="invalid_json" if parsed is None else "missing_in_response")
                continue
            _write_cache(conn, th, item, model=use_model,
                         usage={}, classified_at=classified_at,
                         status="classified", attempts=attempts)
            # A1: вердикт модели сразу доходит до колонки — в обе стороны.
            apply_verdict_to_content(conn, row["content_id"], item["is_ad"])
            summary["classified"] += 1
            if item["is_ad"] == 1:
                summary["ad"] += 1
            else:
                summary["not_ad"] += 1
        _bump_daily(conn, day, posts=len(chunk),
                    failed=sum(1 for _th, row in chunk
                               if (parsed or {}).get(str(row["content_id"])) is None))
        conn.commit()

    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m tuber tg ads [--limit N] [--only H] [--dry-run]``."""
    import argparse

    from . import store as db

    parser = argparse.ArgumentParser(
        prog="tuber tg ads",
        description="семантический детектор рекламы Telegram (D-37, ТЗ-B11)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", default=None, help="ограничить одним хендлом")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill", action="store_true",
                        help="догнать content.is_promo по ad_cache (D-37, A2; без сети)")
    parser.add_argument("--allow-production", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    db_path = config.resolve_db()
    if not args.dry_run:
        gate = db.production_gate_error(db_path, args.allow_production)
        if gate:
            print(gate, file=sys.stderr)
            return 2
    conn = db.connect(db_path)
    try:
        if args.backfill:
            counts = backfill_content_promo(conn)
            print(f"ADS-BACKFILL: 0→1 {counts['to_promo']}, 1→0 "
                  f"{counts['to_normal']}, с вердиктом {counts['matched']}")
            return 0
        summary = run(conn, only=args.only, limit=args.limit, dry_run=args.dry_run)
        if summary.get("no_key"):
            print("ALERT: нет ключа DeepSeek — детектор рекламы пропущен")
            return 0
        if args.dry_run:
            print(f"ADS-DRY-RUN: к разбору {summary['selected']} постов, "
                  f"ключевое правило нашло {summary['keyword_ad']}")
            return 0
        print(f"ADS: к разбору {summary['selected']}, разобрано "
              f"{summary['classified']} (из кэша {summary['cached']}), реклама "
              f"{summary['ad']}, не реклама {summary['not_ad']}, сбоев "
              f"{summary['failed']}, вызовов {summary['model_calls']}, "
              f"токены {summary['tokens_in']}/{summary['tokens_out']}, "
              f"ключевое правило нашло {summary['keyword_ad']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
