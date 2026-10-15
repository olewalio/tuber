"""Расширение поиска видео и каналов (этап 9).

Методология — docs/METHODOLOGY-EXPANSION-SHORTS.md, часть A.

Принципы:
- порядок трат от бесплатных механизмов к дорогим:
  mine_mentions (0) → mine_queries (0) → резолв упоминаний (1/50) →
  чарты (1/50) → плейлисты (1/50) → поиск каналов (100/вызов) → проверка;
- кандидат — не канал: он попадает в channel_candidates со статусом new и
  участвует в обходе только после смысловой проверки (>=2 подтверждённых
  ИИ-видео), иначе rejected с причиной;
- дедупликация до траты: известный канал и уже проверенный кандидат не тратят
  вызов API;
- бюджет важнее полноты: EXPAND_BUDGET_UNITS_PER_RUN останавливает прогон.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Any

from . import classify as classify_mod
from . import collect, config, store as db, api as yt

log = logging.getLogger(__name__)

# Верхний предел расхода квоты на один прогон.
BUDGET_UNITS_PER_RUN = 2000


# --- регулярные выражения упоминаний ---------------------------------------

_YT_CHANNEL_RE = re.compile(r"youtube\.com/channel/(UC[A-Za-z0-9_\-]{20,24})")
_YT_HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.\-]{3,30})")
_YT_C_RE = re.compile(r"youtube\.com/c/([A-Za-z0-9_.\-]{2,60})")
_YT_USER_RE = re.compile(r"youtube\.com/user/([A-Za-z0-9_.\-]{2,60})")

# Токен для разбора запросов: буквы (латиница и кириллица), 2+ знака.
_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ]{2,}")
_HASHTAG_RE = re.compile(r"#\S+")

# Стоп-слова (русские и английские), чтобы запросы не собирались из мусора.
STOPWORDS = frozenset({
    # русские
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех",
    "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть",
    "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего",
    "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем", "хорошо",
    "свою", "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя",
    "такой", "им", "более", "всегда", "конечно", "всю", "между", "это",
    "просто", "самый", "весь", "ваш", "под", "этих", "сама", "сегодня",
    "видео", "канал", "канале", "подпишись", "подписка", "смотри", "новое",
    "short", "shorts", "youtube", "https", "www", "com", "бою", "про", "оно",
    # английские
    "the", "and", "for", "you", "with", "that", "this", "are", "was", "were",
    "from", "have", "has", "had", "not", "but", "his", "her", "they", "them",
    "what", "when", "where", "which", "who", "how", "why", "will", "would",
    "can", "could", "should", "about", "into", "over", "your", "our", "their",
    "its", "it's", "don't", "does", "did", "just", "more", "most", "some",
    "any", "all", "one", "two", "new", "get", "got", "out", "use", "using",
    "video", "channel", "subscribe", "watch", "like", "share", "https", "www",
    "com", "www", "youtube", "short", "shorts", "full", "part", "episode",
    "to", "is", "of", "in", "on", "at", "by", "as", "an", "or", "be", "we",
    "he", "she", "if", "do", "my", "me", "up", "so", "no", "it", "am", "been",
    "being", "than", "then", "there", "here", "also", "only", "even", "very",
    "much", "make", "made", "let", "lets", "via", "vs", "the", "и",
})


def _norm_query(text: str) -> str:
    """Нормализовать запрос/фразу для сравнения."""
    return " ".join(str(text).lower().split())


# --- фильтр качества поисковых запросов (этап 11) --------------------------
#
# Дефект 1: mine_queries принимал всё подряд, включая односложные общие слова
# (ai, code, news, tools) и мусор вне темы. Один поисковый прогон стоит 100
# units, поэтому такой запрос гарантированно жжёт квоту впустую.

# Точные слова-корни: совпадают только целиком (иначе 'ai' ловится внутри
# 'email', а 'ml' внутри 'html').
AI_ROOT_WORDS = frozenset({
    "ai", "ии", "gpt", "llm", "ml", "rag",
})
# Корни-подстроки: длинные стемы, безопасные для поиска по вхождению.
AI_ROOT_PREFIXES = (
    "нейро", "нейросет", "машинн", "openai", "claude", "gemini", "anthropic",
    "агент", "agent", "copilot", "midjourney", "chatgpt", "deepseek",
    "nvidia", "трансформер", "inference", "fine-tun", "prompt",
    "stable diffusion",
)
# Общие слова: поодиночке не запрос, допустимы только рядом с ИИ-термином.
GENERIC_WORDS = frozenset({
    "news", "tutorial", "code", "tools", "review", "update", "2026", "how",
    "best", "top", "vs", "новини", "новости", "урок", "обзор", "топ",
})
# Границы слова для точных корней (латиница + кириллица + цифры).
_ROOT_WORD_RE = {
    w: re.compile(rf"(?<![a-zа-яё0-9]){re.escape(w)}(?![a-zа-яё0-9])")
    for w in AI_ROOT_WORDS
}


def has_ai_marker(text: Any) -> bool:
    """Есть ли в тексте ИИ-корень (тот же список, что и в фильтре запросов)."""
    q = _norm_query(text)
    if not q:
        return False
    for word, pattern in _ROOT_WORD_RE.items():
        if pattern.search(q):
            return True
    return any(prefix in q for prefix in AI_ROOT_PREFIXES)


def is_ai_query(query: Any) -> bool:
    """Годится ли запрос-кандидат к работе.

    Принимаем, если выполнено хотя бы одно:
    - содержит ИИ-корень (AI_ROOT_WORDS/AI_ROOT_PREFIXES);
    - состоит из двух и более слов, каждое из которых не стоп- и не общее
      слово (то есть является биграммой из тематических слов).

    Односложные запросы без ИИ-корня отсекаются.
    """
    q = _norm_query(query)
    if not q:
        return False
    if has_ai_marker(q):
        return True
    tokens = _TOKEN_RE.findall(q)
    if len(tokens) < 2:
        return False
    return all(t not in STOPWORDS and t not in GENERIC_WORDS for t in tokens)



def looks_russian(title: Any, description: Any = None) -> bool:
    """Есть ли кириллица в названии/описании канала."""
    text = f"{title or ''} {description or ''}"
    for ch in text:
        if "а" <= ch.lower() <= "я" or ch.lower() == "ё":
            return True
    return False


# --- извлечение упоминаний --------------------------------------------------

def extract_mentions(text: str | None) -> tuple[set[str], set[str]]:
    """Достать из текста (channel_id, handle) упоминания.

    Учитываются ссылки youtube.com/@handle, /channel/UC…, /c/, /user/.
    Handle нормализуется без ведущего '@' и в нижнем регистре.
    """
    if not text:
        return set(), set()
    channel_ids = set(_YT_CHANNEL_RE.findall(text))
    handles: set[str] = set()
    for raw in _YT_HANDLE_RE.findall(text):
        handles.add(raw.lstrip("@").rstrip(".").lower())
    for raw in _YT_C_RE.findall(text):
        handles.add(raw.lstrip("@").rstrip(".").lower())
    for raw in _YT_USER_RE.findall(text):
        handles.add(raw.lstrip("@").rstrip(".").lower())
    handles.discard("")
    return channel_ids, handles


# --- скоринг ----------------------------------------------------------------

def candidate_score(
    source: str,
    subscriber_count: int | None = None,
    mentions: int = 1,
    cfg: Any = config,
) -> float:
    """Оценка кандидата: вес источника + логарифм подписчиков + повторы.

    Русскоязычность в score не входит: при равном score она решает исход
    отдельным ключом сортировки (см. candidate_sort_key).
    """
    weights = getattr(cfg, "EXPAND_SOURCE_WEIGHTS", config.EXPAND_SOURCE_WEIGHTS)
    weight = float(weights.get(source, 1))
    subs = max(int(subscriber_count or 0), 0)
    repeats = max(int(mentions or 1), 1) - 1
    return round(weight + math.log10(subs + 1) + 0.5 * repeats, 4)


def candidate_sort_key(row: Any) -> tuple:
    """Ключ приоритета: score, затем русский язык, затем повторы/подписчики."""
    try:
        score = float(row["score"] or 0.0)
    except (TypeError, ValueError, IndexError, KeyError):
        score = 0.0
    try:
        ru = 1 if looks_russian(row["title"], row["description"]) else 0
    except (TypeError, IndexError):
        ru = 0
    try:
        mentions = int(row["mentions"] or 1)
    except (TypeError, ValueError, IndexError, KeyError):
        mentions = 1
    try:
        subs = int(row["subscriber_count"] or 0)
    except (TypeError, ValueError, IndexError, KeyError):
        subs = 0
    return (-score, -ru, -mentions, -subs)


# --- бюджет -----------------------------------------------------------------

class _Budget:
    """Учёт расхода квоты внутри одного прогона (units из quota_log)."""

    def __init__(self, conn: Any, limit: int):
        self.conn = conn
        self.limit = int(limit)
        self.start = _quota_total(conn)
        self.stopped_reason: str | None = None

    def spent(self) -> int:
        return _quota_total(self.conn) - self.start

    def remaining(self) -> int:
        return self.limit - self.spent()

    def can_spend(self, cost: int) -> bool:
        return self.spent() + max(int(cost), 0) <= self.limit

    def note(self, reason: str) -> None:
        if self.stopped_reason is None:
            self.stopped_reason = reason


def _quota_total(conn: Any) -> int:
    """Суммарный расход квоты по quota_log."""
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(units),0) AS u FROM quota_log"
        ).fetchone()
    except Exception:  # нет таблицы/соединения — считаем нулём
        return 0
    return int(row["u"]) if row else 0


# --- состояние прогона ------------------------------------------------------

def _new_state(conn: Any, cfg: Any = config) -> dict[str, Any]:
    """Рабочее состояние прогона: счётчики и кэши."""
    sources = list(getattr(cfg, "EXPAND_SOURCE_WEIGHTS", config.EXPAND_SOURCE_WEIGHTS))
    return {
        "sources": {s: 0 for s in sources},
        "known": db.known_channel_ids(
            conn, [r["channel_id"] for r in conn.execute("SELECT channel_id FROM channels")]
        ),
        "candidates": {
            r["channel_id"]: r
            for r in conn.execute("SELECT * FROM channel_candidates")
        },
        "accepted": 0,
        "rejected": 0,
        "steps": [],
    }


def _add_candidate(
    conn: Any,
    state: dict[str, Any],
    channel_id: str | None,
    source: str,
    evidence: str | None = None,
    title: str | None = None,
    handle: str | None = None,
    description: str | None = None,
    subscriber_count: int | None = None,
    cfg: Any = config,
) -> bool:
    """Добавить кандидата с дедупликацией до траты. True — новая запись.

    Известный канал (channels) и отклонённый кандидат не возрождаются.
    Повторная находка увеличивает mentions и пересчитывает score.
    """
    if not channel_id:
        return False
    if channel_id in state["known"]:
        return False
    existing = state["candidates"].get(channel_id)
    if existing is not None:
        if existing["status"] == "rejected":
            return False
        db.bump_candidate_mention(conn, channel_id, evidence, source)
        mentions = int(existing["mentions"] or 1) + 1
        subs = subscriber_count if subscriber_count is not None else existing["subscriber_count"]
        score = candidate_score(source, subs, mentions, cfg)
        conn.execute(
            "UPDATE channel_candidates SET score=?, mentions=?, "
            "subscriber_count=COALESCE(?, subscriber_count), "
            "title=COALESCE(?, title), handle=COALESCE(?, handle) "
            "WHERE channel_id=?",
            (score, mentions, subscriber_count, title, handle, channel_id),
        )
        conn.commit()
        state["candidates"] = {
            r["channel_id"]: r
            for r in conn.execute("SELECT * FROM channel_candidates")
        }
        return False

    score = candidate_score(source, subscriber_count, 1, cfg)
    db.upsert_channel_candidate(
        conn,
        {
            "channel_id": channel_id,
            "title": title,
            "handle": handle,
            "description": description,
            "source": source,
            "evidence": evidence,
            "mentions": 1,
            "subscriber_count": subscriber_count,
            "score": score,
            "status": "new",
        },
    )
    state["candidates"] = {
        r["channel_id"]: r
        for r in conn.execute("SELECT * FROM channel_candidates")
    }
    state["sources"][source] = state["sources"].get(source, 0) + 1
    return True


# --- шаг 1: упоминания (0 units) -------------------------------------------

def mine_mentions(conn: Any, cfg: Any = config,
                  state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Регекс по описаниям ИИ-видео: handle/channel/c/user. Стоимость 0 units.

    Кандидаты со source='mention', evidence = id видео-донора.
    Отсекаются самоупоминания и уже известные каналы.
    """
    if state is None:
        state = _new_state(conn, cfg)
    rows = conn.execute(
        """
        SELECT v.video_id, v.description, v.channel_id,
               c.handle AS channel_handle
        FROM videos v
        JOIN video_classification vc ON vc.video_id = v.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE vc.is_ai = 1 AND v.description IS NOT NULL AND v.description != ''
        """
    ).fetchall()

    new_ids = 0
    new_handles = 0
    for row in rows:
        channel_ids, handles = extract_mentions(row["description"])
        donor = row["video_id"]
        own_id = row["channel_id"]
        own_handle = (row["channel_handle"] or "").lstrip("@").lower()
        for cid in channel_ids:
            if cid == own_id:
                continue  # самоупоминание
            if _add_candidate(conn, state, cid, "mention",
                              evidence=f"video:{donor}", cfg=cfg):
                new_ids += 1
        for handle in handles:
            if own_handle and handle == own_handle:
                continue  # самоупоминание
            if _add_candidate(conn, state, f"@{handle}", "mention",
                              evidence=f"video:{donor}", handle=handle, cfg=cfg):
                new_handles += 1

    return {
        "videos_scanned": len(rows),
        "new_channel_ids": new_ids,
        "new_handles": new_handles,
        "units": 0,
    }


