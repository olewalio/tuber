"""Граф первопроходцев (ТЗ-47, контур 4 плана «Сливки»).

Доводит до авторов два уже собранных сигнала графа ``edge`` и механики сюжетов:

* ``trusted_indegree_30d`` — сколько РАЗНЫХ крупных авторов (верхние 10 % по
  размеру внутри своей платформы) сослались на источник за 30 суток. Считаются
  три вида ребёр: ``mention``, ``quote``, ``repost``. Один крупный автор,
  сославшийся десять раз, считается ОДИН раз.
* ``lead_time_median`` — медиана ``lead_time`` автора по его постам внутри
  сюжетов. ``lead_time`` поста — минуты от поста до первого поста ВТОРОГО
  независимого автора сюжета (``t2``). Отрицательное значение = пост написан
  раньше «остальных» (до того, как тему подхватил второй автор), ноль/плюс —
  наравне или позже. Знак согласован со знаком ``story.lead_time_min``
  (там лаг считается первым автором до второго, здесь — наоборот).
* ``first_mover`` / ``first_mover_share`` — автор, первым написавший внутри
  сюжета НЕ МЕНЕЕ чем за 30 минут до второго независимого автора, и доля таких
  сюжетов от числа его многолетних сюжетов. Плюс пост-уровневая ось
  ``score.first_mover`` (1.0 у первого поста, 0.0 у остальных участников — только
  для сюжетов с ≥ 2 авторами).

Размер источника:
* ``source.subs``, если известен;
* для X без подписчиков — медиана ``metric_snapshot.views`` по его постам
  (фолбэк — ``avg_views``). Официальный API X подписчиков не отдаёт, поэтому без
  этой подмены половина X-реестра выпадала бы из сравнения.

«Верхние 10 %» считаются ВНУТРИ платформы: абсолютные подписчики YouTube (десятки
миллионов) и Telegram (десятки тысяч) несравнимы, и единая шкала вытеснила бы из
доверенных весь Telegram (см. ту же логику размерных классов в плане, контур 3).

Ноль сетевых запросов: только уже собранные ``edge``, ``story_member``,
``metric_snapshot``.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

from tuber.core import storage

#: Доля верхних источников «по размеру» (внутри платформы).
SIZE_TOP_FRACTION = 0.10
#: Виды рёбер «крупный сослался на мелкого» (контур 4): упоминание, цита, репост.
TRUSTED_EDGE_KINDS: tuple[str, ...] = ("mention", "quote", "repost")
#: Окно наблюдения trusted_indegree.
WINDOW_DAYS = 30
#: Минимальный отрыв первопроходца, минуты.
FIRST_MOVER_MIN_LEAD_MIN = 30.0
#: Платформа/режим для журнала прогонов (``run``/``run_log``).
RUN_PLATFORM = "graph"
RUN_MODE = "first-movers"
#: Поле «крупный крупнее мелкого» в списке пар.
PAIR_MIN_RATIO = 10.0
#: Нижний порог размера МЕЛКОЙ стороны пары: ниже него размер считается
#: НЕИЗВЕСТНЫМ (счётчик подписчиков не снят), а не «крошечным».
#: Число взято из отбора «сливок» (``slivki.SELECT_SUBS_MIN = 1000``, ТЗ-45):
#: источник с меньшим числом подписчиков в базе не считается измеренным.
#: Замер на боевой базе 21.09.2026: Telegram-канал с ``subs=1`` давал
#: вырожденное превышение ×103 065 в списке «кого читают верхние» (D-57).
#: Порог калибруемый: это константа, а не «магическое» условие в теле.
PAIR_MIN_SMALL_SIZE = 1000.0


def _utcnow(now=None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 4], fmt)
        except ValueError:
            continue
    return None


def _minutes(a, b) -> float | None:
    ta, tb = _parse(a), _parse(b)
    if ta is None or tb is None:
        return None
    return (tb - ta).total_seconds() / 60.0


# ===========================================================================
# Размер источника и «доверенные» (верхние 10 %)
# ===========================================================================

def source_sizes(con) -> dict[int, dict]:
    """Размер каждого источника: ``{source_id: {platform, handle, size, basis}}``.

    ``basis`` — ``subs`` | ``views_median`` | ``avg_views`` — чем измерен размер.
    Источники без измеримого размера в словарь не попадают.
    """
    views: dict[int, list] = {}
    for r in con.execute(
        """SELECT c.source_id AS sid, ms.views AS v
             FROM metric_snapshot ms JOIN content c ON c.id = ms.content_id
            WHERE ms.views IS NOT NULL AND c.source_id IS NOT NULL"""):
        views.setdefault(r["sid"], []).append(r["v"])

    sizes: dict[int, dict] = {}
    for r in con.execute(
            "SELECT id, platform, handle, subs, avg_views FROM source"):
        sid = r["id"]
        basis = "subs"
        size = float(r["subs"]) if r["subs"] is not None else None
        if size is None and r["platform"] == "x":
            vals = views.get(sid)
            if vals:
                size, basis = float(statistics.median(vals)), "views_median"
            elif r["avg_views"] is not None:
                size, basis = float(r["avg_views"]), "avg_views"
        if size is None:
            continue
        sizes[sid] = {"platform": r["platform"], "handle": r["handle"],
                      "size": size, "basis": basis}
    return sizes


def trusted_source_ids(con, *, sizes=None, fraction: float = SIZE_TOP_FRACTION) -> set[int]:
    """ID источников верхней ``fraction`` по размеру ВНУТРИ своей платформы."""
    sizes = sizes if sizes is not None else source_sizes(con)
    by_platform: dict[str, list[tuple[float, int]]] = {}
    for sid, info in sizes.items():
        by_platform.setdefault(info["platform"], []).append((info["size"], sid))
    trusted: set[int] = set()
    for items in by_platform.values():
        items.sort(key=lambda x: x[0], reverse=True)
        k = max(1, int(len(items) * fraction))
        trusted.update(sid for _size, sid in items[:k])
    return trusted


# ===========================================================================
# trusted_indegree_30d
# ===========================================================================

def target_source_map(con) -> dict[tuple[str, str], int]:
    """``{(platform, lower(handle)): source_id}`` — индекс цели ребра в реестре."""
    out: dict[tuple[str, str], int] = {}
    for r in con.execute(
            "SELECT id, platform, handle FROM source WHERE handle IS NOT NULL"):
        out[(r["platform"], str(r["handle"]).lower())] = r["id"]
    return out


def trusted_indegree(con, *, days: int = WINDOW_DAYS, now=None,
                     trusted: set[int] | None = None) -> dict[tuple[str, str, str], set[int]]:
    """Разные доверенные авторы по цели ребра за окно ``days``.

    Возвращает ``{(target_type, target_platform, target_value): {from_source_id,…}}``.
    Учитываются только рёбра ``mention``/``quote``/``repost`` окна и только
    ссылающиеся из ``trusted``.
    """
    now = _utcnow(now)
    cutoff = _iso(now - timedelta(days=days))
    if trusted is None:
        trusted = trusted_source_ids(con)
    marks = ",".join("?" for _ in TRUSTED_EDGE_KINDS)
    groups: dict[tuple[str, str, str], set[int]] = {}
    for r in con.execute(
            f"""SELECT target_type, target_platform, target_value, from_source_id
                  FROM edge
                 WHERE kind IN ({marks}) AND from_source_id IS NOT NULL
                   AND last_seen_at >= ?""",
            list(TRUSTED_EDGE_KINDS) + [cutoff]):
        if r["from_source_id"] not in trusted:
            continue
        key = (r["target_type"], r["target_platform"], r["target_value"])
        groups.setdefault(key, set()).add(r["from_source_id"])
    return groups


def indegree_by_source(groups: dict, tmap: dict[tuple[str, str], int]) -> dict[int, set[int]]:
    """Перевести цели-аккаунты в ``{source_id: {доверенный from_source_id,…}}``."""
    out: dict[int, set[int]] = {}
    for (ttype, tplat, tvalue), refs in groups.items():
        if ttype not in ("account", "channel"):
            continue
        sid = tmap.get((tplat, str(tvalue or "").lower()))
        if sid is None:
            continue
        out.setdefault(sid, set()).update(refs)
    return out


# ===========================================================================
# lead_time и first_mover по сюжетам
# ===========================================================================

def story_first_movers(con, *, limit: int | None = None,
                       tmap: dict[tuple[str, str], int] | None = None) -> dict:
    """Разобрать сюжеты на первопроходцев и лаги авторов.

    Возвращает::

        {
          "authors": {source_id: {"stories": {sid,…}, "first_moves": int,
                                  "leads": [min,…]}},
          "post_first_mover": {content_id: 1.0|0.0},
          "stories": int,                # разобрано многолетних сюжетов
          "stories_with_first_mover": int,
          "single_author_skipped": int,
        }

    Сюжеты с одним независимым автором в разбор не входят: у них нет «остальных»,
    и первенство мерило бы одиночество, а не скорость.
    """
    tmap = tmap if tmap is not None else target_source_map(con)
    rows = con.execute(
        """SELECT sm.story_id AS story_id, sm.content_id AS content_id,
                  sm.handle AS handle, c.published_at AS published_at,
                  c.platform AS platform, c.source_id AS source_id
             FROM story_member sm JOIN content c ON c.id = sm.content_id
            WHERE c.published_at IS NOT NULL
            ORDER BY sm.story_id, c.published_at, sm.content_id""").fetchall()

    by_story: dict[int, list] = {}
    for r in rows:
        by_story.setdefault(r["story_id"], []).append(r)

    story_ids = sorted(by_story)
    if limit:
        story_ids = story_ids[: int(limit)]

    authors: dict[int, dict] = {}
    post_flag: dict[int, float] = {}
    multi = with_fm = single = 0

    def _author_key(row) -> tuple[int | None, str | None]:
        sid = row["source_id"]
        if sid is None:
            handle = (row["handle"] or "").lower()
            if handle:
                sid = tmap.get((row["platform"], handle))
        handle = (row["handle"] or "").lower() or None
        return sid, handle

    for story_id in story_ids:
        members = by_story[story_id]
        if not members:
            continue
        first_sid, first_handle = _author_key(members[0])
        first_key = first_sid if first_sid is not None else ("h", first_handle)
        # второй НЕЗАВИСИМЫЙ автор: первый пост другого автора
        second = None
        for m in members[1:]:
            sid, handle = _author_key(m)
            key = sid if sid is not None else ("h", handle)
            if key is not None and key != first_key:
                second = m
                break
        if second is None:
            single += 1
            continue
        multi += 1
        t2 = second["published_at"]

        is_first_mover = first_sid is not None
        if is_first_mover:
            lag = _minutes(members[0]["published_at"], t2)
            is_first_mover = lag is not None and lag >= FIRST_MOVER_MIN_LEAD_MIN
        if is_first_mover:
            with_fm += 1

        if first_sid is not None:
            post_flag[members[0]["content_id"]] = 1.0 if is_first_mover else 0.0
        for m in members:
            if m is not members[0]:
                post_flag.setdefault(m["content_id"], 0.0)

        for m in members:
            sid, handle = _author_key(m)
            if sid is None:
                continue
            acc = authors.setdefault(
                sid, {"stories": set(), "first_moves": 0, "leads": []})
            acc["stories"].add(story_id)
            lead = _minutes(t2, m["published_at"])  # пост минус t2: раньше -> <0
            if lead is not None:
                acc["leads"].append(lead)
        if is_first_mover and first_sid is not None:
            authors[first_sid]["first_moves"] += 1

    return {"authors": authors, "post_first_mover": post_flag,
            "stories": multi, "stories_with_first_mover": with_fm,
            "single_author_skipped": single}


# ===========================================================================
# Сборка и запись
# ===========================================================================

def build(con, *, days: int = WINDOW_DAYS, limit: int | None = None,
          now=None) -> dict:
    """Посчитать все три оси + пары, ничего не записывая."""
    now = _utcnow(now)
    sizes = source_sizes(con)
    trusted = trusted_source_ids(con, sizes=sizes)
    tmap = target_source_map(con)
    groups = trusted_indegree(con, days=days, now=now, trusted=trusted)
    by_source = indegree_by_source(groups, tmap)
    stories = story_first_movers(con, limit=limit, tmap=tmap)

    authors = stories["authors"]
    ledger: dict[int, dict] = {}
    for sid, acc in authors.items():
        share = (acc["first_moves"] / len(acc["stories"])) if acc["stories"] else 0.0
        leads = acc["leads"]
        ledger[sid] = {
            "first_moves": acc["first_moves"],
            "stories": len(acc["stories"]),
            "first_mover_share": round(share, 6),
            "lead_time_median": (round(statistics.median(leads), 3) if leads else None),
            "lead_time_posts": len(leads),
        }
    # Источники, на которые сослались крупные, но без сюжетов, тоже попадают в
    # реестр: trusted_indegree — самостоятельная ось.
    for sid, refs in by_source.items():
        entry = ledger.setdefault(sid, {
            "first_moves": 0, "stories": 0, "first_mover_share": 0.0,
            "lead_time_median": None, "lead_time_posts": 0})
        entry["trusted_indegree_30d"] = len(refs)
    for entry in ledger.values():
        entry.setdefault("trusted_indegree_30d", 0)

    return {
        "window_days": days,
        "sizes": sizes,
        "trusted": trusted,
        "groups": groups,
        "indegree_by_source": by_source,
        "story_stats": {k: v for k, v in stories.items() if k != "authors"},
        "authors": authors,
        "post_first_mover": stories["post_first_mover"],
        "ledger": ledger,
        "computed_at": _iso(now),
    }


def write(con, data: dict) -> dict:
    """Записать реестр ``first_mover`` и пост-уровневую ось ``score.first_mover``."""
    ts = data["computed_at"]
    sizes = data["sizes"]
    con.execute("DELETE FROM first_mover")
    written = 0
    for sid, entry in data["ledger"].items():
        info = sizes.get(sid, {})
        handle = info.get("handle")
        platform = info.get("platform")
        if platform is None:
            row = con.execute("SELECT platform, handle FROM source WHERE id=?",
                              (sid,)).fetchone()
            if row is not None:
                platform, handle = row["platform"], row["handle"]
        con.execute(
            """INSERT INTO first_mover (source_id, platform, handle, first_moves,
                 stories, first_mover_share, lead_time_median, lead_time_posts,
                 trusted_indegree_30d, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid, platform, handle, entry["first_moves"], entry["stories"],
             entry["first_mover_share"], entry["lead_time_median"],
             entry["lead_time_posts"], entry["trusted_indegree_30d"], ts))
        written += 1
    score_rows = 0
    # Полный пересчёт оси: сперва снять прежние значения, чтобы пост, выпавший из
    # сюжета, не остался с устаревшим first_mover=1.0 от прошлого прогона.
    con.execute("UPDATE score SET first_mover=NULL WHERE first_mover IS NOT NULL")
    for cid, flag in data["post_first_mover"].items():
        cur = con.execute(
            """UPDATE score SET first_mover=?
                WHERE content_id=?
                  AND computed_at=(SELECT MAX(computed_at) FROM score WHERE content_id=?)""",
            (float(flag), cid, cid))
        score_rows += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    con.commit()
    return {"ledger_rows": written, "score_rows": score_rows}


