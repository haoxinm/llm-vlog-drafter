"""Selection plan + catalog -> exact edit plan (DESIGN §8.3, §13.2).

Deterministic: the LLM (or baseline planner) says which moment, how long and where in the moment; this module computes
exact frame-rounded source ranges with rational arithmetic, clamps to moment and asset bounds, lays clips out from integer
frame counts, and records every clamp, shortfall and transition change in `resolver_notes`.

Placement rules
  * primary_video clips are laid end to end across scenes (V1); a transition never changes their length.
  * every other role (overlay, natural_audio, music, narration) is laid end to end on its own track, starting at the
    start of the scene it is listed in.
Titles (V2) are placed from their start rule (scene start, clip start or the end of the sequence) and moved inside the sequence
when needed (`planning.titles.resolve_titles`).
Transitions are center-aligned dissolves when both neighbours have enough source handle (>= half the transition each side,
docs/phase0-filmcraft-spike.md §7 gap 4). Otherwise the alignment shifts (start_at_cut / end_at_cut), the dissolve is
shortened, or it becomes a cut. Each change is noted on the clip; nothing is downgraded silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from trip_vlog.planning.catalog_view import AssetInfo, MomentInfo, PlanningCatalog
from trip_vlog.planning.duration import (cumulative_starts, fps_string, frames_to_float, parse_fps, seconds_to_frames,
                                         sequence_dimensions)
from trip_vlog.planning.models import (SCHEMA_VERSION, AudioSettings, EditClip, EditPlan, EditScene, EditTransition,
                                       ProjectSettings, SceneMarker, SelectionClip, SelectionPlan, SequenceSettings,
                                       selection_plan_hash, with_hash)
from trip_vlog.planning.titles import resolve_titles

DEFAULT_STILL_SECONDS = 3.0
MIN_DISSOLVE_FRAMES = 2


class PlanResolutionError(ValueError):
    """The selection plan cannot be resolved (unknown IDs, moment shorter than a frame...). `issues` lists every problem."""

    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


@dataclass(frozen=True)
class PlanSettings:
    title: str = "Trip Vlog"
    target_duration_seconds: float = 180.0
    aspect_ratio: str = "16:9"
    frame_rate: str = "30"
    music_policy: str = "user_supplied_only"
    preserve_natural_audio: bool = True
    duck_music_under_speech: bool = True
    still_duration_seconds: float = DEFAULT_STILL_SECONDS
    include_narration: bool = False
    short_side: int = 1080

    @classmethod
    def from_config(cls, config: Any, still_duration_seconds: float | None = None) -> "PlanSettings":
        p = config.project
        return cls(title=p.title, target_duration_seconds=float(p.target_duration_seconds), aspect_ratio=p.aspect_ratio,
                   frame_rate=p.frame_rate, music_policy=p.music_policy, preserve_natural_audio=p.preserve_natural_audio,
                   include_narration=p.include_narration,
                   still_duration_seconds=float(getattr(config.media, "still_duration_seconds", None) or still_duration_seconds
                                                or DEFAULT_STILL_SECONDS))


def effective_plan_settings(config: Any) -> PlanSettings:
    """`PlanSettings.from_config` with the target duration accepted revisions moved (see `Config.set_revision_overrides`): the plan a revision
    makes records the target it was made for. Planning itself always uses `from_config`."""
    from dataclasses import replace

    return replace(PlanSettings.from_config(config), target_duration_seconds=float(getattr(config, "effective_target_seconds", config.project.target_duration_seconds)))


@dataclass
class _Resolved:
    clip: SelectionClip
    asset: AssetInfo
    moment: MomentInfo
    in_frame: int | None   # None for stills
    out_frame: int | None
    duration: int
    notes: list[str]


def _fmt(frames: int, fps: Fraction) -> str:
    return f"{frames_to_float(frames, fps):.3f}s ({frames}f)"


def _asset_frames(asset: AssetInfo, fps: Fraction) -> int | None:
    if asset.duration_seconds is None:
        return None
    return seconds_to_frames(asset.duration_seconds, fps, "floor")


def _clip_range(clip: SelectionClip, asset: AssetInfo, moment: MomentInfo, fps: Fraction) -> tuple[int, int, list[str]]:
    """(in_frame, out_frame, notes) for a video/audio clip inside its moment, in sequence frames."""
    notes: list[str] = []
    m_in = max(0, seconds_to_frames(moment.source_in_seconds or 0.0, fps, "ceil"))
    m_out = seconds_to_frames(moment.source_out_seconds or 0.0, fps, "floor")
    a_frames = _asset_frames(asset, fps)
    if a_frames is not None and m_out > a_frames:
        notes.append(f"moment end clamped to asset duration ({_fmt(m_out, fps)} -> {_fmt(a_frames, fps)})")
        m_out = a_frames
    length = m_out - m_in
    if length < 1:
        raise PlanResolutionError([f"{clip.clip_instance_id}: moment {moment.moment_id} is shorter than one frame at {fps_string(fps)} fps"])
    req = max(1, seconds_to_frames(clip.target_duration_seconds, fps))
    use = min(req, length)
    if req > length:
        notes.append(f"shortfall: requested {_fmt(req, fps)} but moment {moment.moment_id} is only {_fmt(length, fps)}; "
                     f"used the whole moment (short by {_fmt(req - length, fps)})")
    anchor = clip.anchor
    if anchor == "best":
        b_in = max(m_in, seconds_to_frames(moment.best_in_seconds, fps, "ceil")) if moment.best_in_seconds is not None else None
        b_out = min(m_out, seconds_to_frames(moment.best_out_seconds, fps, "floor")) if moment.best_out_seconds is not None else None
        if b_in is not None and b_out is not None and b_out > b_in:
            sub = b_out - b_in
            start = b_in + (sub - use) // 2 if sub >= use else b_in + sub // 2 - use // 2
            start = min(max(start, m_in), m_out - use)
            if sub < use:
                notes.append(f"anchor=best: best sub-range ({_fmt(sub, fps)}) shorter than the clip; centered on it within the moment")
        else:
            notes.append("anchor=best: moment has no best sub-range; used the center of the moment")
            start = m_in + (length - use) // 2
    elif anchor == "start":
        start = m_in
    elif anchor == "end":
        start = m_out - use
    else:
        start = m_in + (length - use) // 2
    return start, start + use, notes


def _handles(r: _Resolved, fps: Fraction) -> tuple[int | None, int | None]:
    """Source frames available before the in point and after the out point (None = unlimited, i.e. a still)."""
    if r.in_frame is None or r.out_frame is None:
        return None, None
    a_frames = _asset_frames(r.asset, fps)
    after = None if a_frames is None else max(0, a_frames - r.out_frame)
    return r.in_frame, after


def _plan_transition(r: _Resolved, prev: _Resolved | None, fps: Fraction) -> EditTransition:
    cut = EditTransition(type="cut", duration_seconds=0.0, duration_frames=0, alignment="center")
    t = r.clip.transition_in
    if t.type == "cut":
        return cut
    if r.clip.track_role != "primary_video":
        r.notes.append("dissolve requested on a non-primary track; used a cut (transitions are only supported on the primary video track)")
        return cut
    if prev is None:
        r.notes.append("dissolve requested on the first clip of the timeline; used a cut (nothing to dissolve from)")
        return cut
    want = seconds_to_frames(t.duration_seconds, fps)
    if want < MIN_DISSOLVE_FRAMES:
        r.notes.append(f"dissolve of {want} frame(s) is not meaningful at {fps_string(fps)} fps; used a cut")
        return cut
    d = min(want, prev.duration, r.duration)
    if d < want:
        r.notes.append(f"dissolve shortened from {_fmt(want, fps)} to {_fmt(d, fps)} to fit the adjacent clip durations")
    if d < MIN_DISSOLVE_FRAMES:
        r.notes.append("dissolve became shorter than 2 frames after fitting adjacent clips; used a cut")
        return cut
    _, after_prev = _handles(prev, fps)
    before_cur, _ = _handles(r, fps)
    inf = 10**12
    ap, bc = (inf if after_prev is None else after_prev), (inf if before_cur is None else before_cur)
    half = (d + 1) // 2
    if ap >= half and bc >= half:
        return EditTransition(type="dissolve", duration_seconds=frames_to_float(d, fps), duration_frames=d, alignment="center")
    for align, ok in (("start_at_cut", ap >= d), ("end_at_cut", bc >= d)):
        if ok:
            r.notes.append(f"center-aligned dissolve needs {half}f of source handle on both sides (outgoing has "
                           f"{ap if ap < inf else 'unlimited'}f, incoming has {bc if bc < inf else 'unlimited'}f); alignment changed to {align}")
            return EditTransition(type="dissolve", duration_seconds=frames_to_float(d, fps), duration_frames=d, alignment=align)  # type: ignore[arg-type]
    shrunk = 2 * min(ap, bc)
    if shrunk >= MIN_DISSOLVE_FRAMES:
        shrunk = min(shrunk, d)
        r.notes.append(f"insufficient source handles for a {_fmt(d, fps)} dissolve (outgoing {ap}f, incoming {bc}f); shortened to {_fmt(shrunk, fps)}")
        return EditTransition(type="dissolve", duration_seconds=frames_to_float(shrunk, fps), duration_frames=shrunk, alignment="center")
    r.notes.append(f"insufficient source handles for a dissolve (outgoing {ap}f, incoming {bc}f); used a cut")
    return cut


def resolve_selection_plan(selection: SelectionPlan, catalog: PlanningCatalog, settings: PlanSettings) -> EditPlan:
    """Resolve a (reference-valid) selection plan into an EditPlan. Raises PlanResolutionError on unresolvable clips."""
    fps = parse_fps(settings.frame_rate)
    issues: list[str] = []
    resolved: list[list[_Resolved]] = []
    for scene in selection.scenes:
        row: list[_Resolved] = []
        for clip in scene.clips:
            moment = catalog.moments.get(clip.moment_id)
            if moment is None:
                issues.append(f"{clip.clip_instance_id}: unknown moment_id {clip.moment_id!r}")
                continue
            asset = catalog.assets.get(moment.asset_id)
            if asset is None:
                issues.append(f"{clip.clip_instance_id}: moment {clip.moment_id} references unknown asset {moment.asset_id!r}")
                continue
            try:
                if asset.media_type == "image":
                    dur = max(1, seconds_to_frames(clip.target_duration_seconds, fps))
                    row.append(_Resolved(clip, asset, moment, None, None, dur, []))
                else:
                    if moment.source_in_seconds is None or moment.source_out_seconds is None:
                        raise PlanResolutionError([f"{clip.clip_instance_id}: moment {moment.moment_id} has no time range"])
                    a, b, notes = _clip_range(clip, asset, moment, fps)
                    row.append(_Resolved(clip, asset, moment, a, b, b - a, notes))
            except PlanResolutionError as e:
                issues.extend(e.issues)
        resolved.append(row)
    primary_count = sum(1 for row in resolved for r in row if r.clip.track_role == "primary_video")
    if primary_count == 0 and not issues:
        issues.append("selection plan has no primary_video clips")
    if issues:
        raise PlanResolutionError(issues)

    # transitions: need the previous primary clip across scene boundaries
    transitions: dict[str, EditTransition] = {}
    prev: _Resolved | None = None
    for row in resolved:
        for r in row:
            transitions[r.clip.clip_instance_id] = _plan_transition(r, prev if r.clip.track_role == "primary_video" else None, fps)
            if r.clip.track_role == "primary_video":
                prev = r

    # layout from integer frame counts
    primary_durations = [r.duration for row in resolved for r in row if r.clip.track_role == "primary_video"]
    primary_starts = iter(cumulative_starts(primary_durations))
    cursor_end = 0
    scenes: list[EditScene] = []
    markers: list[SceneMarker] = []
    shortfalls = 0
    for scene, row in zip(selection.scenes, resolved):
        scene_start = cursor_end
        role_cursor: dict[str, int] = {}
        clips: list[EditClip] = []
        scene_primary = 0
        for r in row:
            role = r.clip.track_role
            if role == "primary_video":
                start = next(primary_starts)
                scene_primary += r.duration
                cursor_end = start + r.duration
            else:
                start = role_cursor.get(role, scene_start)
                role_cursor[role] = start + r.duration
            if any(n.startswith("shortfall") for n in r.notes):
                shortfalls += 1
            c = r.clip
            clips.append(EditClip(
                clip_instance_id=c.clip_instance_id, asset_id=r.asset.asset_id, moment_id=r.moment.moment_id,
                media_type=r.asset.media_type if r.asset.media_type in ("video", "image", "audio") else "video",  # type: ignore[arg-type]
                anchor=c.anchor, track_role=role, audio_policy=c.audio_policy,
                requested_duration_seconds=c.target_duration_seconds,
                source_in_seconds=None if r.in_frame is None else frames_to_float(r.in_frame, fps),
                source_out_seconds=None if r.out_frame is None else frames_to_float(r.out_frame, fps),
                source_in_frame=r.in_frame, source_out_frame=r.out_frame,
                timeline_start_frame=start, duration_frames=r.duration, transition_in=transitions[c.clip_instance_id],
                resolver_notes=r.notes, rationale=c.rationale, intentional_repeat_reason=c.intentional_repeat_reason))
        scenes.append(EditScene(scene_id=scene.scene_id, title=scene.title, purpose=scene.purpose,
                                target_duration_seconds=scene.target_duration_seconds, start_frame=scene_start,
                                duration_frames=scene_primary, clips=clips))
        markers.append(SceneMarker(marker_id=f"marker_{scene.scene_id}", name=scene.title, frame=scene_start, scene_id=scene.scene_id))
    total = cursor_end
    titles, title_issues = resolve_titles(selection, scenes, fps, total)
    if title_issues:
        raise PlanResolutionError(title_issues)
    width, height = sequence_dimensions(settings.aspect_ratio, settings.short_side)
    uncertainties = list(selection.uncertainties)
    if shortfalls:
        uncertainties.append(f"{shortfalls} clip(s) are shorter than requested because their moment is short (see resolver_notes)")
    plan = EditPlan(
        schema_version=SCHEMA_VERSION, plan_hash="0" * 64, selection_plan_hash=selection_plan_hash(selection),
        project=ProjectSettings(title=settings.title, target_duration_seconds=settings.target_duration_seconds,
                                aspect_ratio=settings.aspect_ratio, frame_rate=settings.frame_rate),
        sequence=SequenceSettings(width=width, height=height, fps=fps_string(fps), aspect_ratio=settings.aspect_ratio,
                                  still_duration_seconds=settings.still_duration_seconds),
        scenes=scenes, markers=markers, titles=titles,
        audio=AudioSettings(music_policy=settings.music_policy, preserve_natural_audio=settings.preserve_natural_audio,  # type: ignore[arg-type]
                            duck_music_under_speech=settings.duck_music_under_speech),
        total_duration_frames=total, total_duration_seconds=frames_to_float(total, fps), uncertainties=uncertainties)
    return with_hash(plan)
