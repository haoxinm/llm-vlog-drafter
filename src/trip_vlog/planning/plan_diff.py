"""Localized plan changes: a closed set of operations, applied through the resolver and the validators, with a readable diff.

Independent of who proposes the changes: the QC critic (Phase 5b) uses it today, natural-language revisions (Phase 6, DESIGN §16)
will reuse it. Nothing here talks to a model or to FilmCraft.

How it works
  * ``selection_from_edit`` turns the current edit plan back into the selection plan it was resolved from (the selection plan on
    disk when it still matches the edit plan, else a reconstruction: every field of a clip and title that the resolver reads is
    kept in the edit plan). ``check_roundtrip`` proves that resolving it again gives the same timeline; when it does not (the plan was
    edited by hand, or the catalog changed since) ``apply_ops`` raises ``PlanBaseError`` and nothing is changed.
  * An operation is a small, closed record (``PlanOp``). The only times it carries are durations in seconds; code snaps them to frames and
    the existing resolver computes source ranges. Operations are applied one at a time to a copy of the selection plan; after each one the
    plan is validated again (``validate_selection_plan``, ``resolve_selection_plan``, ``validate_edit_plan``: must_include / must_exclude,
    total-duration tolerance, transitions and handles, titles, repeats). An operation that leaves the plan with any error is REFUSED, with
    the reason, and the plan stays as it was before it; later operations still get their turn (``atomic=True``: all or nothing).
  * ``diff_plans`` describes the result for a person: per changed scene the clips removed, moved, trimmed, swapped, with transitions
    and sound changes, the duration change of the scene and of the whole film.

Operations (``OPERATIONS``)
  trim_clip       clip_instance_id + duration_seconds and/or anchor (start|center|end|best); within the moment's usable length
  swap_moment     clip_instance_id + moment_id, one of the scene's candidate moments (``PlanContext.scene_candidates``); the same kind of media
  remove_clip     clip_instance_id; never the last primary clip of a scene, never a clip a title starts at
  move_clip       clip_instance_id + to_position (1-based, inside the clip's own scene)
  set_transition  clip_instance_id + transition_type (cut|dissolve) [+ transition_seconds for a dissolve]; refused when the resolver cannot honour it
  set_clip_audio  clip_instance_id + audio: mute (muted) | keep (natural_sound) | lower (ambient_low, the configured ambient gain)
  shorten_scene   scene_id + shorten_by_seconds; the scene's primary clips are scaled down together, none below its minimum
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from trip_vlog.filmcraft.plan_mapping import SUPPORTED_ALIGNMENTS
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.duration import frames_to_float, parse_fps, seconds_to_frames
from trip_vlog.planning.models import (EditPlan, SelectionClip, SelectionPlan, SelectionScene, SelectionTitle, SelectionTransition, TitleStart,
                                       selection_plan_hash)
from trip_vlog.planning.resolve_plan import PlanResolutionError, PlanSettings, resolve_selection_plan
from trip_vlog.planning.selection import MAX_STILL_SECONDS, MIN_CLIP_SECONDS, fit_total, moment_limit
from trip_vlog.planning.validate import ValidationReport, ValidationSettings, validate_edit_plan, validate_selection_plan

OPERATIONS = ("trim_clip", "swap_moment", "remove_clip", "move_clip", "set_transition", "set_clip_audio", "shorten_scene")
ANCHORS = ("start", "center", "end", "best")
AUDIO_MODES = {"mute": "muted", "keep": "natural_sound", "lower": "ambient_low"}
MIN_DISSOLVE_SECONDS = 0.1
MAX_DISSOLVE_SECONDS = 5.0
MAX_REASON_CHARS = 300
RESOLVER_SHORTFALL_NOTE = "clip(s) are shorter than requested because their moment is short"   # appended to uncertainties by the resolver

# fields each operation needs (one of the tuple's alternatives in a nested tuple) and may carry
_REQUIRED: dict[str, tuple[Any, ...]] = {
    "trim_clip": ("clip_instance_id", ("duration_seconds", "anchor")),
    "swap_moment": ("clip_instance_id", "moment_id"),
    "remove_clip": ("clip_instance_id",),
    "move_clip": ("clip_instance_id", "to_position"),
    "set_transition": ("clip_instance_id", "transition_type"),
    "set_clip_audio": ("clip_instance_id", "audio"),
    "shorten_scene": ("scene_id", "shorten_by_seconds"),
}
_FIELDS: dict[str, tuple[str, ...]] = {
    "trim_clip": ("clip_instance_id", "duration_seconds", "anchor"),
    "swap_moment": ("clip_instance_id", "moment_id"),
    "remove_clip": ("clip_instance_id",),
    "move_clip": ("clip_instance_id", "to_position"),
    "set_transition": ("clip_instance_id", "transition_type", "transition_seconds"),
    "set_clip_audio": ("clip_instance_id", "audio"),
    "shorten_scene": ("scene_id", "shorten_by_seconds"),
}


class OpError(ValueError):
    """An operation record is malformed (unknown operation, missing or out-of-set parameter)."""


class OpRefused(Exception):
    """One operation cannot be applied (the plan stays as it was). `code` is stable, `message` is for people."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PlanBaseError(RuntimeError):
    """The current plan cannot serve as the base of a change (it no longer resolves to itself)."""


