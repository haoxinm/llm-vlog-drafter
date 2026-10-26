"""Story outline (DESIGN §8.1): models, JSON schemas, deterministic validation and repair.

Two documents share the scene shape:
  * the *response* (what the story editor returns): no scene ids and no schema version; the model gives the order;
  * the *outline document* (`plans/story_outline.json`, what the user reviews, edits and approves): code-assigned stable scene
    ids (`scene_01`, ...) and `schema_version`.

Validation has two modes (§11.4):
  * ``repair=True`` (model output): unambiguous problems are repaired and every repair is recorded; problems that would
    change the editorial intent are errors, which the caller sends back to the model once before failing.
  * ``repair=False`` (an outline the user edited): nothing is rewritten. Problems are reported and the outline cannot be
    approved until they are fixed.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trip_vlog.llm.untrusted import untrusted_block
from trip_vlog.planning.models import ID_PATTERN, TITLE_MAX_CHARS
from trip_vlog.planning.outline_catalog import OutlineCatalog
from trip_vlog.planning.validate import ValidationReport, ValidationSettings, _schema_errors
from trip_vlog.state import stable_hash

OUTLINE_VERSION = "1"
ROLES = ("opening_hook", "arrival_context", "establishing_place", "activity_action", "personal_reaction", "culture_food",
         "scenic_breathing_room", "transition", "reflection_ending", "other")
STORY_MODES = ("chronological", "mostly_chronological", "thematic")
MAX_SCENES = 60
MAX_CANDIDATES = 200
MAX_TITLE = 200            # scene title, outline document
MAX_PURPOSE = 500
MAX_TONE = 200
MAX_NOTE = 500             # chronology_note, hook justification, missing_coverage / uncertainties items
MAX_PREMISE = 1500
MAX_LIST = 100
MAX_SCENE_SECONDS = 7200
MIN_SCENE_SECONDS = 0.5    # a reply's budget of zero or less is raised to this
SCENE_TITLE_CHARS = TITLE_MAX_CHARS    # a scene title may become an on-screen title (selection.scene_titles)
Role = Literal["opening_hook", "arrival_context", "establishing_place", "activity_action", "personal_reaction", "culture_food",
               "scenic_breathing_room", "transition", "reflection_ending", "other"]
StoryMode = Literal["chronological", "mostly_chronological", "thematic"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class OutlineScene(_Model):
    scene_id: str = Field(pattern=ID_PATTERN)
    role: Role
    title: str = Field(max_length=MAX_TITLE)
    purpose: str = Field(max_length=MAX_PURPOSE)
    tone: str = Field(max_length=MAX_TONE)
    target_duration_seconds: float = Field(gt=0, le=MAX_SCENE_SECONDS)
    candidate_moment_ids: list[str] = Field(min_length=1, max_length=MAX_CANDIDATES)
    chronology_note: str | None = Field(max_length=MAX_NOTE)


class OutlineHook(_Model):
    used: bool
    moment_id: str | None
    justification: str | None = Field(max_length=MAX_NOTE)


class StoryOutline(_Model):
    schema_version: Literal["1"]
    premise: str = Field(max_length=MAX_PREMISE)
    story_mode_used: StoryMode
    scenes: list[OutlineScene] = Field(min_length=1, max_length=MAX_SCENES)
    hook: OutlineHook
    missing_coverage: list[str] = Field(max_length=MAX_LIST)
    uncertainties: list[str] = Field(max_length=MAX_LIST)

    def total_seconds(self) -> float:
        return round(sum(s.target_duration_seconds for s in self.scenes), 3)

    def moment_ids(self) -> list[str]:
        return [m for s in self.scenes for m in s.candidate_moment_ids]


def to_json_dict(outline: StoryOutline) -> dict[str, Any]:
    return outline.model_dump(mode="json")


def outline_hash(outline: StoryOutline) -> str:
    """Hash of the outline *content* (key order and whitespace in the file do not matter)."""
    return stable_hash(to_json_dict(outline))


def outline_text(outline: StoryOutline) -> str:
    return json.dumps(to_json_dict(outline), ensure_ascii=False, indent=2) + "\n"


def read_outline_doc(path: Path) -> Any:
    """Parse a hand-editable JSON file. ``utf-8-sig``: Notepad and some editors on Windows add a byte order mark."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _strip_ids(doc: Any) -> Any:
    """A copy of an outline document whose moment ids carry no blanks (a hand edit can leave some). Anything else is returned as it is."""
    if not isinstance(doc, dict):
        return doc
    d = copy.deepcopy(doc)
    for sc in d.get("scenes") if isinstance(d.get("scenes"), list) else []:
        if isinstance(sc, dict) and isinstance(sc.get("candidate_moment_ids"), list):
            sc["candidate_moment_ids"] = [m.strip() if isinstance(m, str) else m for m in sc["candidate_moment_ids"]]
    if isinstance(d.get("hook"), dict) and isinstance(d["hook"].get("moment_id"), str):
        d["hook"]["moment_id"] = d["hook"]["moment_id"].strip()
    return d


