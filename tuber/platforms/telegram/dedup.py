"""Кластеризация почти-дублей Telegram по simhash (D-38, D-39).

Точный ``text_hash`` ловит только побайтовые повторы. Почти-дубли (перестановки,
синонимы, вставки/удаления слов) образуют кластер по близости simhash: два
текста считаются одним «событием», если расстояние Хэмминга их 64-битных
simhash не больше :data:`SIMHASH_CLUSTER_THRESHOLD`.

Simhash берётся из :mod:`tuber.core.textsim` (единый источник, не вторая
реализация).

Кандидаты на склейку ищутся LSH-блокировкой: 64 бита режутся на
``threshold+1 = 7`` блоков; если два хэша различаются не более чем в 6 битах,
хотя бы один блок совпадает точно (принцип Дирихле). Совпавший блок — кандидат,
окончательное решение принимает проверка Хэмминга. Это даёт линейную по корпусу
сложность вместо квадратичной.

Порог закреплён по калибровке D-38 (см. :data:`SIMHASH_CLUSTER_THRESHOLD`).
"""
from __future__ import annotations

from collections import defaultdict

from tuber.core.textsim import hamming, simhash64

#: Порог расстояния Хэмминга simhash для склейки почти-дублей.
#:
#: Калибровка 23.09.2026 на нашем корпусе Telegram (6000 текстов ≥40 символов,
#: 156 571 размеченная пара; правило разметки «похожие» — Jaccard шинглов k=3
#: ≥ 0.6, ``tuber.core.textsim.JACCARD_SIMILAR``). Распределение расстояний:
#: у похожих пар p50=3, p90=11, p95=14; у непохожих p5=24, p10=26. При пороге 6:
#: склеек 230, из них истинных 227, ложных 3 → доля ложных 1.3 % (≤ 5 %).
#: Порог 8 даёт 5.5 % ложных — за границей. Пересчитывать при смене корпуса.
SIMHASH_CLUSTER_THRESHOLD = 6
#: Число блоков LSH = порог + 1 (гарантия Дирихле).
_BLOCK_COUNT = SIMHASH_CLUSTER_THRESHOLD + 1


def _block_values(sig: int, nblocks: int = _BLOCK_COUNT):
    """Значения блоков 64-битного хэша (для LSH-кандидатов)."""
    out = []
    bit = 0
    base, rem = divmod(64, nblocks)
    for i in range(nblocks):
        width = base + (1 if i < rem else 0)
        out.append((i, (sig >> bit) & ((1 << width) - 1)))
        bit += width
    return out


def cluster_by_text(items, *, threshold: int = SIMHASH_CLUSTER_THRESHOLD):
    """Склеить почти-дубли.

    ``items`` — итерируемое из ``(key, text)`` (key hashable). Возвращает
    ``(cluster_of, clusters)``: ``cluster_of[key] = cluster_id`` (int),
    ``clusters[cluster_id] = [key, ...]``. Записи с пустым текстом (нет шинглов)
    в кластеризацию не попадают и получают уникальный кластер (singleton).

    Кластеры транзитивны (объединение по цепочке близких): так и должно быть,
    иначе «событие» рассыпается на несвязные куски.
    """
    items = list(items)
    parent: dict = {k: k for k, _ in items}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    sigs = {}
    buckets: dict = defaultdict(list)
    for key, text in items:
        if not (text and text.strip()):
            # Пустой текст не кластеризуем: у всех пустых simhash=0, и они
            # слиплись бы в один ложный «событие»-кластер.
            sigs[key] = 0
            continue
        sig = simhash64(text)
        sigs[key] = sig
        for bv in _block_values(sig):
            buckets[bv].append(key)

    for keys in buckets.values():
        if len(keys) < 2:
            continue
        for i in range(len(keys)):
            a = keys[i]
            for j in range(i + 1, len(keys)):
                b = keys[j]
                if find(a) == find(b):
                    continue
                if hamming(sigs[a], sigs[b]) <= threshold:
                    union(a, b)

    clusters: dict = defaultdict(list)
    cluster_of: dict = {}
    for key, _ in items:
        root = find(key)
        cluster_of[key] = root
        clusters[root].append(key)
    return cluster_of, dict(clusters)


def dup_posts_by_channel(rows, *, dup_hours: int):
    """Сколько постов канала имеют почти-дубль в ДРУГОМ канале ±``dup_hours``.

    ``rows`` — итерируемое из ``(key, text, channel_id, dt)``, где ``dt`` —
    ``datetime``. Замена точного ``EXISTS(text_hash=…)`` в
    :func:`tuber.platforms.telegram.scoring.compute_baselines` (D-39): кластер
    simhash ловит и почти-дубли, а не только побайтовые.

    Возвращает ``{channel_id: число постов-дублей}`` и ``{channel_id: всего
    постов}``.
    """
    from datetime import timedelta

    data = list(rows)
    cluster_of, _ = cluster_by_text((k, t) for k, t, _, _ in data)
    delta = timedelta(hours=int(dup_hours))
    groups: dict = defaultdict(list)
    for k, _t, cid, dt in data:
        groups[cluster_of[k]].append((dt, cid))
    dup = defaultdict(int)
    total = defaultdict(int)
    for members in groups.values():
        for _dt, cid in members:
            total[cid] += 1
        if len(members) < 2:
            continue
        members.sort()
        times = [m[0] for m in members]
        counts: dict = defaultdict(int)
        lo = 0
        hi = 0
        n = len(members)
        for i in range(n):
            # окно [ts[i]-delta, ts[i]+delta]
            while hi < n and times[hi] <= times[i] + delta:
                counts[members[hi][1]] += 1
                hi += 1
            while lo < n and times[lo] < times[i] - delta:
                counts[members[lo][1]] -= 1
                lo += 1
            cid_i = members[i][1]
            in_window = sum(counts.values())
            if in_window - counts[cid_i] > 0:
                dup[cid_i] += 1
    return dict(dup), dict(total)

