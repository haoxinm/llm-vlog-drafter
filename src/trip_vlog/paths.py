"""Cross-platform path helpers (pathlib only, no shell)."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePath

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def normalize_rel_path(rel: PurePath) -> str:
    """Relative path -> POSIX separators + Unicode NFC. Works for PureWindowsPath/PurePosixPath.

    Case is never changed. Absolute paths and '..' are rejected.
    """
    if rel.anchor or rel.drive:
        raise ValueError(f"path must be relative: {rel}")
    parts = [p for p in rel.parts if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"invalid relative path: {rel}")
    return nfc("/".join(parts))


def long_path(path: Path) -> Path:
    """On Windows, return an extended-length (\\\\?\\) path for long absolute paths; otherwise unchanged."""
    if os.name != "nt":
        return path
    s = os.path.abspath(str(path))
    if len(s) < 240 or s.startswith("\\\\?\\"):
        return Path(s)
    if s.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def is_within(child: Path, parent: Path) -> bool:
    """True if resolved `child` is `parent` or lies inside it."""
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def safe_filename(name: str, max_len: int = 40) -> str:
    """Make a string safe as a file-name component on Windows, macOS and Linux."""
    s = _INVALID_CHARS.sub("_", nfc(name)).strip(" .")
    s = s[:max_len].rstrip(" .") or "_"
    if s.split(".")[0].upper() in WINDOWS_RESERVED:
        s = "_" + s
    return s


def check_portable_component(part: str) -> str | None:
    """Return a problem description for a path component that is invalid on Windows, else None."""
    if _INVALID_CHARS.search(part):
        return f"contains characters invalid on Windows: {part!r}"
    if part != part.rstrip(" ."):
        return f"ends with a dot or space: {part!r}"
    if part.split(".")[0].upper() in WINDOWS_RESERVED:
        return f"reserved device name: {part!r}"
    return None
