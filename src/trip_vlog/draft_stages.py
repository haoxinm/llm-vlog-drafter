"""Phase 2c stages: segment -> plan -> approval gate -> assemble -> preview -> qc -> handoff (DESIGN §9.5, §12-§15, §18).

Rules this module enforces
  * Nothing touches FilmCraft before the plan is approved (the approval stage stops the run with ``awaiting_approval``).
  * The plan on disk (``plans/edit_plan.json``) is the unit of approval and of execution. It is re-validated at assemble
    time, so a hand-edited or corrupted plan is rejected before any FilmCraft call.
  * Approval is bound to the plan hash recomputed from the file content; a changed plan invalidates it.
  * Stages cache on their inputs plus an artifact check, so an unchanged re-run does no work and cannot duplicate clips.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from trip_vlog import __version__
from trip_vlog.config import Config
from trip_vlog.filmcraft.adapter import FilmCraftAdapter
from trip_vlog.filmcraft.execution import failed_builds, latest_complete_project, load_journal, sha256_file
from trip_vlog.filmcraft.mappings import MediaMap
from trip_vlog.filmcraft.plan_mapping import MappingError, MappingResult, MappingSettings, edit_plan_to_spec
from trip_vlog.filmcraft.timeline_spec import TimelineSpec, downgrade_dissolves
from trip_vlog.handoff.compatibility import (REPORT_JSON as COMPAT_JSON, REPORT_MD as COMPAT_MD, CompatInputs, build_compatibility_report,
                                             load_finish_report, render_compatibility_markdown, summary_of)
from trip_vlog.handoff.captions import REPORT_NAME as CAPTIONS_REPORT, SRT_NAME, CaptionSettings, build_captions, write_srt
from trip_vlog.handoff.interchange import export_handoff
from trip_vlog.handoff.media_manifest import manifest_rows, missing_used_media, write_media_manifest
from trip_vlog.handoff.report import HandoffReportInputs, read_resolve_verification, render_handoff_report
from trip_vlog.logging_utils import Status, utc_now_iso
from trip_vlog.media.segment import segment_assets, segment_inputs
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan, edit_plan_hash, to_json_dict
from trip_vlog.planning.pipeline import PlanPaths, plan_baseline_stage, plan_inputs, plan_paths
from trip_vlog.planning.validate import ValidationReport, ValidationSettings, validate_edit_plan
from trip_vlog.qc.checks import TranscriptSpan
from trip_vlog.qc.report import QcInputs, run_qc
from trip_vlog.state import StageResult, StageRunner, atomic_write_json, atomic_write_text, stable_hash

if TYPE_CHECKING:  # pragma: no cover
    from trip_vlog.orchestrator import RunContext

DRAFT_STAGES = ("scan", "probe", "catalog", "thumbnails", "segment", "plan", "approval", "assemble", "preview", "qc", "revise", "critic", "handoff")   # revise: only for `trip-vlog revise`; critic: only with qc.critic != off
CHANGE_APPROVAL_MODES = ("critic_fix", "revision")   # approval modes of a plan that a round of localized changes produced (bound to the new plan's hash)

# Test hook: replace to drive a fake FilmCraft server (``lambda ctx: FilmCraftAdapter(..., argv_prefix=...)``).
adapter_factory: Callable[["RunContext"], FilmCraftAdapter] | None = None


class PlanRejected(ValueError):
    """The on-disk edit plan failed validation; ``report`` has the findings."""

    def __init__(self, message: str, report: ValidationReport | None = None):
        super().__init__(message)
        self.report = report


@dataclass
class Prepared:
    plan: EditPlan
    mapping: MappingResult
    validation: ValidationReport
    plan_hash: str

    @property
    def spec(self) -> TimelineSpec:
        return self.mapping.spec


# ---- plan file, approval -----------------------------------------------------------------------------------

def _pp(ctx: Any) -> PlanPaths:
    return plan_paths(ctx.workspace, ctx.config, dict(ctx.paths.outputs))


def load_plan_json(path: Path) -> Any:
    """Parse a JSON file the user may have edited by hand. ``utf-8-sig``: Notepad and some editors on Windows add a byte order mark."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def plan_file_hash(path: Path) -> str:
    """Hash of the plan *content* (not the recorded plan_hash field), so edits are always noticed."""
    doc = load_plan_json(path)
    try:
        return edit_plan_hash(EditPlan.model_validate(doc))
    except ValidationError:
        return "invalid-" + stable_hash(doc)[:48]


def validate_plan_file(ctx: Any) -> tuple[EditPlan, ValidationReport]:
    path = _pp(ctx).edit_plan
    if not path.is_file():
        raise PlanRejected(f"{path} does not exist; run the plan stage first")
    try:
        doc = load_plan_json(path)
    except (OSError, ValueError) as e:
        raise PlanRejected(f"{path.name} is not readable JSON: {e}") from None
    view = PlanningCatalog.from_catalog(ctx.catalog)
    report = validate_edit_plan(doc, view, ValidationSettings.effective(ctx.config))
    if not report.ok:
        atomic_write_json(_pp(ctx).plans_dir / "assemble_validation_report.json", report.to_dict())
        raise PlanRejected("edit plan rejected before any FilmCraft change:\n" + report.format(), report)
    assert report.plan is not None
    return report.plan, report


