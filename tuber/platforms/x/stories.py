"""Сюжеты: кластеризация постов за окно 72 часа (ТЗ-3 Р2; пересборка 16.09.2026).

Алгоритм (переписан по итогам D-08):
  * решение о склейке принимает ВЗВЕШЕННОЕ пересечение:
      1. сущности взвешиваются по редкости (IDF по корпусу окна), а не
         сравниваются как множества (D-08);
      2. сущности-«гиганты» (встречаются в доле постов корпуса выше
         `STORY_GIANT_ENTITY_DOC_FREQ`) не могут быть достаточным признаком:
         в балле участвуют только редкие общие сущности;
      3. текстовое подтверждение — IDF-взвешенное пересечение токенов
         (доля от короткого поста), а не порог simhash (замер показал, что
         расстояния Хэмминга 23-42 при любом разумном пороге не срабатывают);
      4. время — ограничение кандидатов: посты одного события близки
         (`STORY_TIME_GATE_HOURS`), за его пределами склейки нет;
      5. кандидат сравнивается со ВСЕМИ участниками кластера, а не только с
         зачином, поэтому порядок постов не меняет результат.
  * роли участников: primary / echo / amplifier / extender;
  * метрики сюжета: xconf (независимые АВТОРЫ, не посты), first_mover,
    время первого поста, lead_time_min, topics, entities, is_new_entity;
  * одиночное мнение (xconf=1 без extender) помечается `single` и в главный
    блок не идёт, кроме funding/release со ссылкой на первоисточник;
  * накрутка (xconf≥3, все авторы с dup_ratio>0.5) помечается `suspect`.

Пороги `STORY_*` подобраны замером на размеченном наборе
`tests/data/story_pairs.jsonl` (см. `tests/test_story_pairs.py`): до правки
precision/recall/F1 = 0.286/0.121/0.170, после — 0.69/0.76/0.73.
Сеть здесь не используется вообще.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone

from . import ai_filter, config, store as db

# ------------------------------------------------------------------- simhash
_WORD_RE = re.compile(r"[a-zа-я0-9]+", re.I)


def _shingles(text, k=3):
    words = _WORD_RE.findall((text or "").lower())
    if len(words) < k:
        return set(words) or ({text.strip().lower()} if text and text.strip() else set())
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def simhash64(text):
    """64-битный simhash текста (детерминированный, только stdlib)."""
    v = [0] * 64
    for sh in _shingles(text):
        h = int(hashlib.md5(sh.encode("utf-8", "replace")).hexdigest()[:16], 16)
        for b in range(64):
            v[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(64):
        if v[b] > 0:
            out |= (1 << b)
    return out


def hamming(a, b):
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


# ------------------------------------------------------ токены (IDF, D-08)
# simhash64/hamming оставлены как диагностика (тест детерминированности), но в
# решении о склейке не участвуют: замер D-08 показал расстояния 23-42 бита при
# любом разумном пороге. Текстовое подтверждение считает `content_tokens`.
#
# Служебные слова не несут события: убираем их, иначе «the/today/new» склеивают
# всё подряд.
_STOPWORDS = frozenset("""
a an the and or but if then else for of to in on at by with from as is are was
were be been being it its this that these those i you he she we they them his
her their our your me my me us not no do does did done have has had will would
can could should may might must about into over under out up down more most very
just so than too also s t re ve ll d m new now today via per
""".split())


def content_tokens(text):
    """Множество значимых токенов текста (нижний регистр, без стоп-слов)."""
    words = _WORD_RE.findall((text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


# ----------------------------------------------------------------- сущности
_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")
_URL_RE = re.compile(r"https?://([^\s/<>\"']+)([^\s<>\"']*)?", re.I)
_GENERIC_HOSTS = {"x.com", "twitter.com", "t.co", "youtube.com", "youtu.be",
                  "instagram.com", "t.me", "facebook.com", "linkedin.com"}
# Словарь моделей/проектов (Р2.1). Совпадение — по границам слов.
_MODEL_WORDS = (
    "gpt", "chatgpt", "gpt-4", "gpt-5", "claude", "gemini", "llama", "mistral",
    "deepseek", "qwen", "grok", "sonnet", "opus", "haiku", "copilot", "cursor",
    "midjourney", "sora", "stable diffusion", "ollama", "langchain", "llamaindex",
    "hugging face", "huggingface", "transformers", "pytorch", "tensorflow",
    "vllm", "llama.cpp", "rag", "mcp", "diffusers", "windsurf", "cline", "aider",
)
_ORG_WORDS = (
    "openai", "anthropic", "google", "deepmind", "meta", "microsoft", "nvidia",
    "xai", "amazon", "apple", "cohere", "stability", "perplexity", "github",
    "huggingface", "mistral", "bytedance", "alibaba", "baidu", "tencent",
    "moonshot", "zhipu", "nous research", "eleutherai",
)
_ENT_WORDS = tuple(sorted(set(_MODEL_WORDS) | set(_ORG_WORDS), key=len, reverse=True))
_ENT_RE = re.compile(r"(?<![a-z0-9_-])(" + "|".join(re.escape(w) for w in _ENT_WORDS)
                     + r")(?![a-z0-9_-])", re.I)


def _json_list(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return value if isinstance(value, list) else [value]


def extract_entities(text, mentions=None, links=None):
    """Набор ключевых сущностей поста: упоминания, домены ссылок, модели/проекты."""
    ents = set()
    body = text or ""
    for m in _json_list(mentions) or []:
        m = str(m).lstrip("@").lower()
        if m:
            ents.add("@" + m)
    for m in _MENTION_RE.findall(body):
        ents.add("@" + m.lower())
    for l in _json_list(links) or []:
        u = _URL_RE.search(str(l))
        if u:
            host = u.group(1).lower()
            if host.startswith("www."):
                host = host[4:]
            if host not in _GENERIC_HOSTS:
                ents.add(host)
    for u in _URL_RE.finditer(body):
        host = u.group(1).lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in _GENERIC_HOSTS:
            ents.add(host)
    for m in _ENT_RE.finditer(body):
        ents.add(m.group(1).lower())
    return ents


# ---------------------------------------------------------------- кластеризация
def _core_entities(ents):
    """Только имена моделей/проектов и упоминания — без домен ссылок."""
    return {e for e in ents if e.startswith("@") or e in _ENT_WORDS}


def _doc_freq(items, key):
    df = {}
    for it in items:
        for x in key(it):
            df[x] = df.get(x, 0) + 1
    return df


def _feature(p):
    ents = extract_entities(p["text"], p["mentions"], p["links"])
    return {"post": p, "t": p["published_at_utc"] or "",
            "tokens": content_tokens(p["text"]), "ents": ents,
            "core": _core_entities(ents)}


def _hours_between(a, b):
    ta, tb = db.parse_iso(a), db.parse_iso(b)
    if not ta or not tb:
        return None
    return abs((tb - ta).total_seconds()) / 3600.0


class _MatchContext:
    """Редкость сущностей и токенов по корпусу окна + набор сущностей-«гигантов».

    IDF считается по постам окна (как simhash раньше): «гигант» — сущность,
    встречающаяся в доле постов корпуса не ниже
    `config.STORY_GIANT_ENTITY_DOC_FREQ`.
    """

    def __init__(self, feats):
        self.n = max(1, len(feats))
        self.ent_df = _doc_freq(feats, lambda f: f["ents"])
        self.tok_df = _doc_freq(feats, lambda f: f["tokens"])
        self.giants = {e for e, c in self.ent_df.items()
                       if c / self.n >= float(config.STORY_GIANT_ENTITY_DOC_FREQ)}

    def idf_ent(self, e):
        # +1 держит вес положительным даже у слова, встречающегося во всех
        # постах корпуса (иначе на маленьком корпусе все веса обнуляются).
        return 1.0 + _log((self.n + 1.0) / (1.0 + self.ent_df.get(e, 0)))

    def idf_tok(self, w):
        return 1.0 + _log((self.n + 1.0) / (1.0 + self.tok_df.get(w, 0)))


def _log(x):
    return math.log(x) if x > 0 else 0.0


def _pair_score(ctx, a, b):
    """Балл «одно событие» для пары постов. None — если не прошло окно времени.

    Балл = IDF-взвешенное пересечение токенов (доля от КОРОТКОГО поста)
           + вес редких общих сущностей (гиганты не в счёт).

    Слабое текстовое пересечение само по себе не склеивает: если общая только
    сущность-«гигант» (openai/nvidia/...) и текст совпал ниже «сильного»
    порога, балла нет. Это отсекает связку «разные события про одну и ту же
    большую компанию» (D-08), не мешая подтверждённым парам.
    """
    dt = _hours_between(a["t"], b["t"])
    if dt is None or dt > float(config.STORY_TIME_GATE_HOURS):
        return None
    common = a["tokens"] & b["tokens"]
    base = min(sum(ctx.idf_tok(w) for w in a["tokens"]),
               sum(ctx.idf_tok(w) for w in b["tokens"]))
    text_part = (sum(ctx.idf_tok(w) for w in common) / base) if base else 0.0
    shared_rare = [e for e in (a["ents"] & b["ents"]) if e not in ctx.giants]
    ent_part = sum(ctx.idf_ent(e) for e in shared_rare)
    ent_part = min(1.0, ent_part / float(config.STORY_ENTITY_IDF_NORM))
    if not shared_rare:
        # Без редкой общей сущности одного общего слова мало: нужно «сильное»
        # текстовое подтверждение И минимум два общих значимых слова. Иначе
        # одно слово-гигант (openai/nvidia) склеивало бы разные события.
        if (len(common) < int(config.STORY_MIN_SHARED_TOKENS)
                or text_part < float(config.STORY_TEXT_STRONG)):
            return 0.0
    return text_part + float(config.STORY_ENTITY_IDF_W) * ent_part


def cluster_posts(posts, *, threshold=None, ctx=None):
    """Разбить посты на кластеры. Возвращает список списков (по возрастанию времени).

    Посты: словари/строки sqlite с полями tweet_id, text, published_at_utc,
    mentions, links, is_retweet, is_quote, orig_handle, owner_handle.

    Склейка — агломеративная по СРЕДНЕЙ связи (average linkage): сливаются два
    кластера с наибольшим средним баллом, пока он не ниже порога. Одиночная
    связь «совпал с любым участником» здесь не годится: она тянет цепочку
    A~B~C и склеивает весь корпус через один обзорный пост. Средняя связь и
    выбор пары с максимальным баллом делают разбиение независимым от порядка
    постов (D-08 п.3).
    """
    threshold = (config.STORY_MATCH_MIN if threshold is None
                 else float(threshold))
    items = sorted(posts, key=lambda p: (p["published_at_utc"] or "",
                                         str(p["tweet_id"])))
    feats = [_feature(p) for p in items]
    ctx = ctx if ctx is not None else _MatchContext(feats)
    n = len(feats)
    scores = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            s = _pair_score(ctx, feats[i], feats[j])
            scores[i][j] = scores[j][i] = 0.0 if s is None else s
    groups = [[i] for i in range(n)]
    # Средняя связь пересчитывается только для пар с объединённым кластером.
    while len(groups) > 1:
        best = None
        for gi in range(len(groups)):
            a = groups[gi]
            for gj in range(gi + 1, len(groups)):
                b = groups[gj]
                link = (sum(scores[i][j] for i in a for j in b)
                        / (len(a) * len(b)))
                if link < threshold:
                    continue
                if best is None or link > best[0]:
                    best = (link, gi, gj)
        if best is None:
            break
        _link, gi, gj = best
        groups[gi] = groups[gi] + groups[gj]
        del groups[gj]
    return [[feats[i]["post"] for i in sorted(g)] for g in groups]


# --------------------------------------------------------------------- роли
def assign_roles(posts, primary_tweet_id=None):
    """Роли участников сюжета (Р2.3). Посты — в порядке времени.

    primary   — самый ранний пост сюжета;
    amplifier — ретвит/цитата (или ссылка/упоминание первоисточника);
    extender  — добавляет новую сущность, которой не было у предыдущих;
    echo      — пересказ без ссылки на первоисточник.
    """
    ordered = sorted(posts, key=lambda p: (p["published_at_utc"] or "",
                                           str(p["tweet_id"])))
    roles, seen_ents = [], set()
    primary = ordered[0] if ordered else None
    primary_tid = primary_tweet_id or (str(primary["tweet_id"]) if primary else None)
    primary_handle = (db.post_author(primary) or "").lower() if primary else ""
    for i, p in enumerate(ordered):
        if i == 0:
            roles.append((str(p["tweet_id"]), "primary"))
            seen_ents |= extract_entities(p["text"], p["mentions"], p["links"])
            continue
        ents = extract_entities(p["text"], p["mentions"], p["links"])
        handle = (db.post_author(p) or "").lower()
        links = [str(x) for x in _json_list(p["links"])]
        orig = (p["orig_handle"] or "").lower()
        is_amp = bool(p["is_retweet"] or p["is_quote"]
                      or (primary_tid and any(primary_tid in l for l in links))
                      or (primary_handle and orig == primary_handle)
                      or (primary_handle and ("@" + primary_handle) in ents))
        if is_amp:
            roles.append((str(p["tweet_id"]), "amplifier"))
        elif ents - seen_ents:
            roles.append((str(p["tweet_id"]), "extender"))
            seen_ents |= ents
        else:
            roles.append((str(p["tweet_id"]), "echo"))
        seen_ents |= ents
    return roles


def xconf(posts):
    """Число независимых АВТОРОВ сюжета, а не постов (Р2.4)."""
    handles = set()
    for p in posts:
        h = (db.post_author(p) or "").lower()
        if h:
            handles.add(h)
    return len(handles)


def _seconds_between(a, b):
    ta, tb = db.parse_iso(a), db.parse_iso(b)
    if not ta or not tb:
        return None
    return (tb - ta).total_seconds()


def lead_time_min(posts):
    """Минуты от первого поста до первого поста ВТОРОГО независимого автора."""
    ordered = sorted(posts, key=lambda p: (p["published_at_utc"] or "",
                                           str(p["tweet_id"])))
    if not ordered:
        return None
    first = ordered[0]
    first_h = (db.post_author(first) or "").lower()
    for p in ordered[1:]:
        h = (db.post_author(p) or "").lower()
        if h and h != first_h:
            sec = _seconds_between(first["published_at_utc"], p["published_at_utc"])
            return None if sec is None else round(sec / 60.0, 3)
    return None


# ------------------------------------------------------------------ выборка
def _select_window(con, window_hours, now):
    since = db.iso(now - timedelta(hours=window_hours))
    # ТЗ-8 задача 1: в сюжет попадают только классифицированные посты с
    # приговором модели is_ai=1. Посты с is_ai=0 и без приговора не кластеризуем.
    rows = con.execute(
        "SELECT p.*, a.handle AS acc_handle, a.dup_ratio AS acc_dup_ratio,"
        " c.topic AS c_topic, c.claim_type AS c_claim"
        " FROM posts p JOIN accounts a ON a.id=p.account_id "
        + ai_filter.ai_join("p", "c")
        + " WHERE p.deleted_at IS NULL"
        + " AND p.published_at_utc >= ? AND p.published_at_utc <= ?"
        + " ORDER BY p.published_at_utc ASC, p.tweet_id ASC",
        (since, db.iso(now))).fetchall()
    return since, list(rows)


def _entities_before(con, since):
    """Сущности, встречавшиеся в БД раньше окна (для is_new_entity)."""
    ents = set()
    for r in con.execute("SELECT text, mentions, links FROM posts"
                         " WHERE published_at_utc < ?", (since,)):
        ents |= extract_entities(r["text"], r["mentions"], r["links"])
    return ents


def _is_suspect(authors, dup_by_handle):
    if len(authors) < config.SUSPECT_MIN_XCONF:
        return False
    ratios = [dup_by_handle.get(h) for h in authors]
    if not ratios or any(r is None for r in ratios):
        return False
    return all(r > config.SUSPECT_DUP_RATIO for r in ratios)


# --------------------------------------------------------------------- запуск
def run(con, *, window_hours=None, threshold=None, run_id=None, now=None,
        dry_run=False):
    """Пересобрать сюжеты за окно. Идемпотентно (старые строки окна заменяются).

    ТЗ-6 фикс: `dry_run=True` считает сюжеты, но не пишет в БД (иначе CLI
    `stories --dry-run` падал с KeyError: в сводке не было ключа dry_run).
    """
    now = now or datetime.now(timezone.utc)
    window_hours = int(window_hours or config.STORIES_WINDOW_HOURS)
    # threshold — порог балла склейки (доля), не биты simhash (D-08).
    threshold = (config.STORY_MATCH_MIN if threshold is None
                 else float(threshold))
    since, rows = _select_window(con, window_hours, now)
    older_ents = _entities_before(con, since)
    dup_by_handle = {r["handle"].lower(): r["dup_ratio"] for r in con.execute(
        "SELECT handle, dup_ratio FROM accounts WHERE handle IS NOT NULL")}

    clusters = cluster_posts(rows, threshold=threshold)
    summary = {"window_hours": window_hours, "threshold": threshold,
               "posts": len(rows), "stories": 0, "multi": 0, "single": 0,
               "suspect": 0, "new_entity": 0, "dry_run": bool(dry_run)}

    if not dry_run:
        con.execute("DELETE FROM story_posts WHERE story_id IN"
                    " (SELECT id FROM stories WHERE created_at >= ?)", (since,))
        con.execute("DELETE FROM stories WHERE created_at >= ?", (since,))
        con.commit()

    for posts in clusters:
        roles = assign_roles(posts)
        role_by_tid = dict(roles)
        authors = {(db.post_author(p) or "").lower() for p in posts}
        authors.discard("")
        xc = len(authors)
        ordered = sorted(posts, key=lambda p: (p["published_at_utc"] or "",
                                               str(p["tweet_id"])))
        first = ordered[0]
        ents = set()
        for p in posts:
            ents |= extract_entities(p["text"], p["mentions"], p["links"])
        topics = sorted({p["c_topic"] for p in posts if p["c_topic"]})
        claims = {p["c_claim"] for p in posts if p["c_claim"]}
        has_ext = any(r == "extender" for _t, r in roles)
        has_source_link = any(
            (p["c_claim"] in ("funding", "release")
             and (p["text"] and ("http" in (p["text"] or ""))
                  or _json_list(p["links"])))
            for p in posts)
        is_single = 1 if (xc == 1 and not has_ext
                          and not (claims & {"funding", "release"} and has_source_link)
                          ) else 0
        is_new = 1 if (ents - older_ents) else 0
        suspect = 1 if _is_suspect(authors, dup_by_handle) else 0
        created = db.iso(now)
        if not dry_run:
            con.execute(
                """INSERT INTO stories (created_at, window_hours, threshold,
                     first_tweet_id, first_mover, published_at, xconf, post_count,
                     lead_time_min, topics, entities, is_new_entity, is_single,
                     suspect, claimed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (created, window_hours, threshold, str(first["tweet_id"]),
                 db.post_author(first),
                 first["published_at_utc"], xc, len(posts), lead_time_min(posts),
                 json.dumps(topics, ensure_ascii=False),
                 json.dumps(sorted(ents), ensure_ascii=False), is_new, is_single,
                 suspect, db.iso(now)))
            # lastrowid после записи в представление всегда 0: настоящий id
            # возвращает триггер через temp.x_last_insert (см. store.last_insert_id).
            story_id = db.last_insert_id(con)
            for p in posts:
                con.execute(
                    "INSERT OR REPLACE INTO story_posts (story_id, tweet_id, handle,"
                    " role, added_at) VALUES (?,?,?,?,?)",
                    (story_id, str(p["tweet_id"]),
                     db.post_author(p),
                     role_by_tid.get(str(p["tweet_id"])), db.iso(now)))
        summary["stories"] += 1
        summary["multi"] += 1 if xc >= 2 else 0
        summary["single"] += is_single
        summary["suspect"] += suspect
        summary["new_entity"] += is_new
    con.commit()
    if threshold != config.STORY_MATCH_MIN:
        db.log_run(con, "INFO",
                   f"stories: STORY_MATCH_MIN={threshold} (изменён с "
                   f"{config.STORY_MATCH_MIN})", run_id=run_id)
        con.commit()
    return summary


