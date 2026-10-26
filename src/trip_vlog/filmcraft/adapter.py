"""FilmCraftAdapter (DESIGN §12): the only module that talks to FilmCraft.

Rules enforced here
  * every tool / command is on an allowlist and its parameters are validated locally first (``safety``),
  * the MCP server has no confinement, so reads are limited to media root + workspace and writes to the workspace,
  * frames become ticks only via ``Timebase`` (exact rational arithmetic),
  * clips are placed at computed positions (never rippled), stops at the first failing command,
  * after mutating, state is read back (``sequence_inspect``) and verified (see ``build_project``).
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from trip_vlog.config import FilmCraftConfig
from trip_vlog.filmcraft import discovery
from trip_vlog.filmcraft.command_log import CommandLog, Sanitizer, default_replacements
from trip_vlog.filmcraft.discovery import CapabilityMap
from trip_vlog.filmcraft.errors import (
    CapabilityError, CommandError, FilmCraftError, ParameterError, SequenceNotEmpty, SpecValidationError)
from trip_vlog.filmcraft.inspect import (
    ProjectSnapshot, SequenceSnapshot, VerificationReport, parse_media_status, parse_project, parse_sequence,
    verify_against_spec)
from trip_vlog.filmcraft.interchange import (
    GLOBAL_LIMITS, FormatReport, HandoffReport, postprocess_and_validate, resolve_format, titles_not_carried)
from trip_vlog.filmcraft.mappings import ClipMap, ClipRef, MediaMap, build_media_map, canon_path
from trip_vlog.filmcraft.mcp_client import McpClient, ToolResult
from trip_vlog.filmcraft.preview import RenderReport, evaluate_preview, run_ffprobe
from trip_vlog.filmcraft.safety import PathPolicy, validate_tool_call
from trip_vlog.filmcraft.timebase import Timebase, fps_command_value
from trip_vlog.filmcraft.timeline_spec import (
    Placement, TimelineSpec, check_media_bounds, downgrade_dissolves, ensure_valid, parse_track)
from trip_vlog.paths import safe_filename

HandlePolicy = Literal["error", "cut"]
IMPORT_CHUNK = 50


@dataclass
class SequenceSettings:
    name: str
    width: int
    height: int
    fps: Fraction
    video_tracks: int = 2
    audio_tracks: int = 2

    @classmethod
    def from_spec(cls, spec: TimelineSpec) -> "SequenceSettings":
        return cls(spec.sequence_name, spec.width, spec.height, spec.fps, max(1, spec.video_track_count),
                   max(1, spec.audio_track_count))


@dataclass(frozen=True)
class SequenceRef:
    sequence_id: int
    name: str


@dataclass
class ImportResult:
    media_map: MediaMap
    durations: dict[str, Fraction]  # source path -> seconds (as reported by FilmCraft)
    imported: int
    errors: list[Any] = field(default_factory=list)


@dataclass
class ApplyReport:
    clip_map: ClipMap
    placed: int = 0
    warnings: list[str] = field(default_factory=list)
    downgraded_dissolves: list[str] = field(default_factory=list)
    effective_spec: TimelineSpec | None = None
    framing: dict[int, float] = field(default_factory=dict)   # clip id -> scale (percent) FilmCraft chose for clip.fitToFrame


class FilmCraftAdapter:
    def __init__(
        self,
        workspace: Path | str,
        media_root: Path | str | None = None,
        *,
        config: FilmCraftConfig | None = None,
        executable: str | os.PathLike[str] | None = None,
        argv_prefix: Sequence[str] | None = None,
        request_timeout: float = 120.0,
        ffprobe_path: str | None = None,
        client_factory: Callable[..., Any] | None = None,
        handle_policy: HandlePolicy = "error",
        command_log: CommandLog | None = None,
        preview_file: str = "preview/draft.mp4",
        interchange_dir: str = "resolve_handoff",
    ):
        self.config = config or FilmCraftConfig()
        self.policy = PathPolicy(workspace, media_root)
        self.workspace = self.policy.workspace
        self.media_root = self.policy.media_root
        self.fc_dir = self.workspace / "filmcraft"
        self.executable = executable if executable is not None else self.config.executable
        self.argv_prefix = list(argv_prefix) if argv_prefix else None
        self.request_timeout = request_timeout
        self.ffprobe_path = ffprobe_path
        self.client_factory = client_factory or McpClient
        self.handle_policy = handle_policy
        self.log = command_log or CommandLog(
            self.fc_dir / "command_log.jsonl", Sanitizer(default_replacements(self.workspace, self.media_root)))
        self.capabilities: CapabilityMap | None = None
        self._client: Any = None
        self.project_path: Path | None = None
        self.sequence_id: int | None = None
        self.spec: TimelineSpec | None = None  # last spec built / applied by this adapter
        self.preview_file = preview_file
        self.interchange_dir = interchange_dir

    # ------------------------------------------------------------------ paths
    @property
    def snapshot_path(self) -> Path:
        return self.fc_dir / "capability_snapshot.json"

    @property
    def journal_path(self) -> Path:
        return self.fc_dir / "execution_journal.json"

    @property
    def data_dir(self) -> Path:
        return self.fc_dir / "data"

    # ------------------------------------------------------------------ discovery
    def _base_argv(self) -> list[str]:
        if self.argv_prefix:
            return list(self.argv_prefix)
        found = discovery.find_filmcraft_cli(self.executable)
        self._source = found.source
        return [str(found.path)]

    def discover_capabilities(self, force: bool = False) -> CapabilityMap:
        """Locate the binary, read version/tools/commands, write ``filmcraft/capability_snapshot.json``.

        Raises ``CapabilityError`` if a required capability is missing.
        """
        if self.capabilities is not None and not force:
            return self.capabilities
        argv0 = self._base_argv()
        cmap = discovery.discover_capabilities(
            argv0, log=self.log, timeout=self.request_timeout, client_factory=self.client_factory,
            executable_source=getattr(self, "_source", "argv_prefix"))
        self.fc_dir.mkdir(parents=True, exist_ok=True)
        discovery.write_snapshot(cmap, self.snapshot_path)
        self.capabilities = cmap
        cmap.require()
        return cmap

    @property
    def filmcraft_version(self) -> str:
        return self.discover_capabilities().version

    # ------------------------------------------------------------------ session handling
    def _start_session(self, project: Path | None, label: str) -> None:
        self.close()
        argv = [*self._base_argv(), "mcp"]
        if project is not None:
            argv += ["--project", str(project)]
        argv += ["--data-dir", str(self.data_dir)]
        self.fc_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._client = self.client_factory(argv, cwd=self.fc_dir, default_timeout=self.request_timeout, log=self.log, label=label)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def kill_server(self) -> None:
        """Hard-kill the running server (used to prove persistence: nothing unsaved survives)."""
        if self._client is not None:
            try:
                self._client.kill()
            finally:
                self._client = None

    def __enter__(self) -> "FilmCraftAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._client is not None and getattr(self._client, "alive", True)

    def _require_session(self) -> Any:
        if not self.is_open:
            raise FilmCraftError("no FilmCraft project is open; call create_project/open_project first")
        return self._client

    # ------------------------------------------------------------------ low-level calls
    def _tool(self, name: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> ToolResult:
        args = validate_tool_call(name, arguments, self.policy)
        res = self._require_session().tool_call(name, args, timeout)
        if not res.ok:
            cid = args.get("id", name)
            raise CommandError(str(cid), res.text.strip()[:500] or "no message", args.get("params"))
        return res

    def _cmd(self, cid: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        return self._tool("command_run", {"id": cid, "params": params or {}}, timeout).payload

    # ------------------------------------------------------------------ project lifecycle
    def create_project(self, project_path: Path | str, name: str | None = None) -> None:
        """Start a fresh empty project and save it to ``project_path`` (inside the workspace). Never overwrites."""
        self.discover_capabilities()
        path = self.policy.check_write(project_path, "project_path")
        if path.suffix.lower() != ".fcproj":
            raise ParameterError(f"project path must end in .fcproj: {path.name}")
        if path.exists():
            raise ParameterError(f"{path} already exists; the adapter never mutates an existing project in place "
                                 "(versioned rebuilds, DESIGN §12.2)")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._start_session(None, "build")
        self._cmd("file.newProject", {"name": name or path.stem})
        self._cmd("file.saveAs", {"path": str(path)})
        if not path.is_file():
            raise CommandError("file.saveAs", f"FilmCraft reported success but {path} does not exist")
        self.project_path = path
        self.sequence_id = None

    def open_project(self, project_path: Path | str) -> ProjectSnapshot:
        """Open an existing .fcproj in a NEW server process (``mcp --project``)."""
        self.discover_capabilities()
        path = self.policy.check_read(project_path, "project_path")
        if not path.is_file():
            raise ParameterError(f"project file not found: {path}")
        self._start_session(path, "open")
        self.project_path = path
        snap = self.inspect_project()
        self.sequence_id = snap.active_sequence
        return snap

    def save_project(self) -> None:
        """Save to the project's own path (checkpoint)."""
        if self.project_path is None:
            raise FilmCraftError("project has no path yet; create_project saves it first")
        self._cmd("file.save", {"path": str(self.project_path)})

    # ------------------------------------------------------------------ inspection
    def inspect_sequence(self) -> SequenceSnapshot:
        return parse_sequence(self._tool("sequence_inspect").payload)

    def media_status(self) -> tuple:
        return parse_media_status(self._cmd("media.status", {}))

    def inspect_project(self) -> ProjectSnapshot:
        raw = self._tool("project_inspect").payload
        return parse_project(raw, self.media_status())

    # ------------------------------------------------------------------ media
    def import_media(self, paths: Sequence[str | os.PathLike[str]]) -> ImportResult:
        """Import originals (read-only) and map each path to the FilmCraft item that references it."""
        checked: dict[str, str] = {}
        for p in paths:
            rp = self.policy.check_read(p, "import_media")
            if not rp.is_file():
                raise ParameterError(f"media file not found: {rp}")
            checked.setdefault(canon_path(rp), str(rp))
        originals = {canon_path(Path(os.fspath(p)).resolve()): os.fspath(p) for p in paths}
        errors: list[Any] = []
        todo = list(checked.values())
        for i in range(0, len(todo), IMPORT_CHUNK):
            res = self._tool("media_import", {"paths": todo[i:i + IMPORT_CHUNK]}).payload
            if isinstance(res, dict) and res.get("errors"):
                errors += res["errors"]
        if errors:
            raise CommandError("media_import", f"FilmCraft could not import: {errors[:3]}")
        # media.status says which item points at which original file; key by the resolved path we sent
        rows = [r.__dict__ for r in self.media_status()]
        mm = build_media_map(todo, rows)
        project = parse_project(self._tool("project_inspect").payload)
        durations: dict[str, Fraction] = {}
        for rp_str in todo:
            item = project.item(mm.item_for(rp_str))
            if item is not None and item.duration_seconds is not None:
                durations[rp_str] = item.duration_seconds
        # re-key onto the spec's spelling of each path
        out_map, out_dur = MediaMap(), {}
        for key, orig in originals.items():
            resolved = checked[key]
            out_map.add(orig, mm.item_for(resolved))
            if resolved in durations:
                out_dur[orig] = durations[resolved]
        return ImportResult(out_map, out_dur, len(todo), errors)

    # ------------------------------------------------------------------ sequence
    def create_sequence(self, settings: SequenceSettings) -> SequenceRef:
        tb = Timebase(settings.fps)
        res = self._cmd("file.newSequence", {
            "name": settings.name, "width": settings.width, "height": settings.height,
            "fps": fps_command_value(tb.fps), "video": settings.video_tracks, "audio": max(1, settings.audio_tracks)})
        snap = self.inspect_sequence()
        problems = []
        if (snap.width, snap.height) != (settings.width, settings.height):
            problems.append(f"size {snap.width}x{snap.height}")
        if snap.fps != tb.fps:
            problems.append(f"frame rate {snap.fps} (wanted {tb.fps})")
        nv = sum(1 for t in snap.tracks if t.kind == "video")
        na = sum(1 for t in snap.tracks if t.kind == "audio")
        if nv < settings.video_tracks or na < settings.audio_tracks:
            problems.append(f"tracks V{nv}/A{na} (wanted V{settings.video_tracks}/A{settings.audio_tracks})")
        if problems:
            raise CommandError("file.newSequence", "sequence was created differently than requested: " + ", ".join(problems))
        sid = res.get("sequence") if isinstance(res, dict) else None
        self.sequence_id = snap.sequence_id if snap.sequence_id is not None else sid
        return SequenceRef(int(self.sequence_id), settings.name)

    # ------------------------------------------------------------------ timeline
    def apply_timeline(self, spec: TimelineSpec, media_map: MediaMap, durations: dict[str, Fraction] | None = None) -> ApplyReport:
        """Place everything in ``spec`` on the (empty) active sequence.

        All validation - structure, paths, source bounds, dissolve handles - runs BEFORE the first mutation.
        Placement is at computed frame positions; nothing is rippled.
        """
        ensure_valid(spec)
        for p in spec.placements:
            self.policy.check_read(p.source_path, f"{p.clip_instance_id}.source_path")
        before = self.inspect_sequence()
        if before.clip_count or before.markers:
            raise SequenceNotEmpty(f"the active sequence already holds {before.clip_count} clip(s) and "
                                   f"{len(before.markers)} marker(s); applying again would duplicate them. "
                                   "Build a new project version instead.")
        if (before.width, before.height) != (spec.width, spec.height) or before.fps != spec.fps:
            raise SpecValidationError([f"active sequence is {before.width}x{before.height}@{before.fps}, spec wants "
                                       f"{spec.width}x{spec.height}@{spec.fps}"])
        have = {t.name for t in before.tracks}
        absent = [t for t in spec.tracks if t not in have]
        if absent:
            raise SpecValidationError([f"active sequence lacks track(s) {absent}"])
        if durations is None:
            proj = parse_project(self._tool("project_inspect").payload)
            durations = {p: proj.item(media_map.item_for(p)).duration_seconds  # type: ignore[union-attr]
                         for p in spec.asset_paths if proj.item(media_map.item_for(p)) is not None}
        errors, handle_issues = check_media_bounds(spec, durations)
        report = ApplyReport(clip_map=ClipMap())
        if handle_issues:
            if self.handle_policy == "error":
                errors += [f"{h.clip_instance_id}: dissolve lacks source handle - {h.message}" for h in handle_issues]
            else:
                ids = [h.clip_instance_id for h in handle_issues]
                spec = downgrade_dissolves(spec, ids)
                report.downgraded_dissolves = ids
                report.warnings += [f"{h.clip_instance_id}: dissolve downgraded to a cut - {h.message}" for h in handle_issues]
        if errors:
            raise SpecValidationError(errors)
        report.effective_spec = spec
        tb = spec.timebase

        order = sorted(spec.placements, key=lambda p: (parse_track(p.track)[0] != "V", parse_track(p.track)[1], p.timeline_start_frame))
        for p in order:
            ref = self._place(p, tb, media_map, report)
            report.clip_map.refs[p.clip_instance_id] = ref
            report.placed += 1
        for p in order:
            if p.audio_gain_db is not None:
                ref = report.clip_map[p.clip_instance_id]
                target = ref.audio_clip if p.media_kind == "video" else ref.clip
                if target is None:
                    raise CommandError("clip.audioGain", f"{p.clip_instance_id}: gain requested but FilmCraft created no audio clip")
                self._cmd("clip.audioGain", {"clips": [target], "mode": "set", "db": float(p.audio_gain_db)})
        self._apply_framing_and_ducking(order, report, tb)
        for p in order:
            if p.transition_in.type == "dissolve":
                res = self._cmd("sequence.applyVideoTransition", {
                    "clip": report.clip_map[p.clip_instance_id].clip, "edge": "in", "effect": "cross_dissolve",
                    "frames": p.transition_in.frames})
                if isinstance(res, dict) and "transition" in res:
                    report.clip_map.transitions[p.clip_instance_id] = res["transition"]
        for t in spec.titles:
            res = self._cmd("graphics.newText", {
                "text": t.text, "seconds": float(tb.frames_to_seconds(t.duration_frames)), "time": tb.frames_to_ticks(t.start_frame),
                "size": t.size or max(12, spec.height // 13),
                "position": list(t.position) if t.position else [spec.width // 10, spec.height * 5 // 6],
                "track": parse_track(t.track)[1] - 1})
            if isinstance(res, dict) and "clip" in res:
                report.clip_map.titles.append(res["clip"])
        for m in spec.markers:
            params: dict[str, Any] = {"time": tb.frames_to_ticks(m.frame), "name": m.name}
            if m.comment:
                params["comment"] = m.comment
            res = self._cmd("markers.add", params)
            if isinstance(res, dict) and "marker" in res:
                report.clip_map.markers.append(res["marker"])
        self.spec = spec
        return report

    def _require_capability(self, name: str, why: str) -> None:
        caps = self.discover_capabilities()
        if not caps.has(name):
            raise CapabilityError(f"{why} needs the FilmCraft command behind capability '{name}' ({caps.capabilities[name].id}), which this "
                                  "FilmCraft build does not offer; turn the matching option off in the config", [name])

    def _apply_framing_and_ducking(self, order: Sequence[Placement], report: ApplyReport, tb: Timebase) -> None:
        fit = [p for p in order if p.fit_source_size is not None]
        if fit:
            self._require_capability("fit_to_frame", "scaling mixed-aspect clips to the frame (filmcraft.fit_mixed_aspect)")
            res = self._cmd("clip.fitToFrame", {"clips": [report.clip_map[p.clip_instance_id].clip for p in fit]})
            # FilmCraft answers with the scale it chose; it is verified again after save and reopen (verify_against_spec)
            report.framing = {int(r["clip"]): float(r["scale"]) for r in (res.get("clips") if isinstance(res, dict) else None) or []
                              if isinstance(r, dict) and "clip" in r and "scale" in r}
        keyed = [p for p in order if p.gain_keys]
        if keyed:
            self._require_capability("clip_keyframe", "ducking music (filmcraft.duck_music)")
        for p in keyed:
            clip = report.clip_map[p.clip_instance_id].clip
            for k in p.gain_keys:
                self._cmd("effects.setParam", {"clip": clip, "effect": "volume", "param": "level", "value": float(k.db),
                                               "time": tb.frames_to_ticks(k.frame), "keyframe": True})

    def _place(self, p: Placement, tb: Timebase, media_map: MediaMap, report: ApplyReport) -> ClipRef:
        params: dict[str, Any] = {
            "item": media_map.item_for(p.source_path), "track": p.track, "frame": p.timeline_start_frame,
            "duration": tb.frames_to_ticks(p.duration_frames)}
        if p.media_kind in ("video", "audio") and p.source_in_seconds:
            params["sourceIn"] = tb.seconds_to_ticks(p.source_in_seconds)
        if p.media_kind == "video":
            params["audio"] = bool(p.include_linked_audio)
            if p.include_linked_audio:
                params["audioTrack"] = p.audio_track
        res = self._cmd("timeline.place", params)
        clips = res.get("clips") if isinstance(res, dict) else None
        if not clips or not all(isinstance(c, int) for c in clips):
            raise CommandError("timeline.place", f"{p.clip_instance_id}: unexpected result {res!r}")
        if p.media_kind == "video" and p.include_linked_audio:
            if len(clips) >= 2:
                return ClipRef(p.clip_instance_id, p.track, clips[0], clips[1], p.audio_track)
            report.warnings.append(f"{p.clip_instance_id}: FilmCraft created no audio clip (source has no audio?)")
        return ClipRef(p.clip_instance_id, p.track, clips[0], None, None)

    def verify(self, spec: TimelineSpec, media_map: MediaMap | None = None) -> VerificationReport:
        """Read back the sequence + media state and compare with ``spec``."""
        return verify_against_spec(self.inspect_sequence(), spec, project=self.inspect_project(), media_map=media_map)

    # ------------------------------------------------------------------ build (versioned, idempotent)
    def build_project(self, spec: TimelineSpec, project_name: str | None = None, *, force_new_version: bool = False):
        """Idempotent versioned rebuild; see ``execution.run_build``. Returns a ``BuildResult``."""
        from trip_vlog.filmcraft.execution import run_build

        return run_build(self, spec, project_name or self.config.project_name, force_new_version=force_new_version)

    # ------------------------------------------------------------------ preview
    def _ensure_open(self, project_path: Path | str | None = None) -> None:
        if self.is_open and project_path is None:
            return
        path = project_path or self.project_path
        if path is None:
            from trip_vlog.filmcraft.execution import latest_complete_project

            path = latest_complete_project(self)
        if path is None:
            raise FilmCraftError("no built FilmCraft project to open; run build_project first")
        if self.is_open and self.project_path and canon_path(self.project_path) == canon_path(Path(path)):
            return
        self.open_project(path)

    def render_preview(self, output: Path | str | None = None, *, project_path: Path | str | None = None,
                       scale: float = 1.0, timeout: float | None = None) -> RenderReport:
        """Render ``preview/draft.mp4`` through ``file.exportMedia`` (wait:true) and verify it with ffprobe."""
        self._ensure_open(project_path)
        out = self.policy.check_write(output if output is not None else self.workspace / self.preview_file, "preview")
        if out.suffix.lower() != ".mp4":
            raise ParameterError(f"preview path must end in .mp4: {out.name}")
        if not (0 < scale <= 1):
            raise ParameterError("scale must be in (0, 1]")
        out.parent.mkdir(parents=True, exist_ok=True)
        snap = self.inspect_sequence()
        if snap.clip_count == 0:
            raise FilmCraftError("the sequence is empty; nothing to render")
        seconds = float(Fraction(snap.duration_frames) / snap.fps)
        params: dict[str, Any] = {"path": str(out), "format": "h264", "wait": True}
        if scale != 1.0:
            params["scale"] = scale
        if self.sequence_id is not None:
            params["sequence"] = self.sequence_id
        t0 = time.monotonic()
        res = self._cmd("file.exportMedia", params, timeout if timeout is not None else max(300.0, 60.0 + 12.0 * seconds))
        took = time.monotonic() - t0
        engine = res if isinstance(res, dict) else {}
        offline = [str(x) for x in (engine.get("offlineMedia") or [])]
        if not out.is_file() or out.stat().st_size == 0:
            raise CommandError("file.exportMedia", f"FilmCraft reported success but {out} was not written")
        probe = run_ffprobe(out, self.ffprobe_path)
        has_audio = any(t.kind == "audio" and t.clips for t in snap.tracks)
        rep = evaluate_preview(probe, path=str(out), width=snap.width, height=snap.height, fps=snap.fps,
                               frames=snap.duration_frames, expect_audio=has_audio, scale=scale)
        rep.render_seconds = round(took, 2)
        rep.offline_media = offline
        rep.engine_result = {k: v for k, v in engine.items() if k in ("job", "result")}
        if offline:
            from trip_vlog.filmcraft.preview import Check

            rep.checks.append(Check("no offline media in render", False, f"offline: {offline[:3]}"))
            rep.ok = False
        return rep

    def read_back(self, project_path: Path | str | None = None) -> tuple[SequenceSnapshot, ProjectSnapshot]:
        """The sequence and project as FilmCraft reports them from a freshly opened process (after save and reopen)."""
        path = project_path or self.project_path
        if path is None:
            from trip_vlog.filmcraft.execution import latest_complete_project

            path = latest_complete_project(self)
        if path is None:
            raise FilmCraftError("no built FilmCraft project to open; run build_project first")
        self.close()
        project = self.open_project(path)
        return self.inspect_sequence(), project

    def render_music_bed(self, output: Path | str, music_tracks: Sequence[str], *, project_path: Path | str | None = None,
                         timeout: float | None = None) -> Path:
        """Render the audio of the music track(s) alone to a WAV, to measure the ducking in what FilmCraft really renders.

        The saved project is opened in a NEW server process, every other audio track is muted in that process only, and the
        process is closed without saving: the project file is never written (the caller checks its hash), and the mute is gone with it.
        """
        self._require_capability("mute_track", "rendering the music bed alone")
        out = self.policy.check_write(output, "music_bed")
        if out.suffix.lower() != ".wav":
            raise ParameterError(f"music bed path must end in .wav: {out.name}")
        path = project_path or self.project_path
        if path is None:
            from trip_vlog.filmcraft.execution import latest_complete_project

            path = latest_complete_project(self)
        if path is None:
            raise FilmCraftError("no built FilmCraft project to open; run build_project first")
        out.parent.mkdir(parents=True, exist_ok=True)
        self.close()
        try:
            self.open_project(path)
            snap = self.inspect_sequence()
            keep = set(music_tracks)
            for t in snap.tracks:
                if t.kind == "audio" and t.name not in keep:
                    self._cmd("mixer.setStrip", {"strip": t.name, "muted": True})
            params: dict[str, Any] = {"path": str(out), "format": "wav", "wait": True}
            if self.sequence_id is not None:
                params["sequence"] = self.sequence_id
            seconds = float(Fraction(snap.duration_frames) / snap.fps)
            self._cmd("file.exportMedia", params, timeout if timeout is not None else max(300.0, 60.0 + 6.0 * seconds))
        finally:
            self.close()      # no save: the muted strips never reach the project file
        if not out.is_file() or out.stat().st_size == 0:
            raise CommandError("file.exportMedia", f"FilmCraft reported success but {out} was not written")
        return out

    # ------------------------------------------------------------------ interchange
    def export_interchange(self, formats: Sequence[str] | None = None, out_dir: Path | str | None = None, *,
                           spec: TimelineSpec | None = None, project_path: Path | str | None = None) -> HandoffReport:
        """Export OTIO (primary) and FCP7 XML (fallback) by default into ``resolve_handoff/``.

        Relative media references are rewritten to absolute ``file://`` URLs, the file is reloaded and validated
        against the timeline read back from FilmCraft, and the loss list per format is reported.
        """
        self._ensure_open(project_path)
        names = list(formats) if formats is not None else list(self.config.interchange_formats)
        out = self.policy.check_write(out_dir if out_dir is not None else self.workspace / self.interchange_dir, "out_dir")
        out.mkdir(parents=True, exist_ok=True)
        infos = []
        for n in names:
            info = resolve_format(n)
            if info not in infos:
                infos.append(info)
        snap = self.inspect_sequence()
        project = self.inspect_project()
        known = {canon_path(m.path): m.path for m in project.media if m.path}
        report = HandoffReport(out_dir=str(out), titles_not_carried=titles_not_carried(spec or self.spec),
                               limitations=list(GLOBAL_LIMITS), filmcraft_version=self.discover_capabilities().version)
        for info in infos:
            target = self.policy.check_write(out / info.filename, "interchange")
            rep = FormatReport(format=info.key, fc_format=info.fc_format, path=str(target), role=info.role, loses=list(info.loses))
            report.formats.append(rep)
            params: dict[str, Any] = {"format": info.fc_format, "path": str(target)}
            if self.sequence_id is not None:
                params["sequence"] = self.sequence_id
            try:
                res = self._cmd("file.exportInterchange", params)
            except CommandError as e:
                rep.error = str(e)
                continue
            if not target.is_file() or target.stat().st_size == 0:
                rep.error = f"FilmCraft reported success but {target.name} is missing or empty"
                continue
            rep.exported = True
            if isinstance(res, dict) and isinstance(res.get("report"), list):
                rep.engine_report = [str(x) for x in res["report"]]
            postprocess_and_validate(info, target, snap, known, rep)
        return report
