"""ТЗ-18 — приём кандидатов X из внешних фидов (описания YouTube / посты Telegram).

Оба проекта-экспортёра (tuber-os и tuber-telegram) пишут файл по договору
`docs/EXCHANGE-FEED.md`: поля `kind`, `handle`, `mentions` (число), `videos`
(число), `video_ids` (список), `ai_hint`, `first_seen`, `last_seen`, `source`
(строка), `sources` (список), `examples` (список строк), `exported_at`.

Терпимость к формату (ТЗ-21/B). Договор пишет `videos` ЧИСЛОМ (tuber-os), а
старый фид tuber-telegram писал СПИСКОМ; `sources` пишется списком, но может
прийти строкой или отсутствовать; `examples` — списком строк или объектов.
Потребитель обязан принять оба вида: негодное значение поля не роняет строку,
а считается в счётчике `bad_fields` — импорт продолжается.

Этот модуль — только НАПОЛНЕНИЕ очереди `candidates`. Он принципиально:
  * не ходит в сеть (никакой верификации, никакого сбора);
  * не регистрирует аккаунты: пороги регистрации остаются единственным путём
    (`candidates` -> `accounts` только через `registry.verify_account`);
  * НИКОГДА не меняет `validated`, `reject_reason`, `verified_at` и `llm_checked`
    у существующих кандидатов: `reject` остаётся `reject`, `ok`/`provisional`
    не понижаются.

Терпимость к чужому формату: строка чужого `kind` игнорируется со счётчиком
(`skipped_kinds`), битая строка пропускается со счётчиком (`bad_lines`), импорт
продолжается. Ошибкой считается только отсутствие файла (не ошибка —
`feeds_missing`), пустой файл и файл, где ВСЕ строки битые.
"""
from __future__ import annotations

import json
import math
import os

from . import blocklist, config, store as db, registry

WANTED_KIND = "x"

# Причины отсева (порядок применения). Всегда присутствуют в отчёте, с нулями.
SKIP_REASONS = (
    "bad_handle",
    "service_handle",
    "blocklist",
    "already_registered",
    "bot",
    "news_giant",
    "below_mention_threshold",
)


class FeedError(Exception):
    """Фид непригоден: пуст или все строки битые. Импорт отменяется."""


# --------------------------------------------------------------------- фид-метки
def feed_tag(path):
    """Короткая метка фида-источника (например `tuber-os`, `tuber-telegram`).

    Берём имя проекта из пути `<проект>/data/exchange/<файл>`; для произвольного
    пути — имя каталога-родителя.
    """
    real = os.path.realpath(path)
    parts = real.split(os.sep)
    if "exchange" in parts:
        i = parts.index("exchange")
        if i - 2 >= 1:
            return parts[i - 2]
    parent = os.path.basename(os.path.dirname(real))
    if parent:
        return parent
    return os.path.splitext(os.path.basename(real))[0]


def found_via_token(tag, path):
    """Значение для `found_via`: `yt_desc:<проект>` или `tg_posts:<проект>`."""
    low = (tag or "").lower()
    name = os.path.basename(path or "").lower()
    if "telegram" in low or low.split("-")[-1] == "tg" or "telegram" in name:
        return f"tg_posts:{tag}"
    if low.endswith("os") or "youtube" in low or "yt" in low.split("-") or "desc" in low:
        return f"yt_desc:{tag}"
    return f"feed:{tag}"


# ------------------------------------------------------------------ разбор строк
def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _num(value):
    """Число из значения (int/float/строка с числом). ``None`` — тип негоден."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _id_str(value):
    """Строковый id из значения-строки, числа, объекта или списка (ТЗ-21/B)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("id", "video_id", "url", "link", "video_url", "text"):
            got = _id_str(value.get(key))
            if got:
                return got
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            got = _id_str(item)
            if got:
                return got
        return None
    return None


