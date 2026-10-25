"""Deterministic plan validation (DESIGN §11, §19.3 item 3): selection plans and resolved edit plans.

`validate_selection_plan` checks what the planner may get wrong (IDs, feasibility, constraints) before resolution.
`validate_edit_plan` checks the resolved plan (time correctness, frame tolerance, durations, transitions and handles,
duplicates, orientation). Both return a typed `ValidationReport` (errors / warnings / repairs).

Repairs (edit plans only, `settings.repair`) are applied only when unambiguous, each is recorded, and a plan with any
error is never "fixed up" beyond those repairs:
  * source range not on a frame boundary -> rounded to the nearest frame
  * source_out a couple of frames past the end of the asset -> clamped to the asset's last full frame
  * duration/frame fields inconsistent with the source range by at most one frame -> recomputed from the range
  * timeline positions, scene spans, markers and totals that disagree with the frame counts -> recomputed
  * cut with a non-zero duration, or dissolve on a non-primary track / first clip -> cut
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping

from pydantic import ValidationError

from trip_vlog.planning.catalog_view import AssetInfo, MomentInfo, PlanningCatalog, ref_matches, ref_targets
from trip_vlog.planning.duration import (cumulative_starts, frame_distance, frames_to_float, parse_fps, seconds_to_frames)
from trip_vlog.planning.models import EditClip, EditPlan, EditTransition, SelectionPlan, edit_plan_hash

AUDIO_ROLES = ("natural_audio", "music", "narration")
VISUAL_ROLES = ("primary_video", "overlay")


@dataclass(frozen=True)
class ValidationSettings:
    frame_rate: str = "30"
    target_duration_seconds: float = 180.0
    duration_tolerance_pct: float = 5.0       # total duration may differ from target by max(pct%, seconds)
    duration_tolerance_seconds: float = 2.0
    scene_budget_tolerance_pct: float = 10.0
    scene_budget_tolerance_seconds: float = 1.0
    source_tolerance_seconds: float = 0.001   # accepted overshoot past the asset/moment end
    repair_max_overshoot_frames: int = 2      # larger overshoots are errors, not repairs
    must_include: tuple[str, ...] = ()
    must_exclude: tuple[str, ...] = ()
    music_policy: str = "user_supplied_only"
    include_narration: bool = False
    check_hash: bool = True
    repair: bool = True

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "ValidationSettings":
        p, e = config.project, config.editorial
        base = dict(frame_rate=p.frame_rate, target_duration_seconds=float(p.target_duration_seconds),
                    must_include=tuple(e.must_include), must_exclude=tuple(e.must_exclude),
                    music_policy=p.music_policy, include_narration=p.include_narration)
        for name in ("duration_tolerance_pct", "duration_tolerance_seconds"):
            if hasattr(config.project, name):
                base[name] = getattr(config.project, name)
        base.update(overrides)
        return cls(**base)

    def total_tolerance_seconds(self) -> float:
        return max(self.target_duration_seconds * self.duration_tolerance_pct / 100.0, self.duration_tolerance_seconds)


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    path: str = ""
    clip_instance_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in (("code", self.code), ("message", self.message), ("path", self.path or None),
                                  ("clip_instance_id", self.clip_instance_id)) if v is not None}


@dataclass(frozen=True)
class Repair:
    code: str
    message: str
    path: str = ""
    clip_instance_id: str | None = None
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message, "before": self.before, "after": self.after}
        if self.path:
            d["path"] = self.path
        if self.clip_instance_id:
            d["clip_instance_id"] = self.clip_instance_id
        return d


@dataclass
class ValidationReport:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    plan: Any = None  # the (possibly repaired) validated plan, when it parsed

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self, kind: str = "errors") -> list[str]:
        return [i.code for i in getattr(self, kind)]

    def error(self, code: str, message: str, path: str = "", clip: str | None = None) -> None:
        self.errors.append(Issue(code, message, path, clip))

    def warn(self, code: str, message: str, path: str = "", clip: str | None = None) -> None:
        self.warnings.append(Issue(code, message, path, clip))

    def repaired(self, code: str, message: str, before: Any, after: Any, path: str = "", clip: str | None = None) -> None:
        self.repairs.append(Repair(code, message, path, clip, before, after))

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": [i.to_dict() for i in self.errors], "warnings": [i.to_dict() for i in self.warnings],
                "repairs": [r.to_dict() for r in self.repairs]}

    def format(self) -> str:
        lines = [f"errors: {len(self.errors)}, warnings: {len(self.warnings)}, repairs: {len(self.repairs)}"]
        lines += [f"  ERROR   {i.code}: {i.message}" for i in self.errors]
        lines += [f"  WARNING {i.code}: {i.message}" for i in self.warnings]
        lines += [f"  REPAIR  {r.code}: {r.message}" for r in self.repairs]
        return "\n".join(lines)


def _schema_errors(report: ValidationReport, err: ValidationError) -> None:
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "<root>"
        report.error("schema", f"{loc}: {e['msg']}", loc)


def _check_refs(report: ValidationReport, clips: list[tuple[str, str]], catalog: PlanningCatalog, s: ValidationSettings) -> None:
    """clips: (clip_instance_id, moment_id). must_include / must_exclude references (§11.1)."""
    used = [(cid, catalog.moments[mid], catalog.assets.get(catalog.moments[mid].asset_id)) for cid, mid in clips if mid in catalog.moments]
    for ref in s.must_exclude:
        for cid, m, a in used:
            if a is not None and ref_matches(ref, a, m):
                report.error("excluded_included", f"clip {cid} uses {m.moment_id}, which matches must_exclude {ref!r}", clip=cid)
    used_ids = {m.moment_id for _, m, _ in used}
    for ref in s.must_include:
        targets = [m for m in ref_targets(ref, catalog) if catalog.assets[m.asset_id].present]
        if not targets:
            report.warn("must_include_unavailable", f"must_include {ref!r} matches no available moment")
        elif not any(m.moment_id in used_ids for m in targets):
            report.error("must_include_missing", f"must_include {ref!r} is not used by any clip")


# ---- selection plans -----------------------------------------------------------------------------------

def validate_selection_plan(plan: SelectionPlan | Mapping[str, Any], catalog: PlanningCatalog, settings: ValidationSettings) -> ValidationReport:
    report = ValidationReport()
    if not isinstance(plan, SelectionPlan):
        try:
            plan = SelectionPlan.model_validate(plan)
        except ValidationError as e:
            _schema_errors(report, e)
            return report
    report.plan = plan
    fps = parse_fps(settings.frame_rate)
    one_frame = float(1 / fps)
    seen_scenes: set[str] = set()
    seen_clips: set[str] = set()
    used_moments: dict[str, str] = {}
    clips: list[tuple[str, str]] = []
    total_primary = 0.0
    for si, scene in enumerate(plan.scenes):
        if scene.scene_id in seen_scenes:
            report.error("duplicate_scene_id", f"scene_id {scene.scene_id!r} is used more than once", f"scenes[{si}]")
        seen_scenes.add(scene.scene_id)
        scene_total = 0.0
        for ci, c in enumerate(scene.clips):
            path = f"scenes[{si}].clips[{ci}]"
            cid = c.clip_instance_id
            if cid in seen_clips:
                report.error("duplicate_clip_instance_id", f"clip_instance_id {cid!r} is used more than once", path, cid)
            seen_clips.add(cid)
            clips.append((cid, c.moment_id))
            if c.track_role == "primary_video":
                scene_total += c.target_duration_seconds
                total_primary += c.target_duration_seconds
            t = c.transition_in
            if t.type == "cut" and t.duration_seconds > 0:
                report.warn("transition_inconsistent", f"clip {cid}: a cut cannot have a duration ({t.duration_seconds}s); treated as a cut", path, cid)
            if t.type == "dissolve" and t.duration_seconds <= 0:
                report.warn("transition_inconsistent", f"clip {cid}: dissolve with zero duration; will be resolved as a cut", path, cid)
            m = catalog.moments.get(c.moment_id)
            if m is None:
                report.error("moment_not_found", f"clip {cid}: unknown moment_id {c.moment_id!r}", path, cid)
                continue
            a = catalog.assets.get(m.asset_id)
            if a is None:
                report.error("asset_not_found", f"clip {cid}: moment {m.moment_id} references unknown asset {m.asset_id!r}", path, cid)
                continue
            _check_asset_usable(report, a, path, cid)
            _check_role(report, c.track_role, a, settings, path, cid)
            length = m.length_seconds
            if a.media_type != "image" and length is not None:
                if length < one_frame - 1e-9:
                    report.error("moment_too_short", f"clip {cid}: moment {m.moment_id} is shorter than one frame", path, cid)
                elif c.target_duration_seconds > length + one_frame:
                    report.warn("duration_exceeds_moment", f"clip {cid}: target {c.target_duration_seconds:g}s exceeds moment length "
                                f"{length:.3f}s; the resolver will use the whole moment", path, cid)
            if m.moment_id in used_moments and not c.intentional_repeat_reason:
                report.error("unexplained_repeat", f"clip {cid} repeats moment {m.moment_id} already used by {used_moments[m.moment_id]} "
                             "without intentional_repeat_reason", path, cid)
            used_moments.setdefault(m.moment_id, cid)
        tol = max(scene.target_duration_seconds * settings.scene_budget_tolerance_pct / 100, settings.scene_budget_tolerance_seconds)
        if scene_total > scene.target_duration_seconds + tol:
            report.warn("scene_over_budget", f"scene {scene.scene_id}: clips total {scene_total:.2f}s, budget {scene.target_duration_seconds:g}s")
    if abs(total_primary - settings.target_duration_seconds) > settings.total_tolerance_seconds():
        report.warn("total_duration_off_target", f"primary clips total {total_primary:.2f}s; target {settings.target_duration_seconds:g}s "
                    f"(tolerance {settings.total_tolerance_seconds():.2f}s)")
    _check_refs(report, clips, catalog, settings)
    _check_duplicates_selection(report, plan, catalog)
    return report


def _check_asset_usable(report: ValidationReport, a: AssetInfo, path: str, cid: str | None) -> None:
    if not a.present:
        report.error("asset_missing", f"asset {a.asset_id} ({a.relative_path}) is no longer on disk", path, cid)
    elif not a.probe_ok:
        report.error("asset_unreadable", f"asset {a.asset_id} ({a.relative_path}) could not be probed", path, cid)
    if a.analysis_status == "stale":
        report.warn("asset_stale", f"asset {a.asset_id} changed since it was analysed; re-run indexing and segmentation", path, cid)


def _check_role(report: ValidationReport, role: str, a: AssetInfo, s: ValidationSettings, path: str, cid: str | None) -> None:
    if role in VISUAL_ROLES and a.media_type not in ("video", "image"):
        report.error("track_media_mismatch", f"clip {cid}: {role} needs a video or image, asset is {a.media_type}", path, cid)
    if role in AUDIO_ROLES and not a.has_audio:
        report.error("no_audio", f"clip {cid}: {role} needs audio but asset {a.asset_id} has none", path, cid)
    if role == "music" and s.music_policy == "none":
        report.error("music_not_allowed", f"clip {cid}: music clip but project.music_policy is 'none'", path, cid)
    if role == "narration" and not s.include_narration:
        report.error("narration_not_allowed", f"clip {cid}: narration clip but project.include_narration is false", path, cid)


def _check_duplicates_selection(report: ValidationReport, plan: SelectionPlan, catalog: PlanningCatalog) -> None:
    items = [(c, catalog.moments[c.moment_id]) for sc in plan.scenes for c in sc.clips if c.moment_id in catalog.moments]
    for i, (c1, m1) in enumerate(items):
        for c2, m2 in items[i + 1:]:
            if m1.moment_id == m2.moment_id:
                continue
            if m1.asset_id == m2.asset_id and None not in (m1.source_in_seconds, m2.source_in_seconds):
                if m1.source_in_seconds < m2.source_out_seconds and m2.source_in_seconds < m1.source_out_seconds:  # type: ignore[operator]
                    report.warn("overlapping_source_ranges", f"clips {c1.clip_instance_id} and {c2.clip_instance_id} use overlapping moments of {m1.asset_id}")
    _content_duplicates(report, [catalog.assets[m.asset_id] for _, m in items if m.asset_id in catalog.assets])


def _content_duplicates(report: ValidationReport, assets: list[AssetInfo]) -> None:
    by_cv: dict[str, set[str]] = {}
    for a in assets:
        if a.content_version:
            by_cv.setdefault(a.content_version, set()).add(a.asset_id)
    for cv, ids in sorted(by_cv.items()):
        if len(ids) > 1:
            report.warn("duplicate_content", f"assets {', '.join(sorted(ids))} have the same content fingerprint (probable copies)")


# ---- edit plans ------------------------------------------------------------------------------------------

def _orientation_of(w: int, h: int) -> str:
    return "portrait" if h > w else "landscape" if w > h else "square"


def validate_edit_plan(plan: EditPlan | Mapping[str, Any], catalog: PlanningCatalog, settings: ValidationSettings) -> ValidationReport:
    report = ValidationReport()
    if not isinstance(plan, EditPlan):
        try:
            plan = EditPlan.model_validate(plan)
        except ValidationError as e:
            _schema_errors(report, e)
            return report
    plan = plan.model_copy(deep=True)
    original_hash = plan.plan_hash
    try:
        fps = parse_fps(plan.sequence.fps)
    except ValueError as e:
        report.error("bad_frame_rate", f"sequence.fps: {e}", "sequence.fps")
        report.plan = plan
        return report
    one_frame = float(1 / fps)
    clips = [(si, ci, c) for si, s in enumerate(plan.scenes) for ci, c in enumerate(s.clips)]
    seen_clips: set[str] = set()
    seen_scenes: set[str] = set()
    for si, s in enumerate(plan.scenes):
        if s.scene_id in seen_scenes:
            report.error("duplicate_scene_id", f"scene_id {s.scene_id!r} is used more than once", f"scenes[{si}]")
        seen_scenes.add(s.scene_id)
    seq_orient = _orientation_of(plan.sequence.width, plan.sequence.height)

    for si, ci, c in clips:
        path, cid = f"scenes[{si}].clips[{ci}]", c.clip_instance_id
        if cid in seen_clips:
            report.error("duplicate_clip_instance_id", f"clip_instance_id {cid!r} is used more than once", path, cid)
        seen_clips.add(cid)
        a = catalog.assets.get(c.asset_id)
        m = catalog.moments.get(c.moment_id)
        if a is None:
            report.error("asset_not_found", f"clip {cid}: unknown asset_id {c.asset_id!r}", path, cid)
        else:
            _check_asset_usable(report, a, path, cid)
            _check_role(report, c.track_role, a, settings, path, cid)
            if a.media_type != c.media_type:
                report.error("media_type_mismatch", f"clip {cid}: plan says {c.media_type} but asset is {a.media_type}", path, cid)
            if c.audio_policy == "natural_sound" and c.track_role == "primary_video" and a.media_type == "video" and not a.has_audio:
                report.warn("silent_source", f"clip {cid}: audio_policy natural_sound but the source has no audio", path, cid)
            o = a.orientation
            if o in ("portrait", "landscape") and seq_orient in ("portrait", "landscape") and o != seq_orient and c.track_role in VISUAL_ROLES:
                report.warn("orientation_mismatch", f"clip {cid}: {o} source in a {seq_orient} {plan.sequence.width}x{plan.sequence.height} "
                            "sequence (will need scaling/reframing); review", path, cid)
        if m is None:
            report.error("moment_not_found", f"clip {cid}: unknown moment_id {c.moment_id!r}", path, cid)
        elif m.asset_id != c.asset_id:
            report.error("moment_asset_mismatch", f"clip {cid}: moment {m.moment_id} belongs to {m.asset_id}, not {c.asset_id}", path, cid)
        if c.media_type == "image":
            _check_still(report, c, path)
        else:
            _check_range(report, c, a, m, fps, one_frame, settings, path)
        if not c.resolver_notes and c.media_type != "image" and c.source_in_frame is not None and c.source_out_frame is not None:
            if seconds_to_frames(c.requested_duration_seconds, fps) - c.duration_frames > 1:
                report.warn("unexplained_shortfall", f"clip {cid}: {c.duration_frames}f is shorter than requested "
                            f"{c.requested_duration_seconds:g}s with no resolver note", path, cid)

    _check_transitions(report, plan, catalog, fps, one_frame)
    _layout(report, plan, fps, settings)
    _check_totals(report, plan, fps, settings)
    _check_duplicates_edit(report, plan, catalog)
    _check_refs(report, [(c.clip_instance_id, c.moment_id) for _, _, c in clips], catalog, settings)

    if report.repairs:
        plan.plan_hash = edit_plan_hash(plan)
    elif settings.check_hash and original_hash != edit_plan_hash(plan):
        report.error("plan_hash_mismatch", "plan_hash does not match the plan content (edited after resolution?)", "plan_hash")
    report.plan = plan
    return report


def _check_still(report: ValidationReport, c: EditClip, path: str) -> None:
    if c.duration_frames < 1:
        report.error("still_duration", f"still {c.clip_instance_id} must have a positive duration", path, c.clip_instance_id)
    if c.source_in_seconds is not None or c.source_out_seconds is not None:
        report.warn("still_has_source_range", f"still {c.clip_instance_id} carries a source range; it is ignored", path, c.clip_instance_id)


def _check_range(report: ValidationReport, c: EditClip, a: AssetInfo | None, m: MomentInfo | None, fps: Fraction, one_frame: float,
                 s: ValidationSettings, path: str) -> None:
    cid = c.clip_instance_id
    if c.source_in_seconds is None or c.source_out_seconds is None:
        report.error("missing_source_range", f"clip {cid}: video/audio clips need source_in_seconds and source_out_seconds", path, cid)
        return
    if not (math.isfinite(c.source_in_seconds) and math.isfinite(c.source_out_seconds)):
        report.error("non_finite_time", f"clip {cid}: non-finite source time", path, cid)
        return
    if c.source_in_seconds < 0:
        report.error("source_in_negative", f"clip {cid}: source_in_seconds {c.source_in_seconds} < 0", path, cid)
        return
    if c.source_out_seconds <= c.source_in_seconds:
        report.error("source_out_not_after_in", f"clip {cid}: source_out_seconds {c.source_out_seconds} <= source_in_seconds {c.source_in_seconds}", path, cid)
        return
    # frame alignment (repair: round to nearest frame)
    for name in ("source_in_seconds", "source_out_seconds"):
        v = getattr(c, name)
        if frame_distance(v, fps) > Fraction(1, 1000) and s.repair:
            new = frames_to_float(seconds_to_frames(v, fps), fps)
            setattr(c, name, new)
            report.repaired("frame_rounding", f"clip {cid}: {name} {v} is not on a frame boundary; rounded to {new}", v, new, f"{path}.{name}", cid)
        elif frame_distance(v, fps) > Fraction(1, 1000):
            report.warn("not_frame_aligned", f"clip {cid}: {name} {v} is not on a frame boundary", path, cid)
    in_f, out_f = seconds_to_frames(c.source_in_seconds, fps), seconds_to_frames(c.source_out_seconds, fps)
    clamped = False
    # asset bounds
    if a is not None and a.duration_seconds is not None:
        over = c.source_out_seconds - a.duration_seconds
        if over > s.source_tolerance_seconds:
            limit = seconds_to_frames(a.duration_seconds, fps, "floor")
            if s.repair and over <= s.repair_max_overshoot_frames * one_frame and limit > in_f:
                report.repaired("clamp_to_asset_end", f"clip {cid}: source_out {c.source_out_seconds} is past the asset end "
                                f"{a.duration_seconds}; clamped to frame {limit}", c.source_out_seconds, frames_to_float(limit, fps), f"{path}.source_out_seconds", cid)
                c.source_out_seconds = frames_to_float(limit, fps)
                out_f = limit
                clamped = True
            else:
                report.error("source_out_of_range", f"clip {cid}: source_out_seconds {c.source_out_seconds} exceeds asset duration {a.duration_seconds}", path, cid)
                return
    # moment bounds
    if m is not None and m.source_in_seconds is not None and m.source_out_seconds is not None:
        tol = max(one_frame, s.source_tolerance_seconds)
        if c.source_in_seconds < m.source_in_seconds - tol or c.source_out_seconds > m.source_out_seconds + tol:
            report.error("outside_moment", f"clip {cid}: range {c.source_in_seconds}-{c.source_out_seconds}s lies outside moment "
                         f"{m.moment_id} ({m.source_in_seconds}-{m.source_out_seconds}s)", path, cid)
    # frame fields
    for name, want in (("source_in_frame", in_f), ("source_out_frame", out_f)):
        have = getattr(c, name)
        if have != want:
            if s.repair:
                setattr(c, name, want)
                report.repaired("frame_fields", f"clip {cid}: {name} {have} != {want} implied by seconds; recomputed", have, want, f"{path}.{name}", cid)
            else:
                report.error("frame_field_mismatch", f"clip {cid}: {name} {have} != {want}", path, cid)
    span = out_f - in_f
    if c.duration_frames != span:
        if (abs(c.duration_frames - span) <= 1 or clamped) and s.repair:
            report.repaired("duration_frames", f"clip {cid}: duration_frames {c.duration_frames} differs from source span {span}f; recomputed",
                            c.duration_frames, span, f"{path}.duration_frames", cid)
            c.duration_frames = span
        else:
            report.error("duration_mismatch", f"clip {cid}: duration_frames {c.duration_frames} does not match source span {span}f", path, cid)


def _check_transitions(report: ValidationReport, plan: EditPlan, catalog: PlanningCatalog, fps: Fraction, one_frame: float) -> None:
    prev: EditClip | None = None
    def cut(_c: EditClip) -> EditTransition:
        return EditTransition(type="cut", duration_seconds=0.0, duration_frames=0, alignment="center")

    for si, s in enumerate(plan.scenes):
        for ci, c in enumerate(s.clips):
            t, path, cid = c.transition_in, f"scenes[{si}].clips[{ci}].transition_in", c.clip_instance_id
            if t.type == "cut":
                if t.duration_frames or t.duration_seconds:
                    report.repaired("cut_duration", f"clip {cid}: cut with non-zero duration set to 0", [t.duration_frames, t.duration_seconds], [0, 0.0], path, cid)
                    c.transition_in = cut(c)
            else:
                reason = None
                if c.track_role != "primary_video":
                    reason = "dissolve on a non-primary track is not supported"
                elif prev is None:
                    reason = "dissolve on the first clip has nothing to dissolve from"
                if reason:
                    report.repaired("dissolve_to_cut", f"clip {cid}: {reason}; replaced by a cut", t.type, "cut", path, cid)
                    c.transition_in = cut(c)
                else:
                    _check_dissolve(report, c, prev, catalog, fps, path)  # type: ignore[arg-type]
            if c.track_role == "primary_video":
                prev = c


def _handle_frames(c: EditClip, catalog: PlanningCatalog, fps: Fraction) -> tuple[float, float]:
    """(before, after) source handle frames; infinity for stills."""
    if c.media_type == "image" or c.source_in_frame is None or c.source_out_frame is None:
        return math.inf, math.inf
    a = catalog.assets.get(c.asset_id)
    after = math.inf if a is None or a.duration_seconds is None else max(0, seconds_to_frames(a.duration_seconds, fps, "floor") - c.source_out_frame)
    return float(c.source_in_frame), float(after)


def _check_dissolve(report: ValidationReport, c: EditClip, prev: EditClip, catalog: PlanningCatalog, fps: Fraction, path: str) -> None:
    t, cid = c.transition_in, c.clip_instance_id
    if t.duration_frames < 2:
        report.error("transition_too_short", f"clip {cid}: dissolve needs at least 2 frames", path, cid)
        return
    if abs(frames_to_float(t.duration_frames, fps) - t.duration_seconds) > float(1 / fps):
        new = frames_to_float(t.duration_frames, fps)
        report.repaired("transition_seconds", f"clip {cid}: transition seconds recomputed from frames", t.duration_seconds, new, path, cid)
        t.duration_seconds = new
    if t.duration_frames > min(prev.duration_frames, c.duration_frames):
        report.error("transition_exceeds_clip", f"clip {cid}: {t.duration_frames}f dissolve is longer than an adjacent clip "
                     f"({prev.duration_frames}f before, {c.duration_frames}f after)", path, cid)
        return
    _, after_prev = _handle_frames(prev, catalog, fps)
    before_cur, _ = _handle_frames(c, catalog, fps)
    d = t.duration_frames
    need_prev, need_cur = {"center": ((d + 1) // 2,) * 2, "start_at_cut": (d, 0), "end_at_cut": (0, d)}[t.alignment]
    if after_prev < need_prev or before_cur < need_cur:
        report.error("insufficient_handles", f"clip {cid}: {t.alignment} dissolve of {d}f needs {need_prev}f after the previous clip's out point "
                     f"(has {after_prev:g}) and {need_cur}f before this clip's in point (has {before_cur:g})", path, cid)


def _layout(report: ValidationReport, plan: EditPlan, fps: Fraction, s: ValidationSettings) -> None:
    """Timeline positions are derived from integer frame counts; disagreements are repaired (or errors without repair)."""
    primaries = [c for sc in plan.scenes for c in sc.clips if c.track_role == "primary_video"]
    if not primaries:
        report.error("no_primary_clips", "the plan has no primary_video clips")
        return
    starts = iter(cumulative_starts(c.duration_frames for c in primaries))
    cursor = 0
    for si, sc in enumerate(plan.scenes):
        sc_start, sc_primary = cursor, 0
        role_cursor: dict[str, int] = {}
        for ci, c in enumerate(sc.clips):
            path = f"scenes[{si}].clips[{ci}].timeline_start_frame"
            if c.track_role == "primary_video":
                want = next(starts)
                sc_primary += c.duration_frames
                cursor = want + c.duration_frames
                if c.timeline_start_frame != want:
                    _fix_or_err(report, s, "timeline_position", f"clip {c.clip_instance_id}: timeline_start_frame {c.timeline_start_frame} should be {want}",
                                c, "timeline_start_frame", want, path)
            else:
                # non-primary clips: any non-negative position is allowed (checked for overlaps below)
                role_cursor[c.track_role] = max(role_cursor.get(c.track_role, 0), c.timeline_start_frame + c.duration_frames)
        for name, want in (("start_frame", sc_start), ("duration_frames", sc_primary)):
            have = getattr(sc, name)
            if have != want:
                _fix_or_err(report, s, "scene_span", f"scene {sc.scene_id}: {name} {have} should be {want}", sc, name, want, f"scenes[{si}].{name}")
    for mi, mk in enumerate(plan.markers):
        sc = next((x for x in plan.scenes if x.scene_id == mk.scene_id), None)
        if sc is None:
            report.error("marker_scene_unknown", f"marker {mk.marker_id} references unknown scene {mk.scene_id}", f"markers[{mi}]")
        elif mk.frame != sc.start_frame:
            _fix_or_err(report, s, "marker_position", f"marker {mk.marker_id}: frame {mk.frame} should be {sc.start_frame}", mk, "frame", sc.start_frame, f"markers[{mi}].frame")
    total = cursor
    if plan.total_duration_frames != total:
        _fix_or_err(report, s, "total_frames", f"total_duration_frames {plan.total_duration_frames} should be {total}", plan, "total_duration_frames", total, "total_duration_frames")
    if abs(plan.total_duration_seconds - frames_to_float(total, fps)) > 1e-6:
        _fix_or_err(report, s, "total_seconds", f"total_duration_seconds {plan.total_duration_seconds} should be {frames_to_float(total, fps)}",
                    plan, "total_duration_seconds", frames_to_float(total, fps), "total_duration_seconds")
    # non-primary track overlaps
    by_role: dict[str, list[EditClip]] = {}
    for sc in plan.scenes:
        for c in sc.clips:
            if c.track_role != "primary_video":
                by_role.setdefault(c.track_role, []).append(c)
    for role, cs in sorted(by_role.items()):
        cs.sort(key=lambda c: (c.timeline_start_frame, c.clip_instance_id))
        for x, y in zip(cs, cs[1:]):
            if y.timeline_start_frame < x.timeline_start_frame + x.duration_frames:
                report.warn("track_overlap", f"{role} clips {x.clip_instance_id} and {y.clip_instance_id} overlap on the timeline")
        for c in cs:
            if c.timeline_start_frame + c.duration_frames > total:
                report.warn("beyond_timeline_end", f"{role} clip {c.clip_instance_id} extends past the end of the primary video ({total}f)")


def _fix_or_err(report: ValidationReport, s: ValidationSettings, code: str, msg: str, obj: Any, attr: str, want: Any, path: str) -> None:
    if s.repair:
        report.repaired(code, msg + "; recomputed from frame counts", getattr(obj, attr), want, path)
        setattr(obj, attr, want)
    else:
        report.error(code, msg, path)


def _check_totals(report: ValidationReport, plan: EditPlan, fps: Fraction, s: ValidationSettings) -> None:
    total_s = float(Fraction(plan.total_duration_frames) / fps)
    tol = s.total_tolerance_seconds()
    if abs(total_s - s.target_duration_seconds) > tol:
        report.error("total_duration_out_of_tolerance", f"total duration {total_s:.2f}s differs from target {s.target_duration_seconds:g}s by more than "
                     f"{tol:.2f}s (max of {s.duration_tolerance_pct:g}% and {s.duration_tolerance_seconds:g}s)")
    for si, sc in enumerate(plan.scenes):
        actual = float(Fraction(sc.duration_frames) / fps)
        stol = max(sc.target_duration_seconds * s.scene_budget_tolerance_pct / 100, s.scene_budget_tolerance_seconds)
        if actual > sc.target_duration_seconds + stol:
            report.warn("scene_over_budget", f"scene {sc.scene_id}: {actual:.2f}s exceeds its budget of {sc.target_duration_seconds:g}s", f"scenes[{si}]")


def _check_duplicates_edit(report: ValidationReport, plan: EditPlan, catalog: PlanningCatalog) -> None:
    clips = [c for sc in plan.scenes for c in sc.clips]
    for i, x in enumerate(clips):
        for y in clips[i + 1:]:
            if x.asset_id != y.asset_id:
                continue
            explained = bool(x.intentional_repeat_reason or y.intentional_repeat_reason)
            if x.media_type == "image" or y.media_type == "image":
                if x.media_type == y.media_type and not explained:
                    report.error("unexplained_repeat", f"still {x.asset_id} is used by clips {x.clip_instance_id} and {y.clip_instance_id} without intentional_repeat_reason",
                                 clip=y.clip_instance_id)
                continue
            if None in (x.source_in_frame, x.source_out_frame, y.source_in_frame, y.source_out_frame):
                continue
            overlap = min(x.source_out_frame, y.source_out_frame) - max(x.source_in_frame, y.source_in_frame)  # type: ignore[operator,type-var]
            if overlap <= 0:
                continue
            ratio = overlap / min(x.duration_frames, y.duration_frames)
            if explained:
                continue
            if ratio >= 0.5:
                report.error("unexplained_repeat", f"clips {x.clip_instance_id} and {y.clip_instance_id} reuse {ratio:.0%} of the same source range "
                             "without intentional_repeat_reason", clip=y.clip_instance_id)
            else:
                report.warn("overlapping_source_ranges", f"clips {x.clip_instance_id} and {y.clip_instance_id} have overlapping source ranges of {x.asset_id}")
    _content_duplicates(report, [catalog.assets[c.asset_id] for c in clips if c.asset_id in catalog.assets])
