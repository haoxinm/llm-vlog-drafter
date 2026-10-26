"""resolve_handoff/compatibility_report.json and .md: for every feature the plan uses, what FilmCraft supports, whether the exported
FCP7 XML really contains it, and what is recorded about Resolve (DESIGN §15, §20 Phase 5).

One row per feature (cuts, dissolves, stills, overlays on V2, titles, markers, clip gain / mute, music track, mixed source frame
rate, mixed size / orientation, speed changes), each with

  * ``plan_count``       how often the plan uses it;
  * ``filmcraft``        supported / unsupported / unknown, from the capability map of this workspace (the commands the adapter
                         uses) and whether the saved project read back equal after reopening;
  * ``xml``              the exported ``timeline.xml`` parsed and compared with what was sent to FilmCraft at frame precision
                         (``trip_vlog.handoff.xml_roundtrip``): items expected / found, and every mismatch;
  * ``resolve``          the RECORDED result on Resolve (``resolve_evidence``, static, versioned) or, when a real
                         ``resolve_finish_report.json`` of this very timeline exists, the per-check results of that run;
  * ``verdict``          verified | expected | manual | unsupported:

        verified     FilmCraft supports it, the XML carries it correctly AND Resolve is recorded (or was measured) to import it;
        expected     FilmCraft and the XML are fine, the Resolve result is unverified or only partly recorded;
        manual       a step in Resolve is needed (``instruction``), or the measured Resolve run failed the feature;
        unsupported  FilmCraft cannot do it, or the XML does not carry it as planned (so the handoff would be wrong).

Nothing is claimed beyond the recorded evidence: "not reported" stays ``unverified``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from trip_vlog.filmcraft.timeline_spec import TimelineSpec
from trip_vlog.handoff import resolve_evidence as RE
from trip_vlog.handoff import xml_roundtrip as X
from trip_vlog.handoff.fcp7_xml import Fcp7Timeline, XmlReadError, parse_fcp7_xml
from trip_vlog.logging_utils import utc_now_iso
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan

SCHEMA_VERSION = 1
REPORT_JSON = "compatibility_report.json"
REPORT_MD = "compatibility_report.md"
VERDICTS = ("verified", "expected", "manual", "unsupported")
MAX_LISTED = 12

# (feature, label, FilmCraft capability the adapter uses for it)
FEATURES: tuple[tuple[str, str, str], ...] = (
    (X.F_SETTINGS, "Timeline frame rate, size and duration", "create_sequence"),
    (X.F_CUTS, "Hard cuts on V1 (clip order, record and source in/out)", "place_clip"),
    (X.F_DISSOLVES, "Dissolves (cross dissolve centred on the cut)", "add_transition"),
    (X.F_STILLS, "Still images", "place_clip"),
    (X.F_OVERLAYS, "Overlay clips on V2", "place_clip"),
    (X.F_TITLES, "Titles (text on V2)", "add_title"),
    (X.F_MARKERS, "Scene markers", "add_marker"),
    (X.F_GAIN, "Constant clip audio gain", "audio_gain"),
    (X.F_MUTE, "Video clips placed without their audio", "place_clip"),
    (X.F_MUSIC, "Music track (A2)", "place_clip"),
    (X.F_AUDIO, "Other stand-alone audio clips", "place_clip"),
    ("mixed_fps", "Source clips with a frame rate different from the sequence", "place_clip"),
    ("mixed_resolution", "Source clips with a size or orientation different from the sequence", "place_clip"),
    (X.F_SPEED, "Speed changes", ""),
)
LABELS = {f: label for f, label, _ in FEATURES}
CAPABILITY = {f: cap for f, _, cap in FEATURES}

# Which per-check results of a real resolve-finish run (resolve/compare.py) speak for a feature.
FINISH_COVERAGE: dict[str, tuple[str, ...]] = {
    X.F_SETTINGS: ("fps", "duration", "resolution"),
    X.F_CUTS: ("clip_order", "record_inout", "source_in"),
    X.F_OVERLAYS: ("track_structure", "clip_order", "record_inout"),
    X.F_STILLS: ("media_online", "image_sequence_misdetected"),
    X.F_DISSOLVES: ("transitions",),
    X.F_MARKERS: ("markers",),
    X.F_TITLES: ("titles",),
}


# ---- what the plan uses --------------------------------------------------------------------------------

@dataclass
class Usage:
    count: int = 0
    notes: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    ids: list[str] = field(default_factory=list)   # clip_instance_ids that use the feature (not written to the report in full)


def plan_usage(plan: EditPlan, spec: TimelineSpec, catalog: PlanningCatalog) -> dict[str, Usage]:
    from fractions import Fraction

    seq_fps = Fraction(plan.sequence.fps)
    clips = [c for s in plan.scenes for c in s.clips]
    use: dict[str, Usage] = {f: Usage() for f, _, _ in FEATURES}
    use[X.F_SETTINGS].count = 1
    v1 = sorted((p for p in spec.placements if p.track == "V1" and p.media_kind in ("video", "image")), key=lambda p: p.timeline_start_frame)
    use[X.F_CUTS].count = sum(1 for p in v1[1:] if p.transition_in.type != "dissolve")
    asked = [c for c in clips if c.transition_in.type == "dissolve" and c.media_type in ("video", "image")]
    placed = [p for p in spec.placements if p.transition_in.type == "dissolve"]
    use[X.F_DISSOLVES].count = len(asked)
    if len(asked) != len(placed):
        use[X.F_DISSOLVES].notes.append(f"{len(asked) - len(placed)} of {len(asked)} requested dissolve(s) were placed as hard cuts (alignment or "
                                        "handles the engine cannot place; see Mapping warnings in handoff_report.md)")
    use[X.F_DISSOLVES].data["placed"] = len(placed)
    stills = [c for c in clips if c.media_type == "image" and c.track_role == "primary_video"]
    use[X.F_STILLS].count = len(stills)
    digits = [catalog.assets[c.asset_id].relative_path for c in stills if c.asset_id in catalog.assets
              and re.search(r"\d", Path(catalog.assets[c.asset_id].relative_path).stem)]
    use[X.F_STILLS].data["with_digits_in_name"] = len(digits)
    use[X.F_OVERLAYS].count = sum(1 for c in clips if c.track_role == "overlay" and c.media_type in ("video", "image"))
    use[X.F_TITLES].count = len(plan.titles)
    use[X.F_MARKERS].count = len(plan.markers)
    use[X.F_GAIN].count = sum(1 for p in spec.placements if p.audio_gain_db not in (None, 0.0) and p.has_audio)
    use[X.F_MUTE].count = sum(1 for c in clips if c.media_type == "video" and c.track_role == "primary_video" and c.audio_policy == "muted"
                              and (catalog.assets.get(c.asset_id) is None or catalog.assets[c.asset_id].has_audio))
    use[X.F_MUSIC].count = sum(1 for c in clips if c.track_role == "music")
    use[X.F_AUDIO].count = sum(1 for c in clips if c.media_type == "audio" and c.track_role != "music")
    fps_off, res_off, orient_off = [], [], 0
    for c in clips:
        a = catalog.assets.get(c.asset_id)
        if a is None or c.media_type not in ("video", "image"):
            continue
        if c.media_type == "video" and a.fps is not None and a.fps != seq_fps:
            fps_off.append(c.clip_instance_id)
        w, h = a.display_width or a.width, a.display_height or a.height
        if w and h and (w, h) != (plan.sequence.width, plan.sequence.height):
            res_off.append(c.clip_instance_id)
            orient_off += a.orientation not in (None, "square") and a.orientation != ("portrait" if plan.sequence.height > plan.sequence.width else "landscape")
    use["mixed_fps"].ids, use["mixed_resolution"].ids = fps_off, res_off
    use["mixed_fps"].count = len(fps_off)
    use["mixed_fps"].data["clips"] = fps_off[:MAX_LISTED]
    use["mixed_resolution"].count = len(res_off)
    use["mixed_resolution"].data.update(clips=res_off[:MAX_LISTED], orientation_differs=orient_off)
    return use


# ---- inputs and the finish report ------------------------------------------------------------------------

@dataclass
class CompatInputs:
    plan: EditPlan
    spec: TimelineSpec                       # effective spec: what was sent to FilmCraft (dissolves already downgraded where needed)
    catalog: PlanningCatalog
    capabilities: Mapping[str, bool] | None  # FilmCraft capability name -> usable (None: no capability snapshot)
    xml_path: Path | None                    # exported timeline.xml (None: FCP7 XML was not exported)
    filmcraft_version: str | None = None
    verified_after_reopen: bool | None = None
    mapping_warnings: list[dict[str, Any]] = field(default_factory=list)
    finish_report: Mapping[str, Any] | None = None
    tool_version: str = ""
    fold_case: bool | None = None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_finish_report(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(report, None) or (None, why-not). The file is JSON written by resolve-finish; it is data, never executed."""
    if not path.is_file():
        return None, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"resolve_finish_report.json is not readable ({e})"
    return (doc, None) if isinstance(doc, dict) else (None, "resolve_finish_report.json is not a JSON object")


