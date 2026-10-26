"""handoff_report.md (DESIGN §15.1, §15.3): what was produced, how to import it, what is lost, what is unverified."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trip_vlog.handoff.interchange import DANGEROUS_FORMATS, RESOLVE_OBSERVATIONS, HandoffExport
from trip_vlog.logging_utils import utc_now_iso

OBSERVED_NOTE = "observed on Resolve 21.1 with the Phase 0 fixture"
STATIC_LIMITATIONS = (
    "Titles are not carried by any interchange format (OTIO: a clip with a missing media reference; FCP7 XML: a clipitem without "
    "media). Recreate them in Resolve from the list below, or use the preview as a reference.",
    "OTIO: an audio fade-out is exported as an SMPTE_Dissolve transition at the clip end.",
    "FCPXML: transitions on tracks other than V1 are dropped (audio fades are lost). " + f"FCPXML {RESOLVE_OBSERVATIONS['fcpxml']} ({OBSERVED_NOTE}).",
    f"EDL: only one video track (V1) is exported, and no frame rate is stored. EDL {RESOLVE_OBSERVATIONS['edl']} ({OBSERVED_NOTE}).",
    f"AAF: {RESOLVE_OBSERVATIONS['aaf']} ({OBSERVED_NOTE}).",
    "Effects and keyframes other than cuts, dissolves, markers and constant clip gain are not represented in the interchange files.",
    "Dissolves are centre-aligned only; a dissolve the plan wanted aligned to the cut start/end is placed as a hard cut (see Mapping warnings).",
)


def read_resolve_verification(path: Path) -> tuple[str, str]:
    """('absent' | 'passing' | 'not_passing' | 'unreadable', description) for resolve_verification.json."""
    if not path.is_file():
        return "absent", ""
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        status = str(doc.get("status", "")).lower()
    except (OSError, ValueError, AttributeError):
        return "unreadable", "resolve_verification.json exists but could not be read"
    res = doc.get("resolve") or {}
    where = ", ".join(x for x in (f"{res.get('product', 'Resolve')} {res.get('version', '')}".strip(), (doc.get("os") or {}).get("system")
                                  if isinstance(doc.get("os"), dict) else None) if x)
    if status == "pass":
        return "passing", f"status 'pass' ({where or 'Resolve version not recorded'}), written {doc.get('timestamp', 'at an unknown time')}"
    return "not_passing", f"status '{status or 'unknown'}' ({where or 'no Resolve session'})"


@dataclass
class HandoffReportInputs:
    title: str
    filmcraft_version: str | None
    tool_version: str
    platform: str
    out_dir: Path
    export: HandoffExport
    project_ref: dict[str, Any] | None
    preview_path: str | None
    plan_hash: str | None
    qc_status: str | None
    media_rows: list[dict[str, Any]]
    missing_media: list[str]
    mapping_warnings: list[dict[str, Any]] = field(default_factory=list)
    verification: tuple[str, str] = ("absent", "")
    stage_notes: list[str] = field(default_factory=list)  # e.g. "preview failed: ..."
    compatibility: dict[str, Any] | None = None           # handoff.compatibility.summary_of(...)
    captions: dict[str, Any] | None = None                # handoff.captions: what the optional SRT holds (None: captions are off)
    readback: dict[str, Any] | None = None                # handoff.readback.run_fallback record, when the fallback was needed


def _esc(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_handoff_report(i: HandoffReportInputs) -> str:
    ex = i.export
    L: list[str] = [f"# Resolve handoff report: {_esc(i.title)}", "",
                    f"- Generated: {utc_now_iso()} by trip-vlog {i.tool_version} on {i.platform}",
                    f"- FilmCraft version: {i.filmcraft_version or 'unknown'}",
                    f"- Edit plan hash: `{(i.plan_hash or 'n/a')[:16]}`; QC status: {i.qc_status or 'not run'}", ""]

    state, detail = i.verification
    L += ["## Resolve import status", ""]
    if state == "passing":
        L.append(f"**Verified by the user-run script** (`tools/resolve_verify.py`): {detail}. Only the checks listed in "
                 "`resolve_verification.json` are claimed; anything else is unverified.")
    else:
        L.append("**Resolve import is UNVERIFIED for this package.** No passing `resolve_verification.json` is present"
                 + (f" ({detail})" if detail else "") + ". Run the verification script on a machine with DaVinci Resolve open "
                 "(see Verification below); fidelity is not claimed until it passes.")
    L.append("")

    cp = i.compatibility
    if cp:
        L += ["## Compatibility report", "",
              f"See **`compatibility_report.md`** / `compatibility_report.json` in this folder: per feature the plan uses, what FilmCraft supports, "
              f"whether the exported FCP7 XML carries it (checked by parsing `timeline.xml` against the plan), and the RECORDED Resolve result. "
              f"Overall **{cp['overall']}**: {cp['verified']} verified, {cp['expected']} expected, {cp['manual']} needing a manual step, "
              f"{cp['unsupported']} unsupported" + (f", {cp['unknown']} unknown (not checked)" if cp.get("unknown") else "") + ". XML round trip: "
              + {True: "identical to the plan.", False: f"{cp['xml_mismatches']} mismatch(es) (listed there).",
                 None: f"not checked ({cp['xml_status'].replace('_', ' ')})."}[cp["xml_roundtrip_ok"]]
              + (" A real resolve-finish run was used for the Resolve column." if cp.get("finish_report_used") else ""), ""]

    if ex.dangerous_requested:
        L += ["> **WARNING: unsafe formats were explicitly requested.** " + "; ".join(
            f"`{k}`: {RESOLVE_OBSERVATIONS[k]}" for k in ex.dangerous_requested) + f" ({OBSERVED_NOTE}). "
            "Do not open these on a machine with unsaved Resolve work. FCP7 XML is the recommended file.", ""]

    L += ["## Files", ""]
    if ex.error and not ex.report:
        L += [f"**Interchange export FAILED**: {_esc(ex.error)}", "",
              "Fallback (DESIGN §15.3): no timeline file was produced, and none was substituted. What you still have:", "",
              "- the FilmCraft project (`project_reference.json` points to it); open it in FilmCraft to continue editing,",
              "- `edit_plan.json` (the validated plan), `media_manifest.csv`, and the draft preview (reference only, not editable),",
              "- this report. Retry with `trip-vlog resume` after fixing the cause above.", ""]
    elif ex.report:
        rep = ex.report
        L += ["| Format | Role | Resolve 21.1 observation | Exported | Checks | File |", "|---|---|---|---|---|---|"]
        for f in rep.formats:
            failed = [c for c in f.checks if not c.ok]
            checks = "all passed" if f.exported and not failed else (f"{len(failed)} failed" if failed else "-")
            role = "PRIMARY" if f is rep.formats[0] else "secondary"
            L.append(f"| {f.format} | {role} | "
                     f"{_esc(RESOLVE_OBSERVATIONS.get(f.format, 'not observed'))} | {'yes' if f.exported else 'NO: ' + _esc(f.error or '')} | "
                     f"{checks} | `{Path(f.path).name}` |")
        L.append("")
        for f in rep.formats:
            for c in (c for c in f.checks if not c.ok):
                L.append(f"- {f.format} check failed: {_esc(c.name)}: {_esc(c.detail)}")
            for w in f.warnings:
                L.append(f"- {f.format} warning: {_esc(w)}")
        if ex.otio_reload:
            L.append(f"- OpenTimelineIO library reload of timeline.otio: **{ex.otio_reload.get('status')}** "
                     f"{_esc(ex.otio_reload.get('detail') or '(' + str(ex.otio_reload.get('frames')) + ' frames, ' + str(ex.otio_reload.get('clips')) + ' clips)')}")
        L.append("")
        if rep.formats:
            first = rep.formats[0].format
            L += [f"Import the **{first}** file first"
                  + (" (verified to import in Resolve 21.1 with the Phase 0 fixture)." if first == "fcp7_xml" else "."), ""]
    rb = i.readback
    if rb:
        L += ["## Read-back interchange (DESIGN §15.3)", ""]
        nat = rb.get("native") or {}
        if rb["status"] == "generated":
            L += [f"**`{rb['file']}` was generated from the FilmCraft read-back**, not by FilmCraft's exporter: {_esc(rb['reason'])}.",
                  "The saved project was reopened in a fresh FilmCraft process, its timeline was read through FilmCraft's inspection interface, and the XML was written from "
                  "that, never from the edit plan. The file was then parsed again and compared with the timeline sent to FilmCraft: it round-trips exactly "
                  f"({rb.get('clips')} clip item(s)). SHA-256 `{rb.get('sha256')}`.", ""]
            if rb.get("kept_native"):
                L += [f"FilmCraft's own (lossy) export is kept as `{rb['kept_native']}`.", ""]
        elif rb["status"] == "native_lossy_kept":
            L += [f"**FilmCraft's own `{rb.get('file')}` is lossy but was kept**: {_esc(rb['reason'])}.", ""]
            L += [f"Differences ({rb.get('diff_count')}):", ""] + [f"- {_esc(d.get('detail') or d.get('ref'))}" for d in rb.get("diff", [])] + [""]
        else:
            L += [f"**The read-back fallback was needed but did not produce a usable file**: {_esc(rb['reason'])}.", ""]
        if nat.get("issues"):
            L += [f"FilmCraft's own FCP7 XML export was **{nat.get('state')}**:", ""] + [f"- {_esc(x)}" for x in nat["issues"]] + [""]
        cc = rb.get("crosscheck") or {}
        L += ["Cross-check of the read-back against the resolved edit plan (clip order, source ranges, tracks, durations): "
              + ("**no differences**." if cc.get("ok") else f"**{cc.get('count')} difference(s)**, reported, not papered over:"), ""]
        L += [f"- {_esc(x)}" for x in cc.get("items", [])] + ([""] if cc.get("items") else [])
        if rb.get("not_carried"):
            L += ["Present in FilmCraft but not carried by this file:", ""] + [f"- {_esc(x)}" for x in rb["not_carried"]] + [""]
        if rb.get("loses"):
            L += ["What a file generated from the read-back does not carry:", ""] + [f"- {_esc(x)}" for x in rb["loses"]] + [""]
    cap = i.captions
    if cap:
        if cap.get("file"):
            L += [f"Captions: **`{cap['file']}`** ({cap['cues']} cue(s) from {cap['clips_captioned']} clip(s); {cap['encoding']}). The words are the speech transcript's own, "
                  f"never rewritten; timing is mapped through the edit plan, and where a transcript segment is cut by a clip or split into cues the position of words "
                  f"inside the segment is an estimate (the transcript has segment timing only). {len(cap['clips_skipped'])} clip(s) have none (reasons in `captions_report.json`). "
                  "Check them against the speech before publishing.", ""]
        else:
            L += ["Captions were requested (`captions.enabled`) but no clip has usable transcript text, so there is no SRT (see `captions_report.json`).", ""]
    L += ["Also in this folder: `media_manifest.csv`, `edit_plan.json` (validated plan), `project_reference.json` "
          "(path and hash of the FilmCraft `.fcproj`; the project is not copied) and this report.",
          f"Draft preview (reference only, not copied): `{i.preview_path or 'not rendered'}`.", ""]

    L += ["## Import into DaVinci Resolve", "",
          "1. Keep the original media at the absolute paths in `media_manifest.csv` (nothing was copied).",
          f"2. Create or open a Resolve project with the sequence frame rate and resolution of the draft (see `edit_plan.json` / the FilmCraft project).",
          "3. File > Import > Timeline..., choose `timeline.xml` (FCP7 XML) first; use `timeline.otio` if your Resolve version supports OTIO import.",
          "4. If clips show offline, use Media Pool > Relink Selected Clips and point Resolve at the original media folder.",
          "5. Recreate titles from the list below (they are not carried), then check cuts, transitions and audio.",
          *([f"6. Captions are a MANUAL step: import `{cap['file']}` yourself (in Resolve: File > Import > Subtitle..., menu wording varies by version) and check that it "
             "lines up with the speech; if it is offset, compare with the timeline's start timecode. trip-vlog does not import subtitles, and importing this file "
             "has not been verified in Resolve (see the runbook in DESIGN.md)."] if cap and cap.get("file") else []),
          "", "## Verification", "",
          "`python tools/resolve_verify.py --handoff <this folder>` imports these files into a scratch Resolve project and writes "
          "`resolve_verification.json` here. See `docs/resolve-verification.md` for setup (Resolve scripting must be enabled); if the scripting API "
          "is unavailable the script prints a manual checklist. Re-run `trip-vlog resume` afterwards to refresh this report.", ""]

    L += ["## Known limitations", "", f"From the Phase 0 spike ({OBSERVED_NOTE}):", ""] + [f"- {x}" for x in STATIC_LIMITATIONS] + [""]
    if ex.report:
        L += ["Losses reported for the formats exported in this run:", ""]
        for f in ex.report.formats:
            for x in f.loses:
                L.append(f"- {f.format}: {_esc(x)}")
        for x in ex.report.limitations:
            L.append(f"- {_esc(x)}")
        L.append("")
        if ex.report.titles_not_carried:
            L += ["Titles that must be recreated:", ""] + [f"- \"{_esc(t['text'])}\" at frame {t['start_frame']} for {t['duration_frames']} frames on {t['track']}"
                                                            for t in ex.report.titles_not_carried] + [
                "`trip-vlog resolve-finish` can recreate them as editable Resolve titles (experimental, Studio only); "
                "otherwise add them by hand. The FilmCraft project keeps them as editable text clips.", ""]
        else:
            L += ["This draft contains no titles.", ""]

    if i.mapping_warnings:
        L += ["## Mapping warnings", ""] + [f"- {_esc(w.get('clip_instance_id') or '')}: {_esc(w['message'])}" for w in i.mapping_warnings] + [""]

    L += ["## Media", "", f"{len(i.media_rows)} asset(s) listed in `media_manifest.csv`; "
          f"{sum(1 for r in i.media_rows if r['used_in_timeline'] == 'yes')} used in the timeline."]
    if i.missing_media:
        L += ["", "**Missing media (used in the timeline but not found on disk):**"] + [f"- `{m}`" for m in i.missing_media]
    else:
        L.append("All media used in the timeline was found on disk when this report was written.")
    L += ["", "**Absolute paths:** all media references in the timeline files and in `media_manifest.csv` are absolute. If you move this "
          "package or the media to another machine or drive, the links break and must be relinked in Resolve (or re-exported after "
          "updating the media location).", ""]
    if i.stage_notes:
        L += ["## Notes from the run", ""] + [f"- {_esc(n)}" for n in i.stage_notes] + [""]
    return "\n".join(L).rstrip("\n") + "\n"
