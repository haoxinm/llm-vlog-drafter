"""LLM clip selection (DESIGN §8.2, §8.3, §9.3, §11.4): approved outline + catalog records -> selection plan -> edit plan.

The model sees the approved outline (scenes, roles, budgets, candidate moments), the compact records of exactly those
moments (with how much of each may be used) and the project rules, and returns clips per scene: moment, target duration,
anchor, audio policy, transition intent. Code does the rest: it assigns the clip ids, builds the `SelectionPlan`, resolves it
to exact frames with the existing resolver and validates both plans.

Feedback loop (§9.5, §11.4). Each reply is first checked read-only (``repair=False``):
  1. every problem the model can fix is an error: unknown or not-offered moments, excluded moments, durations longer than the
     moment, scene or total budgets out of tolerance, repeated moments without a reason, transitions longer than their clips,
     invented title text, and whatever the generic validators of `planning.validate` find;
  2. errors go back to the model once (``planning.selection_retries``, default 1) together with its previous reply;
  3. the last reply is then repaired deterministically, only where that is unambiguous, and every repair is recorded: invalid
     clips are dropped, durations clamped into the moment's bounds, transitions shortened, scene and total durations scaled
     proportionally within each moment's bounds (never beyond a moment, never below one second), invalid titles dropped;
  4. what is still wrong after that fails the stage with the findings. Nothing ambiguous is changed silently.
Always applied (recorded, but not worth a second request): the outline's scene order, the exact outline wording of a scene title
the model capitalised differently, a cut that carries a duration.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.types import LLMRequest, TextPart, Usage
from trip_vlog.llm.untrusted import PREAMBLE, untrusted_block
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.duration import frames_to_float, parse_fps, seconds_to_frames
from trip_vlog.planning.models import (SCHEMA_VERSION, TITLE_MAX_CHARS, TITLE_MAX_SECONDS, TITLE_MIN_SECONDS, EditPlan, SelectionClip, SelectionPlan,
                                       SelectionScene, SelectionTitle, SelectionTransition, TitleStart)
from trip_vlog.planning.outline import StoryOutline, findings_text, reask_parts
from trip_vlog.planning.outline_catalog import OutlineCatalog, OutlineMoment
from trip_vlog.planning.resolve_plan import PlanResolutionError, PlanSettings, resolve_selection_plan
from trip_vlog.planning.titles import TitleSettings, classify_text, default_titles, normalize_text, text_problems, title_id_for
from trip_vlog.planning.validate import (ValidationReport, ValidationSettings, validate_edit_plan, validate_selection_plan)
from trip_vlog.prompts import PromptTemplate, load_template
from trip_vlog.state import stable_hash

SYSTEM_TEMPLATE = "timeline_planner"
INPUT_TEMPLATE = "timeline_planner_input"
RESPONSE_SCHEMA_VERSION = "1"
MIN_CLIP_SECONDS = 1.0          # shortest clip the planner may use (or the whole moment when it is shorter)
MAX_STILL_SECONDS = 10.0        # longest a photo is held when durations are scaled
MAX_CLIPS_PER_SCENE = 500
MAX_SCENES = 200
MAX_TITLES_SCHEMA = 50
DISSOLVE_MIN_SECONDS = 0.1       # a dissolve shortened below this becomes a cut
MAX_TRANSITION_SECONDS = 5.0     # limits of the selection plan that the response schema leaves out (see response_schema)
MAX_TITLE_OFFSET_SECONDS = 600.0
MAX_RATIONALE = 400
MAX_TITLE_RATIONALE = 300
MAX_UNCERTAINTY = 500


class SelectionError(RuntimeError):
    """The planner could not produce a valid plan. `report` holds the last findings, `history` every attempt's."""

    def __init__(self, message: str, report: ValidationReport | None = None, history: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.report = report
        self.history = history or []


# ---- prompts ----------------------------------------------------------------------------------------------------------

def templates() -> tuple[PromptTemplate, PromptTemplate]:
    return load_template(SYSTEM_TEMPLATE), load_template(INPUT_TEMPLATE)


def prompt_info() -> dict[str, Any]:
    s, i = templates()
    return {"system": {"id": s.id, "version": s.version, "sha256": s.sha256}, "input": {"id": i.id, "version": i.version, "sha256": i.sha256},
            "response_schema": RESPONSE_SCHEMA_VERSION}


# ---- response schema (strict-mode friendly: every property required, nullable instead of optional) --------------------

def response_schema() -> dict[str, Any]:
    """Cosmetic limits (text length, number ranges) are not part of this schema: providers ignore them and the gateway would fail the
    whole stage over one long sentence. They are said in the descriptions; `build_work` / `check_work` shorten and clamp what comes back."""
    clip = {
        "moment_id": {"type": "string", "description": "A candidate moment of this scene, copied exactly from its 'id' field."},
        "target_duration_seconds": {"type": "number", "description": "How long the clip plays, in seconds; more than 0 and never more than the moment's 'use' value."},
        "anchor": {"type": "string", "enum": ["start", "center", "end", "best"], "description": "Where inside the moment the clip is taken."},
        "track_role": {"type": "string", "enum": ["primary_video", "overlay"], "description": "primary_video for the story; overlay is rarely needed."},
        "audio_policy": {"type": "string", "enum": ["natural_sound", "ambient_low", "muted"], "description": "What happens to the clip's own sound."},
        "transition_in": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["cut", "dissolve"]},
            "duration_seconds": {"type": "number", "description": "0 for a cut; 0.3 to 1.0 for a dissolve (never more than 5)."}},
            "required": ["type", "duration_seconds"], "additionalProperties": False, "description": "How the clip begins."},
        "rationale": {"type": "string", "description": f"One short sentence on why the clip is there (at most {MAX_RATIONALE} characters)."},
        "intentional_repeat_reason": {"type": ["string", "null"], "description": "Only when a moment is deliberately used again; otherwise null."},
    }
    start = {
        "relative_to": {"type": "string", "enum": ["scene_start", "clip_start"], "description": "Start at the scene's first frame or at one of its clips."},
        "clip_number": {"type": ["integer", "null"], "description": "For clip_start: the clip's position in the scene's clip list, counted from 1; else null."},
        "offset_seconds": {"type": "number", "description": f"Seconds after that point (0 to {MAX_TITLE_OFFSET_SECONDS:g})."},
    }
    title = {
        "scene_id": {"type": "string", "description": "The scene the title belongs to."},
        "text": {"type": "string", "description": f"Exactly the outline's title of that scene (at most {TITLE_MAX_CHARS} characters, one line)."},
        "start": {"type": "object", "properties": start, "required": list(start), "additionalProperties": False},
        "duration_seconds": {"type": "number", "description": f"{TITLE_MIN_SECONDS:g} to {TITLE_MAX_SECONDS:g} seconds."},
        "rationale": {"type": "string", "description": f"At most {MAX_TITLE_RATIONALE} characters."},
    }
    scene = {
        "scene_id": {"type": "string", "description": "The outline's scene_id, exactly."},
        "clips": {"type": "array", "minItems": 1, "description": "Clips in playing order (at most 500).",
                  "items": {"type": "object", "properties": clip, "required": list(clip), "additionalProperties": False}},
    }
    props = {
        "scenes": {"type": "array", "minItems": 1, "description": "One entry per outline scene, in the outline's order.",
                   "items": {"type": "object", "properties": scene, "required": list(scene), "additionalProperties": False}},
        "titles": {"type": "array", "description": "Scene titles, only when enabled; otherwise empty.",
                   "items": {"type": "object", "properties": title, "required": list(title), "additionalProperties": False}},
        "uncertainties": {"type": "array", "items": {"type": "string"},
                          "description": f"Things the user should check in the result (up to 100, each at most {MAX_UNCERTAINTY} characters)."},
    }
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"https://trip-vlog.local/schemas/timeline_planner_response:{RESPONSE_SCHEMA_VERSION}",
            "title": "timeline_planner_response", "description": "Timeline planner reply: clips per outline scene (moment, duration, anchor, sound, "
            "transition) and optional scene titles. No timestamps and no editor commands; clip ids are assigned by code.",
            "type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def write_response_schema(directory: Any) -> Any:
    from pathlib import Path

    from trip_vlog.state import atomic_write_json

    p = Path(directory) / "timeline_planner_response.schema.json"
    atomic_write_json(p, response_schema())
    return p