def usable_finish_checks(doc: Mapping[str, Any] | None, xml_sha256: str | None) -> tuple[dict[str, Mapping[str, Any]], dict[str, Any]]:
    """({check name: result}, info) from a resolve-finish report, only when it is a real run (not a dry run, not 'manual_required')
    of THIS timeline.xml. ``info`` says why it was not used."""
    info: dict[str, Any] = {"present": doc is not None, "used": False}
    if doc is None:
        return {}, info
    res = doc.get("resolve") or {}
    info.update(status=doc.get("status"), resolve=f"{res.get('product', '?')} {res.get('version', '')}".strip(),
                finished_at=doc.get("finished_at"), project=(doc.get("project") or {}).get("name"))
    checks = doc.get("final_checks")
    if doc.get("dry_run") or doc.get("status") not in ("pass", "partial", "fail") or not isinstance(checks, list) or not checks:
        info["reason"] = f"the report is not a completed run (status {doc.get('status')!r}{', dry run' if doc.get('dry_run') else ''})"
        return {}, info
    recorded = ((doc.get("inputs") or {}).get("timeline_xml_sha256"))
    if xml_sha256 and recorded and recorded != xml_sha256:
        info["reason"] = "the report was made from a different timeline.xml (this one was exported again since); run resolve-finish again"
        return {}, info
    info["used"] = True
    return {c["check"]: c for c in checks if isinstance(c, dict) and "check" in c}, info