def refresh(con, *, days: int = WINDOW_DAYS, limit: int | None = None,
            now=None, write_data: bool = True) -> dict:
    """Полный пересчёт: посчитать, записать, отметить прогон в ``run``/``run_log``."""
    now = _utcnow(now)
    data = build(con, days=days, limit=limit, now=now)
    stats = {"window_days": days, "trusted_sources": len(data["trusted"]),
             "indegree_targets": len(data["indegree_by_source"]),
             "authors": len(data["authors"]),
             "stories": data["story_stats"]["stories"],
             "stories_with_first_mover": data["story_stats"]["stories_with_first_mover"],
             "ledger_rows": 0, "score_rows": 0, "write": bool(write_data)}
    if not write_data:
        return stats
    run_id = storage.add_run(con, RUN_PLATFORM, started_at=_iso(now),
                             mode=RUN_MODE)
    written = write(con, data)
    stats.update(written)
    storage.log_run(con, run_id, _iso(now), "info", None,
                    "first-movers: доверенных %d, авторов %d, первопроходцев-сюжетов %d, "
                    "первого хода у %d, записей реестра %d, оценок обновлено %d"
                    % (stats["trusted_sources"], stats["authors"],
                       data["story_stats"]["stories_with_first_mover"],
                       sum(1 for e in data["ledger"].values() if e["first_moves"] > 0),
                       stats["ledger_rows"], stats["score_rows"]),
                    platform=RUN_PLATFORM)
    con.execute(
        "UPDATE run SET finished_at=?, ok_count=?, items_new=?, errors=0, note=? WHERE id=?",
        (_iso(_utcnow(now)), stats["ledger_rows"], stats["ledger_rows"],
         "graph first-movers", run_id))
    con.commit()
    stats["run_id"] = run_id
    return stats


