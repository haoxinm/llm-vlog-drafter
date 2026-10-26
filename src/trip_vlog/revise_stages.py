"""Phase 6 stage `revise` (DESIGN §16): a natural-language request changes the approved draft as a new plan version.

Where it runs: after `qc`, before the critic and the handoff, only when ``trip-vlog revise`` asked for it (``ctx.data["revise"]``) or a proposed revision was
approved (``trip-vlog approve --revision``, then ``resume``).

  1. A model maps the request onto the closed operations of ``planning.plan_diff`` (``planning.revisions``; it sees a compact plan summary and never edits
     plan JSON). Code checks every operation: ids exist, the plan stays valid, the length stays within ``revise.max_duration_change_pct``, exclusions hold.
  2. Code decides whether the revision may be applied on its own (``planning.revisions.decide``): a question the model asked, a removed scene or many
     removed clips, a big change of length or removed must_include content (thresholds in ``revise.*``) make it *destructive or ambiguous*; so do
     ``workflow.plan_review: true`` and ``revise.auto_apply: false``. Such a revision is only PROPOSED: ``plans/revisions/revise_rNN.json`` / ``.md``
     (status ``proposed``), recorded in ``manifest.revise.pending``; the stage stops (``awaiting_approval``, exit code 3). ``trip-vlog approve --revision``
     binds the approval to that proposal and the plan it was made for; ``resume`` (or ``revise --apply``) then applies exactly those operations, and
     nothing is applied when the plan changed since.
  3. Applying is a *round* of ``revision_common`` (the machinery the critic uses too): a NEW plan version (``plans/edit_plan.json`` is replaced, the parent
     is kept in ``plans/revisions/``), approval mode ``revision`` bound to the NEW plan hash with ``parent_plan_hash``, a NEW versioned FilmCraft project (an
     earlier ``_vNNN.fcproj`` is never touched), a re-rendered preview (the old one is kept as ``draft.before_revise_rNN.mp4``) and a re-run QC. QC is not
     reverted when it gets worse; the summary says so. A crash or Ctrl-C leaves ``revise.applying``; the next run puts the parent files back.

Rules a revision changes on purpose. A revision may make the film shorter or longer: when the new total is outside the tolerance of the target, the
target becomes the new total, and must_include references whose clips a confirmed revision removed are waived. Both are kept in
``manifest.revise.overrides`` (tied to the plan stage's plan, so a re-planned film starts from ``project.yaml`` again), never in ``project.yaml``.

Cache key (``revise_inputs``): request text hash, plan hash, the whole request text sent (plan, alternatives, limits), prompt and schema versions,
endpoint / model / parameters / mode, the ``revise.*`` settings, the validation and planning settings (with the revision overrides), and the pending
proposal. Unchanged inputs make no request (the stage is a cache hit, or the gateway's response cache answers); a reply that produced nothing usable
is dropped from the response cache so the same request asks again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trip_vlog import draft_stages as ds
from trip_vlog import revision_common as rc
from trip_vlog.llm.factory import make_gateway, resolve_model
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.types import LLMError, LLMResponse
from trip_vlog.logging_utils import Status, utc_now_iso
from trip_vlog.planning import revisions as rev
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan
from trip_vlog.planning.plan_diff import REVISION_OPERATIONS, ApplyResult, PlanBaseError, PlanContext, PlanOp, RefusedOp, load_selection_file
from trip_vlog.planning.resolve_plan import effective_plan_settings
from trip_vlog.planning.validate import ValidationSettings
from trip_vlog.state import StageResult, atomic_write_json, atomic_write_text, stable_hash

KIND = rc.REVISE
HOW_TO_APPROVE = "trip-vlog approve --revision --output {ws}, then trip-vlog resume --output {ws}"

# Test hook: replace to inject a gateway built on a fake transport (`lambda ctx: make_gateway(..., "revise", transport=fake)`).
gateway_factory: Callable[[Any], Gateway] | None = None


# ---- state ------------------------------------------------------------------------------------------------------------------

def state_of(ctx: Any) -> dict[str, Any]:
    return rc.state_of(ctx, KIND)


def _save(ctx: Any, **kw: Any) -> dict[str, Any]:
    return rc.save_state(ctx, KIND, **kw)


def pending_of(ctx: Any) -> dict[str, Any]:
    return state_of(ctx).get("pending") or {}


def pending_approved(ctx: Any) -> bool:
    """A proposed revision was approved (`trip-vlog approve --revision`) for the plan that is on disk now."""
    pend = pending_of(ctx)
    if pend.get("status") != "approved":
        return False
    try:
        return ds.plan_file_hash(ds._pp(ctx).edit_plan) == pend.get("parent_plan_hash")
    except (OSError, ValueError):
        return False


def requested(ctx: Any) -> dict[str, Any]:
    r = ctx.data.get("revise")
    return r if isinstance(r, dict) else {}


def revise_wanted(ctx: Any) -> bool:
    """Whether the draft pipeline registers the stage: `trip-vlog revise` asked for it, or an approved proposal waits to be applied."""
    return bool(requested(ctx)) or pending_approved(ctx)


def recover_interrupted(ctx: Any) -> str | None:
    return rc.recover_interrupted(ctx, KIND)


# ---- inputs -----------------------------------------------------------------------------------------------------------------

@dataclass
class Setup:
    prep: Any
    parent: EditPlan
    plan_ctx: PlanContext
    base_selection: Any
    catalog: PlanningCatalog
    alternatives: dict[str, list[str]] | None
    notes: dict[str, str]
    qc: dict[str, Any]
    profile: str
    endpoint: Any
    max_tokens: int


def build_setup(ctx: Any) -> Setup:
    """The plan and rules a revision is made against, from the workspace as it is now. Raises RuntimeError with a plain message when it cannot start."""
    cfg = ctx.config
    try:
        prep = ds._prepare(ctx)
    except ds.PlanRejected as e:
        raise RuntimeError(f"the edit plan is not valid: {e}") from None
    held = ds.approval_record(ctx.manifest)
    if not held or held.get("status") != "approved" or held.get("plan_hash") != prep.plan.plan_hash:
        raise RuntimeError("the current edit plan is not approved; approve it (trip-vlog approve) and let the draft finish before revising it")
    if ds.latest_run(ctx) is None:
        raise RuntimeError("there is no FilmCraft project of this plan yet; finish the draft first (trip-vlog resume)")
    qc: dict[str, Any] = {}
    try:
        qc = json.loads(ds._qc_paths(ctx)[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    alts, notes = rc.outline_alternatives(ctx)
    catalog = PlanningCatalog.from_catalog(ctx.catalog)
    pc = PlanContext(catalog, ValidationSettings.effective(cfg), effective_plan_settings(cfg), alts, float(cfg.qc.min_still_seconds))
    profile, ep = cfg.llm.endpoint_for("revise")
    return Setup(prep, prep.plan, pc, load_selection_file(ds._pp(ctx).selection_plan), catalog, alts, notes, qc, profile, ep,
                 max(256, min(cfg.revise.max_tokens, ep.max_tokens)))


def _project(ctx: Any) -> dict[str, Any]:
    p = ctx.config.project
    return {"title": p.title, "target_duration_seconds": float(ctx.config.effective_target_seconds), "style": p.style, "audience": p.audience, "language": p.language}


def _inputs(ctx: Any, setup: Setup, request: str) -> rev.ReviseInputs:
    return rev.ReviseInputs(setup.parent, setup.catalog, _project(ctx), request, setup.alternatives, setup.notes, ctx.config.revise.max_ops)


def revise_inputs(ctx: Any) -> Any:
    cfg = ctx.config
    req = requested(ctx)
    text = rev.normalize_request(req.get("request") or "")
    base: dict[str, Any] = {"request": rev.request_hash(text) if text else None, "flags": [bool(req.get("dry_run")), bool(req.get("again")), bool(req.get("apply"))],
                            "revise": cfg.revise.model_dump(), "plan_review": cfg.workflow.plan_review, "overrides": cfg.revision_overrides,
                            "validation": stable_hash(ValidationSettings.effective(cfg).__dict__), "planning": stable_hash(effective_plan_settings(cfg).__dict__),
                            "min_still_seconds": float(cfg.qc.min_still_seconds)}
    try:
        setup = build_setup(ctx)
    except RuntimeError as e:
        return {**base, "unavailable": str(e)[:200]}
    pend = pending_of(ctx)
    out = {**base, "plan": setup.parent.plan_hash, "prompt": rev.prompt_info(), "schema": stable_hash(rev.response_schema()), "max_tokens": setup.max_tokens,
           "llm": rc.llm_key(cfg, setup.profile, setup.endpoint), "project": ds._project_sha(ctx),
           "pending": [pend.get("status"), pend.get("revision_hash")] if pend else None}
    if text:
        out["request_text"] = stable_hash(rev.user_text(_inputs(ctx, setup, text), cfg.revise))
    return out


# ---- documents ----------------------------------------------------------------------------------------------------------------

def _gateway(ctx: Any) -> Gateway:
    if gateway_factory is not None:
        return gateway_factory(ctx)
    return make_gateway(ctx.config, ctx.workspace.root, "revise", logger=ctx.logger)


def _op_key(op: PlanOp) -> str:
    return json.dumps({k: v for k, v in op.to_dict().items() if k != "reason"}, sort_keys=True)


@dataclass
class Draft:
    """One revision on its way: the request, what the model understood, the result of trying it, and the decision."""

    request: str
    request_hash: str
    parsed: rev.Parsed
    res: ApplyResult
    decision: rev.Decision
    target: float | None
    waived: list[str]
    refused: list[RefusedOp] = field(default_factory=list)
    resp: LLMResponse | None = None
    number: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def parts(self) -> dict[str, str]:
        return {_op_key(o): self.parsed.parts.get(i, "") for i, o in enumerate(self.parsed.ops)}


def _duration(d: Draft) -> dict[str, Any]:
    before, after = float(d.res.base_edit.total_duration_seconds), float(d.res.edit.total_duration_seconds)
    return {"before_seconds": round(before, 3), "after_seconds": round(after, 3), "delta_seconds": round(after - before, 3), "new_target_seconds": d.target}


def revision_doc(ctx: Any, setup: Setup, d: Draft, n: int, status: str, authorization: str | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    parts = d.parts
    cfg = ctx.config
    resp = d.resp
    return {"schema_version": rev.REPORT_SCHEMA, "kind": "revision", "revision": KIND.tag(n), "status": status, "generated_at": utc_now_iso(),
            "request": d.request, "request_hash": d.request_hash, "summary": d.parsed.summary,
            "parent_plan_hash": d.res.base_edit.plan_hash, "plan_hash": d.res.edit.plan_hash, "authorization": authorization,
            "ops_applied": [{**a.to_dict(), "request_part": parts.get(_op_key(a.op), "")} for a in d.res.applied],
            "ops_refused": [r.to_dict() for r in d.refused], "unsupported": d.parsed.unsupported, "questions": d.parsed.ambiguous,
            "confirm_reasons": d.decision.reasons_to_confirm, "destructive_reasons": d.decision.destructive,
            "duration": _duration(d), "must_include_waived": d.waived, "target_duration_seconds": d.target,
            "diff": d.res.diff.to_dict() if d.res.diff else None, "diff_lines": d.res.diff.lines() if d.res.diff else ["No change."],
            "warnings": d.res.warnings, "notes": [*setup_notes(setup), *d.parsed.notes, *d.notes],
            "endpoint": setup.profile, "local": setup.endpoint.is_local, "model": (resp.model if resp else None) or resolve_model(setup.endpoint),
            "prompt": rev.prompt_info(), "requests": 0 if resp is None or resp.cached else 1, "cache_hits": 1 if resp is not None and resp.cached else 0,
            "usage": resp.usage.to_dict() if resp is not None and not resp.cached else None,
            "next": HOW_TO_APPROVE.format(ws=ctx.workspace.root), **(extra or {})}


def setup_notes(setup: Setup) -> list[str]:
    return [] if setup.qc else ["there is no QC report for the current plan; QC before/after is not compared"]


def write_revision(ctx: Any, n: int, doc: dict[str, Any], res: ApplyResult | None = None) -> str:
    d = rc.revisions_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_json(d / f"{KIND.tag(n)}.json", doc)
    atomic_write_text(d / f"{KIND.tag(n)}.md", rev.render_markdown(doc))
    return rc.rel(ctx, d / f"{KIND.tag(n)}.json")


def _summary(ctx: Any, doc: dict[str, Any], rel: str | None, outcome: str, **extra: Any) -> dict[str, Any]:
    dur = doc.get("duration") or {}
    return {"outcome": outcome, "revision": rel, "revision_summary": rel[:-5] + ".md" if rel else None, "request": doc.get("request"),
            "summary": doc.get("summary"), "changes_applied": len(doc.get("ops_applied") or []), "changes_refused": len(doc.get("ops_refused") or []),
            "duration": dur, "questions": doc.get("questions") or [], "confirm_reasons": doc.get("confirm_reasons") or [],
            "unsupported": doc.get("unsupported") or [], "endpoint": doc.get("endpoint"), "local": doc.get("local"), "model": doc.get("model"),
            "requests": doc.get("requests", 0), "response_cache_hits": doc.get("cache_hits", 0),
            "input_tokens": (doc.get("usage") or {}).get("input_tokens", 0), "output_tokens": (doc.get("usage") or {}).get("output_tokens", 0),
            "plan_hash": doc.get("plan_hash"), "parent_plan_hash": doc.get("parent_plan_hash"), "diff_lines": doc.get("diff_lines") or [],
            "refused": [{"op": r.get("op"), "message": r.get("message")} for r in doc.get("ops_refused") or []], **extra}


# ---- the stage --------------------------------------------------------------------------------------------------------------

def revise_stage(ctx: Any) -> StageResult:
    try:
        return _revise_stage(ctx)
    except PlanBaseError as e:
        return StageResult(Status.FAILED, {"outcome": "failed"}, f"no change can be made to this plan: {e}")
    except Exception as e:  # noqa: BLE001 - an explicit request that could not be carried out is reported, never half-applied
        ctx.logger.event("revise", Status.FAILED, error=f"{type(e).__name__}: {e}")
        return StageResult(Status.FAILED, {"outcome": "failed"}, f"{type(e).__name__}: {e}")


def _revise_stage(ctx: Any) -> StageResult:
    recovered = recover_interrupted(ctx) or ctx.data.pop(KIND.recovered_key, None)
    req = requested(ctx)
    text = rev.normalize_request(req.get("request") or "")
    pend = pending_of(ctx)
    if not text and pend.get("status") == "approved" and not pending_approved(ctx):
        return _stale(ctx, pend, "the edit plan changed since the revision was approved")
    try:
        setup = build_setup(ctx)
    except RuntimeError as e:
        return StageResult(Status.FAILED, {"outcome": "failed"}, str(e))
    if not text:
        if pending_approved(ctx):
            return _apply_pending(ctx, setup, pend, recovered)
        if req.get("apply"):
            if pend.get("status") == "pending":
                return StageResult(Status.FAILED, {"outcome": "not_approved", "revision": pend.get("revision")},
                                   f"revision {pend.get('revision')} is not approved yet: {HOW_TO_APPROVE.format(ws=ctx.workspace.root)}")
            return StageResult(Status.FAILED, {"outcome": "nothing_pending"}, "no proposed revision is waiting; run `trip-vlog revise \"...\"` first")
        return StageResult(Status.SKIPPED, {"skipped": True, "reason": "nothing was requested"})
    return _from_request(ctx, setup, text, req, recovered)


def _guard_repeat(ctx: Any, setup: Setup, request_hash: str) -> dict[str, Any] | None:
    """The round that produced the current plan, when it answered this very request (a repeated command must not apply the same change twice)."""
    done = rc.lineage(state_of(ctx), setup.parent.plan_hash)
    last = done[-1] if done else None
    return last if last and last.get("request_hash") == request_hash else None


def _from_request(ctx: Any, setup: Setup, text: str, req: dict[str, Any], recovered: str | None) -> StageResult:
    cfg = ctx.config
    r = cfg.revise
    if len(text) > r.max_request_chars:
        return StageResult(Status.FAILED, {"outcome": "failed"}, f"the request has {len(text)} characters; the limit is revise.max_request_chars ({r.max_request_chars}): "
                           "ask for one change at a time")
    rhash = rev.request_hash(text)
    dry = bool(req.get("dry_run"))
    again = _guard_repeat(ctx, setup, rhash)
    if again is not None and not req.get("again") and not dry:
        return StageResult(Status.SKIPPED, {"skipped": True, "outcome": "already_applied", "revision": again.get("revision"), "request": text,
                                           "reason": f"this request produced the current plan ({KIND.tag(int(again['round']))}); it is not applied twice (add --again to apply it again)"})
    gw = _gateway(ctx)
    try:
        resp = gw.complete(rev.build_request(_inputs(ctx, setup, text), r, setup.max_tokens))
    except LLMError as e:
        return StageResult(Status.FAILED, {"outcome": "failed", "request": text}, f"the revision model could not be reached or its reply was unusable "
                           f"({type(e).__name__}: {e}); nothing was changed, run the command again")
    parsed = rev.parse_reply(resp.parsed, setup.parent, setup.catalog, r.max_ops)
    res = rev.apply_revision(setup.parent, parsed.ops, setup.plan_ctx, r, base_selection=setup.base_selection)
    refused = [*parsed.rejected, *res.refused]
    must = list(cfg.effective_must_include)
    target = None
    if res.applied:
        target = rev.new_target(setup.parent, res, ValidationSettings.effective(cfg))
        if target is not None:       # the plan records the target it was made for: resolve once more with it (the timeline is the same)
            res = rev.apply_revision(setup.parent, [a.op for a in res.applied], setup.plan_ctx, r, base_selection=setup.base_selection, target=target)
    lost = rev.lost_required(setup.parent, res.edit, setup.catalog, must) if res.applied else []
    decision = rev.decide(parsed, setup.parent, res, lost, rev.Policy.from_config(cfg), must)
    d = Draft(text, rhash, parsed, res, decision, target, lost, refused, resp)
    if recovered:
        d.notes.append(recovered)
    if not res.applied:
        if not (parsed.ambiguous or parsed.unsupported):
            gw.discard([resp.fingerprint])      # nothing usable came out of this reply: the same request must ask again, not reuse it
        return _nothing(ctx, setup, d, dry)
    if dry:
        doc = revision_doc(ctx, setup, d, 0, "dry_run", None)
        return StageResult(Status.SUCCEEDED, _summary(ctx, doc, None, "dry_run", lines=rev.render_markdown(doc).splitlines(),
                                                      would_need_confirmation=decision.needs_confirmation))
    if decision.needs_confirmation:
        return _propose(ctx, setup, d)
    return _apply(ctx, setup, d, "automatic", None)


def _nothing(ctx: Any, setup: Setup, d: Draft, dry: bool) -> StageResult:
    """No operation could be applied. With questions the person is asked (exit 3); otherwise it failed (exit 1). Neither changes the plan."""
    status = "needs_clarification" if d.parsed.ambiguous else "no_change"
    doc = revision_doc(ctx, setup, d, 0, "dry_run" if dry else status, None)
    if dry:
        doc["status"] = "dry_run"
    rel = None
    if not dry and d.parsed.ambiguous:
        n = rc.next_number(ctx, KIND)
        doc["revision"] = KIND.tag(n)
        rel = write_revision(ctx, n, doc)
    lines = rev.render_markdown(doc).splitlines()
    if d.parsed.ambiguous:
        return StageResult(Status.AWAITING_APPROVAL if not dry else Status.SUCCEEDED,
                           _summary(ctx, doc, rel, "needs_clarification", lines=lines, clarification=True))
    why = "; ".join(f"{r.message}" for r in d.refused[:3]) or "the model proposed no operation" + (f" ({d.parsed.unsupported[0]['reason']})" if d.parsed.unsupported else "")
    return StageResult(Status.SUCCEEDED if dry else Status.FAILED, _summary(ctx, doc, None, "no_change", lines=lines), f"no change could be made: {why}")


def _propose(ctx: Any, setup: Setup, d: Draft) -> StageResult:
    pend = pending_of(ctx)
    rh = rev.revision_hash(d.res.base_edit.plan_hash, [a.op for a in d.res.applied], d.target, d.waived)
    n = int(pend["number"]) if pend.get("revision_hash") == rh and pend.get("number") else rc.next_number(ctx, KIND)
    if pend and pend.get("revision_hash") != rh:
        _supersede(ctx, pend)
    doc = revision_doc(ctx, setup, d, n, "proposed", None)
    rel = write_revision(ctx, n, doc)
    _save(ctx, pending={"status": "pending", "number": n, "revision": rel, "revision_hash": rh, "parent_plan_hash": d.res.base_edit.plan_hash,
                        "plan_hash": d.res.edit.plan_hash, "request_hash": d.request_hash, "request": d.request,
                        "ops": [a.op.to_dict() for a in d.res.applied], "target_duration_seconds": d.target, "must_include_waived": d.waived,
                        "reasons": d.decision.reasons_to_confirm, "proposed_at": utc_now_iso()})
    return StageResult(Status.AWAITING_APPROVAL, _summary(ctx, doc, rel, "proposed", lines=rev.render_markdown(doc).splitlines()))


def _supersede(ctx: Any, pend: dict[str, Any]) -> None:
    """A newer request replaces an open proposal: its files stay, marked as superseded."""
    try:
        n = int(pend["number"])
        p = rc.revisions_dir(ctx) / f"{KIND.tag(n)}.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("status") == "proposed":
            doc.update(status="superseded", note="replaced by a newer revision request before it was approved")
            write_revision(ctx, n, doc)
    except (OSError, ValueError, KeyError):
        pass


def _apply_pending(ctx: Any, setup: Setup, pend: dict[str, Any], recovered: str | None) -> StageResult:
    """Apply exactly the approved proposal: the same operations on the same plan must give the same new plan, else nothing is applied."""
    r = ctx.config.revise
    try:
        ops = [PlanOp.from_dict(o, REVISION_OPERATIONS) for o in pend.get("ops") or []]
    except Exception as e:  # noqa: BLE001
        return _stale(ctx, pend, f"the approved proposal cannot be read ({e})")
    res = rev.apply_revision(setup.parent, ops, setup.plan_ctx, r, base_selection=setup.base_selection, target=pend.get("target_duration_seconds"))
    if res.refused or len(res.applied) != len(ops) or res.edit.plan_hash != pend.get("plan_hash"):
        why = "; ".join(x.message for x in res.refused[:2]) or "it no longer produces the plan that was approved (the catalog or the settings changed)"
        return _stale(ctx, pend, f"the approved proposal cannot be applied as approved: {why}")
    saved = _proposal_doc(ctx, pend)
    parsed = rev.Parsed(str(saved.get("summary") or ""), ops, [], list(saved.get("questions") or []), list(saved.get("unsupported") or []), False, "")
    decision = rev.Decision([q["question"] for q in parsed.ambiguous], list(saved.get("destructive_reasons") or []), list(saved.get("confirm_reasons") or pend.get("reasons") or []))
    d = Draft(str(pend.get("request") or ""), str(pend.get("request_hash") or ""), parsed, res, decision, pend.get("target_duration_seconds"),
              list(pend.get("must_include_waived") or []), [RefusedOp(None, "refused", x["message"], x.get("op")) for x in saved.get("ops_refused") or []], None, int(pend["number"]))
    parts = {_op_key(PlanOp.from_dict(a["op"], REVISION_OPERATIONS)): a.get("request_part", "") for a in saved.get("ops_applied") or []}
    parsed.parts = {i: parts.get(_op_key(o), "") for i, o in enumerate(ops)}
    d.notes.append("applied exactly as proposed and approved")
    return _apply(ctx, setup, d, "manual", int(pend["number"]), saved=saved)


def _proposal_doc(ctx: Any, pend: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads((ctx.workspace.root / str(pend.get("revision"))).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _stale(ctx: Any, pend: dict[str, Any], why: str) -> StageResult:
    _save(ctx, pending={**pend, "status": "stale", "stale_reason": why})
    return StageResult(Status.FAILED, {"outcome": "stale", "revision": pend.get("revision")}, why + "; nothing was changed. Run `trip-vlog revise` again.")


def _apply(ctx: Any, setup: Setup, d: Draft, authorization: str, number: int | None, saved: dict[str, Any] | None = None) -> StageResult:
    cfg = ctx.config
    old_ov = cfg.revision_overrides
    cur = rc.manifest_overrides(ctx.manifest) or {}
    new_ov = {"base_plan_hash": (ctx.manifest.get("plan") or {}).get("plan_hash"),
              "target_duration_seconds": d.target or cur.get("target_duration_seconds"),
              "must_include_waived": sorted(set(cur.get("must_include_waived") or []) | set(d.waived))}
    new_ov = {k: v for k, v in new_ov.items() if v not in (None, [])} | {"base_plan_hash": new_ov["base_plan_hash"]}
    n = number or rc.next_number(ctx, KIND)
    d.number = n
    parent = setup.parent
    rh = rev.revision_hash(parent.plan_hash, [a.op for a in d.res.applied], d.target, d.waived)

    def update(rec: dict[str, Any]) -> dict[str, Any]:
        return {"overrides": new_ov, "last": {"revision": rec.get("revision"), "request_hash": d.request_hash}}

    hooks = rc.RoundHooks(
        revision_doc=lambda status, snap_rel, extra: revision_doc(ctx, setup, d, n, status, authorization, extra),
        write_revision=lambda c, k, doc, r: write_revision(c, k, doc, r),
        plan_note=f"revision {KIND.tag(n)} (parent plan {{parent}}): {d.request[:120]!r}, see plans/revisions/{{tag}}.md",
        approval_extra=lambda snap_rel, snap_sha, rec: {"revision_request_hash": d.request_hash, "revision_hash": rh},
        changed_error="the edit plan on disk is not the one this revision was made for (it changed meanwhile); nothing was changed, run `trip-vlog revise` again",
        failed_marker=rh, rec_extra={"request": d.request, "request_hash": d.request_hash, "kind": "revision"}, ran_by="revise", state_update=update)
    cfg.set_revision_overrides(new_ov)       # the new plan is checked against the rules the revision changed (target length, waived requirements)
    try:
        rec = rc.apply_round(ctx, KIND, parent, d.res, number=n, authorization=authorization, qc_before=setup.qc, hooks=hooks)
    except BaseException:
        cfg.set_revision_overrides(old_ov)
        raise
    doc = revision_doc(ctx, setup, d, n, rec["status"], authorization)
    if rec["status"] != "applied":
        cfg.set_revision_overrides(old_ov)
        doc.update(status="failed", note=rec.get("error"))
        return StageResult(Status.FAILED, _summary(ctx, doc, rec.get("revision"), "failed", lines=rev.render_markdown(doc).splitlines()),
                           rec.get("error") or "the revision could not be applied")
    doc.update(project=rec.get("project"), project_version=rec.get("project_version"), qc_before=rec.get("qc_before"), qc_after=rec.get("qc_after"),
               qc_new_failures=rec.get("qc_new_failures"), halt_note=rec.get("halt_note"))
    summ = _summary(ctx, doc, rec.get("revision"), "applied", lines=rev.render_markdown(doc).splitlines(), project=rec.get("project"),
                    project_version=rec.get("project_version"), qc_before=rec.get("qc_before"), qc_after=rec.get("qc_after"),
                    qc_new_failures=rec.get("qc_new_failures") or [], halt=rec.get("halt"), halt_note=rec.get("halt_note"), authorization=authorization,
                    preview_before=rec.get("preview_before"))
    return StageResult(Status.PARTIAL if rec.get("halt") else Status.SUCCEEDED, summ, rec.get("halt_note"))


# ---- history ----------------------------------------------------------------------------------------------------------------

def history(manifest: Any) -> list[dict[str, Any]]:
    """The revisions of this workspace, oldest first, for `status`: applied rounds, and the proposal that waits."""
    st = manifest.get("revise") or {}
    out = [{"revision": r.get("revision"), "status": r.get("status"), "request": r.get("request"), "authorization": r.get("authorization"),
            "plan_hash": r.get("plan_hash"), "project": r.get("project"), "qc_before": r.get("qc_before"), "qc_after": r.get("qc_after"), "halt_note": r.get("halt_note")}
           for r in st.get("rounds", []) if r.get("status") == "applied"]
    pend = st.get("pending")
    if pend:
        out.append({"revision": pend.get("revision"), "status": pend.get("status"), "request": pend.get("request"), "authorization": None,
                    "plan_hash": pend.get("plan_hash"), "project": None, "qc_before": None, "qc_after": None, "halt_note": None})
    return out


def change_lineage(ctx: Any, plan_hash: str) -> list[dict[str, Any]]:
    """Every applied round (critic fixes and revisions, oldest first) that led to ``plan_hash``: the chain of parent hashes across both histories."""
    rounds = [{**r, "by": "revise"} for r in rc.applied_rounds(state_of(ctx))] + [{**r, "by": "critic"} for r in rc.applied_rounds(rc.state_of(ctx, rc.CRITIC))]
    rounds.sort(key=lambda r: str(r.get("at") or ""))
    out: list[dict[str, Any]] = []
    expect = plan_hash
    for r in reversed(rounds):
        if r.get("plan_hash") == expect and r.get("parent_plan_hash") != expect:
            out.append(r)
            expect = r.get("parent_plan_hash")
    return list(reversed(out))


def handoff_notes(ctx: Any, plan_hash: str, project_version: Any) -> list[str]:
    """Lines for the handoff report: the revisions that led to this plan (the critic's lines come from its own state)."""
    done = [r for r in change_lineage(ctx, plan_hash) if r["by"] == "revise"]
    notes: list[str] = []
    for a in done:
        if a.get("halt_note"):
            notes.append(f"WARNING: {a['halt_note']}")
    if done:
        notes.append(f"this is project version v{project_version}: {len(done)} natural-language revision(s) changed the plan after the first draft ("
                     + "; ".join(f"{Path(str(a['revision'])).stem} for {json.dumps(a.get('request', ''), ensure_ascii=False)}" for a in done if a.get("revision"))
                     + "); change summaries: plans/revisions/revise_rNN.md; the earlier project versions are kept in filmcraft/ and the parent plans in plans/revisions/")
    return notes