# ------------------------------------------------------------------ чтение
def story_for_tweet(con, tweet_id):
    return con.execute(
        "SELECT s.*, sp.role FROM story_posts sp JOIN stories s ON s.id=sp.story_id"
        " WHERE sp.tweet_id=? LIMIT 1", (str(tweet_id),)).fetchone()


def story_posts(con, story_id):
    return list(con.execute(
        "SELECT sp.tweet_id, sp.handle, sp.role, p.text, p.lang,"
        " p.published_at_utc, p.is_retweet, p.is_quote, p.links, p.mentions"
        " FROM story_posts sp LEFT JOIN posts p ON p.tweet_id=sp.tweet_id"
        " WHERE sp.story_id=? ORDER BY p.published_at_utc ASC, p.tweet_id ASC", (story_id,)))


def main_stories(con, limit=10, *, include_single=False, since_hours=None):
    """Сюжеты для главного блока: не single, не suspect, по xconf убыв."""
    q = "SELECT * FROM stories WHERE suspect=0"
    params = []
    if not include_single:
        q += " AND is_single=0"
    if since_hours:
        cutoff = db.iso(datetime.now(timezone.utc) - timedelta(hours=since_hours))
        q += " AND published_at >= ?"
        params.append(cutoff)
    q += " ORDER BY xconf DESC, published_at DESC LIMIT ?"
    params.append(int(limit))
    return list(con.execute(q, params))
