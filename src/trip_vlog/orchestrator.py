"""Phase 1 orchestration: scan -> probe -> catalog -> thumbnails, driven by the generic StageRunner."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from trip_vlog import __version__
from trip_vlog.config import Config, ConfigError, ResolvedPaths, load_config, resolve_paths
from trip_vlog.logging_utils import RunLogger, Status, new_run_id, utc_now_iso
from trip_vlog.media import thumbnails as thumbs
from trip_vlog.media.catalog import Catalog
from trip_vlog.media.probe import PROBE_VERSION, ProbeResult, probe_file
from trip_vlog.media.scan import ScanResult, scan_media
from trip_vlog.media.tools import find_ffmpeg, find_ffprobe, tool_version
from trip_vlog.state import Manifest, StageResult, StageRunner, Workspace, atomic_write_json, atomic_write_text, platform_info, stable_hash

INDEX_STAGES = ("scan", "probe", "catalog", "thumbnails")


@dataclass
class RunContext:
    """Shared state handed to every stage function. Phase 2 stages reuse this object."""
    config: Config
    paths: ResolvedPaths
    workspace: Workspace
    logger: RunLogger
    manifest: Manifest
    catalog: Catalog
    ffprobe: Path | None
    ffmpeg: Path | None
    ffprobe_version: str | None = None
    ffmpeg_version: str | None = None
    data: dict[str, Any] = field(default_factory=dict)  # in-memory hand-off between stages of one run
    echo: Callable[[str], None] = print


@dataclass
class IndexResult:
    run_id: str
    stage_status: dict[str, str]
    exit_code: int
    workspace: Path
    summary: dict[str, Any]
    awaiting_approval: bool = False
    dry_run: dict[str, Any] | None = None   # `analyze --dry-run`: what a real run would send


def probe_cache_key(ctx: RunContext) -> str:
    return stable_hash({"v": PROBE_VERSION, "ffprobe": ctx.ffprobe_version})


# ---- stages ---------------------------------------------------------------------------------

def scan_stage(ctx: RunContext) -> StageResult:
    m = ctx.config.media
    t0 = time.perf_counter()
    res = scan_media(ctx.paths.media_root, recursive=m.recursive, follow_symlinks=m.follow_symlinks, fingerprint=m.fingerprint)
    ctx.data["scan"] = res
    for f in res.files:
        ctx.logger.event("scan", Status.FAILED if f.error else Status.SUCCEEDED, asset_id=f.asset_id,
                         error=f.error, path=f.rel_path, media_type=f.media_type_hint)
    for s in res.skipped:
        ctx.logger.event("scan", Status.SKIPPED, path=s.rel_path, reason=s.reason, detail=s.detail)
    by_type: dict[str, int] = {}
    for f in res.files:
        by_type[f.media_type_hint] = by_type.get(f.media_type_hint, 0) + 1
    failed = sum(1 for f in res.files if f.error)
    summary = {"files": len(res.files), "by_type": by_type, "skipped": res.skipped_counts(),
               "case_collisions": len(res.case_collisions), "unreadable": failed,
               "elapsed_seconds": round(time.perf_counter() - t0, 3)}
    return StageResult(Status.PARTIAL if failed else Status.SUCCEEDED, summary)


def _probe_inputs(ctx: RunContext) -> Any:
    scan: ScanResult = ctx.data["scan"]
    return {"files": sorted((f.asset_id, f.content_version, f.error) for f in scan.files), "key": probe_cache_key(ctx)}


def probe_stage(ctx: RunContext) -> StageResult:
    scan: ScanResult = ctx.data["scan"]
    key = probe_cache_key(ctx)
    probes: dict[str, ProbeResult] = {}
    hits = misses = failed = 0
    for f in scan.files:
        if f.error:
            probes[f.asset_id] = ProbeResult(status="failed", error=f.error)
            failed += 1
            continue
        t0 = time.perf_counter()
        cached = ctx.catalog.cache_get(f.asset_id, "probe", key, f.content_version)
        if cached is not None:
            probes[f.asset_id] = ProbeResult.from_dict(cached)
            hits += 1
            ctx.logger.event("probe", Status.SUCCEEDED, asset_id=f.asset_id, cache="hit", duration_ms=(time.perf_counter() - t0) * 1000)
            continue
        misses += 1
        res = probe_file(f.abs_path, f.media_type_hint, ctx.ffprobe, ctx.config.media.probe_timeout_seconds)
        probes[f.asset_id] = res
        dur = (time.perf_counter() - t0) * 1000
        if res.status == "ok":
            ctx.catalog.cache_put(f.asset_id, "probe", key, f.content_version, res.to_dict())
            ctx.logger.event("probe", Status.SUCCEEDED, asset_id=f.asset_id, cache="miss", duration_ms=dur)
        else:
            failed += 1  # failures are not cached: they are retried on the next run
            ctx.logger.event("probe", Status.FAILED, asset_id=f.asset_id, cache="miss", duration_ms=dur, error=res.error, path=f.rel_path)
    ctx.data["probes"] = probes
    summary = {"probed": misses, "cache_hits": hits, "failed": failed}
    if ctx.ffprobe is None and any(f.media_type_hint != "image" for f in scan.files):
        summary["warning"] = "ffprobe not found; video/audio files could not be probed"
    return StageResult(Status.PARTIAL if failed else Status.SUCCEEDED, summary)


def _record_for(f, pr: ProbeResult) -> dict[str, Any]:
    fr = pr.frame_rate
    notes = list(pr.notes)
    return {
        "asset_id": f.asset_id, "relative_path": f.rel_path, "extension": f.extension, "size_bytes": f.size_bytes,
        "mtime_ns": f.mtime_ns, "content_version": f.content_version, "sha256": f.sha256,
        "media_type": pr.media_type if pr.media_type != "unknown" else f.media_type_hint,
        "duration_seconds": pr.duration_seconds, "width": pr.width, "height": pr.height,
        "display_width": pr.display_width, "display_height": pr.display_height,
        "frame_rate_num": fr.numerator if fr else None, "frame_rate_den": fr.denominator if fr else None,
        "is_vfr": int(pr.is_vfr), "rotation_degrees": pr.rotation_degrees, "codec": pr.codec, "pix_fmt": pr.pix_fmt,
        "hdr": pr.hdr, "audio_streams": pr.audio_streams, "capture_time": pr.capture_time,
        "capture_time_source": pr.capture_time_source, "gps_json": json.dumps(pr.gps) if pr.gps else None,
        "format_name": pr.format_name, "technical_notes_json": json.dumps(notes),
        "probe_status": "ok" if pr.status == "ok" else "failed", "probe_error": pr.error,
    }


def catalog_stage(ctx: RunContext) -> StageResult:
    scan: ScanResult = ctx.data["scan"]
    probes: dict[str, ProbeResult] = ctx.data.get("probes", {})
    key = probe_cache_key(ctx)
    counts = {"new": 0, "unchanged": 0, "changed": 0, "reappeared": 0, "algorithm_changed": 0}
    for f in scan.files:
        pr = probes.get(f.asset_id)
        if pr is None:
            cached = ctx.catalog.cache_get(f.asset_id, "probe", key, f.content_version)
            pr = ProbeResult.from_dict(cached) if cached else ProbeResult(status="failed", error="not probed")
        kind = ctx.catalog.upsert_asset(_record_for(f, pr))
        counts[kind] += 1
        if kind == "changed":
            ctx.logger.event("catalog", Status.SUCCEEDED, asset_id=f.asset_id, message="content_version changed; analysis marked stale")
    gone = ctx.catalog.mark_missing(f.asset_id for f in scan.files)
    dups, moves, stale = ctx.catalog.probable_duplicates(), ctx.catalog.probable_moves(), ctx.catalog.stale_assets()
    n = ctx.catalog.export_assets_jsonl(ctx.workspace.assets_jsonl, ctx.config.media.export_gps)
    report = {"generated_at": utc_now_iso(), "scan": scan.to_report(), "probable_duplicates": dups,
              "probable_moves": moves, "missing_assets": gone, "stale_assets": stale,
              "failed_probes": [{"asset_id": a, "path": f.rel_path, "error": probes[a].error}
                                for f in scan.files for a in [f.asset_id] if a in probes and probes[a].status != "ok"]}
    atomic_write_json(ctx.workspace.inventory_report, report)
    summary = {**counts, "missing": len(gone), "stale": len(stale), "probable_duplicates": len(dups),
               "probable_moves": len(moves), "assets_exported": n}
    return StageResult(Status.SUCCEEDED, summary)


def _thumb_settings(ctx: RunContext) -> thumbs.ThumbSettings:
    return thumbs.ThumbSettings(max_side=ctx.config.media.thumbnail_max_side)


def _thumb_inputs(ctx: RunContext) -> Any:
    rows = ctx.catalog.list_assets()
    return {"assets": [(r["asset_id"], r["content_version"], r["probe_status"]) for r in rows],
            "settings": _thumb_settings(ctx).key(), "frames": ctx.config.media.contact_sheet_frames,
            "ffmpeg": ctx.ffmpeg_version}


def thumbnails_stage(ctx: RunContext) -> StageResult:
    st = _thumb_settings(ctx)
    ws = ctx.workspace
    frames = ctx.config.media.contact_sheet_frames
    hits = made = failed = skipped = 0
    folder_items: dict[str, list[tuple[str, Path, str]]] = {}
    for row in ctx.catalog.list_assets():
        aid, cv, mtype = row["asset_id"], row["content_version"], row["media_type"]
        if row["probe_status"] != "ok" or mtype not in ("video", "image"):
            skipped += 1
            ctx.logger.event("thumbnails", Status.SKIPPED, asset_id=aid, message=f"no thumbnail for {mtype or 'unprobed'} asset")
            continue
        t0 = time.perf_counter()
        name = thumbs.thumbnail_name(aid, cv, st)
        out = ws.thumbnails_dir / name
        cache_key = st.key()
        cached = ctx.catalog.cache_get(aid, "thumbnail", cache_key, cv)
        src = ctx.paths.media_root / Path(*row["relative_path"].split("/"))
        err = None
        if cached is not None and out.exists():
            hits += 1
            ctx.logger.event("thumbnails", Status.SUCCEEDED, asset_id=aid, cache="hit")
        else:
            err = (thumbs.make_video_thumbnail(ctx.ffmpeg, src, row["duration_seconds"], out, st) if mtype == "video"
                   else thumbs.make_image_thumbnail(src, out, st))
            dur = (time.perf_counter() - t0) * 1000
            if err:
                failed += 1
                ctx.catalog.set_artifacts(aid, thumbnail_error=err)
                ctx.logger.event("thumbnails", Status.FAILED, asset_id=aid, cache="miss", duration_ms=dur, error=err, path=row["relative_path"])
                continue
            made += 1
            ctx.catalog.cache_put(aid, "thumbnail", cache_key, cv, {"path": name})
            thumbs._remove_stale(ws.thumbnails_dir, f"{aid}_", name)
            ctx.logger.event("thumbnails", Status.SUCCEEDED, asset_id=aid, cache="miss", duration_ms=dur)
        sheet_rel = None
        if mtype == "video" and frames > 0:
            sname = thumbs.asset_sheet_name(aid, cv, frames, st)
            sheet = ws.contact_sheets_dir / sname
            if not sheet.exists():
                serr = thumbs.make_asset_contact_sheet(ctx.ffmpeg, src, aid, row["duration_seconds"], frames, sheet, ws.tmp_dir, st)
                if serr:
                    ctx.logger.event("contact_sheet", Status.FAILED, asset_id=aid, error=serr, path=row["relative_path"])
                    sheet = None
            sheet_rel = f"media_index/contact_sheets/{sname}" if sheet else None
        ctx.catalog.set_artifacts(aid, thumbnail_path=f"media_index/thumbnails/{name}", contact_sheet_path=sheet_rel)
        folder = row["relative_path"].rpartition("/")[0]
        caption = thumbs.fmt_ts(thumbs.midpoint_seconds(row["duration_seconds"])) if mtype == "video" else "photo"
        folder_items.setdefault(folder, []).append((aid, out, caption))
    sheets = 0
    for folder, items in sorted(folder_items.items()):
        outs, serr = thumbs.make_folder_contact_sheets(items, folder, ws.contact_sheets_dir, st)
        sheets += len(outs)
        if serr:
            ctx.logger.event("contact_sheet", Status.FAILED, error=serr, folder=folder or ".")
    ctx.catalog.export_assets_jsonl(ws.assets_jsonl, ctx.config.media.export_gps)
    try:
        if ws.tmp_dir.exists() and not any(ws.tmp_dir.iterdir()):
            ws.tmp_dir.rmdir()
    except OSError:
        pass
    summary = {"created": made, "cache_hits": hits, "failed": failed, "skipped": skipped, "folder_sheets": sheets}
    return StageResult(Status.PARTIAL if failed else Status.SUCCEEDED, summary)


def build_index_runner(ctx: RunContext) -> StageRunner:
    runner = StageRunner(ctx.manifest, ctx.logger, ctx)
    runner.register("scan", scan_stage, always_run=True)
    runner.register("probe", probe_stage, deps=("scan",), inputs=_probe_inputs)
    runner.register("catalog", catalog_stage, deps=("probe",), always_run=True)
    runner.register("thumbnails", thumbnails_stage, deps=("catalog",), inputs=_thumb_inputs)
    return runner


def build_transcribe_runner(ctx: RunContext, *, with_speech: bool = True) -> StageRunner:
    """index stages + segmentation + audio features + speech transcription (`trip-vlog transcribe`)."""
    from trip_vlog.media.segment import segment_assets, segment_inputs
    from trip_vlog.speech.stage import audio_inputs, audio_stage, transcribe_inputs, transcribe_stage

    runner = build_index_runner(ctx)
    runner.register("segment", segment_assets, deps=("catalog",), inputs=segment_inputs)
    if with_speech:
        runner.register("audio", audio_stage, deps=("segment",), inputs=audio_inputs)
        runner.register("transcribe", transcribe_stage, deps=("audio",), inputs=transcribe_inputs)
    return runner


def build_analyze_runner(ctx: RunContext, *, with_analyze: bool = True) -> StageRunner:
    """index stages + segmentation + audio features + transcription (a no-op without a speech backend) + vision labeling
    (`trip-vlog analyze`). Transcripts must exist before labeling because they are part of the labeling request."""
    from trip_vlog.analysis.stage import analyze_inputs, analyze_stage

    runner = build_transcribe_runner(ctx, with_speech=with_analyze)
    if with_analyze:
        runner.register("analyze", analyze_stage, deps=("transcribe",), inputs=analyze_inputs,
                        verify=lambda c: c.workspace.moments_jsonl.is_file())
    return runner


def build_outline_runner(ctx: RunContext, *, with_analyze: bool = True) -> StageRunner:
    """index + segment + audio + transcribe + analyze, then `outline` -> `outline_approval` (`trip-vlog outline`, Phase 4a).
    Phase 4b appends selection -> resolve -> validate -> assemble after the returned runner's last stage. With
    ``with_analyze=False`` (dry run) only the cheap local stages are registered and nothing is sent anywhere."""
    from trip_vlog.outline_stages import register_outline_stages

    runner = build_analyze_runner(ctx, with_analyze=with_analyze)
    if with_analyze:
        register_outline_stages(runner, after="analyze")
    return runner


def build_draft_runner(ctx: RunContext) -> StageRunner:
    """index stages + segment -> plan -> approval gate -> assemble -> preview -> qc -> handoff."""
    from trip_vlog.draft_stages import build_draft_runner as _build
    return _build(ctx)


# ---- workspace files ------------------------------------------------------------------------

WORKSPACE_README = """# trip-vlog workspace

