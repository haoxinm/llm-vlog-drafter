"""Locate filmcraft-cli, read its version, and build the capability map (DESIGN §3.3).

The map is built from what the installed binary reports at run time (MCP ``tools/list`` and
``command_list``) - never from assumptions. Required capabilities that are absent make
``CapabilityMap.require()`` raise an actionable ``CapabilityError``.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trip_vlog.filmcraft.command_log import CommandLog
from trip_vlog.filmcraft.errors import CapabilityError, FilmCraftNotFound, McpError
from trip_vlog.filmcraft.mcp_client import McpClient
from trip_vlog.logging_utils import utc_now_iso

ENV_VAR = "FILMCRAFT_CLI"
SNAPSHOT_SCHEMA = "trip_vlog.filmcraft.capability_snapshot.v1"


def exe_name(platform_name: str | None = None) -> str:
    return "filmcraft-cli.exe" if (platform_name or sys.platform) == "win32" else "filmcraft-cli"


def standard_locations(platform_name: str | None = None, env: Mapping[str, str] | None = None,
                       home: Path | None = None) -> list[Path]:
    """Per-OS install locations (FilmCraft's documented MSI folder, Homebrew prefixes, ~/.local/bin, cargo...)."""
    plat = platform_name or sys.platform
    env = os.environ if env is None else env
    home = home if home is not None else Path.home()
    exe = exe_name(plat)
    out: list[Path] = []
    if plat == "win32":
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            if env.get(var):
                out.append(Path(env[var]) / "FilmCraft" / exe)
        if env.get("LOCALAPPDATA"):
            out += [Path(env["LOCALAPPDATA"]) / "FilmCraft" / exe, Path(env["LOCALAPPDATA"]) / "Programs" / "FilmCraft" / exe]
        out += [home / ".local" / "bin" / exe, home / ".cargo" / "bin" / exe]
    elif plat == "darwin":
        out += [Path("/opt/homebrew/bin") / exe, Path("/usr/local/bin") / exe, home / ".local" / "bin" / exe,
                home / ".cargo" / "bin" / exe, Path("/Applications/FilmCraft.app/Contents/MacOS") / exe,
                home / "Applications" / "FilmCraft.app" / "Contents" / "MacOS" / exe]
    else:
        out += [home / ".local" / "bin" / exe, Path("/usr/bin") / exe, Path("/usr/local/bin") / exe,
                Path("/opt/filmcraft/bin") / exe, home / ".cargo" / "bin" / exe]
    return out


@dataclass(frozen=True)
class FoundExecutable:
    path: Path
    source: str  # config | env | PATH | standard-location


def _as_file(candidate: str | Path, exe: str) -> Path | None:
    p = Path(candidate).expanduser()
    if p.is_dir():
        p = p / exe
    return p if p.is_file() else None


def find_filmcraft_cli(
    configured: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
) -> FoundExecutable:
    """config.filmcraft.executable, then $FILMCRAFT_CLI, then PATH, then standard install locations.

    An explicitly configured path (config or environment) that does not exist is an error, not a reason to
    silently fall back to another binary.
    """
    env = os.environ if env is None else env
    exe = exe_name(platform_name)
    for source, value in (("config", configured), ("env", env.get(ENV_VAR))):
        if value:
            found = _as_file(value, exe)
            if found is None and not os.path.dirname(str(value)):  # bare command name
                w = which(str(value))
                found = Path(w) if w else None
            if found is None:
                where = "filmcraft.executable in the project config" if source == "config" else f"${ENV_VAR}"
                raise FilmCraftNotFound(f"{where} is set to {str(value)!r} but no such file exists. "
                                        f"Fix the path or unset it to search PATH and the standard install locations.")
            return FoundExecutable(found.resolve(), source)
    w = which(exe)
    if w:
        return FoundExecutable(Path(w).resolve(), "PATH")
    tried = standard_locations(platform_name, env, home)
    for cand in tried:
        if cand.is_file():
            return FoundExecutable(cand.resolve(), "standard-location")
    raise FilmCraftNotFound(
        f"{exe} was not found. Install FilmCraft (Windows MSI/portable zip, the separate macOS filmcraft-cli zip, or "
        f"a Linux package; or build it with `cargo build --release -p filmcraft-cli`), then either put it on PATH, set "
        f"${ENV_VAR}, or set filmcraft.executable in project.yaml. Searched PATH and: "
        + ", ".join(str(t) for t in tried))


_VERSION_RE = re.compile(r"filmcraft-cli\s+(\S+)")


def read_version(executable: Path | str | Sequence[str], timeout: float = 20.0) -> tuple[str, str]:
    """Run ``filmcraft-cli --version``; returns (version, raw output)."""
    argv0 = [str(executable)] if isinstance(executable, (str, os.PathLike)) else [str(a) for a in executable]
    try:
        cp = subprocess.run([*argv0, "--version"], capture_output=True, timeout=timeout, encoding="utf-8",
                            errors="replace", stdin=subprocess.DEVNULL, shell=False,
                            **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}))
    except (OSError, subprocess.SubprocessError) as e:
        raise FilmCraftNotFound(f"cannot run {executable}: {e}") from None
    raw = (cp.stdout or "").strip()
    m = _VERSION_RE.search(raw)
    if cp.returncode != 0 or not m:
        raise FilmCraftNotFound(f"{executable} does not look like filmcraft-cli (--version printed {raw!r}, "
                                f"exit code {cp.returncode})")
    return m[1], raw


