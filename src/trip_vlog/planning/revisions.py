"""Natural-language revisions (DESIGN §16): request, strict response schema, checks in code, confirmation policy, change summary. Pure: no workspace,
no FilmCraft; `revise_stages` wires it into the draft pipeline.

The person writes what they want ("make the beach scene shorter and drop the blurry sunset clip"). A model maps it onto the CLOSED set of operations of
`planning.plan_diff` (the model never edits plan JSON, never writes timestamps), naming clips and scenes by the ids of the compact plan it is sent. It
also lists what is unclear (`ambiguous`), what no operation can do (`unsupported`) and whether the change is destructive. Everything it says is checked:

  * an operation must parse (`PlanOp`, the revision set) and name ids that exist in the plan; the rest is refused with a reason (never silently dropped),
  * every operation is then applied through the resolver and the validators on the real plan (`apply_ops`): the plan has to stay valid, the film may not
    change length by more than ``revise.max_duration_change_pct`` and the exclusion rules still hold; one that fails is refused and the plan stays as it was,
  * code decides whether a confirmation is needed, from the result and ``revise.*`` thresholds (a scene removed, many clips removed, a big change of
    length, required content removed), whatever the model's own `destructive` says; a question from the model (`ambiguous`) always needs one.

A revision that needs a confirmation is only proposed; `trip-vlog approve --revision` and `resume` apply exactly that proposal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.llm.types import LLMRequest, TextPart
from trip_vlog.llm.untrusted import PREAMBLE
from trip_vlog.planning.catalog_view import PlanningCatalog, ref_targets, required_label
from trip_vlog.planning.models import EditPlan
from trip_vlog.planning.plan_diff import (ANCHORS, REVISION_OPERATIONS, ApplyResult, OpError, PlanContext, PlanOp, RefusedOp, apply_ops, render_ops_markdown)
from trip_vlog.planning.validate import ValidationSettings
from trip_vlog.prompts import PromptTemplate, load_template
from trip_vlog.qc import critic as cr
from trip_vlog.state import stable_hash

SYSTEM_TEMPLATE = "revise"
INPUT_TEMPLATE = "revise_input"
RESPONSE_SCHEMA_VERSION = "1"
REPORT_SCHEMA = 1
MAX_TEXT = 600
MAX_QUESTIONS = 8
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ---- prompts, schema ------------------------------------------------------------------------------------------------------

def templates() -> tuple[PromptTemplate, PromptTemplate]:
    return load_template(SYSTEM_TEMPLATE), load_template(INPUT_TEMPLATE)


def prompt_info() -> dict[str, Any]:
    s, i = templates()
    return {"system": {"id": s.id, "version": s.version, "sha256": s.sha256}, "input": {"id": i.id, "version": i.version, "sha256": i.sha256},
            "response_schema": RESPONSE_SCHEMA_VERSION}


def response_schema() -> dict[str, Any]:
    """Strict-mode friendly: every property required, nullable instead of optional, no cosmetic limits (counts and lengths are said in the prompt and
    enforced in code, where a violation costs one operation and not the whole reply)."""
    op = {
        "operation": {"type": "string", "enum": list(REVISION_OPERATIONS), "description": "One operation of the closed set."},
        "clip_instance_id": {"type": ["string", "null"], "description": "The clip to change; null for operations on a scene."},
        "scene_id": {"type": ["string", "null"], "description": "shorten_scene, add_moment, remove_scene, move_scene."},
        "duration_seconds": {"type": ["number", "null"], "description": "trim_clip: the new length in seconds; add_moment: the clip's length."},
        "anchor": {"type": ["string", "null"], "enum": [*ANCHORS, None], "description": "trim_clip: where in the moment the clip is taken."},
        "moment_id": {"type": ["string", "null"], "description": "swap_moment, add_moment: one of the alternatives of the scene."},
        "to_position": {"type": ["integer", "null"], "description": "move_clip, move_scene, add_moment: the new place, counted from 1."},
        "transition_type": {"type": ["string", "null"], "enum": ["cut", "dissolve", None], "description": "set_transition."},
        "transition_seconds": {"type": ["number", "null"], "description": "set_transition: 0 for a cut, 0.3 to 1.0 for a dissolve."},
        "audio": {"type": ["string", "null"], "enum": ["mute", "keep", "lower", None], "description": "set_clip_audio."},
        "shorten_by_seconds": {"type": ["number", "null"], "description": "shorten_scene: how many seconds to take out of the scene."},
        "rationale": {"type": "string", "description": "One short sentence on how this operation serves the request."},
        "request_part": {"type": "string", "description": "The words of the request this operation answers."},
    }
    question = {
        "question": {"type": "string", "description": "What is unclear, as a question the person can answer."},
        "about": {"type": "string", "description": "The words of the request it concerns."},
        "assumption": {"type": ["string", "null"], "description": "What would be done by default, or null."},
    }
    unsupported = {
        "request_part": {"type": "string", "description": "The words of the request that no operation can do."},
        "reason": {"type": "string", "description": "Why not."},
    }
    props = {
        "summary": {"type": "string", "description": "What was understood, in one or two sentences."},
        "operations": {"type": "array", "description": "The operations, in the order they are applied.",
                       "items": {"type": "object", "properties": op, "required": list(op), "additionalProperties": False}},
        "ambiguous": {"type": "array", "description": "Questions about what is unclear; empty when the request is clear.",
                      "items": {"type": "object", "properties": question, "required": list(question), "additionalProperties": False}},
        "destructive": {"type": "boolean", "description": "True when the change removes content the person may not expect to lose or changes the length a lot."},
        "destructive_reason": {"type": ["string", "null"], "description": "One sentence on why; null when destructive is false."},
        "unsupported": {"type": "array", "description": "Parts of the request that no operation can do.",
                        "items": {"type": "object", "properties": unsupported, "required": list(unsupported), "additionalProperties": False}},
    }
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"https://trip-vlog.local/schemas/revise_response:{RESPONSE_SCHEMA_VERSION}",
            "title": "revise_response", "description": "Revision reply: the operations (from a closed set, durations in seconds only, ids copied from the request) that "
            "answer the person's request, questions about what is unclear, whether the change is destructive and what no operation can do.",
            "type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def write_response_schema(directory: Any) -> Path:
    from trip_vlog.state import atomic_write_json

    p = Path(directory) / "revise_response.schema.json"
    atomic_write_json(p, response_schema())
    return p


# ---- request ----------------------------------------------------------------------------------------------------------------

def normalize_request(text: str) -> str:
    """The request as the model and the cache see it: control characters removed, line ends unified, blanks collapsed, ends trimmed."""
    t = CONTROL.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(" ".join(line.split()) for line in t.strip().split("\n"))


def request_hash(text: str) -> str:
    return stable_hash({"request": normalize_request(text)})


@dataclass
class ReviseInputs:
    plan: EditPlan
    catalog: PlanningCatalog
    project: Mapping[str, Any]
    request: str
    alternatives: Mapping[str, Sequence[str]] | None = None
    alternative_notes: Mapping[str, str] = field(default_factory=dict)
    max_ops: int = 12


def _critic_view(inp: ReviseInputs) -> cr.CriticInputs:
    """The plan sections are the critic's (same compact plan, same untrusted wrapping): a view without QC, sheets or images."""
    return cr.CriticInputs(plan=inp.plan, catalog=inp.catalog, qc={}, index=None, sheet_paths={}, project=inp.project, alternatives=inp.alternatives,
                           alternative_notes=inp.alternative_notes, images=False)


