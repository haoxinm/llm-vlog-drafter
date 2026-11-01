"""EditPlan -> TimelineSpec (DESIGN §13.1). Pure: no FilmCraft, no filesystem access beyond building paths.

Track layout (names are the adapter's ``V1..Vn`` / ``A1..An``):

    primary_video  -> V1 (video and stills)      natural audio of a V1 video clip -> A1 (linked)
    overlay        -> V2 (overlay video is silent: its linked audio is never placed, A2 belongs to music)
    natural_audio  -> A1 (stand-alone audio clip)
    music          -> A2
    narration      -> A3 (only created when a narration clip exists)
    titles         -> V2 (``graphics.newText`` through the adapter; frames come straight from the plan, size and position are
                      the TitleSpec defaults: a lower-left text sized from the sequence height)

Audio policy: ``muted`` -> no linked audio; ``natural_sound`` -> linked audio at unity gain;
``ambient_low`` -> linked audio with a constant gain of ``ambient_gain_db`` (default -12 dB). The gain is a
single constant per clip; no ducking or automation is generated (FilmCraft keyframing is not relied on).

Transitions: the adapter places ``cross_dissolve`` centred on the cut (``sequence.applyVideoTransition``). Plans may
ask for ``start_at_cut`` / ``end_at_cut`` alignment (the resolver chooses those when a centred dissolve lacks source
handles). The adapter cannot place those, so they are mapped to a hard cut and a ``MappingWarning`` is recorded;
nothing is downgraded silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from trip_vlog.filmcraft.timeline_spec import MarkerSpec, Placement, TimelineSpec, TitleSpec, TransitionIn, validate_spec
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.duration import frames_to_float, parse_fps
from trip_vlog.planning.models import EditClip, EditPlan

AMBIENT_LOW_GAIN_DB = -12.0
SUPPORTED_ALIGNMENTS = ("center",)
VIDEO_TRACKS = {"primary_video": "V1", "overlay": "V2"}
AUDIO_TRACKS = {"natural_audio": "A1", "music": "A2", "narration": "A3"}
BASE_TRACKS = ("V1", "V2", "A1", "A2")


class MappingError(ValueError):
    """The plan cannot be expressed as a timeline spec."""

    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


@dataclass(frozen=True)
class MappingSettings:
    sequence_name: str = "Main Draft"
    ambient_gain_db: float = AMBIENT_LOW_GAIN_DB
    supported_alignments: tuple[str, ...] = SUPPORTED_ALIGNMENTS


@dataclass(frozen=True)
class MappingWarning:
    code: str
    clip_instance_id: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "clip_instance_id": self.clip_instance_id, "message": self.message}


@dataclass
class MappingResult:
    spec: TimelineSpec
    warnings: list[MappingWarning] = field(default_factory=list)
    used_asset_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"warnings": [w.to_dict() for w in self.warnings], "used_asset_ids": self.used_asset_ids,
                "spec_hash": self.spec.spec_hash()}


def source_path_for(media_root: Path | str, relative_path: str) -> str:
    """Absolute source path of a catalog asset (relative path is POSIX, resolved under the media root)."""
    return str(Path(media_root).joinpath(*relative_path.split("/")))


def _seconds(frame: int | None, fallback: float | None, fps: Fraction) -> float:
    if frame is not None:
        return frames_to_float(frame, fps)
    return float(fallback or 0.0)


def edit_plan_to_spec(plan: EditPlan, catalog: PlanningCatalog, media_root: Path | str,
                      settings: MappingSettings | None = None) -> MappingResult:
    """Map a validated edit plan to a TimelineSpec. Raises MappingError for anything that cannot be mapped."""
    st = settings or MappingSettings()
    fps = parse_fps(plan.sequence.fps)
    warnings: list[MappingWarning] = []
    placements: list[Placement] = []
    errors: list[str] = []
    tracks = set(BASE_TRACKS)
    used: list[str] = []

    for scene in plan.scenes:
        for c in scene.clips:
            asset = catalog.assets.get(c.asset_id)
            if asset is None:
                errors.append(f"{c.clip_instance_id}: asset {c.asset_id} is not in the catalog")
                continue
            if c.asset_id not in used:
                used.append(c.asset_id)
            path = source_path_for(media_root, asset.relative_path)
            tin = _transition(c, fps, st, warnings)
            if c.media_type in ("video", "image"):
                track = VIDEO_TRACKS.get(c.track_role)
                if track is None:
                    errors.append(f"{c.clip_instance_id}: {c.media_type} cannot use track role {c.track_role}")
                    continue
                if c.media_type == "image":
                    placements.append(Placement(c.clip_instance_id, path, "image", track, c.timeline_start_frame, c.duration_frames,
                                                0.0, None, False, None, tin))
                    continue
                linked = track == "V1" and c.audio_policy != "muted" and asset.has_audio
                if c.audio_policy != "muted" and not asset.has_audio:
                    warnings.append(MappingWarning("no_source_audio", c.clip_instance_id,
                                                   "audio requested but the source has no audio stream; placed video only"))
                elif c.audio_policy != "muted" and track != "V1":
                    warnings.append(MappingWarning("overlay_audio_dropped", c.clip_instance_id,
                                                   "overlay clips are placed without their own audio"))
                gain = st.ambient_gain_db if (linked and c.audio_policy == "ambient_low") else None
                placements.append(Placement(
                    c.clip_instance_id, path, "video", track, c.timeline_start_frame, c.duration_frames,
                    _seconds(c.source_in_frame, c.source_in_seconds, fps), _seconds(c.source_out_frame, c.source_out_seconds, fps),
                    linked, gain, tin))
            else:  # audio
                track = AUDIO_TRACKS.get(c.track_role)
                if track is None:
                    errors.append(f"{c.clip_instance_id}: audio cannot use track role {c.track_role}")
                    continue
                tracks.add(track)
                gain = st.ambient_gain_db if c.audio_policy == "ambient_low" else None
                placements.append(Placement(
                    c.clip_instance_id, path, "audio", track, c.timeline_start_frame, c.duration_frames,
                    _seconds(c.source_in_frame, c.source_in_seconds, fps), _seconds(c.source_out_frame, c.source_out_seconds, fps),
                    False, gain, TransitionIn()))
    if errors:
        raise MappingError(errors)
    if "A3" in tracks:
        tracks.add("A2")
    markers = tuple(MarkerSpec(m.frame, m.name, m.scene_id) for m in plan.markers)
    titles = tuple(TitleSpec(t.text, t.start_frame, t.duration_frames, t.track) for t in plan.titles)  # size/position: the TitleSpec defaults
    spec = TimelineSpec(st.sequence_name, plan.sequence.width, plan.sequence.height, fps,
                        tuple(sorted(tracks, key=lambda t: (t[0] != "V", int(t[1:])))), tuple(placements), markers, titles)
    issues = validate_spec(spec)
    if issues:
        raise MappingError(issues)
    return MappingResult(spec, warnings, used)


def _transition(c: EditClip, fps: Fraction, st: MappingSettings, warnings: list[MappingWarning]) -> TransitionIn:
    t = c.transition_in
    if t.type == "cut":
        return TransitionIn()
    if t.alignment not in st.supported_alignments:
        warnings.append(MappingWarning(
            "dissolve_alignment_unsupported", c.clip_instance_id,
            f"the plan asks for a {t.duration_frames}-frame dissolve aligned '{t.alignment}', which the FilmCraft adapter cannot "
            f"place (only {', '.join(st.supported_alignments)}); placed as a hard cut instead"))
        return TransitionIn()
    return TransitionIn("dissolve", t.duration_frames)