def _videos_field(row, bad):
    """Вернуть (число разных видео, список id). Терпимо к чужому типу.

    ТЗ-21/B: договор пишет `videos` ЧИСЛОМ (tuber-os), а старый фид
    tuber-telegram писал списком. Оба вида принимаются: список трактуется как
    перечень id. Если есть явное `video_ids` — оно приоритетно. Негодный тип
    поля не роняет импорт, а считается в ``bad``.
    """
    ids = []
    count = None
    raw = row.get("videos")
    if raw is None:
        pass
    elif isinstance(raw, (list, tuple)):
        for v in raw:
            vid = _id_str(v)
            if vid and vid not in ids:
                ids.append(vid)
        count = len(ids)
    else:
        num = _num(raw)
        if num is None or num < 0:
            bad.append("videos")
        else:
            count = num
    vi = row.get("video_ids")
    if vi is None:
        pass
    elif isinstance(vi, (list, tuple)):
        got = []
        for v in vi:
            vid = _id_str(v)
            if vid and vid not in got:
                got.append(vid)
        if got or not ids:
            ids = got
    elif isinstance(vi, str):
        if vi.strip():
            ids = [vi.strip()]
    else:
        bad.append("video_ids")
    if count is None:
        count = len(ids)
    return int(count), ids


def _src_names(row, bad):
    """Оба поля источника: `sources` (список/строка) и `source` (строка).

    ТЗ-21/B: `sources` может быть списком, строкой или отсутствовать (тогда
    берётся `source`). Негодный тип считается в ``bad``.
    """
    out = []
    raw = row.get("sources")
    if raw is None:
        pass
    elif isinstance(raw, str):
        out.extend(p.strip() for p in raw.split(",") if p.strip())
    elif isinstance(raw, (list, tuple)):
        local_bad = False
        for s in raw:
            name = _id_str(s)
            if name:
                out.append(name)
            elif s is not None:
                local_bad = True
        if local_bad:
            bad.append("sources")
    else:
        bad.append("sources")
    single = row.get("source")
    if isinstance(single, str):
        if single.strip():
            out.append(single.strip())
    elif single is not None:
        bad.append("source")
    seen = []
    for s in out:
        if s not in seen:
            seen.append(s)
    return seen


def _examples(row, bad):
    """Список строк-цитат: список строк, список объектов (`text`) или строка."""
    raw = row.get("examples")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        out = []
        local_bad = False
        for item in raw:
            if isinstance(item, str):
                if item.strip():
                    out.append(item)
            elif isinstance(item, dict):
                text = _id_str(item.get("text") or item.get("quote")
                               or item.get("snippet"))
                if text:
                    out.append(text)
                else:
                    local_bad = True
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                out.append(str(item))
            else:
                local_bad = True
        if local_bad:
            bad.append("examples")
        return out
    bad.append("examples")
    return []


def _pick_iso(a, b, *, newest):
    da, dbb = db.parse_iso(a), db.parse_iso(b)
    if da and dbb:
        return db.iso(max(da, dbb) if newest else min(da, dbb))
    if da:
        return db.iso(da)
    if dbb:
        return db.iso(dbb)
    return None


def read_feed(path, wanted=WANTED_KIND):
    """Прочитать JSONL-фид. Вернуть dict со строками и счётчиками.

    Пустой файл или все строки битые — `FeedError`.
    """
    total = bad = skipped_kinds = 0
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                bad += 1
                continue
            if not isinstance(data, dict):
                bad += 1
                continue
            if str(data.get("kind") or "").lower() != str(wanted).lower():
                skipped_kinds += 1
                continue
            rows.append(data)
    if total == 0:
        raise FeedError(f"фид пуст: {path}")
    if bad == total:
        raise FeedError(f"все строки фида битые ({bad} из {total}): {path}")
    return {"rows": rows, "total": total, "bad": bad, "skipped_kinds": skipped_kinds}


# ------------------------------------------------------------------ фильтр
def reject_reason(con, handle, mentions, accounts):
    """Причина отсева хендла или None, если кандидат проходит."""
    if handle in blocklist.SERVICE_HANDLES:
        return "service_handle"
    if blocklist.is_blocked(con, handle):
        return "blocklist"
    if handle in accounts:
        return "already_registered"
    if registry.is_spam_handle(handle):
        return "bot"
    if handle in config.NEWS_GIANTS_SET:
        return "news_giant"
    if _int(mentions) < config.VERIFY_MIN_MENTIONS:
        return "below_mention_threshold"
    return None