def limits_text(inp: ReviseInputs, cfg: Any) -> str:
    r = cfg
    return "\n".join([
        f"- at most {inp.max_ops} operations",
        f"- the film may not become more than {r.max_duration_change_pct:g}% longer or shorter than it is now ({inp.plan.total_duration_seconds:.2f} s)",
        "- a clip is never shorter than 1 s (a photo: the minimum still length) or longer than its moment's usable length",
        "- swap_moment and add_moment only use the alternatives listed above; without alternatives they are not available",
        "- the last clip of a scene and the last scene of the film cannot be removed; a title is never removed with its scene"])


def user_text(inp: ReviseInputs, revise_cfg: Any) -> str:
    _, tpl = templates()
    view = _critic_view(inp)
    n_clips = sum(len(s.clips) for s in inp.plan.scenes)
    values = {"untrusted_preamble": PREAMBLE, "settings": cr.settings_text(view), "plan": cr.plan_text(view), "titles": cr.titles_text(view),
              "alternatives": cr.alternatives_text(view), "limits": limits_text(inp, revise_cfg), "request": normalize_request(inp.request).replace("END REQUEST", "END  REQUEST"),
              "scene_count": str(len(inp.plan.scenes)), "clip_count": str(n_clips), "total_seconds": f"{inp.plan.total_duration_seconds:.2f}"}
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m[1], m[0]), tpl.text)   # one pass: the request and media text are never mistaken for a placeholder