# --- шаг 2: авто-запросы из данных (0 units) --------------------------------

def _video_texts(conn: Any) -> list[tuple[str, str]]:
    """(video_id, текст) по ИИ-видео: заголовок + теги."""
    import json as _json

    rows = conn.execute(
        """
        SELECT v.video_id, v.title, v.tags
        FROM videos v
        JOIN video_classification vc ON vc.video_id = v.video_id
        WHERE vc.is_ai = 1
        """
    ).fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        tags = ""
        if row["tags"]:
            try:
                loaded = _json.loads(row["tags"])
                if isinstance(loaded, list):
                    tags = " ".join(str(t) for t in loaded)
            except (ValueError, TypeError):
                tags = str(row["tags"])
        out.append((row["video_id"], f"{row['title'] or ''} {tags}"))
    return out


def _tokens(text: str) -> list[str]:
    """Слова текста: без хэштегов, эмодзи, цифр и одиночных букв."""
    cleaned = _HASHTAG_RE.sub(" ", text or "")
    return [t.lower() for t in _TOKEN_RE.findall(cleaned)]


def mine_queries(conn: Any, cfg: Any = config,
                 min_hits: int = 3) -> dict[str, Any]:
    """Частотные термины и биграммы из ИИ-видео. Стоимость 0 units.

    Порог: встретился у >= min_hits разных видео. Стоп-слова, эмодзи,
    хэштеги, одиночные буквы и чистые цифры отсеиваются.
    """
    existing = {_norm_query(q) for q in getattr(cfg, "SEARCH_QUERIES", [])}
    doc_freq: Counter = Counter()
    evidence: dict[str, str] = {}
    for video_id, text in _video_texts(conn):
        tokens = _tokens(text)
        seen: set[str] = set()
        for tok in tokens:
            if tok in STOPWORDS or len(tok) < 2:
                continue
            seen.add(tok)
        for i in range(len(tokens) - 1):
            a, b = tokens[i], tokens[i + 1]
            if a in STOPWORDS or b in STOPWORDS or a == b:
                continue
            if len(a) < 2 or len(b) < 2:
                continue
            seen.add(f"{a} {b}")
        for term in seen:
            if term in existing:
                continue
            doc_freq[term] += 1
            evidence.setdefault(term, f"video:{video_id}")

    candidates = [(term, n) for term, n in doc_freq.items() if n >= min_hits]
    # Фильтр качества: мусорные и общие односложные запросы в базу не попадают.
    filtered = [(term, n) for term, n in candidates if is_ai_query(term)]
    dropped = len(candidates) - len(filtered)
    filtered.sort(key=lambda kv: (-kv[1], kv[0]))
    # Верхняя отсечка, чтобы не заваливать таблицу.
    filtered = filtered[:500]

    now = str(config.now_ts())
    for term, hits in filtered:
        db.upsert_query_candidate(
            conn,
            {
                "query": term,
                "source": "term_mining",
                "evidence": evidence.get(term),
                "hits": hits,
                "score": float(hits),
                "status": "new",
                "kind": "query",
                "discovered_at": now,
            },
        )
    return {
        "terms_found": len(filtered),
        "terms_filtered": dropped,
        "min_hits": min_hits,
        "units": 0,
    }