# ---- context ----------------------------------------------------------------------------------------------------------

@dataclass
class SelectionContext:
    config: Any
    outline: StoryOutline
    oc: OutlineCatalog
    view: PlanningCatalog
    vset: ValidationSettings
    pset: PlanSettings
    tset: TitleSettings
    capabilities: list[str]
    budgets: list[float]                  # effective scene budgets (outline budgets, scaled to the target when they were far off)
    budget_note: str | None = None
    min_still_seconds: float = 1.5
    _limits: dict[str, float | None] = field(default_factory=dict)

    @property
    def fps(self):
        return parse_fps(self.pset.frame_rate)

    def limit(self, moment_id: str) -> float | None:
        """Longest clip (seconds) that fits in the moment; None for photos (any length)."""
        if moment_id not in self._limits:
            self._limits[moment_id] = moment_limit(self.view, moment_id, self.fps)
        return self._limits[moment_id]


def moment_limit(view: PlanningCatalog, moment_id: str, fps) -> float | None:
    m = view.moments.get(moment_id)
    a = view.assets.get(m.asset_id) if m else None
    if m is None or a is None or a.media_type == "image" or m.source_in_seconds is None or m.source_out_seconds is None:
        return None
    m_in = max(0, seconds_to_frames(m.source_in_seconds, fps, "ceil"))
    m_out = seconds_to_frames(m.source_out_seconds, fps, "floor")
    if a.duration_seconds is not None:
        m_out = min(m_out, seconds_to_frames(a.duration_seconds, fps, "floor"))
    return max(0.0, frames_to_float(max(0, m_out - m_in), fps))


def effective_budgets(outline: StoryOutline, target: float, tolerance: float) -> tuple[list[float], str | None]:
    """The outline's scene budgets, scaled proportionally to the target when they add up to more than `tolerance` away from it
    (a hand-edited outline can do that). Otherwise unchanged."""
    raw = [float(s.target_duration_seconds) for s in outline.scenes]
    total = sum(raw)
    if total <= 0 or abs(total - target) <= tolerance:
        return raw, None
    f = target / total
    out = [max(0.5, round(b * f, 2)) for b in raw]
    return out, (f"The outline's scene budgets add up to {total:g} s but the target is {target:g} s; the budgets were scaled by {f:.3f} "
                 f"({sum(out):g} s).")


# ---- request ----------------------------------------------------------------------------------------------------------

def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def settings_text(ctx: SelectionContext) -> str:
    p = ctx.config.project
    return "\n".join([
        f"- title: {_q(p.title)}", f"- target duration: {ctx.vset.target_duration_seconds:g} seconds in total "
        f"(tolerance {ctx.vset.total_tolerance_seconds():g} s)",
        f"- story mode: {p.story_mode}", f"- style: {_q(p.style)}", f"- audience: {_q(p.audience)}", f"- language of titles and notes: {p.language}",
        f"- aspect ratio {p.aspect_ratio}, {p.frame_rate} frames per second", f"- photos stay on screen for about {ctx.config.media.still_duration_seconds:g} s"])