def outline_file_hash(path: Path) -> str:
    """Hash of the file's outline content; ``invalid-...`` when it is unreadable or does not parse (so it never equals an approval)."""
    try:
        doc = read_outline_doc(path)
    except (OSError, ValueError):
        try:
            return "invalid-" + hashlib.sha256(Path(path).read_bytes()).hexdigest()[:48]
        except OSError:
            return "invalid-missing"
    try:
        return outline_hash(StoryOutline.model_validate(_strip_ids(doc)))
    except ValidationError:
        return "invalid-" + stable_hash(doc)[:48]


# ---- JSON schemas (hand-built, OpenAI strict compatible: every property required, nullable instead of optional) --------

def _lim(on: bool, **kw: Any) -> dict[str, Any]:
    return kw if on else {}


def _scene_properties(with_id: bool) -> dict[str, Any]:
    # Cosmetic limits (text length, number range) are part of the outline *document* schema, which the user's edits are checked
    # against. They are left out of the model's response schema: providers ignore them and the gateway would otherwise fail the
    # whole stage over one long sentence. Code shortens or clamps the reply instead (`_tidy_limits`); the limits are said in words.
    props: dict[str, Any] = {}
    if with_id:
        props["scene_id"] = {"type": "string", "pattern": ID_PATTERN, "description": "Stable scene id assigned by code (scene_01, scene_02, ...)."}
    props.update({
        "role": {"type": "string", "enum": list(ROLES), "description": "Editorial role of the scene."},
        "title": {"type": "string", **_lim(with_id, maxLength=MAX_TITLE),
                  "description": f"Short working title, at most {SCENE_TITLE_CHARS} characters (it can become an on-screen title)."},
        "purpose": {"type": "string", **_lim(with_id, maxLength=MAX_PURPOSE),
                    "description": "What this scene does for the story, in one or two sentences (up to 500 characters)."},
        "tone": {"type": "string", **_lim(with_id, maxLength=MAX_TONE), "description": "Emotional tone, a few words."},
        "target_duration_seconds": {"type": "number", **_lim(with_id, exclusiveMinimum=0, maximum=MAX_SCENE_SECONDS),
                                    "description": "Time budget for the scene on the timeline, in seconds (more than 0)."},
        "candidate_moment_ids": {"type": "array", "minItems": 1, **_lim(with_id, maxItems=MAX_CANDIDATES), "items": {"type": "string"},
                                 "description": "IDs copied exactly from the catalog, in the order they would be used."},
        "chronology_note": {"type": ["string", "null"], **_lim(with_id, maxLength=MAX_NOTE),
                            "description": "Why the scene is out of chronological order, or null when it is in order."},
    })
    return props


