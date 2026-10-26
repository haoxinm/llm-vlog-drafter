"""Phase 4b: the LLM `plan` stage of `draft --planner llm` (DESIGN §8.2, §8.3, §9.5, §11).

Runs after `outline_approval`, so it only ever sees an outline a person approved (or the automatic approval of
``--auto-approve``). Reads `plans/story_outline.json`, asks the planner for clips per scene, resolves and validates, and writes
the same files as the baseline planner: `plans/selection_plan.json`, `plans/edit_plan.json`, `plans/plan_summary.md`,
`plans/validation_report.json` and the schemas, plus `plans/selection_report.json` (attempts, findings, repairs, usage).

Cache key (``selection_inputs``): the approved outline (its content hash), the catalog records of its candidate moments (as the
model sees them) and the planning-relevant catalog fields the resolver reads (ranges, lengths, frame rates, audio), the
whole request text, prompt and schema versions, the editor capabilities, endpoint / model / parameters / mode, retry and token
limits, and the project, editorial, validation and title settings. Edit the outline, change the model or the target duration
(which makes a new outline) or change what the resolver reads of a candidate moment and the plan is made again; nothing else does.
"""

from __future__ import annotations

from typing import Any

from trip_vlog import outline_stages
from trip_vlog.llm.factory import resolve_mode, resolve_model
from trip_vlog.llm.types import LLMError, LLMRefusalError, LLMSchemaError, LLMTruncatedError
from trip_vlog.logging_utils import Status, utc_now_iso
from trip_vlog.planning import outline as ol
from trip_vlog.planning import selection as sl
from trip_vlog.planning.capabilities import capability_facts, capability_lines, read_snapshot
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.pipeline import _write_all, plan_paths
from trip_vlog.planning.resolve_plan import PlanSettings
from trip_vlog.planning.titles import TitleSettings
from trip_vlog.planning.validate import ValidationReport, ValidationSettings
from trip_vlog.state import StageResult, atomic_write_json, stable_hash


class SelectionPrerequisite(RuntimeError):
    """The approved outline is missing or invalid; the message says what to do."""


def build_context(ctx: Any) -> tuple[sl.SelectionContext, str]:
    """Selection context from the workspace: the outline file as it is now (re-validated, never rewritten) and the catalog."""
    cfg = ctx.config
    outline, report, oc = outline_stages.check_outline_file(ctx)
    if outline is None or not report.ok:
        raise SelectionPrerequisite("the story outline is missing or not valid (edit plans/story_outline.json, then `trip-vlog resume`):\n" + report.format())
    h = ol.outline_hash(outline)
    vset = ValidationSettings.from_config(cfg)
    budgets, note = sl.effective_budgets(outline, vset.target_duration_seconds, vset.total_tolerance_seconds())
    sctx = sl.SelectionContext(
        config=cfg, outline=outline, oc=oc, view=PlanningCatalog.from_catalog(ctx.catalog), vset=vset, pset=PlanSettings.from_config(cfg),
        tset=TitleSettings.from_config(cfg), capabilities=capability_lines(cfg, read_snapshot(ctx.workspace.root)), budgets=budgets, budget_note=note,
        min_still_seconds=float(cfg.qc.min_still_seconds))
    return sctx, h


def selection_inputs(ctx: Any) -> Any:
    cfg = ctx.config
    sctx, h = build_context(ctx)
    profile, ep = cfg.llm.endpoint_for("text")
    # "catalog": what the resolver and validators read (ranges, best sub-ranges, lengths, frame rates, audio, versions), which the
    # candidate digest (the text a model sees) does not cover
    return {"outline": h, "candidates": sl.candidate_digest(sctx), "catalog": sctx.view.fingerprint(sctx.outline.moment_ids()), "request": stable_hash(sl.user_text(sctx)), "prompt": sl.prompt_info(),
            "schema": stable_hash(sl.response_schema()), "capabilities": capability_facts(cfg, read_snapshot(ctx.workspace.root)),
            "settings": {"validation": sctx.vset.__dict__, "plan": sctx.pset.__dict__, "titles": sctx.tset.key(), "min_still": sctx.min_still_seconds},
            "max_tokens": sl.max_tokens(cfg), "retries": cfg.planning.selection_retries, "planner": "llm",
            "llm": {"endpoint": profile, "api": ep.api, "model": resolve_model(ep), "params": ep.params,
                    "structured_output": ep.structured_output, "mode": resolve_mode(cfg)}}