def constraints_text(ctx: SelectionContext) -> str:
    e = ctx.config.editorial
    oc = ctx.oc
    lines = ["- Use only the candidate moments listed under each scene, and only the scene's own."]
    # References are paths, folders or globs from the user's own layout: the request says "required item N" and names the moments
    # it resolves to; the references themselves appear only in local reports.
    for ref, ids in oc.must_include.items():
        if ids:
            shown = ", ".join(ids[:8]) + (f" (+{len(ids) - 8} more)" if len(ids) > 8 else "")
            lines.append(f"- {oc.ref_label(ref)}: use at least one of these moments: {shown}")
    if e.must_exclude:
        lines.append(f"- {len(e.must_exclude)} exclusion rule(s) were applied; the excluded moments are already removed from the candidates")
    lines.append(f"- at most {e.max_similar_shots_per_scene} visually similar shots (same dup value, same place) in one scene")
    if e.avoid_excessive_transitions:
        lines.append("- keep transitions rare: hard cuts by default")
    if e.avoid_fabricated_context:
        lines.append("- do not add context (places, names, reasons) the catalog does not show; put open questions in uncertainties")
    tol = ctx.vset
    lines.append(f"- a scene's primary_video clips total its budget within max({tol.scene_budget_tolerance_pct:g}%, {tol.scene_budget_tolerance_seconds:g} s)")
    if ctx.budget_note:
        lines.append(f"- {ctx.budget_note}")
    return "\n".join(lines)


def _record(m: OutlineMoment, ctx: SelectionContext) -> str:
    h = m.header()
    h.pop("n", None)
    lim = ctx.limit(m.moment_id)
    if lim is not None:
        h["use"] = round(lim, 2)
    pm = ctx.view.moments.get(m.moment_id)
    if pm is not None and pm.best_in_seconds is not None and pm.best_out_seconds is not None and pm.best_out_seconds > pm.best_in_seconds:
        h["best"] = round(pm.best_out_seconds - pm.best_in_seconds, 2)
    head = json.dumps(h, ensure_ascii=False, separators=(",", ":"))
    block = m.text_block()
    return head if block is None else head + "\n" + block


def scene_candidates(ctx: SelectionContext, si: int) -> list[OutlineMoment]:
    return [m for mid in ctx.outline.scenes[si].candidate_moment_ids if (m := ctx.oc.eligible.get(mid.strip())) is not None]


def candidate_digest(ctx: SelectionContext) -> str:
    """Identity of exactly the catalog records the planner sees (a changed label, speech excerpt or length changes it)."""
    return stable_hash(sorted({_record(m, ctx) for si in range(len(ctx.outline.scenes)) for m in scene_candidates(ctx, si)}))


def outline_text(ctx: SelectionContext) -> str:
    o = ctx.outline
    parts = [untrusted_block("outline_premise", f"premise: {o.premise}\norder: {o.story_mode_used}", max_chars=2000)]
    tol = ctx.vset
    for si, sc in enumerate(o.scenes):
        budget = ctx.budgets[si]
        tol_s = max(budget * tol.scene_budget_tolerance_pct / 100, tol.scene_budget_tolerance_seconds)
        meta = f"title: {sc.title}\npurpose: {sc.purpose}\ntone: {sc.tone}" + (f"\nnote: {sc.chronology_note}" if sc.chronology_note else "")
        recs = [_record(m, ctx) for m in scene_candidates(ctx, si)]
        parts.append(f"### {sc.scene_id} | role: {sc.role} | budget: {budget:g} s (primary_video clips total {budget:g} s, within {tol_s:.1f} s)\n"
                     f"{untrusted_block('outline_scene', meta, max_chars=2000)}\n"
                     f"Candidates ({len(recs)}), in the outline's order:\n" + ("\n".join(recs) if recs else "(none usable)"))
    if o.hook.used and o.hook.moment_id:
        parts.append(f"The opening hook of scene 1 is the moment {o.hook.moment_id}; keep that scene short.")
    return "\n\n".join(parts)


def user_text(ctx: SelectionContext) -> str:
    _, tpl = templates()
    values = {"settings": settings_text(ctx), "constraints": constraints_text(ctx), "capabilities": "\n".join(ctx.capabilities),
              "scene_count": str(len(ctx.outline.scenes)), "untrusted_preamble": PREAMBLE, "outline": outline_text(ctx),
              "target_seconds": f"{ctx.vset.target_duration_seconds:g}", "tolerance_seconds": f"{ctx.vset.total_tolerance_seconds():g}"}
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m[1], m[0]), tpl.text)  # one pass: media text is never mistaken for a placeholder


def max_tokens(config: Any) -> int:
    _, ep = config.llm.endpoint_for("text")
    return max(256, min(config.planning.selection_max_tokens, ep.max_tokens))


def build_request(ctx: SelectionContext, extra: tuple[str, ...] = ()) -> LLMRequest:
    system, _ = templates()
    parts = [TextPart(user_text(ctx)), *(TextPart(t) for t in extra)]
    return LLMRequest.from_template(system, parts, schema=response_schema(), max_tokens=max_tokens(ctx.config))


def estimate_input_tokens(ctx: SelectionContext) -> int:
    system, _ = templates()
    return (len(system.text) + len(user_text(ctx)) + 3) // 4


# ---- duration fitting -------------------------------------------------------------------------------------------------

def _floor2(x: float) -> float:
    return math.floor(x * 100 + 1e-6) / 100