def _body_properties(with_id: bool) -> dict[str, Any]:
    scene = _scene_properties(with_id)
    props: dict[str, Any] = {}
    if with_id:
        props["schema_version"] = {"type": "string", "enum": [OUTLINE_VERSION]}
    props.update({
        "premise": {"type": "string", **_lim(with_id, maxLength=MAX_PREMISE),
                    "description": "A short narrative premise grounded in the catalog (a few sentences)."},
        "story_mode_used": {"type": "string", "enum": list(STORY_MODES), "description": "How the scenes are ordered."},
        "scenes": {"type": "array", "minItems": 1, **_lim(with_id, maxItems=MAX_SCENES),
                   "items": {"type": "object", "properties": scene, "required": list(scene), "additionalProperties": False},
                   "description": "Scenes in story order."},
        "hook": {"type": "object", "properties": {
            "used": {"type": "boolean", "description": "True when the first scene is a short nonlinear opening hook."},
            "moment_id": {"type": ["string", "null"], "description": "The hook moment (also listed in the first scene), else null."},
            "justification": {"type": ["string", "null"], **_lim(with_id, maxLength=MAX_NOTE),
                              "description": "Why this is a real, compelling moment, else null."}},
            "required": ["used", "moment_id", "justification"], "additionalProperties": False},
        "missing_coverage": {"type": "array", **_lim(with_id, maxItems=MAX_LIST), "items": {"type": "string", **_lim(with_id, maxLength=MAX_NOTE)},
                             "description": "Coverage the story needs (or the user asked for) that the catalog lacks."},
        "uncertainties": {"type": "array", **_lim(with_id, maxItems=MAX_LIST), "items": {"type": "string", **_lim(with_id, maxLength=MAX_NOTE)},
                          "description": "Things the user should confirm before the outline is approved."},
    })
    return props


def _schema(name: str, with_id: bool, description: str) -> dict[str, Any]:
    props = _body_properties(with_id)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"https://trip-vlog.local/schemas/{name}:{OUTLINE_VERSION}",
            "title": name, "description": description, "type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def outline_schema() -> dict[str, Any]:
    """The outline document (plans/story_outline.json)."""
    return _schema("story_outline", True, "Approved story outline: scenes with roles, budgets and candidate moment IDs. No timeline commands, no timestamps.")


def response_schema() -> dict[str, Any]:
    """What the story editor returns: the same shape without scene ids and schema_version (code assigns those)."""
    return _schema("story_outline_response", False, "Story editor reply: scenes in story order; scene ids are assigned by code.")


SCHEMAS = {"story_outline": outline_schema, "story_outline_response": response_schema}


def write_schemas(directory: Any) -> list[Path]:
    from trip_vlog.state import atomic_write_json

    out = []
    for name, build in SCHEMAS.items():
        p = Path(directory) / f"{name}.schema.json"
        atomic_write_json(p, build())
        out.append(p)
    return out


def scene_id_for(index: int) -> str:
    return f"scene_{index + 1:02d}"


def document_from_response(resp: Mapping[str, Any]) -> dict[str, Any]:
    """Response -> outline document: assign stable scene ids in the order the model gave and add the schema version."""
    d = copy.deepcopy(dict(resp))
    d["scenes"] = [{"scene_id": scene_id_for(i), **s} for i, s in enumerate(d.get("scenes") or []) if isinstance(s, dict)]
    return {"schema_version": OUTLINE_VERSION, **d}


# ---- settings -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OutlineSettings:
    target_duration_seconds: float = 180.0
    tolerance_seconds: float = 9.0
    story_mode: str = "mostly_chronological"
    allow_nonlinear_hook: bool = True
    opening_hook_seconds: float = 8.0
    closing_seconds: float = 10.0
    max_similar_shots_per_scene: int = 2

    @classmethod
    def from_config(cls, config: Any) -> "OutlineSettings":
        p, e = config.project, config.editorial
        vs = ValidationSettings.from_config(config)
        return cls(target_duration_seconds=float(p.target_duration_seconds), tolerance_seconds=vs.total_tolerance_seconds(),
                   story_mode=p.story_mode, allow_nonlinear_hook=e.allow_nonlinear_hook, opening_hook_seconds=float(e.opening_hook_seconds),
                   closing_seconds=float(e.closing_seconds), max_similar_shots_per_scene=e.max_similar_shots_per_scene)