def build_request(inp: ReviseInputs, revise_cfg: Any, max_tokens: int) -> LLMRequest:
    system, _ = templates()
    sys_text = re.sub(r"\{\{(\w+)\}\}", lambda m: str(inp.max_ops) if m[1] == "max_ops" else m[0], system.text)
    return LLMRequest(system.id, str(system.version), sys_text, (TextPart(user_text(inp, revise_cfg)),), response_schema(), max_tokens)


# ---- response checks --------------------------------------------------------------------------------------------------------

def _text(v: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(CONTROL.sub("", str(v or "")).split())[:limit]


@dataclass
class Parsed:
    summary: str
    ops: list[PlanOp]
    rejected: list[RefusedOp]
    ambiguous: list[dict[str, Any]]
    unsupported: list[dict[str, str]]
    model_destructive: bool
    model_destructive_reason: str
    dropped_over_limit: int = 0
    notes: list[str] = field(default_factory=list)
    parts: dict[int, str] = field(default_factory=dict)       # index into ops -> the words of the request it answers


def _ids_problem(op: PlanOp, plan: EditPlan, catalog: PlanningCatalog) -> str | None:
    clips = {c.clip_instance_id for s in plan.scenes for c in s.clips}
    scenes = {s.scene_id for s in plan.scenes}
    if op.clip_instance_id is not None and op.clip_instance_id not in clips:
        return f"there is no clip {op.clip_instance_id!r} in the plan"
    if op.scene_id is not None and op.scene_id not in scenes:
        return f"there is no scene {op.scene_id!r} in the plan"
    if op.moment_id is not None and op.moment_id not in catalog.moments:
        return f"{op.moment_id!r} is not a moment of this project"
    return None


def parse_reply(parsed: Mapping[str, Any], plan: EditPlan, catalog: PlanningCatalog, max_ops: int) -> Parsed:
    """Check the model's reply: every operation is parsed and its ids looked up, the questions and unsupported parts are cleaned. Nothing is applied here."""
    ops: list[PlanOp] = []
    rejected: list[RefusedOp] = []
    parts: dict[int, str] = {}
    seen: set[str] = set()
    over = 0
    for raw in parsed.get("operations") or []:
        if not isinstance(raw, Mapping):
            rejected.append(RefusedOp(None, "malformed", "an operation is not a record", raw))
            continue
        rec = {**raw, "reason": raw.get("rationale") or raw.get("reason") or ""}
        try:
            op = PlanOp.from_dict(rec, REVISION_OPERATIONS)
        except OpError as e:
            rejected.append(RefusedOp(None, "malformed", str(e), {k: raw.get(k) for k in ("operation", "clip_instance_id", "scene_id", "moment_id")}))
            continue
        why = _ids_problem(op, plan, catalog)
        if why:
            rejected.append(RefusedOp(op, "unknown_id", why))
            continue
        key = json.dumps({k: v for k, v in op.to_dict().items() if k != "reason"}, sort_keys=True)
        if key in seen:
            continue            # the same operation twice is one operation
        seen.add(key)
        if len(ops) >= max_ops:
            over += 1
            continue
        parts[len(ops)] = _text(raw.get("request_part"), 300)
        ops.append(op)
    amb = [{"question": _text(a.get("question")), "about": _text(a.get("about"), 300), "assumption": _text(a.get("assumption"), 300) or None}
           for a in (parsed.get("ambiguous") or []) if isinstance(a, Mapping) and _text(a.get("question"))][:MAX_QUESTIONS]
    uns = [{"request_part": _text(u.get("request_part"), 300), "reason": _text(u.get("reason"))}
           for u in (parsed.get("unsupported") or []) if isinstance(u, Mapping) and (_text(u.get("request_part")) or _text(u.get("reason")))][:MAX_QUESTIONS]
    notes = [f"{over} operation(s) beyond the limit of {max_ops} were dropped"] if over else []
    return Parsed(_text(parsed.get("summary")), ops, rejected, amb, uns, bool(parsed.get("destructive")), _text(parsed.get("destructive_reason")), over, notes, parts)


# ---- applying: rules, result, policy ----------------------------------------------------------------------------------------

def revision_vset(vset: ValidationSettings, parent: EditPlan, revise_cfg: Any) -> ValidationSettings:
    """The rules a revision is tried against: the current length of the film is the target and the hard bound ``revise.max_duration_change_pct`` the
    tolerance (a revision may change the length on purpose, within bounds); required content is checked afterwards (confirmation, not refusal)."""
    return replace(vset, target_duration_seconds=float(parent.total_duration_seconds), duration_tolerance_pct=float(revise_cfg.max_duration_change_pct),
                   duration_tolerance_seconds=0.0, must_include=())


def required_status(plan: EditPlan, catalog: PlanningCatalog, refs: Sequence[str]) -> dict[str, bool]:
    """For each must_include reference that matches something available: whether a clip of the plan uses it."""
    used = {c.moment_id for s in plan.scenes for c in s.clips}
    out: dict[str, bool] = {}
    for ref in dict.fromkeys(refs):
        targets = [m for m in ref_targets(ref, catalog) if catalog.assets[m.asset_id].present]
        if targets:
            out[ref] = any(m.moment_id in used for m in targets)
    return out


def lost_required(old: EditPlan, new: EditPlan, catalog: PlanningCatalog, refs: Sequence[str]) -> list[str]:
    """must_include references the old plan satisfied and the new one does not."""
    before, after = required_status(old, catalog, refs), required_status(new, catalog, refs)
    return [r for r, ok in before.items() if ok and not after.get(r, False)]


def apply_revision(parent: EditPlan, ops: Sequence[PlanOp], ctx: PlanContext, revise_cfg: Any, *, base_selection: Any = None, target: float | None = None) -> ApplyResult:
    """Apply ``ops`` with the revision rules. ``target``: the target duration the new plan records (None: unchanged)."""
    vset = revision_vset(ctx.vset, parent, revise_cfg)
    pset = ctx.pset if target is None else replace(ctx.pset, target_duration_seconds=float(target))
    return apply_ops(parent, ops, replace(ctx, vset=vset, pset=pset), base_selection=base_selection)


def new_target(parent: EditPlan, result: ApplyResult, effective: ValidationSettings) -> float | None:
    """The target duration to record when the new total is outside the tolerance of the current one; None when the current target still holds."""
    total = float(result.edit.total_duration_seconds)
    return None if abs(total - effective.target_duration_seconds) <= effective.total_tolerance_seconds() else round(total, 3)


@dataclass
class Policy:
    confirm_duration_change_pct: float = 15.0
    confirm_duration_change_min_seconds: float = 10.0
    confirm_removed_clips: int = 3
    confirm_scene_removal: bool = True
    auto_apply: bool = True
    plan_review: bool = False

    @classmethod
    def from_config(cls, cfg: Any) -> "Policy":
        r = cfg.revise
        return cls(r.confirm_duration_change_pct, r.confirm_duration_change_min_seconds, r.confirm_removed_clips, r.confirm_scene_removal, r.auto_apply,
                   bool(cfg.workflow.plan_review))


@dataclass
class Decision:
    ambiguous: list[str]
    destructive: list[str]
    reasons_to_confirm: list[str]       # why a confirmation is needed at all (includes the two above and the settings)

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.reasons_to_confirm)


