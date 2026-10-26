"""Review follow-ups of the revision loop: overrides and proposals do not outlive a re-plan, a rejected approved proposal is not retried, confirmation
thresholds, partial requests, the clarification exit code, dry runs, dropped transitions, revision numbers, the critic after a regressed revision."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from critic_helpers import make_draft_ctx, stub_rebuild
from planning_helpers import make_catalog, mid, sel_clip, sel_plan
from revise_helpers import ReviseTransport, answer, gateway_for, op, revise_config

from trip_vlog import cli
from trip_vlog import critic_stages as cs
from trip_vlog import draft_stages as ds
from trip_vlog import revise_stages as rs
from trip_vlog import revision_common as rc
from trip_vlog.logging_utils import Status
from trip_vlog.orchestrator import approve_revision
from trip_vlog.planning import revisions as rev
from trip_vlog.planning.models import EditPlan
from trip_vlog.planning.plan_diff import REVISION_OPERATIONS, PlanContext, PlanOp, apply_ops
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.planning.validate import ValidationSettings
from trip_vlog.state import Manifest, StageResult, Workspace

CAT = make_catalog()
A0, A1 = (mid(CAT, "day1/a.mp4", i) for i in range(2))
B0 = mid(CAT, "day1/b.mp4", 0)
P0 = mid(CAT, "day2/p.mp4", 0)
STILL = mid(CAT, "day1/c.jpg")
PS = PlanSettings(frame_rate="30", target_duration_seconds=18.5)
VS = ValidationSettings(frame_rate="30", target_duration_seconds=18.5)


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def make(revise=None, workflow=None, transport=None, **kw):
        cfg = revise_config(revise, workflow, **kw)
        ctx, sel, plan = make_draft_ctx(tmp_path, monkeypatch, cfg)
        monkeypatch.setattr(ds, "latest_run", lambda c: {"project_path": "filmcraft/Trip Vlog Draft_v001.fcproj", "version": 1})
        t = transport or ReviseTransport()
        monkeypatch.setattr(rs, "gateway_factory", lambda c: gateway_for(c.config, c.workspace.root, t))
        monkeypatch.setattr(rc, "outline_alternatives", lambda c: (None, {}))
        (ctx.workspace.root / "project.yaml").write_text(cfg.to_yaml(), encoding="utf-8")
        return ctx, plan, t
    return make


def ask(ctx, text, **flags):
    ctx.data["revise"] = {"request": text, **flags}
    ctx.data.pop("prepared", None)
    return rs.revise_stage(ctx)


def approved(ctx):
    approve_revision(ctx.workspace.root)
    ctx.manifest.data.update(Manifest.load(ctx.workspace.manifest_path).data)


def current_plan(ctx):
    return EditPlan.model_validate(rj(ds._pp(ctx).edit_plan))


def core_plan():
    sel = sel_plan([sel_clip("c1", A0, 5), sel_clip("c2", A1, 4), sel_clip("c3", B0, 3)], [sel_clip("c4", P0, 3.5, audio="muted"), sel_clip("c5", STILL, 3)])
    return resolve_selection_plan(sel.model_copy(update={"schema_version": "1.1"}), CAT, PS)


def decision(p, ops, **policy):
    cfg = revise_config(revise={"confirm_duration_change_pct": 15, **policy})
    res = rev.apply_revision(p, [PlanOp.from_dict(o, rev.REVISION_OPERATIONS) for o in ops], PlanContext(CAT, VS, PS), cfg.revise)
    parsed = rev.Parsed("", [a.op for a in res.applied], [], [], [], False, "")
    return rev.decide(parsed, p, res, [], rev.Policy.from_config(cfg), ()), res


# ---- 1: overrides and proposals belong to the plan they were made on ------------------------------------------------------

def make_override(ctx, monkeypatch):
    """An applied revision that moved the target (scene removal, 18.5 s -> 12 s): overrides in the manifest."""
    stub_rebuild(monkeypatch)
    ask(ctx, "drop the second scene")
    approved(ctx)
    ctx.data.pop("revise", None)
    assert rs.revise_stage(ctx).status == Status.SUCCEEDED
    assert ctx.manifest.get("revise")["overrides"]["target_duration_seconds"] == pytest.approx(12.0)


def test_a_re_plan_that_reproduces_the_same_hash_still_forgets_overrides_and_proposals(setup, monkeypatch):
    ctx, plan, _ = setup(transport=ReviseTransport(lambda c: answer([op("remove_scene", scene_id="scene_002")])))
    ctx.manifest.set(plan={"plan_hash": plan.plan_hash})        # the plan stage's plan; the overrides are tied to it
    make_override(ctx, monkeypatch)
    same = plan.plan_hash                                       # a re-plan that makes the very same plan again: the hash tie alone would keep them
    rs._save(ctx, pending={"status": "pending", "number": 9, "revision": "plans/revisions/revise_r09.json"})
    assert rc.manifest_overrides(ctx.manifest) is not None
    monkeypatch.setattr(ds, "plan_baseline_stage", lambda c: StageResult(Status.SUCCEEDED, {"plan_hash": same}))
    ctx.config.workflow.planner = "baseline"
    assert ds.plan_stage(ctx).status == Status.SUCCEEDED
    assert rc.manifest_overrides(ctx.manifest) is None and not ctx.manifest.get("revise").get("overrides") and not ctx.manifest.get("revise").get("pending")
    assert ctx.config.revision_overrides == {} and ctx.manifest.get("revise")["rounds"], "the history stays"


def test_a_plan_stage_that_fails_keeps_the_overrides(setup, monkeypatch):
    ctx, plan, _ = setup(transport=ReviseTransport(lambda c: answer([op("remove_scene", scene_id="scene_002")])))
    make_override(ctx, monkeypatch)
    monkeypatch.setattr(ds, "plan_baseline_stage", lambda c: StageResult(Status.FAILED, {}, "boom"))
    ctx.config.workflow.planner = "baseline"
    assert ds.plan_stage(ctx).status == Status.FAILED
    assert ctx.manifest.get("revise")["overrides"]["target_duration_seconds"] == pytest.approx(12.0)


def test_reset_overrides_clears_only_the_overrides_and_status_shows_them(tmp_path, capsys):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_dirs()
    ws.project_yaml.write_text("{}\n", encoding="utf-8")
    m = Manifest(ws.manifest_path)
    pend = {"status": "pending", "revision": "plans/revisions/revise_r02.json"}
    m.set(pipeline="draft", revise={"rounds": [{"status": "applied", "revision": "plans/revisions/revise_r01.json"}], "pending": pend,
                                    "overrides": {"base_plan_hash": "a" * 64, "target_duration_seconds": 12.0, "must_include_waived": ["day2/p.mp4"]}})
    m.save()
    assert cli.main(["status", "--output", str(ws.root)]) == 0
    out = capsys.readouterr().out
    assert "overrides : target duration 12 s, 1 must_include reference(s) waived" in out and "revise --reset-overrides" in out
    assert cli.main(["revise", "--output", str(ws.root), "--reset-overrides", "shorter"]) == 2
    assert "takes no request" in capsys.readouterr().err and Manifest.load(ws.manifest_path).get("revise")["overrides"]
    assert cli.main(["revise", "--output", str(ws.root), "--reset-overrides"]) == 0
    assert "overrides cleared: target duration 12 s" in capsys.readouterr().out
    st = Manifest.load(ws.manifest_path).get("revise")
    assert not st["overrides"] and st["pending"] == pend and len(st["rounds"]) == 1
    assert cli.main(["revise", "--output", str(ws.root), "--reset-overrides"]) == 0
    assert "no revision overrides are active" in capsys.readouterr().out
    assert cli.main(["status", "--output", str(ws.root)]) == 0 and "overrides :" not in capsys.readouterr().out


# ---- 2: an approved proposal the plan rules reject is not retried -------------------------------------------------------

def test_an_approved_proposal_whose_plan_is_rejected_is_not_retried_and_failed_is_not_duplicated(setup, monkeypatch):
    ctx, plan, _ = setup(transport=ReviseTransport(lambda c: answer([op("remove_scene", scene_id="scene_002")])))
    stub_rebuild(monkeypatch, fail_assemble="the plan is rejected", reject_plan=True)
    for _ in range(2):          # propose the same revision twice, approve it, and have the rebuild reject it each time
        ask(ctx, "drop the second scene")
        approved(ctx)
        assert rs.revise_wanted(ctx) is True
        ctx.data.pop("revise", None)
        res = rs.revise_stage(ctx)
        assert res.status == Status.FAILED and "rejected" in res.error
        assert ctx.manifest.get("revise")["pending"]["status"] == "stale"
        assert rs.revise_wanted(ctx) is False, "resume does not build it again"
    assert len(ctx.manifest.get("revise")["failed"]) == 1


# ---- 3: confirmation thresholds -------------------------------------------------------------------------------------------

def test_a_big_share_needs_confirmation_whatever_the_seconds_and_the_target_always_does():
    p = core_plan()      # 18.5 s
    rm = [{"op": "remove_scene", "scene_id": "scene_002"}]          # -6.5 s (-35%): under the 10 s floor
    d, _ = decision(p, rm, confirm_scene_removal=False, confirm_duration_change_min_seconds=10)
    assert d.needs_confirmation and "-6.5 s (-35%)" in d.destructive[0]
    d, _ = decision(p, rm, confirm_scene_removal=False, confirm_duration_change_min_seconds=10, confirm_change_pct_any=40)
    assert not d.needs_confirmation
    d, res = decision(p, [{"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 4}])
    assert not d.needs_confirmation
    cfg = revise_config()
    pol = rev.Policy.from_config(cfg)
    assert pol.confirm_change_pct_any == 30.0
    parsed = rev.Parsed("", [a.op for a in res.applied], [], [], [], False, "")
    d = rev.decide(parsed, p, res, [], pol, (), 12.0)
    assert d.needs_confirmation and d.destructive == ["moves the target duration of the film to 12 s"]


def test_a_43_percent_cut_of_a_short_film_is_proposed_not_applied(setup, monkeypatch):
    ctx, plan, _ = setup(transport=ReviseTransport(lambda c: answer([op("shorten_scene", scene_id="scene_001", shorten_by_seconds=4.0), op("remove_clip", clip_instance_id="c5")])),
                         revise={"confirm_scene_removal": False, "confirm_removed_clips": 9, "max_duration_change_pct": 60})
    calls = stub_rebuild(monkeypatch)
    res = ask(ctx, "much shorter")
    assert res.status == Status.AWAITING_APPROVAL and not calls and current_plan(ctx).plan_hash == plan.plan_hash
    assert any("-7.0 s (-38%)" in r for r in res.summary["confirm_reasons"]) and any("moves the target duration" in r for r in res.summary["confirm_reasons"])


def test_moving_only_the_target_needs_confirmation(setup, monkeypatch):
    ctx, plan, _ = setup(transport=ReviseTransport(lambda c: answer([op("shorten_scene", scene_id="scene_001", shorten_by_seconds=3.0)])),
                         revise={"confirm_duration_change_pct": 100})        # -16%: no share rule fires; the target (18.5 s, tolerance 2 s) moves
    stub_rebuild(monkeypatch)
    res = ask(ctx, "a bit shorter")
    assert res.status == Status.AWAITING_APPROVAL and len(res.summary["confirm_reasons"]) == 1 and res.summary["confirm_reasons"][0].startswith("moves the target duration of the film to 15.5")


# ---- 5, 6: clarification and dry runs -----------------------------------------------------------------------------------------

def test_a_clarification_is_not_approvable_and_says_so(setup, monkeypatch):
    from revise_helpers import question

    ctx, plan, _ = setup(transport=ReviseTransport(lambda c: answer(ambiguous=[question()])))
    res = ask(ctx, "make it better")
    assert res.status == Status.PARTIAL and res.summary["outcome"] == "needs_clarification" and ctx.data["revise_clarification"] is True
    assert not ctx.manifest.get("revise", {}).get("pending")


def test_a_dry_run_is_never_a_cache_hit(setup):
    ctx, plan, t = setup(transport=ReviseTransport(lambda c: answer([op("trim_clip", clip_instance_id="c1", duration_seconds=4.0)])))
    ctx.data["revise"] = {"request": "shorter", "dry_run": True}
    a = rs.revise_inputs(ctx)
    assert a != rs.revise_inputs(ctx)         # a fresh nonce: the runner can never match an earlier record
    ctx.data["revise"] = {"request": "shorter"}
    assert rs.revise_inputs(ctx) == rs.revise_inputs(ctx)


# ---- 7: dropped transitions ---------------------------------------------------------------------------------------------------

def test_moving_a_scene_whose_first_clip_dissolves_notes_the_dropped_transition():
    cat = make_catalog()
    a0, a1 = (mid(cat, "day1/a.mp4", i) for i in range(2))
    p0, p1 = (mid(cat, "day2/p.mp4", i) for i in range(2))
    sel = sel_plan([sel_clip("clip_001", a0, 5), sel_clip("clip_002", a1, 4)], [sel_clip("clip_004", p0, 3.5, audio="muted", dissolve=0.5), sel_clip("clip_005", p1, 3)])
    sel = sel.model_copy(update={"schema_version": "1.1", "titles": []})
    ps = PlanSettings(frame_rate="30", target_duration_seconds=18.5)
    vs = ValidationSettings(frame_rate="30", target_duration_seconds=18.5, duration_tolerance_pct=60, duration_tolerance_seconds=0)
    edit = resolve_selection_plan(sel, cat, ps)
    assert next(c for s in edit.scenes for c in s.clips if c.clip_instance_id == "clip_004").transition_in.type == "dissolve"
    res = apply_ops(edit, [PlanOp.from_dict({"op": "move_scene", "scene_id": "scene_002", "to_position": 1}, REVISION_OPERATIONS),
                           PlanOp.from_dict({"op": "trim_clip", "clip_instance_id": "clip_001", "duration_seconds": 4.5}, REVISION_OPERATIONS)],
                    PlanContext(cat, vs, ps), base_selection=sel)
    assert len(res.applied) == 2
    assert res.applied[0].notes[-1] == "transition dropped: the dissolve into clip_004 became a cut (its neighbours changed)"
    assert not any(n.startswith("transition dropped") for n in res.applied[1].notes)
    doc = {"revision": "revise_r01", "status": "applied", "request": "x", "ops_applied": [a.to_dict() for a in res.applied]}
    assert "transition dropped: the dissolve into clip_004" in rev.render_markdown(doc)


# ---- 8: revision numbers --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [rc.REVISE, rc.CRITIC])
def test_revision_numbers_grow_past_99(tmp_path, kind):
    ctx = type("C", (), {})()
    ctx.paths = type("P", (), {})()
    ws = Workspace(tmp_path / "ws")
    ctx.workspace = ws
    ctx.config = revise_config()
    ctx.paths.outputs = {"edit_plan_file": ws.root / "plans" / "edit_plan.json"}
    d = ws.root / "plans" / "revisions"
    d.mkdir(parents=True)
    assert rc.next_number(ctx, kind) == 1
    for n, want in ((98, 99), (99, 100), (100, 101)):
        (d / f"{kind.tag(n)}.json").write_text("{}", encoding="utf-8")
        (d / f"{kind.tag(n)}.parent_edit_plan.json").write_text("{}", encoding="utf-8")
        assert rc.next_number(ctx, kind) == want
    assert kind.tag(100) == f"{kind.prefix}100"


# ---- 9: the critic after a revision that made QC worse; the selection plan is an input ----------------------------------------------

def test_a_regressed_revision_stops_the_critic_from_changing_the_plan_on_its_own(setup, monkeypatch):
    ctx, plan, _ = setup(transport=ReviseTransport(lambda c: answer([op("trim_clip", clip_instance_id="c1", duration_seconds=4.0)])))
    stub_rebuild(monkeypatch, qc_failed=["total_duration"])
    assert ask(ctx, "shorter").status == Status.PARTIAL
    h = current_plan(ctx).plan_hash
    rec = ctx.manifest.get("revise")["rounds"][-1]
    assert rec["halt"] == "qc_regression" and "no automatic critic round is applied on top of it" in rec["halt_note"] and "no further revisions" not in rec["halt_note"]
    assert cs._halting_round(ctx, h, [])["halt"] == "qc_regression"
    assert cs._halting_round(ctx, "some-other-hash", []) is None


def test_the_selection_plan_is_part_of_the_stage_inputs(setup):
    ctx, plan, _ = setup(transport=ReviseTransport())
    ctx.data["revise"] = {"request": "shorter"}
    before = rs.revise_inputs(ctx)
    sp = ds._pp(ctx).selection_plan
    sp.write_text(json.dumps({**json.loads(sp.read_text(encoding="utf-8")), "note": "edited"}), encoding="utf-8")
    assert rs.revise_inputs(ctx)["selection_plan"] != before["selection_plan"]


def test_without_a_revision_on_top_the_critic_uses_only_its_own_lineage(setup):
    ctx, plan, _ = setup(transport=ReviseTransport())
    old = {"round": 1, "status": "applied", "plan_hash": "P", "parent_plan_hash": "Q", "halt": "qc_regression", "at": "2026-01-01T00:00:00Z"}
    ctx.manifest.set(critic={"rounds": [old]})
    assert cs._halting_round(ctx, "P", []) is None          # a plan back on an old round's output: only the critic's own `done` counts
    assert cs._halting_round(ctx, "P", [old]) is old


def test_asking_for_the_critic_lifts_a_halt_left_by_a_revision(setup, monkeypatch):
    from trip_vlog import orchestrator as orch

    ctx, plan, _ = setup(transport=ReviseTransport(lambda c: answer([op("trim_clip", clip_instance_id="c1", duration_seconds=4.0)])))
    stub_rebuild(monkeypatch, qc_failed=["total_duration"])
    ask(ctx, "shorter")
    assert ctx.manifest.get("revise")["rounds"][-1]["halt"] == "qc_regression"
    ctx.manifest.save()
    seen = {}
    monkeypatch.setattr(orch, "_execute", lambda *a, **k: seen.setdefault("ran", True))
    orch.run_critic(ctx.workspace.root, None, {}, mode="apply", echo=lambda *_: None)
    rec = Manifest.load(ctx.workspace.manifest_path).get("revise")["rounds"][-1]
    assert seen["ran"] and rec["halt"] is None and rec["halt_lifted"] == "qc_regression"