# ---- validation + repair ---------------------------------------------------------------------------------------------

def _label(d: dict[str, Any], si: int) -> str:
    """Findings go back to the model, so a label carries no text taken from the reply or the media: the scene's position only."""
    return f"scene {si + 1}"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip()


def _tidy_limits(d: dict[str, Any], report: ValidationReport) -> None:
    """Model replies only: shorten texts and clamp budgets to the limits of the outline document (the response schema does not
    carry them, see `_scene_properties`). Every change is a recorded repair. A value of the wrong type is left to the schema check."""
    def text(obj: Any, key: str, limit: int, path: str) -> None:
        v = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(v, str) and len(v) > limit:
            obj[key] = _cut(v, limit)
            report.repaired("text_shortened", f"{path} had {len(v)} characters, the limit is {limit}; shortened", len(v), len(obj[key]), path)

    text(d, "premise", MAX_PREMISE, "premise")
    for si, sc in enumerate(d.get("scenes") if isinstance(d.get("scenes"), list) else []):
        if not isinstance(sc, dict):
            continue
        ids = sc.get("candidate_moment_ids")
        if isinstance(ids, list) and len(ids) > MAX_CANDIDATES:
            report.repaired("list_shortened", f"scenes[{si}].candidate_moment_ids had {len(ids)} entries, the limit is {MAX_CANDIDATES}; the rest was dropped",
                            len(ids), MAX_CANDIDATES, f"scenes[{si}].candidate_moment_ids")
            del ids[MAX_CANDIDATES:]
        for key, limit in (("title", MAX_TITLE), ("purpose", MAX_PURPOSE), ("tone", MAX_TONE), ("chronology_note", MAX_NOTE)):
            text(sc, key, limit, f"scenes[{si}].{key}")
        v = sc.get("target_duration_seconds")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and not (0 < v <= MAX_SCENE_SECONDS):
            new = float(MIN_SCENE_SECONDS if v <= 0 else MAX_SCENE_SECONDS)
            sc["target_duration_seconds"] = new
            report.repaired("scene_budget_clamped", f"scenes[{si}].target_duration_seconds is {v:g} s, allowed is more than 0 up to {MAX_SCENE_SECONDS} s; set to {new:g}",
                            v, new, f"scenes[{si}].target_duration_seconds")
    if isinstance(d.get("hook"), dict):
        text(d["hook"], "justification", MAX_NOTE, "hook.justification")
    for key in ("missing_coverage", "uncertainties"):
        items = d.get(key)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if isinstance(item, str) and len(item) > MAX_NOTE:
                items[i] = _cut(item, MAX_NOTE)
                report.repaired("text_shortened", f"{key}[{i}] had {len(item)} characters, the limit is {MAX_NOTE}; shortened", len(item), len(items[i]), f"{key}[{i}]")
        if len(items) > MAX_LIST:
            report.repaired("list_shortened", f"{key} had {len(items)} entries, the limit is {MAX_LIST}; the rest was dropped", len(items), MAX_LIST, key)
            del items[MAX_LIST:]


def _scene_epoch(oc: OutlineCatalog, ids: list[str]) -> float | None:
    ts = []
    for i in ids:
        m = oc.eligible.get(i)
        if m is not None and m.sort_key[0] == 0:
            ts.append(m.sort_key[1])
    return min(ts) if ts else None


