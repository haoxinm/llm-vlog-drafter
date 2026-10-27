"""`trip-vlog reseal`: accept a hand edit of `plans/edit_plan.json` and/or `plans/story_outline.json` explicitly (DESIGN §20 Phase 7).

Why it exists. An edit plan carries a `plan_hash` field that must match its content, and every consumer re-checks it (`approve`, assemble), so a
plan edited in a text editor is refused with `plan_hash_mismatch`. The story outline has no hash field, but its approval, its readable summary
(`story_outline.md`) and the plan made from it are bound to a hash of its content. `reseal` makes the edit official, in four steps:

  1. re-run the FULL validators on the file as it is (the plan: schema, catalog references, source ranges, transitions, titles, must_include /
     must_exclude, duration tolerance, ...; the outline: schema, moments, durations, coverage); any error refuses the seal and changes nothing;
  2. keep a copy of the file as it was (`edit_plan.<hash>.bak.json` / `story_outline.<hash>.bak.json`, the names the plan and outline stages use too);
  3. write the validated document back (the plan with `plan_hash` recomputed, frame-boundary repairs applied and listed; the outline in its canonical
     form) and refresh what is derived from it (`plan_summary.md` / `validation_report.json`, or `story_outline.md`);
  4. record it in the manifest (`reseal`), and treat the approval exactly as a hand edit is treated by the pipeline: an approval is bound to the hash
     of the content, so one that no longer matches is marked `invalidated`. Nothing is ever approved here: run `trip-vlog approve`.

What it does not do: it does not touch the recorded hash of the plan or outline the stages generated (so a later re-plan still sees the file as
edited and keeps its backup, and the plan does not qualify for the "derived from the approved outline" approval); it does not run FilmCraft; it
never touches media, FilmCraft projects or Resolve projects.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trip_vlog.config import ConfigError, load_config, resolve_paths
from trip_vlog.logging_utils import RunLogger, Status, new_run_id, utc_now_iso
from trip_vlog.media.catalog import Catalog
from trip_vlog.paths import long_path
from trip_vlog.state import Manifest, Workspace, atomic_write_json, atomic_write_text

WHAT = ("outline", "plan")


@dataclass
class SealResult:
    what: str                                   # "plan" | "outline"
    path: str                                   # workspace-relative
    outcome: str                                # "sealed" | "unchanged" | "refused"
    previous_hash: str | None = None            # hash of the content before (the hash approvals are bound to)
    new_hash: str | None = None
    backup: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    approval: str | None = None                 # the approval record: "none" | "kept" | "invalidated" | "for another plan" / "for another outline"
    critic_blocked: str | None = None           # plan only: why critic / revise will refuse this plan until it is re-planned (None: they can work on it)

    @property
    def ok(self) -> bool:
        return self.outcome != "refused"


def _approval_state(ap: Any, key: str, lineage: set[Any], new: str | None, what: str) -> str:
    """What the approval record says about this content: none (no approved record), kept (bound to the new hash), invalidated (bound to the hash the
    file had before the edit (`lineage`: its own recorded plan_hash / the hash the stage recorded), so the edit voids it), or for another plan/outline (bound to some other hash: this seal neither made nor voids it)."""
    if not isinstance(ap, dict) or ap.get("status") != "approved":
        return "none"
    held = ap.get(key)
    if held == new:
        return "kept"
    return "invalidated" if held in lineage else f"for another {what}"


def _roundtrip_block(ctx: Any, sealed: Any, selection: Any) -> str | None:
    """Critic and revise only work on a plan that its selection plan resolves to exactly (plan_diff.check_roundtrip). Returns the reason it does not, or None."""
    try:
        from trip_vlog.planning.catalog_view import PlanningCatalog
        from trip_vlog.planning.plan_diff import PlanBaseError, PlanContext, check_roundtrip, selection_from_edit
        from trip_vlog.planning.validate import ValidationSettings
        from trip_vlog.planning.resolve_plan import effective_plan_settings
        from trip_vlog.revision_common import outline_alternatives

        alts, _ = outline_alternatives(ctx)
        pc = PlanContext(PlanningCatalog.from_catalog(ctx.catalog), ValidationSettings.effective(ctx.config), effective_plan_settings(ctx.config), alts,
                         float(ctx.config.qc.min_still_seconds))
        try:
            check_roundtrip(sealed, selection_from_edit(sealed, selection), pc)
        except PlanBaseError as e:
            return str(e)
    except Exception:       # noqa: BLE001 - advice only; never fail a seal over it
        return None
    return None


def _rel(root: Path, p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(root).as_posix()
    except ValueError:
        return str(p)


def backup_file(path: Path, content_hash: str) -> str | None:
    """Copy `path` to `<stem>.<hash12>.bak.json` beside it. The name is the stage convention; an existing backup with other bytes is never overwritten
    (`...bak2.json`, ...). Returns the backup's name, None when an identical backup is already there."""
    base = content_hash[:12].replace("-", "")
    for i in range(1, 100):
        bak = path.with_name(f"{path.stem}.{base}.bak.json" if i == 1 else f"{path.stem}.{base}.bak{i}.json")
        if not long_path(bak).exists():
            shutil.copyfile(long_path(path), long_path(bak))
            return bak.name
        if long_path(bak).read_bytes() == long_path(path).read_bytes():
            return bak.name
    raise OSError(f"too many backups of {path.name} with the same hash")