# ---- operations ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanOp:
    op: str
    clip_instance_id: str | None = None
    scene_id: str | None = None
    duration_seconds: float | None = None
    anchor: str | None = None
    moment_id: str | None = None
    to_position: int | None = None
    transition_type: str | None = None
    transition_seconds: float | None = None
    audio: str | None = None
    shorten_by_seconds: float | None = None
    reason: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PlanOp":
        """Build and check an operation from a record. Accepts the flat shape with null for 'not used' (the critic's reply). Parameters
        that do not belong to the operation are dropped; missing or out-of-set ones raise `OpError`."""
        name = d.get("op", d.get("operation"))
        if name not in OPERATIONS:
            raise OpError(f"unknown operation {name!r} (allowed: {', '.join(OPERATIONS)})")
        vals: dict[str, Any] = {k: d.get(k) for k in _FIELDS[name]}
        for need in _REQUIRED[name]:
            alts = need if isinstance(need, tuple) else (need,)
            if all(vals.get(a) is None for a in alts):
                raise OpError(f"{name} needs {' or '.join(alts)}")
        for k in ("clip_instance_id", "scene_id", "moment_id"):
            if vals.get(k) is not None and (not isinstance(vals[k], str) or not vals[k].strip()):
                raise OpError(f"{name}: {k} must be a non-empty string")
            if isinstance(vals.get(k), str):
                vals[k] = vals[k].strip()
        for k in ("duration_seconds", "transition_seconds", "shorten_by_seconds"):
            v = vals.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
                raise OpError(f"{name}: {k} must be a finite number")
            if v is not None:
                vals[k] = float(v)
        if name == "trim_clip":
            if vals["duration_seconds"] is not None and vals["duration_seconds"] <= 0:
                raise OpError("trim_clip: duration_seconds must be more than 0")
            if vals["anchor"] is not None and vals["anchor"] not in ANCHORS:
                raise OpError(f"trim_clip: anchor must be one of {', '.join(ANCHORS)}")
        if name == "move_clip":
            p = vals["to_position"]
            if isinstance(p, bool) or not isinstance(p, int) or p < 1:
                raise OpError("move_clip: to_position must be an integer from 1")
        if name == "set_transition":
            if vals["transition_type"] not in ("cut", "dissolve"):
                raise OpError("set_transition: transition_type must be cut or dissolve")
            if vals["transition_type"] == "cut":
                vals["transition_seconds"] = 0.0
            elif vals["transition_seconds"] is None:
                raise OpError("set_transition: a dissolve needs transition_seconds")
            elif not (MIN_DISSOLVE_SECONDS <= vals["transition_seconds"] <= MAX_DISSOLVE_SECONDS):
                raise OpError(f"set_transition: a dissolve lasts {MIN_DISSOLVE_SECONDS:g} to {MAX_DISSOLVE_SECONDS:g} seconds")
        if name == "set_clip_audio" and vals["audio"] not in AUDIO_MODES:
            raise OpError(f"set_clip_audio: audio must be one of {', '.join(AUDIO_MODES)}")
        if name == "shorten_scene" and vals["shorten_by_seconds"] <= 0:
            raise OpError("shorten_scene: shorten_by_seconds must be more than 0")
        reason = " ".join(str(d.get("reason") or "").split())[:MAX_REASON_CHARS]
        return cls(op=name, reason=reason, **vals)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.op}
        for k in _FIELDS[self.op]:
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        if self.reason:
            out["reason"] = self.reason
        return out

    @property
    def target_clip(self) -> str | None:
        return self.clip_instance_id

    def describe(self) -> str:
        n = self.op
        if n == "trim_clip":
            bits = ([f"to {self.duration_seconds:g} s"] if self.duration_seconds is not None else []) + ([f"anchor {self.anchor}"] if self.anchor else [])
            return f"trim {self.clip_instance_id} " + ", ".join(bits)
        if n == "swap_moment":
            return f"replace the moment of {self.clip_instance_id} with {self.moment_id}"
        if n == "remove_clip":
            return f"remove {self.clip_instance_id}"
        if n == "move_clip":
            return f"move {self.clip_instance_id} to position {self.to_position} of its scene"
        if n == "set_transition":
            return (f"cut into {self.clip_instance_id}" if self.transition_type == "cut"
                    else f"dissolve of {self.transition_seconds:g} s into {self.clip_instance_id}")
        if n == "set_clip_audio":
            return f"{ {'mute': 'mute', 'keep': 'keep the sound of', 'lower': 'lower the sound of'}[self.audio or 'keep'] } {self.clip_instance_id}"
        return f"shorten scene {self.scene_id} by {self.shorten_by_seconds:g} s"