def approval_record(manifest: Any) -> dict[str, Any] | None:
    ap = manifest.get("approval")
    return ap if isinstance(ap, dict) else None


def approve_current_plan(manifest: Any, plan_hash: str, mode: str, **extra: Any) -> dict[str, Any]:
    ap = {"status": "approved", "mode": mode, "plan_hash": plan_hash, "approved_at": utc_now_iso(), **extra}
    manifest.set(approval=ap)
    manifest.save()
    return ap


def _derived_approval(ctx: Any, plan_hash: str) -> dict[str, Any] | None:
    """llm planner: the human checkpoint is the outline (DESIGN §9.5), so a plan the planner made from the approved outline and
    that nobody has touched since is approved automatically, and recorded as such. Not when `workflow.plan_review` asks for a
    stop, and never for a plan whose content differs from what the plan stage wrote (a hand edit needs its own approval)."""
    from trip_vlog.outline_stages import outline_approval_record

    m, cfg = ctx.manifest, ctx.config
    if cfg.workflow.planner != "llm" or cfg.workflow.plan_review:
        return None
    oap, rec = outline_approval_record(m), m.get("plan") or {}
    if not oap or oap.get("status") != "approved" or rec.get("planner") != "llm":
        return None
    if rec.get("outline_hash") != oap.get("outline_hash") or rec.get("plan_hash") != plan_hash:
        return None
    return {"outline_hash": oap["outline_hash"], "outline_approval_mode": oap.get("mode")}


def approval_stage(ctx: Any) -> StageResult:
    path = _pp(ctx).edit_plan
    cur = plan_file_hash(path)
    m = ctx.manifest
    held = approval_record(m)
    if held and held.get("status") == "approved" and held.get("mode") in CHANGE_APPROVAL_MODES and held.get("plan_hash") == cur:
        # the plan is the product of a critic fix (qc.critic: apply) or a natural-language revision: approved on the strength of its approved parent and
        # the revision it records, bound to this plan's hash; any edit changes the hash and falls through to the normal rules below
        return StageResult(Status.SUCCEEDED, {"mode": held.get("mode"), "plan_hash": cur, "parent_plan_hash": held.get("parent_plan_hash"), "revision": held.get("revision")})
    if ctx.config.workflow.approval == "auto_approve":
        approve_current_plan(m, cur, "automatic")
        return StageResult(Status.SUCCEEDED, {"mode": "automatic", "plan_hash": cur})
    derived = _derived_approval(ctx, cur)
    if derived is not None:
        approve_current_plan(m, cur, "derived_from_outline", **derived)
        return StageResult(Status.SUCCEEDED, {"mode": "derived_from_outline", "plan_hash": cur, **derived})
    ap = approval_record(m)
    if ap and ap.get("status") == "approved" and ap.get("plan_hash") == cur:
        return StageResult(Status.SUCCEEDED, {"mode": ap.get("mode"), "plan_hash": cur})
    summary: dict[str, Any] = {"plan_hash": cur, "plan_summary": _rel(ctx, _pp(ctx).summary), "edit_plan": _rel(ctx, path)}
    if ap and ap.get("status") == "approved":
        summary["invalidated"] = f"plan changed since approval (approved {str(ap.get('plan_hash'))[:12]}, now {cur[:12]})"
        m.set(approval={"status": "invalidated", "previous_plan_hash": ap.get("plan_hash"), "plan_hash": cur, "at": utc_now_iso()})
    else:
        m.set(approval={"status": "pending", "plan_hash": cur, "at": utc_now_iso()})
    m.save()
    return StageResult(Status.AWAITING_APPROVAL, summary)


