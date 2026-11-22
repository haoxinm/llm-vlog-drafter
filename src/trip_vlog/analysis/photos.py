"""Near-duplicate photo detection with a 64-bit difference hash (dHash), Pillow only (DESIGN §7.4).

Photos whose hashes differ in at most `distance` bits are grouped (single linkage); a group of two or more gets a stable
`duplicate_group` ID so the planner can avoid repeating almost the same picture. Photos are never discarded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

PHASH_VERSION = 2   # 2: near-flat pictures are not hashed
FLAT_MAX_STEP = 4   # brightest step between neighbouring samples (0-255) below which a picture counts as flat


def dhash(path: Path) -> int:
    """64-bit dHash of the orientation-corrected picture (9x8 grayscale, left pixel brighter than right). Raises OSError/ValueError."""
    return _dhash(path)[0]


def dhash_or_flat(path: Path) -> int | None:
    """The dHash, or None for a near-flat picture (blank wall, lens cap, night sky): it has no gradient to compare, every such
    picture would hash to 0 and be called a duplicate of every other one."""
    h, flat = _dhash(path)
    return None if flat else h


def _dhash(path: Path) -> tuple[int, bool]:
    from PIL import Image, ImageOps

    from trip_vlog.paths import long_path

    with Image.open(long_path(path)) as im:
        im.draft("L", (64, 64))
        im = ImageOps.exif_transpose(im)
        im = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        px = list(im.tobytes())
    bits = 0
    step = 0
    for row in range(8):
        for col in range(8):
            a, b = px[row * 9 + col], px[row * 9 + col + 1]
            step = max(step, abs(a - b))
            bits = (bits << 1) | (1 if a > b else 0)
    return bits, step < FLAT_MAX_STEP


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def group_duplicates(hashes: Mapping[str, int], distance: int) -> dict[str, str]:
    """asset_id -> duplicate_group for every photo that has a near-duplicate. The group ID derives from its smallest asset ID."""
    ids = sorted(hashes)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    if distance < 0:
        pairs: Any = ()
    elif distance >= 63:
        pairs = ((a, b) for n, a in enumerate(ids) for b in ids[n + 1:])
    else:
        # pigeonhole: two 64-bit hashes within `distance` bits agree exactly on at least one of `distance + 1` segments
        k = distance + 1
        bounds = [(i * 64 // k, (i + 1) * 64 // k) for i in range(k)]
        buckets: dict[tuple[int, int], list[str]] = {}
        for i in ids:
            h = hashes[i]
            for si, (lo, hi) in enumerate(bounds):
                buckets.setdefault((si, (h >> lo) & ((1 << (hi - lo)) - 1)), []).append(i)
        pairs = ((a, b) for ms in buckets.values() if len(ms) > 1 for n, a in enumerate(ms) for b in ms[n + 1:])
    for a, b in pairs:
        if hamming(hashes[a], hashes[b]) <= distance:
            union(a, b)
    members: dict[str, list[str]] = {}
    for i in ids:
        members.setdefault(find(i), []).append(i)
    return {m: "dup_" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:10]
            for root, ms in members.items() if len(ms) > 1 for m in ms}