def _source_keys(tag, bucket):
    keys = []
    for name in sorted(bucket.get("sources") or ()):
        keys.append(f"feed:{tag}:src:{name}")
    for vid in sorted(bucket.get("videos") or ()):
        keys.append(f"feed:{tag}:video:{vid}")
    if not keys:
        keys.append(f"feed:{tag}")
    return keys


def _append_tokens(existing, new_tokens):
    parts = [p.strip() for p in str(existing or "").split(",") if p.strip()]
    for t in new_tokens:
        if t and t not in parts:
            parts.append(t)
    return ", ".join(parts)


# ------------------------------------------------------------------ импорт
def import_candidates(con, paths, *, limit=None, dry=False, wanted=WANTED_KIND):
    """Принять кандидатов `kind="x"` из фидов. Вернуть отчёт прогона.

    `limit` — максимум ПРИНЯТЫХ кандидатов за прогон (по умолчанию из конфига),
    отбор идёт по `mentions` desc, затем `videos` desc, затем `ai_hint`.
    `dry=True` — всё считается, но транзакция откатывается (в БД не пишем).
    """
    limit = config.FEED_IMPORT_LIMIT if limit is None else int(limit)
    feeds, feeds_missing = [], []
    combined = {}
    counters = {r: 0 for r in SKIP_REASONS}
    bad_fields_total = 0

    for path in paths or []:
        if not os.path.isfile(path):
            feeds_missing.append(path)
            continue
        parsed = read_feed(path, wanted=wanted)   # FeedError -> наружу
        tag = feed_tag(path)
        token = found_via_token(tag, path)
        feed_bad_fields = 0
        for row in parsed["rows"]:
            bad = []
            handle, err = registry.validate_handle(row.get("handle"))
            if err:
                counters["bad_handle"] += 1
                continue
            # --- терпимое чтение полей (ТЗ-21/B): чужой тип не роняет строку ---
            raw_mentions = row.get("mentions")
            mentions = _num(raw_mentions) if raw_mentions is not None else None
            if raw_mentions is not None and (mentions is None or mentions < 0):
                bad.append("mentions")
                mentions = 0
            mentions = mentions or 0
            video_count, video_ids = _videos_field(row, bad)
            src_names = _src_names(row, bad)
            _examples(row, bad)                    # проверяется формат примеров
            raw_hint = row.get("ai_hint")
            if raw_hint is None:
                hint = 0
            else:
                hint_num = _num(raw_hint)
                if hint_num is None:
                    bad.append("ai_hint")
                    hint = 0
                else:
                    hint = 1 if hint_num else 0
            feed_bad_fields += len(bad)

            e = combined.get(handle)
            if e is None:
                e = combined[handle] = {
                    "handle": handle, "mentions": 0, "ai_hint": 0,
                    "first_seen": None, "last_seen": None,
                    "tags": [], "tokens": [], "by_tag": {},
                }
            e["mentions"] += mentions
            e["ai_hint"] = max(e["ai_hint"], hint)
            e["first_seen"] = _pick_iso(e["first_seen"], row.get("first_seen"), newest=False)
            e["last_seen"] = _pick_iso(e["last_seen"], row.get("last_seen"), newest=True)
            if tag not in e["tags"]:
                e["tags"].append(tag)
            if token not in e["tokens"]:
                e["tokens"].append(token)
            bucket = e["by_tag"].setdefault(
                tag, {"sources": set(), "videos": set(), "video_count": 0})
            bucket["sources"].update(src_names)
            bucket["videos"].update(video_ids)
            bucket["video_count"] = max(bucket.get("video_count") or 0, video_count)
        bad_fields_total += feed_bad_fields
        feeds.append({"path": path, "feed_tag": tag, "rows": parsed["total"],
                      "x_rows": len(parsed["rows"]),
                      "skipped_kinds": parsed["skipped_kinds"],
                      "bad_lines": parsed["bad"],
                      "bad_fields": feed_bad_fields})

    accounts = {r[0] for r in con.execute("SELECT handle FROM accounts")}
    accepted = []
    for e in combined.values():
        reason = reject_reason(con, e["handle"], e["mentions"], accounts)
        if reason:
            counters[reason] += 1
            continue
        accepted.append(e)

    # Приоритет очереди: вперёд идут самые подтверждённые.
    accepted.sort(key=lambda e: (-e["mentions"], -_candidate_videos(e),
                                 -e["ai_hint"], e["handle"]))
    if limit >= 0 and len(accepted) > limit:
        skipped_limit = len(accepted) - limit
        accepted = accepted[:limit]
    else:
        skipped_limit = 0

    imported_new = merged = 0
    now = db.utcnow_iso()
    for e in accepted:
        handle = e["handle"]
        new_keys = []
        for tag, bucket in e["by_tag"].items():
            for key in _source_keys(tag, bucket):
                if key not in new_keys:
                    new_keys.append(key)
        row = con.execute("SELECT * FROM candidates WHERE handle=?", (handle,)).fetchone()
        if row is None:
            found_via = ", ".join(e["tokens"])
            feed_source = ", ".join(e["tags"])
            priority = registry.candidate_priority(
                con, handle, distinct_sources=len(new_keys), found_via=found_via,
                rubric=None, sources=new_keys)
            con.execute(
                """INSERT INTO candidates
                     (handle, found_via, found_in_account, seen_count, distinct_sources,
                      first_seen_at, last_seen_at, validated, reject_reason, llm_checked,
                      sources, priority, rubric, lang_guess, spam, promoted_by,
                      verified_at, ai_hint, feed_source)
                   VALUES (?,?,?,?,?,?,?,NULL,NULL,0,?,?,NULL,NULL,?,NULL,NULL,?,?)""",
                (handle, found_via, e["tags"][0], e["mentions"], len(new_keys),
                 e["first_seen"] or now, e["last_seen"] or now,
                 json.dumps(new_keys, ensure_ascii=False), priority,
                 registry.candidate_spam(handle), e["ai_hint"], feed_source))
            imported_new += 1
            continue

        # --- слияние: обновляем только «фактовые» поля, статус не трогаем ---
        merged += 1
        existing_sources = []
        if row["sources"]:
            try:
                existing_sources = json.loads(row["sources"])
            except (ValueError, TypeError):
                existing_sources = []
        for key in new_keys:
            if key not in existing_sources:
                existing_sources.append(key)
        orig_found_via = row["found_via"] or ""
        found_via = _append_tokens(orig_found_via, e["tokens"])
        feed_source = _append_tokens(row["feed_source"] if _has(row, "feed_source") else None,
                                     e["tags"])
        first_seen = _pick_iso(row["first_seen_at"], e["first_seen"], newest=False)
        last_seen = _pick_iso(row["last_seen_at"], e["last_seen"], newest=True)
        seen = _int(row["seen_count"]) + e["mentions"]
        ai_hint = max(_int(row["ai_hint"]) if _has(row, "ai_hint") else 0, e["ai_hint"])
        priority = registry.candidate_priority(
            con, handle, distinct_sources=len(existing_sources),
            found_via=orig_found_via or found_via, rubric=row["rubric"],
            sources=existing_sources)
        con.execute(
            """UPDATE candidates
                 SET seen_count=?, distinct_sources=?, first_seen_at=?, last_seen_at=?,
                     found_via=?, sources=?, priority=?, spam=?, ai_hint=?, feed_source=?
               WHERE handle=?""",
            (seen, len(existing_sources), first_seen, last_seen, found_via,
             json.dumps(existing_sources, ensure_ascii=False), priority,
             registry.candidate_spam(handle), ai_hint, feed_source, handle))

    queue_total = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    if dry:
        con.rollback()
    else:
        con.commit()

    return {
        "feeds": feeds,
        "feeds_missing": feeds_missing,
        "imported_new": imported_new,
        "merged": merged,
        "skipped_filter": counters,
        "bad_fields": bad_fields_total,
        "skipped_limit": skipped_limit,
        "queue_total": queue_total,
        "dry": bool(dry),
    }