# ===========================================================================
# Выдача
# ===========================================================================

def read_ledger(con) -> dict[int, dict]:
    """Реестр ``first_mover`` как ``{source_id: row}``."""
    out: dict[int, dict] = {}
    for r in con.execute("SELECT * FROM first_mover"):
        out[r["source_id"]] = dict(r)
    return out


def small_side_known(info: dict, *, floor: float = PAIR_MIN_SMALL_SIZE) -> bool:
    """Известен ли размер мелкой стороны пары (не ниже порога, D-57).

    ``subs=1`` — это не «мелкий канал», а неснятый счётчик: размер неизвестен.
    Такой источник не должен попадать в «кого читают верхние» как рекордное
    превышение ×100000.
    """
    size = info.get("size")
    return size is not None and float(size) >= float(floor)


def pairs(con, *, min_ratio: float = PAIR_MIN_RATIO, days: int = WINDOW_DAYS,
          limit: int | None = None, now=None, data: dict | None = None,
          stats: dict | None = None) -> list[dict]:
    """Пары «крупный → мелкий» по рёбрам доверенных ссылающихся.

    Пара попадает в список, если размер крупного ≥ ``min_ratio`` × размер мелкого
    И размер мелкой стороны ИЗВЕСТЕН (≥ :data:`PAIR_MIN_SMALL_SIZE`): вырожденные
    «×100000» на ``subs=1`` из выдачи уходят (D-57). ``distinct_trusted`` — сколько
    разных доверенных авторов сослалось на мелкого (в паре указывается и общее
    число, и конкретный крупный).

    ``stats`` (необязательный out-параметр) получает счётчики:
    ``skipped_small_unknown`` — сколько пар отсеяно порогом мелкой стороны.
    """
    data = data if data is not None else build(con, days=days, limit=limit, now=now)
    sizes = data["sizes"]
    tmap = target_source_map(con)
    ledger = data["ledger"]
    # TODO(debt-D-57): закрыт в ТЗ-52 — размер мелкой стороны ниже
    # PAIR_MIN_SMALL_SIZE (или неизвестен) в пары не попадает; см. TECH-DEBT.md.
    out: list[dict] = []
    skipped_small_unknown = 0
    for (ttype, tplat, tvalue), refs in data["groups"].items():
        if ttype not in ("account", "channel"):
            continue
        tid = tmap.get((tplat, str(tvalue or "").lower()))
        if tid is None or tid not in sizes:
            continue
        small = sizes[tid]["size"]
        if small <= 0:
            # Размер неизвестен вовсе — мелкая сторона не измерена.
            for rid in refs:
                if sizes.get(rid, {}).get("size", 0) >= small * min_ratio:
                    skipped_small_unknown += 1
            continue
        small_known = small_side_known(sizes[tid])
        for rid in refs:
            if rid not in sizes:
                continue
            big = sizes[rid]["size"]
            if big < small * min_ratio:
                continue
            if not small_known:
                # Вырожденная пара (неснятый счётчик мелкой стороны): не рекорд.
                skipped_small_unknown += 1
                continue
            entry = ledger.get(tid, {})
            out.append({
                "big_source_id": rid,
                "big_handle": sizes[rid]["handle"],
                "big_platform": sizes[rid]["platform"],
                "big_size": round(big, 1),
                "big_basis": sizes[rid]["basis"],
                "small_source_id": tid,
                "small_handle": sizes[tid]["handle"],
                "small_platform": sizes[tid]["platform"],
                "small_size": round(small, 1),
                "small_basis": sizes[tid]["basis"],
                "ratio": round(big / small, 2),
                "distinct_trusted": len(refs),
                "lead_time_median": entry.get("lead_time_median"),
                "first_mover_share": entry.get("first_mover_share"),
            })
    out.sort(key=lambda p: (-p["ratio"], p["big_handle"] or "", p["small_handle"] or ""))
    if stats is not None:
        stats["skipped_small_unknown"] = skipped_small_unknown
    return out