def fit_total(durations: list[float], mins: list[float], maxs: list[float], target: float) -> list[float]:
    """Scale `durations` proportionally so that they add up to `target`, each kept inside [min, max]. A clip that hits a bound
    stays there and the others take the rest. Best effort when `target` lies outside what the bounds allow. Two decimals."""
    n = len(durations)
    if n == 0:
        return []
    lo = [min(a, b) for a, b in zip(mins, maxs)]
    d = list(durations)
    free = set(range(n))
    for _ in range(n + 2):
        fixed = sum(d[i] for i in range(n) if i not in free)
        base = sum(durations[i] for i in free)
        if not free or base <= 0:
            break
        f = (target - fixed) / base
        clamped = False
        for i in sorted(free):
            v = durations[i] * f
            if v < lo[i] - 1e-9:
                d[i], clamped = lo[i], True
                free.discard(i)
            elif v > maxs[i] + 1e-9:
                d[i], clamped = maxs[i], True
                free.discard(i)
        if not clamped:
            for i in free:
                d[i] = durations[i] * f
            break
    out = [min(max(round(v, 2), lo[i]), maxs[i]) for i, v in enumerate(d)]
    drift = round(target - sum(out), 2)
    if abs(drift) >= 0.01:   # rounding: let the clips that still have room take it, largest first
        for i in sorted(range(n), key=lambda i: -out[i]):
            room = (maxs[i] - out[i]) if drift > 0 else (out[i] - lo[i])
            step = min(abs(drift), max(0.0, room))
            if step >= 0.01:
                new = min(max(round(out[i] + math.copysign(step, drift), 2), lo[i]), maxs[i])   # rounding must not pass a bound
                drift = round(drift - (new - out[i]), 2)
                out[i] = new
            if abs(drift) < 0.01:
                break
    return out


# ---- the working copy of a reply --------------------------------------------------------------------------------------

def _label(sc: dict[str, Any]) -> str:
    """Findings go back to the model: the label carries the scene id only, no text taken from the outline or the media."""
    return f"scene {sc['scene_id']}"


def _shorten(value: Any, limit: int, path: str, report: ValidationReport) -> Any:
    """Free text longer than the plan allows is cut (a recorded repair, not a reason to fail the stage; the response schema has no length limits)."""
    if value is None or isinstance(value, str) and len(value) <= limit:
        return value
    text = str(value)
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    report.repaired("text_shortened", f"{path} had {len(text)} characters, the limit is {limit}; shortened", len(text), len(cut), path)
    return cut


def build_work(parsed: Any, ctx: SelectionContext, report: ValidationReport) -> dict[str, Any]:
    """Reply -> working copy aligned with the outline: one entry per outline scene (in outline order), clips as plain dicts."""
    o = ctx.outline
    order = [s.scene_id for s in o.scenes]
    got: dict[str, dict[str, Any]] = {}
    seen_order: list[str] = []
    for i, rs in enumerate(parsed.get("scenes") or []):
        sid = str(rs.get("scene_id", "")).strip()
        if sid not in order:
            report.error("unknown_scene", f"the reply has a scene {sid!r} that is not in the outline (scene ids: {', '.join(order)})", f"scenes[{i}].scene_id")
        elif sid in got:
            report.error("duplicate_scene", f"the reply lists scene {sid} more than once", f"scenes[{i}].scene_id")
        else:
            got[sid] = rs
            seen_order.append(sid)
    known = [s for s in order if s in got]
    if seen_order != known:
        report.repaired("scene_order", "the scenes were not in the outline's order; reordered", seen_order, known, "scenes")
    scenes = []
    for si, osc in enumerate(o.scenes):
        rs = got.get(osc.scene_id)
        if rs is None:
            report.error("scene_missing", f"scene {osc.scene_id} of the outline has no clips in the reply", "scenes")
            continue
        clips = []
        raw_clips = rs.get("clips") or []
        if len(raw_clips) > MAX_CLIPS_PER_SCENE:
            report.repaired("list_shortened", f"scenes[{si}] has {len(raw_clips)} clips, the limit is {MAX_CLIPS_PER_SCENE}; the rest was dropped",
                            len(raw_clips), MAX_CLIPS_PER_SCENE, f"scenes[{si}].clips")
        for n, c in enumerate(raw_clips[:MAX_CLIPS_PER_SCENE], 1):
            d = copy.deepcopy(c)
            d["moment_id"] = str(d.get("moment_id", "")).strip()
            d["_n"] = n          # the position in the model's own list: titles refer to it (clips may be dropped later)
            for key, limit in (("rationale", MAX_RATIONALE), ("intentional_repeat_reason", MAX_RATIONALE)):
                d[key] = _shorten(d.get(key), limit, f"scenes[{si}].clips[{n - 1}].{key}", report)
            clips.append(d)
        scenes.append({"scene_id": osc.scene_id, "title": osc.title, "purpose": osc.purpose, "budget": ctx.budgets[si],
                       "candidates": [m.strip() for m in osc.candidate_moment_ids], "clips": clips,
                       "n_original": min(len(raw_clips), MAX_CLIPS_PER_SCENE)})
    titles = []
    for i, t in enumerate(parsed.get("titles") or []):
        d = copy.deepcopy(t)
        d["scene_id"] = str(d.get("scene_id", "")).strip()
        d["_i"] = i
        d["rationale"] = _shorten(d.get("rationale"), MAX_TITLE_RATIONALE, f"titles[{i}].rationale", report)
        titles.append(d)
    uncertainties = [_shorten(u, MAX_UNCERTAINTY, f"uncertainties[{i}]", report) for i, u in enumerate(parsed.get("uncertainties") or [])]
    return {"scenes": scenes, "titles": titles, "uncertainties": [u for u in uncertainties if u is not None], "notes": []}


def _primaries(work: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for sc in work["scenes"] for c in sc["clips"] if c["track_role"] == "primary_video"]


def _bounds(c: dict[str, Any], ctx: SelectionContext) -> tuple[float, float]:
    lim = ctx.limit(c["moment_id"])
    if lim is None:
        return ctx.min_still_seconds, MAX_STILL_SECONDS
    return min(MIN_CLIP_SECONDS, lim), lim


