"""Точные подписчики Telegram через MTProto (ТЗ-B10, D-62 + D-68).

Зачем
-----
Два остатка сходятся в ОДИН проход, а не в два механизма:

* **D-62.** Превью ``t.me/<handle>`` умеет отвечать JS-оболочкой (антибот), и
  тогда для вердикта берётся ОКРУГЛЁННОЕ число из ленты ``/s/`` (``10.7K``).
  Для ряда роста подписчиков этого мало.
* **D-68.** У части кандидатов Telegram ``source.subs`` пуст вовсе, поэтому
  ``vr`` не вычислить и канал не проходит порог повышения.

Оба случая закрывает MTProto-резолв: ``get_entity(handle)`` +
``GetFullChannelRequest`` → ``full_chat.participants_count`` — ТОЧНОЕ число.

Границы
-------
* не более :data:`~config.EXACT_SUBS_MAX_PER_RUN` (1000) каналов за прогон;
* пауза между MTProto-запросами (:data:`~config.EXACT_SUBS_PAUSE_SEC`);
* при ``FloodWaitError`` проход немедленно останавливается, число сделанных
  запросов попадает в отчёт;
* сеть изолирована: ``client`` можно подставить (фейк) — тогда Telethon и
  учётка не нужны; разбор ответа (:func:`parse_full_channel`) чистый.

CLI::

    python3 -m tuber tg exact-subs [--db PATH] [--limit N] [--dry]
                                   [--allow-production] [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

from . import config
from . import store as db
from . import promote as tg_promote

#: Подстроки в тексте ошибки, означающие «канал не найден/недоступен».
_NOT_FOUND_MARKERS = (
    "cannot find", "no user has", "username not occupied", "cannot resolve",
    "nobody is using", "channelinvalid", "channelprivate", "invalidusername",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _norm(handle) -> str:
    h = (handle or "").strip()
    if h.startswith("@"):
        h = h[1:]
    if h.lower().startswith("t.me/"):
        h = h[len("t.me/"):]
    return h.strip().lstrip("@").lower()


def _is_flood(exc: BaseException) -> bool:
    """Похоже ли исключение на FloodWait (без жёсткой зависимости от Telethon)."""
    if "FloodWait" in type(exc).__name__:
        return True
    try:
        from telethon.errors import FloodWaitError
    except Exception:  # noqa: BLE001 — Telethon не установлен
        return False
    return isinstance(exc, FloodWaitError)


def _looks_not_found(message: str) -> bool:
    low = (message or "").lower()
    return any(m in low for m in _NOT_FOUND_MARKERS)


def parse_full_channel(handle, entity, full_chat) -> dict:
    """Чистый разбор ответа MTProto (D-62): точные подписчики и метаданные.

    Без сети и без Telethon: принимает любые объекты с нужными атрибутами,
    поэтому тестируется на заготовке.
    """
    raw_subs = getattr(full_chat, "participants_count", None)
    try:
        subs = int(raw_subs) if raw_subs is not None else None
    except (TypeError, ValueError):
        subs = None
    return {
        "handle": handle,
        "tg_id": getattr(entity, "id", None),
        "subs": subs,
        "title": getattr(entity, "title", None),
        "about": getattr(full_chat, "about", None),
    }


async def _resolve_async(handles, *, client, limit: int, pause: float,
                         sleep, log=None) -> tuple[dict, dict]:
    """Асинхронный резолв списка ``handles`` (сеть изолирована ``client``)."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    results: dict[str, dict] = {}
    stats = {"requested": len(handles), "resolved": 0, "not_found": 0,
             "errors": 0, "done": 0, "flood_wait": None, "stopped": None}
    done = 0
    for handle in handles:
        if done >= max(0, int(limit)):
            stats["stopped"] = "limit"
            break
        try:
            entity = await client.get_entity(handle)
            full = await client(GetFullChannelRequest(entity))
            results[handle] = parse_full_channel(handle, entity, full.full_chat)
            stats["resolved"] += 1
        except Exception as exc:  # noqa: BLE001 — один канал не роняет проход
            if _is_flood(exc):
                stats["flood_wait"] = int(getattr(exc, "seconds", 0) or 0)
                stats["stopped"] = "flood_wait"
                if log:
                    log(handle, f"FLOOD_WAIT {stats['flood_wait']}s — стоп прохода")
                break
            message = f"{type(exc).__name__}: {exc}"
            if _looks_not_found(message):
                results[handle] = {"handle": handle, "subs": None, "error": "not_found"}
                stats["not_found"] += 1
            else:
                results[handle] = {"handle": handle, "subs": None,
                                   "error": message[:150]}
                stats["errors"] += 1
            if log:
                log(handle, message[:150])
        done += 1
        stats["done"] = done
        if pause:
            sleep(pause)
    return results, stats