# ----------------------------------------------------------------------------
# Required capabilities (DESIGN §3.3) -> tool / command ids and the parameter names we rely on
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    kind: str  # "tool" | "command"
    id: str
    required: bool = True
    params: tuple[str, ...] = ()  # parameter / argument names the adapter sends
    note: str = ""


CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec("run_command", "tool", "command_run", note="MCP tool executing one engine command"),
    CapabilitySpec("list_commands", "tool", "command_list"),
    CapabilitySpec("inspect_project", "tool", "project_inspect"),
    CapabilitySpec("inspect_sequence", "tool", "sequence_inspect"),
    CapabilitySpec("import_media", "tool", "media_import", params=("paths",)),
    CapabilitySpec("create_project", "command", "file.newProject", params=("name",)),
    CapabilitySpec("save_project", "command", "file.saveAs", params=("path",)),
    CapabilitySpec("save_checkpoint", "command", "file.save", required=False),
    CapabilitySpec("open_project", "command", "file.open", required=False, params=("path",),
                   note="reopening uses `mcp --project <file>` in a new process; file.open is optional"),
    CapabilitySpec("verify_media", "command", "media.status", note="media online + original path"),
    CapabilitySpec("create_sequence", "command", "file.newSequence", params=("name", "width", "height", "fps", "video", "audio")),
    CapabilitySpec("add_tracks", "command", "sequence.addTracks", required=False, params=("video", "audio")),
    CapabilitySpec("place_clip", "command", "timeline.place",
                   params=("item", "track", "frame", "sourceIn", "duration", "video", "audio", "audioTrack")),
    CapabilitySpec("trim_clip", "command", "timeline.trim", required=False, params=("clip", "edge", "mode", "deltaFrames")),
    CapabilitySpec("move_clip", "command", "timeline.move", required=False, params=("moves",)),
    CapabilitySpec("add_marker", "command", "markers.add", params=("time", "name", "comment")),
    CapabilitySpec("add_title", "command", "graphics.newText", params=("text", "seconds", "time", "size", "position", "track")),
    CapabilitySpec("add_transition", "command", "sequence.applyVideoTransition", params=("clip", "edge", "effect", "frames")),
    CapabilitySpec("audio_gain", "command", "clip.audioGain", params=("clips", "mode", "db")),
    CapabilitySpec("audio_fade", "command", "sequence.applyAudioTransition", required=False, params=("clip", "effect", "edge", "frames")),
    CapabilitySpec("undo", "command", "edit.undo", required=False),
    CapabilitySpec("render_preview", "command", "file.exportMedia", params=("path", "format", "wait")),
    CapabilitySpec("render_frame", "tool", "render_frame", required=False),
    CapabilitySpec("export_interchange", "command", "file.exportInterchange", params=("format", "path", "sequence")),
)

_PARAM_KEY = re.compile(r'"(\w+)"\s*:')


def param_keys(grammar: str | None) -> set[str]:
    """Parameter names mentioned in a command's compact ``params`` grammar string."""
    return set(_PARAM_KEY.findall(grammar or ""))


@dataclass
class Capability:
    name: str
    kind: str
    id: str
    required: bool
    found: bool
    params: str | None = None  # the grammar string reported by the binary (commands)
    schema: dict[str, Any] | None = None  # MCP inputSchema (tools)
    missing_params: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.found and not self.missing_params

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "id": self.id, "required": self.required, "found": self.found,
                "usable": self.usable, "params": self.params, "schema": self.schema,
                "missing_params": self.missing_params, "note": self.note}


