"""The optional LLM critic (DESIGN §9.4, §14 steps 6-8): request, strict response schema, evidence checks, proposal checks, report.

The critic reads the compact edit plan, the deterministic QC results, the audio measurements and, when an image-capable endpoint is
configured, the preview sheets together with the tile index (so a finding can cite tile ids). It answers with findings; each may carry ONE
proposed change from the closed set of `planning.plan_diff` (code turns durations into frames and re-resolves the plan; the model never
writes timestamps). Everything it says is checked in code:

  * evidence must name something that was supplied (a tile whose image was sent, a QC check, a clip or scene of the plan, a clip with audio
    measurements). Evidence that does not is dropped; a finding left with none is dropped too, with a note ("do not claim to have heard or
    seen content not supplied"). With no images supplied, tile evidence is never valid.
  * ids a finding lists must exist; unknown ones are removed with a note.
  * a proposed change is parsed as a `PlanOp`, then tried on the real plan through the resolver and validators on its own. One that does not
    parse, names a missing id, is out of bounds or breaks validation stays on the finding as a note (`change.status: invalid`) and changes nothing.
  * for `apply`, only changes of findings that are local (one scene, or adjacent clips) and not below the configured severity are selected, at most
    `qc.critic_max_ops_per_round` of them.

This module is pure (no workspace, no FilmCraft); `critic_stages` wires it into the draft pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.llm.types import ImagePart, LLMRequest, TextPart
from trip_vlog.llm.untrusted import PREAMBLE, untrusted_block
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.duration import parse_fps
from trip_vlog.planning.models import EditPlan
from trip_vlog.planning.plan_diff import (ANCHORS, OPERATIONS, ApplyResult, OpError, PlanBaseError, PlanContext, PlanOp, apply_ops, render_ops_markdown)
from trip_vlog.planning.selection import moment_limit
from trip_vlog.prompts import PromptTemplate, load_template
from trip_vlog.qc.checks import clip_audio_features
from trip_vlog.qc.preview import PreviewSheetIndex
from trip_vlog.state import stable_hash

SYSTEM_TEMPLATE = "critic"
INPUT_TEMPLATE = "critic_input"
RESPONSE_SCHEMA_VERSION = "1"
REPORT_SCHEMA = 1
SEVERITIES = ("info", "minor", "major")
CATEGORIES = ("story_clarity", "repetition", "pacing", "cut_point", "orientation", "audio_conflict", "missing_context", "other")
EVIDENCE_KINDS = ("tile", "qc_check", "plan", "audio")
MAX_FINDINGS = 20                 # findings kept from one reply (the prompt asks for at most 8)
MAX_TEXT = 600                    # free text kept per field
MAX_ALTERNATIVES_PER_SCENE = 12
MAX_QC_DATA_CHARS = 1200
SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2}


# ---- prompts, schema ------------------------------------------------------------------------------------------------------

def templates() -> tuple[PromptTemplate, PromptTemplate]:
    return load_template(SYSTEM_TEMPLATE), load_template(INPUT_TEMPLATE)


def prompt_info() -> dict[str, Any]:
    s, i = templates()
    return {"system": {"id": s.id, "version": s.version, "sha256": s.sha256}, "input": {"id": i.id, "version": i.version, "sha256": i.sha256},
            "response_schema": RESPONSE_SCHEMA_VERSION}


def response_schema() -> dict[str, Any]:
    """Strict-mode friendly: every property required, nullable instead of optional, no cosmetic limits (lengths, counts, ranges are said in the
    prompt and enforced in code, where a violation costs one finding and not the whole reply)."""
    evidence = {
        "kind": {"type": "string", "enum": list(EVIDENCE_KINDS), "description": "tile: a tile id whose image was supplied; qc_check: the name of an automatic check; "
                 "plan: a clip or scene id of the plan; audio: a clip id that has audio measurements."},
        "ref": {"type": "string", "description": "The tile id, check name, clip or scene id, copied exactly from the request."},
        "note": {"type": "string", "description": "One sentence: what this evidence shows."},
    }
    change = {
        "operation": {"type": "string", "enum": list(OPERATIONS), "description": "One operation of the closed set."},
        "clip_instance_id": {"type": ["string", "null"], "description": "The clip to change; null for shorten_scene."},
        "scene_id": {"type": ["string", "null"], "description": "Only for shorten_scene."},
        "duration_seconds": {"type": ["number", "null"], "description": "trim_clip: the new length in seconds."},
        "anchor": {"type": ["string", "null"], "enum": [*ANCHORS, None], "description": "trim_clip: where in the moment the clip is taken."},
        "moment_id": {"type": ["string", "null"], "description": "swap_moment: one of the alternatives of the clip's scene."},
        "to_position": {"type": ["integer", "null"], "description": "move_clip: the new place inside the scene, counted from 1."},
        "transition_type": {"type": ["string", "null"], "enum": ["cut", "dissolve", None], "description": "set_transition."},
        "transition_seconds": {"type": ["number", "null"], "description": "set_transition: 0 for a cut, 0.3 to 1.0 for a dissolve."},
        "audio": {"type": ["string", "null"], "enum": ["mute", "keep", "lower", None], "description": "set_clip_audio."},
        "shorten_by_seconds": {"type": ["number", "null"], "description": "shorten_scene: how many seconds to take out of the scene."},
        "reason": {"type": "string", "description": "One short sentence on why the change helps."},
    }
    finding = {
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "summary": {"type": "string", "description": "What is wrong, in one or two sentences."},
        "scene_ids": {"type": "array", "items": {"type": "string"}, "description": "Scenes the finding is about, copied exactly."},
        "clip_instance_ids": {"type": "array", "items": {"type": "string"}, "description": "Clips the finding is about, copied exactly."},
        "evidence": {"type": "array", "description": "At least one item, each naming something that was supplied.",
                     "items": {"type": "object", "properties": evidence, "required": list(evidence), "additionalProperties": False}},
        "proposed_change": {"type": ["object", "null"], "properties": change, "required": list(change), "additionalProperties": False,
                            "description": "One small localized change, or null."},
    }
    props = {
        "summary": {"type": "string", "description": "The overall state of the draft in at most two sentences."},
        "findings": {"type": "array", "description": "Actionable findings, most important first (at most 8); empty for a good draft.",
                     "items": {"type": "object", "properties": finding, "required": list(finding), "additionalProperties": False}},
    }
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"https://trip-vlog.local/schemas/critic_response:{RESPONSE_SCHEMA_VERSION}",
            "title": "critic_response", "description": "QC critic reply: findings with severity, category, affected scene and clip ids, evidence that was "
            "supplied, and an optional localized change from a closed set of operations. Durations in seconds only; no timestamps.",
            "type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def write_response_schema(directory: Any) -> Any:
    from trip_vlog.state import atomic_write_json

    p = Path(directory) / "critic_response.schema.json"
    atomic_write_json(p, response_schema())
    return p


# ---- request --------------------------------------------------------------------------------------------------------------

@dataclass
class CriticInputs:
    """Everything the request is made from."""

    plan: EditPlan
    catalog: PlanningCatalog
    qc: Mapping[str, Any]                          # qc_report.json
    index: PreviewSheetIndex | None
    sheet_paths: Mapping[int, Path]                # sheet number -> existing image file
    project: Mapping[str, Any]                     # title, language, style, audience, target seconds (from the config)
    alternatives: Mapping[str, Sequence[str]] | None = None   # scene_id -> candidate moment ids of the approved outline (None: no outline)
    alternative_notes: Mapping[str, str] = field(default_factory=dict)   # moment_id -> one line of description for the request
    max_sheets: int = 4
    images: bool = True                            # the endpoint takes images and the config allows sending them


def shown_sheets(inp: CriticInputs) -> list[int]:
    """Sheet numbers sent as images (empty for a text-only request): evenly spread when there are more than `max_sheets`."""
    if not inp.images or inp.index is None:
        return []
    avail = sorted(n for n in inp.sheet_paths if any(s.number == n and s.path for s in inp.index.sheets))
    if len(avail) <= inp.max_sheets:
        return avail
    pick = sorted({round(i * (len(avail) - 1) / (inp.max_sheets - 1)) for i in range(inp.max_sheets)}) if inp.max_sheets > 1 else [0]
    return [avail[i] for i in pick]


def visible_tiles(inp: CriticInputs) -> set[str]:
    """Tile ids whose pixels the model is given (on a sent sheet, frame extracted)."""
    if inp.index is None:
        return set()
    shown = set(shown_sheets(inp))
    return {t.tile_id for t in inp.index.tiles.values() if t.sheet in shown and t.extracted}


def _q(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def settings_text(inp: CriticInputs) -> str:
    p, s = inp.project, inp.plan.sequence
    return "\n".join([
        f"- title: {_q(p.get('title', ''))}", f"- target duration: {p.get('target_duration_seconds'):g} seconds",
        f"- style: {_q(p.get('style', ''))}", f"- audience: {_q(p.get('audience', ''))}", f"- language of the film: {p.get('language', 'en')}",
        f"- sequence: {s.width}x{s.height} ({s.aspect_ratio}) at {s.fps} frames per second"])


def images_note(inp: CriticInputs, n_images: int) -> str:
    if n_images:
        return (f"Preview sheets: {n_images} image(s) follow the text, each a grid of frames sampled from the rendered preview just before and just after "
                "cuts (plus the first and last frame); every tile is labelled with its record time, scene and clip. You see stills, not motion, and you "
                "hear nothing: sound is known only from the measurements below and the check results.")
    return ("NO images are supplied: you cannot see the film, only read its plan, the check results and the audio measurements. Do not describe how "
            "anything looks or sounds, and do not cite tiles as evidence.")


def _fmt_s(frames: int, fps: Fraction) -> str:
    return f"{float(Fraction(frames) / fps):.2f}"


def plan_text(inp: CriticInputs) -> str:
    plan, cat = inp.plan, inp.catalog
    fps = parse_fps(plan.sequence.fps)
    out: list[str] = []
    for sc in plan.scenes:
        start = sc.start_frame
        head = (f"### {sc.scene_id} | record {_fmt_s(start, fps)}-{_fmt_s(start + sc.duration_frames, fps)} s | budget {sc.target_duration_seconds:g} s | "
                f"{len(sc.clips)} clip(s)")
        meta = f"title: {sc.title}\npurpose: {sc.purpose}"
        lines, notes = [], []
        for c in sc.clips:
            a = cat.assets.get(c.asset_id)
            m = cat.moments.get(c.moment_id)
            kind = "photo" if c.media_type == "image" else c.media_type
            if c.media_type == "image":
                src = "photo"
            else:
                lim = moment_limit(cat, c.moment_id, fps)
                src = f"source {c.source_in_seconds:.2f}-{c.source_out_seconds:.2f} s" + (f", usable {lim:.2f} s" if lim is not None else "")
            t = c.transition_in
            trans = "cut" if t.type == "cut" else f"dissolve {t.duration_seconds:.2f} s ({t.alignment})"
            row = (f"{c.clip_instance_id} | {kind} | {c.moment_id} | record {_fmt_s(c.timeline_start_frame, fps)}-"
                   f"{_fmt_s(c.timeline_start_frame + c.duration_frames, fps)} s ({_fmt_s(c.duration_frames, fps)} s) | {src} | anchor {c.anchor} | "
                   f"sound {c.audio_policy} | transition in: {trans}")
            if c.track_role != "primary_video":
                row += f" | role {c.track_role}"
            if a is not None and a.orientation:
                row += f" | {a.orientation}"
            lines.append(row)
            desc = (m.description or "").strip() if m is not None else ""
            tags = ", ".join(m.tags) if m is not None and m.tags else ""
            if desc or tags or c.rationale:
                notes.append(f"{c.clip_instance_id}: " + "; ".join(x for x in (desc[:200], f"tags {tags}" if tags else "", f"planner note: {c.rationale[:160]}" if c.rationale else "") if x))
        out.append(head + "\n" + untrusted_block("scene", meta, max_chars=1000) + "\n" + "\n".join(lines)
                   + ("\n" + untrusted_block("clip_notes", "\n".join(notes), max_chars=3000) if notes else ""))
    return "\n\n".join(out)


def titles_text(inp: CriticInputs) -> str:
    plan = inp.plan
    if not plan.titles:
        return "Titles: none."
    fps = parse_fps(plan.sequence.fps)
    rows = [f"{t.title_id} | {t.role} | {t.scene_id} | record {_fmt_s(t.start_frame, fps)}-{_fmt_s(t.start_frame + t.duration_frames, fps)} s | text: {t.text}" for t in plan.titles]
    return "Titles (text on a second track):\n" + untrusted_block("titles", "\n".join(rows), max_chars=2000)


def alternatives_text(inp: CriticInputs) -> str:
    if inp.alternatives is None:
        return "None: this plan was not made from an approved story outline, so swap_moment is not available."
    used = {c.moment_id for s in inp.plan.scenes for c in s.clips}
    fps = parse_fps(inp.plan.sequence.fps)
    parts = []
    for sc in inp.plan.scenes:
        ids = [m for m in inp.alternatives.get(sc.scene_id, ()) if m not in used and m in inp.catalog.moments][:MAX_ALTERNATIVES_PER_SCENE]
        if not ids:
            continue
        rows = []
        for mid in ids:
            lim = moment_limit(inp.catalog, mid, fps)
            rows.append(f"{mid} | {'photo' if lim is None else f'video, usable {lim:.2f} s'} | {inp.alternative_notes.get(mid, '')[:200]}")
        parts.append(f"{sc.scene_id}:\n" + untrusted_block("alternatives", "\n".join(rows), max_chars=3500))
    return "\n".join(parts) if parts else "None: every candidate moment of every scene is already used."


def qc_text(inp: CriticInputs) -> str:
    rows = []
    for c in inp.qc.get("checks", []):
        data = c.get("data")
        extra = ""
        if data:
            blob = _q(data)
            extra = " | data " + (blob if len(blob) <= MAX_QC_DATA_CHARS else blob[:MAX_QC_DATA_CHARS] + "...")
        rows.append(f"{c['name']} | {c['status']} | {c.get('detail', '')}{extra}")
    return (f"Overall status: {inp.qc.get('status')}.\n" + untrusted_block("qc_results", "\n".join(rows), max_chars=12000)) if rows else "No results."


def audio_text(inp: CriticInputs) -> str:
    feats = clip_audio_features(inp.plan, inp.catalog)
    rows = []
    for s in inp.plan.scenes:
        for c in s.clips:
            a = feats.get(c.clip_instance_id)
            if a:
                rows.append(f"{c.clip_instance_id} | sound {c.audio_policy} | mean {a.get('mean_db')} dB | active share {a.get('active_ratio')} | silent {a.get('silent')} | "
                            f"wind likely {a.get('wind_likely')} | clipped {a.get('clipped')}")
    return "\n".join(rows) if rows else "No audio measurements are available."


def tiles_text(inp: CriticInputs) -> str:
    idx = inp.index
    if idx is None or not idx.tiles:
        return "No preview tiles exist."
    shown = set(shown_sheets(inp))
    rows = []
    for t in sorted(idx.tiles.values(), key=lambda t: t.tile_id):
        state = ("image supplied" if t.sheet in shown else "image NOT supplied") if t.extracted else "frame not extracted"
        side = t.side if t.cut_index is None else f"cut {t.cut_index} {t.side}"
        rows.append(f"{t.tile_id} | sheet {t.sheet} | record {t.record_seconds:.2f} s | {side} | {t.scene_id} | {t.clip_instance_id} | {state}")
    cuts = [f"cut {c.cut_index} at {c.record_seconds:.2f} s: {c.outgoing_clip_instance_id} -> {c.incoming_clip_instance_id} (tiles {c.before_tile}, {c.after_tile})" for c in idx.cuts]
    note = (f"\nOnly {idx.cuts_sampled} of {idx.cuts_total} cuts were sampled." if idx.truncated else "")
    return "\n".join(rows) + "\n\nCuts:\n" + "\n".join(cuts) + note


def user_text(inp: CriticInputs, n_images: int) -> str:
    _, tpl = templates()
    n_clips = sum(len(s.clips) for s in inp.plan.scenes)
    values = {"untrusted_preamble": PREAMBLE, "settings": settings_text(inp), "images_note": images_note(inp, n_images), "plan": plan_text(inp),
              "titles": titles_text(inp), "alternatives": alternatives_text(inp), "qc_results": qc_text(inp), "audio": audio_text(inp),
              "tiles": tiles_text(inp), "scene_count": str(len(inp.plan.scenes)), "clip_count": str(n_clips),
              "total_seconds": f"{inp.plan.total_duration_seconds:.2f}"}
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m[1], m[0]), tpl.text)   # one pass: media text is never mistaken for a placeholder


def sheet_identity(plan_hash: str, number: int, index_digest: str) -> str:
    """Names a sheet image by what it was made from (the plan and the tiles), not by its JPEG bytes, which vary with the ffmpeg build."""
    return f"preview-sheet:{plan_hash[:16]}:{number}:{index_digest[:16]}"


def index_digest(index: PreviewSheetIndex | None) -> str:
    if index is None:
        return "none"
    d = index.to_dict()
    d.pop("generated_at", None)
    return stable_hash(d)


def qc_digest(qc: Mapping[str, Any]) -> str:
    return stable_hash({"status": qc.get("status"), "checks": [{k: c.get(k) for k in ("name", "status", "detail", "data")} for c in qc.get("checks", [])]})


def build_request(inp: CriticInputs, max_tokens: int) -> tuple[LLMRequest, list[int]]:
    """The gateway request and the numbers of the sheets sent as images."""
    system, _ = templates()
    shown = shown_sheets(inp)
    parts: list[Any] = [TextPart(user_text(inp, len(shown)))]
    digest = index_digest(inp.index)
    for k, n in enumerate(shown, 1):
        sheet = next(s for s in inp.index.sheets if s.number == n)  # type: ignore[union-attr]
        parts.append(TextPart(f"Preview sheet {n} (image {k} of {len(shown)}); tiles on it, in reading order: {', '.join(sheet.tiles)}"))
        parts.append(ImagePart(inp.sheet_paths[n], media_type="image/jpeg", identity=sheet_identity(inp.plan.plan_hash, n, digest)))
    return LLMRequest.from_template(system, parts, schema=response_schema(), max_tokens=max_tokens), shown


# ---- response checks ------------------------------------------------------------------------------------------------------

@dataclass
class Finding:
    id: str
    severity: str
    category: str
    summary: str
    scene_ids: list[str]
    clip_instance_ids: list[str]
    evidence: list[dict[str, str]]
    dropped_evidence: list[dict[str, str]] = field(default_factory=list)
    proposed_change: dict[str, Any] | None = None     # as the model gave it (nulls removed)
    op: PlanOp | None = None
    change_status: str = "none"                         # none | invalid | not_local | below_severity | unsupported | valid | over_limit | applied | refused | pending
    change_note: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "severity": self.severity, "category": self.category, "summary": self.summary, "scene_ids": self.scene_ids,
                             "clip_instance_ids": self.clip_instance_ids, "evidence": self.evidence,
                             "proposed_change": self.op.to_dict() if self.op else self.proposed_change,
                             "change": {"status": self.change_status, "note": self.change_note}}
        if self.dropped_evidence:
            d["dropped_evidence"] = self.dropped_evidence
        if self.notes:
            d["notes"] = self.notes
        return d


@dataclass
class EvidenceSpace:
    """What was supplied: the ids evidence and affected lists may name."""

    scene_ids: set[str]
    clip_scene: dict[str, str]           # clip_instance_id -> scene_id
    qc_checks: set[str]
    tiles: set[str]                      # tiles whose image was supplied
    audio_clips: set[str]
    images: bool
    tile_clip: dict[str, str] = field(default_factory=dict)       # tile id -> clip_instance_id it shows
    check_data: dict[str, str] = field(default_factory=dict)      # qc check name -> its data as compact JSON text (to find the clips it names)

    @classmethod
    def from_inputs(cls, inp: CriticInputs) -> "EvidenceSpace":
        tile_clip = {t.tile_id: t.clip_instance_id for t in inp.index.tiles.values()} if inp.index is not None else {}
        check_data = {c["name"]: _q(c.get("data") or {}) for c in inp.qc.get("checks", [])}
        return cls({s.scene_id for s in inp.plan.scenes}, {c.clip_instance_id: s.scene_id for s in inp.plan.scenes for c in s.clips},
                   {c["name"] for c in inp.qc.get("checks", [])}, visible_tiles(inp), set(clip_audio_features(inp.plan, inp.catalog)), bool(shown_sheets(inp)),
                   tile_clip, check_data)


def _text(v: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(v or "").split())[:limit]


def check_evidence(ev: Mapping[str, Any], space: EvidenceSpace) -> str | None:
    """None when the evidence names something that was supplied, else why not."""
    kind, ref = ev.get("kind"), str(ev.get("ref") or "").strip()
    if kind == "tile":
        if not space.images:
            return "no images were supplied, so no tile was seen"
        return None if ref in space.tiles else f"tile {ref!r} is not a tile whose image was supplied"
    if kind == "qc_check":
        return None if ref in space.qc_checks else f"{ref!r} is not a check in the results"
    if kind == "plan":
        return None if ref in space.clip_scene or ref in space.scene_ids else f"{ref!r} is not a clip or scene of the plan"
    if kind == "audio":
        return None if ref in space.audio_clips else f"{ref!r} has no audio measurements"
    return f"unknown evidence kind {kind!r}"


def parse_findings(parsed: Mapping[str, Any], space: EvidenceSpace) -> tuple[list[Finding], list[dict[str, Any]], list[str]]:
    """Reply -> (kept findings, dropped findings with the reason, notes). Proposed changes are parsed here and judged later (`assess_proposals`)."""
    kept: list[Finding] = []
    dropped: list[dict[str, Any]] = []
    notes: list[str] = []
    raw = list(parsed.get("findings") or [])
    if len(raw) > MAX_FINDINGS:
        notes.append(f"the reply had {len(raw)} findings; only the first {MAX_FINDINGS} were kept")
        raw = raw[:MAX_FINDINGS]
    for r in raw:
        summary = _text(r.get("summary"))
        scenes = [s for s in dict.fromkeys(str(x).strip() for x in r.get("scene_ids") or []) if s]
        clips = [c for c in dict.fromkeys(str(x).strip() for x in r.get("clip_instance_ids") or []) if c]
        local_notes = []
        bad = [s for s in scenes if s not in space.scene_ids] + [c for c in clips if c not in space.clip_scene]
        if bad:
            local_notes.append("ids that are not in the plan were removed: " + ", ".join(bad))
            scenes = [s for s in scenes if s in space.scene_ids]
            clips = [c for c in clips if c in space.clip_scene]
        good, rejected = [], []
        for e in r.get("evidence") or []:
            item = {"kind": str(e.get("kind")), "ref": str(e.get("ref") or "").strip(), "note": _text(e.get("note"), 300)}
            why = check_evidence(item, space)
            (good if why is None else rejected).append(item if why is None else {**item, "reason": why})
        if not good:
            dropped.append({"summary": summary, "severity": r.get("severity"), "category": r.get("category"),
                            "reason": "it cites no evidence that was supplied" + (f" ({rejected[0]['reason']})" if rejected else " (no evidence given)")})
            continue
        f = Finding(id=f"finding_{len(kept) + 1:02d}", severity=r["severity"], category=r["category"], summary=summary, scene_ids=scenes,
                    clip_instance_ids=clips, evidence=good, dropped_evidence=rejected, notes=local_notes)
        pc = r.get("proposed_change")
        if isinstance(pc, Mapping):
            f.proposed_change = {k: v for k, v in pc.items() if v is not None}
            try:
                f.op = PlanOp.from_dict(pc)
                f.change_status = "valid"       # provisional: `assess_proposals` decides
            except OpError as e:
                f.change_status, f.change_note = "invalid", str(e)
        kept.append(f)
    return kept, dropped, notes


# ---- proposals ------------------------------------------------------------------------------------------------------------

def _primary_order(plan: EditPlan) -> list[str]:
    return [c.clip_instance_id for s in plan.scenes for c in s.clips if c.track_role == "primary_video"]


def locality_problem(f: Finding, op: PlanOp, plan: EditPlan) -> str | None:
    """None when the finding and its change are local: one scene, or adjacent clips (at most two neighbouring scenes), and the change touches
    what the finding is about (a clip it names, or the clip next to one it names)."""
    clip_scene = {c.clip_instance_id: s.scene_id for s in plan.scenes for c in s.clips}
    scene_order = [s.scene_id for s in plan.scenes]
    order = _primary_order(plan)
    named = list(f.clip_instance_ids)
    scenes = set(f.scene_ids) | {clip_scene[c] for c in named}
    if not scenes:
        return "the finding names no scene or clip, so the change is not shown to be local"
    pos_s = sorted(scene_order.index(x) for x in scenes)
    if len(pos_s) > 2 or (len(pos_s) == 2 and pos_s[1] - pos_s[0] != 1):
        return f"the finding spans {len(scenes)} scenes that are not neighbours; that is not a localized fix"
    if len(pos_s) == 2 and named:
        pos = sorted(order.index(c) for c in named if c in order)
        if len(pos) != len(named) or pos != list(range(pos[0], pos[0] + len(pos))):
            return "the finding spans two scenes but its clips are not adjacent"
    target_scene = op.scene_id if op.op == "shorten_scene" else clip_scene.get(op.clip_instance_id or "")
    if target_scene is None:
        return "the change names a clip or scene that is not in the plan"
    if target_scene not in scenes:
        return f"the change is in scene {target_scene}, which the finding is not about"
    if op.op != "shorten_scene" and named and op.clip_instance_id not in named:
        near = op.clip_instance_id in order and any(c in order and abs(order.index(c) - order.index(op.clip_instance_id)) == 1 for c in named)
        if not near:
            return f"the change is on {op.clip_instance_id}, which the finding neither names nor is next to"
    return None


@dataclass
class ProposalPolicy:
    mode: str = "report"                  # report | apply
    min_severity: str = "major"
    max_ops: int = 3


def _mentions(text: str, ident: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(ident) + r"(?![A-Za-z0-9_.-])", text) is not None


def evidence_problem(f: Finding, op: PlanOp, plan: EditPlan, space: EvidenceSpace) -> str | None:
    """None when at least one evidence item of the finding refers to what the change touches (the clip, or for shorten_scene the scene): a tile of that
    clip, a plan entry for that clip or its scene, audio measurements of that clip, or a check whose data names it. Else why not."""
    clip_scene = {c.clip_instance_id: s.scene_id for s in plan.scenes for c in s.clips}
    if op.op == "shorten_scene":
        scene = op.scene_id or ""
        clips = {c for c, sc in clip_scene.items() if sc == scene}
        target = f"scene {scene}"
    else:
        cid = op.clip_instance_id or ""
        scene, clips, target = clip_scene.get(cid, ""), {cid}, f"clip {cid}"
    for ev in f.evidence:
        kind, ref = ev.get("kind"), str(ev.get("ref") or "").strip()
        if kind == "tile" and space.tile_clip.get(ref) in clips:
            return None
        if kind == "plan" and (ref in clips or ref == scene):
            return None
        if kind == "audio" and ref in clips:
            return None
        if kind == "qc_check" and any(_mentions(space.check_data.get(ref, ""), x) for x in (*clips, scene) if x):
            return None
    return f"none of the evidence cited refers to {target}, which the change touches (cite its tile, its plan entry, its audio or a check that names it)"


def assess_proposals(findings: list[Finding], plan: EditPlan, ctx: PlanContext, policy: ProposalPolicy,
                     *, base_selection: Any = None, space: EvidenceSpace | None = None) -> tuple[list[Finding], ApplyResult | None]:
    """Judge every proposed change through the real resolver and validators and pick the ones to apply.

    Proposals that are local and of enough severity are tried one after the other, most severe first, each on top of the ones already chosen (a
    dissolve that needs the source handle a trim frees is judged after that trim); the first `policy.max_ops` that pass are chosen. Sets
    `change_status` / `change_note` on each finding and returns the chosen findings in order with the combined `ApplyResult` (None when nothing was
    chosen). With `space`, a proposal whose evidence does not refer to what it changes is reported (`unsupported`), not chosen. Raises `PlanBaseError` when the plan cannot serve as a base (see `plan_diff.check_roundtrip`)."""
    eligible: list[Finding] = []
    verified = False
    for f in findings:
        if f.op is None:
            continue
        probs = locality_problem(f, f.op, plan)
        if probs:
            f.change_status, f.change_note = "not_local", probs
            continue
        if f.severity == "info" or SEVERITY_RANK[f.severity] < SEVERITY_RANK[policy.min_severity]:
            r = apply_ops(plan, [f.op], ctx, base_selection=base_selection, verify_base=not verified)
            verified = True
            verdict = "it would be accepted" if not r.refused else "it would be refused: " + r.refused[0].message
            f.change_status, f.change_note = "below_severity", f"a {f.severity} finding; changes are applied for {policy.min_severity} and above ({verdict})"
            continue
        why = evidence_problem(f, f.op, plan, space) if space is not None else None
        if why:
            f.change_status, f.change_note = "unsupported", why
            continue
        eligible.append(f)
    eligible.sort(key=lambda f: -SEVERITY_RANK[f.severity])      # major first, otherwise the reply's order
    chosen: list[Finding] = []
    cur_edit, cur_sel = plan, base_selection
    for f in eligible:
        if len(chosen) >= policy.max_ops:
            f.change_status, f.change_note = "over_limit", f"more than {policy.max_ops} changes in one round; this one waits for another round"
            continue
        r = apply_ops(cur_edit, [f.op], ctx, base_selection=cur_sel, verify_base=not verified)   # type: ignore[arg-type]
        verified = True
        if r.refused:
            f.change_status = "invalid"
            f.change_note = ("together with the changes chosen before it: " if chosen else "") + r.refused[0].message
            continue
        chosen.append(f)
        cur_edit, cur_sel = r.edit, r.selection
        f.change_status, f.change_note = "valid", "; ".join(r.applied[0].notes)
    if not chosen:
        return [], None
    return chosen, apply_ops(plan, [f.op for f in chosen if f.op], ctx, base_selection=base_selection, verify_base=False)


# ---- report ---------------------------------------------------------------------------------------------------------------

def counts_by_severity(findings: Sequence[Finding]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}


def qc_block(report: Mapping[str, Any], report_rel: str) -> dict[str, Any]:
    """The `critic` block of qc_report.json."""
    return {"status": report["status"], "mode": report["mode"], "reason": report.get("reason"), "report": report_rel,
            "model": report.get("model"), "images_sent": (report.get("images") or {}).get("sent", False),
            "findings": len(report.get("findings", [])), "by_severity": report.get("by_severity", {}), "dropped_findings": len(report.get("dropped_findings", [])),
            "proposals_valid": report.get("proposals_valid", 0), "changes_applied": report.get("changes_applied", 0),
            "plan_hash": report.get("plan_hash"), "rounds": len(report.get("rounds", []))}


def render_markdown(r: Mapping[str, Any]) -> str:
    esc = lambda s: str(s).replace("\r", " ").replace("\n", " ")   # noqa: E731
    L = [f"# Critic report: {r['status']}", "",
         f"- Mode: `{r['mode']}`; plan `{str(r.get('plan_hash') or '')[:16]}`; generated {r.get('generated_at')}",
         f"- Model: {r.get('model') or '(none)'} at endpoint `{r.get('endpoint')}` ({'local' if r.get('local') else 'remote'}); "
         f"{r.get('requests', 0)} request(s), {r.get('cache_hits', 0)} served from the response cache"]
    im = r.get("images") or {}
    L.append("- Images: " + (f"{len(im.get('sheets', []))} preview sheet(s) were sent" if im.get("sent") else f"none were sent ({im.get('reason')}); this review is text only and could not see the film"))
    if r.get("reason"):
        L.append(f"- {esc(r['reason'])}")
    L.append("")
    if r.get("summary"):
        L += [f"> {esc(r['summary'])}", ""]
    L += ["The critic is advisory: it never fails a draft. Its findings are only as good as what it was given (above), every item of evidence was "
          "checked against what was supplied, and a proposed change is applied only if it is local and the resolver and validators accept it.", ""]
    findings = r.get("findings", [])
    L.append(f"## Findings ({len(findings)})")
    L.append("")
    if not findings:
        L += ["No findings.", ""]
    for f in findings:
        ch = f.get("change") or {}
        L += [f"### {f['id']}: {f['severity']} / {f['category']}", "", esc(f["summary"]), "",
              f"- About: scenes {', '.join(f['scene_ids']) or '-'}; clips {', '.join(f['clip_instance_ids']) or '-'}",
              "- Evidence: " + "; ".join(f"{e['kind']} `{e['ref']}`" + (f" ({esc(e['note'])})" if e.get("note") else "") for e in f["evidence"])]
        if f.get("dropped_evidence"):
            L.append("- Evidence not accepted: " + "; ".join(f"`{e['ref']}` ({e['reason']})" for e in f["dropped_evidence"]))
        if f.get("proposed_change"):
            pc = f["proposed_change"]
            L.append(f"- Proposed change: `{json.dumps(pc, ensure_ascii=False)}`")
            L.append(f"- Change status: **{ch.get('status')}**" + (f": {esc(ch['note'])}" if ch.get("note") else ""))
        for n in f.get("notes", []):
            L.append(f"- Note: {esc(n)}")
        L.append("")
    if r.get("dropped_findings"):
        L += ["## Dropped findings", ""] + [f"- {esc(d['summary'])} ({esc(d['reason'])})" for d in r["dropped_findings"]] + [""]
    wc = r.get("would_change")
    if wc:
        L += ["## " + ("What would change" if r["mode"] != "apply" else "Selected for this round"), ""] + [f"    {x}" for x in wc.get("lines", [])] + [""]
    for rd in r.get("rounds", []):
        L += [f"## Round {rd['round']}: {rd['status']}", ""]
        if rd.get("revision"):
            L.append(f"- Revision: `{rd['revision']}` (parent plan `{str(rd.get('parent_plan_hash'))[:12]}` -> `{str(rd.get('plan_hash'))[:12]}`)")
        if rd.get("project"):
            L.append(f"- FilmCraft project: `{rd['project']}` (the earlier version is kept)")
        if rd.get("error"):
            L.append(f"- Error: {esc(rd['error'])}")
        if rd.get("halt_note"):
            L.append(f"- WARNING: {esc(rd['halt_note'])}")
        L += [f"- {x}" for x in rd.get("ops_text", [])] + [f"    {x}" for x in rd.get("diff_lines", [])] + [""]
    if r.get("notes"):
        L += ["## Notes", ""] + [f"- {esc(n)}" for n in r["notes"]] + [""]
    return "\n".join(L).rstrip("\n") + "\n"


__all__ = ["CATEGORIES", "CriticInputs", "EvidenceSpace", "Finding", "PlanBaseError", "ProposalPolicy", "SEVERITIES", "assess_proposals", "build_request",
           "check_evidence", "counts_by_severity", "index_digest", "locality_problem", "parse_findings", "prompt_info", "qc_block", "qc_digest", "render_markdown",
           "render_ops_markdown", "response_schema", "shown_sheets", "user_text", "visible_tiles", "write_response_schema"]