def _fail_report(ctx: Any, pp: Any, base: dict[str, Any], message: str, history: list[dict[str, Any]], report: ValidationReport | None) -> StageResult:
    pp.plans_dir.mkdir(parents=True, exist_ok=True)
    rp = pp.plans_dir / "selection_report.json"
    atomic_write_json(rp, {"ok": False, "error": message, "attempts": history, "generated_at": utc_now_iso()})
    if report is not None:
        atomic_write_json(pp.validation_report, report.to_dict())
    return StageResult(Status.FAILED, {**base, "report": outline_stages._rel(ctx, rp)}, message)


def plan_llm_stage(ctx: Any) -> StageResult:
    cfg = ctx.config
    pp = plan_paths(ctx.workspace, cfg, dict(ctx.paths.outputs))
    profile, ep = cfg.llm.endpoint_for("text")
    base = {"planner": "llm", "endpoint": profile, "local": ep.is_local}
    try:
        sctx, h = build_context(ctx)
    except SelectionPrerequisite as e:
        return StageResult(Status.FAILED, base, str(e))
    ap = outline_stages.outline_approval_record(ctx.manifest)
    if not ap or ap.get("status") != "approved" or ap.get("outline_hash") != h:
        return StageResult(Status.FAILED, base, "the story outline has not been approved (or changed since); run `trip-vlog approve` and `trip-vlog resume`")
    try:
        gateway = outline_stages.gateway_factory(ctx) if outline_stages.gateway_factory else outline_stages._default_gateway(ctx)
        res = sl.generate_selection(gateway, sctx)
    except sl.SelectionError as e:
        res = _fail_report(ctx, pp, base, str(e), e.history, e.report)
        res.summary["planning_failure"] = True      # the plan was refused: an earlier plan no longer matches (see draft_stages.plan_stage)
        return res
    except LLMError as e:
        res = _fail_report(ctx, pp, base, f"{type(e).__name__}: {e}", [], None)
        if isinstance(e, (LLMSchemaError, LLMRefusalError, LLMTruncatedError)):
            res.summary["planning_failure"] = True  # the model answered but its reply is unusable: also a refused plan
        return res
    usage = {"requests": res.requests, "input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens}
    _write_all(ctx.workspace, cfg, dict(ctx.paths.outputs), res.selection, res.edit, res.report, sctx.view,
               oc=sctx.oc, outline=sctx.outline, usage=usage)
    sl.write_response_schema(pp.plans_dir)
    atomic_write_json(pp.plans_dir / "selection_report.json", {
        "ok": True, "generated_at": utc_now_iso(), "outline_hash": h, "plan_hash": res.edit.plan_hash, "model": res.model, "prompt": sl.prompt_info(),
        "endpoint": profile, "local": ep.is_local, "usage": res.usage.to_dict(), "requests": res.requests, "response_cache_hits": res.cache_hits,
        "attempts": res.attempts, "warnings": [i.to_dict() for i in res.report.warnings], "repairs": [r.to_dict() for r in res.report.repairs],
        "candidates": sl.candidate_digest(sctx), "capabilities": sctx.capabilities, "estimated_input_tokens": sl.estimate_input_tokens(sctx)})
    summary = {**base, "model": res.model or None, "scenes": len(res.edit.scenes), "clips": sum(len(s.clips) for s in res.edit.scenes),
               "titles": len(res.edit.titles), "duration_seconds": res.edit.total_duration_seconds,
               "target_seconds": res.edit.project.target_duration_seconds, "requests": res.requests, "response_cache_hits": res.cache_hits,
               "input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens, "reasked": len(res.attempts) - 1,
               "repairs": len(res.report.repairs), "warnings": len(res.report.warnings), "errors": 0, "plan_hash": res.edit.plan_hash,
               "outline_hash": h, "report": outline_stages._rel(ctx, pp.plans_dir / "selection_report.json")}
    return StageResult(Status.SUCCEEDED, summary)
