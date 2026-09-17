"""Сквозные сюжеты: связывание материалов разных платформ (ТЗ «сквозной сюжет»).

Задача
------
Движок сюжетов умел строить только X-сюжеты (``tuber/platforms/x/stories.py``):
он кластеризовал посты ``posts`` (после приговора модели ``is_ai=1``) и писал
строки ``story`` с ``platform='x'``. Из-за этого четвёртый блок выдачи
(``tuber/analysis/report.py::cross_story_section``) был ПУСТ: чтобы попасть в
него, сюжет должен объединять ``content`` минимум ДВУХ разных платформ, а
Telegram и YouTube в построении сюжетов не участвовали вообще.

Этот модуль обобщает построение сюжетов на ЕДИНОЕ ядро: источник — таблица
``content`` (X + Telegram + YouTube), окно — то же ``STORIES_WINDOW_HOURS``.

Пересборка 17.09.2026: устранение ПРИЧИН ложных склеек (ТЗ «качество сквозных
сюжетов»)
--------------------------------------------------------------------------
Первая версия модуля переиспользовала правило X один-в-один и давала precision
0.19 на размеченных 47 парах (см. ``docs/CROSS-STORIES.md``). Замер показал три
системные причины, а не «плохой порог»:

1. **Насыщение сущностного вклада.** ``STORY_ENTITY_IDF_NORM=3.0`` делает
   ``ent_part=1.0`` почти для любой общей сущности, поэтому балл X-правила
   ``text_part + 0.1*ent_part`` вырождается в ``text_part + 0.1`` и одна общая
   сущность гарантирует склейку. Здесь сущность больше НЕ добавляет к баллу
   константу: она лишь открывает более слабый текстовый порог и только при
   **≥2 независимых значимых** совпадениях (``CROSS_ENT_MIN``), а «значимость»
   сущности нормируется её частотой (df ≤ ``CROSS_ANCHOR_MAX_DF``) — нормировка
   устойчива к размеру корпуса, в отличие от доли-«гиганта».
2. **Транзитивные склейки.** Средняя связь (UPGMA) тянет цепочку A~B~C при
   A≁C. Здесь слияние разрешено, только если ВСЕ кросс-платформенные пары между
   двумя группами уже приняты (критерий «кросс-платформенного клика»); пары
   внутри одной платформы в кандидаты не попадают и потому не проверяются. Так
   «три перепоста одного твита» собираются, а цепочка через среднее звено — нет.
3. **Языковый перекос IDF.** IDF считался по англоязычному X-контексту, а
   текстовое подтверждение нормировалось на КОРОТКИЙ пост (containment). Для
   нелатинских текстов (корейский/китайский режет ``_WORD_RE``) и коротких
   заголовков YouTube это давало ``text_part≈1.0`` на одном общем слове:
   например пара «Qwen model on apple silicon» ↔ «Yandex weights» имела
   containment 0.49 при косинусе 0.02. Здесь (а) IDF токенов считается по
   ВСЕМУ корпусу окна (все языки, не только английский X), (б) мера —
   симметричная **TF-IDF-косинусная**: ни короткий, ни длинный документ не
   может «раздуть» близость в одиночку, а TF не даёт одному общему слову
   перевесить повторяющееся событийное содержимое.

Порог ``--text-min`` (default ``CROSS_TEXT_MIN=0.30`` — значение и имя не
менялись) теперь применяется к TF-IDF-косинусу. Замер на НОВОЙ независимой
разметке (окно 240 ч, ``docs/cross-story-labels-240.jsonl``, 64 пары; см.
``docs/CROSS-STORIES.md``) даёт на неизменном default 0.30 precision 1.00 при
recall 0.14; рекомендованный для боевой выдачи порог ``0.15`` даёт precision
0.92 / recall 0.94 (а на разбиении design/test — precision 0.83 / recall 1.00 на
отложенной части). Почему default оставлен 0.30 — см. доклад и §4 ТЗ.

Кластеризация — детерминированное слияние по средней близости с критерием
«кросс-платформенного клика» (см. :func:`_cluster`). Результат не зависит от
порядка входа (сортировка ``content`` по ``published_at, platform, external_id``;
выбор пары с максимальной связью; при равных баллах — лексикографически).

Идемпотентность
---------------
Повторный прогон удаляет прежние строки ``platform='cross'`` (каскадом с
``story_member``) и вставляет заново в детерминированном порядке. Прогон
``dry_run=True`` в базу не пишет. Признак «сквозной» — значение
``story.platform='cross'``, добавленное идемпотентным сидом
(:data:`tuber.core.schema.PLATFORM_SEED` и :func:`ensure_cross_platform`), без
переписывания существующих значений ``platform``.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from tuber.platforms.x import config as xconfig
from tuber.platforms.x import store as xdb
from tuber.platforms.x import stories as xstories

#: Значение ``story.platform`` для сквозного сюжета.
PLATFORM = "cross"
# TODO(debt-D-46): закрыт в ТЗ-6 — движок переработан (IDF, транзитивность),
# сюжеты наполнены — см. TECH-DEBT.md.
#: Платформы-участники (порядок не важен; сортировка задаёт детерминизм).
PLATFORMS = ("x", "telegram", "youtube")
#: Максимальная частота сущности по общему корпусу, при которой она может быть
#: «якорем» склейки / значимым совпадением. Нормировка по частоте устойчива к
#: размеру корпуса (в отличие от доли-«гиганта», см. докстринг).
CROSS_ANCHOR_MAX_DF = 50
#: Порог симметричной TF-IDF-косинусной близости, ниже которого пара не
#: склеивается по тексту. Значение default не менялось с первой версии (0.30), но
#: применяется к косинусу, а не к containment. Рекомендация для боевой выдачи —
#: 0.15 (precision 0.92 / recall 0.94 на новой разметке, см. docs/CROSS-STORIES.md).
CROSS_TEXT_MIN = 0.30
#: Более слабый текстовый порог, допустимый ТОЛЬКО при ≥2 независимых значимых
#: общих сущностях: подтверждение «сущности + заметный общий текст».
CROSS_ENT_SUPPORT_MIN = 0.10
#: Сколько независимых значимых общих сущностей требуется для слабого пути.
CROSS_ENT_MIN = 2
#: Якорь, встречающийся более чем в стольких постах окна, из перебора исключаем:
#: иначе инвертированный индекс даёт слишком много пар-кандидатов, а сущность
#: всё равно неспецифична.
_ANCHOR_LIST_CAP = 60


def ensure_cross_platform(con) -> None:
    """Идемпотентно добавить строку ``platform('cross')`` (FK для ``story``)."""
    con.execute(
        "INSERT INTO platform(code, title) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET title=excluded.title",
        (PLATFORM, "Сквозной сюжет (несколько платформ)"))


# --------------------------------------------------------------------------- #
# Выборка корпуса
# --------------------------------------------------------------------------- #
def _row_to_post(row) -> dict:
    text = row["text"] or ""
    title = row["title"]
    # Для YouTube осмысленный текст — это заголовок + описание: заголовок несёт
    # событие, описание часто пустое или служебное.
    if row["platform"] == "youtube" and title:
        text = f"{title}\n{text}"
    return {
        "content_id": row["id"],
        "platform": row["platform"],
        "external_id": row["external_id"],
        # Уникальный в пределах корпуса идентификатор: helpers X (lead_time_min,
        # assign_roles) сортируют по ``tweet_id`` как по строке.
        "tweet_id": f"{row['platform']}:{row['external_id']}",
        "source_id": row["source_id"],
        "source_handle": row["source_handle"],
        "text": text,
        "mentions": row["mentions"],
        "links": row["links"],
        "published_at_utc": row["published_at"],
        "author_handle": row["author_handle"],
    }


def _select_content(con, since: str, now_iso: str) -> list[dict]:
    rows = con.execute(
        "SELECT c.id, c.platform, c.external_id, c.source_id, c.title, c.text,"
        " c.links, c.mentions, c.published_at, c.author_handle,"
        " s.handle AS source_handle"
        " FROM content c LEFT JOIN source s ON s.id = c.source_id"
        " WHERE c.deleted_at IS NULL AND c.published_at >= ?"
        " AND c.published_at <= ? AND c.platform IN ('x','telegram','youtube')"
        " ORDER BY c.published_at ASC, c.platform ASC, c.external_id ASC",
        (since, now_iso)).fetchall()
    posts = [_row_to_post(r) for r in rows]
    return [p for p in posts if len((p["text"] or "").strip())
            >= int(xconfig.STORY_MIN_TEXT_LEN)]


def _select_x_context(con, since: str, now_iso: str) -> list[dict]:
    """X-корпус окна с приговором ``is_ai=1`` — калибровочный контекст «гигантов».

    Если таблицы ``classification`` нет (усечённые тестовые базы), контекст
    пуст: сюжеты всё равно строятся, но «гиганты» считаются по общему корпусу.
    """
    try:
        rows = con.execute(
            "SELECT c.id, c.platform, c.external_id, c.source_id, c.title,"
            " c.text, c.links, c.mentions, c.published_at, c.author_handle,"
            " s.handle AS source_handle"
            " FROM content c"
            " JOIN classification cl ON cl.content_id = c.id AND cl.is_ai = 1"
            " LEFT JOIN source s ON s.id = c.source_id"
            " WHERE c.platform = 'x' AND c.deleted_at IS NULL"
            " AND c.published_at >= ? AND c.published_at <= ?"
            " ORDER BY c.published_at ASC, c.external_id ASC",
            (since, now_iso)).fetchall()
    except Exception:  # noqa: BLE001 — отсутствие classification не фатально
        return []
    return [_row_to_post(r) for r in rows]


# --------------------------------------------------------------------------- #
# Контекст: IDF токенов по всему корпусу, «гиганты» — по X-контексту
# --------------------------------------------------------------------------- #
def _feature_with_tf(post: dict) -> dict:
    """Признаки X-модуля + частоты слов для TF-IDF-косинуса."""
    f = xstories._feature(post)
    f["tf"] = _tf(post)
    return f


def _document_freq(feats, key: str) -> dict[str, int]:
    df: dict[str, int] = {}
    for f in feats:
        for x in f[key]:
            df[x] = df.get(x, 0) + 1
    return df


def _log(x: float) -> float:
    return math.log(x) if x > 0 else 0.0


class _CrossContext:
    """IDF токенов по ВСЕМУ корпусу окна + df сущностей + набор «гигантов».

    IDF токенов считается по всем платформам и языкам окна, а не по
    англоязычному X: иначе русские/корейские/китайские токены кажутся редкими и
    «общая ИИ-лексика» переоценивается (ТЗ, причина 3). «Гиганты» (сущности,
    которые не могут склеить пару сами по себе) калибруются на X-контексте —
    ровно том корпусе, где правило X работает эталонно.
    """

    def __init__(self, feats, x_feats=None):
        self.n = max(1, len(feats))
        self.tok_df = _document_freq(feats, "tokens")
        self.ent_df = _document_freq(feats, "ents")
        x = x_feats if x_feats else feats
        xn = max(1, len(x))
        x_df = _document_freq(x, "ents")
        self.giants = {e for e, c in x_df.items()
                       if c / xn >= float(xconfig.STORY_GIANT_ENTITY_DOC_FREQ)}

    def idf_tok(self, w: str) -> float:
        return 1.0 + _log((self.n + 1.0) / (1.0 + self.tok_df.get(w, 0)))

    def idf_ent(self, e: str) -> float:
        return 1.0 + _log((self.n + 1.0) / (1.0 + self.ent_df.get(e, 0)))


# --------------------------------------------------------------------------- #
# Кандидаты и приём пар
# --------------------------------------------------------------------------- #
def _candidate_pairs(feats, ctx, anchor_max_df: int) -> set[tuple[int, int]]:
    """Пары разных платформ с общей «якорной» сущностью (не гигантом)."""
    df = ctx.ent_df
    index: dict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(feats):
        for e in f["core"]:
            if e in ctx.giants:
                continue
            if df.get(e, 0) > anchor_max_df:
                continue
            index[e].append(i)
    pairs: set[tuple[int, int]] = set()
    for e in sorted(index):
        lst = index[e]
        if len(lst) > _ANCHOR_LIST_CAP:
            continue
        for a in range(len(lst)):
            for b in range(a + 1, len(lst)):
                i, j = lst[a], lst[b]
                if feats[i]["post"]["platform"] != feats[j]["post"]["platform"]:
                    pairs.add((i, j))
    return pairs


def _tf(post: dict) -> dict:
    """Частоты значимых слов поста (нижний регистр, без стоп-слов)."""
    from collections import Counter
    words = xstories._WORD_RE.findall((post.get("text") or "").lower())
    return Counter(w for w in words
                   if len(w) >= 3 and w not in xstories._STOPWORDS)


def _lex_sim(ctx, a, b) -> float:
    """Симметричная TF-IDF-косинусная близость двух постов.

    ``cos(v_a, v_b)``, где ``v`` — вектор значимых слов с весом
    ``(1 + ln tf) · idf``. В отличие от прежнего асимметричного containment
    (доля пересечения от КОРОТКОГО поста) короткий пост не может «раздуть» меру:
    именно на этом нелатинские тексты и короткие заголовки YouTube давали
    ``text_part≈1.0`` на одном общем слове. Косинус устойчив и к длине, и к
    языку (IDF считается по всему корпусу окна).
    """
    va = {w: (1.0 + _log(c)) * ctx.idf_tok(w) for w, c in a["tf"].items()}
    vb = {w: (1.0 + _log(c)) * ctx.idf_tok(w) for w, c in b["tf"].items()}
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if not (na and nb):
        return 0.0
    dot = sum(va.get(w, 0.0) * vb.get(w, 0.0) for w in va.keys() & vb.keys())
    return dot / (na * nb)


def _specific_shared(ctx, a, b, anchor_max_df: int) -> list[str]:
    """Общие сущности, значимые независимо: не «гиганты» и df ≤ anchor_max_df."""
    return [e for e in (a["ents"] & b["ents"])
            if e not in ctx.giants and ctx.ent_df.get(e, 0) <= anchor_max_df]


def _accepted_edges(feats, ctx, pairs, threshold, text_min, anchor_max_df) -> dict:
    """Принятые пары: сильная близость ИЛИ слабая + ≥2 значимые общие сущности."""
    edges: dict[tuple[int, int], float] = {}
    ent_min = int(CROSS_ENT_MIN)
    support = float(CROSS_ENT_SUPPORT_MIN)
    for i, j in pairs:
        a, b = feats[i], feats[j]
        dt = xstories._hours_between(a["t"], b["t"])
        if dt is None or dt > float(xconfig.STORY_TIME_GATE_HOURS):
            continue
        d = _lex_sim(ctx, a, b)
        if d >= float(text_min):
            edges[(i, j)] = d
        elif (d >= min(support, float(text_min))
              and len(_specific_shared(ctx, a, b, anchor_max_df)) >= ent_min):
            edges[(i, j)] = d
    return edges


def _key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _cluster(n: int, edges: dict, threshold: float, plats) -> list[list[int]]:
    """Слияние по средней близости с критерием «кросс-платформенного клика».

    Две группы сливаются, только если КАЖДАЯ кросс-платформенная пара между ними
    уже принята (``edges``), а средняя связь по этим парам не ниже ``threshold``.
    Пары внутри одной платформы не являются кандидатами и не проверяются — иначе
    группа из нескольких перепостов одной платформы вокруг общего зачина была бы
    невозможна. Это отсекает транзитивные цепочки A~B~C при A≁C.
    """
    active = sorted({x for pair in edges for x in pair})
    members: dict[int, set[int]] = {i: {i} for i in active}
    roots = set(active)
    changed = True
    while changed:
        changed = False
        best = None
        rl = sorted(roots)
        for ai in range(len(rl)):
            ma = members[rl[ai]]
            for bi in range(ai + 1, len(rl)):
                mb = members[rl[bi]]
                if len(ma) * len(mb) > 4096:
                    continue
                ok, total, ncross = True, 0.0, 0
                for x in ma:
                    for y in mb:
                        if plats[x] == plats[y]:
                            continue
                        s = edges.get((x, y), edges.get((y, x)))
                        if s is None:
                            ok = False
                            break
                        total += s
                        ncross += 1
                    if not ok:
                        break
                if not ok or ncross == 0:
                    continue
                avg = total / ncross
                if avg < threshold:
                    continue
                if best is None or avg > best[0]:
                    best = (avg, rl[ai], rl[bi])
        if best:
            _, ra, rb = best
            members[ra] |= members[rb]
            del members[rb]
            roots.discard(rb)
            changed = True
    groups: dict[int, list[int]] = defaultdict(list)
    in_group = {i: root for root, mem in members.items() for i in mem}
    for i in range(n):
        groups[in_group.get(i, i)].append(i)
    return list(groups.values())


# --------------------------------------------------------------------------- #
# Метрики сюжета
# --------------------------------------------------------------------------- #
def _handle(post: dict) -> str:
    h = (post.get("author_handle") or post.get("source_handle") or "").strip()
    return h.lstrip("@") if h else f"{post['platform']}:{post['external_id']}"


def _label(posts: list[dict]) -> str:
    first = min(posts, key=lambda p: (p["published_at_utc"] or "", p["platform"],
                                      p["external_id"]))
    snippet = " ".join((first["text"] or "").split())
    return snippet[:80] or f"сквозной сюжет {first['platform']}"


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #
def run(con, *, window_hours=None, threshold=None, text_min=None,
        anchor_max_df=None, run_id=None, now=None, dry_run=False) -> dict:
    """Пересобрать сквозные сюжеты за окно. Идемпотентно и детерминированно."""
    now = now or datetime.now(timezone.utc)
    window_hours = int(window_hours or xconfig.STORIES_WINDOW_HOURS)
    threshold = (xconfig.STORY_MATCH_MIN if threshold is None
                 else float(threshold))
    text_min = CROSS_TEXT_MIN if text_min is None else float(text_min)
    anchor_max_df = (CROSS_ANCHOR_MAX_DF if anchor_max_df is None
                     else int(anchor_max_df))
    since = xdb.iso(now - timedelta(hours=window_hours))
    now_iso = xdb.iso(now)

    posts = _select_content(con, since, now_iso)
    ctx_posts = _select_x_context(con, since, now_iso)
    feats = [_feature_with_tf(p) for p in posts]
    ctx_feats = [xstories._feature(p) for p in ctx_posts]
    ctx = _CrossContext(feats, ctx_feats if ctx_feats else feats)

    pairs = _candidate_pairs(feats, ctx, anchor_max_df)
    edges = _accepted_edges(feats, ctx, pairs, threshold, text_min, anchor_max_df)
    plats = [f["post"]["platform"] for f in feats]
    groups = _cluster(len(feats), edges, threshold, plats)

    summary = {"window_hours": window_hours, "threshold": threshold,
               "text_min": text_min, "anchor_max_df": anchor_max_df,
               "posts": len(feats), "context_posts": len(ctx_feats),
               "stories": 0, "stories_all": 0, "single_platform": 0,
               "members": 0, "candidate_pairs": len(pairs),
               "positive_pairs": len(edges), "dry_run": bool(dry_run)}

    cross_groups = []
    for g in groups:
        plats_g = {feats[i]["post"]["platform"] for i in g}
        if len(g) >= 2 and len(plats_g) >= 2:
            cross_groups.append(g)
        elif len(g) >= 2:
            summary["single_platform"] += 1
        summary["stories_all"] += 1

    # Детерминированный порядок вставки: по времени первого поста, затем по
    # составу (платформа+external_id) — повторный прогон даёт те же id.
    def group_key(g):
        first = min(g, key=lambda i: (feats[i]["t"], feats[i]["post"]["platform"],
                                      feats[i]["post"]["external_id"]))
        return (feats[first]["t"], feats[first]["post"]["platform"],
                feats[first]["post"]["external_id"],
                tuple(sorted((feats[i]["post"]["platform"],
                              feats[i]["post"]["external_id"]) for i in g)))

    cross_groups.sort(key=group_key)

    if not dry_run:
        ensure_cross_platform(con)
        con.execute("DELETE FROM story_member WHERE story_id IN"
                    " (SELECT id FROM story WHERE platform = ?)", (PLATFORM,))
        con.execute("DELETE FROM story WHERE platform = ?", (PLATFORM,))

    for g in cross_groups:
        ordered = sorted(g, key=lambda i: (feats[i]["t"],
                                           feats[i]["post"]["platform"],
                                           feats[i]["post"]["external_id"]))
        posts_in = [feats[i]["post"] for i in ordered]
        first = posts_in[0]
        handles = {_handle(p) for p in posts_in}
        ents: set[str] = set()
        for p in posts_in:
            ents |= xstories.extract_entities(p["text"], p["mentions"], p["links"])
        xc = len(handles)
        summary["stories"] += 1
        summary["members"] += len(posts_in)
        if dry_run:
            continue
        cur = con.execute(
            "INSERT INTO story (platform, created_at, window_hours, threshold,"
            " title, topic, canonical_content_id, first_content_id,"
            " first_mover_source_id, first_pub_at, last_pub_at, source_count,"
            " content_count, xconf, lead_time_min, topics, entities,"
            " is_new_entity, is_single, suspect, claimed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (PLATFORM, now_iso, window_hours, threshold, _label(posts_in), None,
             first["content_id"], first["content_id"], first.get("source_id"),
             first["published_at_utc"],
             max(p["published_at_utc"] or "" for p in posts_in),
             len({p.get("source_id") for p in posts_in if p.get("source_id")}),
             len(posts_in), xc, xstories.lead_time_min(posts_in), None,
             json.dumps(sorted(ents), ensure_ascii=False), 0, 0, 0, now_iso))
        story_id = cur.lastrowid
        for pos, p in enumerate(posts_in):
            con.execute(
                "INSERT OR REPLACE INTO story_member (story_id, content_id, role,"
                " is_canonical, added_at, handle) VALUES (?,?,?,?,?,?)",
                (story_id, p["content_id"], "primary" if pos == 0 else "echo",
                 1 if pos == 0 else 0, now_iso, _handle(p)))
    con.commit()
    return summary


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #
def cross_stories(con) -> list:
    return list(con.execute(
        "SELECT * FROM story WHERE platform = ? ORDER BY id", (PLATFORM,)))


def story_members(con, story_id: int) -> list:
    return list(con.execute(
        "SELECT sm.*, c.platform, c.external_id, c.url, c.title, c.text,"
        " c.published_at, c.author_handle"
        " FROM story_member sm JOIN content c ON c.id = sm.content_id"
        " WHERE sm.story_id = ?"
        " ORDER BY c.published_at ASC, c.platform ASC", (story_id,)))
