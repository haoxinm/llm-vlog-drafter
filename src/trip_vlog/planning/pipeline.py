"""Top-level baseline planning: catalog -> selection plan -> edit plan -> validation report -> files under plans/.

`plan_baseline` accepts a RunContext-like object (needs `.catalog`, `.workspace`, `.config`; `.paths.outputs` optional) or a
workspace path. `plan_baseline_stage` / `plan_inputs` plug into the StageRunner (see the report for registration).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trip_vlog.config import Config
from trip_vlog.logging_utils import Status
from trip_vlog.media.catalog import Catalog
from trip_vlog.planning.baseline import BaselineSettings, build_baseline_selection
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan, SelectionPlan, to_json_dict, write_schemas
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.planning.summary import render_summary
from trip_vlog.planning.validate import ValidationReport, ValidationSettings, validate_edit_plan, validate_selection_plan
from trip_vlog.state import StageResult, Workspace, atomic_write_json, atomic_write_text, stable_hash


class PlanningError(RuntimeError):
    """Planning produced an invalid plan. `report` holds the validation findings."""

    def __init__(self, message: str, report: ValidationReport | None = None):
        super().__init__(message)
        self.report = report


@dataclass
class PlanPaths:
    plans_dir: Path
    selection_plan: Path
    edit_plan: Path
    summary: Path
    validation_report: Path


def plan_paths(workspace: Workspace, config: Config | None = None, outputs: dict[str, Path] | None = None) -> PlanPaths:
    edit = (outputs or {}).get("edit_plan_file") or (workspace.root / (config.outputs.edit_plan_file if config else "plans/edit_plan.json"))
    d = Path(edit).parent
    return PlanPaths(d, d / "selection_plan.json", Path(edit), d / "plan_summary.md", d / "validation_report.json")


def _resolve_inputs(ctx_or_path: Any, config: Config | None) -> tuple[Workspace, Config, Catalog, bool, dict[str, Path] | None]:
    if hasattr(ctx_or_path, "catalog") and hasattr(ctx_or_path, "workspace"):
        cfg = config or ctx_or_path.config
        paths = getattr(ctx_or_path, "paths", None)
        return ctx_or_path.workspace, cfg, ctx_or_path.catalog, False, (dict(paths.outputs) if paths is not None else None)
    ws = Workspace(Path(ctx_or_path).expanduser().resolve())
    if config is None:
        from trip_vlog.config import load_config
        config = load_config(ws.project_yaml if ws.project_yaml.exists() else None)
    return ws, config, Catalog(ws.catalog_path), True, None


def plan_baseline(ctx_or_workspace: Any, config: Config | None = None, *, write: bool = True) -> tuple[SelectionPlan, EditPlan, ValidationReport]:
    """Build, resolve and validate a baseline plan; write plans/{selection_plan,edit_plan}.json, schema copies, plan_summary.md.

    Returns (selection_plan, edit_plan, report). The edit plan is the validated (and possibly repaired) one. Files are
    written even when validation fails so the findings can be inspected; check `report.ok` before using the plan.
    """
    ws, cfg, catalog, own, outputs = _resolve_inputs(ctx_or_workspace, config)
    try:
        view = PlanningCatalog.from_catalog(catalog)
        selection = build_baseline_selection(view, BaselineSettings.from_config(cfg))
        vset = ValidationSettings.from_config(cfg)
        sel_report = validate_selection_plan(selection, view, vset)
        if not sel_report.ok:
            _write_failure(ws, cfg, outputs, sel_report, write)
            raise PlanningError("baseline selection plan failed validation:\n" + sel_report.format(), sel_report)
        edit = resolve_selection_plan(selection, view, PlanSettings.from_config(cfg))
        report = validate_edit_plan(edit, view, vset)
        report.warnings = sel_report.warnings + [w for w in report.warnings if w not in sel_report.warnings]
        final = report.plan or edit
        if write:
            _write_all(ws, cfg, outputs, selection, final, report, view)
        return selection, final, report
    finally:
        if own:
            catalog.close()


def _write_failure(ws: Workspace, cfg: Config, outputs: dict[str, Path] | None, report: ValidationReport, write: bool) -> None:
    if write:
        atomic_write_json(plan_paths(ws, cfg, outputs).validation_report, report.to_dict())


def _write_all(ws: Workspace, cfg: Config, outputs: dict[str, Path] | None, selection: SelectionPlan, edit: EditPlan,
               report: ValidationReport, view: PlanningCatalog) -> PlanPaths:
    pp = plan_paths(ws, cfg, outputs)
    pp.plans_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(pp.selection_plan, to_json_dict(selection))
    atomic_write_json(pp.edit_plan, to_json_dict(edit))
    write_schemas(pp.plans_dir)
    atomic_write_text(pp.summary, render_summary(edit, view, report))
    atomic_write_json(pp.validation_report, report.to_dict())
    return pp


def plan_inputs(ctx: Any) -> Any:
    """Stage input description: planning-relevant catalog content plus the settings that drive the plan."""
    view = PlanningCatalog.from_catalog(ctx.catalog)
    cfg = ctx.config
    return {"catalog": view.fingerprint(), "baseline": BaselineSettings.from_config(cfg).key(),
            "plan": stable_hash(PlanSettings.from_config(cfg).__dict__), "validation": stable_hash(ValidationSettings.from_config(cfg).__dict__)}


def plan_baseline_stage(ctx: Any) -> StageResult:
    """Stage: plan_baseline; FAILED (so dependents never run) when the plan does not validate."""
    try:
        selection, edit, report = plan_baseline(ctx)
    except PlanningError as e:
        return StageResult(Status.FAILED, {"errors": [i.to_dict() for i in (e.report.errors if e.report else [])]}, str(e))
    except ValueError as e:  # BaselinePlanError, PlanResolutionError
        return StageResult(Status.FAILED, {}, str(e))
    summary = {"scenes": len(edit.scenes), "clips": sum(len(s.clips) for s in edit.scenes),
               "duration_seconds": edit.total_duration_seconds, "target_seconds": edit.project.target_duration_seconds,
               "errors": len(report.errors), "warnings": len(report.warnings), "repairs": len(report.repairs),
               "plan_hash": edit.plan_hash}
    if not report.ok:
        return StageResult(Status.FAILED, summary, "edit plan failed validation:\n" + report.format())
    return StageResult(Status.SUCCEEDED, summary)