def _has(row, column):
    """Есть ли колонка в строке (страховка на недо-мигрированной базе)."""
    try:
        return column in row.keys()
    except (AttributeError, IndexError):
        return False


# Значение для сортировки: общее число видео по всем фидам-источникам.
def _candidate_videos(e):
    return sum(max(int(b.get("video_count") or 0), len(b.get("videos") or ()))
               for b in e["by_tag"].values())


def horizon_days(queue_total, checks_per_day=None, requests_per_day=None):
    """Честный горизонт проверки очереди при текущем суточном бюджете."""
    checks = int(checks_per_day if checks_per_day is not None
                 else config.DISCOVERY_MAX_VERIFY_DEFAULT) or 1
    return int(math.ceil(int(queue_total) / checks))


FEED_KIND = "x"
FEED_SOURCE = "tuber-x:candidates"


def export_queue(con, path=None, *, limit=None):
    """Выгрузить очередь `candidates` в канонический JSONL-фид (ТЗ-21/C).

    tuber-x — потребитель моста, но договор `docs/EXCHANGE-FEED.md` обязателен
    для всех трёх проектов: то, что проект отдаёт наружу, должно читаться по тем
    же правилам. Экспорт только читает: ни реестр, ни очередь не меняются.

    Возвращает сводку (written, out, total, top).
    """
    rows = []
    for row in con.execute(
            "SELECT * FROM candidates ORDER BY priority DESC, seen_count DESC, handle"):
        sources = []
        if _has(row, "sources") and row["sources"]:
            try:
                sources = [str(s) for s in json.loads(row["sources"])]
            except (ValueError, TypeError):
                sources = []
        video_ids = sorted({k.split(":video:", 1)[1] for k in sources
                            if ":video:" in k})
        found_via = row["found_via"] if _has(row, "found_via") else None
        rows.append({
            "kind": FEED_KIND,
            "handle": row["handle"],
            "mentions": _int(row["seen_count"]) if _has(row, "seen_count") else 0,
            "videos": len(video_ids),
            "video_ids": video_ids,
            "sources": sources,
            "source": FEED_SOURCE,
            "examples": [],
            "ai_hint": _int(row["ai_hint"]) if _has(row, "ai_hint") else 0,
            "found_via": found_via,
            "first_seen": row["first_seen_at"] if _has(row, "first_seen_at") else None,
            "last_seen": row["last_seen_at"] if _has(row, "last_seen_at") else None,
        })
    if limit is not None and int(limit) >= 0:
        rows = rows[: int(limit)]
    written = 0
    if path:
        target = os.path.abspath(path)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                written += 1
        os.replace(tmp, target)
    return {
        "written": written,
        "out": os.path.abspath(path) if path else None,
        "total": len(rows),
        "top": [{"handle": r["handle"], "mentions": r["mentions"]} for r in rows[:5]],
    }


def feed_influx(con):
    """Сколько кандидатов пришло из описаний YouTube и из постов Telegram.

    Возвращает dict с числами, пригодный для суточной сводки. Устойчив к
    недо-мигрированной базе (нет колонки `feed_source`) — тогда нули.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(candidates)")}
    out = {"yt_desc": 0, "tg_posts": 0, "queue_total": 0,
           "checks_per_day": config.DISCOVERY_MAX_VERIFY_DEFAULT,
           "requests_per_day": config.DISCOVERY_DAILY_BUDGET, "horizon_days": 0}
    out["queue_total"] = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    if "feed_source" in cols:
        out["yt_desc"] = con.execute(
            "SELECT COUNT(*) FROM candidates WHERE feed_source LIKE '%tuber-os%'"
        ).fetchone()[0]
        out["tg_posts"] = con.execute(
            "SELECT COUNT(*) FROM candidates WHERE feed_source LIKE '%tuber-telegram%'"
        ).fetchone()[0]
    out["horizon_days"] = horizon_days(out["queue_total"], out["checks_per_day"])
    return out