Generated by `trip-vlog` {version}. Your original media is never modified; this folder only holds derived files.

- `project.yaml`: effective configuration of the last run (edit it and re-run `trip-vlog index`).
- `manifest.json`: tool/platform/ffprobe versions, config hash and per-stage status (used by `status` and `resume`).
- `media_index/catalog.sqlite`: the authoritative catalog (assets, analysis cache). Do not edit by hand.
- `media_index/assets.jsonl`: read-only export regenerated from SQLite; never used as input.
- `media_index/inventory_report.json`: skipped/unsupported files, case collisions, probable duplicates/moves, failed probes.
- `media_index/thumbnails/`, `media_index/contact_sheets/`: cached JPEGs (safe to delete; they are rebuilt).
- `logs/run.jsonl`, `logs/errors.jsonl`: structured logs (secrets masked).
- `plans/`: `edit_plan.json` (validated plan), `plan_summary.md` (read this before approving), `selection_plan.json`, `story_outline.json` / `story_outline.md` (`trip-vlog outline`), schemas.
- `filmcraft/`: versioned `.fcproj` drafts, `capability_snapshot.json`, `command_log.jsonl`, `execution_journal.json`.
- `preview/`: `draft.mp4`, `qc_report.json` / `.md`, `contact_sheet_timeline.jpg` (more sheets for many cuts).
- `qc/`: `preview_sheet.json`, the index of the preview sheets (tile -> record time, clip, scene); with `qc.critic` on, `critic_report.json` / `.md`.
- `plans/revisions/`: with `qc.critic: apply`, one `critic_rNN.json` / `.md` per round (the change and its diff) and the parent plan it started from; `trip-vlog revise` writes `revise_rNN.json` / `.md` (the request, what changed per scene, what was refused, QC before and after) and its parent plan the same way.
- `resolve_handoff/`: interchange files, `media_manifest.csv`, `handoff_report.md`, `compatibility_report.json` / `.md`, `edit_plan.json`, `project_reference.json`.