def check_work(work: dict[str, Any], ctx: SelectionContext, report: ValidationReport, *, repair: bool) -> None:
    """Deterministic checks of the reply against the outline, the catalog and the budgets (see the module docstring). With
    ``repair`` the unambiguous problems are fixed and recorded; without it every one is an error."""
    oc = ctx.oc
    one_frame = float(1 / ctx.fps)

    def problem(code: str, msg: str, path: str, fix: str, before: Any = None, after: Any = None) -> bool:
        """Record a problem; True when the caller should apply its fix."""
        if repair:
            report.repaired(code, f"{msg}; {fix}", before, after, path)
            return True
        report.error(code, msg, path)
        return False

    # ---- clips: references, durations, repeats
    used: dict[str, str] = {}
    for si, sc in enumerate(work["scenes"]):
        cand = set(sc["candidates"])
        kept = []
        for c in sc["clips"]:
            path = f"scenes[{si}].clips[{c['_n'] - 1}]"
            mid = c["moment_id"]
            bad = None
            if mid not in oc.all_ids:
                bad = ("unknown_moment", f"{_label(sc)}: {mid!r} is not a moment in the catalog")
            elif mid in oc.excluded_ids:
                bad = ("excluded_moment", f"{_label(sc)}: {mid} matches a must_exclude rule")
            elif mid not in oc.eligible:
                bad = ("unavailable_moment", f"{_label(sc)}: {mid} belongs to a missing, unreadable or audio-only asset")
            elif mid not in cand:
                bad = ("moment_not_in_scene", f"{_label(sc)}: {mid} is not one of this scene's candidate moments (use only the ones listed for it)")
            if bad is None and mid in used and not (c.get("intentional_repeat_reason") or "").strip():
                bad = ("unexplained_repeat", f"{_label(sc)}: {mid} is already used by clip {used[mid]} and has no intentional_repeat_reason")
            if bad is not None:
                if problem(bad[0], bad[1], path, "the clip was removed", mid, None):
                    continue
            else:
                lim = ctx.limit(mid)
                t = float(c["target_duration_seconds"])
                if lim is not None:
                    lo = min(MIN_CLIP_SECONDS, lim)
                    if t > lim + one_frame:
                        if problem("duration_exceeds_moment", f"{_label(sc)}: clip of {mid} asks for {t:g} s but at most {lim:.2f} s can be used",
                                   path, f"shortened to {_floor2(lim):g} s", t, _floor2(lim)):
                            c["target_duration_seconds"] = _floor2(lim)
                    elif t < lo - 1e-9:
                        if problem("clip_too_short", f"{_label(sc)}: clip of {mid} is {t:g} s, the minimum is {lo:g} s", path, f"lengthened to {lo:g} s", t, lo):
                            c["target_duration_seconds"] = lo
                else:
                    if t < ctx.min_still_seconds - 1e-9:
                        if problem("still_too_short", f"{_label(sc)}: photo {mid} is shown for {t:g} s, the minimum is {ctx.min_still_seconds:g} s", path,
                                   f"lengthened to {ctx.min_still_seconds:g} s", t, ctx.min_still_seconds):
                            c["target_duration_seconds"] = ctx.min_still_seconds
                    elif t > MAX_STILL_SECONDS + 1e-9:
                        if problem("still_too_long", f"{_label(sc)}: photo {mid} is shown for {t:g} s, the maximum is {MAX_STILL_SECONDS:g} s", path,
                                   f"shortened to {MAX_STILL_SECONDS:g} s", t, MAX_STILL_SECONDS):
                            c["target_duration_seconds"] = MAX_STILL_SECONDS
                used.setdefault(mid, f"{sc['scene_id']}#{c['_n']}")
            kept.append(c)
        sc["clips"] = kept

    # ---- budgets
    scene_tol = lambda b: max(b * ctx.vset.scene_budget_tolerance_pct / 100, ctx.vset.scene_budget_tolerance_seconds)  # noqa: E731
    for si, sc in enumerate(work["scenes"]):
        prim = [c for c in sc["clips"] if c["track_role"] == "primary_video"]
        if not prim:
            report.error("no_primary_clip", f"{_label(sc)} has no usable primary_video clip", f"scenes[{si}].clips")
            continue
        total = round(sum(float(c["target_duration_seconds"]) for c in prim), 3)
        if abs(total - sc["budget"]) > scene_tol(sc["budget"]) + 1e-9:
            msg = f"{_label(sc)}: clips total {total:g} s, the budget is {sc['budget']:g} s (tolerance {scene_tol(sc['budget']):.1f} s)"
            if not repair:
                report.error("scene_budget_off", msg, f"scenes[{si}].clips")
                continue
            _rescale(prim, ctx, sc["budget"])
            after = round(sum(float(c["target_duration_seconds"]) for c in prim), 3)
            report.repaired("scene_rescaled", f"{msg}; clip durations scaled within their moments to {after:g} s", total, after, f"scenes[{si}].clips")
            if abs(after - sc["budget"]) > scene_tol(sc["budget"]) + 1e-9:
                report.warn("scene_budget_unmet", f"{_label(sc)}: only {after:g} s of the {sc['budget']:g} s budget can be filled from the chosen moments",
                            f"scenes[{si}].clips")
    prim_all = _primaries(work)
    total = round(sum(float(c["target_duration_seconds"]) for c in prim_all), 3)
    target, tol = ctx.vset.target_duration_seconds, ctx.vset.total_tolerance_seconds()
    if prim_all and abs(total - target) > tol + 1e-9:
        msg = f"the primary_video clips total {total:g} s, the target is {target:g} s (tolerance {tol:g} s)"
        if not repair:
            report.error("total_duration_off", msg, "scenes")
        else:
            _rescale(prim_all, ctx, target)
            after = round(sum(float(c["target_duration_seconds"]) for c in prim_all), 3)
            report.repaired("total_rescaled", f"{msg}; all clip durations scaled within their moments to {after:g} s", total, after, "scenes")
            if abs(after - target) > tol + 1e-9:
                report.error("total_duration_unreachable", f"the chosen moments can only fill {after:g} s of the {target:g} s target "
                             f"(tolerance {tol:g} s): there is not enough usable footage in the outline's candidates", "scenes")

    # ---- transitions (after the durations are final)
    prev: dict[str, Any] | None = None
    for si, sc in enumerate(work["scenes"]):
        for c in sc["clips"]:
            t = c["transition_in"]
            path = f"scenes[{si}].clips[{c['_n'] - 1}].transition_in"
            if t["type"] == "cut" and t["duration_seconds"]:
                report.repaired("cut_duration", f"{_label(sc)}: a cut cannot have a duration ({t['duration_seconds']:g} s); set to 0", t["duration_seconds"], 0.0, path)
                t["duration_seconds"] = 0.0
            if t["type"] == "dissolve" and t["duration_seconds"] > MAX_TRANSITION_SECONDS:
                if problem("transition_too_long", f"{_label(sc)}: a {t['duration_seconds']:g} s dissolve; the longest is {MAX_TRANSITION_SECONDS:g} s", path,
                           f"shortened to {MAX_TRANSITION_SECONDS:g} s", t["duration_seconds"], MAX_TRANSITION_SECONDS):
                    t["duration_seconds"] = MAX_TRANSITION_SECONDS
            if t["type"] == "dissolve" and c["track_role"] == "primary_video":
                limit = None if prev is None else min(float(prev["target_duration_seconds"]), float(c["target_duration_seconds"]))
                if t["duration_seconds"] <= 0:
                    report.repaired("dissolve_without_duration", f"{_label(sc)}: a dissolve without duration became a cut", t["duration_seconds"], 0.0, path)
                    t["type"], t["duration_seconds"] = "cut", 0.0
                elif limit is not None and t["duration_seconds"] > limit + 1e-9:
                    new = _floor2(limit)
                    msg = (f"{_label(sc)}: a {t['duration_seconds']:g} s dissolve is longer than the shorter adjacent clip ({limit:g} s)")
                    if problem("transition_too_long", msg, path, f"shortened to {new:g} s" if new >= DISSOLVE_MIN_SECONDS else "changed to a cut",
                               t["duration_seconds"], new):
                        if new >= DISSOLVE_MIN_SECONDS:
                            t["duration_seconds"] = new
                        else:
                            t["type"], t["duration_seconds"] = "cut", 0.0
            if c["track_role"] == "primary_video":
                prev = c
    n_dissolves = sum(1 for c in prim_all if c["transition_in"]["type"] == "dissolve")
    if ctx.config.editorial.avoid_excessive_transitions and n_dissolves * 4 > max(1, len(prim_all)):
        report.warn("many_transitions", f"{n_dissolves} of {len(prim_all)} clips begin with a dissolve (editorial.avoid_excessive_transitions)")

    # ---- similar shots
    for si, sc in enumerate(work["scenes"]):
        groups: dict[str, int] = {}
        for c in sc["clips"]:
            m = oc.eligible.get(c["moment_id"])
            if m is not None and m.duplicate_group:
                groups[m.duplicate_group] = groups.get(m.duplicate_group, 0) + 1
        for g, n in groups.items():
            if n > ctx.config.editorial.max_similar_shots_per_scene:
                report.warn("similar_shots", f"{_label(sc)}: {n} clips come from the near-duplicate group {g} "
                            f"(editorial.max_similar_shots_per_scene is {ctx.config.editorial.max_similar_shots_per_scene})")

    _check_titles(work, ctx, report, repair)