# ---- context ------------------------------------------------------------------------------------------------------------

@dataclass
class PlanContext:
    """Everything an operation is checked against. ``scene_candidates``: scene_id -> moment ids that ``swap_moment`` may use (the approved
    outline's candidates); None disables swapping. ``vset`` carries the tolerance and the must_include / must_exclude rules (a caller that
    changes the target on purpose, e.g. a revision that shortens the film, passes its own)."""

    catalog: PlanningCatalog
    vset: ValidationSettings
    pset: PlanSettings
    scene_candidates: Mapping[str, Sequence[str]] | None = None
    min_still_seconds: float = 1.5
    # dissolve alignments the editor can place (the FilmCraft adapter places only centred ones; a dissolve that needs another alignment because
    # a neighbour lacks source handle would silently become a hard cut there, so an operation asking for one is refused)
    supported_alignments: tuple[str, ...] = SUPPORTED_ALIGNMENTS

    @property
    def fps(self) -> Fraction:
        return parse_fps(self.pset.frame_rate)


# ---- selection plan <-> edit plan ---------------------------------------------------------------------------------------

def selection_from_edit(edit: EditPlan, base: SelectionPlan | None = None) -> SelectionPlan:
    """The selection plan behind ``edit``. ``base`` (the plan on disk) is used when its hash is the one the edit plan was resolved from."""
    if base is not None and selection_plan_hash(base) == edit.selection_plan_hash:
        return base.model_copy(deep=True)
    scenes = [SelectionScene(
        scene_id=s.scene_id, title=s.title, purpose=s.purpose, target_duration_seconds=s.target_duration_seconds,
        clips=[SelectionClip(
            clip_instance_id=c.clip_instance_id, moment_id=c.moment_id, target_duration_seconds=c.requested_duration_seconds, anchor=c.anchor,
            track_role=c.track_role, audio_policy=c.audio_policy,
            transition_in=SelectionTransition(type=c.transition_in.type, duration_seconds=c.transition_in.duration_seconds),
            rationale=c.rationale, intentional_repeat_reason=c.intentional_repeat_reason) for c in s.clips]) for s in edit.scenes]
    titles = [SelectionTitle(title_id=t.title_id, text=t.text, role=t.role, scene_id=t.scene_id, text_source=t.text_source,
                 start=TitleStart.model_validate(t.start.model_dump()), duration_seconds=t.duration_seconds, track=t.track, style=t.style,
                 rationale=t.rationale) for t in edit.titles]
    unc = [u for u in edit.uncertainties if RESOLVER_SHORTFALL_NOTE not in u][:100]
    return SelectionPlan(schema_version=edit.schema_version, planner="revision", scenes=scenes, titles=titles, uncertainties=unc)


def layout_signature(plan: EditPlan) -> list[Any]:
    """What the timeline is made of (not hashes, notes or wording): used to prove that re-resolving a selection gives the same plan."""
    clips = [[s.scene_id, c.clip_instance_id, c.asset_id, c.moment_id, c.track_role, c.audio_policy, c.source_in_frame, c.source_out_frame,
              c.timeline_start_frame, c.duration_frames, c.transition_in.type, c.transition_in.duration_frames, c.transition_in.alignment]
             for s in plan.scenes for c in s.clips]
    titles = [[t.title_id, t.text, t.start_frame, t.duration_frames] for t in plan.titles]
    return [clips, titles, plan.total_duration_frames]


def check_roundtrip(edit: EditPlan, selection: SelectionPlan, ctx: PlanContext) -> None:
    """Raise `PlanBaseError` unless resolving ``selection`` reproduces ``edit``'s timeline exactly."""
    try:
        again = resolve_selection_plan(selection, ctx.catalog, ctx.pset)
    except PlanResolutionError as e:
        raise PlanBaseError("the current plan no longer resolves against the catalog: " + "; ".join(e.issues[:3])) from None
    if layout_signature(again) != layout_signature(edit):
        raise PlanBaseError("the current plan differs from what its selection plan resolves to (edited by hand, or the catalog or the "
                            "project settings changed since it was made); changes are only applied to a plan that matches its selection")