def resolve_handles(handles, *, client=None, limit: int | None = None,
                    pause: float | None = None, sleep=None,
                    log=None) -> tuple[dict, dict]:
    """Резолв точных подписчиков через MTProto.

    ``client`` — асинхронный Telethon-совместимый объект (фейк в тестах). Если
    не передан, собирается из учётки контура (сессия + ключи ``.env``).
    """
    import time as _time

    limit = config.EXACT_SUBS_MAX_PER_RUN if limit is None else int(limit)
    pause = config.EXACT_SUBS_PAUSE_SEC if pause is None else float(pause)
    sleep = _time.sleep if sleep is None else sleep
    handles = list(handles)
    if client is not None:
        return asyncio.run(_resolve_async(handles, client=client, limit=limit,
                                          pause=pause, sleep=sleep, log=log))

    from .comments import make_telethon_client

    async def _run():
        c = make_telethon_client()
        await c.connect()
        try:
            if not await c.is_user_authorized():
                raise RuntimeError("сессия Telethon не авторизована")
            return await _resolve_async(handles, client=c, limit=limit,
                                        pause=pause, sleep=sleep, log=log)
        finally:
            await c.disconnect()

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Отбор целей и запись результата
# ---------------------------------------------------------------------------

def _review_evidence(meta_json) -> dict:
    if not meta_json:
        return {}
    try:
        meta = json.loads(meta_json)
    except (TypeError, ValueError):
        return {}
    review = (meta or {}).get("review") or {}
    return review.get("evidence") or {}


def collect_targets(con, limit: int | None = None) -> tuple[list[str], dict]:
    """Кого резолвить: кандидаты с округлёнными subs (D-62) + без subs (D-68).

    Возвращает ``(handles, targets)``; ``targets[handle]`` несёт id строк
    очереди/реестра и (для D-62) ранее округлённое число — базу расхождения.
    """
    targets: dict[str, dict] = {}

    def bucket(handle):
        h = _norm(handle)
        return targets.setdefault(h, {"handle": h, "candidate_ids": [],
                                      "source_ids": [], "rounded": None})

    # D-62: кандидаты очереди, где превью отдало JS-оболочку (округлённые subs).
    for r in con.execute(
            "SELECT id, handle, meta_json FROM candidate WHERE platform='telegram' "
            "AND json_extract(meta_json,'$.review.evidence.subs_source')='rounded'"):
        t = bucket(r["handle"])
        t["candidate_ids"].append(r["id"])
        ev = _review_evidence(r["meta_json"])
        if t["rounded"] is None and ev.get("subs") is not None:
            try:
                t["rounded"] = int(ev["subs"])
            except (TypeError, ValueError):
                pass
    # D-68: кандидаты реестра без подписчиков.
    for r in con.execute(
            "SELECT id, handle FROM source WHERE platform='telegram' "
            "AND status='candidate' AND (subs IS NULL OR subs <= 0)"):
        bucket(r["handle"])["source_ids"].append(r["id"])

    handles = sorted(targets)
    if limit is not None:
        handles = handles[: max(0, int(limit))]
    return handles, targets