def _rescale(clips: list[dict[str, Any]], ctx: SelectionContext, target: float) -> None:
    if not clips:
        return
    bounds = [_bounds(c, ctx) for c in clips]
    new = fit_total([float(c["target_duration_seconds"]) for c in clips], [b[0] for b in bounds], [b[1] for b in bounds], target)
    for c, v in zip(clips, new):
        c["target_duration_seconds"] = v


def _check_titles(work: dict[str, Any], ctx: SelectionContext, report: ValidationReport, repair: bool) -> None:
    ts = ctx.tset
    scenes = {sc["scene_id"]: sc for sc in work["scenes"]}
    outline_scenes = {s.scene_id: s for s in ctx.outline.scenes}
    if work["titles"] and not ts.scene_titles:
        report.warn("titles_ignored", f"the reply has {len(work['titles'])} title(s) but editorial.scene_titles is off; they were ignored")
        work["titles"] = []
        return
    fixed = default_titles(ctx.outline.scenes[0].scene_id, ctx.outline.scenes[-1].scene_id, ts, [])
    room = max(0, min(ts.max_titles, MAX_TITLES_SCHEMA) - len(fixed))
    kept = []
    for t in work["titles"]:
        path = f"titles[{t['_i']}]"
        sc = scenes.get(t["scene_id"])
        why: tuple[str, str] | None = None
        t["text"] = str(t.get("text", ""))
        norm = normalize_text(t["text"])
        osc0 = outline_scenes.get(t["scene_id"])
        unusable = text_problems(normalize_text(osc0.title)) if osc0 is not None else []
        if unusable:
            # the outline's own title cannot be an on-screen title (e.g. longer than 60 characters; the outline allows 200): that is not
            # the planner's mistake and asking again cannot help, so the title is skipped, in either mode, and the reason recorded
            report.repaired("title_scene_title_unusable", f"title for scene {t['scene_id']} skipped: the outline's scene title is not usable as an "
                            f"on-screen title ({'; '.join(unusable)})", t["text"], None, path)
            continue
        if sc is None:
            why = ("title_scene_unknown", f"title {t['text']!r}: unknown scene_id {t['scene_id']!r}")
        elif text_problems(norm):
            why = ("title_text", f"title {t['text']!r}: " + "; ".join(text_problems(norm)))
        else:
            osc = outline_scenes[t["scene_id"]]
            found = classify_text(norm, "scene_title", osc, ts, set())
            if found is None:
                why = ("title_text_not_allowed",
                       f"title {t['text']!r} is not the outline's title of scene {osc.scene_id}; titles carry only the outline's scene "
                       "titles, never invented places, dates or facts")
            elif norm != normalize_text(osc.title):
                report.repaired("title_text_exact", f"title {t['text']!r} was written as {osc.title!r} (the outline's wording)", t["text"], osc.title, path)
                norm = normalize_text(osc.title)
            t["text"] = norm
        if why is None and sc is not None:
            st = t.get("start") or {}
            if st.get("relative_to") == "clip_start":
                n = st.get("clip_number")
                if isinstance(n, bool) or not isinstance(n, int) or not (1 <= n <= sc["n_original"]):
                    why = ("title_clip_unknown", f"title {t['text']!r}: clip_number {n!r} is not a clip of scene {sc['scene_id']} ({sc['n_original']} clips)")
                elif not any(c["_n"] == n for c in sc["clips"]):
                    # the number counts the model's own list; that clip was removed above, and the title must not slide onto the next one
                    why = ("title_clip_removed", f"title {t['text']!r}: clip {n} of scene {sc['scene_id']} was removed")
            off = float(st.get("offset_seconds") or 0.0)
            if why is None and not (0.0 <= off <= MAX_TITLE_OFFSET_SECONDS):
                new_off = min(max(off, 0.0), MAX_TITLE_OFFSET_SECONDS)
                msg = f"title {t['text']!r}: offset {off:g} s is outside 0-{MAX_TITLE_OFFSET_SECONDS:g} s"
                if repair:
                    report.repaired("title_offset", f"{msg}; set to {new_off:g} s", off, new_off, path)
                    t["start"] = {**st, "offset_seconds": new_off}
                else:
                    report.error("title_offset", msg, path)
            d = float(t["duration_seconds"])
            if why is None and not (TITLE_MIN_SECONDS - 1e-9 <= d <= TITLE_MAX_SECONDS + 1e-9):
                new = min(max(d, TITLE_MIN_SECONDS), TITLE_MAX_SECONDS)
                msg = f"title {t['text']!r}: {d:g} s is outside {TITLE_MIN_SECONDS:g}-{TITLE_MAX_SECONDS:g} s"
                if repair:
                    report.repaired("title_duration", f"{msg}; set to {new:g} s", d, new, path)
                    t["duration_seconds"] = new
                else:
                    report.error("title_duration", msg, path)
        if why is not None:
            if repair:
                report.repaired(why[0], why[1] + "; the title was dropped", t["text"], None, path)
            else:
                report.error(why[0], why[1], path)
            continue
        kept.append(t)
    if len(kept) > room:
        msg = f"{len(kept) + len(fixed)} titles were proposed, editorial.max_titles is {ts.max_titles}"
        if repair:
            report.repaired("too_many_titles", f"{msg}; the last {len(kept) - room} were dropped", len(kept) + len(fixed), ts.max_titles, "titles")
            kept = kept[:room]
        else:
            report.error("too_many_titles", msg, "titles")
    work["titles"] = kept


