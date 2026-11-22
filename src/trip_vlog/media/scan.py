"""Safe, read-only media inventory (DESIGN §7.1)."""

from __future__ import annotations

import os
import stat as stat_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from trip_vlog.media.ids import asset_id_for, compute_content_version
from trip_vlog.paths import long_path, nfc

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp", ".gif"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".webm", ".3gp", ".mpg", ".mpeg", ".wmv", ".mxf"})
AUDIO_EXTS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff"})

HIDDEN_NAMES = frozenset({
    "thumbs.db", "desktop.ini", ".ds_store", "__macosx", "$recycle.bin", "system volume information",
    "icon\r", ".spotlight-v100", ".trashes", ".fseventsd", ".temporaryitems",
})
_WIN_HIDDEN = getattr(stat_mod, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_WIN_SYSTEM = getattr(stat_mod, "FILE_ATTRIBUTE_SYSTEM", 0x4)


def media_type_for_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


@dataclass
class ScannedFile:
    rel_path: str  # POSIX separators, Unicode NFC
    abs_path: Path
    asset_id: str
    media_type_hint: str
    extension: str
    size_bytes: int
    mtime_ns: int
    content_version: str | None = None
    sha256: str | None = None
    error: str | None = None  # e.g. unreadable while fingerprinting


@dataclass
class SkippedEntry:
    rel_path: str
    reason: str  # hidden | system_file | symlink | symlink_loop | unsupported_extension | not_regular_file |
                 # unreadable | unicode_collision | not_recursive
    detail: str | None = None


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    skipped: list[SkippedEntry] = field(default_factory=list)
    case_collisions: list[list[str]] = field(default_factory=list)

    def skipped_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.skipped:
            out[s.reason] = out.get(s.reason, 0) + 1
        return dict(sorted(out.items()))

    def to_report(self) -> dict[str, Any]:
        return {
            "files": len(self.files),
            "skipped_counts": self.skipped_counts(),
            "skipped": [{"path": s.rel_path, "reason": s.reason, "detail": s.detail} for s in self.skipped],
            "case_collisions": self.case_collisions,
        }


def _hidden_reason(entry: os.DirEntry[str]) -> str | None:
    name = entry.name
    low = name.lower()
    if low in HIDDEN_NAMES:
        return "system_file"
    if name.startswith("."):  # dotfiles, AppleDouble ._*
        return "hidden"
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return None
    if attrs & (_WIN_HIDDEN | _WIN_SYSTEM):
        return "hidden"
    return None


def find_case_collisions(rel_paths: Iterable[str]) -> list[list[str]]:
    """Groups of paths that differ only by case (they would clash on Windows/macOS default volumes)."""
    groups: dict[str, list[str]] = {}
    for p in rel_paths:
        groups.setdefault(nfc(p).casefold(), []).append(p)
    return sorted(sorted(g) for g in groups.values() if len(g) > 1)


def scan_media(root: Path, *, recursive: bool = True, follow_symlinks: bool = False,
               fingerprint: str = "fast") -> ScanResult:
    """Enumerate supported media under `root` (never writes). Per-file problems are reported, not raised."""
    result = ScanResult()
    seen_norm: dict[str, str] = {}
    visited_dirs: set[str] = set()

    def walk(dir_path: Path, rel_parts: tuple[str, ...]) -> None:
        if follow_symlinks:
            real = os.path.normcase(os.path.realpath(dir_path))
            if real in visited_dirs:
                result.skipped.append(SkippedEntry(nfc("/".join(rel_parts)), "symlink_loop"))
                return
            visited_dirs.add(real)
        try:
            with os.scandir(long_path(dir_path)) as it:
                entries = sorted(it, key=lambda e: nfc(e.name))
        except OSError as e:
            result.skipped.append(SkippedEntry(nfc("/".join(rel_parts)) or ".", "unreadable", e.strerror or str(e)))
            return
        for entry in entries:
            rel = rel_parts + (entry.name,)
            rel_str = nfc("/".join(rel))
            reason = _hidden_reason(entry)
            if reason:
                result.skipped.append(SkippedEntry(rel_str, reason))
                continue
            try:
                if entry.is_symlink() and not follow_symlinks:
                    result.skipped.append(SkippedEntry(rel_str, "symlink"))
                    continue
                if entry.is_dir(follow_symlinks=follow_symlinks):
                    if recursive:
                        walk(dir_path / entry.name, rel)
                    else:
                        result.skipped.append(SkippedEntry(rel_str, "not_recursive"))
                    continue
                if not entry.is_file(follow_symlinks=follow_symlinks):
                    result.skipped.append(SkippedEntry(rel_str, "not_regular_file"))
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                mtype = media_type_for_ext(ext)
                if mtype is None:
                    result.skipped.append(SkippedEntry(rel_str, "unsupported_extension", ext or "(none)"))
                    continue
                st = entry.stat(follow_symlinks=follow_symlinks)
            except OSError as e:
                result.skipped.append(SkippedEntry(rel_str, "unreadable", e.strerror or str(e)))
                continue
            if rel_str in seen_norm:
                result.skipped.append(SkippedEntry(
                    rel_str, "unicode_collision", f"same normalized path as {seen_norm[rel_str]!r}"))
                continue
            seen_norm[rel_str] = "/".join(rel)
            sf = ScannedFile(rel_str, dir_path / entry.name, asset_id_for(rel_str), mtype, ext, st.st_size, st.st_mtime_ns)
            try:
                sf.content_version, sf.sha256 = compute_content_version(sf.abs_path, fingerprint, st.st_size, st.st_mtime_ns)
            except OSError as e:
                sf.error = f"cannot read file: {e.strerror or e}"
            result.files.append(sf)

    walk(Path(root), ())
    result.files.sort(key=lambda f: f.rel_path)
    result.case_collisions = find_case_collisions(f.rel_path for f in result.files)
    return result