# ---- rows ----------------------------------------------------------------------------------------------

def _filmcraft(i: CompatInputs, feature: str, usage: Usage) -> dict[str, Any]:
    if feature == X.F_SPEED:
        return {"status": "not_used", "capability": None, "detail": "the pipeline never generates speed changes"}
    cap = CAPABILITY[feature]
    if i.capabilities is None:
        return {"status": "unknown", "capability": cap, "detail": "no FilmCraft capability snapshot for this workspace"}
    if not i.capabilities.get(cap, False):
        return {"status": "unsupported", "capability": cap, "detail": f"this FilmCraft build does not offer the command the adapter uses for '{cap}'"}
    parts = ["the command the adapter uses is offered"]
    if i.verified_after_reopen is True:
        parts.append("the saved project read back equal to the plan after reopening")
    elif i.verified_after_reopen is False:
        return {"status": "unsupported", "capability": cap, "detail": "the saved project did NOT read back equal to the plan after reopening"}
    parts.extend(usage.notes)
    return {"status": "supported", "capability": cap, "detail": "; ".join(parts)}


def _xml_block(feature: str, usage: Usage, tl: Fcp7Timeline | None, rt: X.RoundTrip | None, xml_state: str) -> dict[str, Any]:
    if rt is None or tl is None:
        return {"status": xml_state, "expected": usage.count, "matched": None, "mismatches": []}
    mism = rt.for_feature(feature)
    if feature == X.F_CUTS:
        expected, matched = rt.expected.get("cuts_boundaries", 0), rt.matched.get("cuts_boundaries", 0)
    elif feature in ("mixed_fps", "mixed_resolution"):
        # no XML element of their own: the clips that use them must have matched in every compared field (record, source, path)
        ok_ids = {r["clip_instance_id"] for r in rt.clips if r.get("ok")}
        bad_ids = {r["clip_instance_id"] for r in rt.clips if not r.get("ok")}
        expected, matched = len(usage.ids), sum(1 for c in usage.ids if c in ok_ids and c not in bad_ids)
        if matched != expected and not mism:
            mism = [X.Mismatch(feature, c, "missing", detail="a clip using this feature differs in the XML (see the cuts / overlays rows)")
                    for c in usage.ids if c not in ok_ids or c in bad_ids]
    elif feature in (X.F_MUTE, X.F_SPEED):
        expected = usage.count
        matched = max(0, usage.count - len(mism)) if feature == X.F_MUTE else 0
    else:
        expected, matched = rt.expected.get(feature, 0), rt.matched.get(feature, 0)
    return {"status": "mismatch" if mism else "ok", "expected": expected, "matched": matched,
            "mismatches": [m.to_dict() for m in mism[:MAX_LISTED]], "mismatch_count": len(mism)}


