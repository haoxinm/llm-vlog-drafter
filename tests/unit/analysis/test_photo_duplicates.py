from __future__ import annotations

import random

from trip_vlog.analysis.photos import group_duplicates, hamming


def _naive(hashes, d):
    ids = sorted(hashes)
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            if hamming(hashes[a], hashes[b]) <= d:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    groups = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return sorted(sorted(m) for m in groups.values() if len(m) > 1)


def _partition(res):
    g = {}
    for k, v in res.items():
        g.setdefault(v, []).append(k)
    return sorted(sorted(m) for m in g.values())


def test_bucketed_grouping_matches_pairwise_scan():
    rnd = random.Random(7)
    base = [rnd.getrandbits(64) for _ in range(30)]
    hashes = {}
    for i in range(150):
        h = rnd.choice(base)
        for _ in range(rnd.randint(0, 6)):
            h ^= 1 << rnd.randrange(64)
        hashes[f"a{i:03d}"] = h
    for d in (0, 1, 4, 10, 63, 64):
        assert _partition(group_duplicates(hashes, d)) == _naive(hashes, d)