def load_selection_file(path: Path | str) -> SelectionPlan | None:
    try:
        return SelectionPlan.model_validate(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    except (OSError, ValueError, ValidationError):
        return None


# ---- single operations on a selection plan ------------------------------------------------------------------------------

def _find(sel: SelectionPlan, clip_id: str | None) -> tuple[SelectionScene, int, SelectionClip]:
    for sc in sel.scenes:
        for i, c in enumerate(sc.clips):
            if c.clip_instance_id == clip_id:
                return sc, i, c
    raise OpRefused("unknown_clip", f"there is no clip {clip_id!r} in the plan")


def _primaries(sel: SelectionPlan) -> list[SelectionClip]:
    return [c for sc in sel.scenes for c in sc.clips if c.track_role == "primary_video"]


def _snap(seconds: float, fps: Fraction) -> float:
    return frames_to_float(max(1, seconds_to_frames(seconds, fps)), fps)


def _limits(ctx: PlanContext, moment_id: str) -> tuple[float, float]:
    """(shortest, longest) seconds a clip of this moment may have: video min(1 s, moment) to the moment's usable length; photos min_still to 10 s."""
    lim = moment_limit(ctx.catalog, moment_id, ctx.fps)
    if lim is None:
        return ctx.min_still_seconds, MAX_STILL_SECONDS
    return min(MIN_CLIP_SECONDS, lim), lim


def _is_still(ctx: PlanContext, moment_id: str) -> bool:
    m = ctx.catalog.moments.get(moment_id)
    a = ctx.catalog.assets.get(m.asset_id) if m else None
    return a is not None and a.media_type == "image"


def _apply_trim(sel: SelectionPlan, op: PlanOp, ctx: PlanContext) -> list[str]:
    _, _, c = _find(sel, op.clip_instance_id)
    one = float(1 / ctx.fps)
    changed = False
    notes: list[str] = []
    if op.anchor is not None:
        if _is_still(ctx, c.moment_id):
            raise OpRefused("anchor_on_still", f"{c.clip_instance_id} is a photo: it has no anchor to change")
        if op.anchor != c.anchor:
            c.anchor, changed = op.anchor, True
    if op.duration_seconds is not None:
        lo, hi = _limits(ctx, c.moment_id)
        want = op.duration_seconds
        if want > hi + one:
            raise OpRefused("duration_exceeds_moment", f"{c.clip_instance_id}: {want:g} s is longer than the {hi:.2f} s that moment {c.moment_id} offers")
        if want < lo - 1e-9:
            raise OpRefused("duration_too_short", f"{c.clip_instance_id}: {want:g} s is shorter than the minimum of {lo:g} s")
        new = min(_snap(want, ctx.fps), hi)
        if abs(new - c.target_duration_seconds) > 1e-6:
            notes.append(f"{c.clip_instance_id}: {c.target_duration_seconds:g} s -> {new:g} s")
            c.target_duration_seconds, changed = round(new, 6), True
    if not changed:
        raise OpRefused("no_change", f"{c.clip_instance_id} already has that duration and anchor")
    return notes


def _apply_swap(sel: SelectionPlan, op: PlanOp, ctx: PlanContext) -> list[str]:
    sc, _, c = _find(sel, op.clip_instance_id)
    if ctx.scene_candidates is None:
        raise OpRefused("swap_unavailable", "moments can only be swapped for the candidates of an approved story outline, and this plan has none")
    if op.moment_id not in set(ctx.scene_candidates.get(sc.scene_id, ())):
        raise OpRefused("moment_not_candidate", f"{op.moment_id} is not one of the candidate moments of scene {sc.scene_id}")
    if op.moment_id == c.moment_id:
        raise OpRefused("no_change", f"{c.clip_instance_id} already uses {op.moment_id}")
    if op.moment_id not in ctx.catalog.moments:
        raise OpRefused("unknown_moment", f"{op.moment_id} is not a moment in the catalog")
    if _is_still(ctx, c.moment_id) != _is_still(ctx, op.moment_id):
        raise OpRefused("media_kind_differs", f"{op.moment_id} is {'a photo' if _is_still(ctx, op.moment_id) else 'a video'}; a clip is only swapped for the same kind of media")
    if any(x.moment_id == op.moment_id for s in sel.scenes for x in s.clips):
        raise OpRefused("moment_in_use", f"{op.moment_id} is already used by another clip")
    lo, hi = _limits(ctx, op.moment_id)
    new = min(c.target_duration_seconds, hi)
    if new < lo - 1e-9:
        raise OpRefused("duration_too_short", f"{op.moment_id} offers only {hi:.2f} s, less than the minimum clip length of {lo:g} s")
    old_m, old_d = c.moment_id, c.target_duration_seconds
    c.moment_id, c.target_duration_seconds, c.intentional_repeat_reason = op.moment_id, round(_snap(new, ctx.fps), 6), None
    if op.reason:
        c.rationale = op.reason[:MAX_REASON_CHARS]
    return [f"{c.clip_instance_id}: moment {old_m} -> {op.moment_id}"
            + (f" (duration {old_d:g} s -> {c.target_duration_seconds:g} s, the new moment is shorter)" if abs(old_d - c.target_duration_seconds) > 1e-6 else "")]


def _apply_remove(sel: SelectionPlan, op: PlanOp, ctx: PlanContext) -> list[str]:
    sc, i, c = _find(sel, op.clip_instance_id)
    anchored = [t.title_id for t in sel.titles if t.start.clip_instance_id == c.clip_instance_id]
    if anchored:
        raise OpRefused("title_anchored", f"title {', '.join(anchored)} starts at {c.clip_instance_id}; the clip is not removed from under a title")
    if c.track_role == "primary_video" and sum(1 for x in sc.clips if x.track_role == "primary_video") <= 1:
        raise OpRefused("last_primary_clip", f"{c.clip_instance_id} is the only primary clip of scene {sc.scene_id}")
    del sc.clips[i]
    return [f"{c.clip_instance_id} removed from scene {sc.scene_id}"]


def _apply_move(sel: SelectionPlan, op: PlanOp, ctx: PlanContext) -> list[str]:
    sc, i, c = _find(sel, op.clip_instance_id)
    n = len(sc.clips)
    if not (1 <= int(op.to_position or 0) <= n):
        raise OpRefused("position_out_of_range", f"scene {sc.scene_id} has {n} clip(s); position {op.to_position} does not exist")
    if op.to_position - 1 == i:
        raise OpRefused("no_change", f"{c.clip_instance_id} is already at position {op.to_position}")
    sc.clips.insert(op.to_position - 1, sc.clips.pop(i))
    return [f"{c.clip_instance_id}: position {i + 1} -> {op.to_position} in scene {sc.scene_id}"]


def _apply_transition(sel: SelectionPlan, op: PlanOp, ctx: PlanContext) -> list[str]:
    _, _, c = _find(sel, op.clip_instance_id)
    if c.track_role != "primary_video":
        raise OpRefused("transition_not_primary", f"{c.clip_instance_id} is not on the primary video track; transitions only exist there")
    prim = _primaries(sel)
    cur = c.transition_in
    if op.transition_type == "dissolve":
        if prim and prim[0].clip_instance_id == c.clip_instance_id:
            raise OpRefused("dissolve_on_first_clip", f"{c.clip_instance_id} is the first clip of the film; there is nothing to dissolve from")
        prev = prim[[x.clip_instance_id for x in prim].index(c.clip_instance_id) - 1]
        limit = min(prev.target_duration_seconds, c.target_duration_seconds)
        if op.transition_seconds > limit + float(1 / ctx.fps):
            raise OpRefused("transition_too_long", f"a {op.transition_seconds:g} s dissolve is longer than the shorter neighbouring clip ({limit:g} s)")
        new = SelectionTransition(type="dissolve", duration_seconds=round(_snap(op.transition_seconds, ctx.fps), 6))
    else:
        new = SelectionTransition(type="cut", duration_seconds=0.0)
    if new.type == cur.type and abs(new.duration_seconds - cur.duration_seconds) < 1e-6:
        raise OpRefused("no_change", f"{c.clip_instance_id} already begins with that transition")
    c.transition_in = new
    return [f"{c.clip_instance_id}: {cur.type} {cur.duration_seconds:g} s -> {new.type} {new.duration_seconds:g} s"]


def _apply_audio(sel: SelectionPlan, op: PlanOp, ctx: PlanContext) -> list[str]:
    _, _, c = _find(sel, op.clip_instance_id)
    if _is_still(ctx, c.moment_id):
        raise OpRefused("audio_on_still", f"{c.clip_instance_id} is a photo: it has no sound")
    new = AUDIO_MODES[op.audio or "keep"]
    if new == c.audio_policy:
        raise OpRefused("no_change", f"{c.clip_instance_id} already has audio_policy {new}")
    old, c.audio_policy = c.audio_policy, new
    return [f"{c.clip_instance_id}: sound {old} -> {new}"]


def _apply_shorten(sel: SelectionPlan, op: PlanOp, ctx: PlanContext) -> list[str]:
    sc = next((s for s in sel.scenes if s.scene_id == op.scene_id), None)
    if sc is None:
        raise OpRefused("unknown_scene", f"there is no scene {op.scene_id!r} in the plan")
    prim = [c for c in sc.clips if c.track_role == "primary_video"]
    cur = [c.target_duration_seconds for c in prim]
    bounds = [_limits(ctx, c.moment_id)[0] for c in prim]
    target = round(sum(cur) - float(op.shorten_by_seconds or 0), 3)
    if target < sum(min(b, d) for b, d in zip(bounds, cur)) - 1e-6:
        room = sum(cur) - sum(min(b, d) for b, d in zip(bounds, cur))
        raise OpRefused("scene_too_short_to_shorten", f"scene {sc.scene_id} can only be shortened by {room:.2f} s (every clip keeps its minimum length)")
    new = fit_total(cur, [min(b, d) for b, d in zip(bounds, cur)], list(cur), target)
    new = [round(_snap(v, ctx.fps), 6) for v in new]
    if all(abs(a - b) < 1e-6 for a, b in zip(new, cur)):
        raise OpRefused("no_change", f"scene {sc.scene_id} is already that short")
    for c, v in zip(prim, new):
        c.target_duration_seconds = min(v, c.target_duration_seconds)
    return [f"scene {sc.scene_id}: {sum(cur):.2f} s -> {sum(c.target_duration_seconds for c in prim):.2f} s"]


_APPLY = {"trim_clip": _apply_trim, "swap_moment": _apply_swap, "remove_clip": _apply_remove, "move_clip": _apply_move,
          "set_transition": _apply_transition, "set_clip_audio": _apply_audio, "shorten_scene": _apply_shorten}


# ---- applying a list ----------------------------------------------------------------------------------------------------

@dataclass
class AppliedOp:
    op: PlanOp
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op.to_dict(), "status": "applied", "notes": list(self.notes)}