def _resolve_block(feature: str, usage: Usage, finish: Mapping[str, Mapping[str, Any]], finish_info: Mapping[str, Any]) -> dict[str, Any]:
    ev = RE.evidence_for(feature)
    result, summary, manual = ev.result, ev.summary, ev.manual
    if feature == X.F_STILLS and not usage.data.get("with_digits_in_name"):
        # the recorded problem is specific to file names with digits (read as frame numbers); nothing is recorded for other names
        result, manual = RE.UNVERIFIED, None
        summary = ("No still in this plan has digits in its file name, so the recorded image-sequence misread is not expected; "
                   "stills without digits were not part of the recorded tests.")
    block: dict[str, Any] = {"status": result, "source": "recorded_evidence", "summary": summary, "reference": ev.reference,
                             "target": f"{RE.RESOLVE_TARGET['product']} {RE.RESOLVE_TARGET['version']} ({RE.RESOLVE_TARGET['os']})",
                             "checks": None}
    cover = FINISH_COVERAGE.get(feature)
    if finish and cover:
        got = {c: finish[c] for c in cover if c in finish}
        if got and all(r.get("status") in ("pass", "fail") for r in got.values()):
            failed = {c: r for c, r in got.items() if r["status"] == "fail"}
            res = finish_info.get("resolve") or "Resolve"
            block.update(source="resolve_finish_report", checks={c: {"status": r["status"], "detail": str(r.get("detail", ""))[:300]} for c, r in got.items()},
                         reference=f"resolve_handoff/{'resolve_finish_report.json'} ({finish_info.get('finished_at') or 'time not recorded'})")
            if failed:
                block.update(status="failed", summary=f"resolve-finish ({res}) read the timeline back and {', '.join(failed)} did not match: "
                             + "; ".join(f"{c}: {str(r.get('detail', ''))[:160]}" for c, r in failed.items()))
            else:
                block.update(status=RE.PROVEN, summary=f"resolve-finish ({res}) imported this workspace's timeline and the read-back checks "
                             f"{', '.join(got)} passed.")
                manual = None
    block["manual"] = manual
    return block


def _verdict(feature: str, fc: Mapping[str, Any], xml: Mapping[str, Any], rs: Mapping[str, Any]) -> tuple[str, str, str | None]:
    if fc["status"] == "unsupported":
        return "unsupported", fc["detail"], None
    if xml["status"] == "mismatch":
        n = xml.get("mismatch_count", len(xml.get("mismatches", [])))
        return "unsupported", f"the exported XML does not carry it as planned ({n} mismatch(es))", None
    if xml["status"] in ("not_exported", "unreadable"):
        return "unsupported", ("FCP7 XML was not exported, so nothing could be checked" if xml["status"] == "not_exported"
                               else "the exported FCP7 XML could not be read"), None
    if rs["status"] == "failed":
        return "manual", rs["summary"], rs.get("manual") or RE.RESOLVE_FINISH
    if rs.get("manual"):
        return "manual", {RE.NEEDS_REPAIR: "arrives broken in Resolve unless repaired (recorded)", RE.ABSENT: "not carried into Resolve (recorded)"}.get(
            rs["status"], "needs a check in Resolve"), rs["manual"]
    if rs["status"] == RE.PROVEN and fc["status"] == "supported":
        return "verified", "FilmCraft supports it, the XML carries it, and Resolve is " + (
            "measured by resolve-finish on this workspace" if rs["source"] == "resolve_finish_report" else "recorded to import it"), None
    why = {RE.PARTIAL: "Resolve result only partly recorded", RE.UNVERIFIED: "Resolve result unverified"}.get(rs["status"], "Resolve result not conclusive")
    return "expected", f"FilmCraft and the XML are fine; {why}", None


