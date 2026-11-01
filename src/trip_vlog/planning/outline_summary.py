"""Readable summary of a story outline (`plans/story_outline.md`): what to review before approving."""

from __future__ import annotations

from typing import Any

from trip_vlog.planning.outline import StoryOutline
from trip_vlog.planning.outline_catalog import OutlineCatalog, OutlineMoment
from trip_vlog.planning.validate import ValidationReport

MAX_MOMENTS_SHOWN = 12


def _esc(text: str) -> str:
    return " ".join(text.split())


def _describe(m: OutlineMoment | None, moment_id: str) -> str:
    if m is None:
        return f"- `{moment_id}`: (not in the current catalog)"
    bits = [m.kind, f"{m.duration_seconds:.1f} s" if m.kind == "video" else "photo"]
    if m.capture:
        bits.append(m.capture)
    if m.interest is not None:
        bits.append(f"interest {m.interest:g}")
    if m.uncertain:
        bits.append("uncertain label")
    if m.audio_tags:
        bits.append("audio " + "/".join(m.audio_tags))
    text = _esc(m.description) if m.description else "(no description: not labeled yet)"
    line = f"- `{moment_id}` ({', '.join(bits)}): {text}"
    if m.transcript:
        line += f' Speech: "{_esc(m.transcript)}"'
    return line


def render_outline_summary(outline: StoryOutline, oc: OutlineCatalog, *, target_seconds: float, title: str | None = None,
                           report: ValidationReport | None = None, outline_hash: str | None = None, workspace: str = "<workspace>",
                           auto_approve: bool = False) -> str:
    total = outline.total_seconds()
    hook = outline.hook
    lines = [f"# {title + ': ' if title else ''}story outline", "",
             f"- Premise: {_esc(outline.premise)}",
             f"- Order: {outline.story_mode_used}",
             f"- Scenes: {len(outline.scenes)}, planned length {total:g} s against a target of {target_seconds:g} s",
             f"- Opening hook: " + (f"yes, `{hook.moment_id}`" + (f" ({_esc(hook.justification)})" if hook.justification else "") if hook.used else "no")]
    if outline_hash:
        lines.append(f"- Outline hash: `{outline_hash[:12]}`")
    lines.append("")
    for i, sc in enumerate(outline.scenes, 1):
        lines += [f"## {i}. {_esc(sc.title)}", "",
                  f"`{sc.scene_id}` | role: {sc.role} | {sc.target_duration_seconds:g} s | tone: {_esc(sc.tone)}", "",
                  _esc(sc.purpose)]
        if sc.chronology_note:
            lines += ["", f"Out of chronological order: {_esc(sc.chronology_note)}"]
        lines += ["", f"Candidate moments ({len(sc.candidate_moment_ids)}):"]
        for mid in sc.candidate_moment_ids[:MAX_MOMENTS_SHOWN]:
            lines.append(_describe(oc.eligible.get(mid), mid))
        if len(sc.candidate_moment_ids) > MAX_MOMENTS_SHOWN:
            lines.append(f"- ... and {len(sc.candidate_moment_ids) - MAX_MOMENTS_SHOWN} more in story_outline.json")
        lines.append("")
    lines += ["## Missing coverage", ""] + ([f"- {_esc(x)}" for x in outline.missing_coverage] or ["- none reported"]) + [""]
    lines += ["## Please confirm", ""] + ([f"- {_esc(x)}" for x in outline.uncertainties] or ["- nothing flagged"]) + [""]
    if report is not None and (report.errors or report.warnings or report.repairs):
        lines += ["## Checks", ""]
        lines += [f"- ERROR `{i.code}`: {_esc(i.message)}" for i in report.errors]
        lines += [f"- warning `{i.code}`: {_esc(i.message)}" for i in report.warnings]
        lines += [f"- repaired `{r.code}`: {_esc(r.message)}" for r in report.repairs]
        lines.append("")
    sel = oc.selection
    if sel.over_limit:
        lines += ["## Catalog size", "",
                  f"{sel.eligible} usable moments were available; {sel.kept} were shown to the story editor ({sel.dropped_total} left out: "
                  + ", ".join(f"{n} {why.replace('_', ' ')}" for why, n in sorted(sel.dropped_by_reason.items())) + "). "
                  "Raise `planning.max_moments_in_prompt` to show more.", ""]
    lines += ["## How to approve", ""]
    if auto_approve:
        lines += ["Automatic approval is on (`workflow.approval: auto_approve`); no review stop is made."]
    else:
        lines += ["Nothing has been selected or built yet. To change the story, edit `plans/story_outline.json`: reorder or retitle scenes, "
                  "change `target_duration_seconds`, add or remove moment IDs from `candidate_moment_ids`. IDs must exist in the catalog "
                  "(`media_index/moments.jsonl`). Then:", "",
                  "```", f"trip-vlog approve --output {workspace}", f"trip-vlog resume --output {workspace}", "```", "",
                  "An edited outline is checked again (without the LLM) and must be approved again."]
    return "\n".join(lines).rstrip("\n") + "\n"