@dataclass
class RefusedOp:
    op: PlanOp | None
    code: str
    message: str
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"op": self.op.to_dict() if self.op else self.raw, "status": "refused", "code": self.code, "message": self.message}
        return d


@dataclass
class ApplyResult:
    base_edit: EditPlan
    selection: SelectionPlan
    edit: EditPlan
    applied: list[AppliedOp] = field(default_factory=list)
    refused: list[RefusedOp] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diff: "PlanDiff | None" = None

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def _roundtrip_model(sel: SelectionPlan) -> SelectionPlan:
    return SelectionPlan.model_validate(sel.model_dump(mode="python"))


def _new_findings(report: ValidationReport, before: set[str]) -> list[str]:
    return [i.message for i in report.warnings if i.message not in before]


def apply_ops(edit: EditPlan, ops: Sequence[PlanOp | Mapping[str, Any]], ctx: PlanContext, *, base_selection: SelectionPlan | None = None,
              atomic: bool = False, verify_base: bool = True) -> ApplyResult:
    """Apply ``ops`` in order to ``edit`` (via its selection plan) and return the new plan with what was applied and what was refused.

    Raises `PlanBaseError` when the plan does not resolve to itself (see `check_roundtrip`; ``verify_base=False`` skips it for a caller
    that has just proved it). ``atomic``: the first refusal discards every change (``applied`` is then empty)."""
    sel0 = selection_from_edit(edit, base_selection)
    if verify_base:
        check_roundtrip(edit, sel0, ctx)
    cur_sel, cur_edit = sel0, edit
    res = ApplyResult(edit, sel0, edit)
    base_warnings = {i.message for i in validate_edit_plan(edit, ctx.catalog, ctx.vset).warnings}
    for raw in ops:
        try:
            op = raw if isinstance(raw, PlanOp) else PlanOp.from_dict(raw)
        except OpError as e:
            res.refused.append(RefusedOp(None, "malformed", str(e), raw))
            continue
        try:
            trial = cur_sel.model_copy(deep=True)
            notes = _APPLY[op.op](trial, op, ctx)
            trial = _roundtrip_model(trial)
        except OpRefused as e:
            res.refused.append(RefusedOp(op, e.code, e.message))
            continue
        except ValidationError as e:
            res.refused.append(RefusedOp(op, "invalid_plan", "the change makes an invalid plan: " + "; ".join(str(x["msg"]) for x in e.errors()[:2])))
            continue
        sr = validate_selection_plan(trial, ctx.catalog, ctx.vset)
        if not sr.ok:
            res.refused.append(RefusedOp(op, "validation", "the plan would no longer validate: " + _first(sr)))
            continue
        try:
            new_edit = resolve_selection_plan(trial, ctx.catalog, ctx.pset)
        except PlanResolutionError as e:
            res.refused.append(RefusedOp(op, "unresolvable", "the change cannot be resolved: " + "; ".join(e.issues[:2])))
            continue
        er = validate_edit_plan(new_edit, ctx.catalog, ctx.vset)
        if not er.ok:
            res.refused.append(RefusedOp(op, "validation", "the plan would no longer validate: " + _first(er)))
            continue
        final = er.plan or new_edit
        why = _not_honoured(op, final, ctx)
        if why:
            res.refused.append(RefusedOp(op, "not_honoured", why))
            continue
        res.warnings += [w for w in _new_findings(er, base_warnings) if w not in res.warnings]
        cur_sel, cur_edit = trial, final
        res.applied.append(AppliedOp(op, notes))
    if atomic and res.refused:
        res.applied = []
        cur_sel, cur_edit = sel0, edit
    res.selection, res.edit = cur_sel, cur_edit
    if res.applied:
        res.diff = diff_plans(edit, cur_edit)
    return res