def report(con, *, days: int = WINDOW_DAYS, limit: int | None = None,
           now=None, refresh_data: bool = False) -> dict:
    """Структура выдачи: первопроходцы, пары «кто читает верхних», сводка."""
    if refresh_data:
        refresh(con, days=days, limit=limit, now=now, write_data=True)
    data = build(con, days=days, limit=limit, now=now)
    ledger = data["ledger"]
    sizes = data["sizes"]

    pioneers = []
    for sid, entry in ledger.items():
        if entry["first_moves"] <= 0:
            continue
        info = sizes.get(sid, {})
        row = con.execute("SELECT handle, platform FROM source WHERE id=?", (sid,)).fetchone()
        pioneers.append({
            "source_id": sid,
            "handle": info.get("handle") or (row["handle"] if row else None),
            "platform": info.get("platform") or (row["platform"] if row else None),
            "first_moves": entry["first_moves"],
            "stories": entry["stories"],
            "first_mover_share": entry["first_mover_share"],
            "lead_time_median": entry["lead_time_median"],
            "trusted_indegree_30d": entry["trusted_indegree_30d"],
        })
    pioneers.sort(key=lambda p: (-p["first_mover_share"], -p["first_moves"],
                                 p["handle"] or ""))

    pair_stats: dict = {}
    pair_list = pairs(con, days=days, limit=limit, now=now, data=data, stats=pair_stats)
    indeg2 = [{"source_id": sid, "handle": (sizes.get(sid) or {}).get("handle"),
               "trusted_indegree_30d": len(refs)}
              for sid, refs in data["indegree_by_source"].items() if len(refs) >= 2]
    indeg2.sort(key=lambda x: -x["trusted_indegree_30d"])

    return {
        "window_days": days,
        "trusted_sources": len(data["trusted"]),
        "authors_with_ledger": len(ledger),
        "authors_with_first_move": len(pioneers),
        "stories": data["story_stats"]["stories"],
        "stories_with_first_mover": data["story_stats"]["stories_with_first_mover"],
        "accounts_indegree_ge2": len(indeg2),
        "pairs_ge10x": len(pair_list),
        # D-57: сколько пар отсеяно порогом «мелкая сторона известна».
        "pairs_skipped_min_size": pair_stats["skipped_small_unknown"],
        "pairs_min_small_size": PAIR_MIN_SMALL_SIZE,
        "pioneers": pioneers,
        "pairs": pair_list,
        "indegree_ge2": indeg2[:20],
    }