# --- приём запросов в работу (0 units) --------------------------------------

def accept_query_candidates(conn: Any, cfg: Any = config,
                            limit: int | None = None) -> dict[str, Any]:
    """Перевести лучшие добытые запросы из new в accepted, мусор — в rejected.

    Эксплуатационный предел: не больше EXPAND_MAX_QUERIES_PER_RUN новых
    запросов за прогон. Раньше автоприём переводил в работу всё подряд.
    """
    if limit is None:
        limit = getattr(cfg, "EXPAND_MAX_QUERIES_PER_RUN",
                        config.EXPAND_MAX_QUERIES_PER_RUN)
    rows = conn.execute(
        "SELECT query, hits FROM query_candidates "
        "WHERE source='term_mining' AND status='new' AND kind='query'"
    ).fetchall()
    passing: list[Any] = []
    rejected = 0
    for row in rows:
        if is_ai_query(row["query"]):
            passing.append(row)
        else:
            db.set_query_candidate_status(conn, row["query"], "rejected",
                                          "не ИИ-запрос")
            rejected += 1
    passing.sort(key=lambda r: (-int(r["hits"] or 0), str(r["query"])))
    accepted = 0
    for row in passing[: max(int(limit), 0)]:
        db.set_query_candidate_status(conn, row["query"], "accepted", None)
        accepted += 1
    return {
        "accepted": accepted,
        "rejected": rejected,
        "eligible": len(passing),
        "queued": max(len(passing) - accepted, 0),
        "units": 0,
    }


def migrate_expand_filters(conn: Any, cfg: Any = config) -> dict[str, Any]:
    """Миграция этапа 11 по боевой базе. Идемпотентна.

    1) Добытые запросы (source='term_mining') сбрасываются в new и прогоняются
       через фильтр качества; лучшие (до EXPAND_MAX_QUERIES_PER_RUN) — в accepted,
       мусор — в rejected.
    2) Кандидаты, отклонённые только из-за неразрешённого handle, возвращаются
       в unresolved с одной учтённой попыткой.
    3) Чарт-кандидаты без ИИ-признака в названии/описании — rejected.
    """
    limit = getattr(cfg, "EXPAND_MAX_QUERIES_PER_RUN",
                    config.EXPAND_MAX_QUERIES_PER_RUN)

    # 1. Запросы.
    conn.execute(
        "UPDATE query_candidates SET status='new', reject_reason=NULL "
        "WHERE source='term_mining' AND kind='query'"
    )
    conn.commit()
    rows = conn.execute(
        "SELECT query, hits FROM query_candidates "
        "WHERE source='term_mining' AND kind='query'"
    ).fetchall()
    passing: list[Any] = []
    queries_rejected = 0
    for row in rows:
        if is_ai_query(row["query"]):
            passing.append(row)
        else:
            db.set_query_candidate_status(conn, row["query"], "rejected",
                                          "не ИИ-запрос")
            queries_rejected += 1
    passing.sort(key=lambda r: (-int(r["hits"] or 0), str(r["query"])))
    queries_accepted = 0
    for row in passing[: max(int(limit), 0)]:
        db.set_query_candidate_status(conn, row["query"], "accepted", None)
        queries_accepted += 1

    # 2. Потерянные из-за лимита handle — обратно в unresolved.
    handles_requeued = db.requeue_unresolved_handles(conn)

    # 3. Чарт-шум без ИИ-признака.
    chart_rows = conn.execute(
        "SELECT channel_id, title, description FROM channel_candidates "
        "WHERE source='chart' AND status='new'"
    ).fetchall()
    charts_rejected = 0
    for row in chart_rows:
        text = f"{row['title'] or ''} {row['description'] or ''}"
        if not has_ai_marker(text):
            db.set_candidate_status(conn, row["channel_id"], "rejected",
                                    "нет ИИ-признака", str(config.now_ts()))
            charts_rejected += 1

    return {
        "queries_total": len(rows),
        "queries_accepted": queries_accepted,
        "queries_rejected": queries_rejected,
        "handles_requeued": handles_requeued,
        "charts_total": len(chart_rows),
        "charts_rejected": charts_rejected,
        "charts_kept": len(chart_rows) - charts_rejected,
    }


# --- резолв упоминаний (1 unit / 50) ---------------------------------------

