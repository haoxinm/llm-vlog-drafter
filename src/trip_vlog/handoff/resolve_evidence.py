"""What is RECORDED about DaVinci Resolve for each handoff feature (DESIGN §15.2, §15.5, §15.6) - a static, versioned table.

Rules for this file
  * An entry says "proven" only for what DESIGN §15.5 / §15.6 (and the Phase 0 notes it summarises) record as observed on
    **DaVinci Resolve Studio 21.1.1.10 on Windows**, through the FCP7 XML handoff file. Nothing is extrapolated to other
    versions, other platforms, other formats or other kinds of media.
  * Everything else is ``unverified``. "Not reported" is not "works".
  * Change an entry only when a new real Resolve run is recorded in DESIGN.md; bump ``EVIDENCE_VERSION`` with it.

A real ``resolve-finish`` run in the workspace (``resolve_handoff/resolve_finish_report.json``) can override an entry for that
workspace; ``trip_vlog.handoff.compatibility`` does that from the per-check results of the report, never from this table.
"""

from __future__ import annotations

from dataclasses import dataclass

EVIDENCE_VERSION = 1
RESOLVE_TARGET = {"product": "DaVinci Resolve Studio", "version": "21.1.1.10", "os": "Windows", "file": "FCP7 XML (timeline.xml)"}

# results
PROVEN = "proven"                  # observed to work on RESOLVE_TARGET
PARTIAL = "partial"                # part of the feature observed, the rest not reported
NEEDS_REPAIR = "needs_repair"      # observed to arrive broken; a repair is recorded
ABSENT = "absent_on_import"        # observed NOT to arrive
UNVERIFIED = "unverified"          # nothing recorded
RESULTS = (PROVEN, PARTIAL, NEEDS_REPAIR, ABSENT, UNVERIFIED)

RESOLVE_FINISH = "Run `trip-vlog resolve-finish --output <workspace>` with Resolve Studio open (external scripting: Local)."


@dataclass(frozen=True)
class Evidence:
    feature: str
    result: str
    summary: str             # what was recorded, in the words of the record
    reference: str           # where it is recorded
    manual: str | None = None   # what the user has to do in Resolve when this feature is in the plan


EVIDENCE: dict[str, Evidence] = {e.feature: e for e in (
    Evidence("timeline_settings", PARTIAL,
             "Frame rate (30) and duration (195 frames) of the Phase 0 fixture were correct after import via ImportTimelineFromFile. "
             "Resolution was not reported.",
             "DESIGN §15.5 (scripted verification, Studio 21.1.1.10); docs/phase0-filmcraft-spike.md §8.1"),
    Evidence("cuts", PROVEN,
             "Clip order and record and source in/out of the clips on V1 of the Phase 0 fixture (a landscape clip and a still) were correct "
             "and the media was linked (the still excepted, see 'stills'). The fixture has no hard cut between two clips, so hard cuts as "
             "such were not recorded separately.",
             "DESIGN §15.5 (scripted verification, Studio 21.1.1.10); docs/phase0-filmcraft-spike.md §8, §8.1"),
    Evidence("dissolves", PROVEN,
             "The two transitions of the Phase 0 fixture were found as timeline items: one cross dissolve centred on the V1 cut, plus the "
             "audio fade (listed by Resolve as 'Final Cut Pro 7', ending at the clip end). Their look was not reported.",
             "DESIGN §15.5; docs/phase0-filmcraft-spike.md §8.1 (scripted runs)"),
    Evidence("stills", NEEDS_REPAIR,
             "A still whose file name contains digits (e.g. still_1080p.png, IMG_1042.JPG) is misread as an image sequence and arrives "
             "offline; `resolve-finish` repairs it with ReplaceClip, using an isolated copy of the file when the original has numbered "
             "siblings (proven on a 60-still workspace).",
             "DESIGN §15.5, §15.6 step 2",
             manual="Stills with digits in their file name arrive offline. Run `trip-vlog resolve-finish` (repairs them), or in Resolve "
                    "copy each offline still into its own folder and use Replace Clip on it. Do not move the copies away from the project."),
    Evidence("overlays_v2", PROVEN,
             "The Phase 0 fixture has a portrait clip on V2 (frames 60-120); tracks, clip order and record/source in/out of the imported "
             "timeline were correct.",
             "docs/phase0-filmcraft-spike.md §8 (fixture) and §8.1 (scripted run: tracks, clip order, record and source in/out); DESIGN §15.5"),
    Evidence("titles", ABSENT,
             "Titles are absent after the FCP7 XML import (a third-party report says they arrive as media-less clips). "
             "Recreating them with `resolve-finish` is implemented but has not been exercised on a real Resolve.",
             "DESIGN §15.5, §15.6 step 4",
             manual="Titles are not carried by the XML. Recreate them from the list in handoff_report.md (text, track V2, frames), or try "
                    "`trip-vlog resolve-finish` (experimental)."),
    Evidence("markers", PROVEN,
             "The scene marker survived the FCP7 XML import (third-party measurements say markers can be dropped; `resolve-finish` re-adds "
             "missing ones).",
             "DESIGN §15.5, §15.6 step 3"),
    Evidence("audio_gain", UNVERIFIED,
             "The audio level (-6 dB) of the Phase 0 fixture was not reported.",
             "docs/phase0-filmcraft-spike.md §8.1 (\"audio levels) was not reported yet\")"),
    Evidence("audio_mute", UNVERIFIED,
             "A video clip placed without its audio was not part of the recorded tests.",
             "DESIGN §15.5 (nothing recorded)"),
    Evidence("music_track", PARTIAL,
             "The music clip on A2 of the Phase 0 fixture was part of the compared track structure; its level (-6 dB) was not reported. "
             "The fixture's audio fade was found as a timeline item ('Final Cut Pro 7') ending at the clip end.",
             "docs/phase0-filmcraft-spike.md §8, §8.1; DESIGN §15.5"),
    Evidence("audio_clips", UNVERIFIED, "Stand-alone audio clips other than music were not part of the recorded tests.",
             "DESIGN §15.5 (nothing recorded)"),
    Evidence("mixed_fps", UNVERIFIED, "Nothing is recorded about source clips whose frame rate differs from the sequence.",
             "DESIGN §15.5 (nothing recorded)"),
    Evidence("mixed_resolution", UNVERIFIED,
             "Image sizing and the scale of a portrait clip in a landscape timeline were to be compared (§15.2) but were not reported.",
             "DESIGN §15.2 (to be compared), §15.5 (nothing recorded); docs/phase0-filmcraft-spike.md §8 step 3, §8.1 (not reported)",
             manual="Review the framing of clips whose aspect ratio or orientation differs from the timeline (Inspector > Scaling / Crop); "
                    "portrait clips in a landscape timeline are flagged by the QC `orientation` check."),
    Evidence("speed_changes", UNVERIFIED, "Speed changes are never generated by this pipeline and were not tested in Resolve.",
             "DESIGN §15.5 (nothing recorded)"),
)}


def evidence_for(feature: str) -> Evidence:
    return EVIDENCE.get(feature) or Evidence(feature, UNVERIFIED, "nothing recorded", "DESIGN §15.5 (nothing recorded)")
