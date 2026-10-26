"""What the planner may ask for, in editor-neutral words (DESIGN §8.3, §9.3: "declared capability map").

Built from the plan enums (track roles, transitions, audio policies), the project settings and, when a FilmCraft capability
snapshot exists in the workspace, from what the installed editor offered. The text never names editor commands, parameters or
internal time units. With no snapshot the editor is assumed to offer everything the adapter requires (it refuses to start
otherwise), so the text is the same before and after the first assemble and does not invalidate the plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trip_vlog.planning.models import TITLE_MAX_CHARS, TITLE_MAX_SECONDS, TITLE_MIN_SECONDS

SNAPSHOT_RELATIVE = ("filmcraft", "capability_snapshot.json")


def read_snapshot(workspace_root: Path | str) -> dict[str, Any] | None:
    p = Path(workspace_root).joinpath(*SNAPSHOT_RELATIVE)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _usable(snapshot: dict[str, Any] | None, name: str) -> bool:
    if snapshot is None:
        return True
    cap = (snapshot.get("capabilities") or {}).get(name)
    return bool(cap and cap.get("usable", cap.get("found")))


def capability_lines(config: Any, snapshot: dict[str, Any] | None = None) -> list[str]:
    """One line per capability, ready for the prompt."""
    e, p = config.editorial, config.project
    lines = ["- Tracks: primary_video is the main picture line (video clips and photos, played one after another). overlay is a second "
             "picture line without its own sound, rarely useful. Music and narration are not placed by this step."]
    if _usable(snapshot, "add_transition"):
        lines.append("- Transitions: cut (default) and dissolve (a cross-dissolve of 0.3 to 1.5 seconds between two primary_video clips). A "
                     "dissolve needs spare footage around the cut; where there is none it becomes a cut automatically. No other transitions, "
                     "speed changes, effects, zooms or motion exist.")
    else:
        lines.append("- Transitions: only cut. Dissolves are not available in the installed editor; use cut everywhere.")
    lines.append("- Sound: natural_sound (the clip's own audio), ambient_low (the clip's audio, reduced) and muted. No ducking, fades or mixing "
                 "decisions are made here.")
    if e.scene_titles and _usable(snapshot, "add_title"):
        lines.append(f"- Titles: scene titles are enabled. Simple text, one line of up to {TITLE_MAX_CHARS} characters, shown "
                     f"{TITLE_MIN_SECONDS:g} to {TITLE_MAX_SECONDS:g} seconds in a fixed lower-left style; at most {e.max_titles} titles in the whole "
                     "video"
                     + (", counting the opening title" if e.opening_title else "") + (" and the closing title" if e.closing_title else "") + ".")
    else:
        lines.append("- Titles: you do not add any; titles is an empty array.")
    lines.append(f"- Output: {p.aspect_ratio} at {p.frame_rate} frames per second. Photos can be any length; clips shorter than a moment "
                 "are cut from the place the anchor names.")
    return lines


def capability_facts(config: Any, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """The facts behind ``capability_lines`` (for cache keys and reports)."""
    return {"dissolve": _usable(snapshot, "add_transition"), "titles": bool(config.editorial.scene_titles and _usable(snapshot, "add_title")),
            "alignments": ["center"], "track_roles": ["primary_video", "overlay"]}
