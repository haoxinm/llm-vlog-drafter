"""Phase 4a stages: `outline` (LLM story outline) and `outline_approval` (the human checkpoint, DESIGN §9.5).

  * `outline` writes `plans/story_outline.json` (the unit of approval), `plans/story_outline.md` (read this) and
    `plans/story_outline_report.json` (findings, repairs, what the prompt contained, usage). It caches on everything that
    shapes the request (catalog text, settings, target duration, prompt versions, model/endpoint), so an unchanged re-run
    makes no LLM call and never touches a hand-edited outline. When its inputs change it replaces the file, but first keeps a
    copy of an outline the user had edited (`story_outline.<hash>.bak.json`).
  * `outline_approval` always runs. It re-validates the file deterministically (no LLM): an invalid outline fails the stage; an
    edited one invalidates the earlier approval. With `workflow.approval: stop_after_outline` it stops (exit "awaiting approval")
    until `trip-vlog approve`; with `auto_approve` it records an automatic approval. The approval is bound to the hash of the
    outline's content.

Phase 4b appends its stages after `outline_approval`: ``register_outline_stages(runner, after=...)`` returns the last stage name
to depend on (selection -> resolve -> validate -> assemble).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from trip_vlog.llm.factory import make_gateway, resolve_mode, resolve_model
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.types import LLMError
from trip_vlog.logging_utils import Status, utc_now_iso
from trip_vlog.planning import outline as ol
from trip_vlog.planning.outline_catalog import OutlineCatalog, build_outline_catalog
from trip_vlog.planning.outline_llm import (OutlineError, estimate_input_tokens, generate_outline, max_tokens, prompt_info, user_text)
from trip_vlog.planning.outline_summary import render_outline_summary
from trip_vlog.planning.pipeline import plan_paths
from trip_vlog.planning.validate import ValidationReport
from trip_vlog.state import StageResult, atomic_write_json, atomic_write_text, stable_hash

OUTLINE_STAGES = ("outline", "outline_approval")

# Test hook: replace to inject a gateway built on a fake transport (`lambda ctx: make_gateway(..., "text", transport=fake)`).
gateway_factory: Callable[[Any], Gateway] | None = None


@dataclass
class OutlinePaths:
    plans_dir: Path
    outline: Path
    summary: Path
    report: Path
    validation: Path


def outline_paths(ctx: Any) -> OutlinePaths:
    d = plan_paths(ctx.workspace, ctx.config, dict(ctx.paths.outputs)).plans_dir
    return OutlinePaths(d, d / "story_outline.json", d / "story_outline.md", d / "story_outline_report.json", d / "story_outline_validation.json")


def _rel(ctx: Any, p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(ctx.workspace.root).as_posix()
    except ValueError:
        return str(p)


def _default_gateway(ctx: Any) -> Gateway:
    return make_gateway(ctx.config, ctx.workspace.root, "text", logger=ctx.logger)


# ---- inputs (cache key) ------------------------------------------------------------------------------------

def outline_inputs(ctx: Any) -> Any:
    cfg = ctx.config
    oc = build_outline_catalog(ctx.catalog, cfg)
    profile, ep = cfg.llm.endpoint_for("text")
    settings = ol.OutlineSettings.from_config(cfg)
    return {"request": stable_hash(user_text(cfg, oc)), "prompt": prompt_info(), "schema": stable_hash(ol.response_schema()),
            "settings": settings.__dict__, "max_tokens": max_tokens(cfg), "retries": cfg.planning.outline_retries,
            "llm": {"endpoint": profile, "api": ep.api, "model": resolve_model(ep), "params": ep.params,
                    "structured_output": ep.structured_output, "mode": resolve_mode(cfg)}}


# ---- deterministic file check (no LLM) ----------------------------------------------------------------------

def check_outline_file(ctx: Any, oc: OutlineCatalog | None = None) -> tuple[ol.StoryOutline | None, ValidationReport, OutlineCatalog]:
    """Parse and validate `plans/story_outline.json` as the user may have edited it. Never rewrites the file."""
    oc = oc or build_outline_catalog(ctx.catalog, ctx.config)
    path = outline_paths(ctx).outline
    report = ValidationReport()
    if not path.is_file():
        report.error("missing", f"{path} does not exist; run the outline stage first")
        return None, report, oc
    try:
        doc = ol.read_outline_doc(path)
    except (OSError, ValueError) as e:
        report.error("unreadable", f"{path.name} is not readable JSON: {e}")
        return None, report, oc
    report = ol.validate_outline(doc, oc, ol.OutlineSettings.from_config(ctx.config), repair=False)
    return report.plan, report, oc


def _render(ctx: Any, outline: ol.StoryOutline, oc: OutlineCatalog, report: ValidationReport | None, h: str) -> str:
    return render_outline_summary(outline, oc, target_seconds=float(ctx.config.project.target_duration_seconds), title=ctx.config.project.title,
                                  report=report, outline_hash=h, workspace=str(ctx.workspace.root),
                                  auto_approve=ctx.config.workflow.approval == "auto_approve")


# ---- outline stage -------------------------------------------------------------------------------------------

def _backup_edited(ctx: Any, path: Path) -> str | None:
    """Keep a hand-edited outline before the stage replaces it (its inputs changed)."""
    if not path.is_file():
        return None
    prev = (ctx.manifest.stage("outline").get("summary") or {}).get("outline_hash")
    cur = ol.outline_file_hash(path)
    if cur == prev:
        return None
    bak = path.with_name(f"{path.stem}.{cur[:12].replace('-', '')}.bak.json")
    shutil.copyfile(path, bak)
    return _rel(ctx, bak)


def outline_stage(ctx: Any) -> StageResult:
    cfg, op = ctx.config, outline_paths(ctx)
    oc = build_outline_catalog(ctx.catalog, cfg)
    profile, ep = cfg.llm.endpoint_for("text")
    base = {"endpoint": profile, "local": ep.is_local, "moments_in_prompt": len(oc.moments), "eligible_moments": oc.selection.eligible,
            "left_out": oc.selection.dropped_total}
    try:
        res = generate_outline(gateway_factory(ctx) if gateway_factory else _default_gateway(ctx), cfg, oc)
    except (OutlineError, LLMError) as e:
        hist = getattr(e, "history", [])
        atomic_write_json(op.report, {"ok": False, "error": str(e), "attempts": hist, "selection": oc.selection.to_dict(), "generated_at": utc_now_iso()})
        return StageResult(Status.FAILED, {**base, "report": _rel(ctx, op.report)}, f"{type(e).__name__}: {e}" if isinstance(e, LLMError) else str(e))
    backup = _backup_edited(ctx, op.outline)
    h = ol.outline_hash(res.outline)
    op.plans_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(op.outline, ol.outline_text(res.outline))
    ol.write_schemas(op.plans_dir)
    md = _render(ctx, res.outline, oc, res.report, h)
    atomic_write_text(op.summary, md)
    atomic_write_json(op.report, {
        "ok": True, "outline_hash": h, "generated_at": utc_now_iso(), "model": res.model, "prompt": prompt_info(), "endpoint": profile,
        "local": ep.is_local, "usage": res.usage.to_dict(), "requests": res.requests, "response_cache_hits": res.cache_hits,
        "attempts": res.attempts, "warnings": [i.to_dict() for i in res.report.warnings], "repairs": [r.to_dict() for r in res.report.repairs],
        "selection": oc.selection.to_dict()})
    ctx.manifest.set(outline={"path": _rel(ctx, op.outline), "summary": _rel(ctx, op.summary), "outline_hash": h, "md_hash": h,
                              "prompt": prompt_info(), "model": res.model or resolve_model(ep)})
    ctx.manifest.save()
    summary: dict[str, Any] = {
        **base, "model": res.model or None, "scenes": len(res.outline.scenes), "planned_seconds": res.outline.total_seconds(),
        "target_seconds": float(cfg.project.target_duration_seconds), "hook": res.outline.hook.used, "requests": res.requests,
        "response_cache_hits": res.cache_hits, "input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens,
        "repairs": len(res.report.repairs), "warnings": len(res.report.warnings), "missing_coverage": len(res.outline.missing_coverage),
        "uncertainties": len(res.outline.uncertainties), "outline_hash": h, "outline": _rel(ctx, op.outline), "outline_summary": _rel(ctx, op.summary)}
    if backup:
        summary["edited_outline_backup"] = backup
    return StageResult(Status.SUCCEEDED, summary)


# ---- approval ------------------------------------------------------------------------------------------------

def outline_approval_record(manifest: Any) -> dict[str, Any] | None:
    ap = manifest.get("outline_approval")
    return ap if isinstance(ap, dict) else None


def approve_current_outline(manifest: Any, outline_hash: str, mode: str) -> dict[str, Any]:
    ap = {"status": "approved", "mode": mode, "outline_hash": outline_hash, "approved_at": utc_now_iso()}
    manifest.set(outline_approval=ap)
    manifest.save()
    return ap


def outline_approval_stage(ctx: Any) -> StageResult:
    op, m = outline_paths(ctx), ctx.manifest
    outline, report, oc = check_outline_file(ctx)
    if outline is None or not report.ok:
        atomic_write_json(op.validation, report.to_dict())
        return StageResult(Status.FAILED, {"errors": [i.to_dict() for i in report.errors[:20]], "report": _rel(ctx, op.validation)},
                           "story outline is not valid (edit plans/story_outline.json, then `trip-vlog resume`):\n" + report.format())
    cur = ol.outline_hash(outline)
    rec = m.get("outline") or {}
    if rec.get("md_hash") != cur:  # the user edited the outline: refresh the readable summary
        atomic_write_text(op.summary, _render(ctx, outline, oc, report, cur))
        m.set(outline={**rec, "md_hash": cur})
        m.save()
    if ctx.config.workflow.approval == "auto_approve":
        approve_current_outline(m, cur, "automatic")
        return StageResult(Status.SUCCEEDED, {"mode": "automatic", "outline_hash": cur})
    ap = outline_approval_record(m)
    if ap and ap.get("status") == "approved" and ap.get("outline_hash") == cur:
        return StageResult(Status.SUCCEEDED, {"mode": ap.get("mode"), "outline_hash": cur})
    summary: dict[str, Any] = {"outline_hash": cur, "outline_summary": _rel(ctx, op.summary), "outline": _rel(ctx, op.outline),
                               "warnings": len(report.warnings)}
    if ap and ap.get("status") == "approved":
        summary["invalidated"] = f"outline changed since approval (approved {str(ap.get('outline_hash'))[:12]}, now {cur[:12]})"
        m.set(outline_approval={"status": "invalidated", "previous_outline_hash": ap.get("outline_hash"), "outline_hash": cur, "at": utc_now_iso()})
    else:
        m.set(outline_approval={"status": "pending", "outline_hash": cur, "at": utc_now_iso()})
    m.save()
    return StageResult(Status.AWAITING_APPROVAL, summary)


def register_outline_stages(runner: Any, *, after: str) -> str:
    """Add `outline` and `outline_approval` after stage `after` (normally "analyze"). Returns the stage later stages depend on."""
    runner.register("outline", outline_stage, deps=(after,), inputs=outline_inputs, verify=lambda c: outline_paths(c).outline.is_file())
    runner.register("outline_approval", outline_approval_stage, deps=("outline",), always_run=True)
    return "outline_approval"


# ---- dry run -------------------------------------------------------------------------------------------------

def dry_run_report(ctx: Any) -> dict[str, Any]:
    """What the outline request would contain and where it would go; nothing is sent."""
    cfg = ctx.config
    oc = build_outline_catalog(ctx.catalog, cfg)
    profile, ep = cfg.llm.endpoint_for("text")
    text = user_text(cfg, oc)
    prev = ctx.manifest.stage("outline")
    try:
        cached = prev.get("status") == "succeeded" and prev.get("input_hash") == stable_hash(outline_inputs(ctx)) and outline_paths(ctx).outline.is_file()
    except Exception:  # noqa: BLE001
        cached = False
    s = oc.selection
    return {"endpoint": profile, "where": "local" if ep.is_local else "remote", "model": resolve_model(ep), "eligible": s.eligible,
            "excluded": s.excluded_by_must_exclude, "moments_in_prompt": len(oc.moments), "left_out": s.dropped_total, "limit": s.limit,
            "left_out_by_reason": dict(sorted(s.dropped_by_reason.items())), "unlabeled": sum(1 for m in oc.moments if not m.labeled),
            "with_speech": sum(1 for m in oc.moments if m.transcript), "input_chars": len(text), "estimated_input_tokens": estimate_input_tokens(cfg, oc),
            "max_output_tokens": max_tokens(cfg), "unmatched_must_include": s.unmatched_must_include, "already_outlined": cached,
            "target_seconds": float(cfg.project.target_duration_seconds)}


def format_dry_run(rep: dict[str, Any]) -> str:
    lines = [
        f"outline dry run (nothing is sent): endpoint '{rep['endpoint']}' ({rep['where']}), model {rep['model'] or '(not configured)'}",
        f"  usable moments        : {rep['eligible']}" + (f" ({rep['excluded']} removed by must_exclude)" if rep["excluded"] else ""),
        f"  moments in the prompt : {rep['moments_in_prompt']} (limit planning.max_moments_in_prompt = {rep['limit']})"
        + (f"; {rep['left_out']} left out ({', '.join(f'{n} {w.replace(chr(95), chr(32))}' for w, n in rep['left_out_by_reason'].items())})" if rep["left_out"] else ""),
        f"  without description  : {rep['unlabeled']} (run `trip-vlog analyze` first for useful descriptions)" if rep["unlabeled"] else
        "  without description  : 0",
        f"  with speech excerpt   : {rep['with_speech']}",
        f"  estimated input       : about {rep['estimated_input_tokens']} tokens ({rep['input_chars']} characters); reply up to {rep['max_output_tokens']} tokens",
        f"  target duration       : {rep['target_seconds']:g} s",
        "  result cached         : " + ("yes, the outline is up to date; a run makes no request" if rep["already_outlined"] else "no, a run sends one request (a second only if the first fails validation)")]
    if rep["where"] == "remote" and rep["moments_in_prompt"]:
        lines.append("  note: descriptions, capture times and speech excerpts (no images, no GPS) would leave this machine for the remote endpoint")
    if rep["unmatched_must_include"]:
        lines.append("  warning: must_include matches nothing: " + ", ".join(rep["unmatched_must_include"]))
    if not rep["model"]:
        lines.append("  warning: no model configured (llm.endpoints.<name>.model or its model_env variable)")
    return "\n".join(lines)
