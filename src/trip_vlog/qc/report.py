"""Run all QC checks and write preview/qc_report.json + preview/contact_sheet_timeline.jpg (DESIGN §14)."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from trip_vlog.filmcraft.inspect import ProjectSnapshot, SequenceSnapshot
from trip_vlog.filmcraft.preview import run_ffprobe
from trip_vlog.logging_utils import utc_now_iso
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan
from trip_vlog.qc import checks as C
from trip_vlog.qc.preview import cut_times_from_starts, make_timeline_sheet
from trip_vlog.state import atomic_write_json

QC_SCHEMA = 1


@dataclass
class QcInputs:
    plan: EditPlan
    catalog: PlanningCatalog
    source_paths: list[str]
    sequence: SequenceSnapshot | None        # read back from the reopened project (None if reopening failed)
    project: ProjectSnapshot | None
    reopen_error: str | None
    mismatches: list[str]
    preview: Path
    report_path: Path
    contact_sheet: Path
    ffmpeg: Path | None
    ffprobe: str | None
    config: Any  # trip_vlog.config.Config
    extra: dict[str, Any] = field(default_factory=dict)


def run_qc(i: QcInputs) -> dict[str, Any]:
    cfg, plan = i.config, i.plan
    q, p = cfg.qc, cfg.project
    results: list[C.CheckResult] = []
    results.append(C.check_media_online(i.project, i.source_paths))
    results.append(C.check_project_reopens(i.sequence is not None, i.mismatches, i.reopen_error))
    if i.sequence is not None:
        results.append(C.check_v1_gaps(i.sequence))
    results.append(C.check_duplicates(plan, i.catalog))
    results.append(C.check_stills(plan, q.min_still_seconds))
    seq = i.sequence
    if seq is not None:
        results.append(C.check_total_duration(seq, float(p.target_duration_seconds), p.duration_tolerance_pct, p.duration_tolerance_seconds))
    else:  # fall back to the plan's own frame count
        fps = Fraction(plan.sequence.fps)
        actual = float(Fraction(plan.total_duration_frames) / fps)
        tol = max(float(p.target_duration_seconds) * p.duration_tolerance_pct / 100, p.duration_tolerance_seconds)
        ok = abs(actual - float(p.target_duration_seconds)) <= tol
        results.append(C.CheckResult("total_duration", C.PASS if ok else C.FAIL, f"plan duration {actual:.2f}s (read-back unavailable)"))

    probe = None
    perr = None
    if i.preview.is_file():
        try:
            probe = run_ffprobe(i.preview, i.ffprobe)
        except Exception as e:  # noqa: BLE001
            perr = str(e)
    else:
        perr = f"preview file {i.preview.name} was not rendered"
    fps = Fraction(plan.sequence.fps)
    frames = seq.duration_frames if seq is not None else plan.total_duration_frames
    expect_audio = bool(seq and any(t.kind == "audio" and t.clips for t in seq.tracks)) if seq is not None else any(
        c.audio_policy != "muted" and c.media_type == "video" for s in plan.scenes for c in s.clips)
    results.append(C.check_preview(probe, width=plan.sequence.width, height=plan.sequence.height, fps=fps, frames=frames,
                                   expect_audio=expect_audio, scale=cfg.filmcraft.preview_scale, error=perr))

    black = berr = None
    if probe is not None and i.ffmpeg is not None:
        try:
            black = C.detect_black(i.ffmpeg, i.preview, q.black_min_seconds, q.black_picture_threshold, q.black_pixel_threshold)
        except Exception as e:  # noqa: BLE001
            berr = str(e)
    elif probe is not None:
        berr = "ffmpeg not found; black-frame detection skipped"
    results.append(C.check_black_frames(black, berr))
    results.append(C.check_orientation(plan, i.catalog))

    sheet: dict[str, Any] = {"path": None, "frames": 0}
    if probe is not None and i.ffmpeg is not None:
        starts = [c.start_frame for c in (seq.track("V1").clips if seq is not None and seq.track("V1") else [])] or \
                 [c.timeline_start_frame for s in plan.scenes for c in s.clips if c.track_role == "primary_video"]
        dur = float(Fraction(frames) / fps)
        res = make_timeline_sheet(i.ffmpeg, i.preview, i.contact_sheet, cut_times_from_starts(starts, fps), dur,
                                  columns=q.contact_sheet_columns, tile_width=q.contact_sheet_tile_width, max_frames=q.contact_sheet_max_frames)
        sheet = {"path": res.path.name if res.path else None, "frames": res.frames, "failed_frames": res.failed, "error": res.error}
        if res.error or res.failed:
            results.append(C.CheckResult("contact_sheet", C.WARN, res.error or f"{len(res.failed)} frame(s) could not be extracted",
                                         {"failed": res.failed[:10]}))
    status = C.overall_status(results)
    counts = {s: sum(1 for r in results if r.status == s) for s in (C.PASS, C.WARN, C.FAIL)}
    doc = {"schema_version": QC_SCHEMA, "generated_at": utc_now_iso(), "status": status, "counts": counts,
           "plan_hash": plan.plan_hash, "preview": i.preview.name, "contact_sheet": sheet,
           "checks": [r.to_dict() for r in results]}
    atomic_write_json(i.report_path, doc)
    return doc
