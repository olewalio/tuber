"""Общие функции близости текстов (шинглы, simhash, Хэмминг, Jaccard).

Единственный источник simhash в проекте (D-38): и кластеризация сюжетов X
(:mod:`tuber.platforms.x.stories`), и кластер почти-дублей Telegram
(:mod:`tuber.platforms.telegram.dedup`) берут его ОТСЮДА, а не заводят вторую
реализацию. Замер D-08 показал, что для решений о склейке сюжетов simhash не
используется (расстояния Хэмминга 23-42 при любом разумном пороге), поэтому
``x.stories`` импортирует отсюда только ``simhash64``/``hamming``/``shingles``
как диагностику; решение по сюжетам — взвешенное пересечение токенов.

Только stdlib, детерминированно, без сети.
"""
from __future__ import annotations

import hashlib
import re

_WORD_RE = re.compile(r"[a-zа-я0-9]+", re.I)

#: Длина шингла по умолчанию (слов).
SHINGLE_K = 3


def shingles(text, k: int = SHINGLE_K) -> set[str]:
    """Множество шинглов текста (k подряд идущих слов, нижний регистр).

    Для текстов короче ``k`` слов — множество самих слов (или сам текст, если и
    слов нет): так короткий повтор всё равно даёт сигнал близости.
    """
    words = _WORD_RE.findall((text or "").lower())
    if len(words) < k:
        return set(words) or ({text.strip().lower()} if text and text.strip() else set())
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def simhash64(text) -> int:
    """64-битный simhash текста (детерминированный, только stdlib)."""
    v = [0] * 64
    for sh in shingles(text):
        h = int(hashlib.md5(sh.encode("utf-8", "replace")).hexdigest()[:16], 16)
        for b in range(64):
            v[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(64):
        if v[b] > 0:
            out |= (1 << b)
    return out


def hamming(a: int, b: int) -> int:
    """Расстояние Хэмминга между двумя 64-битными хэшами."""
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def jaccard(a: set, b: set) -> float:
    """Jaccard двух множеств: |A∩B| / |A∪B|; оба пусты → 0.0."""
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def text_similarity(a: str, b: str) -> float:
    """Jaccard шинглов двух текстов (правило разметки «похожие» в D-38)."""
    return jaccard(shingles(a), shingles(b))


#: Порог разметки «похожие» по Jaccard шинглов (D-38).
JACCARD_SIMILAR = 0.6