def resolve_mentions(client: Any, conn: Any, cfg: Any = config,
                     state: dict[str, Any] | None = None,
                     budget: _Budget | None = None) -> dict[str, Any]:
    """Превратить 'хендл-кандидатов' в channel_id через channels?forHandle."""
    if state is None:
        state = _new_state(conn, cfg)
    budget = budget or _Budget(conn, getattr(cfg, "EXPAND_BUDGET_UNITS_PER_RUN", BUDGET_UNITS_PER_RUN))

    pending = [
        r for r in state["candidates"].values()
        if str(r["channel_id"]).startswith("@")
        and r["status"] in ("new", "unresolved")
    ]
    if not pending:
        return {"resolved": 0, "unresolved": 0, "calls": 0, "units": 0}
    # Сначала те, кого ещё не пробовали (resolve_attempts), затем чаще упомянутые.
    pending.sort(key=lambda r: (int(r["resolve_attempts"] or 0),
                                -(int(r["mentions"] or 1))))

    max_resolves = getattr(cfg, "EXPAND_MAX_HANDLE_RESOLVES_PER_RUN",
                           config.EXPAND_MAX_HANDLE_RESOLVES_PER_RUN)
    max_attempts = getattr(cfg, "EXPAND_MAX_RESOLVE_ATTEMPTS",
                           config.EXPAND_MAX_RESOLVE_ATTEMPTS)
    resolved = 0
    unresolved = 0
    calls = 0
    stopped_reason: str | None = None
    for index, row in enumerate(pending):
        if calls >= int(max_resolves):
            budget.note(f"лимит резолва handle ({max_resolves})")
            stopped_reason = "лимит резолва handle"
            _defer_handles(conn, pending[index:], stopped_reason)
            unresolved += len(pending) - index
            break
        if not budget.can_spend(1):
            budget.note("бюджет исчерпан на резолве упоминаний")
            stopped_reason = "бюджет исчерпан на резолве упоминаний"
            _defer_handles(conn, pending[index:], stopped_reason)
            unresolved += len(pending) - index
            break
        prov = str(row["channel_id"])
        handle = str(row["handle"] or prov[1:]).lstrip("@").lower()
        calls += 1
        try:
            items = client.channels_by_handle(["@" + handle])
        except yt.YouTubeError as exc:
            log.warning("резолв упоминаний не удался: %s", exc)
            # Текущий handle — реальная неудачная попытка, остальные откладываем.
            db.mark_candidate_unresolved(conn, prov, "временная ошибка API",
                                         int(max_attempts))
            _defer_handles(conn, pending[index + 1:], "временная ошибка API")
            unresolved += len(pending) - index
            stopped_reason = "ошибка API на резолве"
            break
        item = None
        for got in items:
            custom = ((got.get("snippet") or {}).get("customUrl") or "").lstrip("@").lower()
            if custom == handle or not custom:
                item = got
                break
        if item is None:
            # Не разрешилось: не теряем канал, но ограничиваем число попыток.
            db.mark_candidate_unresolved(conn, prov, "handle не разрешён",
                                         int(max_attempts))
            unresolved += 1
            continue
        cid = item.get("id")
        if not cid:
            db.mark_candidate_unresolved(conn, prov, "handle без channel_id",
                                         int(max_attempts))
            unresolved += 1
            continue
        if cid in state["known"]:
            db.set_candidate_status(conn, prov, "rejected", "канал уже известен",
                                    str(config.now_ts()))
            unresolved += 1
            continue
        # Переносим улику на настоящий channel_id.
        db.upsert_channel_candidate(
            conn,
            {
                "channel_id": cid,
                "title": (item.get("snippet") or {}).get("title"),
                "handle": handle,
                "description": (item.get("snippet") or {}).get("description"),
                "source": "mention",
                "evidence": row["evidence"],
                "subscriber_count": _subs(item),
                "score": candidate_score("mention", _subs(item),
                                         int(row["mentions"] or 1), cfg),
                "status": "new",
            },
        )
        conn.execute("DELETE FROM channel_candidates WHERE channel_id=?", (prov,))
        conn.commit()
        state["sources"]["mention"] = state["sources"].get("mention", 0) + 1
        resolved += 1
    state["candidates"] = {
        r["channel_id"]: r
        for r in conn.execute("SELECT * FROM channel_candidates")
    }
    return {"resolved": resolved, "unresolved": unresolved, "calls": calls,
            "units": calls, "stopped_reason": stopped_reason}


def _defer_handles(conn: Any, rows: Any, reason: str) -> None:
    """Отложить необработанные handle-кандидаты в unresolved.

    Отсрочка из-за лимита/бюджета не считается неудачной попыткой: иначе
    живые каналы отклонялись бы из-за одного лишь исчерпания лимита в прогоне.
    Счётчик resolve_attempts растёт только на реальных обращениях к API.
    """
    for row in rows:
        db.set_candidate_status(conn, str(row["channel_id"]), "unresolved",
                                reason)


def _subs(item: Any) -> int | None:
    """Подписчики канала (None при скрытых данных)."""
    stats = item.get("statistics") or {}
    if stats.get("hiddenSubscriberCount"):
        return None
    value = stats.get("subscriberCount")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- шаг 3: чарты (1 unit / 50) --------------------------------------------

def _scan_seen(conn: Any, key: str, kind: str = "query") -> bool:
    """Проведено ли уже это сканирование (маркер в query_candidates).

    kind отсекает служебные ключи чарта от настоящих поисковых запросов:
    для чартов передаётся kind='chart', для остальных маркеров — 'query'.
    """
    row = conn.execute(
        "SELECT 1 FROM query_candidates WHERE query=? AND kind=? LIMIT 1",
        (key, kind),
    ).fetchone()
    return row is not None


def _mark_scan(conn: Any, key: str, source: str, evidence: str | None = None,
               kind: str = "query") -> None:
    """Отметить сканирование, чтобы не тратить квоту повторно."""
    db.upsert_query_candidate(
        conn,
        {
            "query": key,
            "source": source,
            "evidence": evidence,
            "hits": 1,
            "score": 0.0,
            "status": "accepted",
            "kind": kind,
        },
    )


def scan_charts(client: Any, conn: Any, cfg: Any = config,
                state: dict[str, Any] | None = None,
                budget: _Budget | None = None) -> dict[str, Any]:
    """Чарт популярного по регионам × категориям → новые channel_id."""
    if state is None:
        state = _new_state(conn, cfg)
    budget = budget or _Budget(conn, getattr(cfg, "EXPAND_BUDGET_UNITS_PER_RUN", BUDGET_UNITS_PER_RUN))

    calls = 0
    new = 0
    scanned = 0
    filtered = 0
    for region in getattr(cfg, "CHART_REGIONS", config.CHART_REGIONS):
        for cat in getattr(cfg, "CHART_CATEGORY_IDS", config.CHART_CATEGORY_IDS):
            key = f"chart:{region}:{cat}"
            if _scan_seen(conn, key, kind="chart"):
                continue  # уже сканировали ранее — не тратим
            if not budget.can_spend(config.COST_VIDEOS):
                budget.note(f"бюджет исчерпан на чартах ({region}/{cat})")
                return {"new": new, "scanned": scanned, "calls": calls,
                        "filtered": filtered, "units": calls}
            calls += 1
            try:
                items = client.chart_videos(region, cat)
            except yt.YouTubeError as exc:
                # Для части регион×категория чарта нет — это не сбой прогона.
                log.warning("чарт %s/%s не удался: %s", region, cat, exc)
                _mark_scan(conn, key, "chart", key, kind="chart")
                continue
            scanned += len(items)
            for item in items:
                cid = (item.get("snippet") or {}).get("channelId")
                title = (item.get("snippet") or {}).get("channelTitle")
                # Дефект 3: чарт забивал очередь музыкой, спортом и новостями.
                # В очередь идёт только канал с ИИ-признаком в названии.
                if not has_ai_marker(title):
                    filtered += 1
                    continue
                if _add_candidate(conn, state, cid, "chart",
                                  evidence=key, title=title, cfg=cfg):
                    new += 1
            _mark_scan(conn, key, "chart", key, kind="chart")
    return {"new": new, "scanned": scanned, "calls": calls, "filtered": filtered,
            "units": calls}


