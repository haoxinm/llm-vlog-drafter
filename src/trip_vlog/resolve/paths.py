"""Path and file-URL normalisation for comparing Resolve paths with expected media paths (DESIGN 22.1)."""
from __future__ import annotations

import os
import posixpath
import re
import sys
import unicodedata
import urllib.parse
from pathlib import PureWindowsPath
from typing import Any, Iterable


def normalize_path(p: Any, base_dir: str | os.PathLike | None = None, windows: bool | None = None) -> str:
    """Canonical comparison form: file:// URLs decoded, relative paths resolved against base_dir,
    forward slashes, no trailing slash. Case is folded only when ``windows`` is true; ``paths_equal`` decides about folding itself
    (Windows-style paths, and every path on macOS, whose default volumes are case-insensitive)."""
    if p is None:
        return ""
    s = str(p).strip()
    if not s:
        return ""
    is_url = s.lower().startswith("file:")
    if is_url:
        u = urllib.parse.urlparse(s)
        host = u.netloc
        path = urllib.parse.unquote(u.path)
        if host and host.lower() != "localhost":  # file://server/share/x -> //server/share/x (UNC)
            path = "//" + host + path
        # /C:/dir -> C:/dir
        if re.match(r"^/[A-Za-z]:[/\\]", path) or re.match(r"^/[A-Za-z]:$", path):
            path = path[1:]
        s = path
    s = s.replace("\\", "/")
    if s.startswith("\\\\?\\") or s.startswith("//?/"):
        s = s[4:]
    drive = re.match(r"^[A-Za-z]:/", s) is not None or s.startswith("//")
    if not drive and not s.startswith("/") and base_dir is not None:
        s = os.path.normpath(os.path.join(str(base_dir).replace("\\", "/"), s)).replace("\\", "/")
    else:
        # collapse ./ and ../ without touching UNC / drive roots
        prefix = ""
        m = re.match(r"^([A-Za-z]:)(/.*)?$", s)
        if m:
            prefix, rest = m.group(1), m.group(2) or "/"
        elif s.startswith("//"):
            prefix, rest = "//", s[2:]
        else:
            rest = s
        norm = os.path.normpath(rest.replace("/", os.sep)).replace("\\", "/") if rest else rest
        if prefix == "//" and not norm.startswith("/"):
            norm = norm
        s = prefix + norm if prefix != "//" else "//" + norm.lstrip("/")
    if len(s) > 1:
        s = s.rstrip("/")
    if windows is None:
        windows = os.name == "nt"
    return s.lower() if windows else s


def _windows_style(n: str) -> bool:
    return re.match(r"^[A-Za-z]:/", n) is not None or n.startswith("//")


def paths_equal(a: Any, b: Any, base_a: Any = None, base_b: Any = None, fold_case: bool | None = None) -> bool:
    """Same file by name: normalised separators, file URLs decoded, Unicode compared in NFC (macOS file systems hand back NFD names).

    Case folding (DESIGN §22.1: case-insensitive on Windows and macOS): with ``fold_case=None`` it is on when both paths are Windows-style
    (drive letter or UNC; those are case-insensitive wherever they are compared) or the host is macOS (default volumes are
    case-insensitive). On Linux POSIX paths stay case-sensitive. ``True`` folds any pair, ``False`` never folds."""
    na = unicodedata.normalize("NFC", normalize_path(a, base_a, windows=False))
    nb = unicodedata.normalize("NFC", normalize_path(b, base_b, windows=False))
    if not na or not nb:
        return False
    if na == nb:
        return True
    if fold_case is None:
        fold_case = sys.platform == "darwin" or (_windows_style(na) and _windows_style(nb))
    return bool(fold_case) and na.lower() == nb.lower()



def is_abs_ref(raw: str) -> bool:
    n = normalize_path(raw, None, windows=False)
    return bool(re.match(r"^[A-Za-z]:/", n)) or n.startswith("/")


def format_media_ref(norm: str, style: str) -> str:
    """Render a normalized absolute path (forward slashes) as bare / slash / file_url."""
    win = bool(re.match(r"^[A-Za-z]:/", norm)) or norm.startswith("//")
    if style == "slash":
        return norm
    if style == "bare":
        return str(PureWindowsPath(norm)) if win else norm
    if style == "file_url":
        if norm.startswith("//"):
            return "file:" + urllib.parse.quote(norm, safe="/")
        if win:
            return "file:///" + urllib.parse.quote(norm, safe="/:")
        return "file://" + urllib.parse.quote(norm, safe="/")
    raise ValueError(f"unknown OTIO variant style: {style}")



def native_path(p: Any) -> str:
    """Absolute path with native-style separators for Resolve calls (Windows backslashes if it is a drive path)."""
    n = normalize_path(p, None, windows=False)
    return format_media_ref(n, "bare") if is_abs_ref(n) else str(p)



def common_media_dir(paths: Iterable[Any]) -> str | None:
    """Absolute directory holding all given media files (their common parent if several), native separators."""
    dirs = []
    for sp in paths:
        if sp:
            n = normalize_path(sp, None, windows=False)
            if is_abs_ref(n):
                dirs.append(posixpath.dirname(n))
    if not dirs:
        return None
    uniq = sorted(set(dirs))
    try:
        d = uniq[0] if len(uniq) == 1 else posixpath.commonpath(uniq)
    except ValueError:
        return None
    if not d or d in ("/", "."):
        return None
    return format_media_ref(d, "bare")