def _first(report: ValidationReport) -> str:
    return "; ".join(f"{i.code}: {i.message}" for i in report.errors[:2])


def _not_honoured(op: PlanOp, plan: EditPlan, ctx: PlanContext) -> str | None:
    """The resolver may weaken a request (a dissolve without enough source handle becomes a cut, or needs an alignment the editor cannot place):
    then the operation did not do what was asked."""
    if op.op == "set_transition" and op.transition_type == "dissolve":
        c = next((x for s in plan.scenes for x in s.clips if x.clip_instance_id == op.clip_instance_id), None)
        if c is not None and c.transition_in.type != "dissolve":
            return f"{op.clip_instance_id}: the dissolve cannot be made here (not enough source before or after the cut); it would have become a cut"
        if c is not None and c.transition_in.alignment not in ctx.supported_alignments:
            return (f"{op.clip_instance_id}: the dissolve would need the alignment {c.transition_in.alignment!r} (a neighbouring clip has too little source "
                    f"handle for a centred one), and only {', '.join(ctx.supported_alignments)} can be placed; it would have become a hard cut")
    return None


# ---- diff ---------------------------------------------------------------------------------------------------------------

@dataclass
class ClipChange:
    clip_instance_id: str
    kind: str          # removed | moved | trimmed | swapped | transition | sound | anchor
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"clip_instance_id": self.clip_instance_id, "kind": self.kind, "text": self.text}