def validate_outline(doc: Mapping[str, Any], oc: OutlineCatalog, s: OutlineSettings, *, repair: bool) -> ValidationReport:
    """Validate (and with ``repair=True`` repair) an outline *document*. ``report.plan`` is the resulting `StoryOutline` when
    there are no errors. ``repair=False`` never changes anything: the offered set is every eligible moment, not only the
    ones that fit in the prompt, because the user may legitimately pick any of them."""
    report = ValidationReport()
    if not isinstance(doc, Mapping):      # a hand edit can leave null, a list, a string or a number at the top level
        report.error("schema", f"the outline must be a JSON object, not {type(doc).__name__ if doc is not None else 'null'}", "<root>")
        return report
    d = copy.deepcopy(dict(doc))
    if repair:
        if isinstance(d.get("scenes"), list) and len(d["scenes"]) > MAX_SCENES:     # not in the reply schema: told to the model instead
            report.error("too_many_scenes", f"the outline has {len(d['scenes'])} scenes, the limit is {MAX_SCENES}; merge scenes", "scenes")
            return report
        _tidy_limits(d, report)
    try:
        StoryOutline.model_validate(d)
    except ValidationError as e:
        _schema_errors(report, e)
        return report
    scenes: list[dict[str, Any]] = d["scenes"]
    usable = oc.offered if repair else frozenset(oc.eligible)
    hook = d["hook"]
    hook_scene = next((i for i, sc in enumerate(scenes) if sc["role"] == "opening_hook"), None)
    if isinstance(hook["moment_id"], str):
        hook["moment_id"] = hook["moment_id"].strip()      # a hand-edited id may carry blanks; the plan keeps the clean id
    hook_mid = hook["moment_id"] if hook["used"] and hook_scene == 0 and hook["moment_id"] else None

    # scene ids
    seen_ids: set[str] = set()
    for si, sc in enumerate(scenes):
        if sc["scene_id"] in seen_ids:
            report.error("duplicate_scene_id", f"{_label(d, si)}: scene_id {sc['scene_id']!r} is used more than once", f"scenes[{si}].scene_id")
        seen_ids.add(sc["scene_id"])

    # moment references
    where: dict[str, list[int]] = {}
    for si, sc in enumerate(scenes):
        kept: list[str] = []
        for pi, raw in enumerate(sc["candidate_moment_ids"]):
            mid = raw.strip()
            path = f"scenes[{si}].candidate_moment_ids[{pi}]"
            problem = None
            if mid not in oc.all_ids:
                problem = ("unknown_moment", f"{_label(d, si)}: {mid!r} is not a moment in the catalog")
            elif mid in oc.excluded_ids:
                problem = ("excluded_moment", f"{_label(d, si)}: {mid} matches a must_exclude rule")
            elif mid not in oc.eligible:
                problem = ("unavailable_moment", f"{_label(d, si)}: {mid} belongs to a missing, unreadable or audio-only asset")
            elif mid not in usable:
                problem = ("moment_not_offered", f"{_label(d, si)}: {mid} was not part of the catalog shown to the story editor")
            elif mid in kept:
                problem = ("duplicate_moment", f"{_label(d, si)}: {mid} is listed twice in the same scene")
            elif mid in where and not (mid == hook_mid and where[mid] == [0]):
                problem = ("duplicate_moment", f"{_label(d, si)}: {mid} is already used by scene {where[mid][0] + 1}")
            if problem is None:
                kept.append(mid)
                where.setdefault(mid, []).append(si)
            elif repair:
                report.repaired(problem[0], problem[1] + "; removed", mid, None, path)
            else:
                report.error(problem[0], problem[1], path)
        # ids without the blanks a hand edit may have left; an outline the user edited keeps every entry (problems are errors)
        sc["candidate_moment_ids"] = kept if repair else [raw.strip() for raw in sc["candidate_moment_ids"]]
        if not kept:
            report.error("empty_scene", f"{_label(d, si)} has no usable candidate moments left", f"scenes[{si}].candidate_moment_ids")

    # opening hook (§8.1: only a real moment, only when allowed, only briefly)
    hooks = [i for i, sc in enumerate(scenes) if sc["role"] == "opening_hook"]
    if len(hooks) > 1:
        report.error("multiple_hooks", "more than one scene has the role opening_hook", "scenes")
    if hooks and hooks[0] != 0:
        report.error("hook_not_first", f"the opening_hook scene must be the first scene, it is scene {hooks[0] + 1}", f"scenes[{hooks[0]}].role")
    if hook["used"]:
        if not s.allow_nonlinear_hook:
            report.error("hook_not_allowed", "hook.used is true but editorial.allow_nonlinear_hook is false", "hook.used")
        elif not hooks:
            report.error("hook_scene_missing", "hook.used is true but no scene has the role opening_hook", "hook")
        elif hook["moment_id"] is None or hook["moment_id"] not in oc.eligible:
            report.error("hook_unknown_moment", f"hook.moment_id {hook['moment_id']!r} is not a usable catalog moment", "hook.moment_id")
        elif hooks[0] == 0 and hook["moment_id"] not in scenes[0]["candidate_moment_ids"]:
            report.error("hook_moment_not_in_scene", f"hook.moment_id {hook['moment_id']} is not listed in the opening_hook scene", "hook.moment_id")
        if hooks and s.allow_nonlinear_hook and scenes[hooks[0]]["target_duration_seconds"] > s.opening_hook_seconds + 1e-9:
            old = scenes[hooks[0]]["target_duration_seconds"]
            msg = f"the opening hook lasts {old:g} s, more than editorial.opening_hook_seconds ({s.opening_hook_seconds:g} s)"
            if repair and s.opening_hook_seconds > 0:
                scenes[hooks[0]]["target_duration_seconds"] = s.opening_hook_seconds
                report.repaired("hook_too_long", msg + "; shortened", old, s.opening_hook_seconds, f"scenes[{hooks[0]}].target_duration_seconds")
            else:
                report.error("hook_too_long", msg, f"scenes[{hooks[0]}].target_duration_seconds")
    else:
        if hooks:
            report.error("hook_scene_without_hook", "a scene has the role opening_hook but hook.used is false", f"scenes[{hooks[0]}].role")
        if hook["moment_id"] is not None or hook["justification"] is not None:
            if repair:
                report.repaired("hook_cleared", "hook.used is false; hook.moment_id and justification cleared",
                                {"moment_id": hook["moment_id"], "justification": hook["justification"]}, None, "hook")
                hook["moment_id"] = hook["justification"] = None
            else:
                report.warn("hook_unused_fields", "hook.used is false but hook.moment_id / justification are set", "hook")

    # budget
    total = round(sum(sc["target_duration_seconds"] for sc in scenes), 3)
    off = abs(total - s.target_duration_seconds)
    if off > s.tolerance_seconds + 1e-9:
        if repair:
            _rescale(d, s, report, total)
        else:
            report.warn("duration_off_target", f"scene budgets add up to {total:g} s, target {s.target_duration_seconds:g} s "
                        f"(tolerance {s.tolerance_seconds:g} s); the selection step will rebalance", "scenes")

    # required coverage (must_include) and unavailable references
    used = {m for sc in scenes for m in sc["candidate_moment_ids"]}
    for ref, ids in oc.must_include.items():
        label = oc.ref_label(ref)
        if not ids:
            note = f"must_include {ref!r} matches no available moment"     # local only: the outline file is for the user (never sent as such)
            report.warn("must_include_unavailable", note)
            if repair and note not in d["missing_coverage"]:
                d["missing_coverage"].append(note)
        elif not used & set(ids):
            shown = ", ".join(ids[:5]) + (f", ... ({len(ids)} in all)" if len(ids) > 5 else "")
            # the message goes to the model: the label and the moments only. The reference itself (a path, folder or glob) stays local.
            report.error("must_include_missing", f"{label} is not covered by any scene; it resolves to: {shown}", "scenes")
            report.warn("must_include_ref", f"{label} is the must_include reference {ref!r}")

    # chronology and story mode
    if d["story_mode_used"] != s.story_mode:
        report.warn("story_mode_differs", f"story_mode_used is {d['story_mode_used']!r}, project.story_mode is {s.story_mode!r}", "story_mode_used")
    if s.story_mode == "chronological":
        previous = None
        for si, sc in enumerate(scenes):
            if sc["role"] == "opening_hook":
                continue
            t = _scene_epoch(oc, sc["candidate_moment_ids"])
            if t is None:
                continue
            if previous is not None and t < previous - 1e-9:
                note = " (it has a chronology_note)" if sc["chronology_note"] else " and has no chronology_note"
                report.warn("nonlinear_order", f"{_label(d, si)} starts earlier than the scene before it although project.story_mode is "
                            f"chronological{note}", f"scenes[{si}]")
            previous = t

    if report.errors:
        return report
    try:
        report.plan = StoryOutline.model_validate(d)
    except ValidationError as e:  # a repair produced something the models reject: report it, never write it
        _schema_errors(report, e)
    return report