def format_report(report_data: dict) -> str:
    """Человекочитаемая печать выдачи первопроходцев."""
    r = report_data
    lines = ["=== Граф первопроходцев (Контур 4) ==="]
    lines.append(f"окно: {r['window_days']} дн | доверенных источников:"
                 f" {r['trusted_sources']}")
    lines.append(f"первопроходцы: {r['authors_with_first_move']} авторов;"
                 f" сюжетов с первым ходом: {r['stories_with_first_mover']}"
                 f" из {r['stories']} многолетних")
    lines.append(f"аккаунтов с trusted_indegree_30d ≥ 2: {r['accounts_indegree_ge2']}")
    lines.append(f"пар «крупный → мелкий» с превышением ≥ 10 раз: {r['pairs_ge10x']}"
                 f" | отсеяно порогом мелкой стороны (< {r.get('pairs_min_small_size', 0):.0f}"
                 f" или размер неизвестен): {r.get('pairs_skipped_min_size', 0)}")
    lines.append("")
    lines.append("-- кого читают те, кто уже наверху --")
    if not r["pairs"]:
        lines.append("  (нет)")
    for p in r["pairs"][:30]:
        lead = ("-" if p["lead_time_median"] is None
                else f"{p['lead_time_median']:.0f}")
        lines.append(
            f"  {p['big_handle']} ({p['big_platform']}, {p['big_size']:.0f})"
            f" → {p['small_handle']} ({p['small_platform']}, {p['small_size']:.0f})"
            f" ×{p['ratio']:.0f}, крупных сослалось {p['distinct_trusted']},"
            f" медианный лаг {lead} мин")
    lines.append("")
    lines.append("-- первопроходцы (доля сюжетов, где автор был первым) --")
    if not r["pioneers"]:
        lines.append("  (нет)")
    for p in r["pioneers"][:30]:
        lead = ("-" if p["lead_time_median"] is None
                else f"{p['lead_time_median']:.0f}")
        lines.append(
            f"  {p['handle']} ({p['platform']}): first_mover_share"
            f" {p['first_mover_share']:.3f} ({p['first_moves']}/{p['stories']}),"
            f" медианный лаг {lead} мин, trusted_indegree"
            f" {p['trusted_indegree_30d']}")
    return "\n".join(lines)


def format_json(report_data: dict) -> str:
    return json.dumps(report_data, ensure_ascii=False, sort_keys=True)