# --- шаг 4: плейлисты принятых каналов (1 unit / 50) -----------------------

def _ai_channels(conn: Any, cfg: Any = config, limit: int | None = None) -> list[str]:
    """channel_id каналов, подтверждённых как ИИ (>= MIN_AI_VIDEOS_PER_CHANNEL)."""
    min_ai = getattr(cfg, "MIN_AI_VIDEOS_PER_CHANNEL",
                     config.MIN_AI_VIDEOS_PER_CHANNEL)
    sql = """
        SELECT v.channel_id AS cid, COUNT(*) AS n
        FROM video_classification vc
        JOIN videos v ON v.video_id = vc.video_id
        WHERE vc.is_ai = 1
        GROUP BY v.channel_id
        HAVING COUNT(*) >= ?
        ORDER BY n DESC
    """
    rows = conn.execute(sql, (min_ai,)).fetchall()
    out = [r["cid"] for r in rows if r["cid"]]
    if limit is not None:
        out = out[:limit]
    return out


def scan_playlists(client: Any, conn: Any, cfg: Any = config,
                   state: dict[str, Any] | None = None,
                   budget: _Budget | None = None) -> dict[str, Any]:
    """Плейлисты подтверждённых ИИ-каналов → каналы-владельцы видео."""
    if state is None:
        state = _new_state(conn, cfg)
    budget = budget or _Budget(conn, getattr(cfg, "EXPAND_BUDGET_UNITS_PER_RUN", BUDGET_UNITS_PER_RUN))

    max_playlists = getattr(cfg, "EXPAND_MAX_PLAYLISTS_PER_CHANNEL",
                            config.EXPAND_MAX_PLAYLISTS_PER_CHANNEL)
    max_channels = getattr(cfg, "EXPAND_MAX_PLAYLIST_CHANNELS_PER_RUN",
                           config.EXPAND_MAX_PLAYLIST_CHANNELS_PER_RUN)
    calls = 0
    new = 0
    playlists_scanned = 0
    channels_seen = 0
    for channel_id in _ai_channels(conn, cfg):
        if channels_seen >= int(max_channels):
            budget.note(f"лимит каналов на плейлисты ({max_channels})")
            break
        chan_key = f"playlists:{channel_id}"
        if _scan_seen(conn, chan_key):
            continue  # плейлисты этого канала уже смотрели
        channels_seen += 1
        if not budget.can_spend(config.COST_PLAYLIST_ITEMS):
            budget.note("бюджет исчерпан на плейлистах")
            break
        calls += 1
        try:
            playlists = client.playlists_by_channel(channel_id, max_results=max_playlists)
        except yt.YouTubeError as exc:
            log.warning("плейлисты канала %s не удались: %s", channel_id, exc)
            _mark_scan(conn, chan_key, "playlist", chan_key)
            continue
        _mark_scan(conn, chan_key, "playlist", chan_key)
        for pl in playlists[:max_playlists]:
            pl_id = pl.get("id")
            if not pl_id:
                continue
            key = f"playlist:{pl_id}"
            if _scan_seen(conn, key):
                continue
            if not budget.can_spend(config.COST_PLAYLIST_ITEMS):
                budget.note("бюджет исчерпан на плейлистах")
                return {"new": new, "playlists": playlists_scanned,
                        "calls": calls, "units": calls}
            calls += 1
            playlists_scanned += 1
            try:
                items = client.playlist_items(pl_id, max_results=50)
            except yt.YouTubeError as exc:
                log.warning("плейлист %s не удался: %s", pl_id, exc)
                _mark_scan(conn, key, "playlist", key)
                continue
            for item in items:
                snippet = item.get("snippet") or {}
                owner = (snippet.get("videoOwnerChannelId")
                         or snippet.get("channelId"))
                owner_title = snippet.get("videoOwnerChannelTitle")
                if _add_candidate(conn, state, owner, "playlist",
                                  evidence=key, title=owner_title, cfg=cfg):
                    new += 1
            _mark_scan(conn, key, "playlist", key)
    return {"new": new, "playlists": playlists_scanned, "calls": calls,
            "units": calls}


# --- шаг 5: поиск каналов напрямую (100 units / вызов) ---------------------

def _is_human_query(value: Any) -> bool:
    """Похоже ли значение на человеческую поисковую фразу (ТЗ-33).

    Тонкая обёртка над ``db.is_human_query``: тот же предикат используется
    миграцией ``db._migrate_drop_dead_queries``, чтобы пул фраз и отсев
    «мёртвых» принятых строк не разъезжались. Подробности — в докстроке db.
    """
    return db.is_human_query(value)


def query_phrase_pool(conn: Any, limit: int = 60) -> list[Any]:
    """Пул фраз для поиска каналов из таблицы, в порядке приоритета (D-02).

    Берутся только человеческие ``kind='query'`` со статусом accepted/new и
    runs < 2; отсеянные (status='dropped'), исчерпавшие два круга и служебные
    ключи обхода плейлистов не попадают. Отдельно отстраняются фразы, которые
    упали ``EXPAND_PHRASE_FAIL_LIMIT`` раз подряд, пока не истёк кулдаун
    ``EXPAND_PHRASE_FAIL_COOLDOWN_SEC`` (ТЗ-33): иначе постоянно падающая фраза
    приоритета 1 каждый прогон занимает слоты и голодит остальной пул. Порядок:

    1) проверенные с урожаем (accepted > 0) — по убыванию accepted, затем по
       возрастанию runs (сначала те, что ходили меньше);
    2) курируемые фразы конфига (source='channel_search_static', runs=0) —
       их ни разу не спрашивали, а они выбраны человеком; большой hits машинных
       term_mining не должен вытеснять их из пула;
    3) прочие проверенные без урожая (runs > 0) — второй круг решает их судьбу;
    4) машинные многословные (runs=0, 2+ слова) — по убыванию hits, затем по
       алфавиту;
    5) машинные односложные (runs=0, одно слово) — в самом конце:
       односложные вроде ``claude``, ``chatgpt`` почти бесполезны.
    """
    fail_limit = int(getattr(config, "EXPAND_PHRASE_FAIL_LIMIT",
                             config.EXPAND_PHRASE_FAIL_LIMIT))
    cooldown = int(getattr(config, "EXPAND_PHRASE_FAIL_COOLDOWN_SEC",
                           config.EXPAND_PHRASE_FAIL_COOLDOWN_SEC))
    fail_cutoff = int(config.now_ts()) - cooldown
    rows = conn.execute(
        "SELECT query, source, hits, runs, accepted, fail_count, last_fail_at "
        "FROM query_candidates "
        "WHERE kind='query' AND runs < 2 AND status IN ('accepted','new') "
        "AND (COALESCE(fail_count, 0) < ? OR last_fail_at IS NULL "
        "     OR last_fail_at < ?)",
        (fail_limit, fail_cutoff),
    ).fetchall()
    rows = [r for r in rows if _is_human_query(r["query"])]

    def key(row: Any) -> tuple:
        accepted = int(row["accepted"] or 0)
        runs = int(row["runs"] or 0)
        hits = int(row["hits"] or 0)
        query = str(row["query"])
        source = str(row["source"] or "")
        words = len(query.split())
        if accepted > 0:
            return (0, -accepted, runs, 0, query)
        if source == "channel_search_static" and runs == 0:
            return (1, 0, 0, -hits, query)
        if runs > 0:
            return (2, 0, 0, -hits, query)
        if words >= 2:
            return (3, 0, 0, -hits, query)
        return (4, 0, 0, -hits, query)

    ordered = sorted(rows, key=key)
    return ordered[: max(int(limit), 0)]