def removed_clip_count(parent: EditPlan, result: ApplyResult) -> int:
    new_ids = {c.clip_instance_id for s in result.edit.scenes for c in s.clips}
    return sum(1 for s in parent.scenes for c in s.clips if c.clip_instance_id not in new_ids)


def decide(parsed: Parsed, parent: EditPlan, result: ApplyResult, lost: Sequence[str], policy: Policy, must_include: Sequence[str] = ()) -> Decision:
    """Whether the revision may be applied on its own. Thresholds are the configured ones; the model's own `destructive` and its questions count too."""
    amb = [a["question"] for a in parsed.ambiguous]
    destructive: list[str] = []
    applied = [a.op.op for a in result.applied]
    if "remove_scene" in applied and policy.confirm_scene_removal:
        scenes = [a.op.scene_id for a in result.applied if a.op.op == "remove_scene"]
        destructive.append(f"removes a whole scene ({', '.join(str(s) for s in scenes)})")
    n_removed = removed_clip_count(parent, result)
    if n_removed > policy.confirm_removed_clips:
        destructive.append(f"removes {n_removed} clips (more than {policy.confirm_removed_clips})")
    total0, total1 = float(parent.total_duration_seconds), float(result.edit.total_duration_seconds)
    delta = abs(total1 - total0)
    if total0 > 0 and delta > total0 * policy.confirm_duration_change_pct / 100 and delta >= policy.confirm_duration_change_min_seconds:
        destructive.append(f"changes the length of the film by {total1 - total0:+.1f} s ({(total1 - total0) / total0 * 100:+.0f}%)")
    if lost:
        order = {r: n for n, r in enumerate(dict.fromkeys(must_include), 1)}      # the numbering the validators use: a reference itself is never shown to a model
        destructive.append("removes content that must_include requires (" + ", ".join(required_label(order.get(r, 0)) for r in lost) + ")")
    if parsed.dropped_over_limit:
        destructive.append(f"the request needs more than the limit of {len(parsed.ops)} operations; the rest was dropped")
    if parsed.model_destructive:
        destructive.append("the model considers it destructive" + (f": {parsed.model_destructive_reason}" if parsed.model_destructive_reason else ""))
    reasons = [*(f"unclear: {q}" for q in amb), *destructive]
    if not reasons and policy.plan_review:
        reasons.append("workflow.plan_review is true: every revision waits for approval")
    elif not reasons and not policy.auto_apply:
        reasons.append("revise.auto_apply is false: every revision waits for approval")
    return Decision(amb, destructive, reasons)