# ---- building the plan ------------------------------------------------------------------------------------------------

def build_selection(work: dict[str, Any], ctx: SelectionContext) -> SelectionPlan:
    n = 0
    scenes = []
    for sc in work["scenes"]:
        clips = []
        for c in sc["clips"]:
            n += 1
            c["id"] = f"clip_{n:03d}"
            t = c["transition_in"]
            clips.append(SelectionClip(
                clip_instance_id=c["id"], moment_id=c["moment_id"], target_duration_seconds=round(float(c["target_duration_seconds"]), 3),
                anchor=c["anchor"], track_role=c["track_role"], audio_policy=c["audio_policy"],
                transition_in=SelectionTransition(type=t["type"], duration_seconds=float(t["duration_seconds"])),
                rationale=str(c.get("rationale") or "")[:2000],
                intentional_repeat_reason=(str(c["intentional_repeat_reason"])[:2000] if c.get("intentional_repeat_reason") else None)))
        scenes.append(SelectionScene(scene_id=sc["scene_id"], title=sc["title"][:200], purpose=sc["purpose"][:500],
                                     target_duration_seconds=sc["budget"], clips=clips))
    titles: list[SelectionTitle] = []
    notes: list[str] = []
    fixed = default_titles(scenes[0].scene_id, scenes[-1].scene_id, ctx.tset, notes)
    opening = [t for t in fixed if t.role == "opening_title"]
    closing = [t for t in fixed if t.role == "closing"]
    scene_titles = []
    for t in work["titles"]:
        sc = next(s for s in work["scenes"] if s["scene_id"] == t["scene_id"])
        st = t["start"]
        clip_id = next(c["id"] for c in sc["clips"] if c["_n"] == st["clip_number"]) if st["relative_to"] == "clip_start" else None
        scene_titles.append(SelectionTitle(
            title_id="x", text=t["text"], role="scene_title", scene_id=t["scene_id"], text_source="outline_scene_title",
            start=TitleStart(relative_to=st["relative_to"], clip_instance_id=clip_id, offset_seconds=float(st.get("offset_seconds") or 0.0)),
            duration_seconds=float(t["duration_seconds"]), rationale=str(t.get("rationale") or "")[:500]))
    for t in [*opening, *scene_titles, *closing]:
        titles.append(t.model_copy(update={"title_id": title_id_for(len(titles) + 1)}))
    # remember the final id of every proposed title (for dropping one that overlaps another after resolution)
    for wt, st in zip(work["titles"], [t for t in titles if t.role == "scene_title"]):
        wt["_final_id"] = st.title_id
    uncertainties = [*work["uncertainties"], *work["notes"], *notes]
    if ctx.budget_note:
        uncertainties.append(ctx.budget_note)
    return SelectionPlan(schema_version=SCHEMA_VERSION, planner="llm", scenes=scenes, titles=titles, uncertainties=uncertainties[:100])


# ---- one evaluation of a reply ----------------------------------------------------------------------------------------

@dataclass
class Outcome:
    report: ValidationReport
    selection: SelectionPlan | None = None
    edit: EditPlan | None = None

    @property
    def ok(self) -> bool:
        return self.report.ok and self.selection is not None and self.edit is not None