def _note_phrase_run(conn: Any, query: str) -> None:
    """Отметить, что фразу отправили в поиск: runs+1 и время последнего хода.

    Успешный поиск сбрасывает счётчик подряд идущих сбоев (ТЗ-33): фраза снова
    считается здоровой, поэтому ``fail_count=0`` и ``last_fail_at=NULL``.
    """
    conn.execute(
        "UPDATE query_candidates SET runs=runs+1, last_run_at=?, "
        "fail_count=0, last_fail_at=NULL "
        "WHERE query=? AND kind='query'",
        (str(config.now_ts()), query),
    )
    conn.commit()


def _note_phrase_fail(conn: Any, query: str) -> None:
    """Отметить сбой поиска фразы (ТЗ-33): fail_count+1 и время сбоя.

    ``runs`` не трогаем: фразу даже не спросили, это не круг поиска. Но фразу,
    падающую подряд ``EXPAND_PHRASE_FAIL_LIMIT`` раз, ``query_phrase_pool``
    временно убирает из пула, чтобы она не голодила остальные.
    """
    conn.execute(
        "UPDATE query_candidates SET fail_count=COALESCE(fail_count, 0)+1, "
        "last_fail_at=? WHERE query=? AND kind='query'",
        (int(config.now_ts()), query),
    )
    conn.commit()


def recount_phrase_yields(conn: Any) -> None:
    """Пересчитать accepted по фактам из channel_candidates. Идемпотентно."""
    conn.execute(
        "UPDATE query_candidates SET accepted = ("
        "  SELECT COUNT(*) FROM channel_candidates cc "
        "  WHERE cc.evidence = 'search:' || query_candidates.query "
        "    AND cc.status = 'accepted'"
        ") WHERE kind='query'"
    )
    conn.commit()


def drop_exhausted_phrases(conn: Any) -> int:
    """Отсеять новые фразы после двух нулевых кругов. Идемпотентно."""
    dropped = db.drop_exhausted_phrases(conn)
    if dropped:
        log.info(
            "поисковые фразы: отсеяно %d после 2 нулевых кругов "
            "(экономия %d units за круг)",
            dropped, dropped * config.COST_SEARCH,
        )
    return dropped


def search_new_channels(client: Any, conn: Any, cfg: Any = config,
                        state: dict[str, Any] | None = None,
                        budget: _Budget | None = None) -> dict[str, Any]:
    """search?type=channel по пулу фраз из query_candidates → каналы.

    Пул — не статический список конфига, а таблица (D-02): зарегистрированные
    фразы живут в query_candidates, несут счётчики runs/accepted и отсеиваются
    после нулевого урожая. Статический CHANNEL_SEARCH_QUERIES остаётся лишь
    источником первичной регистрации (см. db._migrate_query_runs).
    """
    if state is None:
        state = _new_state(conn, cfg)
    budget = budget or _Budget(conn, getattr(cfg, "EXPAND_BUDGET_UNITS_PER_RUN", BUDGET_UNITS_PER_RUN))

    calls = 0
    new = 0
    guard: str | None = None
    max_searches = getattr(cfg, "EXPAND_MAX_CHANNEL_SEARCHES_PER_RUN",
                           config.EXPAND_MAX_CHANNEL_SEARCHES_PER_RUN)
    pool_limit = getattr(cfg, "EXPAND_MAX_CHANNEL_SEARCH_POOL",
                         config.EXPAND_MAX_CHANNEL_SEARCH_POOL)
    pool = query_phrase_pool(conn, limit=pool_limit)
    for row in pool:
        if calls >= int(max_searches):
            budget.note(f"лимит поисков каналов ({max_searches})")
            break
        if not budget.can_spend(config.COST_SEARCH):
            budget.note(f"бюджет исчерпан на поиске каналов ({row['query']})")
            break
        query = str(row["query"])
        language = config.query_language(query)
        region = "RU" if language == "ru" else None
        calls += 1
        try:
            items = client.search_channels(
                query, max_results=50, language=language, region=region
            )
        except yt.SearchQuotaGuard as exc:
            # Отдельный лимит вызовов search.list исчерпан: вызов не отправлен,
            # units не потрачены. Прогон не падает — остальные механизмы
            # (разбор имён, упоминания, обход плейлистов) продолжают работу.
            calls -= 1  # вызова не было — не считаем его потраченным
            guard = str(exc)
            log.warning("поисковая квота: %s", exc)
            break
        except yt.YouTubeError as exc:
            log.warning("поиск каналов '%s' не удался: %s", query, exc)
            # Сетевой сбой — не круг поиска (ТЗ-33): фразу даже не спросили,
            # поэтому runs не растёт и она попадёт в следующий прогон. Иначе
            # фраза получала runs=1 и после второго сбоя отсеивалась как
            # «нулевой урожай», хотя запроса к YouTube не было.
            # Отдельный fail_count защищает от фраз, падающих всегда: после
            # EXPAND_PHRASE_FAIL_LIMIT сбоев подряд фраза временно уходит из
            # пула (кулдаун EXPAND_PHRASE_FAIL_COOLDOWN_SEC), иначе каждый
            # прогон она бы занимала слоты и голодила остальные фразы.
            _note_phrase_fail(conn, query)
            continue
        for item in items:
            ident = item.get("id") or {}
            cid = ident.get("channelId") if isinstance(ident, dict) else ident
            snippet = item.get("snippet") or {}
            if _add_candidate(conn, state, cid, "channel_search",
                              evidence=f"search:{query}",
                              title=snippet.get("title"),
                              description=snippet.get("description"),
                              handle=(snippet.get("customUrl") or "").lstrip("@") or None,
                              cfg=cfg):
                new += 1
        _note_phrase_run(conn, query)
    recount_phrase_yields(conn)
    dropped = drop_exhausted_phrases(conn)
    units = calls * config.COST_SEARCH
    return {"new": new, "queries": calls, "calls": calls, "units": units,
            "phrases_pool": len(pool), "phrases_used": calls,
            "phrases_dropped": dropped,
            "guard": guard, "guard_skipped": 1 if guard else 0}


# --- проверка кандидатов ----------------------------------------------------

