"""Sanitized JSONL command log (``filmcraft/command_log.jsonl``, DESIGN §12.1).

Every request and response is recorded after masking:
  * media root -> ``$MEDIA_ROOT``, workspace -> ``$WORKSPACE``, home directory -> ``~`` (plain, JSON-escaped
    backslash, forward-slash and ``file://`` URL spellings; case-insensitive on Windows / macOS),
  * values under keys that look like tokens / secrets / passwords / authorization headers -> ``***``,
  * secret-looking environment values and ``key=value`` secrets (via ``logging_utils.sanitize``),
  * very large strings (base64 frames, the 700-command catalog) are truncated with a SHA-256 stub.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

from trip_vlog.logging_utils import sanitize, utc_now_iso

_SECRET_KEY = re.compile(r"token|secret|password|passwd|authorization|api[_-]?key|credential", re.I)
MAX_STRING = 2000


def _spellings(path: Path) -> set[str]:
    s = str(path)
    out = {s, path.as_posix(), s.replace("\\", "\\\\"), urllib.parse.quote(path.as_posix(), safe="/:")}
    try:
        out.add(path.as_uri())
    except ValueError:
        pass
    return {o for o in out if len(o) > 1}


class Sanitizer:
    def __init__(self, replacements: Iterable[tuple[Path | str, str]] = ()):
        pairs: list[tuple[str, str]] = []
        for real, token in replacements:
            if real is None or str(real) in ("", "/", "\\"):
                continue
            for sp in _spellings(Path(real)):
                pairs.append((sp, token))
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        flags = re.IGNORECASE if sys.platform in ("win32", "darwin") else 0
        self._rules = [(re.compile(re.escape(lit), flags), tok) for lit, tok in pairs]

    def text(self, s: str) -> str:
        for rx, tok in self._rules:
            s = rx.sub(tok.replace("\\", "\\\\"), s)
        s = sanitize(s)
        if len(s) > MAX_STRING:
            digest = hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:12]
            s = f"{s[:MAX_STRING // 2]}...[truncated {len(s)} chars sha256={digest}]"
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, dict):
            return {self.text(str(k)): ("***" if _SECRET_KEY.search(str(k)) else self.obj(v)) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        if isinstance(o, str):
            return self.text(o)
        return o


def default_replacements(workspace: Path | None, media_root: Path | None) -> list[tuple[Path, str]]:
    reps: list[tuple[Path, str]] = []
    if media_root is not None:
        reps.append((Path(media_root), "$MEDIA_ROOT"))
    if workspace is not None:
        reps.append((Path(workspace), "$WORKSPACE"))
    try:
        reps.append((Path.home(), "~"))
    except RuntimeError:
        pass
    return reps


class CommandLog:
    """Thread-safe append-only JSONL writer. ``path=None`` keeps records in memory only (tests)."""

    def __init__(self, path: Path | None, sanitizer: Sanitizer | None = None):
        self.path = Path(path) if path is not None else None
        self.sanitizer = sanitizer or Sanitizer()
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._t0 = time.monotonic()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": utc_now_iso(), "t": round(time.monotonic() - self._t0, 3), **fields}
            rec = self.sanitizer.obj(rec)
            self.records.append(rec) if self.path is None else None
            if self.path is not None:
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            return rec
