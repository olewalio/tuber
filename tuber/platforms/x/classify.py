"""Классификация постов моделью DeepSeek (ТЗ-3 Р1).

Правила, зашитые в код и проверяемые тестами:

* тематическое решение принимает **модель**, не список ключевых слов (Р1.1).
  Ключевые слова работают только предфильтром и помечают пост `heuristic`,
  а не `classified`;
* батчи по 20 постов, ответ — строгий JSON, валидируется схемой; при
  невалидном ответе один повтор, затем статус `failed` и повтор в следующем
  прогоне (Р1.3);
* кэш по `text_hash`: одинаковый текст не классифицируется дважды (Р1.4);
* перед прогоном проверяется бюджет DeepSeek, дневной потолок Tuber-x
  (`CLASSIFY_DAILY_CAP`, default 400, боевое значение — env
  `TUBER_X_CLASSIFY_DAILY_CAP`) и денежный предохранитель стадии
  (`CLASSIFY_DAILY_USD_CAP`, env `TUBER_X_CLASSIFY_DAILY_USD_CAP`),
  приоритет: свежие, TIER-A, уникальные тексты (Р1.5, ТЗ-B);
* язык постов в БД не переводится — перевод только в отчёте (Р1.6).

Ключей в коде нет: ключ берётся из окружения `DEEPSEEK_API_KEY`. Сеть — только
через канал `DeepSeekBroker` (`channels.py` -> `broker.raw_http_post`).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import config, store as db

TOPICS = tuple(config.RUBRIC_TOPICS)
CLAIM_TYPES = tuple(config.CLAIM_TYPES)

SYSTEM_PROMPT = (
    "Ты — тематический классификатор постов о разработке и применении ИИ. "
    "Решение принимаешь ты, а не список слов. Для КАЖДОГО поста верни объект:\n"
    "  idx — номер поста из входа (целое),\n"
    "  is_ai — 1, если пост про искусственный интеллект/машинное обучение/"
    "инструменты разработки с ИИ, иначе 0,\n"
    "  topic — ровно одна рубрика из списка (строка, как в списке),\n"
    "  subtopic — короткая тема по-русски, 1–3 слова,\n"
    "  claim_type — одно из: release, funding, research, opinion, news, howto, "
    "incident,\n"
    "  novelty — число от 0.0 до 1.0: насколько утверждение новое по сравнению "
    "с уже собранными за 7 дней,\n"
    "  lang — код языка поста (например ru, en),\n"
    "  title_ru — короткий заголовок по-русски, до 120 знаков,\n"
    "  summary_ru — 1 предложение по-русски, что за пост, до 200 знаков.\n"
    "title_ru и summary_ru — перевод смысла на русский (имена/модели/числа "
    "сохрани), а не транслитерация и не сам английский текст.\n"
    "Ответ — строго JSON вида {\"items\": [ ... ]} без пояснений. "
    "Обязательно верни ровно один объект на каждый idx без пропусков.\n"
    "Границы рубрик (важно, не путай по одному слову «новый/новинка»):\n"
    "  * «инструменты разработчика» — SDK, библиотеки, фреймворки, IDE, "
    "dev-tools, API для разработчиков;\n"
    "  * «инфраструктура и железо» — дата-центры, чипы, ускорители, питание/"
    "энергоснабжение, сети, верификация инфраструктуры. Пост про инфраструктуру "
    "или железо НЕ относится к «инструментам разработчика»;\n"
    "  * «релизы моделей» — выход новой модели/весов, а не запуск продукта или "
    "раунд инвестиций.\n"
    "Рубрики: __TOPICS__."
)


def _system_prompt():
    # Без str.format: в промпте есть литеральные фигурные скобки JSON.
    return SYSTEM_PROMPT.replace("__TOPICS__", "; ".join(TOPICS))


# ------------------------------------------------------------------ предфильтр
def prefilter_pass(text, links=""):
    """Грубый предфильтр (Р1.1): есть ли вообще ИИ-индикатор.

    Это НЕ классификация: прошедший пост всё равно уходит к модели, а
    непрошедший помечается `heuristic` и не называется `classified`.
    """
    hay = f"{text or ''} {links or ''}".lower()
    return any(ind in hay for ind in config.AI_TEXT_INDICATORS)


def text_hash_of(text):
    from .registry import text_hash
    return text_hash(text)


# ------------------------------------------------------------------ выборка
def _daily_row(con, day):
    return con.execute("SELECT * FROM classify_daily WHERE day=?", (day,)).fetchone()


def daily_used(con, day=None):
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = _daily_row(con, day)
    return int(row["posts"]) if row else 0


def _bump_daily(con, day, *, posts=0, model_calls=0, failed=0, prompt_tokens=0,
                completion_tokens=0, cost_usd=0.0):
    # Суммирующий UPSERT по дню переехал в триггер представления: SQLite
    # запрещает UPSERT по представлению, семантика та же (счётчики складываются).
    con.execute(
        """INSERT INTO classify_daily (day, posts, model_calls, failed,
             prompt_tokens, completion_tokens, cost_usd)
           VALUES (?,?,?,?,?,?,?)""",
        (day, posts, model_calls, failed, prompt_tokens, completion_tokens,
         round(float(cost_usd), 8)))
    con.commit()


def select_pending(con, limit=None, *, now=None):
    """Посты с текстом, у которых нет успешной записи в `classified`.

    Приоритет: свежие -> TIER-A -> остальные. Дубли текста отсекаются на
    уровне `text_hash`: классифицируется уникальный текст один раз (Р1.4).
    """
    q = ("SELECT p.tweet_id, p.text, p.text_hash, p.lang, p.published_at_utc,"
         " p.links, a.tier AS acc_tier, a.handle AS acc_handle"
         " FROM posts p JOIN accounts a ON a.id=p.account_id"
         " LEFT JOIN classified c ON c.text_hash=p.text_hash"
         " WHERE p.text IS NOT NULL AND length(p.text) > 0"
         " AND p.deleted_at IS NULL"
         " AND (c.text_hash IS NULL OR c.status='failed')"
         " ORDER BY (a.tier='A') DESC, p.published_at_utc DESC")
    rows = list(con.execute(q))
    out, seen = [], set()
    for r in rows:
        th = r["text_hash"] or text_hash_of(r["text"])
        if not th or th in seen:
            continue
        seen.add(th)
        out.append({"tweet_id": r["tweet_id"], "text": r["text"], "text_hash": th,
                    "lang": r["lang"], "published_at_utc": r["published_at_utc"],
                    "links": r["links"], "handle": r["acc_handle"],
                    "tier": r["acc_tier"]})
        if limit and len(out) >= int(limit):
            break
    return out


def recent_claims(con, *, days=7, limit=None, now=None):
    """Сжатый список уже классифицированных утверждений за 7 дней (для novelty)."""
    now = now or datetime.now(timezone.utc)
    since = db.iso(now - timedelta(days=days))
    limit = limit or config.CLASSIFY_RECENT_CLAIMS
    rows = con.execute(
        "SELECT DISTINCT topic, subtopic, claim_type FROM classified"
        " WHERE status='classified' AND classified_at >= ?"
        " ORDER BY classified_at DESC LIMIT ?", (since, limit)).fetchall()
    return [{"topic": r["topic"], "subtopic": r["subtopic"],
             "claim_type": r["claim_type"]} for r in rows]


# ------------------------------------------------------------------- промпт
def build_user_prompt(items, recent=None):
    lines = []
    if recent:
        lines.append("Уже собрано за 7 дней (для оценки novelty):")
        for r in recent:
            lines.append(f"  - {r.get('topic')} / {r.get('subtopic')} / "
                         f"{r.get('claim_type')}")
        lines.append("")
    lines.append("Посты (idx. текст):")
    for i, it in enumerate(items, 1):
        text = (it["text"] or "").strip()
        if len(text) > config.CLASSIFY_MAX_TEXT_CHARS:
            text = text[:config.CLASSIFY_MAX_TEXT_CHARS]
        text = re.sub(r"\s+", " ", text)
        lines.append(f"{i}. [{it.get('lang') or '?'}] {text}")
    return "\n".join(lines)


# ------------------------------------------------------------------ валидация
def _as_bool01(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "yes", "да") else 0
    return None


def _norm_topic(value):
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("_", "-").replace(" ", "-")
    v_spaced = value.strip().lower()
    for t in TOPICS:
        if v_spaced == t.lower():
            return t
    for slug, t in zip(config.RUBRIC_SLUGS, TOPICS):
        if v == slug.lower():
            return t
    for t in TOPICS:
        if v_spaced and (v_spaced in t.lower() or t.lower() in v_spaced):
            return t
    return None


def _norm_claim(value):
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in CLAIM_TYPES else None


def _norm_novelty(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f < 0.0 or f > 1.0:
        return None
    return round(f, 4)


def validate_response(raw, items):
    """Строгая проверка ответа модели (Р1.3).

    Возвращает (map_idx_fields | None, error). Индекс — 1-based, как в промпте.
    Требуется ровно один корректный объект на каждый idx.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        return None, f"не JSON: {e}"
    if not isinstance(data, dict):
        return None, "верхний уровень не объект"
    arr = data.get("items")
    if not isinstance(arr, list):
        return None, "нет массива items"
    want = set(range(1, len(items) + 1))
    out = {}
    for entry in arr:
        if not isinstance(entry, dict):
            return None, "элемент items не объект"
        try:
            idx = int(entry.get("idx"))
        except (TypeError, ValueError):
            return None, "idx не целое"
        if idx not in want:
            return None, f"лишний idx={idx}"
        is_ai = _as_bool01(entry.get("is_ai"))
        topic = _norm_topic(entry.get("topic"))
        claim = _norm_claim(entry.get("claim_type"))
        novelty = _norm_novelty(entry.get("novelty"))
        subtopic = entry.get("subtopic")
        subtopic = subtopic.strip() if isinstance(subtopic, str) else None
        lang = entry.get("lang")
        lang = lang.strip()[:8] if isinstance(lang, str) and lang.strip() else None
        if is_ai is None or topic is None or claim is None or novelty is None:
            return None, f"поля idx={idx} не прошли схему"
        if subtopic:
            subtopic = " ".join(subtopic.split()[:3])
        title_ru = entry.get("title_ru")
        title_ru = " ".join(title_ru.split())[:120] if isinstance(title_ru, str) \
            and title_ru.strip() else None
        summary_ru = entry.get("summary_ru")
        summary_ru = " ".join(summary_ru.split())[:200] if isinstance(summary_ru, str) \
            and summary_ru.strip() else None
        out[idx] = {"is_ai": is_ai, "topic": topic, "subtopic": subtopic,
                    "claim_type": claim, "novelty": novelty, "lang": lang,
                    "title_ru": title_ru, "summary_ru": summary_ru}
    missing = want - set(out)
    if missing:
        return None, f"нет объектов для idx={sorted(missing)}"
    return out, None


