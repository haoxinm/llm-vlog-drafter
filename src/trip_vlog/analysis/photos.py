"""Near-duplicate photo detection with a 64-bit difference hash (dHash), Pillow only (DESIGN §7.4).

Photos whose hashes differ in at most `distance` bits are grouped (single linkage); a group of two or more gets a stable
`duplicate_group` ID so the planner can avoid repeating almost the same picture. Photos are never discarded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping

PHASH_VERSION = 1


def dhash(path: Path) -> int:
    """64-bit dHash of the orientation-corrected picture (9x8 grayscale, left pixel brighter than right). Raises OSError/ValueError."""
    from PIL import Image, ImageOps

    from trip_vlog.paths import long_path

    with Image.open(long_path(path)) as im:
        im.draft("L", (64, 64))
        im = ImageOps.exif_transpose(im)
        im = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        px = list(im.tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return bits


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

    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            if hamming(hashes[a], hashes[b]) <= distance:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    members: dict[str, list[str]] = {}
    for i in ids:
        members.setdefault(find(i), []).append(i)
    return {m: "dup_" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:10]
            for root, ms in members.items() if len(ms) > 1 for m in ms}
