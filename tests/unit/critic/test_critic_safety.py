"""Safety rails of the critic apply loop: round counting, approved revisions, failures, interruption, QC regression, stale plans (stubbed rebuild)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from critic_helpers import CriticTransport, change, critic_config, finding, gateway_for, make_draft_ctx, stub_rebuild
from test_critic_stage import plan_files, proposals, rj

from trip_vlog import cli
from trip_vlog import critic_stages as cs
from trip_vlog import draft_stages as ds
from trip_vlog import orchestrator as orch
from trip_vlog.logging_utils import Status
from trip_vlog.planning.models import EditPlan, with_hash
from trip_vlog.orchestrator import approve_pending
from trip_vlog.state import atomic_write_json


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def make(qc=None, workflow=None, transport=None, **kw):
        cfg = critic_config({"critic": "apply", **(qc or {})}, workflow, **kw)
        ctx, sel, plan = make_draft_ctx(tmp_path, monkeypatch, cfg)
        t = transport or CriticTransport()
        monkeypatch.setattr(cs, "gateway_factory", lambda c: gateway_for(c.config, c.workspace.root, t))
        return ctx, plan, t
    return make


def proposals_once():
    """Two proposals for the first review, a clean bill of health for the review of the result."""
    n = {"i": 0}

    def respond(call):
        n["i"] += 1
        return proposals(call) if n["i"] == 1 else {"summary": "Better.", "findings": []}
    return respond


# ---- 1: the round limit cannot be bypassed ------------------------------------------------------------------------------

def test_lineage_counts_every_round_when_the_plan_returns_to_an_earlier_hash():
    rounds = [{"status": "applied", "round": i + 1, "parent_plan_hash": a, "plan_hash": b} for i, (a, b) in enumerate([("A", "B"), ("B", "A"), ("A", "B"), ("B", "A")])]
    state = {"rounds": rounds + [{"status": "failed", "round": 5, "parent_plan_hash": "A", "plan_hash": "C"}]}
    assert [r["round"] for r in cs.lineage(state, "A")] == [1, 2, 3, 4]
    assert cs.lineage(state, "B") == [] and cs.lineage(state, "Z") == []       # only the tip of the chain has a lineage and cs.seen_hashes(state) == {"A", "B"}


def test_a_critic_that_flips_a_setting_back_and_forth_is_stopped_at_the_second_flip(setup, monkeypatch):
    n = {"i": 0}

    def respond(call):
        n["i"] += 1
        if n["i"] > 12:
            raise RuntimeError("the loop did not stop")
        txt = "\n".join(call.texts)
        lowered = "sound ambient_low" in txt.split("c3 | ")[1].split("\n")[0]
        return {"summary": "", "findings": [finding(severity="major", clips=["c3"], proposed=change("set_clip_audio", clip_instance_id="c3", audio="keep" if lowered else "lower"))]}
    ctx, plan, t = setup(qc={"critic_max_rounds": 5}, transport=CriticTransport(respond))
    rebuild = stub_rebuild(monkeypatch)
    for _ in range(3):                                                    # run, resume, resume
        assert cs.critic_stage(ctx).status in (Status.SUCCEEDED, Status.PARTIAL)
    assert [c[0] for c in rebuild].count("assemble") == 1 and len(ctx.manifest.get("critic")["rounds"]) == 1
    notes = [f["change"]["note"] for f in rj(ctx.workspace.root / "qc" / "critic_report.json")["findings"]]
    assert any("return the plan to a version" in x for x in notes)


def test_the_run_never_applies_more_rounds_than_the_limit(setup, monkeypatch):
    i = {"n": 0}

    def respond(call):
        i["n"] += 1
        c = ["c1", "c2", "c3", "c4", "c5"][(i["n"] - 1) % 5]
        return {"summary": "", "findings": [finding(severity="major", clips=[c], proposed=change("set_clip_audio", clip_instance_id=c, audio="lower" if c != "c4" else "keep"))]}
    ctx, plan, _ = setup(qc={"critic_max_rounds": 2}, transport=CriticTransport(respond))
    rebuild = stub_rebuild(monkeypatch)
    cs.critic_stage(ctx)
    cs.critic_stage(ctx)
    assert [c[0] for c in rebuild].count("assemble") <= 2


# ---- 2: an approved revision is applied although qc.critic is off now -------------------------------------------------

def test_an_approved_revision_is_applied_by_resume_even_when_the_critic_is_off_in_the_config(setup, monkeypatch, capsys):
    ctx, plan, t = setup(workflow={"plan_review": True}, transport=CriticTransport(proposals_once()))
    rebuild = stub_rebuild(monkeypatch)
    root = ctx.workspace.root
    (root / "project.yaml").write_text(ctx.config.to_yaml(), encoding="utf-8")
    assert cs.critic_stage(ctx).status == Status.AWAITING_APPROVAL
    ctx.manifest.stages["critic"] = {"status": "awaiting_approval"}
    ctx.manifest.save()
    ctx.config.qc.critic = "off"                       # project.yaml says off when `resume` runs
    assert not cs.critic_wanted(ctx) and "critic" not in ds.build_draft_runner(ctx).names
    approve_pending(root, "critic")
    ctx.manifest.data = type(ctx.manifest).load(ctx.manifest.path).data
    capsys.readouterr()
    assert cli.main(["status", "--output", str(root)]) == 0
    assert "the critic revision is approved" in capsys.readouterr().out
    assert cs.effective_mode(ctx) == "apply" and "critic" in ds.build_draft_runner(ctx).names
    runner = ds.build_draft_runner(ctx)
    assert str(runner.run(only=["critic"])["critic"]) == "succeeded", ctx.manifest.stage("critic")
    new_hash = ds.plan_file_hash(ds._pp(ctx).edit_plan)
    assert new_hash != plan.plan_hash and ctx.manifest.get("approval")["mode"] == "critic_fix" and [c[0] for c in rebuild] == ["assemble", "preview", "qc"]
    assert ctx.manifest.get("critic")["pending"] is None
    assert not cs.critic_wanted(ctx)                   # done: with the config off the stage is not registered again


def test_an_approval_for_another_plan_does_not_switch_the_critic_on(setup):
    ctx, plan, _ = setup(workflow={"plan_review": True})
    ctx.config.qc.critic = "off"
    ctx.manifest.set(critic={"pending": {"status": "approved", "parent_plan_hash": "somebody else's plan", "number": 1, "revision_hash": "x"}})
    assert cs.effective_mode(ctx) == "off"


# ---- 3: only a rejected plan is remembered as failed -------------------------------------------------------------------

def test_a_failure_of_the_environment_is_not_remembered_and_the_next_run_retries(setup, monkeypatch):
    t = CriticTransport(proposals_once())
    ctx, plan, _ = setup(transport=t)
    stub_rebuild(monkeypatch, fail_assemble="FileNotFoundError: filmcraft-cli not found")
    before = plan_files(ctx)
    res = cs.critic_stage(ctx)
    assert res.status == Status.PARTIAL and plan_files(ctx) == before and not ctx.manifest.get("critic").get("failed")
    rebuild = stub_rebuild(monkeypatch)                                    # the CLI is installed now
    assert cs.critic_stage(ctx).status == Status.SUCCEEDED
    assert [c[0] for c in rebuild] == ["assemble", "preview", "qc"] and ds.plan_file_hash(ds._pp(ctx).edit_plan) != plan.plan_hash


def test_a_rejected_plan_is_remembered_and_trip_vlog_critic_clears_the_list(setup, monkeypatch):
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    stub_rebuild(monkeypatch, fail_assemble="plan rejected", reject_plan=True)
    cs.critic_stage(ctx)
    assert len(ctx.manifest.get("critic")["failed"]) == 1
    key = cs.critic_inputs(ctx)
    assert key["failed"] == ctx.manifest.get("critic")["failed"]
    root = ctx.workspace.root
    (root / "project.yaml").write_text(ctx.config.to_yaml(), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(orch, "_execute", lambda *a, **k: seen.setdefault("overrides", a[3]))
    orch.run_critic(root, None, None, mode="apply")
    assert seen["overrides"]["qc.critic"] == "apply"
    assert "failed" not in type(ctx.manifest).load(ctx.manifest.path).get("critic")
    ctx.manifest.data = type(ctx.manifest).load(ctx.manifest.path).data
    assert cs.critic_inputs(ctx) != key


# ---- 4: crash / Ctrl-C in the middle of a round ----------------------------------------------------------------------------

def test_ctrl_c_during_the_rebuild_puts_the_previous_state_back_and_is_not_swallowed(setup, monkeypatch):
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    stub_rebuild(monkeypatch, preview_raises=KeyboardInterrupt())
    before, approval = plan_files(ctx), dict(ctx.manifest.get("approval"))
    with pytest.raises(KeyboardInterrupt):
        cs.critic_stage(ctx)
    root = ctx.workspace.root
    assert plan_files(ctx) == before and ctx.manifest.get("approval") == approval
    st = ctx.manifest.get("critic")
    assert st["applying"] is None and not st.get("rounds") and not st.get("failed")
    assert rj(root / "plans" / "revisions" / "critic_r01.json")["status"] == "failed"
    assert (root / "preview" / "draft.mp4").read_bytes() == b"not a real video"


def test_a_round_that_died_is_rolled_back_at_the_next_start(setup, monkeypatch):
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    root = ctx.workspace.root
    stub_rebuild(monkeypatch, preview_raises=KeyboardInterrupt())
    before, approval = plan_files(ctx), dict(ctx.manifest.get("approval"))
    qc_before = (root / "preview" / "qc_report.json").read_bytes()
    real = cs._rollback
    monkeypatch.setattr(cs, "_rollback", lambda *a, **k: (_ for _ in ()).throw(OSError("killed while rolling back")))
    with pytest.raises(KeyboardInterrupt):      # the process "dies" before anything is put back; the interrupt still comes through and the marker stays
        cs.critic_stage(ctx)
    mk = ctx.manifest.get("critic")["applying"]
    assert mk["number"] == 1 and mk["parent_plan_hash"] == plan.plan_hash and mk["new_plan_hash"] != plan.plan_hash
    assert plan_files(ctx) != before and ctx.manifest.get("approval")["mode"] == "critic_fix"
    assert rj(root / "plans" / "revisions" / "critic_r01.json")["status"] == "applying"
    monkeypatch.setattr(cs, "_rollback", real)
    stub_rebuild(monkeypatch)
    note = cs.recover_interrupted(ctx)
    assert note and "interrupted" in note
    assert plan_files(ctx) == before and ctx.manifest.get("approval") == approval and ctx.manifest.get("critic")["applying"] is None
    assert (root / "preview" / "qc_report.json").read_bytes() == qc_before and (root / "preview" / "draft.mp4").read_bytes() == b"not a real video"
    rev = rj(root / "plans" / "revisions" / "critic_r01.json")
    assert rev["status"] == "rolled_back" and "rolled_back" in (root / "plans" / "revisions" / "critic_r01.md").read_text(encoding="utf-8")
    assert cs.recover_interrupted(ctx) is None


def test_the_stage_recovers_by_itself_and_then_runs_normally(setup, monkeypatch):
    ctx, plan, t = setup(transport=CriticTransport(proposals), qc={"critic": "report"})
    before = plan_files(ctx)
    ctx.manifest.set(critic={"applying": {"number": 1, "tag": "critic_r01", "parent_plan_hash": plan.plan_hash, "new_plan_hash": "n", "saved": {}}})
    assert cs.critic_stage(ctx).status == Status.SUCCEEDED
    assert ctx.manifest.get("critic")["applying"] is None and plan_files(ctx) == before
    assert any("interrupted" in n for n in rj(ctx.workspace.root / "qc" / "critic_report.json")["notes"])


# ---- 7: rollback covers the mapping report; files that did not exist are deleted ---------------------------------------------

@pytest.mark.parametrize("existed", [True, False])
def test_a_failed_round_restores_or_deletes_the_mapping_report(setup, monkeypatch, existed):
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    mp = ctx.workspace.root / "filmcraft" / "mapping_report.json"
    mp.parent.mkdir(exist_ok=True)
    if existed:
        mp.write_text('{"old": true}', encoding="utf-8")
    stub_rebuild(monkeypatch, fail_assemble="boom")
    cs.critic_stage(ctx)
    assert (mp.read_text(encoding="utf-8") == '{"old": true}') if existed else not mp.exists()


def test_a_parent_file_that_did_not_exist_is_deleted_on_rollback(setup, monkeypatch):
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    pp = ds._pp(ctx)
    assert not pp.summary.exists() and not pp.validation_report.exists()          # the fixture has no summary or validation report
    stub_rebuild(monkeypatch, fail_assemble="boom")
    cs.critic_stage(ctx)
    assert not pp.summary.exists() and not pp.validation_report.exists()          # _write_all wrote them for the new plan; they are gone again


# ---- 5: QC regression stops further rounds ----------------------------------------------------------------------------------

def _rounds_setup(setup, monkeypatch, **stub):
    i = {"n": 0}

    def respond(call):
        i["n"] += 1
        c, secs = [("c1", 4.5), ("c2", 3.5), ("c3", 2.5)][min(i["n"], 3) - 1]
        return {"summary": "", "findings": [finding(severity="major", clips=[c], proposed=change("trim_clip", clip_instance_id=c, duration_seconds=secs))]}
    ctx, plan, t = setup(qc={"critic_max_rounds": 3}, transport=CriticTransport(respond))
    return ctx, plan, stub_rebuild(monkeypatch, **stub)


def test_new_failing_checks_halt_further_rounds_without_reverting(setup, monkeypatch):
    ctx, plan, rebuild = _rounds_setup(setup, monkeypatch, qc_failed=["black_frames"])
    res = cs.critic_stage(ctx)
    assert res.status == Status.SUCCEEDED and [c[0] for c in rebuild].count("assemble") == 1
    rnd = ctx.manifest.get("critic")["rounds"][0]
    assert rnd["halt"] == "qc_regression" and rnd["qc_regressed"] is True and rnd["qc_new_failures"] == ["black_frames"] and rnd["status"] == "applied"
    assert ds.plan_file_hash(ds._pp(ctx).edit_plan) == rnd["plan_hash"] != plan.plan_hash        # not reverted
    assert res.summary["halt"] == "qc_regression" and "QC got worse after critic_r01" in res.summary["halt_note"] and "black_frames" in res.summary["halt_note"]
    rep = rj(ctx.workspace.root / "qc" / "critic_report.json")
    assert "QC got worse" in rep["reason"] and "WARNING" in (ctx.workspace.root / "qc" / "critic_report.md").read_text(encoding="utf-8")
    assert rj(ctx.workspace.root / "plans" / "revisions" / "critic_r01.json")["halt"] == "qc_regression"
    cs.critic_stage(ctx)                                                                         # a later run does not continue either
    assert [c[0] for c in rebuild].count("assemble") == 1


def test_a_check_that_already_failed_is_not_a_regression_but_a_new_one_next_to_it_is(setup, monkeypatch):
    for after, rounds in ((["total_duration"], 3), (["total_duration", "orientation"], 1)):
        ctx, plan, rebuild = _rounds_setup(setup, monkeypatch, qc_failed=after)
        qc = rj(ctx.workspace.root / "preview" / "qc_report.json")
        qc["status"] = "fail"
        qc["checks"][0]["status"] = "fail"                                                       # total_duration failed before the critic ran
        atomic_write_json(ctx.workspace.root / "preview" / "qc_report.json", qc)
        cs.critic_stage(ctx)
        assert [c[0] for c in rebuild].count("assemble") == rounds, after
        shutil.rmtree(ctx.workspace.root.parent)


def test_a_preview_that_fails_stops_the_rounds_and_qc_is_not_run_on_it(setup, monkeypatch):
    ctx, plan, rebuild = _rounds_setup(setup, monkeypatch, preview_status=Status.FAILED)
    res = cs.critic_stage(ctx)
    assert res.status == Status.PARTIAL and "preview" in res.error
    assert [c[0] for c in rebuild] == ["assemble", "preview"]
    rnd = ctx.manifest.get("critic")["rounds"][0]
    assert rnd["halt"] == "preview_failed" and rnd["qc_after"] is None
    assert res.summary["halt"] == "preview_failed"
    cs.critic_stage(ctx)
    assert [c[0] for c in rebuild].count("assemble") == 1


def test_a_qc_that_crashes_counts_as_a_regression_and_stops(setup, monkeypatch):
    ctx, plan, rebuild = _rounds_setup(setup, monkeypatch, qc_raises=True)
    res = cs.critic_stage(ctx)
    assert res.status == Status.PARTIAL and ctx.manifest.get("critic")["rounds"][0]["halt"] == "qc_failed" and [c[0] for c in rebuild].count("assemble") == 1


def test_the_cli_and_the_handoff_note_surface_a_regression(setup, monkeypatch, capsys):
    ctx, plan, rebuild = _rounds_setup(setup, monkeypatch, qc_failed=["black_frames"])
    res = cs.critic_stage(ctx)
    ctx.manifest.stages["critic"] = {"status": "succeeded", "summary": res.summary}
    ctx.manifest.stages["qc"] = {"status": "succeeded", "summary": {"status": "fail", "failed_checks": ["black_frames"]}}
    ctx.manifest.save()
    cli._print_draft_result(SimpleNamespace(workspace=ctx.workspace.root, run_id="r", stage_status={"critic": "succeeded"}, awaiting_approval=False,
                                            summary={"assets": 1, "probe_failed": 0, "stale": 0}))
    assert "WARNING: QC got worse after critic_r01" in capsys.readouterr().out
    rnd = ctx.manifest.get("critic")["rounds"][0]
    assert cs.lineage(ctx.manifest.get("critic"), rnd["plan_hash"])[0]["halt_note"].startswith("QC got worse")


# ---- 8: cache key ---------------------------------------------------------------------------------------------------------------

def test_the_cache_key_covers_validation_planning_still_length_and_the_failed_list(tmp_path, monkeypatch):
    def key(**kw):
        d = tmp_path / f"ws{len(list(tmp_path.iterdir()))}"     # a plain name: ':' and braces are not valid in Windows folder names
        d.mkdir()
        ctx, _, _ = make_draft_ctx(d, monkeypatch, critic_config(**kw))
        return ctx, cs.critic_inputs(ctx)
    ctx, k0 = key()
    assert {"validation", "planning", "min_still_seconds", "failed"} <= set(k0)
    assert key(qc={"min_still_seconds": 2.0})[1] != k0
    ctx.manifest.set(critic={"failed": ["abc"]})
    assert cs.critic_inputs(ctx)["failed"] == ["abc"] and cs.critic_inputs(ctx) != k0


# ---- 9: the plan on disk is re-checked before anything is written ---------------------------------------------------------

def test_a_plan_edited_after_the_review_aborts_the_round_without_changes(setup, monkeypatch):
    ctx, plan, t = setup()
    root = ctx.workspace.root

    def respond(call):
        doc = rj(ds._pp(ctx).edit_plan)                 # the person edits the plan while the critic is thinking
        doc["uncertainties"] = ["edited by hand"]
        atomic_write_json(ds._pp(ctx).edit_plan, with_hash(EditPlan.model_validate(doc)).model_dump(mode="json"))
        return proposals(call)
    t.respond = respond
    rebuild = stub_rebuild(monkeypatch)
    res = cs.critic_stage(ctx)
    edited = ds._pp(ctx).edit_plan.read_bytes()
    assert rebuild == [] and res.status == Status.PARTIAL and "changed meanwhile" in res.error
    assert json.loads(edited)["uncertainties"] == ["edited by hand"]
    assert not (root / "plans" / "revisions").exists() and not ctx.manifest.get("critic", {}).get("rounds") and not ctx.manifest.get("critic", {}).get("failed")
    assert ctx.manifest.get("critic", {}).get("applying") is None


# ---- follow-up review: recovery must not destroy what the person did since --------------------------------------------------

def _crashed_round(setup, monkeypatch, **stub):
    """A round that "died" in the middle: new plan and approval on disk, marker still set."""
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    stub_rebuild(monkeypatch, preview_raises=KeyboardInterrupt(), **stub)
    real = cs._rollback
    monkeypatch.setattr(cs, "_rollback", lambda *a, **k: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(KeyboardInterrupt):      # a restore that fails never hides the interrupt
        cs.critic_stage(ctx)
    monkeypatch.setattr(cs, "_rollback", real)
    assert ctx.manifest.get("critic")["applying"]
    return ctx, plan


def test_a_plan_edited_after_the_crash_is_kept_and_nothing_is_deleted(setup, monkeypatch):
    ctx, plan = _crashed_round(setup, monkeypatch)
    root, pp = ctx.workspace.root, ds._pp(ctx)
    doc = rj(pp.edit_plan)
    doc["uncertainties"] = ["edited by hand after the crash"]
    atomic_write_json(pp.edit_plan, with_hash(EditPlan.model_validate(doc)).model_dump(mode="json"))
    edited, approval = pp.edit_plan.read_bytes(), dict(ctx.manifest.get("approval"))
    mapping = root / "filmcraft" / "mapping_report.json"
    note = cs.recover_interrupted(ctx)
    assert "nothing was put back" in note and pp.edit_plan.read_bytes() == edited and ctx.manifest.get("approval") == approval
    assert mapping.is_file() and pp.summary.is_file()                       # files the round wrote are not deleted under a plan that is not the round's
    assert ctx.manifest.get("critic")["applying"] is None
    rev = rj(root / "plans" / "revisions" / "critic_r01.json")
    assert rev["status"] == "rolled_back" and "nothing was put back" in rev["note"]


def test_a_plan_approved_by_somebody_else_after_the_crash_keeps_its_approval(setup, monkeypatch):
    ctx, plan = _crashed_round(setup, monkeypatch)
    ctx.manifest.set(approval={"status": "approved", "mode": "manual", "plan_hash": "another"})
    cs.recover_interrupted(ctx)
    assert ctx.manifest.get("approval")["plan_hash"] == "another"


def test_a_marker_whose_plan_is_still_the_parent_only_cleans_up_what_the_round_wrote(setup, monkeypatch):
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    pp = ds._pp(ctx)
    before = plan_files(ctx)
    stub_rebuild(monkeypatch)
    ctx.manifest.set(critic={"applying": {"number": 1, "tag": "critic_r01", "parent_plan_hash": plan.plan_hash, "new_plan_hash": "n", "saved": {"edit_plan": None, "summary": None}}})
    pp.summary.write_text("the person's summary", encoding="utf-8")
    note = cs.recover_interrupted(ctx)
    assert "previous plan" in note and plan_files(ctx) == before and pp.summary.read_text(encoding="utf-8") == "the person's summary"


def test_recovery_forgets_the_input_hashes_of_the_stages_that_built_on_the_half_done_state(setup, monkeypatch):
    ctx, plan = _crashed_round(setup, monkeypatch)
    for name in ("assemble", "preview", "qc"):
        ctx.manifest.stages[name] = {"status": "succeeded", "input_hash": "built on the new plan"}
    cs.recover_interrupted(ctx)
    assert all(ctx.manifest.stages[n]["input_hash"] is None for n in ("assemble", "preview", "qc"))
    assert ds.plan_file_hash(ds._pp(ctx).edit_plan) == plan.plan_hash


def test_a_stale_marker_is_recovered_before_the_runner_starts_even_with_the_critic_off(setup, monkeypatch, tmp_path):
    ctx, plan = _crashed_round(setup, monkeypatch)
    ctx.config.qc.critic = "off"
    root = ctx.workspace.root
    (root / "project.yaml").write_text(ctx.config.to_yaml(), encoding="utf-8")
    seen = {}

    class Stop(Exception):
        pass

    def spy_runner(c):
        seen["applying"] = c.manifest.get("critic")["applying"]
        seen["plan"] = ds.plan_file_hash(ds._pp(c).edit_plan)
        raise Stop

    monkeypatch.setattr(ds, "build_draft_runner", spy_runner)
    with pytest.raises(Stop):
        orch.run_critic(root, None, {"qc.critic": "off"}, mode="report", echo=lambda *_: None)
    assert seen == {"applying": None, "plan": plan.plan_hash}


def test_an_interrupt_while_the_round_is_recorded_rolls_it_back(setup, monkeypatch):
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    stub_rebuild(monkeypatch)
    real = cs._write_revision

    def write(ctx_, n, doc, res):
        if doc["status"] == "applied":
            raise KeyboardInterrupt
        return real(ctx_, n, doc, res)
    monkeypatch.setattr(cs, "_write_revision", write)
    before = plan_files(ctx)
    with pytest.raises(KeyboardInterrupt):
        cs.critic_stage(ctx)
    st = ctx.manifest.get("critic")
    assert plan_files(ctx) == before and st["applying"] is None and not st.get("rounds")


def test_only_a_plan_rejection_is_remembered_not_a_failure_that_merely_has_errors(setup, monkeypatch):
    from trip_vlog.state import StageResult
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    monkeypatch.setattr(ds, "assemble_stage", lambda c: StageResult(Status.FAILED, {"errors": [{"code": "spec"}]}, "FilmCraft said no"))
    cs.critic_stage(ctx)
    assert not ctx.manifest.get("critic").get("failed")

    def rejected(c):
        raise ds.PlanRejected("bad plan")
    monkeypatch.setattr(ds, "assemble_stage", rejected)
    cs.critic_stage(ctx)
    assert len(ctx.manifest.get("critic")["failed"]) == 1


def _approved_with_critic_off(setup, monkeypatch, second):
    n = {"i": 0}

    def respond(call):
        n["i"] += 1
        return proposals(call) if n["i"] == 1 else second(call)
    ctx, plan, _ = setup(qc={"critic_max_rounds": 3}, workflow={"plan_review": True}, transport=CriticTransport(respond))
    rebuild = stub_rebuild(monkeypatch)
    (ctx.workspace.root / "project.yaml").write_text(ctx.config.to_yaml(), encoding="utf-8")
    assert cs.critic_stage(ctx).status == Status.AWAITING_APPROVAL
    approve_pending(ctx.workspace.root, "critic")
    ctx.manifest.data = type(ctx.manifest).load(ctx.manifest.path).data
    ctx.config.qc.critic = "off"
    return ctx, plan, rebuild


def test_after_the_approved_round_the_critic_goes_back_to_the_configured_mode(setup, monkeypatch):
    more = lambda call: {"summary": "", "findings": [finding(severity="major", clips=["c3"], proposed=change("trim_clip", clip_instance_id="c3", duration_seconds=2.5))]}   # noqa: E731
    ctx, plan, rebuild = _approved_with_critic_off(setup, monkeypatch, more)
    cs.critic_stage(ctx)
    assert [c[0] for c in rebuild].count("assemble") == 1 and cs.effective_mode(ctx) == "off"
    rep = rj(ctx.workspace.root / "qc" / "critic_report.json")
    assert rep["mode"] == "report" and rep["findings"][0]["change"]["status"] == "valid" and "would be applied" in rep["findings"][0]["change"]["note"]


def test_an_approval_for_a_revision_the_critic_no_longer_proposes_is_dropped(setup, monkeypatch):
    other = lambda call: {"summary": "", "findings": [finding(severity="major", clips=["c3"], proposed=change("trim_clip", clip_instance_id="c3", duration_seconds=2.5))]}   # noqa: E731
    ctx, plan, rebuild = _approved_with_critic_off(setup, monkeypatch, other)
    ctx.config.qc.critic = "off"
    # the review now differs from the one that was approved (e.g. another model or prompt): the approval does not carry over
    cs_state = ctx.manifest.get("critic")
    ctx.manifest.set(critic={**cs_state, "pending": {**cs_state["pending"], "revision_hash": "of another revision"}})
    cs.critic_stage(ctx)
    assert rebuild == [] and ctx.manifest.get("critic")["pending"] is None and cs.effective_mode(ctx) == "off"


def test_a_reformatted_plan_after_the_crash_is_the_persons_even_though_its_content_hash_is_unchanged(setup, monkeypatch):
    ctx, plan = _crashed_round(setup, monkeypatch)
    pp = ds._pp(ctx).edit_plan
    pp.write_bytes(pp.read_bytes() + b"\n")
    edited = pp.read_bytes()
    cs.recover_interrupted(ctx)
    assert pp.read_bytes() == edited and ctx.manifest.get("critic")["applying"] is None


def test_a_restore_that_fails_during_an_interrupt_still_raises_the_interrupt_and_keeps_the_marker(setup, monkeypatch):
    ctx, plan = _crashed_round(setup, monkeypatch)     # asserts the KeyboardInterrupt came through
    assert ctx.manifest.get("critic")["applying"]["parent_plan_hash"] == plan.plan_hash


def test_a_locked_file_during_recovery_does_not_stop_the_run_and_recovery_is_retried(setup, monkeypatch):
    ctx, plan = _crashed_round(setup, monkeypatch)
    real = cs._rollback
    monkeypatch.setattr(cs, "_rollback", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked by a player")))
    note = cs.recover_interrupted(ctx)
    assert "retried" in note and ctx.manifest.get("critic")["applying"]
    monkeypatch.setattr(cs, "_rollback", real)
    assert "previous plan" in cs.recover_interrupted(ctx)
    assert ctx.manifest.get("critic")["applying"] is None and ds.plan_file_hash(ds._pp(ctx).edit_plan) == plan.plan_hash


def test_a_plan_left_half_written_by_an_earlier_restore_is_recovered_not_kept(setup, monkeypatch):
    ctx, plan = _crashed_round(setup, monkeypatch)
    ds._pp(ctx).edit_plan.write_bytes(b'{"schema_ver')
    note = cs.recover_interrupted(ctx)
    assert "previous plan" in note and ds.plan_file_hash(ds._pp(ctx).edit_plan) == plan.plan_hash


def test_restoring_a_file_goes_through_a_temporary_copy(tmp_path, monkeypatch):
    src, target = tmp_path / "parent.json", tmp_path / "live.json"
    src.write_text("parent", encoding="utf-8")
    target.write_text("new", encoding="utf-8")

    def cut_off(a, b):
        Path(b).write_text("par", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(cs.shutil, "copyfile", cut_off)
    with pytest.raises(KeyboardInterrupt):
        cs._replace_from(src, target)
    assert target.read_text(encoding="utf-8") == "new" and sorted(p.name for p in tmp_path.iterdir()) == ["live.json", "parent.json"]


def test_a_selection_plan_the_round_created_is_removed_when_the_plan_is_still_the_parent(setup, monkeypatch):
    ctx, plan, _ = setup(transport=CriticTransport(proposals_once()))
    pp = ds._pp(ctx)
    d = ctx.workspace.root / "plans" / "revisions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "critic_r01.selection_plan.json").write_bytes(b'{"made": "by the round"}')
    pp.selection_plan.write_bytes(b'{"made": "by the round"}')
    ctx.manifest.set(critic={"applying": {"number": 1, "tag": "critic_r01", "parent_plan_hash": plan.plan_hash, "new_plan_hash": "n",
                                          "saved": {"edit_plan": "plans/revisions/critic_r01.parent_edit_plan.json", "selection_plan": None}}})
    cs.recover_interrupted(ctx)
    assert not pp.selection_plan.exists()


def test_the_recovery_note_is_not_left_in_the_manifest(setup, monkeypatch):
    ctx, plan = _crashed_round(setup, monkeypatch)
    cs.recover_interrupted(ctx)
    assert "recovered" not in (ctx.manifest.get("critic") or {})
