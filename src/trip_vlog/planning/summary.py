"""Human-readable markdown summary of an edit plan (scenes, clips, durations, titles, notes).

Baseline plans get the compact table. A plan made by the LLM planner (pass ``selection`` and, for the moment descriptions,
``oc``) additionally shows, per scene: the outline role and budget, each clip's moment description and the model's reason,
then the titles, resolver notes, automatic repairs and the planner's uncertainties.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.duration import frames_to_float, parse_fps
from trip_vlog.planning.models import EditPlan, SelectionPlan
from trip_vlog.planning.validate import ValidationReport


def timecode(frames: int, fps: Fraction) -> str:
    """HH:MM:SS.mmm for a frame count (display only)."""
    ms = round(Fraction(frames) / fps * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _esc(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def _short(text: str, limit: int = 140) -> str:
    t = _esc(text)
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _moment_text(oc: Any, moment_id: str) -> str:
    m = oc.eligible.get(moment_id) if oc is not None else None
    if m is None or not m.description:
        return "(no description)"
    return _short(m.description, 110)


def render_summary(plan: EditPlan, catalog: PlanningCatalog | None = None, report: ValidationReport | None = None, *,
                   selection: SelectionPlan | None = None, oc: Any = None, outline: Any = None, usage: dict[str, Any] | None = None,
                   approval_note: str | None = None) -> str:
    fps = parse_fps(plan.sequence.fps)
    seq = plan.sequence
    n_clips = sum(len(s.clips) for s in plan.scenes)
    llm = selection is not None and selection.planner == "llm"
    role_of = {s.scene_id: s.role for s in outline.scenes} if outline is not None else {}
    lines = [
        f"# {plan.project.title}: edit plan summary", "",
        f"- Planner: {selection.planner if selection is not None else 'baseline'}",
        f"- Sequence: {seq.width}x{seq.height} ({seq.aspect_ratio}) at {seq.fps} fps",
        f"- Total: {plan.total_duration_seconds:.2f} s ({plan.total_duration_frames} frames) against a target of "
        f"{plan.project.target_duration_seconds:g} s",
        f"- Scenes: {len(plan.scenes)}, clips: {n_clips}, titles: {len(plan.titles)}, plan hash: `{plan.plan_hash[:12]}`",
    ]
    if report is not None:
        lines.append(f"- Validation: {len(report.errors)} error(s), {len(report.warnings)} warning(s), {len(report.repairs)} repair(s)")
    if usage:
        lines.append(f"- LLM: {usage.get('requests', 0)} request(s), {usage.get('input_tokens', 0)} input / {usage.get('output_tokens', 0)} output tokens")
    if approval_note:
        lines.append(f"- Approval: {approval_note}")
    lines.append("")
    for sc in plan.scenes:
        head = _esc(sc.purpose).rstrip(".") + ". " if sc.purpose.strip() else ""
        role = f" Role: {role_of[sc.scene_id].replace('_', ' ')}." if sc.scene_id in role_of else ""
        lines += [f"## {sc.scene_id}: {_esc(sc.title)}", "",
                  f"{head}Starts at {timecode(sc.start_frame, fps)}, lasts {frames_to_float(sc.duration_frames, fps):.2f} s "
                  f"(budget {sc.target_duration_seconds:g} s).{role}", ""]
        if llm:
            lines += ["| Clip | Moment | Length | Anchor | Track | Audio | Transition | Why |", "|---|---|---|---|---|---|---|---|"]
        else:
            lines += ["| Clip | Source | Source range | Length | Track | Audio | Transition |", "|---|---|---|---|---|---|---|"]
        for c in sc.clips:
            a = catalog.assets.get(c.asset_id) if catalog else None
            src = a.relative_path if a else c.asset_id
            rng = "still image" if c.media_type == "image" else f"{c.source_in_seconds:.3f} - {c.source_out_seconds:.3f} s"
            t = c.transition_in
            trans = "cut" if t.type == "cut" else f"dissolve {t.duration_seconds:.2f} s ({t.alignment})"
            length = f"{frames_to_float(c.duration_frames, fps):.2f} s"
            if llm:
                why = _short(c.rationale, 120) + (f" (repeat: {_short(c.intentional_repeat_reason, 80)})" if c.intentional_repeat_reason else "")
                lines.append(f"| {c.clip_instance_id} | {_esc(src)}: {_moment_text(oc, c.moment_id)} ({rng}) | {length} | {c.anchor} | {c.track_role} | "
                             f"{c.audio_policy} | {trans} | {why} |")
            else:
                lines.append(f"| {c.clip_instance_id} | {_esc(src)} | {rng} | {length} | {c.track_role} | {c.audio_policy} | {trans} |")
        notes = [(c.clip_instance_id, n) for c in sc.clips for n in c.resolver_notes]
        if notes:
            lines += ["", "Resolver notes:"] + [f"- {cid}: {_esc(n)}" for cid, n in notes]
        lines.append("")
    if plan.titles:
        lines += ["## Titles", "", "Simple text on V2; editable in FilmCraft and recreated by `trip-vlog resolve-finish` (not carried by the interchange files).", "",
                  "| Title | Text | Scene | Starts | Length | Source |", "|---|---|---|---|---|---|"]
        for t in plan.titles:
            src = {"project_title": "project.title", "outline_scene_title": "outline scene title", "user_config": "your config",
                   "folder_name": "FROM FOLDER NAME (unverified)", "user": "typed by you"}[t.text_source]
            lines.append(f"| {t.title_id} ({t.role.replace('_', ' ')}) | {_esc(t.text)} | {t.scene_id} | {timecode(t.start_frame, fps)} | "
                         f"{frames_to_float(t.duration_frames, fps):.2f} s | {src} |")
        tnotes = [(t.title_id, n) for t in plan.titles for n in t.resolver_notes]
        if tnotes:
            lines += ["", "Title notes:"] + [f"- {tid}: {_esc(n)}" for tid, n in tnotes]
        lines.append("")
    if plan.uncertainties:
        lines += ["## Uncertainties", ""] + [f"- {_esc(u)}" for u in plan.uncertainties] + [""]
    if report is not None and (report.errors or report.warnings or report.repairs):
        lines += ["## Validation", ""]
        lines += [f"- ERROR `{i.code}`: {_esc(i.message)}" for i in report.errors]
        lines += [f"- warning `{i.code}`: {_esc(i.message)}" for i in report.warnings]
        lines += [f"- repaired `{r.code}`: {_esc(r.message)}" for r in report.repairs]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
