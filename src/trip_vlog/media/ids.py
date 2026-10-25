"""Stable asset IDs and content fingerprints (DESIGN §6.1)."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from trip_vlog.paths import long_path

FAST_CHUNK = 4 * 1024 * 1024


def asset_id_for(rel_path: str) -> str:
    """`asset_` + first 16 hex chars of SHA-256 of the normalized relative path (POSIX, NFC)."""
    return "asset_" + hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:16]


def fast_fingerprint(path: Path, size: int, mtime_ns: int) -> str:
    """SHA-256 over (size, mtime_ns, first 4 MiB, last 4 MiB). For small files the two reads overlap."""
    h = hashlib.sha256()
    h.update(struct.pack("<Qq", size, mtime_ns))
    n = min(size, FAST_CHUNK)
    with open(long_path(path), "rb") as f:
        h.update(f.read(n))
        if size > 0:
            f.seek(max(0, size - n))
            h.update(f.read(n))
    return h.hexdigest()


def full_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def compute_content_version(path: Path, mode: str, size: int, mtime_ns: int) -> tuple[str, str | None]:
    """Return (content_version, sha256_or_None). mode: 'fast' | 'sha256'."""
    if mode == "sha256":
        digest = full_sha256(path)
        return f"sha256:{digest}", digest
    if mode == "fast":
        return f"fast:{fast_fingerprint(path, size, mtime_ns)}", None
    raise ValueError(f"unknown fingerprint mode: {mode!r}")


def fingerprint_algorithm(content_version: str | None) -> str | None:
    return content_version.split(":", 1)[0] if content_version else None