def _rescale(d: dict[str, Any], s: OutlineSettings, report: ValidationReport, total: float) -> None:
    """Scale the scene budgets proportionally so they add up to the target. A (clamped) opening hook keeps its length."""
    scenes = d["scenes"]
    fixed = [i for i, sc in enumerate(scenes) if sc["role"] == "opening_hook" and d["hook"]["used"]]
    free = [i for i in range(len(scenes)) if i not in fixed]
    fixed_total = sum(scenes[i]["target_duration_seconds"] for i in fixed)
    free_total = sum(scenes[i]["target_duration_seconds"] for i in free)
    room = s.target_duration_seconds - fixed_total
    if not free or free_total <= 0 or room <= 0:
        report.error("duration_unfixable", f"scene budgets add up to {total:g} s, target {s.target_duration_seconds:g} s, and cannot be rescaled "
                     "without removing scenes", "scenes")
        return
    factor = room / free_total
    before = [sc["target_duration_seconds"] for sc in scenes]
    for i in free:
        scenes[i]["target_duration_seconds"] = max(0.01, round(scenes[i]["target_duration_seconds"] * factor, 2))
    drift = round(s.target_duration_seconds - sum(sc["target_duration_seconds"] for sc in scenes), 2)
    if drift:
        big = max(free, key=lambda i: scenes[i]["target_duration_seconds"])
        scenes[big]["target_duration_seconds"] = round(max(0.01, scenes[big]["target_duration_seconds"] + drift), 2)
    after = round(sum(sc["target_duration_seconds"] for sc in scenes), 3)
    report.repaired("duration_rescaled", f"scene budgets added up to {total:g} s (target {s.target_duration_seconds:g} s, tolerance "
                    f"{s.tolerance_seconds:g} s); scaled the non-hook scenes by {factor:.3f}", before,
                    [sc["target_duration_seconds"] for sc in scenes], "scenes")
    if abs(after - s.target_duration_seconds) > 0.011:
        report.warn("duration_rounding", f"scene budgets add up to {after:g} s after rescaling", "scenes")


def findings_text(report: ValidationReport, limit: int = 30) -> str:
    """The errors, one per line, as sent back to the model for its one corrected reply."""
    lines = [f"- [{i.code}] {i.message}" + (f" (at {i.path})" if i.path else "") for i in report.errors[:limit]]
    return "\n".join(lines)


def reask_parts(parsed: Any, report: ValidationReport) -> tuple[str, str]:
    """The two extra request parts of the one corrected reply: the previous reply and the findings about it. Both can quote text
    that came from the media (a title, a rationale), so both are inert data blocks, never instructions."""
    previous = json.dumps(parsed, ensure_ascii=False, indent=1)
    return ("Your previous reply was:\n" + untrusted_block("previous_reply", previous, max_chars=max(4000, 2 * len(previous))),
            "It has these problems that cannot be fixed automatically:\n" + untrusted_block("findings", findings_text(report), max_chars=20000) +
            "\nReply again with the complete corrected JSON object. Keep everything that was fine.")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":  # python -m trip_vlog.planning.outline [dir]
    import sys

    for _p in write_schemas(sys.argv[1] if len(sys.argv) > 1 else "schemas"):
        print(_p)