@dataclass
class CapabilityMap:
    executable: str
    version: str
    version_output: str
    server_info: dict[str, Any]
    protocol_version: str | None
    tools: list[str]
    command_count: int
    capabilities: dict[str, Capability]
    platform: dict[str, str] = field(default_factory=dict)
    discovered_at: str = ""
    executable_source: str = ""

    def has(self, name: str) -> bool:
        c = self.capabilities.get(name)
        return bool(c and c.usable)

    def command_id(self, name: str) -> str:
        return self.capabilities[name].id

    def missing_required(self) -> list[Capability]:
        return [c for c in self.capabilities.values() if c.required and not c.usable]

    def missing_optional(self) -> list[Capability]:
        return [c for c in self.capabilities.values() if not c.required and not c.usable]

    def require(self) -> "CapabilityMap":
        """Raise a CapabilityError naming every missing required capability, else return self."""
        bad = self.missing_required()
        if bad:
            lines = []
            for c in bad:
                if not c.found:
                    lines.append(f"{c.name}: {c.kind} `{c.id}` is not offered")
                else:
                    lines.append(f"{c.name}: `{c.id}` lacks parameter(s) {', '.join(c.missing_params)}")
            raise CapabilityError(
                f"filmcraft-cli {self.version} ({self.executable}) is missing capabilities this pipeline requires:\n  - "
                + "\n  - ".join(lines)
                + "\nInstall a FilmCraft build that provides them (the pipeline was verified against 0.13.0) or update the "
                  "adapter mapping after re-running the Phase 0 spike.", [c.name for c in bad])
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SNAPSHOT_SCHEMA, "discovered_at": self.discovered_at,
            "filmcraft": {"executable": self.executable, "executable_source": self.executable_source,
                          "version": self.version, "version_output": self.version_output,
                          "server_info": self.server_info, "protocol_version": self.protocol_version},
            "platform": self.platform, "mcp_tools": self.tools, "command_count": self.command_count,
            "capabilities": {k: c.to_dict() for k, c in self.capabilities.items()},
            "missing_required": [c.name for c in self.missing_required()],
            "missing_optional": [c.name for c in self.missing_optional()],
        }


def build_capability_map(
    *, executable: str, version: str, version_output: str, server_info: Mapping[str, Any], protocol_version: str | None,
    tools: Sequence[Mapping[str, Any]], commands: Sequence[Mapping[str, Any]], executable_source: str = "",
    specs: Sequence[CapabilitySpec] = CAPABILITY_SPECS,
) -> CapabilityMap:
    tool_by_name = {t["name"]: t for t in tools if isinstance(t, Mapping) and "name" in t}
    cmd_by_id = {c["id"]: c for c in commands if isinstance(c, Mapping) and "id" in c}
    caps: dict[str, Capability] = {}
    for s in specs:
        if s.kind == "tool":
            t = tool_by_name.get(s.id)
            cap = Capability(s.name, s.kind, s.id, s.required, t is not None, schema=(t or {}).get("inputSchema"), note=s.note)
            if t is not None and s.params:
                props = set(((t.get("inputSchema") or {}).get("properties") or {}))
                cap.missing_params = [p for p in s.params if p not in props]
        else:
            c = cmd_by_id.get(s.id)
            cap = Capability(s.name, s.kind, s.id, s.required, c is not None, params=(c or {}).get("params"), note=s.note)
            if c is not None and s.params:
                have = param_keys(c.get("params"))
                cap.missing_params = [p for p in s.params if p not in have]
        caps[s.name] = cap
    return CapabilityMap(
        executable=executable, version=version, version_output=version_output, server_info=dict(server_info),
        protocol_version=protocol_version, tools=sorted(tool_by_name), command_count=len(cmd_by_id), capabilities=caps,
        platform={"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                  "python": platform.python_version()},
        discovered_at=utc_now_iso(), executable_source=executable_source)


def discover_capabilities(
    executable: str | os.PathLike[str] | Sequence[str],
    *,
    log: CommandLog | None = None,
    timeout: float = 120.0,
    client_factory: Callable[..., Any] | None = None,
    executable_source: str = "",
    version: tuple[str, str] | None = None,
) -> CapabilityMap:
    """Start a throw-away MCP session, read tools + commands, and map them to the required capabilities."""
    argv0 = [os.fspath(executable)] if isinstance(executable, (str, os.PathLike)) else [os.fspath(a) for a in executable]
    ver, raw = version if version else read_version(argv0)
    factory = client_factory or McpClient
    client = factory([*argv0, "mcp"], default_timeout=timeout, log=log, label="discovery")
    try:
        tools = client.list_tools(timeout)
        res = client.tool_call("command_list", {}, timeout)
        if not res.ok or not isinstance(res.payload, list):
            raise McpError(f"command_list failed: {res.text[:300]}")
        info, proto = client.server_info, client.protocol_version
    finally:
        client.close()
    return build_capability_map(executable=" ".join(argv0), version=ver, version_output=raw, server_info=info,
                                protocol_version=proto, tools=tools, commands=res.payload,
                                executable_source=executable_source)


def write_snapshot(cmap: CapabilityMap, path: Path) -> Path:
    """Write ``filmcraft/capability_snapshot.json`` (UTF-8, ``\\n``, atomic)."""
    from trip_vlog.state import atomic_write_json

    atomic_write_json(Path(path), cmap.to_dict())
    return Path(path)