def revision_hash(parent_hash: str, ops: Sequence[PlanOp], target: float | None, waived: Sequence[str]) -> str:
    return stable_hash({"parent": parent_hash, "ops": [o.to_dict() for o in ops], "target": target, "waived": sorted(waived)})


# ---- documents --------------------------------------------------------------------------------------------------------------

def counts_text(applied: int, refused: int) -> str:
    return f"{applied} change(s) made" + (f", {refused} could not be made" if refused else "")


def render_markdown(doc: Mapping[str, Any]) -> str:
    """The change summary people read (`plans/revisions/revise_rNN.md`): what was asked, what changed per scene, what was refused and why."""
    L = [f"# Revision {doc['revision']}: {doc['status']}", ""]
    L += [f"- Request: {json.dumps(doc.get('request', ''), ensure_ascii=False)}", f"- Understood as: {doc.get('summary') or '(no summary)'}",
          f"- Parent plan `{str(doc.get('parent_plan_hash'))[:16]}` -> new plan `{str(doc.get('plan_hash') or '-')[:16]}`",
          f"- Authorization: {doc.get('authorization') or 'none yet (proposed)'}"]
    dur = doc.get("duration") or {}
    if dur:
        L.append(f"- Length: {dur.get('before_seconds'):.2f} s -> {dur.get('after_seconds'):.2f} s ({dur.get('delta_seconds'):+.2f} s)"
                 + (f"; target duration now {dur['new_target_seconds']:g} s" if dur.get("new_target_seconds") else ""))
    if doc.get("status") == "proposed":
        L += ["", "**Waiting for your confirmation.** Nothing was changed. " + (doc.get("next") or "")]
    if doc.get("confirm_reasons"):
        L += ["", "## Why it needs your confirmation", ""] + [f"- {r}" for r in doc["confirm_reasons"]]
    if doc.get("questions"):
        L += ["", "## Questions", ""]
        for q in doc["questions"]:
            L.append(f"- {q['question']}" + (f" (about: {q['about']})" if q.get("about") else "") + (f" Assumed: {q['assumption']}" if q.get("assumption") else ""))
    L += ["", "## Changes", ""]
    ops = doc.get("ops_applied") or []
    refused = doc.get("ops_refused") or []
    for a in ops:
        o = a["op"]
        L.append(f"- {'would be applied' if doc.get('status') in ('proposed', 'dry_run') else 'applied'}: {describe(o)}" + (f" ({o['reason']})" if o.get("reason") else "")
                 + (f" [for: {a['request_part']}]" if a.get("request_part") else ""))
    for r in refused:
        L.append(f"- refused: {describe(r['op']) if r.get('op') and r['op'].get('op') else 'malformed change'}: {r['message']}")
    if not ops and not refused:
        L.append("- none")
    if doc.get("unsupported"):
        L += ["", "## Not possible with revisions", ""] + [f"- {u['request_part']}: {u['reason']}" for u in doc["unsupported"]]
    L += ["", "## What changes", ""] + [f"    {x}" for x in (doc.get("diff_lines") or ["No change."])]
    if doc.get("warnings"):
        L += ["", "## New warnings", ""] + [f"- {w}" for w in doc["warnings"]]
    if doc.get("qc_before") is not None or doc.get("qc_after") is not None:
        L += ["", "## QC", "", f"- before: {doc.get('qc_before')}; after: {doc.get('qc_after')}" + (f"; new failing checks: {', '.join(doc['qc_new_failures'])}" if doc.get("qc_new_failures") else "")]
    if doc.get("halt_note"):
        L += ["", f"WARNING: {doc['halt_note']}"]
    if doc.get("project"):
        L += ["", f"New FilmCraft project: `{doc['project']}` (v{doc.get('project_version')}); earlier versions are kept in `filmcraft/`."]
    if doc.get("note"):
        L += ["", doc["note"]]
    if doc.get("status") == "applied":
        L += ["", f"The parent plan is `{doc['revision']}.parent_edit_plan.json` next to this file."]
    return "\n".join(L) + "\n"


def describe(op: Mapping[str, Any]) -> str:
    try:
        return PlanOp.from_dict(op, REVISION_OPERATIONS).describe()
    except OpError:
        return str(op.get("op"))


__all__ = ["ApplyResult", "Decision", "Parsed", "Policy", "ReviseInputs", "apply_revision", "build_request", "decide", "parse_reply", "render_markdown",
           "render_ops_markdown", "response_schema"]
