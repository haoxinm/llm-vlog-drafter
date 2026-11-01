"""What the QC critic's fixes (Phase 5b, ``critic_stages``) and natural-language revisions (Phase 6, ``revise_stages``) have in common:
a change becomes a NEW plan version, a NEW versioned FilmCraft project, a re-rendered preview and a re-run QC, and a crash at any point puts
the previous version back.

A *round* (one applied change set) does, in this order:

  1. keeps the parent: the plan files, the QC and mapping reports and the preview are copied to ``plans/revisions/<tag>.*`` (the preview next to
     itself as ``<name>.before_<tag>``); a file that did not exist is remembered as such;
  2. writes the marker ``<state_key>.applying`` BEFORE it touches a live file: a crash or Ctrl-C leaves it, and ``recover_interrupted`` (called by
     the orchestrator before any stage runs) puts the saved parent files back, byte-checked (``rollback``: only over what the round wrote; a plan the
     person edited meanwhile is kept);
  3. writes the revision (``<tag>.json`` / ``.md`` with the status ``applying``, the new edit and selection plan beside it), validates the new plan,
     writes the plan files, approves the new plan hash (the approval record binds the NEW hash and links the parent), builds a NEW project
     version (never touching the earlier ``_vNNN.fcproj``), renders the preview, re-runs QC;
  4. records the round in the append-only list ``<state_key>.rounds`` and clears the marker. QC is never reverted automatically; a round that made QC
     worse is reported (``halt``) and stops further automatic rounds.

A rebuild that fails for a reason of the environment puts the previous plan back and is retried by a later ``resume``; only a plan that is rejected
is remembered as failed (``<state_key>.failed``). Everything that differs between the two callers (state key, file prefix, approval mode,
documents, wording) is in ``RoundKind`` and ``RoundHooks``; the machinery is the same.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from trip_vlog import draft_stages as ds
from trip_vlog.filmcraft.execution import sha256_file
from trip_vlog.logging_utils import Status, utc_now_iso
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan, to_json_dict
from trip_vlog.planning.pipeline import _write_all
from trip_vlog.planning.plan_diff import ApplyResult, render_ops_markdown
from trip_vlog.planning.validate import ValidationSettings, validate_edit_plan
from trip_vlog.state import StageResult, atomic_write_json, atomic_write_text, stable_hash

REVISIONS = "revisions"


@dataclass(frozen=True)
class RoundKind:
    state_key: str          # manifest key with the history: "critic" | "revise"
    prefix: str             # revision file prefix, followed by the two-digit number: "critic_r" | "revise_r"
    approval_mode: str      # manifest.approval.mode of the plan a round produced: "critic_fix" | "revision"
    noun: str               # in messages: "critic round" | "revision"
    event: str              # stage name in the run log
    recovered_key: str      # ctx.data key that carries the recovery note to this run's report

    def tag(self, n: int) -> str:
        return f"{self.prefix}{n:02d}"


CRITIC = RoundKind("critic", "critic_r", "critic_fix", "critic round", "critic", "critic_recovered")
REVISE = RoundKind("revise", "revise_r", "revision", "revision", "revise", "revise_recovered")


# ---- paths and state --------------------------------------------------------------------------------------------------

def revisions_dir(ctx: Any) -> Path:
    return ds._pp(ctx).plans_dir / REVISIONS


def rel(ctx: Any, p: Path) -> str:
    return ds._rel(ctx, p)


def qc_dir(ctx: Any) -> Path:
    return Path(ctx.paths.outputs["qc_dir"])


def state_of(ctx: Any, kind: RoundKind) -> dict[str, Any]:
    st = ctx.manifest.get(kind.state_key)
    return st if isinstance(st, dict) else {}


def save_state(ctx: Any, kind: RoundKind, **kw: Any) -> dict[str, Any]:
    st = dict(state_of(ctx, kind))
    st.update(kw)
    ctx.manifest.set(**{kind.state_key: st})
    ctx.manifest.save()
    return st


def applied_rounds(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [r for r in state.get("rounds", []) if r.get("status") == "applied"]


def lineage(state: Mapping[str, Any], plan_hash: str) -> list[dict[str, Any]]:
    """The applied rounds that led to ``plan_hash``, oldest first (empty for a plan no round produced). Walks the append-only round list backwards,
    so a plan that returned to an earlier hash (A -> B -> A) still counts every round."""
    out: list[dict[str, Any]] = []
    expect = plan_hash
    for r in reversed(applied_rounds(state)):
        if r.get("plan_hash") != expect:
            break
        out.append(r)
        expect = r.get("parent_plan_hash")
    return list(reversed(out))


def seen_hashes(state: Mapping[str, Any]) -> set[str]:
    """Every plan hash an applied round started from or produced."""
    return {h for r in applied_rounds(state) for h in (r.get("parent_plan_hash"), r.get("plan_hash")) if h}


def next_number(ctx: Any, kind: RoundKind) -> int:
    """The next revision number: one more than the highest ``<prefix>NN...json`` in the folder, whatever its digit count (r99 -> r100 -> r101)."""
    d = revisions_dir(ctx)
    rx = re.compile(re.escape(kind.prefix) + r"(\d+)(?:\.json|\.[a-z_]+\.json)$")
    nums = [int(m[1]) for p in d.glob(f"{kind.prefix}[0-9]*.json") if (m := rx.match(p.name))] if d.is_dir() else []
    return max(nums + [0]) + 1


def halt_text(rec: Mapping[str, Any], kind: RoundKind) -> str | None:
    """Why no further automatic change followed this round (None when nothing stopped them). A critic round stops further critic rounds; a revision (the
    person's own request) stops the critic from changing the revised plan on its own: it only reports."""
    halt, tag = rec.get("halt"), kind.tag(int(rec.get("round") or 0))
    tail = f"no further {kind.noun}s ran" if kind is CRITIC else "no automatic critic round is applied on top of it (the critic only reports)"
    if halt == "preview_failed":
        return f"the preview of the new version could not be rendered after {tag}; {tail} (nothing was reverted)"
    if halt == "qc_failed":
        return f"QC could not be re-run on the new version after {tag}; {tail} (nothing was reverted)"
    if halt == "qc_regression":
        return (f"QC got worse after {tag}: new failing check(s) {', '.join(rec.get('qc_new_failures') or [])}; {tail}. The change was not "
                f"reverted: the earlier plan is plans/revisions/{tag}.parent_edit_plan.json")
    return None


def outline_alternatives(ctx: Any) -> tuple[dict[str, list[str]] | None, dict[str, str]]:
    """scene_id -> candidate moment ids of the approved outline this plan was made from, plus a line of description per moment. (None, {}) when
    the plan has no approved outline behind it (baseline planner, or the outline was edited after the plan was made)."""
    if ctx.config.workflow.planner != "llm" or ds._qc_outline_scene_ids(ctx) is None:
        return None, {}
    try:
        from trip_vlog.planning import selection as sl
        from trip_vlog.selection_stages import build_context

        sctx, _ = build_context(ctx)
        cands: dict[str, list[str]] = {}
        notes: dict[str, str] = {}
        for si, sc in enumerate(sctx.outline.scenes):
            ms = sl.scene_candidates(sctx, si)
            cands[sc.scene_id] = [m.moment_id for m in ms]
            notes.update({m.moment_id: " ".join((m.description or "").split())[:200] for m in ms})
        return cands, notes
    except Exception:  # noqa: BLE001 - alternatives are an extra; without them swap_moment is simply unavailable
        return None, {}


def llm_key(cfg: Any, profile: str, ep: Any) -> dict[str, Any]:
    """What about the endpoint can change a reply (part of a stage's input hash)."""
    from trip_vlog.llm.factory import resolve_mode, resolve_model

    return {"endpoint": profile, "api": ep.api, "model": resolve_model(ep), "params": ep.params, "structured_output": ep.structured_output, "mode": resolve_mode(cfg)}


# ---- what accepted revisions changed about the rules ------------------------------------------------------------------------

def manifest_overrides(manifest: Any) -> dict[str, Any] | None:
    """``revise.overrides`` (target duration, waived must_include references) while the plan stage's plan is still the one they were made on top of: a
    plan made again (config or media changed, the plan stage re-ran) starts from the configured rules."""
    ov = (manifest.get("revise") or {}).get("overrides")
    if not isinstance(ov, dict):
        return None
    return ov if ov.get("base_plan_hash") == (manifest.get("plan") or {}).get("plan_hash") else None


def apply_manifest_overrides(config: Any, manifest: Any) -> None:
    config.set_revision_overrides(manifest_overrides(manifest))


# ---- files ------------------------------------------------------------------------------------------------------------

PARENT_NAMES = {"edit_plan": "parent_edit_plan.json", "selection_plan": "parent_selection_plan.json", "summary": "parent_plan_summary.md",
                "validation": "parent_validation_report.json", "qc_report": "parent_qc_report.json", "mapping_report": "parent_mapping_report.json"}
DERIVED_STAGES = ("assemble", "preview", "qc", "critic", "revise", "handoff")


def copy_file(src: Path, dst: Path) -> str | None:
    if not src.is_file():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return str(dst)


def replace_from(src: Path, target: Path) -> None:
    """Copy ``src`` over ``target`` through a temporary file in the same folder, so an interruption never leaves a half-written target."""
    tmp = target.with_name(target.name + ".critic-restore.tmp")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def targets(ctx: Any) -> dict[str, Path]:
    """The live files a round replaces, by the key their saved parent copy goes by."""
    pp = ds._pp(ctx)
    return {"edit_plan": pp.edit_plan, "selection_plan": pp.selection_plan, "summary": pp.summary, "validation": pp.validation_report,
            "qc_report": ds._qc_paths(ctx)[0], "mapping_report": ctx.workspace.root / "filmcraft" / "mapping_report.json", "preview": ds._preview_path(ctx)}


def mark_stage(ctx: Any, name: str, res: StageResult, inputs: Callable[[Any], Any], ran_by: str) -> None:
    """Record a stage a round ran itself (assemble, preview, qc) as if the runner had: with its new input hash, so the next run is a cache hit."""
    try:
        h = stable_hash(inputs(ctx))
    except Exception:  # noqa: BLE001
        h = None
    entry = ctx.manifest.stages.setdefault(name, {})
    entry.update(status=str(res.status), decision="ran", input_hash=h, finished_at=utc_now_iso(), summary=res.summary, error=res.error, ran_by=ran_by)
    ctx.manifest.save()


def safely(fn: Callable[[Any], StageResult], ctx: Any) -> StageResult:
    try:
        return fn(ctx)
    except Exception as e:  # noqa: BLE001 - a step after the rebuild must not take the draft down
        return StageResult(Status.FAILED, {}, f"{type(e).__name__}: {e}")


def failed_names(qc: Mapping[str, Any]) -> set[str]:
    return {str(c.get("name")) for c in qc.get("checks", []) if c.get("status") == "fail"}


# ---- rollback and recovery --------------------------------------------------------------------------------------------

def rollback(ctx: Any, saved: Mapping[str, Path | None], approval: Any, *, new_hash: str, parent_hash: str, new_selection: Path | None = None,
             new_plan: Path | None = None) -> str:
    """Put the parent files back, but only over what this round wrote. Looks at the plan on disk first:
      full    it is the round's new plan: every saved file goes back (a saved copy of None means the file did not exist before: the new one is deleted)
              and the parent approval returns if it is still the round's;
      parent  it is already the parent plan (the round stopped before it wrote it): only a selection plan this round wrote is put back;
      kept    it is something else (somebody edited or replaced it since): nothing is touched.
    "The round's new plan" means the same bytes the round wrote (`new_plan`), not merely the same content hash: a reformatted file is the person's.
    Returns which of the three happened."""
    tg = targets(ctx)
    try:
        cur: str | None = ds.plan_file_hash(tg["edit_plan"])
    except (OSError, ValueError):
        cur = None
    unreadable = cur is None    # missing or half-written (an earlier restore was cut off): nothing of the person's to keep, the parent goes back
    if unreadable or cur == new_hash and cur != parent_hash and (new_plan is None or not new_plan.is_file() or new_plan.read_bytes() == tg["edit_plan"].read_bytes()):
        for key, target in tg.items():
            if key not in saved:
                continue
            src = saved[key]
            if src is None:
                target.unlink(missing_ok=True)
            elif src.is_file():
                replace_from(src, target)
        held = ds.approval_record(ctx.manifest)
        if approval is not None and (held or {}).get("plan_hash") == new_hash:
            ctx.manifest.set(approval=approval)
        outcome = "full"
    elif cur == parent_hash:
        sel, src = tg["selection_plan"], saved.get("selection_plan")
        if new_selection is not None and sel.is_file() and new_selection.is_file() and sel.read_bytes() == new_selection.read_bytes():
            if src is None and "selection_plan" in saved:
                sel.unlink(missing_ok=True)     # the round created it
            elif src is not None and src.is_file():
                replace_from(src, sel)
        outcome = "parent"
    else:
        outcome = "kept"
    ctx.manifest.save()
    ctx.data.pop("prepared", None)
    return outcome


def recover_interrupted(ctx: Any, kind: RoundKind, rollback_fn: Callable[..., str] | None = None) -> str | None:
    """A round that was started and never finished (crash, Ctrl-C, power loss) left ``<state_key>.applying``. Run before any stage: put the saved parent
    files back if the plan on disk is still the round's (see `rollback`; a plan the person edited meanwhile is kept), mark the revision `rolled_back`,
    clear the marker and forget the input hashes of the stages that built on the half-done state so they run again. Returns a sentence, or None when
    there was nothing."""
    rb = rollback_fn or rollback
    mk = state_of(ctx, kind).get("applying")
    if not isinstance(mk, dict):
        return None
    root = ctx.workspace.root
    tag = str(mk.get("tag") or kind.tag(int(mk.get("number") or 0)))
    d = revisions_dir(ctx)
    head = f"{kind.noun} {tag} was interrupted before it finished"
    try:
        outcome = rb(ctx, {k: (root / v if v else None) for k, v in (mk.get("saved") or {}).items()}, mk.get("parent_approval"),
                     new_hash=str(mk.get("new_plan_hash")), parent_hash=str(mk.get("parent_plan_hash")), new_selection=d / f"{tag}.selection_plan.json",
                     new_plan=d / f"{tag}.edit_plan.json")
    except OSError as e:    # e.g. a locked file on Windows: keep the marker so the next run tries again, and let this run go on
        note = f"{head}; putting the previous plan back failed ({type(e).__name__}: {e}) and will be retried on the next run"
        ctx.logger.event(kind.event, Status.PARTIAL, message=note)
        return note
    note = {"full": f"{head}; the previous plan (hash {str(mk.get('parent_plan_hash'))[:12]}) was put back",
            "parent": f"{head}; the plan on disk is the previous plan, nothing else was put back",
            "kept": f"{head}; the edit plan on disk was changed since, so nothing was put back and your plan stays"}[outcome]
    try:
        doc = json.loads((d / f"{tag}.json").read_text(encoding="utf-8"))
        doc.update(status="rolled_back", note=note)
        atomic_write_json(d / f"{tag}.json", doc)
        md = d / f"{tag}.md"
        atomic_write_text(md, md.read_text(encoding="utf-8").replace(f"{tag}: applying", f"{tag}: rolled_back", 1) + "\n" + note + "\n")
    except (OSError, ValueError):
        pass
    if outcome == "full":
        for name in DERIVED_STAGES:
            if name in ctx.manifest.stages:
                ctx.manifest.stages[name]["input_hash"] = None
    save_state(ctx, kind, applying=None)
    ctx.data[kind.recovered_key] = note     # shown in this run's report if the stage runs; the orchestrator echoes it either way
    ctx.logger.event(kind.event, Status.PARTIAL, message=note)
    return note


# ---- applying a round -------------------------------------------------------------------------------------------------

@dataclass
class RoundHooks:
    """What a caller supplies to `apply_round`."""

    revision_doc: Callable[[str, str | None, dict[str, Any]], dict[str, Any]]      # (status, snapshot rel, extra) -> the revision document
    write_revision: Callable[[Any, int, dict[str, Any], ApplyResult], str]         # (ctx, number, doc, result) -> rel path of <tag>.json
    plan_note: str                                                                 # the plan's approval note; {tag} and {parent} are filled in
    approval_extra: Callable[[str | None, str | None, dict[str, Any]], dict[str, Any]]   # (snapshot rel, snapshot sha256, rec) -> approval fields
    changed_error: str                                                             # said when the plan on disk is not the one that was reviewed
    failed_marker: str | None = None                                               # remembered in <state_key>.failed when the revision itself is rejected
    rec_extra: dict[str, Any] = field(default_factory=dict)                        # extra fields of the round record
    snapshot: tuple[Path, dict[str, Any]] | None = None                            # a document kept as it was when the round started (the critic's report)
    rollback: Callable[..., str] | None = None
    ran_by: str = ""
    state_update: Callable[[dict[str, Any]], dict[str, Any]] | None = None    # (round record) -> fields saved with the round, in the same write


def apply_round(ctx: Any, kind: RoundKind, parent: EditPlan, res: ApplyResult, *, number: int | None, authorization: str, qc_before: Mapping[str, Any],
                hooks: RoundHooks) -> dict[str, Any]:
    """Write the new plan, approve it, rebuild a NEW FilmCraft project, re-render the preview and re-run QC. Returns the round record (status `applied`, or
    `failed` after the previous plan was put back). QC is never reverted automatically; the record says when it got worse (`halt`), which stops further
    automatic rounds."""
    cfg = ctx.config
    pp = ds._pp(ctx)
    rb = hooks.rollback or rollback
    n = number or next_number(ctx, kind)       # an approved proposal keeps the number it was proposed under
    tag = kind.tag(n)
    rec: dict[str, Any] = {"round": n, "status": "failed", "revision": None, "parent_plan_hash": parent.plan_hash, "plan_hash": res.edit.plan_hash,
                           "authorization": authorization, **hooks.rec_extra, "at": utc_now_iso()}
    try:
        on_disk = ds.plan_file_hash(pp.edit_plan)
    except (OSError, ValueError) as e:
        on_disk = f"unreadable ({type(e).__name__})"
    if on_disk != parent.plan_hash:     # somebody edited the plan after it was reviewed: change nothing
        rec["error"] = hooks.changed_error
        return rec
    d = revisions_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    snap_rel = snap_sha = None
    if hooks.snapshot is not None:
        snap, snap_doc = hooks.snapshot
        atomic_write_json(snap, snap_doc)                                 # the review that motivated this round, kept as it was
        snap_rel, snap_sha = rel(ctx, snap), sha256_file(snap)
    # keep the parent: plan files, QC report, mapping report, preview (None: the file did not exist)
    tg = targets(ctx)
    prev_preview = tg["preview"]
    saved: dict[str, Path | None] = {k: (Path(v) if (v := copy_file(tg[k], d / f"{tag}.{name}")) else None) for k, name in PARENT_NAMES.items()}
    kept = copy_file(prev_preview, prev_preview.with_name(f"{prev_preview.stem}.before_{tag}{prev_preview.suffix}"))
    saved["preview"] = Path(kept) if kept else None
    prev_approval = copy.deepcopy(ds.approval_record(ctx.manifest))
    prev_run = ds.latest_run(ctx)
    # the marker comes before the first change to a live file: if this process dies from here on, the next run puts the parent back
    save_state(ctx, kind, applying={"number": n, "tag": tag, "new_plan_hash": res.edit.plan_hash, "parent_plan_hash": parent.plan_hash, "started_at": utc_now_iso(),
                                    "saved": {k: (rel(ctx, v) if v else None) for k, v in saved.items()}, "parent_approval": prev_approval})
    if hooks.snapshot is not None:
        rec.update(report=snap_rel, report_sha256=snap_sha)
    rec.update(ops=[a.op.to_dict() for a in res.applied], ops_text=render_ops_markdown(res.applied, res.refused),
               diff_lines=res.diff.lines() if res.diff else [], qc_before=qc_before.get("status"),
               parent_project=prev_run["project_path"] if prev_run else None, preview_before=rel(ctx, saved["preview"]) if saved["preview"] else None)
    rdoc = hooks.revision_doc("applying", snap_rel, {"saved_parent_files": {k: (rel(ctx, v) if v else None) for k, v in saved.items()}})
    rec["revision"] = hooks.write_revision(ctx, n, rdoc, res)
    atomic_write_json(d / f"{tag}.edit_plan.json", to_json_dict(res.edit))
    atomic_write_json(d / f"{tag}.selection_plan.json", to_json_dict(res.selection))

    def restore(why: str, *, remember: bool) -> dict[str, Any]:
        """Put the previous state back. ``remember``: the revision itself is at fault (the plan is rejected), so `resume` must not build it again;
        a failure of the environment is retried by the next run."""
        rb(ctx, saved, prev_approval, new_hash=res.edit.plan_hash, parent_hash=parent.plan_hash, new_selection=d / f"{tag}.selection_plan.json",
           new_plan=d / f"{tag}.edit_plan.json")
        rec.update(status="failed", error=why, remembered=bool(remember and hooks.failed_marker))
        upd: dict[str, Any] = {"applying": None, "rounds": [r for r in state_of(ctx, kind).get("rounds", []) if not (r.get("round") == n and r.get("plan_hash") == res.edit.plan_hash)]}
        if remember and hooks.failed_marker:
            upd["failed"] = list(dict.fromkeys([*state_of(ctx, kind).get("failed", []), hooks.failed_marker]))
        save_state(ctx, kind, **upd)
        rdoc.update(status="failed", error=why, note=f"the previous plan was put back; the proposed new plan is kept in this folder as {tag}.edit_plan.json")
        hooks.write_revision(ctx, n, rdoc, res)
        return rec

    try:
        try:
            view = PlanningCatalog.from_catalog(ctx.catalog)
            vrep = validate_edit_plan(res.edit, view, ValidationSettings.effective(cfg))
            if vrep.errors:
                return restore(f"the new plan does not validate ({vrep.errors[0].message}); the previous plan was put back", remember=True)
            _write_all(ctx.workspace, cfg, dict(ctx.paths.outputs), res.selection, res.edit, vrep, view,
                       approval_note=hooks.plan_note.format(tag=tag, parent=parent.plan_hash[:12]))
            ds.approve_current_plan(ctx.manifest, res.edit.plan_hash, kind.approval_mode, parent_plan_hash=parent.plan_hash, revision=rec["revision"],
                                    **hooks.approval_extra(snap_rel, snap_sha, rec), authorization=authorization, parent_approval=prev_approval)
            ctx.data.pop("prepared", None)
            asm = ds.assemble_stage(ctx)
        except ds.PlanRejected as e:
            return restore(f"the new plan was rejected ({e}); the previous plan was put back", remember=True)
        except Exception as e:  # noqa: BLE001
            return restore(f"the new plan could not be written or built ({type(e).__name__}: {e}); the previous plan was put back", remember=False)
        if asm.status == Status.FAILED:
            return restore(f"the new FilmCraft project could not be built; the previous plan was put back: {asm.error}", remember=bool(asm.summary.get("plan_rejected")))
        mark_stage(ctx, "assemble", asm, ds._assemble_inputs, hooks.ran_by)
        prv = safely(ds.preview_stage, ctx)
        mark_stage(ctx, "preview", prv, ds._preview_inputs, hooks.ran_by)
        qc: StageResult | None = None
        if prv.status == Status.SUCCEEDED:      # QC only runs on a preview that was rendered
            qc = safely(ds.qc_stage, ctx)
            mark_stage(ctx, "qc", qc, ds._qc_inputs, hooks.ran_by)
        qc_ok = qc is not None and qc.status != Status.FAILED and "status" in qc.summary
        after = [str(x) for x in (qc.summary.get("failed_checks") or [])] if qc is not None and qc_ok else []
        new_failures = sorted(set(after) - failed_names(qc_before))
        halt = "preview_failed" if qc is None else "qc_failed" if not qc_ok else "qc_regression" if new_failures else None
        rec.update(status="applied", project=asm.summary.get("project"), project_version=asm.summary.get("version"), preview_status=str(prv.status),
                   qc_after=qc.summary.get("status") if qc is not None else None, qc_failed_after=after, qc_new_failures=new_failures,
                   qc_regressed=halt in ("qc_failed", "qc_regression"), halt=halt)
        rec["halt_note"] = halt_text(rec, kind)
        rdoc.update(status="applied", project=rec["project"], project_version=rec["project_version"], qc_before=rec["qc_before"], qc_after=rec["qc_after"],
                    preview_before=rec["preview_before"], qc_regressed=rec["qc_regressed"], qc_new_failures=new_failures, halt=halt, halt_note=rec["halt_note"])
        hooks.write_revision(ctx, n, rdoc, res)
        save_state(ctx, kind, rounds=[*state_of(ctx, kind).get("rounds", []), rec], pending=None, applying=None,
                   **(hooks.state_update(rec) if hooks.state_update else {}))
    except BaseException:   # Ctrl-C, SystemExit (also while the round is being recorded): the previous plan goes back, then it goes on
        try:
            restore("the round was interrupted; the previous plan was put back", remember=False)
        except Exception as e:  # noqa: BLE001 - never hide the interrupt; the marker stays and the next run rolls back
            ctx.logger.event(kind.event, Status.FAILED, message=f"putting the previous plan back failed ({type(e).__name__}: {e}); the next run retries")
        raise
    return rec