def build_compatibility_report(i: CompatInputs) -> dict[str, Any]:
    exp = X.expected_from_spec(i.spec)
    tl: Fcp7Timeline | None = None
    rt: X.RoundTrip | None = None
    xml_state, xml_error, xml_sha = "not_exported", None, None
    if i.xml_path is not None and Path(i.xml_path).is_file():
        xml_sha = sha256_of(Path(i.xml_path))
        try:
            tl = parse_fcp7_xml(Path(i.xml_path))
            rt = X.compare_timeline_xml(tl, exp, fold_case=i.fold_case)
            xml_state = "ok"
        except XmlReadError as e:
            xml_state, xml_error = "unreadable", str(e)
    finish, finish_info = usable_finish_checks(i.finish_report, xml_sha)
    usage = plan_usage(i.plan, i.spec, i.catalog)
    rows: list[dict[str, Any]] = []
    for feature, label, _cap in FEATURES:
        u = usage[feature]
        mism = rt.for_feature(feature) if rt else []
        if u.count == 0 and not mism:
            continue
        fc = _filmcraft(i, feature, u)
        xb = _xml_block(feature, u, tl, rt, xml_state)
        rs = _resolve_block(feature, u, finish, finish_info)
        verdict, reason, instruction = _verdict(feature, fc, xb, rs)
        rows.append({"feature": feature, "label": label, "plan_count": u.count, "details": u.data, "notes": u.notes,
                     "filmcraft": fc, "xml": xb, "resolve": rs, "verdict": verdict, "reason": reason, "instruction": instruction})
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICTS}
    overall = "problem" if counts["unsupported"] else "attention" if counts["manual"] else "ok"
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": utc_now_iso(), "tool_version": i.tool_version,
        "plan_hash": i.plan.plan_hash, "filmcraft_version": i.filmcraft_version,
        "resolve_evidence": {"evidence_version": RE.EVIDENCE_VERSION, "resolve": dict(RE.RESOLVE_TARGET),
                             "note": "Recorded results only (DESIGN §15.5, §15.6). Everything not recorded is 'unverified'."},
        "resolve_finish_report": finish_info,
        "xml": {"path": Path(i.xml_path).name if i.xml_path else None, "sha256": xml_sha, "status": xml_state, "error": xml_error,
                "sequence": None if tl is None else {"fps": str(tl.fps), "width": tl.width, "height": tl.height, "duration": tl.duration},
                "counts": tl.counts() if tl else None,
                "roundtrip": None if rt is None else {**rt.to_dict(), "plan_clips": sum(1 for e in exp.clips if e.kind == "video" and not e.is_title)}},
        "overall": overall, "counts": counts, "features": rows,
        "mapping_warnings": [w.get("message") for w in i.mapping_warnings][:MAX_LISTED],
    }


