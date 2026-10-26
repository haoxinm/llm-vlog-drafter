"""Run all QC checks and write preview/qc_report.json + .md, the preview contact sheets and qc/preview_sheet.json (DESIGN §14).

Seam for the (Phase 5b) critic: a critic reads ``qc/preview_sheet.json`` (tiles -> record time, clip, scene, side; sheet files) and
the ``checks`` of ``qc_report.json`` (each names clips and scenes in ``data``), and adds its advisory findings as ``extra_results``
(never ``fail``: subjective findings do not fail a draft). The report carries a ``critic`` block that says it has not run."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.filmcraft.inspect import ProjectSnapshot, SequenceSnapshot
from trip_vlog.filmcraft.preview import run_ffprobe
from trip_vlog.logging_utils import utc_now_iso
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan
from trip_vlog.qc import checks as C
from trip_vlog.qc.preview import CutClip, build_preview_sheets, plan_tiles
from trip_vlog.state import atomic_write_json, atomic_write_text

QC_SCHEMA = 2   # 2: speech/audio/music/scene-order/decode checks, several preview sheets + index, `critic` block


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
    transcripts: Mapping[str, Sequence[C.TranscriptSpan]] = field(default_factory=dict)  # asset_id -> segments (empty: transcription did not run)
    outline_scene_ids: list[str] | None = None   # the approved outline's scenes in order, when this plan was made from one
    index_path: Path | None = None               # qc/preview_sheet.json
    workspace_root: Path | None = None           # index paths are stored relative to this
    extra_results: list[C.CheckResult] = field(default_factory=list)   # advisory findings added by a reviewer (Phase 5b critic)


def run_qc(i: QcInputs) -> dict[str, Any]:
    cfg, plan = i.config, i.plan
    q, p = cfg.qc, cfg.project
    results: list[C.CheckResult] = []
    results.append(C.check_media_online(i.project, i.source_paths))
    results.append(C.check_project_reopens(i.sequence is not None, i.mismatches, i.reopen_error))
    if i.sequence is not None:
        results.append(C.check_v1_gaps(i.sequence))
    tcheck = C.check_titles(plan, i.sequence)
    if tcheck is not None:
        results.append(tcheck)
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
    if probe is not None:
        results.append(C.check_preview_decodes(i.ffmpeg, i.preview))
    clip_audio = C.clip_audio_features(plan, i.catalog)
    results.append(C.check_speech_truncation(plan, i.transcripts, q.speech_cut_tolerance_seconds))
    results.append(C.check_audio_discontinuity(plan, clip_audio, q.audio_jump_db, q.audio_active_ratio_min, cfg.filmcraft.ambient_low_gain_db))
    results.append(C.check_music_vs_speech(plan, clip_audio, i.transcripts, ambient_gain_db=cfg.filmcraft.ambient_low_gain_db,
                                           active_ratio_min=q.audio_active_ratio_min, max_gain_db=q.music_max_gain_db))
    results.append(C.check_scene_order(plan, i.outline_scene_ids))
    results.extend(i.extra_results)

    sheet: dict[str, Any] = {"path": None, "frames": 0, "sheets": [], "index": None}
    if probe is not None and i.ffmpeg is not None:
        rel = _relative_to(i.workspace_root)
        clips = [CutClip(c.clip_instance_id, s.scene_id, c.timeline_start_frame, c.timeline_start_frame + c.duration_frames)
                 for s in plan.scenes for c in s.clips if c.track_role == "primary_video"]
        tiles = plan_tiles(clips, fps, frames, offset_seconds=q.cut_sample_offset_seconds, max_per_sheet=q.contact_sheet_max_frames,
                           max_sheets=q.contact_sheet_max_sheets)
        res = build_preview_sheets(i.ffmpeg, i.preview, i.contact_sheet, tiles, fps, columns=q.contact_sheet_columns,
                                   tile_width=q.contact_sheet_tile_width, max_per_sheet=q.contact_sheet_max_frames,
                                   offset_seconds=q.cut_sample_offset_seconds, duration_frames=frames, plan_hash=plan.plan_hash, rel=rel)
        res.index.generated_at = utc_now_iso()
        if i.index_path is not None:
            atomic_write_json(i.index_path, res.index.to_dict())
        sheet = {"path": res.written[0].name if res.written else None, "frames": res.frames, "failed_frames": res.failed,
                 "error": res.index.error, "sheets": [s.path for s in res.index.sheets if s.path],
                 "index": rel(i.index_path) if i.index_path is not None else None,
                 "cuts_total": res.index.cuts_total, "cuts_sampled": res.index.cuts_sampled}
        if res.index.error or res.failed:
            results.append(C.CheckResult("contact_sheet", C.WARN, res.index.error or f"{len(res.failed)} frame(s) could not be extracted",
                                         {"failed": res.failed[:10]}))
    status = C.overall_status(results)
    counts = {s: sum(1 for r in results if r.status == s) for s in (C.PASS, C.WARN, C.FAIL, C.SKIPPED)}
    doc = {"schema_version": QC_SCHEMA, "generated_at": utc_now_iso(), "status": status, "counts": counts,
           "plan_hash": plan.plan_hash, "preview": i.preview.name, "contact_sheet": sheet,
           "critic": {"status": "not_run", "reason": "deterministic checks only; an LLM critic (Phase 5b) would read "
                      "contact_sheet.index and the checks above",
                      "inputs": {"preview_sheet_index": sheet.get("index"), "checks": "checks", "plan_hash": plan.plan_hash}},
           "checks": [r.to_dict() for r in results]}
    atomic_write_json(i.report_path, doc)
    atomic_write_text(i.report_path.with_suffix(".md"), render_qc_markdown(doc))
    return doc


def _relative_to(root: Path | None):
    def rel(p: Path) -> str:
        if root is not None:
            try:
                return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                pass
        return Path(p).as_posix()
    return rel


def _esc(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_qc_markdown(doc: dict[str, Any]) -> str:
    """qc_report.md: the same content as qc_report.json for a person (status table, then what to look at)."""
    sheet = doc.get("contact_sheet") or {}
    c = doc["counts"]
    L = [f"# QC report: {doc['status'].upper()}", "",
         f"- Generated: {doc['generated_at']}; plan `{str(doc.get('plan_hash') or '')[:16]}`; preview `{doc['preview']}`",
         f"- {c['pass']} pass, {c['warn']} warn, {c['fail']} fail, {c.get('skipped', 0)} skipped (could not run; not a pass). Only objective defects fail (offline media, render or decode failure, "
         "gaps, duration out of tolerance); heuristics only warn.", ""]
    L += ["| Check | Status | Detail |", "|---|---|---|"]
    for r in doc["checks"]:
        L.append(f"| {r['name']} | {r['status']} | {_esc(r.get('detail', ''))} |")
    L.append("")
    flagged = [r for r in doc["checks"] if r["status"] in ("warn", "fail") and (r.get("data") or {}).get("findings")]
    for r in flagged:
        L += [f"## {r['name']}", ""]
        for f in r["data"]["findings"][:10]:
            L.append("- " + _esc(", ".join(f"{k}={v}" for k, v in f.items() if v not in (None, "", []))))
        L.append("")
    if sheet.get("sheets"):
        L += ["## Preview sheets", "", f"{sheet.get('frames', 0)} frame(s) around {sheet.get('cuts_sampled', 0)} of {sheet.get('cuts_total', 0)} cut(s); "
              f"index `{sheet.get('index')}` maps every tile to its record time, clip and scene.", ""]
        L += [f"- `{p}`" for p in sheet["sheets"]] + [""]
    crit = doc.get("critic") or {}
    L += ["## Critic", "", f"{crit.get('status', 'not_run')}: {crit.get('reason', '')}", ""]
    return "\n".join(L).rstrip("\n") + "\n"
