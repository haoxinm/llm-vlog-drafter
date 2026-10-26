"""Safety rails of the critic apply loop: round counting, approved revisions, failures, interruption, QC regression, stale plans (stubbed rebuild)."""
from __future__ import annotations

import json
import shutil
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
    assert cs.critic_stage(ctx).status == Status.PARTIAL        # the process "dies" before anything is put back (the stage reports it and leaves the marker)
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
        d = tmp_path / json.dumps(kw, sort_keys=True).replace("/", "_").replace('"', "")[:60]
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