def summary_of(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Small block for stage summaries, the handoff report and `status`."""
    rt = (doc.get("xml") or {}).get("roundtrip")
    return {"overall": doc["overall"], **doc["counts"], "xml_status": doc["xml"]["status"],
            "xml_roundtrip_ok": None if rt is None else rt["ok"], "xml_mismatches": None if rt is None else len(rt["mismatches"]),
            "finish_report_used": doc["resolve_finish_report"].get("used", False)}


# ---- markdown ------------------------------------------------------------------------------------------

def _esc(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_compatibility_markdown(doc: Mapping[str, Any]) -> str:
    ev, xml, fin = doc["resolve_evidence"], doc["xml"], doc["resolve_finish_report"]
    c = doc["counts"]
    L = ["# Handoff compatibility report", "",
         f"- Overall: **{doc['overall']}** ({c['verified']} verified, {c['expected']} expected, {c['manual']} manual, {c['unsupported']} unsupported)",
         f"- Generated {doc['generated_at']} by trip-vlog {doc.get('tool_version') or '?'}; FilmCraft {doc.get('filmcraft_version') or 'unknown'}; "
         f"plan `{str(doc.get('plan_hash') or '')[:16]}`",
         f"- Recorded Resolve evidence (table v{ev['evidence_version']}): {ev['resolve']['product']} {ev['resolve']['version']} on {ev['resolve']['os']}, "
         f"through {ev['resolve']['file']}. "
         "Anything not recorded there is **unverified**; nothing is claimed for other Resolve versions or platforms.", ""]
    if fin.get("used"):
        L.append(f"- A real `resolve-finish` run of this timeline ({_esc(fin.get('resolve'))}, status {fin.get('status')}, {fin.get('finished_at') or 'time not recorded'}) "
                 "overrides the recorded evidence for the features its checks cover.")
    elif fin.get("present"):
        L.append(f"- `resolve_finish_report.json` exists but was not used: {_esc(fin.get('reason'))}.")
    else:
        L.append("- No `resolve_finish_report.json`: the Resolve column is the recorded evidence only. After `trip-vlog resolve-finish`, run "
                 "`trip-vlog resume` to refresh this report with the measured results.")
    L.append("")
    L += ["| Feature | In plan | FilmCraft | In XML | Resolve | Verdict |", "|---|---|---|---|---|---|"]
    for r in doc["features"]:
        x = r["xml"]
        xs = x["status"] if x["matched"] is None else f"{x['status']} ({x['matched']}/{x['expected']})"
        L.append(f"| {_esc(r['label'])} | {r['plan_count']} | {r['filmcraft']['status']} | {xs} | {r['resolve']['status']} "
                 f"({'measured' if r['resolve']['source'] == 'resolve_finish_report' else 'recorded'}) | **{r['verdict']}** |")
    L.append("")
    manual = [r for r in doc["features"] if r["verdict"] == "manual"]
    if manual:
        L += ["## Manual steps in Resolve", ""] + [f"- **{_esc(r['label'])}**: {_esc(r['instruction'])}" for r in manual] + [""]
    bad = [r for r in doc["features"] if r["verdict"] == "unsupported"]
    if bad:
        L += ["## Unsupported or not carried", ""] + [f"- **{_esc(r['label'])}**: {_esc(r['reason'])}" for r in bad] + [""]
    L += ["## Per feature", ""]
    for r in doc["features"]:
        L += [f"### {_esc(r['label'])} - {r['verdict']}", "", f"- Verdict: {_esc(r['reason'])}",
              f"- FilmCraft: {r['filmcraft']['status']}; {_esc(r['filmcraft']['detail'])}",
              f"- Resolve ({r['resolve']['source'].replace('_', ' ')}): {r['resolve']['status']}; {_esc(r['resolve']['summary'])} [{_esc(r['resolve']['reference'])}]"]
        for m in r["xml"]["mismatches"]:
            L.append(f"- XML mismatch: {_esc(m['ref'])}: {_esc(m['detail'])}")
        L.append("")
    L += ["## FCP7 XML round trip", ""]
    if xml["status"] != "ok":
        L += [f"`timeline.xml`: {xml['status']}" + (f" ({_esc(xml['error'])})" if xml.get("error") else "") + ". Nothing could be compared.", ""]
    else:
        rt, cn = xml["roundtrip"], xml["counts"]
        L += [f"`{xml['path']}` was parsed and compared with the timeline sent to FilmCraft ({rt['plan_clips']} video/still clip(s)) at frame precision "
              f"(tolerance {rt['tolerance_frames']} frame(s)): **{'identical' if rt['ok'] else str(len(rt['mismatches'])) + ' mismatch(es)'}**.",
              f"The XML holds {cn['video_clipitems']} video clip item(s), {cn['title_items']} title item(s), {cn['audio_clipitems']} audio clip item(s), "
              f"{cn['transitions']} transition(s), {cn['markers']} marker(s), {cn['gain_filters']} gain filter(s).", ""]
        for m in rt["mismatches"][:20]:
            L.append(f"- {_esc(m['feature'])}: {_esc(m['ref'])}: {_esc(m['detail'])}")
        if rt["mismatches"]:
            L.append("")
    if doc.get("mapping_warnings"):
        L += ["## Mapping warnings", ""] + [f"- {_esc(w)}" for w in doc["mapping_warnings"]] + [""]
    return "\n".join(L).rstrip("\n") + "\n"