@dataclass
class SceneDiff:
    scene_id: str
    title: str
    before_seconds: float
    after_seconds: float
    changes: list[ClipChange] = field(default_factory=list)

    @property
    def delta_seconds(self) -> float:
        return round(self.after_seconds - self.before_seconds, 3)

    def to_dict(self) -> dict[str, Any]:
        return {"scene_id": self.scene_id, "title": self.title, "before_seconds": self.before_seconds, "after_seconds": self.after_seconds,
                "delta_seconds": self.delta_seconds, "changes": [c.to_dict() for c in self.changes]}


@dataclass
class PlanDiff:
    old_plan_hash: str
    new_plan_hash: str
    scenes: list[SceneDiff]
    total_before_seconds: float
    total_after_seconds: float
    title_changes: list[str] = field(default_factory=list)

    @property
    def total_delta_seconds(self) -> float:
        return round(self.total_after_seconds - self.total_before_seconds, 3)

    @property
    def changed_scene_ids(self) -> list[str]:
        return [s.scene_id for s in self.scenes]

    def to_dict(self) -> dict[str, Any]:
        return {"old_plan_hash": self.old_plan_hash, "new_plan_hash": self.new_plan_hash, "total_before_seconds": self.total_before_seconds,
                "total_after_seconds": self.total_after_seconds, "total_delta_seconds": self.total_delta_seconds,
                "scenes": [s.to_dict() for s in self.scenes], "title_changes": list(self.title_changes)}

    def lines(self) -> list[str]:
        if not self.scenes and not self.title_changes:
            return ["No change."]
        out = [f"Total: {self.total_before_seconds:.2f} s -> {self.total_after_seconds:.2f} s ({_signed(self.total_delta_seconds)} s)"]
        for s in self.scenes:
            out.append(f"Scene {s.scene_id}: {s.before_seconds:.2f} s -> {s.after_seconds:.2f} s ({_signed(s.delta_seconds)} s)")
            out += [f"  - {c.text}" for c in s.changes]
        out += [f"Title: {t}" for t in self.title_changes]
        return out


def _signed(x: float) -> str:
    return f"{x:+.2f}"


def _sec(frames: int, fps: Fraction) -> float:
    return frames_to_float(frames, fps)


