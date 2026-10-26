"""Phase 5b stage `critic` (DESIGN §14 steps 6-8): the optional LLM review of the draft, and localized fixes from it.

Where it runs: after `qc`, before `handoff`, only when ``qc.critic`` is ``report`` or ``apply`` (``trip-vlog critic --mode ...`` sets it for one run).

  report  Reads the validated edit plan, the QC results, the audio measurements and the preview sheets (with the tile index), asks the critic
          (``qc/critic.py``), checks every finding against what was supplied, and writes ``qc/critic_report.json`` + ``.md``: findings, evidence,
          proposed changes and what each would do, and the diff of the changes that would be applied. It fills the ``critic`` block of the QC
          report. It never fails the draft: an unreachable or unusable critic gives a ``partial`` stage and a report that says so.
  apply   As report, then applies the proposals that validate and are local (at most ``qc.critic_max_ops_per_round``, findings of at least
          ``qc.critic_apply_min_severity``) with ``planning.plan_diff``: a NEW edit plan (``plans/edit_plan.json`` is replaced; the parent is kept
          in ``plans/revisions/``), the diff in ``plans/revisions/critic_rNN.json`` / ``.md``, a NEW versioned FilmCraft project (the earlier
          ``_vNNN.fcproj`` is never touched), a re-rendered preview and a re-run QC. Up to ``qc.critic_max_rounds`` rounds; afterwards the result is reviewed
          once more (report only) so the report always describes the latest version.

Approval semantics (``manifest.approval``, mode ``critic_fix``). A critic fix only ever starts from a plan that is approved (manually, automatically
or derived from the outline). The ``critic_fix`` approval is bound to the NEW plan's hash and records ``parent_plan_hash`` (the approved plan it came
from), ``revision`` (the revision file listing exactly the operations applied), ``critic_report`` (+ sha256 of the report snapshot ``qc/critic_report.rNN.json``
that motivated it) and ``authorization``: ``automatic`` (``workflow.plan_review: false``) or ``manual`` (the person ran ``trip-vlog approve --critic``).
It authorizes the listed validated operations and nothing else; a later edit of the plan changes the hash and invalidates it like any other approval.
With ``workflow.plan_review: true`` nothing is applied by itself: the stage writes the proposed revision (``status: proposed``), records it as pending
in ``manifest.critic.pending`` and stops (``awaiting_approval``); ``trip-vlog approve --critic`` then ``resume`` applies exactly that revision.

Safety rails of a round. The round limit counts the applied rounds of the lineage that ends in the current plan (an append-only list in the manifest)
and, separately, the rounds of this run; a change that would return the plan to a version an earlier round already produced or started from is not
applied. Nothing is reverted automatically when QC gets worse, but no further round runs when the new version has a failing check the old one did not
(or its preview or QC could not be produced); the report, the CLI and the handoff note say so. A rebuild that fails for a reason of the environment
(missing FilmCraft CLI, full disk, timeout) puts the previous plan back and is retried by a later `resume`; only a plan that is rejected is remembered
as failed (`trip-vlog critic` clears that list). A round writes a marker (``critic.applying``) before it touches any file; a crash or Ctrl-C leaves it
and the next run of the stage puts the saved parent files back (also deleting files that did not exist before).

Cache key (``critic_inputs``): the plan hash, the QC results digest, the preview sheet index digest, the whole request text (plan, QC, audio, tiles,
alternatives), prompt and schema versions, the images sent, endpoint / model / parameters / mode, the validation and planning settings, the minimum
still length, the failed-revision list, and the mode, round and operation limits plus the rounds already applied. Unchanged inputs make no request: the stage is a cache hit, or (after a `report` is wanted again) the gateway's response cache answers.
"""

from __future__ import annotations

import json
import shutil  # noqa: F401 - tests patch cs.shutil.copyfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trip_vlog import draft_stages as ds
from trip_vlog import revision_common as rc
from trip_vlog.llm.factory import make_gateway, resolve_model
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.types import LLMError, LLMResponse
from trip_vlog.logging_utils import Status, utc_now_iso
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan
from trip_vlog.planning.plan_diff import ApplyResult, PlanBaseError, PlanContext, load_selection_file, render_ops_markdown
from trip_vlog.planning.resolve_plan import effective_plan_settings
from trip_vlog.planning.validate import ValidationSettings
from trip_vlog.qc import critic as cr
from trip_vlog.qc.preview import PreviewSheetIndex, load_preview_sheet_index
from trip_vlog.qc.report import set_critic_block
from trip_vlog.state import StageResult, atomic_write_json, atomic_write_text, stable_hash