Privacy: file names, GPS coordinates and thumbnails of your trip are personal data. GPS is stored in the catalog but
left out of `assets.jsonl` unless `media.export_gps: true`. Do not share this folder without reviewing it.

Commands: `trip-vlog status --output <this folder>`, `trip-vlog approve --output <this folder>` and `trip-vlog resume --output <this folder>`.
"""


def _cfg_overrides_summary(overrides: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in overrides.items() if v is not None}


def run_index(media: Path | str | None, output: Path | str, config_path: Path | str | None = None,
              overrides: Mapping[str, Any] | None = None, *, echo: Callable[[str], None] = print,
              command: str = "index") -> IndexResult:
    """Run the Phase 1 inventory. Raises ConfigError for unusable configuration/paths."""
    return _execute(media, output, config_path, overrides, echo=echo, command=command, pipeline="index")


def run_draft(media: Path | str | None, output: Path | str, config_path: Path | str | None = None,
              overrides: Mapping[str, Any] | None = None, *, echo: Callable[[str], None] = print,
              command: str = "draft") -> IndexResult:
    """Run (or continue) the draft pipeline: index -> segment -> plan -> approval -> assemble -> preview -> qc -> handoff. With
    ``workflow.planner: llm`` the plan is preceded by audio -> transcribe -> analyze -> outline -> outline_approval and made by the LLM."""
    return _execute(media, output, config_path, overrides, echo=echo, command=command, pipeline="draft")


def run_critic(output: Path | str, config_path: Path | str | None = None, overrides: Mapping[str, Any] | None = None, *, mode: str | None = None,
               echo: Callable[[str], None] = print, command: str = "critic") -> IndexResult:
    """Run the draft pipeline with the QC critic on (Phase 5b) for this run only (`project.yaml` is not rewritten). ``mode``: report | apply;
    default the configured ``qc.critic``, or report when that is off. The workspace must already hold a draft."""
    ws = Workspace(Path(output).expanduser().resolve())
    manifest = Manifest.load(ws.manifest_path) if ws.manifest_path.exists() else None
    if manifest is None or manifest.get("pipeline") != "draft" or not ws.project_yaml.exists():
        raise ConfigError(f"{ws.root} holds no draft; run `trip-vlog draft` first (the critic reviews a finished draft)")
    ov = dict(overrides or {})
    if mode is None:
        cfg_file = Path(config_path) if config_path else ws.project_yaml
        configured = load_config(cfg_file, ov).qc.critic
        mode = configured if configured != "off" else "report"
    if mode not in ("report", "apply"):
        raise ConfigError(f"critic mode must be report or apply, got {mode!r}")
    ov["qc.critic"] = mode
    # asking for a review is a fresh start: revisions remembered as failed are tried again, and a stop after a QC regression is lifted
    st = dict(manifest.get("critic") or {})
    if st.get("failed") or (st.get("rounds") and st["rounds"][-1].get("halt")):
        st.pop("failed", None)
        if st.get("rounds") and st["rounds"][-1].get("halt"):
            st["rounds"] = [*st["rounds"][:-1], {**st["rounds"][-1], "halt_lifted": st["rounds"][-1]["halt"], "halt": None}]
        manifest.set(critic=st)
        manifest.save()
    return _execute(None, output, config_path, ov, echo=echo, command=command, pipeline="draft", persist_config=False)


def run_revise(output: Path | str, config_path: Path | str | None = None, overrides: Mapping[str, Any] | None = None, *, request: str | None = None,
               dry_run: bool = False, apply: bool = False, again: bool = False, echo: Callable[[str], None] = print, command: str = "revise") -> IndexResult:
    """Run the draft pipeline with the natural-language revision stage (Phase 6, DESIGN §16) for this run only (`project.yaml` is not rewritten).
    ``request``: what the person wants changed. ``apply``: with no request, apply the proposal that was approved with `approve --revision`.
    ``dry_run``: ask the model and show the diff; nothing is written to the plan. The workspace must already hold a finished draft."""
    ws = Workspace(Path(output).expanduser().resolve())
    manifest = Manifest.load(ws.manifest_path) if ws.manifest_path.exists() else None
    if manifest is None or manifest.get("pipeline") != "draft" or not ws.project_yaml.exists():
        raise ConfigError(f"{ws.root} holds no draft; run `trip-vlog draft` first (a revision changes a finished draft)")
    text = (request or "").strip()
    if not text and not apply:
        raise ConfigError("give a request, e.g. trip-vlog revise --output WS \"make the beach scene shorter\" (or --file FILE; or --apply for an approved proposal)")
    if text and apply:
        raise ConfigError("--apply applies a proposal that was approved; it takes no new request")
    if dry_run and apply:
        raise ConfigError("--dry-run only shows what a new request would change; it cannot be combined with --apply")
    return _execute(None, output, config_path, dict(overrides or {}), echo=echo, command=command, pipeline="draft", persist_config=False,
                    revise={"request": text or None, "dry_run": dry_run, "apply": apply and not text, "again": again})


def run_analyze(media: Path | str | None, output: Path | str, config_path: Path | str | None = None,
                overrides: Mapping[str, Any] | None = None, *, limit: int | None = None, dry_run: bool = False,
                echo: Callable[[str], None] = print, command: str = "analyze") -> IndexResult:
    """Index + segment + vision-label (Phase 3b). `limit` analyzes only the first N assets (by path); `dry_run` computes what
    would be sent (see `IndexResult.dry_run`) without contacting any endpoint."""
    return _execute(media, output, config_path, overrides, echo=echo, command=command, pipeline="analyze",
                    analyze_limit=limit, dry_run=dry_run)


def run_transcribe(media: Path | str | None, output: Path | str, config_path: Path | str | None = None,
                   overrides: Mapping[str, Any] | None = None, *, limit: int | None = None, dry_run: bool = False,
                   echo: Callable[[str], None] = print, command: str = "transcribe") -> IndexResult:
    """Index + segment + audio features + speech transcription (Phase 3c). `limit` covers the first N assets (by path); `dry_run`
    reports what would be extracted and sent (see `IndexResult.dry_run`) without extracting audio or contacting any backend."""
    return _execute(media, output, config_path, overrides, echo=echo, command=command, pipeline="transcribe",
                    analyze_limit=limit, dry_run=dry_run)


def run_outline(media: Path | str | None, output: Path | str, config_path: Path | str | None = None,
                overrides: Mapping[str, Any] | None = None, *, dry_run: bool = False,
                echo: Callable[[str], None] = print, command: str = "outline") -> IndexResult:
    """Index + segment + audio + transcribe + analyze + story outline + approval stop (Phase 4a). `dry_run` reports what the
    outline request would contain (see `IndexResult.dry_run`) and sends nothing."""
    return _execute(media, output, config_path, overrides, echo=echo, command=command, pipeline="outline", dry_run=dry_run)


def _execute(media: Path | str | None, output: Path | str, config_path: Path | str | None,
             overrides: Mapping[str, Any] | None, *, echo: Callable[[str], None], command: str, pipeline: str,
             analyze_limit: int | None = None, dry_run: bool = False, persist_config: bool = True, revise: dict[str, Any] | None = None) -> IndexResult:
    ws = Workspace(Path(output).expanduser().resolve())
    overrides = dict(overrides or {})
    cfg_file: Path | None = Path(config_path) if config_path else (ws.project_yaml if ws.project_yaml.exists() else None)
    config = load_config(cfg_file, overrides)
    manifest = Manifest.load(ws.manifest_path)
    from trip_vlog.revision_common import apply_manifest_overrides

    apply_manifest_overrides(config, manifest)    # what accepted revisions changed about the rules (target length, waived requirements); not in project.yaml
    if media is None:
        media = manifest.get("media_root")
        if not media:
            raise ConfigError("--media is required for the first run in a workspace")
    paths = resolve_paths(config, media, ws.root)
    prev_root = manifest.get("media_root")
    if prev_root and os.path.normcase(str(prev_root)) != os.path.normcase(str(paths.media_root)):
        raise ConfigError(f"workspace was indexed from a different media folder ({prev_root}); use a new --output")

    ws.ensure_dirs()
    run_id = new_run_id()
    redactions = [(str(paths.media_root), "<media_root>")]
    logger = RunLogger(ws.run_log, ws.errors_log, run_id, redactions)
    ffprobe, ffmpeg = find_ffprobe(config.media.ffprobe_path), find_ffmpeg(config.media.ffmpeg_path)
    ffprobe_ver, ffmpeg_ver = tool_version(ffprobe), tool_version(ffmpeg)
    if persist_config:   # `critic --mode ...` is a one-off: it must not change what the next `resume` does
        atomic_write_text(ws.project_yaml, config.to_yaml())
        manifest.set(config_hash=config.config_hash())
    manifest.set(
        tool_version=__version__, platform=platform_info(),
        media_root=str(paths.media_root), run_id=run_id, command=command, run_started_at=utc_now_iso(),
        run_finished_at=None, ffprobe={"path": str(ffprobe) if ffprobe else None, "version": ffprobe_ver},
        ffmpeg={"path": str(ffmpeg) if ffmpeg else None, "version": ffmpeg_ver},
        cli_overrides=_cfg_overrides_summary(overrides),
    )
    if pipeline == "draft":
        manifest.set(pipeline="draft", planner=config.workflow.planner)
    elif pipeline == "analyze" and manifest.get("pipeline") not in ("draft", "outline"):
        manifest.set(pipeline="analyze")
    elif pipeline == "transcribe" and manifest.get("pipeline") in (None, "index"):
        manifest.set(pipeline="transcribe")
    elif pipeline == "outline" and manifest.get("pipeline") != "draft":
        manifest.set(pipeline="outline")
    manifest.save()
    atomic_write_text(ws.readme, WORKSPACE_README.format(version=__version__))
    logger.event("run", Status.RUNNING, command=command, tool_version=__version__)
    if ffprobe is None:
        echo("warning: ffprobe not found; videos/audio cannot be probed (install FFmpeg, see README)")
    if ffmpeg is None:
        echo("warning: ffmpeg not found; video thumbnails will be skipped")

    ctx: RunContext | None = None
    dry_report: dict[str, Any] | None = None
    try:
        with Catalog(ws.catalog_path) as catalog:
            ctx = RunContext(config, paths, ws, logger, manifest, catalog, ffprobe, ffmpeg, ffprobe_ver, ffmpeg_ver, echo=echo)
            ctx.data["analyze_limit" if pipeline != "transcribe" else "transcribe_limit"] = analyze_limit
            if revise:
                ctx.data["revise"] = revise
            if pipeline == "draft":
                from trip_vlog.critic_stages import recover_interrupted
                from trip_vlog.draft_stages import build_draft_runner
                from trip_vlog.revise_stages import recover_interrupted as recover_revision

                for recover in (recover_interrupted, recover_revision):     # before any stage looks at the plan, whether or not the critic or a revision runs now
                    note = recover(ctx)
                    if note:
                        echo(f"warning: {note}")
                runner = build_draft_runner(ctx)
            elif pipeline == "analyze":
                runner = build_analyze_runner(ctx, with_analyze=not dry_run)
            elif pipeline == "transcribe":
                runner = build_transcribe_runner(ctx, with_speech=not dry_run)
            elif pipeline == "outline":
                runner = build_outline_runner(ctx, with_analyze=not dry_run)
            else:
                runner = build_index_runner(ctx)
            statuses = runner.run()
            if pipeline == "analyze" and dry_run:
                from trip_vlog.analysis.stage import dry_run_report
                dry_report = dry_run_report(ctx, analyze_limit)
            if pipeline == "transcribe" and dry_run:
                from trip_vlog.speech.stage import dry_run_report as speech_dry_run
                dry_report = speech_dry_run(ctx)
            if pipeline == "outline" and dry_run:
                from trip_vlog.outline_stages import dry_run_report as outline_dry_run
                dry_report = outline_dry_run(ctx)
            manifest.set(run_finished_at=utc_now_iso(), catalog_schema_version=catalog.schema_version())
            manifest.save()
            assets = catalog.list_assets()
            summary = {
                "assets": len(assets),
                "probe_failed": sum(1 for a in assets if a["probe_status"] != "ok"),
                "stale": len(catalog.stale_assets()),
            }
    finally:
        adapter = ctx.data.get("adapter") if ctx is not None else None
        if adapter is not None:
            adapter.close()  # never leave a FilmCraft server process behind
    any_failed = any(s == Status.FAILED for s in statuses.values())
    awaiting = any(s == Status.AWAITING_APPROVAL for s in statuses.values())
    logger.event("run", Status.FAILED if any_failed else Status.AWAITING_APPROVAL if awaiting else Status.SUCCEEDED, command=command)
    return IndexResult(run_id, {k: str(v) for k, v in statuses.items()}, 1 if any_failed else 3 if awaiting else 0, ws.root, summary,
                       awaiting_approval=awaiting, dry_run=dry_report)


class ApprovalRefused(RuntimeError):
    """`approve` cannot record an approval (message is user-facing)."""


def approve_plan(output: Path | str) -> dict[str, Any]:
    """Record manual approval of the current plans/edit_plan.json (bound to its recomputed plan hash)."""
    from trip_vlog.draft_stages import PlanRejected, approve_current_plan, plan_file_hash, validate_plan_file
    from trip_vlog.draft_stages import _pp  # noqa: PLC2701

    ws = Workspace(Path(output).expanduser().resolve())
    if not ws.manifest_path.exists() or not ws.project_yaml.exists():
        raise ApprovalRefused(f"{ws.root} is not a trip-vlog workspace; run `trip-vlog draft` first")
    manifest = Manifest.load(ws.manifest_path)
    if manifest.stage("plan").get("status") != "succeeded" and manifest.stage("plan").get("decision") != "cache_hit":
        raise ApprovalRefused("there is no successful plan yet; run `trip-vlog draft` (or `resume`) first")
    config = load_config(ws.project_yaml)
    from trip_vlog.revision_common import apply_manifest_overrides

    apply_manifest_overrides(config, manifest)
    paths = resolve_paths(config, manifest.get("media_root") or ws.root.parent, ws.root)
    with Catalog(ws.catalog_path) as catalog:
        logger = RunLogger(ws.run_log, ws.errors_log, new_run_id())
        ctx = RunContext(config, paths, ws, logger, manifest, catalog, None, None)
        try:
            validate_plan_file(ctx)
        except PlanRejected as e:
            raise ApprovalRefused(f"refusing to approve an invalid plan:\n{e}") from None
        ap = approve_current_plan(manifest, plan_file_hash(_pp(ctx).edit_plan), "manual")
    logger.event("approval", Status.SUCCEEDED, plan_hash=ap["plan_hash"], mode="manual")
    return ap


def approve_outline(output: Path | str) -> dict[str, Any]:
    """Record manual approval of the current plans/story_outline.json (bound to the hash of its content)."""
    from trip_vlog.outline_stages import approve_current_outline, check_outline_file

    ws = Workspace(Path(output).expanduser().resolve())
    if not ws.manifest_path.exists() or not ws.project_yaml.exists():
        raise ApprovalRefused(f"{ws.root} is not a trip-vlog workspace; run `trip-vlog outline` first")
    manifest = Manifest.load(ws.manifest_path)
    st = manifest.stage("outline")
    if st.get("status") != "succeeded" and st.get("decision") != "cache_hit":
        raise ApprovalRefused("there is no successful story outline yet; run `trip-vlog outline` (or `resume`) first")
    config = load_config(ws.project_yaml)
    paths = resolve_paths(config, manifest.get("media_root") or ws.root.parent, ws.root)
    with Catalog(ws.catalog_path) as catalog:
        logger = RunLogger(ws.run_log, ws.errors_log, new_run_id())
        ctx = RunContext(config, paths, ws, logger, manifest, catalog, None, None)
        outline, report, _ = check_outline_file(ctx)
        if outline is None or not report.ok:
            raise ApprovalRefused("refusing to approve an invalid story outline:\n" + report.format())
        from trip_vlog.planning.outline import outline_hash

        ap = approve_current_outline(manifest, outline_hash(outline), "manual")
    logger.event("approval", Status.SUCCEEDED, outline_hash=ap["outline_hash"], mode="manual", what="outline")
    return ap


def approve_critic(output: Path | str) -> dict[str, Any]:
    """Approve the critic revision that waits for it (`workflow.plan_review: true` with `qc.critic: apply`). The approval is bound to the revision
    (its operations and the plan they start from); the next `resume` applies exactly that revision. Refused when the plan changed since."""
    from trip_vlog.draft_stages import plan_file_hash

    ws = Workspace(Path(output).expanduser().resolve())
    if not ws.manifest_path.exists() or not ws.project_yaml.exists():
        raise ApprovalRefused(f"{ws.root} is not a trip-vlog workspace; run `trip-vlog draft` first")
    manifest = Manifest.load(ws.manifest_path)
    st = dict(manifest.get("critic") or {})
    pend = dict(st.get("pending") or {})
    if pend.get("status") not in ("pending", "approved"):
        raise ApprovalRefused("no critic revision is waiting for approval; one is proposed by `trip-vlog critic --mode apply` "
                              "(or a draft with qc.critic: apply) when workflow.plan_review is true")
    config = load_config(ws.project_yaml)
    paths = resolve_paths(config, manifest.get("media_root") or ws.root.parent, ws.root)
    try:
        cur = plan_file_hash(paths.outputs["edit_plan_file"])
    except (OSError, ValueError) as e:
        raise ApprovalRefused(f"the edit plan cannot be read: {e}") from None
    if cur != pend.get("parent_plan_hash"):
        raise ApprovalRefused("the edit plan changed since the critic revision was proposed; run `trip-vlog critic --mode apply` again")
    pend.update(status="approved", approved_at=utc_now_iso())
    st["pending"] = pend
    manifest.set(critic=st)
    manifest.save()
    logger = RunLogger(ws.run_log, ws.errors_log, new_run_id())
    logger.event("approval", Status.SUCCEEDED, revision=pend.get("revision"), parent_plan_hash=pend.get("parent_plan_hash"), mode="manual", what="critic_fix")
    return pend


def approve_revision(output: Path | str) -> dict[str, Any]:
    """Approve the proposed natural-language revision (`trip-vlog revise` stopped for it: ambiguous, destructive or workflow.plan_review). The approval is
    bound to that proposal (its operations and the plan they start from); the next `resume` (or `revise --apply`) applies exactly that revision.
    Refused when the plan changed since, or when the proposal is not applicable."""
    from trip_vlog.draft_stages import plan_file_hash

    ws = Workspace(Path(output).expanduser().resolve())
    if not ws.manifest_path.exists() or not ws.project_yaml.exists():
        raise ApprovalRefused(f"{ws.root} is not a trip-vlog workspace; run `trip-vlog draft` first")
    manifest = Manifest.load(ws.manifest_path)
    st = dict(manifest.get("revise") or {})
    pend = dict(st.get("pending") or {})
    if pend.get("status") == "stale":
        raise ApprovalRefused(f"the proposed revision {pend.get('revision')} can no longer be applied ({pend.get('stale_reason')}); run `trip-vlog revise` again")
    if pend.get("status") not in ("pending", "approved"):
        raise ApprovalRefused("no revision is waiting for approval; `trip-vlog revise \"...\"` proposes one when a request is ambiguous or destructive "
                              "(or when workflow.plan_review is true)")
    config = load_config(ws.project_yaml)
    paths = resolve_paths(config, manifest.get("media_root") or ws.root.parent, ws.root)
    try:
        cur = plan_file_hash(paths.outputs["edit_plan_file"])
    except (OSError, ValueError) as e:
        raise ApprovalRefused(f"the edit plan cannot be read: {e}") from None
    if cur != pend.get("parent_plan_hash"):
        raise ApprovalRefused("the edit plan changed since the revision was proposed; nothing was approved. Run `trip-vlog revise` again")
    pend.update(status="approved", approved_at=utc_now_iso())
    st["pending"] = pend
    manifest.set(revise=st)
    manifest.save()
    logger = RunLogger(ws.run_log, ws.errors_log, new_run_id())
    logger.event("approval", Status.SUCCEEDED, revision=pend.get("revision"), parent_plan_hash=pend.get("parent_plan_hash"), mode="manual", what="revision")
    return pend


def approve_pending(output: Path | str, which: str | None = None) -> tuple[str, dict[str, Any]]:
    """Approve what is waiting: the story outline, the edit plan, a critic revision or a natural-language revision. ``which`` ("outline" | "plan" | "critic" |
    "revision") forces the choice.
    Returns (kind, approval record). Without ``which``: whichever stage stopped at the approval gate (outline first, then the plan, then the critic);
    else the outline when the workspace ran the outline pipeline, else the edit plan (the baseline `draft` path)."""
    if which not in (None, "outline", "plan", "critic", "revision"):
        raise ValueError("which must be 'outline', 'plan', 'critic', 'revision' or None")
    ws = Workspace(Path(output).expanduser().resolve())
    manifest = Manifest.load(ws.manifest_path)
    if which == "critic":
        return "critic", approve_critic(output)
    if which == "revision":
        return "revision", approve_revision(output)
    if which is None:
        if manifest.stage("outline_approval").get("status") == "awaiting_approval":
            which = "outline"
        elif manifest.stage("approval").get("status") == "awaiting_approval":
            which = "plan"
        elif manifest.stage("revise").get("status") == "awaiting_approval":
            return "revision", approve_revision(output)
        elif manifest.stage("critic").get("status") == "awaiting_approval":
            return "critic", approve_critic(output)
        else:
            has_outline, has_plan = bool(manifest.stage("outline_approval")), bool(manifest.stage("approval"))
            which = "outline" if manifest.get("pipeline") == "outline" or (has_outline and not has_plan) else "plan"
    return which, (approve_outline(output) if which == "outline" else approve_plan(output))


def read_status(output: Path | str) -> dict[str, Any]:
    ws = Workspace(Path(output).expanduser().resolve())
    if not ws.manifest_path.exists():
        raise ConfigError(f"no manifest.json in {ws.root}; run `trip-vlog index` first")
    manifest = Manifest.load(ws.manifest_path)
    info: dict[str, Any] = {"workspace": str(ws.root), "manifest": manifest.data}
    if ws.catalog_path.exists():
        with Catalog(ws.catalog_path) as cat:
            rows = cat.list_assets(present_only=False)
            info["catalog"] = {
                "schema_version": cat.schema_version(),
                "assets_present": sum(1 for r in rows if r["present"]),
                "assets_missing": sum(1 for r in rows if not r["present"]),
                "probe_failed": [r["relative_path"] for r in rows if r["present"] and r["probe_status"] != "ok"],
                "stale": cat.stale_assets(),
                "probable_duplicates": cat.probable_duplicates(),
                "probable_moves": cat.probable_moves(),
                "thumbnail_errors": [r["relative_path"] for r in rows if r["present"] and r["thumbnail_error"]],
            }
    return info
