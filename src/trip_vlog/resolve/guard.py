"""Allowlisted, logged access to DaVinci Resolve's scripting API (DESIGN 15.6, 17).

``resolve-finish`` never touches a raw Resolve object. Every object it gets from the API is wrapped in a
:class:`Guard` that only exposes the methods listed in :data:`ALLOWED` for that kind of object, logs each call with
sanitised arguments and a summarised result, and enforces two argument rules:

* ``MediaPoolItem.ReplaceClip(path)`` only accepts a path that is listed in the media manifest, or an isolated copy of a
  manifest still registered with :meth:`ApiSession.allow_staged`;
* ``Timeline.DeleteClips`` / ``MediaPool.DeleteTimelines`` only accept objects that this run created.

There is no ``eval``/``exec`` and nothing here is reachable by an LLM. Methods that delete projects, load or close
projects, or run scripts are not in the allowlist, so calling them raises :class:`AllowlistViolation`.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from trip_vlog.logging_utils import sanitize, utc_now_iso
from trip_vlog.resolve.paths import normalize_path

# role -> {method: result role}. A result role is "" (plain value), "name" (one wrapped object), "list:name"
# (list of wrapped objects) or "dict:name" (dict whose values are wrapped objects).
ALLOWED: dict[str, dict[str, str]] = {
    "resolve": {"GetProductName": "", "GetVersionString": "", "GetVersion": "", "GetProjectManager": "pm"},
    "pm": {"GetCurrentProject": "project", "GetCurrentFolder": "", "GetProjectListInCurrentFolder": "",
           "CreateProject": "project", "SaveProject": ""},
    "project": {"GetName": "", "SetSetting": "", "GetSetting": "", "GetMediaPool": "mp", "SetCurrentTimeline": "",
                "GetCurrentTimeline": "timeline"},
    "mp": {"ImportTimelineFromFile": "timeline", "CreateEmptyTimeline": "timeline", "AppendToTimeline": "list:item",
           "GetRootFolder": "folder", "DeleteTimelines": ""},
    "folder": {"GetName": "", "GetClipList": "list:mpi"},
    "mpi": {"GetName": "", "GetClipProperty": "", "ReplaceClip": ""},
    "timeline": {"GetName": "", "GetStartFrame": "", "GetEndFrame": "", "GetSetting": "", "GetTrackCount": "",
                 "GetTrackName": "", "GetItemListInTrack": "list:item", "GetMarkers": "", "AddMarker": "", "AddTrack": "",
                 "InsertFusionTitleIntoTimeline": "item", "InsertTitleIntoTimeline": "item", "DeleteClips": ""},
    "item": {"GetName": "", "GetStart": "", "GetEnd": "", "GetDuration": "", "GetLeftOffset": "",
             "GetMediaPoolItem": "mpi", "GetFusionCompCount": "", "GetFusionCompByIndex": "comp"},
    "comp": {"FindToolByID": "tool", "FindTool": "tool"},
    "tool": {"SetInput": "", "GetInput": "", "GetAttrs": ""},
}
# Calls that change something in Resolve (everything else only reads).
MUTATING = frozenset({
    "CreateProject", "SaveProject", "SetSetting", "SetCurrentTimeline", "ImportTimelineFromFile", "CreateEmptyTimeline",
    "AppendToTimeline", "DeleteTimelines", "ReplaceClip", "AddMarker", "AddTrack", "InsertFusionTitleIntoTimeline",
    "InsertTitleIntoTimeline", "DeleteClips", "SetInput",
})
MAX_LOGGED_CALLS = 20000
_MAX_STR = 300


class AllowlistViolation(AttributeError):
    """A call outside the allowlist, or an allowlisted call with a forbidden argument. Subclasses AttributeError so
    ``getattr(obj, name, default)`` treats a blocked read-only probe like a missing method."""


def media_key(path: Any) -> str:
    """Comparison key of a media path: normalised, case-folded on Windows and macOS."""
    n = normalize_path(path, None, windows=False)
    return n.lower() if (os.name == "nt" or sys.platform == "darwin") else n


def stills_key(source: Any) -> str:
    """Folder name of the isolated copy of a source still: first 12 hex of sha1(media_key(source))."""
    return hashlib.sha1(media_key(source).encode("utf-8")).hexdigest()[:12]


_HEX12 = re.compile(r"^[0-9a-f]{12}$")


@dataclass
class ApiSession:
    """The call log and the rules shared by all guarded objects of one run."""

    allowed_media: frozenset[str] = frozenset()  # media_key() of every path in media_manifest.csv
    log: list[dict[str, Any]] = field(default_factory=list)
    dropped_calls: int = 0
    created_items: dict[int, Any] = field(default_factory=dict)  # id(raw) -> raw; created by this run
    created_timelines: dict[int, Any] = field(default_factory=dict)
    redactions: tuple[tuple[str, str], ...] = ()
    stills_root: Any = None  # <workspace>/resolve_handoff/stills; isolated copies of manifest stills live below it
    staged_media: set[str] = field(default_factory=set)  # media_key() of registered isolated copies
    clock: Callable[[], str] = utc_now_iso

    # ---- wrapping
    def wrap(self, raw: Any, role: str) -> Any:
        return None if raw is None else Guard(raw, role, self)

    def unwrap(self, v: Any) -> Any:
        if isinstance(v, Guard):
            return object.__getattribute__(v, "_raw")
        if isinstance(v, (list, tuple)):
            return type(v)(self.unwrap(x) for x in v)
        if isinstance(v, dict):
            return {k: self.unwrap(x) for k, x in v.items()}
        return v

    def _wrap_result(self, res: Any, spec: str) -> Any:
        if not spec or res is None:
            return res
        if spec.startswith("list:"):
            return [self.wrap(x, spec[5:]) for x in res] if isinstance(res, (list, tuple)) else res
        if spec.startswith("dict:"):
            return {k: self.wrap(x, spec[5:]) for k, x in res.items()} if isinstance(res, dict) else res
        return self.wrap(res, spec)

    # ---- logging
    def _text(self, s: str) -> str:
        s = sanitize(s, redactions=self.redactions)
        return s if len(s) <= _MAX_STR else s[:_MAX_STR] + "..."

    def describe(self, v: Any, depth: int = 0) -> Any:
        """JSON-safe, size-bounded description of an argument or result."""
        if isinstance(v, Guard):
            return f"<{object.__getattribute__(v, '_role')}>"
        if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            return self._text(v)
        if isinstance(v, (list, tuple)):
            if depth > 3:
                return f"[{len(v)} items]"
            return [self.describe(x, depth + 1) for x in v[:12]] + ([f"... {len(v) - 12} more"] if len(v) > 12 else [])
        if isinstance(v, dict):
            if depth > 3:
                return f"{{{len(v)} keys}}"
            out = {str(k): self.describe(x, depth + 1) for k, x in list(v.items())[:20]}
            if len(v) > 20:
                out["..."] = f"{len(v) - 20} more"
            return out
        return f"<{type(v).__name__}>"

    def record(self, entry: dict[str, Any]) -> None:
        if len(self.log) >= MAX_LOGGED_CALLS:
            self.dropped_calls += 1
            return
        entry["n"] = len(self.log) + 1
        entry["at"] = self.clock()
        self.log.append(entry)

    def allow_staged(self, source: Any, staged: Any) -> None:
        """Also allow ReplaceClip with ``staged``: the isolated copy of the manifest file ``source``. Refused unless the source
        is in the manifest and ``staged`` is a file with the same name directly inside ``<stills_root>/<key of source>/``."""
        if media_key(source) not in self.allowed_media:
            raise AllowlistViolation(f"staged copy refused: {source!r} is not listed in media_manifest.csv")
        if self.stills_root is None:
            raise AllowlistViolation("staged copy refused: no stills root configured")
        try:
            sp = Path(str(staged)).resolve()
            root = Path(str(self.stills_root)).resolve()
        except (OSError, ValueError) as e:
            raise AllowlistViolation(f"staged copy refused: {e}") from None
        base = os.path.basename(str(source).replace("\\", "/"))
        if not (sp.is_file() and sp.parent.parent == root and _HEX12.match(sp.parent.name) and sp.parent.name == stills_key(source)
                and sp.name == base):
            raise AllowlistViolation(f"staged copy refused: {staged!r} is not the isolated copy of {base!r} under {root}")
        self.staged_media.update({media_key(sp), media_key(staged)})

    # ---- argument rules
    def check_args(self, role: str, name: str, raw_args: tuple[Any, ...]) -> None:
        if role == "mpi" and name == "ReplaceClip":
            path = raw_args[0] if raw_args else None
            if not isinstance(path, str) or (media_key(path) not in self.allowed_media and media_key(path) not in self.staged_media):
                raise AllowlistViolation(f"ReplaceClip refused: {path!r} is not listed in media_manifest.csv")
        elif role == "timeline" and name == "DeleteClips":
            items = raw_args[0] if raw_args else None
            if not isinstance(items, (list, tuple)) or not items or any(id(i) not in self.created_items for i in items):
                raise AllowlistViolation("DeleteClips refused: only timeline items created by this run may be deleted")
        elif role == "mp" and name == "DeleteTimelines":
            tls = raw_args[0] if raw_args else None
            if not isinstance(tls, (list, tuple)) or not tls or any(id(t) not in self.created_timelines for t in tls):
                raise AllowlistViolation("DeleteTimelines refused: only timelines created by this run may be deleted")

    def after_call(self, role: str, name: str, raw_res: Any) -> None:
        """Remember what this run created so that rollback can be restricted to it."""
        if role == "timeline" and name in ("InsertFusionTitleIntoTimeline", "InsertTitleIntoTimeline") and raw_res is not None:
            self.created_items[id(raw_res)] = raw_res
        elif role == "mp" and name == "AppendToTimeline" and isinstance(raw_res, (list, tuple)):
            for i in raw_res:
                if i is not None:
                    self.created_items[id(i)] = i
        elif role == "mp" and name in ("CreateEmptyTimeline", "ImportTimelineFromFile") and raw_res is not None:
            self.created_timelines[id(raw_res)] = raw_res

    # ---- summaries
    def mutating_calls(self) -> list[dict[str, Any]]:
        return [e for e in self.log if e.get("mutating")]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.log:
            out[e["call"]] = out.get(e["call"], 0) + 1
        return dict(sorted(out.items()))


class Guard:
    """Proxy for one Resolve object; only allowlisted methods are reachable."""

    __slots__ = ("_raw", "_role", "_session")

    def __init__(self, raw: Any, role: str, session: ApiSession):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_role", role)
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        role: str = object.__getattribute__(self, "_role")
        session: ApiSession = object.__getattribute__(self, "_session")
        raw = object.__getattribute__(self, "_raw")
        allowed = ALLOWED.get(role, {})
        if name.startswith("_") or name not in allowed:
            session.record({"call": f"{role}.{name}", "args": [], "blocked": True, "mutating": name in MUTATING,
                            "error": "not in the allowlist"})
            raise AllowlistViolation(f"{role}.{name} is not an allowlisted Resolve API call")
        result_spec = allowed[name]
        fn = getattr(raw, name, None)

        def call(*args: Any) -> Any:
            entry: dict[str, Any] = {"call": f"{role}.{name}", "args": [session.describe(a) for a in args],
                                     "mutating": name in MUTATING}
            raw_args = tuple(session.unwrap(a) for a in args)
            try:
                session.check_args(role, name, raw_args)
            except AllowlistViolation as e:
                entry.update(blocked=True, error=str(e))
                session.record(entry)
                raise
            if fn is None:
                entry["error"] = f"AttributeError: {type(raw).__name__} has no method {name}"
                session.record(entry)
                raise AttributeError(entry["error"])
            t0 = time.perf_counter()
            try:
                raw_res = fn(*raw_args)
            except Exception as e:  # noqa: BLE001 - the Resolve API raises arbitrary exception types
                entry["error"] = session._text(f"{type(e).__name__}: {e}")
                entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                session.record(entry)
                raise
            entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            session.after_call(role, name, raw_res)
            res = session._wrap_result(raw_res, result_spec)
            entry["result"] = session.describe(raw_res if not result_spec else res)
            session.record(entry)
            return res

        call.__name__ = name
        return call

    def __repr__(self) -> str:
        return f"<Guard {object.__getattribute__(self, '_role')}>"


def media_keys(paths: Iterable[Any]) -> frozenset[str]:
    return frozenset(media_key(p) for p in paths if p)