# ------------------------------------------------------------------- запись
def _upsert(con, *, text_hash, tweet_id, status, method, model, fields=None,
            attempts=1, error=None, usage=None):
    fields = fields or {}
    usage = usage or {}
    con.execute(
        """INSERT INTO classified (text_hash, tweet_id, is_ai, topic, subtopic,
             claim_type, novelty, lang, title_ru, summary_ru, status, method, model,
             attempts, error, prompt_tokens, completion_tokens, cost_usd, classified_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (text_hash, str(tweet_id) if tweet_id else None, fields.get("is_ai"),
         fields.get("topic"), fields.get("subtopic"), fields.get("claim_type"),
         fields.get("novelty"), fields.get("lang"), fields.get("title_ru"),
         fields.get("summary_ru"), status, method, model,
         int(attempts), error, usage.get("prompt_tokens"),
         usage.get("completion_tokens"), usage.get("cost_usd"),
         db.utcnow_iso()))
    con.commit()


def mark_heuristic(con, item, *, reason="prefilter"):
    """Предфильтр отсеял пост: помечаем `heuristic`, тема не выдумывается."""
    _upsert(con, text_hash=item["text_hash"], tweet_id=item["tweet_id"],
            status="heuristic", method="heuristic", model=None,
            fields={"is_ai": 0, "topic": None, "novelty": None,
                    "lang": item.get("lang")}, attempts=1, error=reason)


def _mark_failed(con, item, model, error, *, attempts):
    _upsert(con, text_hash=item["text_hash"], tweet_id=item["tweet_id"],
            status="failed", method="model", model=model, fields={},
            attempts=attempts, error=str(error)[:300])


# ------------------------------------------------------------------- прогон
def _default_client():
    from . import channels
    return channels.DeepSeekBroker()


def run(con, client=None, *, limit=None, batch=None, dry_run=False, run_id=None,
        now=None, use_prefilter=False, check_budget=True, model=None):
    """Прогон классификации. Идемпотентен по кэшу `text_hash`.

    ТЗ-6 задача 1: предфильтр по словам ВЫКЛЮЧЕН по умолчанию
    (`use_prefilter=False`). Замер заказчика 15.09.2026: предфильтр отсеивал
    12.5% постов, из них половину — по делу, а экономия составляла ~0.24 USD в
    месяц против реальной потери релевантного контента. Поэтому к модели уходят
    ВСЕ выбранные посты. Список `config.AI_TEXT_INDICATORS` работает только при
    явно включённом режиме (`--prefilter`); отсеянный пост помечается
    `heuristic` и не называется `classified`.

    Возвращает сводку: сколько выбрано/классифицировано/эвристик/отказов,
    сколько вызовов модели и стоимость. Повторный запуск на тех же данных
    даёт 0 вызовов модели (Р1.4, тест `test_classify_cache`).
    """
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    batch = int(batch or config.CLASSIFY_BATCH)
    model_name = model or config.DEEPSEEK_MODEL
    own_client = False
    if client is None:
        client = _default_client()
        own_client = True
        if run_id is not None:
            client.run_id = run_id

    summary = {"selected": 0, "unique_texts": 0, "classified": 0, "heuristic": 0,
               "failed": 0, "model_calls": 0, "cost_usd": 0.0, "batches": 0,
               "budget_ok": True, "budget_reason": None, "daily_cap": config.CLASSIFY_DAILY_CAP,
               "daily_used": daily_used(con, day), "dry_run": dry_run,
               "model": model_name, "detail": []}

    if check_budget:
        try:
            ok, reason, _details = client.budget_status()
        except Exception as e:  # noqa: BLE001
            ok, reason = True, f"бюджет не проверен ({type(e).__name__})"
        if not ok:
            summary["budget_ok"] = False
            summary["budget_reason"] = reason
            db.log_run(con, "WARN", f"classify: прогон остановлен — {reason}",
                       run_id=run_id)
            con.commit()
            if own_client:
                client.close()
            return summary

    left = config.CLASSIFY_DAILY_CAP - summary["daily_used"]
    if left <= 0:
        summary["budget_reason"] = (f"дневной потолок {config.CLASSIFY_DAILY_CAP} "
                                    f"постов исчерпан ({summary['daily_used']})")
        summary["budget_ok"] = False
        if own_client:
            client.close()
        return summary

    # ТЗ-B (G2): денежный предохранитель стадии. Если фактический расход дня по
    # `classify_daily.cost_usd` уже достиг `CLASSIFY_DAILY_USD_CAP`, прогон
    # останавливается мягко (без исключения): посты остаются в pending и
    # повторятся в следующем прогоне. 0 и меньше — предохранитель выключен.
    usd_cap = float(config.CLASSIFY_DAILY_USD_CAP)
    if usd_cap > 0:
        spent_row = _daily_row(con, day)
        spent = float(spent_row["cost_usd"] or 0.0) if spent_row else 0.0
        if spent >= usd_cap:
            summary["budget_reason"] = (f"дневной $ лимит стадии {usd_cap} "
                                        f"исчерпан (${spent:.6f})")
            summary["budget_ok"] = False
            db.log_run(con, "WARN",
                       f"classify: прогон остановлен — {summary['budget_reason']}",
                       run_id=run_id)
            con.commit()
            if own_client:
                client.close()
            return summary

    want = min(int(limit) if limit else left, left)
    pending = select_pending(con, limit=want, now=now)
    summary["selected"] = len(pending)
    summary["unique_texts"] = len(pending)
    if not pending or dry_run:
        summary["detail"] = [{"tweet_id": p["tweet_id"], "handle": p["handle"]}
                             for p in pending[:20]]
        if own_client:
            client.close()
        return summary

    recent = recent_claims(con, now=now)
    heuristic_items, model_items = [], []
    for it in pending:
        if use_prefilter and not prefilter_pass(it["text"], it.get("links") or ""):
            heuristic_items.append(it)
        else:
            model_items.append(it)

    if heuristic_items and not dry_run:
        for it in heuristic_items:
            mark_heuristic(con, it)
        summary["heuristic"] = len(heuristic_items)
        _bump_daily(con, day, posts=len(heuristic_items))

    for start in range(0, len(model_items), batch):
        chunk = model_items[start:start + batch]
        summary["batches"] += 1
        user = build_user_prompt(chunk, recent=recent)
        parsed, err = None, None
        attempts = 0
        prompt = user
        temperature = None
        for attempt in range(config.CLASSIFY_RETRIES + 1):
            attempts += 1
            try:
                res = client.classify(_system_prompt(), prompt,
                                      temperature=temperature)
            except Exception as e:  # noqa: BLE001 — канал может отказать
                err = f"{type(e).__name__}: {e}"
                summary["failed"] += len(chunk)
                for it in chunk:
                    _mark_failed(con, it, model_name, err, attempts=attempts)
                db.log_run(con, "ERROR", f"classify: канал отказал — {err}",
                           run_id=run_id)
                con.commit()
                _bump_daily(con, day, posts=len(chunk), failed=len(chunk))
                parsed = None
                err = None
                break
            content = res.get("content") if isinstance(res, dict) else res
            usage = (res.get("usage") if isinstance(res, dict) else None) or {}
            summary["model_calls"] += 1
            summary["cost_usd"] += float(usage.get("cost_usd") or 0.0)
            parsed, err = validate_response(content, chunk)
            _bump_daily(con, day, model_calls=1,
                        prompt_tokens=usage.get("prompt_tokens") or 0,
                        completion_tokens=usage.get("completion_tokens") or 0,
                        cost_usd=usage.get("cost_usd") or 0.0)
            if parsed is not None:
                break
            db.log_run(con, "WARN",
                       f"classify: невалидный ответ (попытка {attempt + 1}): {err}",
                       run_id=run_id)
            con.commit()
            # Повтор осмысленен только с ненулевой температурой и прямой правкой.
            temperature = config.CLASSIFY_RETRY_TEMPERATURE
            prompt = (user + "\n\nПредыдущий ответ не прошёл проверку схемы ("
                      + str(err) + "). Верни строго JSON с массивом items: ровно "
                      "один объект на каждый idx, topic — строка из списка рубрик, "
                      "claim_type — из допустимых, novelty — число 0..1.")
        if parsed is None:
            if err is not None:
                summary["failed"] += len(chunk)
                for it in chunk:
                    _mark_failed(con, it, model_name, err, attempts=attempts)
                _bump_daily(con, day, posts=len(chunk), failed=len(chunk))
                con.commit()
            continue
        for i, it in enumerate(chunk, 1):
            fields = parsed[i]
            _upsert(con, text_hash=it["text_hash"], tweet_id=it["tweet_id"],
                    status="classified", method="model", model=model_name,
                    fields=fields,
                    usage={"cost_usd": 0.0})  # токены учтены выше
        summary["classified"] += len(chunk)
        _bump_daily(con, day, posts=len(chunk))
        for i, it in enumerate(chunk, 1):
            summary["detail"].append({"tweet_id": it["tweet_id"],
                                      "handle": it["handle"], **parsed[i]})
    summary["daily_used"] = daily_used(con, day)
    if own_client:
        client.close()
    return summary


def coverage(con, *, since_days=None, now=None):
    """Доля постов с непустым `topic` (для П1 и отчёта)."""
    now = now or datetime.now(timezone.utc)
    params, q = [], ("SELECT COUNT(*) FROM posts p JOIN classified c"
                     " ON c.text_hash=p.text_hash"
                     " WHERE p.deleted_at IS NULL AND c.status='classified'")
    if since_days:
        q += " AND p.published_at_utc >= ?"
        params.append(db.iso(now - timedelta(days=since_days)))
    with_topic = con.execute(q + " AND c.topic IS NOT NULL", params).fetchone()[0]
    classified = con.execute(q, params).fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM posts WHERE deleted_at IS NULL"
                        ).fetchone()[0]
    heur = con.execute("SELECT COUNT(*) FROM classified WHERE status='heuristic'"
                       ).fetchone()[0]
    failed = con.execute("SELECT COUNT(*) FROM classified WHERE status='failed'"
                         ).fetchone()[0]
    return {"total_posts": total, "classified": classified, "with_topic": with_topic,
            "heuristic": heur, "failed": failed,
            "ratio": (with_topic / classified) if classified else None}
