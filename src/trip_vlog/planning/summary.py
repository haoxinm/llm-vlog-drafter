"""Human-readable markdown summary of an edit plan (scenes, clips, durations, notes)."""

from __future__ import annotations

from fractions import Fraction

from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.duration import frames_to_float, parse_fps
from trip_vlog.planning.models import EditPlan
from trip_vlog.planning.validate import ValidationReport


def timecode(frames: int, fps: Fraction) -> str:
    """HH:MM:SS.mmm for a frame count (display only)."""
    ms = round(Fraction(frames) / fps * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_summary(plan: EditPlan, catalog: PlanningCatalog | None = None, report: ValidationReport | None = None) -> str:
    fps = parse_fps(plan.sequence.fps)
    seq = plan.sequence
    n_clips = sum(len(s.clips) for s in plan.scenes)
    lines = [
        f"# {plan.project.title}: edit plan summary", "",
        f"- Sequence: {seq.width}x{seq.height} ({seq.aspect_ratio}) at {seq.fps} fps",
        f"- Total: {plan.total_duration_seconds:.2f} s ({plan.total_duration_frames} frames) against a target of "
        f"{plan.project.target_duration_seconds:g} s",
        f"- Scenes: {len(plan.scenes)}, clips: {n_clips}, plan hash: `{plan.plan_hash[:12]}`",
    ]
    if report is not None:
        lines.append(f"- Validation: {len(report.errors)} error(s), {len(report.warnings)} warning(s), {len(report.repairs)} repair(s)")
    lines.append("")
    for sc in plan.scenes:
        lines += [f"## {sc.scene_id}: {_esc(sc.title)}", "",
                  f"{_esc(sc.purpose)}. Starts at {timecode(sc.start_frame, fps)}, lasts {frames_to_float(sc.duration_frames, fps):.2f} s "
                  f"(budget {sc.target_duration_seconds:g} s).", "",
                  "| Clip | Source | Source range | Length | Track | Audio | Transition |", "|---|---|---|---|---|---|---|"]
        for c in sc.clips:
            a = catalog.assets.get(c.asset_id) if catalog else None
            src = a.relative_path if a else c.asset_id
            rng = "still image" if c.media_type == "image" else f"{c.source_in_seconds:.3f} - {c.source_out_seconds:.3f} s"
            t = c.transition_in
            trans = "cut" if t.type == "cut" else f"dissolve {t.duration_seconds:.2f} s ({t.alignment})"
            lines.append(f"| {c.clip_instance_id} | {_esc(src)} | {rng} | {frames_to_float(c.duration_frames, fps):.2f} s | "
                         f"{c.track_role} | {c.audio_policy} | {trans} |")
        notes = [(c.clip_instance_id, n) for c in sc.clips for n in c.resolver_notes]
        if notes:
            lines += ["", "Resolver notes:"] + [f"- {cid}: {_esc(n)}" for cid, n in notes]
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