def _probe_channel(client: Any, conn: Any, channel_id: str, cfg: Any,
                   budget: _Budget, search_state: dict[str, int]) -> dict[str, Any]:
    """Проверить одного кандидата. Возвращает сводку по нему."""
    # 1) Полные данные канала (1 unit).
    items = client.channels_by_ids([channel_id])
    if not items:
        return {"status": "rejected", "reason": "канал не найден", "ai": 0}
    item = items[0]
    collect.store_channel(conn, item)

    # 2) Последние видео: uploads-плейлист (1 unit) или search?channelId (100).
    uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    video_ids: list[str] = []
    if uploads:
        pl_items = client.playlist_items(
            uploads, max_results=getattr(cfg, "EXPAND_PROBE_VIDEOS",
                                         config.EXPAND_PROBE_VIDEOS)
        )
        for pl in pl_items:
            vid = (pl.get("contentDetails") or {}).get("videoId")
            if vid and vid not in video_ids:
                video_ids.append(vid)
    else:
        max_fb = getattr(cfg, "EXPAND_MAX_SEARCH_FALLBACKS",
                         config.EXPAND_MAX_SEARCH_FALLBACKS)
        if search_state.get("searches", 0) >= max_fb:
            # Лимит, а не содержательная причина: канал не теряем до следующего прогона.
            return {"status": "skipped",
                    "reason": "нет uploads-плейлиста, лимит дорогих проверок исчерпан",
                    "ai": 0}
        if not budget.can_spend(config.COST_SEARCH):
            budget.note("бюджет исчерпан на дорогой проверке канала")
            return {"status": "skipped", "reason": "бюджет", "ai": 0}
        search_state["searches"] = search_state.get("searches", 0) + 1
        try:
            results = client.search_videos(
                "", order="date", max_pages=1,
                extra={"channelId": channel_id, "type": "video"},
            )
        except yt.SearchQuotaGuard as exc:
            # Лимит вызовов search.list исчерпан: вызов не отправлен. Кандидата
            # не теряем (вернётся в следующий прогон), прогон не падает.
            search_state["searches"] -= 1
            log.warning("поисковая квота (проверка канала): %s", exc)
            return {"status": "skipped", "reason": str(exc), "ai": 0,
                    "search_guard": True}
        for res in results:
            vid = (res.get("id") or {}).get("videoId")
            if vid:
                video_ids.append(vid)
        video_ids = video_ids[: getattr(cfg, "EXPAND_PROBE_VIDEOS",
                                        config.EXPAND_PROBE_VIDEOS)]

    if not video_ids:
        return {"status": "rejected", "reason": "нет свежих видео", "ai": 0}

    # 3) Метаданные видео (1 unit за 50).
    if not budget.can_spend(config.COST_VIDEOS):
        budget.note("бюджет исчерпан на проверке видео")
        return {"status": "skipped", "reason": "бюджет", "ai": 0}
    meta = client.videos_by_ids(video_ids)
    usable = [m for m in meta if collect._is_usable(m)]
    stored = collect.store_videos(conn, usable, f"probe:{channel_id}", cfg)

    probe_ids = list(stored["video_ids"])
    if not probe_ids:
        # Видео уже были в базе — берём уже сохранённые id для разбора.
        probe_ids = [m.get("id") for m in usable if m.get("id")]

    # 4) Смысловой разбор существующим классификатором (без квоты YouTube).
    if probe_ids:
        try:
            classify_mod.classify_videos(conn, cfg, video_ids=probe_ids)
        except Exception as exc:  # разбор упал — кандидат остаётся new
            log.warning("разбор кандидата %s не удался: %s", channel_id, exc)
            return {"status": "error", "reason": f"разбор: {exc}", "ai": 0}

    min_ai = getattr(cfg, "MIN_AI_VIDEOS_PER_CANDIDATE",
                     config.MIN_AI_VIDEOS_PER_CHANNEL)
    probed = len(probe_ids)
    # Доля считается по РАЗОБРАННЫМ видео: классификатор пропускает короткие и
    # непригодные ролики, у них нет строки в video_classification. Если брать в
    # знаменатель все проверенные, пропущенные видео молча считаются «не ИИ» и
    # доля занижается.
    ai = 0
    checked = 0
    if probe_ids:
        marks = ",".join("?" for _ in probe_ids)
        row = conn.execute(
            f"SELECT COUNT(*) AS n, "
            f"COALESCE(SUM(is_ai = 1), 0) AS ai "
            f"FROM video_classification WHERE video_id IN ({marks})",
            probe_ids,
        ).fetchone()
        if row:
            checked = int(row["n"])
            ai = int(row["ai"])
    if checked >= int(getattr(cfg, "MIN_AI_SHARE_MIN_VIDEOS",
                              config.MIN_AI_SHARE_MIN_VIDEOS)):
        share_pct = getattr(cfg, "MIN_AI_SHARE_PERCENT",
                            config.MIN_AI_SHARE_PERCENT)
        share = ai / checked * 100
        if ai >= int(min_ai) and share >= float(share_pct):
            return {"status": "accepted", "reason": None, "ai": ai}
        if ai < int(min_ai):
            return {"status": "rejected",
                    "reason": f"ИИ-видео {ai} < {int(min_ai)}", "ai": ai}
        reason = (f"ИИ-видео {ai} из {checked} разобранных ({share:.0f}%) "
                  f"< {share_pct}%")
        if checked != probed:
            reason += f" (проверено {probed})"
        return {"status": "rejected", "reason": reason, "ai": ai}
    if ai >= int(min_ai):
        return {"status": "accepted", "reason": None, "ai": ai}
    return {"status": "rejected", "reason": f"ИИ-видео {ai} < {int(min_ai)}", "ai": ai}


def probe_candidates(client: Any, conn: Any, cfg: Any = config,
                     limit: int | None = None,
                     state: dict[str, Any] | None = None,
                     budget: _Budget | None = None,
                     max_new_channels: int | None = None) -> dict[str, Any]:
    """Проверить верхушку очереди кандидатов смыслом.

    Правило приёма: >=2 подтверждённых ИИ-видео → accepted, иначе rejected.
    Отклонённый канал повторно не проверяется.
    """
    if state is None:
        state = _new_state(conn, cfg)
    budget = budget or _Budget(conn, getattr(cfg, "EXPAND_BUDGET_UNITS_PER_RUN", BUDGET_UNITS_PER_RUN))
    if limit is None:
        limit = getattr(cfg, "EXPAND_MAX_PROBES_PER_RUN",
                        config.EXPAND_MAX_PROBES_PER_RUN)
    if max_new_channels is None:
        max_new_channels = getattr(cfg, "EXPAND_MAX_NEW_CHANNELS_PER_RUN",
                                   config.EXPAND_MAX_NEW_CHANNELS_PER_RUN)

    rows = [r for r in state["candidates"].values()
            if r["status"] == "new" and not str(r["channel_id"]).startswith("@")]
    rows.sort(key=candidate_sort_key)
    rows = rows[:int(limit)]

    probed = 0
    accepted = 0
    rejected = 0
    skipped = 0
    guard_skips = 0
    guard_reason: str | None = None
    stopped_reason = None
    search_state: dict[str, int] = {"searches": 0}

    for row in rows:
        if accepted >= int(max_new_channels):
            stopped_reason = f"достигнут лимит новых каналов за прогон ({max_new_channels})"
            break
        # Дедупликация до траты: канал уже в пуле.
        if row["channel_id"] in state["known"]:
            db.set_candidate_status(conn, row["channel_id"], "accepted",
                                    None, str(config.now_ts()))
            continue
        if not budget.can_spend(config.COST_VIDEOS * 2 + config.COST_CHANNELS):
            budget.note("бюджет исчерпан на проверке кандидатов")
            stopped_reason = "бюджет исчерпан"
            break
        probed += 1
        try:
            res = _probe_channel(client, conn, row["channel_id"], cfg, budget,
                                 search_state)
        except yt.YouTubeError as exc:
            log.warning("проверка кандидата %s не удалась: %s", row["channel_id"], exc)
            res = {"status": "error", "reason": str(exc), "ai": 0}

        now = str(config.now_ts())
        if res["status"] == "accepted":
            db.set_candidate_status(conn, row["channel_id"], "accepted", None, now)
            state["known"].add(row["channel_id"])
            accepted += 1
            state["accepted"] += 1
            # Принятый канал идёт в обход со своими видео.
            if budget.can_spend(config.COST_VIDEOS + config.COST_PLAYLIST_ITEMS):
                try:
                    collect.collect_uploads(conn, row["channel_id"], cfg, client=client)
                except Exception as exc:  # обход не должен ронять прогон
                    log.warning("обход принятого канала %s не удался: %s",
                                row["channel_id"], exc)
        elif res["status"] == "rejected":
            db.set_candidate_status(conn, row["channel_id"], "rejected",
                                    res.get("reason"), now)
            rejected += 1
            state["rejected"] += 1
        else:
            # error/skipped: оставляем new, попробуем в следующий прогон.
            skipped += 1
            if res.get("search_guard"):
                guard_skips += 1
                if guard_reason is None:
                    guard_reason = res.get("reason")

    return {
        "probed": probed,
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
        "search_fallbacks": search_state.get("searches", 0),
        "stopped_reason": stopped_reason,
        "search_guard_skipped": guard_skips,
        "guard": guard_reason,
    }