def _rel(ctx: Any, p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(ctx.workspace.root).as_posix()
    except ValueError:
        return str(p)


# ---- prepare (validate + map) ------------------------------------------------------------------------------

def _prepare(ctx: Any) -> Prepared:
    path = _pp(ctx).edit_plan
    key = plan_file_hash(path)
    cached = ctx.data.get("prepared")
    if cached is not None and cached[0] == key:
        return cached[1]
    plan, report = validate_plan_file(ctx)
    view = PlanningCatalog.from_catalog(ctx.catalog)
    cfg = ctx.config
    try:
        mapping = edit_plan_to_spec(plan, view, ctx.paths.media_root,
                                    MappingSettings(sequence_name=cfg.filmcraft.sequence_name, ambient_gain_db=cfg.filmcraft.ambient_low_gain_db))
    except MappingError as e:
        raise PlanRejected(f"edit plan cannot be mapped to a FilmCraft timeline: {e}", report) from None
    prep = Prepared(plan, mapping, report, key)
    ctx.data["prepared"] = (key, prep)
    return prep


def get_adapter(ctx: Any) -> FilmCraftAdapter:
    ad = ctx.data.get("adapter")
    if ad is None:
        if adapter_factory is not None:
            ad = adapter_factory(ctx)
        else:
            c = ctx.config.filmcraft
            ad = FilmCraftAdapter(ctx.workspace.root, ctx.paths.media_root, config=c, executable=c.executable,
                                  request_timeout=c.request_timeout_seconds, ffprobe_path=str(ctx.ffprobe) if ctx.ffprobe else None,
                                  handle_policy=c.handle_policy, preview_file=ctx.config.outputs.preview_file,
                                  interchange_dir=ctx.config.outputs.interchange_dir)
        ctx.data["adapter"] = ad
    return ad


def latest_run(ctx: Any) -> dict[str, Any] | None:
    """The newest build that COMPLETED (journal status ``complete``, project file present). A failed or interrupted build is never returned, so
    `resume`, preview, QC, the critic, revisions and the handoff only ever work on a project that was verified after reopening."""
    jp = ctx.workspace.root / "filmcraft" / "execution_journal.json"
    if not jp.is_file():
        return None
    doc = load_journal(jp)
    runs = [r for r in doc["runs"] if r.get("project_name") == ctx.config.filmcraft.project_name and r.get("status") == "complete"]
    if not runs:
        return None
    r = sorted(runs, key=lambda r: r["version"])[-1]
    return r if (ctx.workspace.root / r["project_path"]).is_file() else None


def failed_versions(ctx: Any) -> list[dict[str, Any]]:
    """Builds of this project that failed or were interrupted (from the journal). Their `_vNNN.fcproj` files are kept for inspection and are not used."""
    jp = ctx.workspace.root / "filmcraft" / "execution_journal.json"
    return failed_builds(load_journal(jp), ctx.config.filmcraft.project_name) if jp.is_file() else []


def record_failed_builds(ctx: Any) -> list[dict[str, Any]]:
    """Copy the journal's failed builds into the manifest (`filmcraft.failed_versions`), which `status` shows. Merges into the existing `filmcraft` record."""
    failed = failed_versions(ctx)
    fc = dict(ctx.manifest.get("filmcraft") or {})
    if failed or "failed_versions" in fc:
        fc["failed_versions"] = failed
        ctx.manifest.set(filmcraft=fc)
        ctx.manifest.save()
    return failed


def _project_sha(ctx: Any) -> str | None:
    r = latest_run(ctx)
    return r.get("project_sha256") if r else None


# ---- stages ----------------------------------------------------------------------------------------------

PLAN_FILES = ("edit_plan", "selection_plan", "summary")     # PlanPaths attributes of what a plan stage writes next to each other


def _backup_edited_plan(ctx: Any, path: Path) -> str | None:
    """The stage is about to replace `plans/edit_plan.json`: keep a copy of one the user edited by hand
    (`edit_plan.<hash>.bak.json`), the way the outline stage does for the outline. 'Edited' = it differs from the plan the last
    plan stage recorded."""
    if not path.is_file():
        return None
    prev = (ctx.manifest.stage("plan").get("summary") or {}).get("plan_hash") or (ctx.manifest.get("plan") or {}).get("plan_hash")
    try:
        cur = plan_file_hash(path)
    except (OSError, ValueError):
        cur = "invalid-" + sha256_file(path)[:48]
    if cur == prev:
        return None
    bak = path.with_name(f"{path.stem}.{cur[:12].replace('-', '')}.bak.json")
    shutil.copyfile(path, bak)
    return _rel(ctx, bak)


def _set_stale(ctx: Any) -> tuple[list[str], list[str]]:
    """The plan stage failed: the files of an earlier plan must not look current next to the failure report. Each is renamed
    `<name>.stale<ext>` (`edit_plan.stale.json`, `plan_summary.stale.md`, `selection_plan.stale.json`), replacing an older stale copy;
    the next successful plan stage writes the normal names again. The approval record is untouched: it is bound to a hash that no
    file carries any more, so nothing can be assembled from a stale plan."""
    pp = _pp(ctx)
    moved: list[str] = []
    failed: list[str] = []
    for attr in PLAN_FILES:
        f: Path = getattr(pp, attr)
        if f.is_file():
            try:
                f.replace(f.with_name(f"{f.stem}.stale{f.suffix}"))
                moved.append(_rel(ctx, f))
            except OSError:      # e.g. a file locked by another program on Windows
                failed.append(_rel(ctx, f))
    return moved, failed


def plan_stage(ctx: Any) -> StageResult:
    planner = ctx.config.workflow.planner
    backup = _backup_edited_plan(ctx, _pp(ctx).edit_plan)
    if planner == "llm":
        from trip_vlog.selection_stages import plan_llm_stage

        res = plan_llm_stage(ctx)
    else:
        res = plan_baseline_stage(ctx)
    ctx.data.pop("prepared", None)
    if backup:
        res.summary["edited_plan_backup"] = backup
    if res.status == Status.FAILED and (res.summary.pop("planning_failure", False) or planner != "llm"):
        # only when the plan itself was refused (validation / selection failure): an endpoint timeout, a 401/429/5xx or a missing
        # prerequisite says nothing about the earlier plan, which stays where it is
        stale, failed = _set_stale(ctx)
        if stale:
            res.summary["stale_files"] = stale
        if failed:
            res.summary["stale_rename_failed"] = failed
    elif res.status == Status.FAILED:
        res.summary.pop("planning_failure", None)
    else:
        ctx.manifest.set(planner=planner, plan={
            "path": _rel(ctx, _pp(ctx).edit_plan), "summary": _rel(ctx, _pp(ctx).summary), "plan_hash": res.summary.get("plan_hash"),
            "planner": planner, "outline_hash": res.summary.get("outline_hash")})
        _forget_revision_rules(ctx)
        ctx.manifest.save()
    return res


def _forget_revision_rules(ctx: Any) -> None:
    """A plan that was made again starts from the configured rules: the revision overrides (target length, waived requirements) and an open proposal
    belonged to the earlier plan, even when the new plan happens to have the same hash."""
    st = ctx.manifest.get("revise")
    if isinstance(st, dict) and (st.get("overrides") or st.get("pending")):
        ctx.manifest.set(revise={**st, "overrides": None, "pending": None})
    ctx.config.set_revision_overrides(None)


def _assemble_inputs(ctx: Any) -> Any:
    c = ctx.config.filmcraft
    return {"plan": plan_file_hash(_pp(ctx).edit_plan),
            "fc": [c.project_name, c.sequence_name, c.handle_policy, c.ambient_low_gain_db, c.executable],
            "assets": [(a["asset_id"], a["content_version"]) for a in ctx.catalog.list_assets()]}


def _assemble_verify(ctx: Any) -> bool:
    r = latest_run(ctx)
    if not r:
        return False
    p = ctx.workspace.root / r["project_path"]
    return r["spec_hash"] == _prepare(ctx).spec.spec_hash() and r.get("project_sha256") == sha256_file(p)


def assemble_stage(ctx: Any) -> StageResult:
    try:
        prep = _prepare(ctx)
    except PlanRejected as e:
        return StageResult(Status.FAILED, {"errors": [i.to_dict() for i in (e.report.errors if e.report else [])], "plan_rejected": True}, str(e))
    atomic_write_json(ctx.workspace.root / "filmcraft" / "mapping_report.json", prep.mapping.to_dict())
    ad = get_adapter(ctx)
    cfg = ctx.config
    try:
        build = ad.build_project(prep.spec, cfg.filmcraft.project_name)
    except Exception as e:  # noqa: BLE001 - FilmCraft, path policy, spec and verification errors all stop assembly
        failed = record_failed_builds(ctx)    # the partial project stays on disk, marked failed in the journal and manifest, and is not used
        return StageResult(Status.FAILED, {"mapping_warnings": len(prep.mapping.warnings), **({"failed_version": failed[-1]["version"], "failed_project": failed[-1]["project"]} if failed else {})},
                           f"{type(e).__name__}: {e}")
    run = build.run
    ctx.manifest.set(filmcraft={
        "version": run.get("filmcraft_version"), "capability_snapshot": "filmcraft/capability_snapshot.json",
        "executable": str(getattr(ad.capabilities, "executable", "") or "") or None,
        "project": run["project_path"], "project_version": build.version, "spec_hash": build.spec_hash,
        "journal": "filmcraft/execution_journal.json"})
    ctx.manifest.save()
    record_failed_builds(ctx)
    n_clips = sum(len(s.clips) for s in prep.plan.scenes)
    summary = {"project": run["project_path"], "version": build.version, "noop": build.noop, "clips": n_clips,
               "placements": len(prep.spec.placements), "verified_after_reopen": bool(run.get("verified_after_reopen")),
               "filmcraft_version": run.get("filmcraft_version"), "mapping_warnings": [w.to_dict() for w in prep.mapping.warnings],
               "warnings": list(build.warnings)}
    return StageResult(Status.SUCCEEDED, summary)  # warnings are recorded in the summary and the handoff report


def _preview_path(ctx: Any) -> Path:
    return Path(ctx.paths.outputs["preview_file"])


def _preview_inputs(ctx: Any) -> Any:
    return {"project": _project_sha(ctx), "scale": ctx.config.filmcraft.preview_scale}


def preview_stage(ctx: Any) -> StageResult:
    run = latest_run(ctx)
    if run is None:
        return StageResult(Status.FAILED, {}, "no completed FilmCraft project to render")
    ad = get_adapter(ctx)
    out = _preview_path(ctx)
    try:
        rep = ad.render_preview(out, project_path=ctx.workspace.root / run["project_path"], scale=ctx.config.filmcraft.preview_scale)
    except Exception as e:  # noqa: BLE001
        return StageResult(Status.FAILED, {}, f"{type(e).__name__}: {e}")
    atomic_write_json(out.parent / "render_report.json", rep.to_dict())
    summary = {"preview": _rel(ctx, out), "duration_seconds": rep.duration_seconds, "size": f"{rep.width}x{rep.height}",
               "render_seconds": rep.render_seconds, "checks_failed": [c.name for c in rep.failures]}
    return StageResult(Status.SUCCEEDED if rep.ok else Status.PARTIAL, summary)


def _qc_paths(ctx: Any) -> tuple[Path, Path]:
    d = _preview_path(ctx).parent
    return d / "qc_report.json", d / "contact_sheet_timeline.jpg"


def _qc_index_path(ctx: Any) -> Path:
    return Path(ctx.paths.outputs["qc_dir"]) / "preview_sheet.json"


def _file_sha(p: Path) -> str | None:
    return sha256_file(p) if p.is_file() else None


def _qc_transcripts(ctx: Any) -> dict[str, list[TranscriptSpan]]:
    """asset_id -> transcript segments of the CURRENT content of the asset (empty when transcription did not run)."""
    cur = {a["asset_id"]: a["content_version"] for a in ctx.catalog.list_assets()}
    out: dict[str, list[TranscriptSpan]] = {}
    for r in ctx.catalog.list_transcripts():
        if r["content_version"] != cur.get(r["asset_id"]):
            continue
        try:
            coarse = json.loads(r["data_json"] or "{}").get("timestamps") == "coarse"
        except ValueError:
            coarse = False
        out.setdefault(r["asset_id"], []).append(TranscriptSpan(float(r["start_seconds"] or 0.0), float(r["end_seconds"] or 0.0), r["text"] or "",
                                                                coarse, r["transcript_id"]))
    return out


def _qc_outline_scene_ids(ctx: Any) -> list[str] | None:
    """Scenes of the approved outline the plan was made from, in order; None for a baseline plan or an outline edited since."""
    from trip_vlog.outline_stages import outline_paths
    from trip_vlog.planning.outline import outline_file_hash, read_outline_doc

    rec = ctx.manifest.get("plan") or {}
    h = rec.get("outline_hash")
    if not h or rec.get("planner") != "llm":
        return None
    path = outline_paths(ctx).outline
    if not path.is_file() or outline_file_hash(path) != h:
        return None
    try:
        return [str(sc["scene_id"]) for sc in read_outline_doc(path)["scenes"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _qc_inputs(ctx: Any) -> Any:
    audio = [(m.moment_id, (m.features or {}).get("audio")) for m in PlanningCatalog.from_catalog(ctx.catalog).moments.values()]
    return {"project": _project_sha(ctx), "preview": _file_sha(_preview_path(ctx)), "plan": plan_file_hash(_pp(ctx).edit_plan),
            "qc": ctx.config.qc.model_dump(), "tol": [ctx.config.project.duration_tolerance_pct, ctx.config.project.duration_tolerance_seconds],
            **({"target": ctx.config.effective_target_seconds} if ctx.config.revision_overrides.get("target_duration_seconds") else {}),
            "ambient_db": ctx.config.filmcraft.ambient_low_gain_db, "ffmpeg": ctx.ffmpeg is not None,
            "evidence": stable_hash({"audio": sorted(audio, key=lambda x: x[0]),
                                     "transcripts": {a: [(t.start, t.end, t.text, t.coarse) for t in ts] for a, ts in _qc_transcripts(ctx).items()}}),
            "outline": _qc_outline_scene_ids(ctx)}


def _qc_verify(ctx: Any) -> bool:
    rp = _qc_paths(ctx)[0]
    if not rp.is_file() or not rp.with_suffix(".md").is_file():
        return False
    try:
        idx = (load_plan_json(rp).get("contact_sheet") or {}).get("index")
    except (OSError, ValueError):
        return False
    return not idx or _qc_index_path(ctx).is_file()


def qc_stage(ctx: Any) -> StageResult:
    run = latest_run(ctx)
    if run is None:
        return StageResult(Status.FAILED, {}, "no completed FilmCraft project to check")
    try:
        prep = _prepare(ctx)
    except PlanRejected as e:
        return StageResult(Status.FAILED, {}, str(e))
    ad = get_adapter(ctx)
    proj_path = ctx.workspace.root / run["project_path"]
    seq = project = None
    mism: list[str] = []
    err = None
    try:
        ad.close()  # a fresh server process proves the saved file reopens
        project = ad.open_project(proj_path)
        seq = ad.inspect_sequence()
        eff: TimelineSpec = downgrade_dissolves(prep.spec, run.get("downgraded_dissolves") or [])
        mism = [str(m) for m in ad.verify(eff, MediaMap.from_dict(run.get("media_mapping", {}))).mismatches]
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        seq = None
    report_path, sheet = _qc_paths(ctx)
    doc = run_qc(QcInputs(
        plan=prep.plan, catalog=PlanningCatalog.from_catalog(ctx.catalog), source_paths=prep.spec.asset_paths, sequence=seq,
        project=project, reopen_error=err, mismatches=mism, preview=_preview_path(ctx), report_path=report_path, contact_sheet=sheet,
        ffmpeg=ctx.ffmpeg, ffprobe=str(ctx.ffprobe) if ctx.ffprobe else None, config=ctx.config,
        transcripts=_qc_transcripts(ctx), outline_scene_ids=_qc_outline_scene_ids(ctx), index_path=_qc_index_path(ctx),
        workspace_root=ctx.workspace.root))
    cs = doc["contact_sheet"]
    summary = {"status": doc["status"], **doc["counts"], "report": _rel(ctx, report_path),
               "contact_sheet": _rel(ctx, sheet) if cs["path"] else None, "preview_sheets": len(cs.get("sheets") or []),
               "preview_sheet_index": cs.get("index"),
               "failed_checks": [c["name"] for c in doc["checks"] if c["status"] == "fail"],
               "warned_checks": [c["name"] for c in doc["checks"] if c["status"] == "warn"]}
    return StageResult(Status.SUCCEEDED, summary)  # QC findings are reported in the summary, they do not fail the stage


COMPAT_FINISH = "resolve_finish_report.json"


def _capabilities(ctx: Any) -> dict[str, bool] | None:
    """FilmCraft capability name -> usable, from this workspace's capability snapshot (None when there is none)."""
    p = ctx.workspace.root / "filmcraft" / "capability_snapshot.json"
    try:
        caps = json.loads(p.read_text(encoding="utf-8"))["capabilities"]
        return {name: bool(c.get("usable")) for name, c in caps.items()}
    except (OSError, ValueError, KeyError, AttributeError):
        return None


def _handoff_dir(ctx: Any) -> Path:
    return Path(ctx.paths.outputs["interchange_dir"])


def _qc_paths_critic(ctx: Any) -> Path:
    return Path(ctx.paths.outputs["qc_dir"]) / "critic_report.md"


def _captions_inputs(ctx: Any) -> Any:
    """Captions come from the plan and the current transcripts; the options shape them. Off: None (a file left from an earlier run is set aside)."""
    if not ctx.config.captions.enabled:
        return {"off": True}
    return {"options": ctx.config.captions.model_dump(),
            "transcripts": stable_hash({a: [(t.start, t.end, t.text, t.coarse) for t in ts] for a, ts in sorted(_qc_transcripts(ctx).items())})}


def _write_captions(ctx: Any, prep: Prepared, out: Path, notes: list[str]) -> dict[str, Any] | None:
    """The optional SRT of the handoff (config `captions.enabled`). Returns what the handoff report says about it."""
    srt = out / SRT_NAME
    prev = (ctx.manifest.get("captions") or {}).get("sha256")
    if not ctx.config.captions.enabled:
        if srt.is_file():       # captions were turned off since
            if prev:            # the file is the one this tool wrote: it does not belong to this handoff any more
                w = write_srt(srt, [], prev)
                notes.extend(w["warnings"])
                if w["stale"]:
                    notes.append(f"captions are off (captions.enabled): the earlier {SRT_NAME} was set aside as {w['stale']}")
            else:
                notes.append(f"captions are off (captions.enabled): {SRT_NAME} in the handoff folder was not written by this tool and was left alone")
        if prev:
            ctx.manifest.set(captions=None)
            ctx.manifest.save()
        return None
    res = build_captions(prep.plan, _qc_transcripts(ctx), CaptionSettings.from_config(ctx.config))
    w = write_srt(srt, res.cues, prev)
    info = {**res.to_dict(), "file": SRT_NAME if res.cues else None, "sha256": w["sha256"], "backup_of_edited_file": w["backup"],
            "encoding": "UTF-8 without BOM, CRLF line endings", "generated_at": utc_now_iso()}
    atomic_write_json(out / CAPTIONS_REPORT, info)
    ctx.manifest.set(captions={"sha256": w["sha256"], "file": info["file"], "cues": len(res.cues)})
    ctx.manifest.save()
    notes.extend(w["warnings"])
    if w["backup"]:
        notes.append(f"{SRT_NAME} had been edited since it was written; that version was kept as {w['backup']}")
    if not res.cues:
        notes.append("captions are on but no clip has usable transcript text (see captions_report.json); no SRT was written. Transcribe first with `trip-vlog transcribe`")
    return info


def _handoff_inputs(ctx: Any) -> Any:
    out = _handoff_dir(ctx)
    return {"project": _project_sha(ctx), **({"critic_report": c} if (c := _file_sha(_qc_paths_critic(ctx))) else {}), "plan": plan_file_hash(_pp(ctx).edit_plan),
            "formats": list(ctx.config.filmcraft.interchange_formats), "qc": _file_sha(_qc_paths(ctx)[0]),
            "resolve_verification": _file_sha(out / "resolve_verification.json"),
            "resolve_finish_report": _file_sha(out / COMPAT_FINISH),
            "filmcraft_version": (ctx.manifest.get("filmcraft") or {}).get("version"),
            "failed_builds": [f["version"] for f in failed_versions(ctx)], "captions": _captions_inputs(ctx),
            "assets": [(a["asset_id"], a["content_version"]) for a in ctx.catalog.list_assets()]}


def handoff_stage(ctx: Any) -> StageResult:
    run = latest_run(ctx)
    if run is None:
        return StageResult(Status.FAILED, {}, "no completed FilmCraft project to hand off")
    try:
        prep = _prepare(ctx)
    except PlanRejected as e:
        return StageResult(Status.FAILED, {}, str(e))
    cfg: Config = ctx.config
    out = _handoff_dir(ctx)
    out.mkdir(parents=True, exist_ok=True)
    proj = ctx.workspace.root / run["project_path"]
    eff = downgrade_dissolves(prep.spec, run.get("downgraded_dissolves") or [])
    notes: list[str] = []
    cs = ctx.manifest.get("critic") or {}
    if cs.get("rounds"):
        from trip_vlog.critic_stages import lineage

        applied = lineage(cs, prep.plan.plan_hash)
        for a in applied:
            if a.get("halt_note"):
                notes.append(f"WARNING: {a['halt_note']}")
        if applied:
            notes.append(f"this is project version v{run['version']}: {len(applied)} critic round(s) changed the plan after the first draft (" + ", ".join(
                a["revision"] for a in applied if a.get("revision")) + "); the earlier project versions are kept in filmcraft/ and the parent plans in plans/revisions/")
    if (ctx.manifest.get("revise") or {}).get("rounds"):
        from trip_vlog.revise_stages import handoff_notes

        notes += handoff_notes(ctx, prep.plan.plan_hash, run["version"])
    rs = [r for r in (ctx.manifest.get("reseal") or []) if isinstance(r, dict) and r.get("what") == "plan" and r.get("hash") == prep.plan.plan_hash]
    if rs:
        notes.append(f"this plan was edited by hand and re-sealed with `trip-vlog reseal` on {rs[-1].get('at')} (previous content hash {str(rs[-1].get('previous_hash'))[:12]}"
                     + (f", backup {rs[-1]['backup']}" if rs[-1].get("backup") else "") + ")")
    failed = failed_versions(ctx)
    if failed:
        notes.append("failed FilmCraft build(s) " + ", ".join(f"v{f['version']} ({f['status']})" for f in failed) + " are listed in filmcraft/execution_journal.json; their "
                     "partial _vNNN.fcproj files are kept for inspection, are not part of this handoff, and can be deleted by you when no longer wanted")
    if _qc_paths_critic(ctx).is_file():
        notes.append(f"critic report: {_rel(ctx, _qc_paths_critic(ctx))}")
    export = export_handoff(get_adapter(ctx), eff, cfg.filmcraft.interchange_formats, out, proj)
    if export.error:
        notes.append(f"interchange export failed: {export.error}")
    captions = _write_captions(ctx, prep, out, notes)
    rows = manifest_rows(ctx.catalog.list_assets(), ctx.paths.media_root, prep.mapping.used_asset_ids)
    write_media_manifest(out / "media_manifest.csv", rows)
    atomic_write_json(out / "edit_plan.json", to_json_dict(prep.plan))
    ref = {"project_path": str(proj), "project_relative_to_workspace": run["project_path"], "project_version": run["version"],
           "sha256": run.get("project_sha256"), "filmcraft_version": run.get("filmcraft_version"),
           "note": "The .fcproj is referenced, not copied. Media is referenced by absolute path and was not copied."}
    atomic_write_json(out / "project_reference.json", ref)
    qc_status = None
    qp = _qc_paths(ctx)[0]
    if qp.is_file():
        try:
            qc_status = load_plan_json(qp).get("status")
        except (OSError, ValueError):
            qc_status = None
    preview = _preview_path(ctx)
    missing = missing_used_media(rows)
    xml_fmt = next((f for f in (export.report.formats if export.report else []) if f.format == "fcp7_xml" and f.exported), None)
    finish_doc, finish_note = load_finish_report(out / COMPAT_FINISH)
    if finish_note:
        notes.append(finish_note)
    compat = build_compatibility_report(CompatInputs(
        plan=prep.plan, spec=eff, catalog=PlanningCatalog.from_catalog(ctx.catalog), capabilities=_capabilities(ctx),
        xml_path=Path(xml_fmt.path) if xml_fmt else None, filmcraft_version=run.get("filmcraft_version"),
        verified_after_reopen=run.get("verified_after_reopen"), mapping_warnings=[w.to_dict() for w in prep.mapping.warnings],
        finish_report=finish_doc, tool_version=__version__))
    atomic_write_json(out / COMPAT_JSON, compat)
    atomic_write_text(out / COMPAT_MD, render_compatibility_markdown(compat))
    compat_summary = summary_of(compat)
    text = render_handoff_report(HandoffReportInputs(
        title=prep.plan.project.title, filmcraft_version=run.get("filmcraft_version"), tool_version=__version__,
        platform=f"{ctx.manifest.get('platform', {}).get('system', '?')} {ctx.manifest.get('platform', {}).get('machine', '')}".strip(),
        out_dir=out, export=export, project_ref=ref, preview_path=str(preview) if preview.is_file() else None,
        plan_hash=prep.plan.plan_hash, qc_status=qc_status, media_rows=rows, missing_media=missing,
        mapping_warnings=[w.to_dict() for w in prep.mapping.warnings],
        verification=read_resolve_verification(out / "resolve_verification.json"), stage_notes=notes, compatibility=compat_summary, captions=captions))
    atomic_write_text(out / "handoff_report.md", text)
    atomic_write_json(out / "handoff_summary.json", {**export.to_dict(), "missing_media": missing, "generated_at": utc_now_iso(),
                                                      "compatibility": compat_summary})
    ok = export.report is not None and export.report.ok and not missing and compat_summary["xml_roundtrip_ok"] is not False
    summary = {"formats": export.exported_files and [Path(f).name for f in export.exported_files], "files": sorted(p.name for p in out.iterdir()),
               "missing_media": len(missing), "export_error": export.error, "otio_reload": (export.otio_reload or {}).get("status"),
               "compatibility": compat_summary, "compatibility_report": _rel(ctx, out / COMPAT_JSON),
               **({"captions": {k: captions[k] for k in ("file", "cues", "clips_captioned")}} if captions else {})}
    return StageResult(Status.SUCCEEDED if ok else Status.PARTIAL, summary)


# ---- registration ----------------------------------------------------------------------------------------

def build_draft_runner(ctx: Any) -> StageRunner:
    from trip_vlog.orchestrator import build_index_runner
    from trip_vlog.orchestrator import build_outline_runner as _build_outline_runner

    plan_path_exists = lambda c: _pp(c).edit_plan.is_file()
    if ctx.config.workflow.planner == "llm":
        # index, segment, audio, transcribe, analyze, outline, outline_approval (the human checkpoint), then the LLM selection.
        # The baseline planner never needs an LLM and skips all of that.
        from trip_vlog.selection_stages import selection_inputs

        runner = _build_outline_runner(ctx)
        runner.register("plan", plan_stage, deps=("outline_approval",), verify=plan_path_exists, inputs=selection_inputs)
    else:
        runner = build_index_runner(ctx)
        runner.register("segment", segment_assets, deps=("catalog",), inputs=segment_inputs)
        runner.register("plan", plan_stage, deps=("segment",), verify=plan_path_exists,
                        inputs=lambda c: {**plan_inputs(c), "planner": c.config.workflow.planner})
    runner.register("approval", approval_stage, deps=("plan",), always_run=True)
    runner.register("assemble", assemble_stage, deps=("approval",), inputs=_assemble_inputs, verify=_assemble_verify)
    runner.register("preview", preview_stage, deps=("assemble",), inputs=_preview_inputs, verify=lambda c: _preview_path(c).is_file())
    runner.register("qc", qc_stage, deps=("assemble",), inputs=_qc_inputs, verify=_qc_verify)
    from trip_vlog.critic_stages import critic_inputs, critic_stage, critic_verify, critic_wanted
    from trip_vlog.revise_stages import revise_inputs, revise_stage, revise_wanted

    if revise_wanted(ctx):
        # after qc, before the critic and the handoff: a revision rebuilds the project, preview and QC, and everything after it works on the latest version.
        # Registered for `trip-vlog revise`, and (in apply mode) when a proposed revision was approved with `approve --revision`.
        runner.register("revise", revise_stage, deps=("qc",), inputs=revise_inputs)
    if critic_wanted(ctx):
        # after qc, before handoff: a critic fix rebuilds the project, preview and QC, and the handoff then packages the latest version.
        # Also registered (in apply mode) when a revision was approved with `approve --critic`, whatever qc.critic says now.
        runner.register("critic", critic_stage, deps=("qc",), inputs=critic_inputs, verify=critic_verify)
    runner.register("handoff", handoff_stage, deps=("assemble",), inputs=_handoff_inputs,
                    verify=lambda c: (_handoff_dir(c) / "handoff_report.md").is_file() and (_handoff_dir(c) / COMPAT_JSON).is_file()
                    and (not c.config.captions.enabled or (_handoff_dir(c) / CAPTIONS_REPORT).is_file()))
    return runner