# ---- the plan ----------------------------------------------------------------------------------------------------

def _seal_plan(ctx: Any, root: Path) -> SealResult:
    from trip_vlog.draft_stages import _pp, load_plan_json, plan_file_hash
    from trip_vlog.planning.catalog_view import PlanningCatalog
    from trip_vlog.planning.models import EditPlan, SelectionPlan, to_json_dict, with_hash
    from trip_vlog.planning.summary import render_summary
    from trip_vlog.planning.validate import ValidationSettings, validate_edit_plan

    pp = _pp(ctx)
    res = SealResult("plan", _rel(root, pp.edit_plan), "refused")
    if not pp.edit_plan.is_file():
        res.errors.append(f"{pp.edit_plan} does not exist; run `trip-vlog draft` first")
        return res
    try:
        doc = load_plan_json(pp.edit_plan)
    except (OSError, ValueError) as e:
        res.errors.append(f"{pp.edit_plan.name} is not readable JSON: {e}")
        return res
    res.previous_hash = plan_file_hash(pp.edit_plan)
    lineage = {res.previous_hash, doc.get("plan_hash") if isinstance(doc, dict) else None, (ctx.manifest.get("plan") or {}).get("plan_hash")} - {None}
    view = PlanningCatalog.from_catalog(ctx.catalog)
    settings = ValidationSettings.effective(ctx.config)
    # the plan_hash field is exactly what is being re-computed, so it is the one check that is skipped here (and made again on the sealed result)
    report = validate_edit_plan(doc, view, dataclasses.replace(settings, check_hash=False))
    res.warnings = [i.message for i in report.warnings]
    res.repairs = [r.message for r in report.repairs]
    if not report.ok or report.plan is None:
        res.errors = [f"{i.code}: {i.message}" + (f" ({i.path})" if i.path else "") for i in report.errors]
        atomic_write_json(pp.plans_dir / "reseal_report.json", {"ok": False, "at": utc_now_iso(), **report.to_dict()})
        return res
    sealed: EditPlan = with_hash(report.plan)
    again = validate_edit_plan(sealed, view, settings)          # the file that will exist must pass the checks every consumer makes
    if not again.ok:
        res.errors = [f"{i.code}: {i.message}" for i in again.errors]
        return res
    res.new_hash = sealed.plan_hash
    new_doc = to_json_dict(sealed)
    if new_doc == doc and not pp.edit_plan.read_bytes().startswith(b"\xef\xbb\xbf"):
        res.outcome = "unchanged"
        res.approval = _approval_state(ctx.manifest.get("approval"), "plan_hash", lineage, res.new_hash, "plan")
        res.notes.append("the plan is already sealed: its plan_hash matches its content and it passed every check; nothing was written")
        return res
    res.backup = backup_file(pp.edit_plan, res.previous_hash)
    atomic_write_json(pp.edit_plan, new_doc)
    selection = None
    try:
        selection = SelectionPlan.model_validate(json.loads(pp.selection_plan.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError, ValidationError):
        pass
    note = f"re-sealed after a manual edit on {utc_now_iso()}; needs `trip-vlog approve` unless the approval below still matches"
    atomic_write_text(pp.summary, render_summary(sealed, view, again, selection=selection, approval_note=note))
    atomic_write_json(pp.validation_report, again.to_dict())
    atomic_write_json(pp.plans_dir / "reseal_report.json", {"ok": True, "at": utc_now_iso(), "previous_hash": res.previous_hash, "plan_hash": res.new_hash,
                                                            "backup": res.backup, "repairs": res.repairs, **again.to_dict()})
    res.outcome = "sealed"
    ap = ctx.manifest.get("approval")
    res.approval = _approval_state(ap, "plan_hash", lineage, res.new_hash, "plan")
    if res.approval == "invalidated":
        # the same record the approval stage writes when it finds a changed plan
        ctx.manifest.set(approval={"status": "invalidated", "previous_plan_hash": ap.get("plan_hash"), "plan_hash": res.new_hash, "at": utc_now_iso()})
    res.critic_blocked = _roundtrip_block(ctx, sealed, selection)
    if res.critic_blocked:
        res.notes.append("`trip-vlog critic` and `trip-vlog revise` will refuse this plan until it is re-planned, because it no longer matches its selection plan: " + res.critic_blocked)
    if res.approval == "kept":
        res.notes.append("the content hash did not change, so the existing approval of this plan still applies")
    if res.approval == "invalidated" and "assemble" in ctx.manifest.stages:
        res.notes.append("a FilmCraft project built from the earlier plan is kept; after approval `resume` builds a NEW project version from this plan")
    return res


# ---- the outline -------------------------------------------------------------------------------------------------

def _seal_outline(ctx: Any, root: Path) -> SealResult:
    from trip_vlog.outline_stages import _render, check_outline_file, outline_approval_record, outline_paths
    from trip_vlog.planning import outline as ol

    op = outline_paths(ctx)
    res = SealResult("outline", _rel(root, op.outline), "refused")
    if not op.outline.is_file():
        res.errors.append(f"{op.outline} does not exist; this workspace has no story outline (run `trip-vlog outline`)")
        return res
    res.previous_hash = ol.outline_file_hash(op.outline)
    outline, report, oc = check_outline_file(ctx)
    res.warnings = [i.message for i in report.warnings]
    if outline is None or not report.ok:
        res.errors = [f"{i.code}: {i.message}" + (f" ({i.path})" if i.path else "") for i in report.errors]
        atomic_write_json(op.plans_dir / "reseal_outline_report.json", {"ok": False, "at": utc_now_iso(), **report.to_dict()})
        return res
    res.new_hash = ol.outline_hash(outline)
    text = ol.outline_text(outline)
    on_disk = op.outline.read_bytes()
    rec = ctx.manifest.get("outline") or {}
    in_sync = on_disk == text.encode("utf-8") and rec.get("md_hash") == res.new_hash and op.summary.is_file()
    ap = outline_approval_record(ctx.manifest)
    lineage = {res.previous_hash, rec.get("md_hash"), rec.get("outline_hash")} - {None}
    if in_sync:
        res.outcome = "unchanged"
        res.approval = _approval_state(ap, "outline_hash", lineage, res.new_hash, "outline")
        res.notes.append("the outline is already in canonical form and its summary is current; nothing was written")
        return res
    if on_disk != text.encode("utf-8"):
        res.backup = backup_file(op.outline, res.previous_hash)
        atomic_write_text(op.outline, text)
    atomic_write_text(op.summary, _render(ctx, outline, oc, report, res.new_hash))
    ctx.manifest.set(outline={**rec, "md_hash": res.new_hash})
    atomic_write_json(op.plans_dir / "reseal_outline_report.json", {"ok": True, "at": utc_now_iso(), "previous_hash": res.previous_hash,
                                                                    "outline_hash": res.new_hash, "backup": res.backup, **report.to_dict()})
    res.outcome = "sealed"
    res.approval = _approval_state(ap, "outline_hash", lineage, res.new_hash, "outline")
    if res.approval == "invalidated":
        ctx.manifest.set(outline_approval={"status": "invalidated", "previous_outline_hash": ap.get("outline_hash"), "outline_hash": res.new_hash, "at": utc_now_iso()})
    made_from = (ctx.manifest.get("plan") or {}).get("outline_hash")
    if made_from and made_from != res.new_hash:
        res.notes.append("the edit plan was made from a different outline: after you approve this outline, `resume` makes a NEW plan from it "
                         "(a plan you edited is kept as plans/edit_plan.<hash>.bak.json; use `reseal --plan` on a plan you want to keep)")
    return res


# ---- entry point -------------------------------------------------------------------------------------------------

def reseal(output: Path | str, what: tuple[str, ...] | None = None) -> list[SealResult]:
    """Re-validate and re-seal the outline and/or the plan of the workspace at `output`. `what`: any of ("outline", "plan"); None = every file that exists.
    Raises ConfigError when `output` is not a workspace or a round is half-way applied."""
    ws = Workspace(Path(output).expanduser().resolve())
    if not ws.manifest_path.exists() or not ws.project_yaml.exists():
        raise ConfigError(f"{ws.root} is not a trip-vlog workspace; run `trip-vlog draft` first")
    manifest = Manifest.load(ws.manifest_path)
    for key, name in (("critic", "critic round"), ("revise", "revision")):
        if isinstance((manifest.get(key) or {}).get("applying"), dict):
            raise ConfigError(f"a {name} was interrupted before it finished; run `trip-vlog resume --output {ws.root}` first, it puts the previous plan back")
    from trip_vlog.orchestrator import RunContext
    from trip_vlog.revision_common import apply_manifest_overrides

    config = load_config(ws.project_yaml)
    apply_manifest_overrides(config, manifest)
    paths = resolve_paths(config, manifest.get("media_root") or ws.root.parent, ws.root)
    if not ws.catalog_path.exists():
        raise ConfigError(f"{ws.root} has no catalog; run `trip-vlog index` first")
    wanted = tuple(what) if what else None
    results: list[SealResult] = []
    with Catalog(ws.catalog_path) as catalog:
        logger = RunLogger(ws.run_log, ws.errors_log, new_run_id())
        ctx = RunContext(config, paths, ws, logger, manifest, catalog, None, None)
        from trip_vlog.draft_stages import _pp
        from trip_vlog.outline_stages import outline_paths

        for kind, exists in (("outline", lambda: outline_paths(ctx).outline.is_file()), ("plan", lambda: _pp(ctx).edit_plan.is_file())):
            if wanted is not None and kind not in wanted:
                continue
            if wanted is None and not exists():
                continue            # nothing to seal: not an error unless the person asked for it by name
            results.append((_seal_outline if kind == "outline" else _seal_plan)(ctx, ws.root))
        if not results:
            raise ConfigError(f"{ws.root} has neither plans/story_outline.json nor plans/edit_plan.json; nothing to re-seal")
        sealed = [r for r in results if r.outcome == "sealed"]
        if sealed:
            manifest.set(reseal=[*(manifest.get("reseal") or []), *({"at": utc_now_iso(), "what": r.what, "previous_hash": r.previous_hash, "hash": r.new_hash,
                                                                   "backup": r.backup, "repairs": len(r.repairs), "approval": r.approval} for r in sealed)])
            manifest.save()
        for r in results:
            logger.event("reseal", Status.SUCCEEDED if r.ok else Status.FAILED, what=r.what, outcome=r.outcome, previous_hash=r.previous_hash, hash=r.new_hash)
    return results