CRITIC_JSON = "critic_report.json"
CRITIC_MD = "critic_report.md"
REVISIONS = "revisions"

# Test hook: replace to inject a gateway built on a fake transport (`lambda ctx: make_gateway(..., "critic", transport=fake)`).
gateway_factory: Callable[[Any], Gateway] | None = None


# ---- paths and state --------------------------------------------------------------------------------------------------

def _qc_dir(ctx: Any) -> Path:
    return rc.qc_dir(ctx)


def report_paths(ctx: Any) -> tuple[Path, Path]:
    d = _qc_dir(ctx)
    return d / CRITIC_JSON, d / CRITIC_MD


def revisions_dir(ctx: Any) -> Path:
    return rc.revisions_dir(ctx)


def _rel(ctx: Any, p: Path) -> str:
    return ds._rel(ctx, p)


def state_of(ctx: Any) -> dict[str, Any]:
    return rc.state_of(ctx, rc.CRITIC)


def _save_state(ctx: Any, **kw: Any) -> dict[str, Any]:
    return rc.save_state(ctx, rc.CRITIC, **kw)


applied_rounds = rc.applied_rounds
lineage = rc.lineage
seen_hashes = rc.seen_hashes


def pending_approved(ctx: Any) -> bool:
    """A critic revision was approved (`trip-vlog approve --critic`) for the plan that is on disk now."""
    pend = state_of(ctx).get("pending") or {}
    if pend.get("status") != "approved":
        return False
    try:
        return ds.plan_file_hash(ds._pp(ctx).edit_plan) == pend.get("parent_plan_hash")
    except (OSError, ValueError):
        return False


def effective_mode(ctx: Any) -> str:
    """qc.critic, except that an approved revision is applied whatever the configuration says now (it was approved to be applied)."""
    return "apply" if pending_approved(ctx) else ctx.config.qc.critic


def _loop_mode(ctx: Any) -> str:
    m = effective_mode(ctx)
    return "report" if m == "off" else m


def critic_wanted(ctx: Any) -> bool:
    """Whether the draft pipeline registers the critic stage: qc.critic is not off, or an approved revision waits to be applied."""
    return effective_mode(ctx) != "off"


def halt_text(rec: dict[str, Any]) -> str | None:
    """Why no further round ran after this round (None when nothing stopped them)."""
    return rc.halt_text(rec, rc.CRITIC)


def next_revision_number(ctx: Any) -> int:
    return rc.next_number(ctx, rc.CRITIC)


# ---- inputs -------------------------------------------------------------------------------------------------------------

@dataclass
class Aux:
    prep: Any
    plan_ctx: PlanContext
    base_selection: Any
    profile: str
    endpoint: Any
    images_reason: str | None
    max_tokens: int
    notes: list[str] = field(default_factory=list)


_outline_alternatives = rc.outline_alternatives