def _merge(into: ValidationReport, other: ValidationReport) -> None:
    into.errors += [i for i in other.errors if i not in into.errors]
    into.warnings += [i for i in other.warnings if i not in into.warnings]
    into.repairs += [r for r in other.repairs if r not in into.repairs]


def _post_resolve_repair(work: dict[str, Any], ctx: SelectionContext, selection: SelectionPlan, edit: EditPlan, report: ValidationReport) -> bool:
    """Repairs for what only shows up once the plan is resolved. True when something changed (the caller resolves again)."""
    codes = set(report.codes("errors"))
    changed = False
    if "total_duration_out_of_tolerance" in codes and edit.total_duration_seconds > 0:
        prim = _primaries(work)
        sel_total = sum(float(c["target_duration_seconds"]) for c in prim)
        want = sel_total * ctx.vset.target_duration_seconds / edit.total_duration_seconds
        before = round(sel_total, 3)
        _rescale(prim, ctx, want)
        after = round(sum(float(c["target_duration_seconds"]) for c in prim), 3)
        if abs(after - before) >= 0.01:
            report.repaired("total_rescaled_after_resolve", f"the resolved plan lasts {edit.total_duration_seconds:g} s, the target is "
                            f"{ctx.vset.target_duration_seconds:g} s; clip durations scaled within their moments", before, after, "scenes")
            changed = True
    overlapping = [e for e in report.errors if e.code in ("title_overlap", "title_overlaps_overlay", "title_outside_sequence")]
    if overlapping:
        ordered = sorted(edit.titles, key=lambda t: (t.start_frame, t.title_id))
        drop: set[str] = set()
        end = -1
        for t in ordered:
            if t.start_frame < end:
                drop.add(t.title_id)
            else:
                end = t.start_frame + t.duration_frames
        for t in edit.titles:
            if any(c.track_role == "overlay" and t.start_frame < c.timeline_start_frame + c.duration_frames and
                   c.timeline_start_frame < t.start_frame + t.duration_frames for sc in edit.scenes for c in sc.clips):
                drop.add(t.title_id)
        mine = [t for t in work["titles"] if t.get("_final_id") in drop]
        for t in mine:
            report.repaired("title_dropped", f"title {t['text']!r} overlapped another title or an overlay clip on V2; dropped", t["text"], None, "titles")
            work["titles"].remove(t)
            changed = True
    return changed


def evaluate(parsed: Any, ctx: SelectionContext, *, repair: bool) -> Outcome:
    """One reply -> Outcome (selection + edit plan when there are no errors). `repair`: fix what can be fixed unambiguously."""
    report = ValidationReport()
    work = build_work(parsed, ctx, report)
    check_work(work, ctx, report, repair=repair)
    if report.errors:
        return Outcome(report)
    for _ in range(3 if repair else 1):
        pass_report = ValidationReport(repairs=list(report.repairs), warnings=list(report.warnings))
        try:
            sel = build_selection(work, ctx)
        except ValueError as e:   # a reply the models reject although the schema allowed it
            pass_report.error("schema", f"the reply cannot be turned into a selection plan: {e}")
            return Outcome(pass_report)
        sr = validate_selection_plan(sel, ctx.view, ctx.vset)
        _merge(pass_report, sr)
        if pass_report.errors:
            return Outcome(pass_report, sel)
        try:
            edit = resolve_selection_plan(sel, ctx.view, ctx.pset)
        except PlanResolutionError as e:
            for issue in e.issues:
                pass_report.error("unresolvable", issue)
            return Outcome(pass_report, sel)
        er = validate_edit_plan(edit, ctx.view, ctx.vset)
        _merge(pass_report, er)
        if not pass_report.errors:
            pass_report.plan = er.plan or edit
            return Outcome(pass_report, sel, er.plan or edit)
        if not repair or not _post_resolve_repair(work, ctx, sel, edit, pass_report):
            return Outcome(pass_report, sel, edit)
        report = ValidationReport(repairs=pass_report.repairs, warnings=pass_report.warnings)
    return Outcome(pass_report, sel, edit)


# ---- the loop ---------------------------------------------------------------------------------------------------------

@dataclass
class SelectionResult:
    selection: SelectionPlan
    edit: EditPlan
    report: ValidationReport
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    requests: int = 0
    cache_hits: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)


def generate_selection(gateway: Gateway, ctx: SelectionContext) -> SelectionResult:
    """Ask the planner, check the reply, re-ask once with the findings, then repair what is unambiguous (see the module docstring)."""
    if not ctx.outline.scenes:
        raise SelectionError("the outline has no scenes")
    retries = ctx.config.planning.selection_retries
    extra: tuple[str, ...] = ()
    usage, model, requests, hits = Usage(), "", 0, 0
    history: list[dict[str, Any]] = []
    out: Outcome | None = None
    for attempt in range(retries + 1):
        final = attempt == retries
        resp = gateway.complete(build_request(ctx, extra))
        requests += 0 if resp.cached else 1
        hits += int(resp.cached)
        usage = usage + (Usage() if resp.cached else resp.usage)
        model = resp.model or model
        out = evaluate(resp.parsed, ctx, repair=final)
        rep = out.report
        history.append({"attempt": attempt + 1, "fingerprint": resp.fingerprint, "cached": resp.cached, "repair_mode": final,
                        "errors": [i.to_dict() for i in rep.errors], "warnings": [i.to_dict() for i in rep.warnings],
                        "repairs": [r.to_dict() for r in rep.repairs]})
        if out.ok:
            assert out.selection is not None and out.edit is not None
            return SelectionResult(out.selection, out.edit, rep, model, usage, requests, hits, history)
        extra = reask_parts(resp.parsed, rep)
    assert out is not None
    # the stage failed: forget every reply of this chain, so `resume` asks again instead of replaying the same unusable answers
    gateway.discard([h["fingerprint"] for h in history])
    raise SelectionError("the planner's clip selection failed validation" + (" after a corrected reply was requested" if retries else "") + ":\n"
                         + findings_text(out.report), out.report, history)
