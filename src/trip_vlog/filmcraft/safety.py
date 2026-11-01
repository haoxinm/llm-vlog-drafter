"""Path confinement and the command allowlist (DESIGN §17).

The FilmCraft MCP server has no folder confinement of its own: it reads and writes anywhere the user can.
The adapter therefore restricts, before any call leaves the process,
  * reads  to the media root and the workspace, and
  * writes to the workspace only.
Only allowlisted tools and engine command ids with the exact parameter names below are ever sent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from trip_vlog.filmcraft.errors import ParameterError, PathPolicyError
from trip_vlog.paths import is_within


class PathPolicy:
    def __init__(self, workspace: Path | str, media_root: Path | str | None = None,
                 extra_read_roots: Iterable[Path | str] = ()):
        self.workspace = Path(workspace).expanduser().resolve()
        self.media_root = Path(media_root).expanduser().resolve() if media_root is not None else None
        self.read_roots: list[Path] = [self.workspace]
        if self.media_root is not None:
            self.read_roots.append(self.media_root)
        self.read_roots += [Path(r).expanduser().resolve() for r in extra_read_roots]

    @staticmethod
    def _normalize(path: Any, what: str) -> Path:
        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if not isinstance(path, str) or not path.strip():
            raise PathPolicyError(f"{what}: path must be a non-empty string, got {path!r}")
        if "\x00" in path:
            raise PathPolicyError(f"{what}: path contains a NUL byte")
        p = Path(path)
        if not p.is_absolute():
            raise PathPolicyError(f"{what}: path must be absolute (FilmCraft resolves relative paths against its own "
                                  f"working directory), got {path!r}")
        if os.name == "nt" and not p.drive:
            raise PathPolicyError(f"{what}: path needs a drive letter or UNC share, got {path!r}")
        return p

    def check_read(self, path: Any, what: str = "read") -> Path:
        """Resolved path if it lies inside the media root or the workspace, else PathPolicyError."""
        p = self._normalize(path, what).resolve()
        if not any(is_within(p, r) for r in self.read_roots):
            raise PathPolicyError(f"{what}: {p} is outside the allowed read roots (media root and workspace)")
        return p

    def check_write(self, path: Any, what: str = "write") -> Path:
        """Resolved path if it lies inside the workspace, else PathPolicyError."""
        p = self._normalize(path, what).resolve()
        if not is_within(p, self.workspace):
            raise PathPolicyError(f"{what}: {p} is outside the workspace {self.workspace}; the adapter only writes there")
        if p == self.workspace:
            raise PathPolicyError(f"{what}: refusing to write to the workspace root itself")
        return p


# ----------------------------------------------------------------------------
# Allowlist: engine command id -> permitted parameter names and path semantics
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandRule:
    allowed: frozenset[str]
    required: frozenset[str] = frozenset()
    write_paths: tuple[str, ...] = ()  # parameter names holding a path to be written
    read_paths: tuple[str, ...] = ()  # parameter names holding a path to be read
    path_lists: tuple[str, ...] = ()  # parameter names holding a list of read paths


def _rule(allowed: str, required: str = "", *, write: str = "", read: str = "", lists: str = "") -> CommandRule:
    sp = lambda s: tuple(x for x in s.split() if x)
    return CommandRule(frozenset(allowed.split()), frozenset(required.split()), sp(write), sp(read), sp(lists))


COMMAND_RULES: dict[str, CommandRule] = {
    "file.newProject": _rule("name", "name"),
    "file.saveAs": _rule("path", "path", write="path"),
    "file.save": _rule("path", write="path"),
    "file.open": _rule("path", "path", read="path"),
    "file.newSequence": _rule("name width height fps video audio sampleRate", "name"),
    "sequence.addTracks": _rule("video audio"),
    "timeline.place": _rule("item track audioTrack time frame seconds sourceIn duration video audio insert", "item track"),
    "timeline.trim": _rule("clip edge mode delta deltaFrames", "clip edge"),
    "timeline.move": _rule("moves insert linked copy", "moves"),
    "sequence.applyVideoTransition": _rule("clip effect frames edge", "clip"),
    "sequence.applyAudioTransition": _rule("clip effect frames edge", "clip"),
    "graphics.newText": _rule("text position size seconds time track", "text"),
    "markers.add": _rule("time name comment color target durationFrames", "time"),
    "clip.audioGain": _rule("clips mode db", "clips db"),
    "media.status": _rule("item"),
    "file.exportMedia": _rule("path format wait scale sequence audio", "path", write="path"),
    "file.exportInterchange": _rule("format path sequence", "format path", write="path"),
}

ALLOWED_TOOLS = frozenset({"command_run", "command_list", "project_inspect", "sequence_inspect", "doc_inspect",
                           "media_import", "render_frame"})
_TOOL_ARGS: dict[str, frozenset[str]] = {
    "command_run": frozenset({"id", "params"}), "command_list": frozenset({"filter", "enabled_only"}),
    "project_inspect": frozenset(), "sequence_inspect": frozenset(), "doc_inspect": frozenset(),
    "media_import": frozenset({"paths"}), "render_frame": frozenset({"seconds", "max_side"}),
}
EXPORT_FORMATS = frozenset({"edl", "xml", "fcpxml", "otio", "aaf", "omf"})
EXPORT_MEDIA_FORMATS = frozenset({"h264"})


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_command(cid: str, params: Mapping[str, Any] | None, policy: PathPolicy) -> dict[str, Any]:
    """Validate an engine command locally and return the (path-normalised) params to send."""
    rule = COMMAND_RULES.get(cid)
    if rule is None:
        raise ParameterError(f"command {cid!r} is not on the adapter allowlist")
    params = dict(params or {})
    unknown = set(params) - rule.allowed
    if unknown:
        raise ParameterError(f"{cid}: unknown parameter(s) {sorted(unknown)}; allowed: {sorted(rule.allowed)}")
    missing = rule.required - set(params)
    if missing:
        raise ParameterError(f"{cid}: missing required parameter(s) {sorted(missing)}")
    for k in rule.write_paths:
        if k in params:
            params[k] = str(policy.check_write(params[k], f"{cid}.{k}"))
    for k in rule.read_paths:
        if k in params:
            params[k] = str(policy.check_read(params[k], f"{cid}.{k}"))

    # light type checks on the parameters whose mistakes would silently corrupt the timeline
    for k in ("item", "clip", "sequence", "frame", "sourceIn", "duration", "time", "video", "audio", "deltaFrames"):
        if k in params and k not in ("video", "audio") and not _is_int(params[k]):
            raise ParameterError(f"{cid}.{k} must be an integer, got {params[k]!r}")
        if k in ("video", "audio") and k in params and cid == "timeline.place" and not isinstance(params[k], bool):
            raise ParameterError(f"{cid}.{k} must be a boolean")
    for k in ("frame", "sourceIn", "duration", "time"):
        if k in params and params[k] < 0:
            raise ParameterError(f"{cid}.{k} must be >= 0")
    if cid == "timeline.place":
        if "duration" in params and params["duration"] <= 0:
            raise ParameterError("timeline.place.duration must be > 0")
        if not (_is_track_ref(params["track"])):
            raise ParameterError(f"timeline.place.track must look like 'V1'/'A2', got {params['track']!r}")
        if "audioTrack" in params and not (_is_track_ref(params["audioTrack"]) and params["audioTrack"].startswith("A")):
            raise ParameterError(f"timeline.place.audioTrack must look like 'A1', got {params['audioTrack']!r}")
        if sum(k in params for k in ("time", "frame", "seconds")) != 1:
            raise ParameterError("timeline.place needs exactly one of time/frame/seconds")
    if cid == "clip.audioGain":
        c = params["clips"]
        if not (isinstance(c, list) and c and all(_is_int(x) for x in c)):
            raise ParameterError("clip.audioGain.clips must be a non-empty list of clip ids")
        if params.get("mode", "set") != "set":
            raise ParameterError("clip.audioGain: only mode 'set' is allowed")
    if cid == "file.exportInterchange" and params["format"] not in EXPORT_FORMATS:
        raise ParameterError(f"file.exportInterchange.format must be one of {sorted(EXPORT_FORMATS)}")
    if cid == "file.exportMedia" and params.get("format", "h264") not in EXPORT_MEDIA_FORMATS:
        raise ParameterError(f"file.exportMedia.format must be one of {sorted(EXPORT_MEDIA_FORMATS)}")
    if cid == "graphics.newText":
        if not isinstance(params["text"], str) or not params["text"].strip():
            raise ParameterError("graphics.newText.text must be a non-empty string")
        if "track" in params and (not _is_int(params["track"]) or params["track"] < 0):
            raise ParameterError("graphics.newText.track is a 0-based index")
    if cid == "markers.add" and "name" in params and not isinstance(params["name"], str):
        raise ParameterError("markers.add.name must be a string")
    return params


def _is_track_ref(v: Any) -> bool:
    import re

    return isinstance(v, str) and re.fullmatch(r"[VA][1-9][0-9]{0,2}", v) is not None


def validate_tool_call(name: str, arguments: Mapping[str, Any] | None, policy: PathPolicy) -> dict[str, Any]:
    """Validate an MCP tool call. ``command_run`` is validated per command id."""
    if name not in ALLOWED_TOOLS:
        raise ParameterError(f"MCP tool {name!r} is not on the adapter allowlist")
    args = dict(arguments or {})
    unknown = set(args) - _TOOL_ARGS[name]
    if unknown:
        raise ParameterError(f"{name}: unknown argument(s) {sorted(unknown)}")
    if name == "command_run":
        if "id" not in args:
            raise ParameterError("command_run: missing 'id'")
        args["params"] = validate_command(args["id"], args.get("params"), policy)
    elif name == "media_import":
        paths = args.get("paths")
        if not (isinstance(paths, list) and paths):
            raise ParameterError("media_import.paths must be a non-empty list")
        args["paths"] = [str(policy.check_read(p, "media_import.paths")) for p in paths]
    return args
