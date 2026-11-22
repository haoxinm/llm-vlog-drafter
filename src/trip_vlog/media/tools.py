"""Locate ffprobe / ffmpeg per platform (config, env, PATH, standard install dirs)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_EXTRA_DIRS_MAC = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
_EXTRA_DIRS_WIN = [r"C:\ProgramData\chocolatey\bin", r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"]


def _exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def find_tool(name: str, configured: str | None = None, env_var: str | None = None) -> Path | None:
    """Resolve an executable. Order: configured path, $ENV_VAR, PATH, standard install locations."""
    candidates: list[str] = []
    for c in (configured, os.environ.get(env_var) if env_var else None):
        if c:
            candidates.append(c)
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_dir():
            p = p / _exe(name)
        if p.is_file():
            return p
        found = shutil.which(str(p))
        if found:
            return Path(found)
    found = shutil.which(name)
    if found:
        return Path(found)
    extra: list[str] = []
    if sys.platform == "darwin":
        extra = _EXTRA_DIRS_MAC
    elif sys.platform == "win32":
        extra = _EXTRA_DIRS_WIN
    for d in extra:
        p = Path(d) / _exe(name)
        if p.is_file():
            return p
    return None


def find_ffprobe(configured: str | None = None) -> Path | None:
    return find_tool("ffprobe", configured, "FFPROBE")


def find_ffmpeg(configured: str | None = None) -> Path | None:
    return find_tool("ffmpeg", configured, "FFMPEG")


def popen_flags() -> dict:
    """subprocess kwargs: no console window on Windows."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def tool_version(exe: Path | None, timeout: float = 15.0) -> str | None:
    """First token after the program name on the `-version` line, e.g. '6.1.1-3ubuntu5'."""
    if exe is None:
        return None
    try:
        cp = subprocess.run([str(exe), "-version"], capture_output=True, timeout=timeout, encoding="utf-8",
                            errors="replace", stdin=subprocess.DEVNULL, **popen_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"version\s+(\S+)", cp.stdout or "")
    return m.group(1) if m else None