def _update_candidate_evidence(con, cand_id: int, subs: int, res: dict) -> None:
    """Записать точное число в ``candidate.meta_json.review.evidence`` (D-62)."""
    row = con.execute("SELECT meta_json FROM candidate WHERE id=?", (cand_id,)).fetchone()
    meta: dict = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0])
        except (TypeError, ValueError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
    ev = review.get("evidence") if isinstance(review.get("evidence"), dict) else {}
    ev["subs_rounded"] = ev.get("subs")
    ev["subs"] = subs
    ev["subs_source"] = "exact"
    ev["subs_exact_at"] = _now_iso()
    if res.get("tg_id") is not None:
        ev["tg_id"] = res.get("tg_id")
    review["evidence"] = ev
    meta["review"] = review
    con.execute("UPDATE candidate SET meta_json=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), cand_id))


def apply_exact_subs(con, results: dict, targets: dict, *, now: str | None = None,
                     min_vr: float | None = None) -> dict:
    """Записать точные subs и пересчитать ``vr`` (D-62 + D-68). Идемпотентно.

    Точное число пишется и в очередь (``candidate.meta_json``), и в реестр
    (``source.subs``). Для строк реестра сразу считается ``vr`` фактическими
    постами — тем же :func:`tuber.platforms.telegram.promote.compute_vr`.
    """
    stamp = now or _now_iso()
    threshold = config.PROMOTE_MIN_VR if min_vr is None else float(min_vr)
    out = {"filled_candidates": 0, "filled_sources": 0, "divergences": [],
           "vr_ok": 0, "vr_no_views": 0, "vr_no_subs": 0, "now_pass_vr": 0,
           "exact_subs": {}}
    for handle, res in results.items():
        subs = res.get("subs")
        if subs is None:
            continue
        t = targets.get(handle) or {}
        out["exact_subs"][handle] = subs
        for cid in t.get("candidate_ids", []):
            _update_candidate_evidence(con, cid, subs, res)
            out["filled_candidates"] += 1
            rounded = t.get("rounded")
            if rounded is not None:
                out["divergences"].append((int(subs), int(rounded)))
        for sid in t.get("source_ids", []):
            con.execute("UPDATE source SET subs=?, subs_at=? WHERE id=?",
                        (subs, stamp, sid))
            out["filled_sources"] += 1
            vr = tg_promote.compute_vr(con, sid, subs)
            if vr["state"] == "ok":
                out["vr_ok"] += 1
                con.execute(
                    "UPDATE source SET vr=?, avg_views=? WHERE id=?",
                    (vr["vr"],
                     int(vr["views_median"]) if vr["views_median"] else None, sid))
                if vr["vr"] is not None and vr["vr"] >= threshold:
                    out["now_pass_vr"] += 1
            elif vr["state"] == "no_views":
                out["vr_no_views"] += 1
            else:
                out["vr_no_subs"] += 1
    con.commit()
    return out


def _divergence_stats(pairs: list[tuple[int, int]]) -> dict:
    if not pairs:
        return {"n": 0, "mean": None, "max": None, "mean_pct": None}
    diffs = [abs(e - r) for e, r in pairs]
    pct = [abs(e - r) / r * 100.0 for e, r in pairs if r]
    return {
        "n": len(diffs),
        "mean": round(sum(diffs) / len(diffs), 1),
        "max": max(diffs),
        "mean_pct": round(sum(pct) / len(pct), 2) if pct else None,
    }


def run_pass(con, *, client=None, limit: int | None = None, pause=None,
             sleep=None, log=None, resolver=None) -> dict:
    """Полный проход: отбор целей → MTProto-резолв → запись + пересчёт ``vr``."""
    # Отбор БЕЗ обрезки по лимиту: лимит применяет резолвер, поэтому «осталось»
    # считается по всей очереди, а не по усечённой выборке.
    handles, targets = collect_targets(con)
    without_subs_before = sum(len(t["source_ids"]) for t in targets.values())
    rounded_before = sum(len(t["candidate_ids"]) for t in targets.values())
    stat = {"targets": len(handles),
            "without_subs_before": without_subs_before,
            "rounded_before": rounded_before}
    if not handles:
        return {**stat, "requested": 0, "resolved": 0, "remaining": 0,
                "remaining_why": {}, "divergence": _divergence_stats([]),
                "filled_candidates": 0, "filled_sources": 0, "vr_ok": 0,
                "now_pass_vr": 0, "still_without_subs": 0, "flood_wait": None,
                "stopped": None}
    if resolver is None:
        resolver = resolve_handles
    results, rstats = resolver(handles, client=client, limit=limit, pause=pause,
                               sleep=sleep, log=log)

    applied = apply_exact_subs(con, results, targets)
    resolved_handles = {h for h, r in results.items() if r.get("subs") is not None}
    remaining = [h for h in handles if h not in resolved_handles]
    why: dict[str, int] = {}
    for h in remaining:
        err = (results.get(h) or {}).get("error")
        if err == "not_found":
            why["не найден/недоступен"] = why.get("не найден/недоступен", 0) + 1
        elif err:
            why["ошибка резолва"] = why.get("ошибка резолва", 0) + 1
        else:
            why["не проверен (лимит/flood/сессия)"] = (
                why.get("не проверен (лимит/flood/сессия)", 0) + 1)
    return {
        **stat,
        "requested": rstats.get("requested", len(handles)),
        "resolved": rstats.get("resolved", len(resolved_handles)),
        "remaining": len(remaining),
        "remaining_why": why,
        "not_found": rstats.get("not_found", 0),
        "errors": rstats.get("errors", 0),
        "flood_wait": rstats.get("flood_wait"),
        "stopped": rstats.get("stopped"),
        "divergence": _divergence_stats(applied["divergences"]),
        "filled_candidates": applied["filled_candidates"],
        "filled_sources": applied["filled_sources"],
        "vr_ok": applied["vr_ok"],
        "vr_no_views": applied["vr_no_views"],
        "vr_no_subs": applied["vr_no_subs"],
        "now_pass_vr": applied["now_pass_vr"],
        "still_without_subs": without_subs_before - applied["filled_sources"],
        "min_vr": threshold_of(con),
    }


def threshold_of(con=None) -> float:
    return float(config.PROMOTE_MIN_VR)


def format_report(s: dict, *, limit: int | None = None) -> str:
    d = s.get("divergence") or {}
    lines = [
        "=== точные подписчики Telegram через MTProto (D-62 + D-68) ===",
        f"целей к резолву: {s.get('targets', 0)} (лимит за прогон: "
        f"{config.EXACT_SUBS_MAX_PER_RUN if limit is None else limit})",
        f"строк очереди с округлёнными subs (D-62): {s.get('rounded_before', 0)}",
        f"кандидатов реестра без subs (D-68): {s.get('without_subs_before', 0)}",
        f"снято точных подписчиков: {s.get('resolved', 0)}; "
        f"осталось: {s.get('remaining', 0)}",
        f"заполнено subs: очередь {s.get('filled_candidates', 0)}, "
        f"реестр {s.get('filled_sources', 0)}; остались без данных: "
        f"{s.get('still_without_subs', 0)}",
        f"расхождение с округлённым (n={d.get('n', 0)}): среднее "
        f"{d.get('mean')} (max {d.get('max')})",
        f"vr после пересчёта: посчитан {s.get('vr_ok', 0)}, "
        f"нет просмотров {s.get('vr_no_views', 0)}; прошли порог "
        f">= {s.get('min_vr', config.PROMOTE_MIN_VR)}: {s.get('now_pass_vr', 0)}",
    ]
    if s.get("flood_wait"):
        lines.append(f"ОСТАНОВКА: flood-wait {s['flood_wait']}s после "
                     f"{s.get('resolved', 0)} запросов")
    why = s.get("remaining_why") or {}
    if why:
        lines.append("остались без данных, почему: "
                     + ", ".join(f"{k}={v}" for k, v in sorted(why.items())))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="tuber tg exact-subs",
        description="точные подписчики Telegram через MTProto (D-62 + D-68)")
    ap.add_argument("--db", default=None, help="путь к базе (по умолчанию из config)")
    ap.add_argument("--limit", type=int, default=None,
                    help="потолок каналов за прогон (по умолчанию из конфига)")
    ap.add_argument("--pause", type=float, default=None, help="пауза между запросами, с")
    ap.add_argument("--dry", action="store_true", help="только план, без сети и записи")
    ap.add_argument("--allow-production", action="store_true",
                    help="разрешить запись в боевую единую базу")
    ap.add_argument("--json", action="store_true", help="машинная сводка")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    path = config.resolve_db(args.db)
    if not args.dry and db.is_production_db(path) and not args.allow_production:
        print("отказ: запись в боевую базу без --allow-production "
              "(приёмка идёт на копии: TUBER_DB=/tmp/tuber-copy.db)", file=sys.stderr)
        return 2
    con = db.connect(path)
    try:
        if args.dry:
            handles, targets = collect_targets(con, limit=args.limit)
            summary = {"dry_run": True, "targets": len(handles),
                       "without_subs_before": sum(len(t["source_ids"])
                                                  for t in targets.values()),
                       "rounded_before": sum(len(t["candidate_ids"])
                                             for t in targets.values())}
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            else:
                print(format_report({**summary, "resolved": 0}, limit=args.limit))
            return 0

        run_id = db.start_run(con, "exact-subs")
        summary = run_pass(con, limit=args.limit, pause=args.pause)
        db.finish_run(con, run_id, channels_ok=summary.get("resolved", 0),
                      errors=summary.get("errors", 0) + summary.get("not_found", 0))
        db.log_run(con, "INFO",
                   f"exact-subs: resolved={summary.get('resolved', 0)} "
                   f"filled_sources={summary.get('filled_sources', 0)} "
                   f"filled_candidates={summary.get('filled_candidates', 0)} "
                   f"now_pass_vr={summary.get('now_pass_vr', 0)}")
        con.commit()
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            print(format_report(summary, limit=args.limit))
        return 0
    finally:
        con.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