def diff_plans(old: EditPlan, new: EditPlan) -> PlanDiff:
    """Scene- and clip-level description of what differs between two edit plans of the same film (clips are matched by id)."""
    fps = parse_fps(new.sequence.fps)
    old_clips = {c.clip_instance_id: (s.scene_id, i, c) for s in old.scenes for i, c in enumerate(s.clips)}
    new_clips = {c.clip_instance_id: (s.scene_id, i, c) for s in new.scenes for i, c in enumerate(s.clips)}
    old_scenes = {s.scene_id: s for s in old.scenes}
    scenes: list[SceneDiff] = []
    for s in new.scenes:
        o = old_scenes.get(s.scene_id)
        changes: list[ClipChange] = []
        if o is not None:
            for cid, (sid, _, oc) in old_clips.items():
                if sid == s.scene_id and cid not in new_clips:
                    changes.append(ClipChange(cid, "removed", f"{cid} removed ({oc.moment_id}, {_sec(oc.duration_frames, fps):.2f} s)"))
            surviving_old = [c.clip_instance_id for c in o.clips if c.clip_instance_id in new_clips and new_clips[c.clip_instance_id][0] == s.scene_id]
            surviving_new = [c.clip_instance_id for c in s.clips if c.clip_instance_id in old_clips and old_clips[c.clip_instance_id][0] == s.scene_id]
            for c in s.clips:
                cid = c.clip_instance_id
                if cid not in old_clips:
                    changes.append(ClipChange(cid, "added", f"{cid} added ({c.moment_id}, {_sec(c.duration_frames, fps):.2f} s)"))
                    continue
                _, _, oc = old_clips[cid]
                if oc.moment_id != c.moment_id:
                    changes.append(ClipChange(cid, "swapped", f"{cid}: moment {oc.moment_id} -> {c.moment_id}"))
                if oc.duration_frames != c.duration_frames:
                    changes.append(ClipChange(cid, "trimmed", f"{cid}: {_sec(oc.duration_frames, fps):.2f} s -> {_sec(c.duration_frames, fps):.2f} s"
                                              + (f" (source {_secs(c.source_in_seconds)}-{_secs(c.source_out_seconds)} s)" if c.source_in_seconds is not None else "")))
                elif oc.anchor != c.anchor or (oc.source_in_frame, oc.source_out_frame) != (c.source_in_frame, c.source_out_frame):
                    changes.append(ClipChange(cid, "anchor", f"{cid}: anchor {oc.anchor} -> {c.anchor} (source {_secs(c.source_in_seconds)}-{_secs(c.source_out_seconds)} s)"))
                if (oc.transition_in.type, oc.transition_in.duration_frames) != (c.transition_in.type, c.transition_in.duration_frames):
                    changes.append(ClipChange(cid, "transition", f"{cid}: {_tr(oc, fps)} -> {_tr(c, fps)} into this clip"))
                if oc.audio_policy != c.audio_policy:
                    changes.append(ClipChange(cid, "sound", f"{cid}: sound {oc.audio_policy} -> {c.audio_policy}"))
            if surviving_old != surviving_new:
                moved = [cid for cid in surviving_new if surviving_new.index(cid) != surviving_old.index(cid)]
                changes.append(ClipChange(moved[0], "moved", "order of the clips: " + ", ".join(surviving_old) + " -> " + ", ".join(surviving_new)))
        before = _sec(o.duration_frames, fps) if o is not None else 0.0
        if changes or abs(before - _sec(s.duration_frames, fps)) > 1e-9:
            scenes.append(SceneDiff(s.scene_id, s.title, before, _sec(s.duration_frames, fps), changes))
    old_t = {t.title_id: t for t in old.titles}
    tchanges = []
    for t in new.titles:
        ot = old_t.get(t.title_id)
        if ot is not None and (ot.start_frame, ot.duration_frames) != (t.start_frame, t.duration_frames):
            tchanges.append(f"{t.title_id} ({t.text!r}) moves from {_sec(ot.start_frame, fps):.2f} s to {_sec(t.start_frame, fps):.2f} s")
    return PlanDiff(old.plan_hash, new.plan_hash, scenes, _sec(old.total_duration_frames, fps), _sec(new.total_duration_frames, fps), tchanges)


def _secs(v: float | None) -> str:
    return "?" if v is None else f"{v:.2f}"


def _tr(c: Any, fps: Fraction) -> str:
    t = c.transition_in
    return "cut" if t.type == "cut" else f"dissolve {_sec(t.duration_frames, fps):.2f} s"


def render_ops_markdown(applied: Sequence[AppliedOp], refused: Sequence[RefusedOp]) -> list[str]:
    out: list[str] = []
    for a in applied:
        out.append(f"- applied: {a.op.describe()}" + (f" ({a.op.reason})" if a.op.reason else ""))
    for r in refused:
        out.append(f"- refused: {r.op.describe() if r.op else 'malformed change'}: {r.message}")
    return out


__all__ = ["OPERATIONS", "ANCHORS", "AUDIO_MODES", "ApplyResult", "AppliedOp", "ClipChange", "OpError", "OpRefused", "PlanBaseError", "PlanContext", "PlanDiff",
           "PlanOp", "RefusedOp", "SceneDiff", "apply_ops", "check_roundtrip", "diff_plans", "layout_signature", "load_selection_file",
           "render_ops_markdown", "selection_from_edit"]