# --- прогон целиком ---------------------------------------------------------

def run_expand(conn: Any, cfg: Any = config, client: Any | None = None,
               budget: int | None = None,
               max_probes: int | None = None,
               max_new_channels: int | None = None,
               dry_run: bool = False) -> dict[str, Any]:
    """Один прогон расширения: от бесплатных механизмов к дорогим.

    dry_run=True — работают только бесплатные шаги (0 units), сеть не зовётся.
    """
    if budget is None:
        budget = getattr(cfg, "EXPAND_BUDGET_UNITS_PER_RUN", BUDGET_UNITS_PER_RUN)
    if max_probes is None:
        max_probes = getattr(cfg, "EXPAND_MAX_PROBES_PER_RUN",
                             config.EXPAND_MAX_PROBES_PER_RUN)
    if max_new_channels is None:
        max_new_channels = getattr(cfg, "EXPAND_MAX_NEW_CHANNELS_PER_RUN",
                                   config.EXPAND_MAX_NEW_CHANNELS_PER_RUN)

    state = _new_state(conn, cfg)
    summary: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "budget": int(budget),
        "sources": dict(state["sources"]),
        "probed": 0,
        "accepted": 0,
        "rejected": 0,
        "units": 0,
        "stopped_reason": None,
        "steps": [],
    }

    # --- бесплатные механизмы (0 units) ---
    mentions = mine_mentions(conn, cfg, state)
    queries = mine_queries(conn, cfg)
    accepted_queries = accept_query_candidates(conn, cfg)
    summary["steps"].append({"step": "mine_mentions", **mentions})
    summary["steps"].append({"step": "mine_queries", **queries})
    summary["steps"].append({"step": "accept_query_candidates", **accepted_queries})
    summary["queries_accepted"] = accepted_queries["accepted"]
    summary["queries_rejected"] = accepted_queries["rejected"]
    summary["queries_queued"] = accepted_queries["queued"]

    if dry_run:
        summary["sources"] = dict(state["sources"])
        summary["units"] = 0
        return summary

    if client is None:
        client = yt.YouTubeClient(conn=conn)
    b = _Budget(conn, int(budget))

    # --- дешёвые платные механизмы ---
    resolved = resolve_mentions(client, conn, cfg, state, b)
    charts = scan_charts(client, conn, cfg, state, b)
    playlists = scan_playlists(client, conn, cfg, state, b)
    searches = search_new_channels(client, conn, cfg, state, b)

    # --- проверка кандидатов ---
    probes = probe_candidates(client, conn, cfg, limit=max_probes,
                              state=state, budget=b,
                              max_new_channels=max_new_channels)

    summary["steps"].extend([
        {"step": "resolve_mentions", **resolved},
        {"step": "scan_charts", **charts},
        {"step": "scan_playlists", **playlists},
        {"step": "search_new_channels", **searches},
        {"step": "probe_candidates", **probes},
    ])
    summary["sources"] = dict(state["sources"])
    summary["probed"] = probes["probed"]
    summary["accepted"] = probes["accepted"]
    summary["rejected"] = probes["rejected"]
    summary["chart_filtered"] = charts.get("filtered", 0)
    summary["phrases_pool"] = searches.get("phrases_pool", 0)
    summary["phrases_used"] = searches.get("phrases_used", 0)
    summary["phrases_dropped"] = searches.get("phrases_dropped", 0)
    summary["unresolved_remaining"] = _count_status(conn, "unresolved")
    summary["units"] = b.spent()
    summary["stopped_reason"] = probes.get("stopped_reason") or b.stopped_reason
    # Причины пропуска поисков (отдельный лимит вызовов search.list): показываем
    # в итоге, если поиск был остановлен предохранителем.
    summary["search_guard_skipped"] = (
        int(searches.get("guard_skipped", 0))
        + int(probes.get("search_guard_skipped", 0))
    )
    summary["search_guard_reason"] = searches.get("guard") or probes.get("guard")
    return summary


def _count_status(conn: Any, status: str) -> int:
    """Сколько кандидатов сейчас в заданном статусе."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM channel_candidates WHERE status=?", (status,)
    ).fetchone()
    return int(row["n"]) if row else 0


def format_summary(summary: dict[str, Any]) -> str:
    """Итог прогона одной строкой."""
    sources = summary.get("sources", {})
    parts = ", ".join(f"{k}: {v}" for k, v in sources.items()) or "нет"
    text = (
        f"Расширение: кандидаты ({parts}); "
        f"проверено {summary.get('probed', 0)}, "
        f"принято {summary.get('accepted', 0)}, "
        f"отклонено {summary.get('rejected', 0)}, "
        f"неразрешённых handle {summary.get('unresolved_remaining', 0)}, "
        f"чарт-шума отсеяно {summary.get('chart_filtered', 0)}, "
        f"запросов в работу {summary.get('queries_accepted', 0)} "
        f"(отсеяно {summary.get('queries_rejected', 0)}), "
        f"фраз в пуле {summary.get('phrases_pool', 0)} "
        f"(использовано {summary.get('phrases_used', 0)}, "
        f"отсеяно по урожаю {summary.get('phrases_dropped', 0)}), "
        f"квоты потрачено {summary.get('units', 0)} units."
    )
    if summary.get("stopped_reason"):
        text += f" Остановка: {summary['stopped_reason']}."
    if summary.get("search_guard_reason"):
        text += (
            f" Поиск пропущен ({summary.get('search_guard_skipped', 0)}): "
            f"{summary['search_guard_reason']}."
        )
    if summary.get("dry_run"):
        text = "[dry-run] " + text
    return text