def build_inputs(ctx: Any) -> tuple[cr.CriticInputs, Aux]:
    """The critic's inputs from the workspace as it is now. Raises RuntimeError with a plain message when there is nothing to review."""
    cfg = ctx.config
    q = cfg.qc
    try:
        prep = ds._prepare(ctx)
    except ds.PlanRejected as e:
        raise RuntimeError(f"the edit plan is not valid: {e}") from None
    qc_path = ds._qc_paths(ctx)[0]
    try:
        qc = json.loads(qc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError(f"there is no readable QC report at {_rel(ctx, qc_path)}; the critic reads its results") from None
    plan: EditPlan = prep.plan
    index: PreviewSheetIndex | None = None
    notes: list[str] = []
    ip = ds._qc_index_path(ctx)
    if ip.is_file():
        try:
            index = load_preview_sheet_index(ip)
            if index.plan_hash != plan.plan_hash:
                notes.append("the preview sheet index belongs to a different plan; it was not used")
                index = None
        except (OSError, ValueError, KeyError) as e:
            notes.append(f"the preview sheet index could not be read ({type(e).__name__}); it was not used")
    sheets: dict[int, Path] = {}
    if index is not None:
        for s in index.sheets:
            p = ctx.workspace.root / s.path if s.path else None
            if p is not None and p.is_file():
                sheets[s.number] = p
    profile, ep = cfg.llm.endpoint_for("critic", images=q.critic_images)
    reason = None
    if not q.critic_images:
        reason = "qc.critic_images is false"
    elif not ep.supports_images:
        reason = f"endpoint profile {profile!r} has supports_images: false"
    elif not sheets:
        reason = "no preview sheets exist for this plan (the preview was not rendered or ffmpeg is missing)"
    alts, alt_notes = _outline_alternatives(ctx)
    pc = PlanContext(PlanningCatalog.from_catalog(ctx.catalog), ValidationSettings.effective(cfg), effective_plan_settings(cfg), alts,
                     float(q.min_still_seconds))
    p = cfg.project
    inp = cr.CriticInputs(
        plan=plan, catalog=pc.catalog, qc=qc, index=index, sheet_paths=sheets,
        project={"title": p.title, "target_duration_seconds": float(cfg.effective_target_seconds), "style": p.style, "audience": p.audience, "language": p.language},
        alternatives=alts, alternative_notes=alt_notes, max_sheets=q.critic_max_sheets, images=reason is None)
    aux = Aux(prep, pc, load_selection_file(ds._pp(ctx).selection_plan), profile, ep, reason, max(256, min(q.critic_max_tokens, ep.max_tokens)), notes)
    return inp, aux


_llm_key = rc.llm_key


def critic_inputs(ctx: Any) -> Any:
    cfg = ctx.config
    q = cfg.qc
    base: dict[str, Any] = {"mode": effective_mode(ctx), "limits": [q.critic_max_rounds, q.critic_max_ops_per_round, q.critic_apply_min_severity],
                            "plan_review": cfg.workflow.plan_review, "validation": stable_hash(ValidationSettings.effective(cfg).__dict__),
                            "planning": stable_hash(effective_plan_settings(cfg).__dict__), "min_still_seconds": float(q.min_still_seconds),
                            "failed": sorted(state_of(ctx).get("failed", []))}
    try:
        inp, aux = build_inputs(ctx)
    except RuntimeError as e:
        return {**base, "unavailable": str(e)[:200]}
    shown = cr.shown_sheets(inp)
    st = state_of(ctx)
    pend = st.get("pending") or {}
    done = lineage(st, inp.plan.plan_hash)
    return {**base, "plan": inp.plan.plan_hash, "qc": cr.qc_digest(inp.qc), "index": cr.index_digest(inp.index), "images": shown,
            "request": stable_hash(cr.user_text(inp, len(shown))), "prompt": cr.prompt_info(), "schema": stable_hash(cr.response_schema()),
            "max_tokens": aux.max_tokens, "llm": _llm_key(cfg, aux.profile, aux.endpoint), "project": ds._project_sha(ctx),
            "rounds_done": len(done), "halted": (_halting_round(ctx, inp.plan.plan_hash, done) or {}).get("halt"), "pending": [pend.get("status"), pend.get("revision_hash")] if pend else None}


def critic_verify(ctx: Any) -> bool:
    """A cache hit also needs the report on disk and the QC report's critic block to be this plan's (a re-run QC writes `not_run` again)."""
    rj, rm = report_paths(ctx)
    if not rj.is_file() or not rm.is_file():
        return False
    try:
        rep = json.loads(rj.read_text(encoding="utf-8"))
        qc = json.loads(ds._qc_paths(ctx)[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    block = qc.get("critic") or {}
    return rep.get("plan_hash") == ds.plan_file_hash(ds._pp(ctx).edit_plan) and block.get("status") not in (None, "not_run") and block.get("plan_hash") == rep.get("plan_hash")


# ---- one review ---------------------------------------------------------------------------------------------------------

@dataclass
class Review:
    inp: cr.CriticInputs
    aux: Aux
    findings: list[cr.Finding]
    dropped: list[dict[str, Any]]
    notes: list[str]
    summary: str
    resp: LLMResponse
    shown: list[int]


def _gateway(ctx: Any) -> Gateway:
    if gateway_factory is not None:
        return gateway_factory(ctx)
    # the same profile `build_inputs` chose (llm.critic, else vision when images are configured, else text), also when no sheet exists this time
    return make_gateway(ctx.config, ctx.workspace.root, "critic", images=ctx.config.qc.critic_images, logger=ctx.logger)


def review(ctx: Any) -> Review:
    inp, aux = build_inputs(ctx)
    req, shown = cr.build_request(inp, aux.max_tokens)
    resp = _gateway(ctx).complete(req)
    space = cr.EvidenceSpace.from_inputs(inp)
    findings, dropped, notes = cr.parse_findings(resp.parsed, space)
    return Review(inp, aux, findings, dropped, aux.notes + notes, " ".join(str(resp.parsed.get("summary") or "").split())[:cr.MAX_TEXT], resp, shown)


def _assess(ctx: Any, rv: Review, mode: str) -> tuple[list[cr.Finding], ApplyResult | None]:
    """Judge the proposals of a review. A plan that cannot serve as a base marks every proposal invalid (with the reason) and changes nothing."""
    q = ctx.config.qc
    policy = cr.ProposalPolicy(mode, q.critic_apply_min_severity, q.critic_max_ops_per_round)
    try:
        selected, res = cr.assess_proposals(rv.findings, rv.inp.plan, rv.aux.plan_ctx, policy, base_selection=rv.aux.base_selection,
                                            space=cr.EvidenceSpace.from_inputs(rv.inp))
    except PlanBaseError as e:
        for f in rv.findings:
            if f.op is not None:
                f.change_status, f.change_note = "invalid", str(e)
        rv.notes.append(f"no change can be made to this plan: {e}")
        return [], None
    return selected, res


# ---- report -------------------------------------------------------------------------------------------------------------

def _report_doc(ctx: Any, rv: Review | None, *, mode: str, status: str, reason: str | None, selected: list[cr.Finding], res: ApplyResult | None,
                rounds: list[dict[str, Any]], error: str | None = None, aux: Aux | None = None, inp: cr.CriticInputs | None = None) -> dict[str, Any]:
    cfg = ctx.config
    inp = inp or (rv.inp if rv else None)
    aux = aux or (rv.aux if rv else None)
    profile = aux.profile if aux else cfg.llm.endpoint_for("critic", images=cfg.qc.critic_images)[0]
    ep = aux.endpoint if aux else cfg.llm.endpoint_for("critic", images=cfg.qc.critic_images)[1]
    findings = rv.findings if rv else []
    shown = rv.shown if rv else []
    doc: dict[str, Any] = {
        "schema_version": cr.REPORT_SCHEMA, "generated_at": utc_now_iso(), "status": status, "mode": mode, "reason": reason, "error": error,
        "plan_hash": inp.plan.plan_hash if inp else None, "qc_digest": cr.qc_digest(inp.qc) if inp else None, "sheet_index_digest": cr.index_digest(inp.index) if inp else None,
        "images": {"sent": bool(shown), "sheets": [_rel(ctx, inp.sheet_paths[n]) for n in shown] if inp else [],
                   "reason": None if shown else (aux.images_reason if aux else None) or "no sheet was available",
                   "tiles_with_image": len(cr.visible_tiles(inp)) if inp else 0},
        "endpoint": profile, "local": ep.is_local, "model": (rv.resp.model if rv else None) or resolve_model(ep), "prompt": cr.prompt_info(),
        "requests": 0 if rv is None or rv.resp.cached else 1, "cache_hits": 1 if rv is not None and rv.resp.cached else 0,
        "usage": rv.resp.usage.to_dict() if rv is not None and not rv.resp.cached else None,
        "summary": rv.summary if rv else "", "findings": [f.to_dict() for f in findings], "dropped_findings": rv.dropped if rv else [],
        "notes": list(rv.notes) if rv else [], "by_severity": cr.counts_by_severity(findings),
        "proposals_valid": sum(1 for f in findings if f.change_status in ("valid", "applied", "pending")),
        "changes_applied": sum(len(r.get("ops", [])) for r in rounds if r.get("status") == "applied"),
        "would_change": None, "rounds": rounds}
    if res is not None and res.applied:
        doc["would_change"] = {"findings": [f.id for f in selected], "ops": [a.to_dict() for a in res.applied], "refused": [r.to_dict() for r in res.refused],
                               "lines": [*render_ops_markdown(res.applied, res.refused), *(res.diff.lines() if res.diff else [])],
                               "diff": res.diff.to_dict() if res.diff else None}
    return doc


def _write_report(ctx: Any, doc: dict[str, Any]) -> str:
    rj, rm = report_paths(ctx)
    atomic_write_json(rj, doc)
    atomic_write_text(rm, cr.render_markdown(doc))
    cr.write_response_schema(_qc_dir(ctx))
    rel = _rel(ctx, rj)
    set_critic_block(ds._qc_paths(ctx)[0], cr.qc_block(doc, rel))
    return rel


# ---- applying a round ---------------------------------------------------------------------------------------------------

def _mark(ctx: Any, name: str, res: StageResult, inputs: Callable[[Any], Any]) -> None:
    rc.mark_stage(ctx, name, res, inputs, "critic")


_replace_from = rc.replace_from


def _revision_doc(ctx: Any, n: int, status: str, parent: EditPlan, res: ApplyResult, selected: list[cr.Finding], snapshot_rel: str | None,
                  authorization: str | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema_version": 1, "kind": "critic_fix", "revision": f"critic_r{n:02d}", "status": status, "generated_at": utc_now_iso(),
            "parent_plan_hash": parent.plan_hash, "plan_hash": res.edit.plan_hash, "critic_report": snapshot_rel, "authorization": authorization,
            "findings": [{"id": f.id, "severity": f.severity, "category": f.category, "summary": f.summary} for f in selected],
            "ops_applied": [a.to_dict() for a in res.applied], "ops_refused": [r.to_dict() for r in res.refused],
            "warnings": res.warnings, "diff": res.diff.to_dict() if res.diff else None, **(extra or {})}


def _revision_md(doc: dict[str, Any], res: ApplyResult) -> str:
    L = [f"# Critic revision {doc['revision']}: {doc['status']}", "",
         f"- Parent plan `{doc['parent_plan_hash'][:16]}` -> new plan `{doc['plan_hash'][:16]}`", f"- Critic report: `{doc.get('critic_report')}`",
         f"- Authorization: {doc.get('authorization') or 'none yet (proposed)'}", "", "## Changes", ""]
    L += render_ops_markdown(res.applied, res.refused) + [""]
    L += ["## What changes", ""] + [f"    {x}" for x in (res.diff.lines() if res.diff else ["No change."])] + [""]
    if res.warnings:
        L += ["## New warnings", ""] + [f"- {w}" for w in res.warnings] + [""]
    L += ["Earlier versions are kept: the parent plan is `" + doc["revision"] + ".parent_edit_plan.json` next to this file and the earlier FilmCraft "
          "project version stays in `filmcraft/`.", ""]
    return "\n".join(L)


def _write_revision(ctx: Any, n: int, doc: dict[str, Any], res: ApplyResult) -> str:
    d = revisions_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_json(d / f"critic_r{n:02d}.json", doc)
    atomic_write_text(d / f"critic_r{n:02d}.md", _revision_md(doc, res))
    return _rel(ctx, d / f"critic_r{n:02d}.json")


def _revision_hash(parent_hash: str, selected: list[cr.Finding]) -> str:
    return stable_hash({"parent": parent_hash, "ops": [f.op.to_dict() for f in selected if f.op]})


def _safely(fn: Callable[[Any], StageResult], ctx: Any) -> StageResult:
    return rc.safely(fn, ctx)


def _rollback(ctx: Any, saved: dict[str, Path | None], approval: Any, *, new_hash: str, parent_hash: str, new_selection: Path | None = None,
              new_plan: Path | None = None) -> str:
    return rc.rollback(ctx, saved, approval, new_hash=new_hash, parent_hash=parent_hash, new_selection=new_selection, new_plan=new_plan)


def recover_interrupted(ctx: Any) -> str | None:
    """A critic round that was started and never finished left `critic.applying`: see `revision_common.recover_interrupted`."""
    return rc.recover_interrupted(ctx, rc.CRITIC, lambda *a, **k: _rollback(*a, **k))


def _apply_round(ctx: Any, rv: Review, selected: list[cr.Finding], res: ApplyResult, report_doc: dict[str, Any], authorization: str,
                 number: int | None = None) -> dict[str, Any]:
    """Apply a critic round (`revision_common.apply_round`): new plan, `critic_fix` approval, NEW project version, preview, QC."""
    parent = rv.inp.plan
    n = number or rc.next_number(ctx, rc.CRITIC)
    snap = _qc_dir(ctx) / f"critic_report.r{n:02d}.json"
    hooks = rc.RoundHooks(
        revision_doc=lambda status, snap_rel, extra: _revision_doc(ctx, n, status, parent, res, selected, snap_rel, authorization, extra),
        write_revision=lambda c, k, doc, r: _write_revision(c, k, doc, r),
        plan_note=f"critic_fix {rc.CRITIC.tag(n)} (parent plan {{parent}}): localized changes from the critic, see plans/revisions/{{tag}}.md",
        approval_extra=lambda snap_rel, snap_sha, rec: {"critic_report": snap_rel, "critic_report_sha256": snap_sha},
        changed_error="the edit plan on disk is not the one that was reviewed (it changed meanwhile); nothing was changed, run the critic again",
        failed_marker=_revision_hash(parent.plan_hash, selected), rec_extra={"findings": [f.id for f in selected]}, snapshot=(snap, report_doc),
        rollback=lambda *a, **k: _rollback(*a, **k), ran_by="critic")
    return rc.apply_round(ctx, rc.CRITIC, parent, res, number=n, authorization=authorization, qc_before=rv.inp.qc, hooks=hooks)


# ---- the stage ----------------------------------------------------------------------------------------------------------

def _summary(ctx: Any, doc: dict[str, Any], rel: str) -> dict[str, Any]:
    run = ds.latest_run(ctx)
    last = next((r for r in reversed(doc["rounds"]) if r.get("status") == "applied"), {})
    return {"mode": doc["mode"], "status": doc["status"], "findings": len(doc["findings"]), "by_severity": doc["by_severity"], "dropped_findings": len(doc["dropped_findings"]),
            "proposals_valid": doc["proposals_valid"], "changes_applied": doc["changes_applied"], "rounds": len(doc["rounds"]),
            "images_sent": doc["images"]["sent"], "images_reason": doc["images"]["reason"], "endpoint": doc["endpoint"], "local": doc["local"], "model": doc["model"],
            "requests": doc["requests"], "response_cache_hits": doc["cache_hits"], "input_tokens": (doc["usage"] or {}).get("input_tokens", 0),
            "output_tokens": (doc["usage"] or {}).get("output_tokens", 0), "report": rel, "plan_hash": doc["plan_hash"],
            "project": run["project_path"] if run else None, "project_version": run["version"] if run else None, "reason": doc["reason"], "error": doc.get("error"),
            "halt": last.get("halt"), "halt_note": last.get("halt_note"), "qc_new_failures": last.get("qc_new_failures") or []}


def _write_report_safely(ctx: Any, doc: dict[str, Any]) -> str:
    try:
        return _write_report(ctx, doc)
    except OSError as e:   # the report is an extra: failing to write it never fails the draft
        ctx.logger.event("critic", Status.FAILED, error=f"could not write the critic report: {e}")
        return _rel(ctx, report_paths(ctx)[0])


def _settle_input_hash(ctx: Any) -> None:
    """The stage's inputs changed while it ran (a fix replaced the plan): record the hash of the state it ends in, so the next run is a cache hit."""
    try:
        h = stable_hash(critic_inputs(ctx))
    except Exception:  # noqa: BLE001
        return
    ctx.manifest.stages.setdefault("critic", {})["input_hash"] = h


def _authorize(ctx: Any, rv: Review, selected: list[cr.Finding], res: ApplyResult, mode: str,
               rounds_doc: list[dict[str, Any]]) -> tuple[str | None, int | None, StageResult | None]:
    """('automatic' | 'manual', revision number or None, None) when the round may run; (None, None, awaiting-approval result) when a person has to approve the revision first
    (workflow.plan_review): the revision is written as `proposed` and recorded as pending in manifest.critic.pending."""
    if not ctx.config.workflow.plan_review:
        return "automatic", None, None
    parent = rv.inp.plan
    rh = _revision_hash(parent.plan_hash, selected)
    pend = state_of(ctx).get("pending") or {}
    if pend.get("status") == "approved" and pend.get("revision_hash") == rh and pend.get("parent_plan_hash") == parent.plan_hash:
        return "manual", int(pend["number"]), None
    n = int(pend["number"]) if pend.get("revision_hash") == rh and pend.get("number") else next_revision_number(ctx)
    for f in selected:
        f.change_status, f.change_note = "pending", "waiting for your approval: trip-vlog approve --critic, then resume"
    doc = _report_doc(ctx, rv, mode=mode, status="ok", reason="workflow.plan_review is true: the proposed changes wait for your approval (trip-vlog approve --critic)",
                      selected=selected, res=res, rounds=rounds_doc)
    rel = _write_report_safely(ctx, doc)
    rpath = _write_revision(ctx, n, _revision_doc(ctx, n, "proposed", parent, res, selected, rel, None), res)
    _save_state(ctx, pending={"status": "pending", "number": n, "revision_hash": rh, "parent_plan_hash": parent.plan_hash, "revision": rpath, "report": rel,
                              "proposed_at": utc_now_iso()})
    return None, None, StageResult(Status.AWAITING_APPROVAL, {**_summary(ctx, doc, rel), "revision": rpath, "revision_summary": rpath[:-5] + ".md"})


def _halting_round(ctx: Any, plan_hash: str, done: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The latest applied round (a critic round or a natural-language revision) that led to ``plan_hash``: its `halt` says whether the critic may still change
    the plan on its own. A revision that made QC worse stops the critic from stacking automatic changes on the revised plan."""
    from trip_vlog.revise_stages import change_lineage

    chain = change_lineage(ctx, plan_hash)
    if chain and chain[-1].get("by") == "revise":
        return chain[-1]
    return done[-1] if done else None       # no revision on top: the critic's own lineage decides, exactly as before revisions existed


def critic_stage(ctx: Any) -> StageResult:
    try:
        return _critic_stage(ctx)
    except Exception as e:  # noqa: BLE001 - the critic is advisory: whatever goes wrong, the draft stands
        ctx.logger.event("critic", Status.FAILED, error=f"{type(e).__name__}: {e}")
        return StageResult(Status.PARTIAL, {"mode": effective_mode(ctx), "status": "error", "error": f"{type(e).__name__}: {e}"}, f"{type(e).__name__}: {e}")


def _critic_stage(ctx: Any) -> StageResult:
    cfg = ctx.config
    q = cfg.qc
    recovered = recover_interrupted(ctx) or ctx.data.pop("critic_recovered", None)      # normally done before the runner starts
    if effective_mode(ctx) == "off":
        return StageResult(Status.SKIPPED, {"skipped": True, "reason": "qc.critic is off"})
    requests = hits = applied_now = 0
    while True:
        st = state_of(ctx)
        mode = _loop_mode(ctx)      # an approved revision runs in apply mode once; afterwards the configured mode (off: one last review) applies again
        try:
            rv = review(ctx)
        except RuntimeError as e:   # nothing to review (no valid plan, no QC report)
            doc = _report_doc(ctx, None, mode=mode, status="error", reason=f"the critic did not run: {e}", selected=[], res=None, rounds=[], error=str(e))
            return StageResult(Status.PARTIAL, _summary(ctx, doc, _write_report_safely(ctx, doc)), str(e))
        except LLMError as e:
            msg = f"{type(e).__name__}: {e}"
            try:
                inp, aux = build_inputs(ctx)
            except RuntimeError:
                inp = aux = None
            doc = _report_doc(ctx, None, mode=mode, status="error", reason=f"the critic could not be reached or its reply was unusable ({type(e).__name__}); the draft is "
                              "unaffected and `resume` tries again", selected=[], res=None, rounds=lineage(st, inp.plan.plan_hash) if inp else [], error=msg, aux=aux, inp=inp)
            return StageResult(Status.PARTIAL, _summary(ctx, doc, _write_report_safely(ctx, doc)), msg)
        requests += 0 if rv.resp.cached else 1
        hits += int(rv.resp.cached)
        done = lineage(st, rv.inp.plan.plan_hash)
        last = _halting_round(ctx, rv.inp.plan.plan_hash, done)
        halted = last.get("halt") if last else None      # the latest round (the critic's or a revision) made QC worse or left no preview: report only, no more rounds
        rounds_left = len(done) < q.critic_max_rounds and applied_now < q.critic_max_rounds and not halted
        selected, res = _assess(ctx, rv, mode)
        if selected and _revision_hash(rv.inp.plan.plan_hash, selected) in state_of(ctx).get("failed", []):
            for f in selected:
                f.change_status, f.change_note = "refused", "building this exact revision failed earlier (see plans/revisions/); it is not tried again"
            selected, res = [], None
        elif selected and res is not None and res.edit.plan_hash in seen_hashes(state_of(ctx)) | {rv.inp.plan.plan_hash}:
            for f in selected:
                f.change_status, f.change_note = "refused", "this would return the plan to a version an earlier round already started from or produced; it is not applied again"
            selected, res = [], None
        pend = state_of(ctx).get("pending") or {}
        if pend.get("status") == "approved" and not (rounds_left and selected and res is not None and res.changed
                                                      and pend.get("revision_hash") == _revision_hash(rv.inp.plan.plan_hash, selected)):
            _save_state(ctx, pending=None)       # an approval for a revision this review no longer proposes is not carried over
            mode = _loop_mode(ctx)
        if mode == "apply" and rounds_left and selected and res is not None and res.changed:
            authorization, number, stop = _authorize(ctx, rv, selected, res, mode, done)
            if stop is not None:
                return stop
            snapshot = _report_doc(ctx, rv, mode=mode, status="ok", reason=f"review that motivated critic round {len(done) + 1}", selected=selected, res=res, rounds=done)
            rec = _apply_round(ctx, rv, selected, res, snapshot, authorization or "automatic", number)
            if rec["status"] == "applied":
                applied_now += 1
                if rec.get("halt") in ("preview_failed", "qc_failed"):     # there is nothing current to review: the preview or QC of this version is missing
                    doc = _report_doc(ctx, rv, mode=mode, status="ok", reason=rec["halt_note"], selected=[], res=None, rounds=[*done, rec])
                    doc["requests"], doc["cache_hits"] = requests, hits
                    return StageResult(Status.PARTIAL, _summary(ctx, doc, _write_report_safely(ctx, doc)), rec["halt_note"])
                continue      # review the new version; the response cache answers a repeat of the same request on the next run
            for f in selected:
                f.change_status, f.change_note = "refused", rec.get("error", "the rebuild failed")
            doc = _report_doc(ctx, rv, mode=mode, status="ok", reason=rec.get("error"), selected=[], res=None, rounds=[*done, rec])
            doc["requests"], doc["cache_hits"] = requests, hits
            return StageResult(Status.PARTIAL, _summary(ctx, doc, _write_report_safely(ctx, doc)), rec.get("error"))
        for f in selected:
            if f.change_status == "valid":
                f.change_note = (f.change_note + "; " if f.change_note else "") + ("would be applied with qc.critic: apply" if mode == "report" else "no round left")
        reason = (last.get("halt_note") if halted else
                  "the round limit (qc.critic_max_rounds) is reached; this is the review of the final version and changes nothing" if mode == "apply" and not rounds_left else None)
        doc = _report_doc(ctx, rv, mode=mode, status="ok", reason=reason, selected=selected, res=res, rounds=done)
        if recovered:
            doc["notes"].append(recovered)
        doc["requests"], doc["cache_hits"] = requests, hits
        rel = _write_report_safely(ctx, doc)
        _settle_input_hash(ctx)
        return StageResult(Status.SUCCEEDED, _summary(ctx, doc, rel))
