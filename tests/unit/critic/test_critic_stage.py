"""The critic stage: report mode, apply mode (rebuild stubbed), approvals, plan_review, caching, errors (fake endpoint, no FilmCraft, no ffmpeg)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from critic_helpers import (A2, CriticTransport, change, critic_config, finding, gateway_for, make_draft_ctx, reply, stub_rebuild)

from trip_vlog import critic_stages as cs
from trip_vlog import draft_stages as ds
from trip_vlog.llm.transport import HttpResponse
from trip_vlog.logging_utils import Status
from trip_vlog.orchestrator import ApprovalRefused, approve_critic, approve_pending
from trip_vlog.planning.models import EditPlan, edit_plan_hash, with_hash
from trip_vlog.state import Manifest, StageRunner, atomic_write_json


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def make(qc=None, workflow=None, transport=None, **kw):
        cfg = critic_config(qc, workflow, **kw)
        ctx, sel, plan = make_draft_ctx(tmp_path, monkeypatch, cfg)
        t = transport or CriticTransport()
        monkeypatch.setattr(cs, "gateway_factory", lambda c: gateway_for(c.config, c.workspace.root, t))
        return ctx, plan, t
    return make


def plan_files(ctx):
    pp = ds._pp(ctx)
    return {n: getattr(pp, n).read_bytes() for n in ("edit_plan", "selection_plan")}


def proposals(call):
    return {"summary": "Two small things.", "findings": [
        finding(severity="major", category="pacing", summary="The opening clip drags.", scenes=["scene_001"], clips=["c1"],
                evidence=[{"kind": "plan", "ref": "c1", "note": "5 s"}, {"kind": "tile", "ref": call.tiles[0][0], "note": "first frame"}],
                proposed=change("trim_clip", clip_instance_id="c1", duration_seconds=4.0)),
        finding(severity="minor", category="story_clarity", summary="Mark the change of place.", clips=["c2"],
                evidence=[{"kind": "qc_check", "ref": "orientation", "note": ""}],
                proposed=change("set_transition", clip_instance_id="c2", transition_type="dissolve", transition_seconds=0.5))]}


# ---- report ---------------------------------------------------------------------------------------------------

def test_report_mode_writes_the_report_fills_the_qc_block_and_changes_nothing(setup):
    t = CriticTransport(proposals)
    ctx, plan, _ = setup(transport=t)
    before = plan_files(ctx)
    manifest_before = json.dumps(ctx.manifest.data["approval"], sort_keys=True)
    res = cs.critic_stage(ctx)
    assert res.status == Status.SUCCEEDED, res
    assert plan_files(ctx) == before and json.dumps(ctx.manifest.data["approval"], sort_keys=True) == manifest_before and not (ctx.workspace.root / "plans" / "revisions").exists()
    assert len(t.calls) == 1 and t.calls[0].images >= 1
    rep = rj(ctx.workspace.root / "qc" / "critic_report.json")
    assert rep["status"] == "ok" and rep["mode"] == "report" and rep["plan_hash"] == plan.plan_hash and rep["images"]["sent"] is True
    assert [f["id"] for f in rep["findings"]] == ["finding_01", "finding_02"] and rep["by_severity"] == {"info": 0, "minor": 1, "major": 1}
    assert [f["change"]["status"] for f in rep["findings"]] == ["valid", "valid"] and "would be applied with qc.critic: apply" in rep["findings"][0]["change"]["note"]
    assert rep["would_change"]["diff"]["total_delta_seconds"] == -1.0 and "c1: 5.00 s -> 4.00 s" in "\n".join(rep["would_change"]["lines"])
    assert rep["model"] == "test-model-resolved" and rep["requests"] == 1 and rep["usage"]["input_tokens"] == 2000
    md = (ctx.workspace.root / "qc" / "critic_report.md").read_text(encoding="utf-8")
    assert "# Critic report: ok" in md and "The opening clip drags." in md and "What would change" in md
    assert (ctx.workspace.root / "qc" / "critic_response.schema.json").is_file()
    qc = rj(ctx.workspace.root / "preview" / "qc_report.json")
    assert qc["critic"]["status"] == "ok" and qc["critic"]["findings"] == 2 and qc["critic"]["report"] == "qc/critic_report.json" and qc["critic"]["images_sent"] is True
    assert "## Critic" in (ctx.workspace.root / "preview" / "qc_report.md").read_text(encoding="utf-8")
    assert res.summary["report"] == "qc/critic_report.json" and res.summary["requests"] == 1 and res.summary["findings"] == 2


def test_without_images_the_critic_runs_text_only_and_the_report_says_so(setup):
    for qc, llm, why in (({"critic_images": False}, None, "qc.critic_images is false"),
                         (None, {"endpoints": {"main": {"api": "anthropic_messages", "model": "m", "supports_images": False}}}, "supports_images: false")):
        t = CriticTransport()
        ctx, plan, _ = setup(qc=qc, llm=llm, transport=t)
        res = cs.critic_stage(ctx)
        assert res.status == Status.SUCCEEDED and t.calls[0].images == 0
        rep = rj(ctx.workspace.root / "qc" / "critic_report.json")
        assert rep["images"] == {"sent": False, "sheets": [], "reason": rep["images"]["reason"], "tiles_with_image": 0} and why in rep["images"]["reason"]
        assert "text only" in (ctx.workspace.root / "qc" / "critic_report.md").read_text(encoding="utf-8")
        assert "NO images are supplied" in t.calls[0].texts[0] and res.summary["images_sent"] is False
        shutil.rmtree(ctx.workspace.root.parent)       # the next case starts clean


def test_findings_citing_nothing_supplied_are_dropped_and_listed(setup):
    t = CriticTransport(lambda call: {"summary": "", "findings": [
        finding(summary="I heard wind in clip 2.", clips=["c2"], evidence=[{"kind": "audio", "ref": "c2", "note": "heard"}]),
        finding(summary="Tile claim.", clips=["c1"], evidence=[{"kind": "tile", "ref": "tile_777", "note": ""}]),
        finding(summary="Fine claim.", clips=["c1"], evidence=[{"kind": "plan", "ref": "c1", "note": ""}])]})
    ctx, plan, _ = setup(transport=t)
    cs.critic_stage(ctx)
    rep = rj(ctx.workspace.root / "qc" / "critic_report.json")
    assert [f["summary"] for f in rep["findings"]] == ["Fine claim."]
    assert [d["summary"] for d in rep["dropped_findings"]] == ["I heard wind in clip 2.", "Tile claim."]
    assert "Dropped findings" in (ctx.workspace.root / "qc" / "critic_report.md").read_text(encoding="utf-8")


def test_an_unusable_reply_or_endpoint_error_never_fails_the_draft(setup):
    for out in (HttpResponse(500, {}, b'{"error":"boom"}'), reply({"summary": "x"}, stop="max_tokens"), reply({"nonsense": True})):
        t = CriticTransport(lambda call, out=out: out)
        ctx, plan, _ = setup(transport=t)
        before = plan_files(ctx)
        res = cs.critic_stage(ctx)
        assert res.status == Status.PARTIAL and res.error, res
        rep = rj(ctx.workspace.root / "qc" / "critic_report.json")
        assert rep["status"] == "error" and rep["error"] and "unaffected" in rep["reason"] and rep["findings"] == []
        assert rj(ctx.workspace.root / "preview" / "qc_report.json")["critic"]["status"] == "error"
        assert plan_files(ctx) == before
        shutil.rmtree(ctx.workspace.root.parent)


def test_a_missing_qc_report_is_a_partial_stage_not_a_failure(setup):
    ctx, plan, t = setup()
    (ctx.workspace.root / "preview" / "qc_report.json").unlink()
    res = cs.critic_stage(ctx)
    assert res.status == Status.PARTIAL and "QC report" in res.error and not t.calls


def test_off_does_nothing(setup):
    ctx, plan, t = setup(qc={"critic": "off"})
    res = cs.critic_stage(ctx)
    assert res.status == Status.SKIPPED and not t.calls and not (ctx.workspace.root / "qc" / "critic_report.json").exists()


def test_swap_is_unavailable_without_an_approved_outline(setup):
    t = CriticTransport(lambda call: {"summary": "", "findings": [finding(clips=["c2"], proposed=change("swap_moment", clip_instance_id="c2", moment_id=A2))]})
    ctx, plan, _ = setup(transport=t)
    cs.critic_stage(ctx)
    f = rj(ctx.workspace.root / "qc" / "critic_report.json")["findings"][0]
    assert f["change"]["status"] == "invalid" and "no" in f["change"]["note"] and "story outline" in f["change"]["note"]
    assert "None: this plan was not made from an approved story outline" in t.calls[0].texts[0]


def test_a_hand_edited_plan_is_reviewed_but_no_change_is_proposed_for_it(setup):
    ctx, plan, t = setup()
    t.respond = proposals
    doc = plan.model_dump(mode="json")
    c = doc["scenes"][0]["clips"][0]
    c["source_in_frame"] += 3
    c["source_out_frame"] += 3
    c["source_in_seconds"] += 0.1
    c["source_out_seconds"] += 0.1
    edited = with_hash(EditPlan.model_validate(doc))
    atomic_write_json(ds._pp(ctx).edit_plan, edited.model_dump(mode="json"))
    ctx.manifest.set(approval={"status": "approved", "mode": "manual", "plan_hash": edited.plan_hash})
    # the QC report and sheet index are of the plan before the edit; the critic reads what is on disk now
    from critic_helpers import make_index, write_qc
    idx, _ = make_index(edited, ctx.workspace.root)
    atomic_write_json(ctx.workspace.root / "qc" / "preview_sheet.json", idx.to_dict())
    write_qc(ctx.workspace.root, edited)
    res = cs.critic_stage(ctx)
    rep = rj(ctx.workspace.root / "qc" / "critic_report.json")
    assert res.status == Status.SUCCEEDED and [f["change"]["status"] for f in rep["findings"]] == ["invalid", "invalid"]
    assert any("no change can be made to this plan" in n for n in rep["notes"]) and rep["would_change"] is None


# ---- caching ----------------------------------------------------------------------------------------------------

def test_cache_key_covers_plan_qc_sheets_prompt_model_and_limits_and_nothing_else(setup, monkeypatch):
    ctx, plan, t = setup()
    k0 = cs.critic_inputs(ctx)
    assert cs.critic_inputs(ctx) == k0 and k0["prompt"]["system"]["version"] == 1 and k0["prompt"]["response_schema"] == "1" and k0["llm"]["model"] == "test-model"
    assert {"plan", "qc", "index", "request", "schema", "images", "llm", "limits", "mode", "max_tokens"} <= set(k0)

    def changed(mutate):
        ctx2, _, _ = setup_variant(mutate)
        return cs.critic_inputs(ctx2) != k0

    def setup_variant(mutate):
        shutil.rmtree(ctx.workspace.root.parent, ignore_errors=True)
        cfg = critic_config(**mutate)
        return make_draft_ctx(ctx.workspace.root.parent, monkeypatch, cfg)

    assert changed({"qc": {"critic": "apply"}}) and changed({"qc": {"critic_max_ops_per_round": 2}}) and changed({"qc": {"critic_images": False}})
    assert changed({"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "other-model"}}}})
    assert changed({"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model", "params": {"temperature": 0.1}}}}})
    assert not changed({"qc": {"critic_max_rounds": 1}})


def test_unchanged_inputs_make_no_request_and_no_new_report(setup):
    t = CriticTransport(proposals)
    ctx, plan, _ = setup(transport=t)

    def runner():
        r = StageRunner(ctx.manifest, ctx.logger, ctx)
        r.register("critic", cs.critic_stage, inputs=cs.critic_inputs, verify=cs.critic_verify)
        return r
    assert str(runner().run()["critic"]) == "succeeded" and len(t.calls) == 1
    stamp = (ctx.workspace.root / "qc" / "critic_report.json").read_bytes()
    assert str(runner().run()["critic"]) == "skipped" and ctx.manifest.stage("critic")["decision"] == "cache_hit" and len(t.calls) == 1
    assert (ctx.workspace.root / "qc" / "critic_report.json").read_bytes() == stamp
    # a QC re-run that wrote `not_run` again is noticed; the answer comes from the response cache, not from a new request
    ds_qc = rj(ctx.workspace.root / "preview" / "qc_report.json")
    ds_qc["critic"] = {"status": "not_run", "reason": "x"}
    atomic_write_json(ctx.workspace.root / "preview" / "qc_report.json", ds_qc)
    assert not cs.critic_verify(ctx)
    runner().run()
    assert len(t.calls) == 1 and rj(ctx.workspace.root / "preview" / "qc_report.json")["critic"]["status"] == "ok"
    # a response cache entry also answers a forced rerun (same request fingerprint)
    cs.critic_stage(ctx)
    assert len(t.calls) == 1


# ---- apply ----------------------------------------------------------------------------------------------------------

def apply_setup(setup, monkeypatch, **kw):
    calls = {"n": 0}

    def respond(call):
        calls["n"] += 1
        return proposals(call) if calls["n"] == 1 else {"summary": "Better.", "findings": []}
    t = CriticTransport(respond)
    ctx, plan, _ = setup(qc={"critic": "apply"}, transport=t, **kw)
    rebuild = stub_rebuild(monkeypatch)
    return ctx, plan, t, rebuild


def test_apply_writes_a_new_plan_version_revision_approval_and_rebuilds_in_order(setup, monkeypatch):
    ctx, plan, t, rebuild = apply_setup(setup, monkeypatch)
    old_plan_bytes = plan_files(ctx)["edit_plan"]
    res = cs.critic_stage(ctx)
    assert res.status == Status.SUCCEEDED, res
    root = ctx.workspace.root
    new = EditPlan.model_validate(rj(root / "plans" / "edit_plan.json"))
    assert new.plan_hash != plan.plan_hash and new.plan_hash == edit_plan_hash(new)
    c1 = next(c for s in new.scenes for c in s.clips if c.clip_instance_id == "c1")
    c2 = next(c for s in new.scenes for c in s.clips if c.clip_instance_id == "c2")
    assert c1.duration_frames == 120 and c2.transition_in.type == "dissolve" and new.selection_plan_hash != plan.selection_plan_hash
    # revision files, parent kept
    rev = rj(root / "plans" / "revisions" / "critic_r01.json")
    assert rev["status"] == "applied" and rev["parent_plan_hash"] == plan.plan_hash and rev["plan_hash"] == new.plan_hash and rev["authorization"] == "automatic"
    assert [o["op"]["op"] for o in rev["ops_applied"]] == ["trim_clip", "set_transition"] and rev["diff"]["total_delta_seconds"] == -1.0
    md = (root / "plans" / "revisions" / "critic_r01.md").read_text(encoding="utf-8")
    assert "c1: 5.00 s -> 4.00 s" in md and "dissolve of 0.5 s into c2" in md and "parent plan" in md
    assert (root / "plans" / "revisions" / "critic_r01.parent_edit_plan.json").read_bytes() == old_plan_bytes
    assert rj(root / "plans" / "revisions" / "critic_r01.edit_plan.json")["plan_hash"] == new.plan_hash
    assert (root / "preview" / "draft.before_critic_r01.mp4").read_bytes() == b"not a real video"
    assert "critic_fix" in (root / "plans" / "plan_summary.md").read_text(encoding="utf-8")
    # approval
    ap = ctx.manifest.get("approval")
    snap = root / "qc" / "critic_report.r01.json"
    assert ap["status"] == "approved" and ap["mode"] == "critic_fix" and ap["plan_hash"] == new.plan_hash and ap["parent_plan_hash"] == plan.plan_hash
    assert ap["revision"] == "plans/revisions/critic_r01.json" and ap["critic_report"] == "qc/critic_report.r01.json" and ap["parent_approval"]["mode"] == "manual"
    import hashlib
    assert ap["critic_report_sha256"] == hashlib.sha256(snap.read_bytes()).hexdigest() and rj(snap)["plan_hash"] == plan.plan_hash
    # rebuild in order, on the new plan, then a second review of the final version
    assert [c[0] for c in rebuild] == ["assemble", "preview", "qc"] and {c[1] for c in rebuild} == {new.plan_hash}
    assert len(t.calls) == 2 and "record 0.00-4.00 s (4.00 s)" in t.calls[1].texts[0]
    for name in ("assemble", "preview", "qc"):
        st = ctx.manifest.stage(name)
        assert st["status"] == "succeeded" and st["ran_by"] == "critic" and st["input_hash"]
    state = ctx.manifest.get("critic")
    assert len(state["rounds"]) == 1 and state["rounds"][0]["status"] == "applied" and state["rounds"][0]["project"].endswith("_v002.fcproj") and state["pending"] is None
    rep = rj(root / "qc" / "critic_report.json")
    assert rep["plan_hash"] == new.plan_hash and rep["findings"] == [] and rep["rounds"][0]["revision"] == "plans/revisions/critic_r01.json"
    assert "round limit" in rep["reason"] and rep["requests"] == 2
    assert rj(root / "preview" / "qc_report.json")["critic"]["rounds"] == 1
    assert res.summary["rounds"] == 1 and res.summary["changes_applied"] == 2 and res.summary["findings"] == 0   # the final review proposes nothing
    # the approval stage keeps the critic_fix record (also under auto_approve)
    ctx.config.workflow.approval = "auto_approve"
    out = ds.approval_stage(ctx)
    assert out.summary["mode"] == "critic_fix" and ctx.manifest.get("approval")["mode"] == "critic_fix"


def test_a_second_run_after_an_applied_round_is_a_cache_hit_and_makes_no_request(setup, monkeypatch):
    ctx, plan, t, rebuild = apply_setup(setup, monkeypatch)

    def runner():
        r = StageRunner(ctx.manifest, ctx.logger, ctx)
        r.register("critic", cs.critic_stage, inputs=cs.critic_inputs, verify=cs.critic_verify)
        return r
    assert str(runner().run()["critic"]) == "succeeded" and len(t.calls) == 2 and len(rebuild) == 3
    assert str(runner().run()["critic"]) == "skipped" and ctx.manifest.stage("critic")["decision"] == "cache_hit"
    assert len(t.calls) == 2 and len(rebuild) == 3


def test_max_rounds_and_ops_are_respected(setup, monkeypatch):
    n = {"i": 0}

    def respond(call):
        n["i"] += 1
        return {"summary": "", "findings": [finding(severity="major", clips=["c3"], proposed=change("set_clip_audio", clip_instance_id="c3", audio="lower")) if n["i"] == 1 else
                                            finding(severity="major", clips=["c4"], proposed=change("set_clip_audio", clip_instance_id="c4", audio="keep"))]}
    t = CriticTransport(respond)
    ctx, plan, _ = setup(qc={"critic": "apply", "critic_max_rounds": 2}, transport=t)
    rebuild = stub_rebuild(monkeypatch)
    cs.critic_stage(ctx)
    assert n["i"] == 3 and len(ctx.manifest.get("critic")["rounds"]) == 2 and [c[0] for c in rebuild].count("assemble") == 2
    assert [p.name for p in sorted((ctx.workspace.root / "plans" / "revisions").glob("critic_r0?.json"))] == ["critic_r01.json", "critic_r02.json"]
    last = cs.lineage(ctx.manifest.get("critic"), ds.plan_file_hash(ds._pp(ctx).edit_plan))
    assert [r["round"] for r in last] == [1, 2] and last[1]["parent_plan_hash"] == last[0]["plan_hash"]


def test_apply_with_nothing_valid_changes_nothing(setup, monkeypatch):
    t = CriticTransport(lambda call: {"summary": "", "findings": [finding(clips=["c1"], proposed=change("trim_clip", clip_instance_id="c1", duration_seconds=99))]})
    ctx, plan, _ = setup(qc={"critic": "apply"}, transport=t)
    rebuild = stub_rebuild(monkeypatch)
    before = plan_files(ctx)
    res = cs.critic_stage(ctx)
    assert res.status == Status.SUCCEEDED and rebuild == [] and plan_files(ctx) == before and ctx.manifest.get("approval")["mode"] == "manual"
    assert rj(ctx.workspace.root / "qc" / "critic_report.json")["findings"][0]["change"]["status"] == "invalid"


def test_a_failed_rebuild_puts_the_previous_plan_back_and_keeps_the_proposed_one(setup, monkeypatch):
    t = CriticTransport(proposals)
    ctx, plan, _ = setup(qc={"critic": "apply"}, transport=t)
    stub_rebuild(monkeypatch, fail_assemble="FilmCraft exploded")
    before = plan_files(ctx)
    summary_before = (ctx.workspace.root / "plans" / "plan_summary.md").read_bytes() if (ctx.workspace.root / "plans" / "plan_summary.md").exists() else None
    res = cs.critic_stage(ctx)
    assert res.status == Status.PARTIAL and "FilmCraft exploded" in res.error
    assert plan_files(ctx) == before and ctx.manifest.get("approval")["mode"] == "manual" and ctx.manifest.get("approval")["plan_hash"] == plan.plan_hash
    root = ctx.workspace.root
    rev = rj(root / "plans" / "revisions" / "critic_r01.json")
    assert rev["status"] == "failed" and "put back" in rev["note"] and (root / "plans" / "revisions" / "critic_r01.edit_plan.json").is_file()
    assert not ctx.manifest.get("critic", {}).get("rounds")
    assert summary_before is None or (root / "plans" / "plan_summary.md").read_bytes() == summary_before
    assert rj(root / "qc" / "critic_report.json")["reason"].startswith("the new FilmCraft project could not be built")
    # the same revision is not built (and does not fail) again on the next run; the report says why
    calls_before = len(t.calls)
    res = cs.critic_stage(ctx)
    assert res.status == Status.SUCCEEDED and len(t.calls) == calls_before
    assert not (root / "plans" / "revisions" / "critic_r02.json").exists()
    assert [f["change"]["status"] for f in rj(root / "qc" / "critic_report.json")["findings"]] == ["refused", "refused"]


# ---- plan_review ------------------------------------------------------------------------------------------------

def test_plan_review_stops_for_approval_then_applies_exactly_the_approved_revision(setup, monkeypatch):
    ctx, plan, t, rebuild = apply_setup(setup, monkeypatch, workflow={"plan_review": True})
    root = ctx.workspace.root
    (root / "project.yaml").write_text(ctx.config.to_yaml(), encoding="utf-8")
    before = plan_files(ctx)
    res = cs.critic_stage(ctx)
    assert res.status == Status.AWAITING_APPROVAL and rebuild == [] and plan_files(ctx) == before
    pend = ctx.manifest.get("critic")["pending"]
    assert pend["status"] == "pending" and pend["parent_plan_hash"] == plan.plan_hash and pend["revision"] == "plans/revisions/critic_r01.json"
    rev = rj(root / pend["revision"])
    assert rev["status"] == "proposed" and rev["authorization"] is None and len(rev["ops_applied"]) == 2
    assert res.summary["revision"] == pend["revision"] and ctx.manifest.get("approval")["mode"] == "manual"
    assert rj(root / "qc" / "critic_report.json")["findings"][0]["change"]["status"] == "pending"
    # asking again changes nothing (same revision number, no new request: the response cache answers)
    assert cs.critic_stage(ctx).status == Status.AWAITING_APPROVAL and len(t.calls) == 1 and ctx.manifest.get("critic")["pending"]["number"] == 1
    assert len(list((root / "plans" / "revisions").glob("critic_r*.json"))) == 1
    # approve
    ctx.manifest.stages["critic"] = {"status": "awaiting_approval"}      # what the runner records
    ctx.manifest.save()
    assert cs.critic_inputs(ctx)["pending"] == ["pending", pend["revision_hash"]]
    with pytest.raises(ApprovalRefused, match="not a trip-vlog workspace"):
        approve_critic(root / "nowhere")
    kind, ap = approve_pending(root, None)
    assert kind == "critic" and ap["status"] == "approved" and ap["revision_hash"] == pend["revision_hash"]
    ctx.manifest.data = Manifest.load(ctx.manifest.path).data               # `approve` is another process
    assert cs.critic_inputs(ctx)["pending"] == ["approved", pend["revision_hash"]]
    res = cs.critic_stage(ctx)
    assert res.status == Status.SUCCEEDED, res
    ap = ctx.manifest.get("approval")
    assert ap["mode"] == "critic_fix" and ap["authorization"] == "manual" and ap["parent_plan_hash"] == plan.plan_hash
    assert rj(root / "plans" / "revisions" / "critic_r01.json")["status"] == "applied" and [c[0] for c in rebuild] == ["assemble", "preview", "qc"]
    assert ctx.manifest.get("critic")["pending"] is None


def test_approving_a_critic_revision_is_refused_without_one_or_after_the_plan_changed(setup, monkeypatch):
    ctx, plan, t, rebuild = apply_setup(setup, monkeypatch, workflow={"plan_review": True})
    root = ctx.workspace.root
    (root / "project.yaml").write_text(ctx.config.to_yaml(), encoding="utf-8")
    with pytest.raises(ApprovalRefused, match="no critic revision is waiting"):
        approve_critic(root)
    cs.critic_stage(ctx)
    p = ds._pp(ctx).edit_plan
    doc = rj(p)
    doc["uncertainties"] = ["edited by hand"]
    atomic_write_json(p, with_hash(EditPlan.model_validate(doc)).model_dump(mode="json"))
    with pytest.raises(ApprovalRefused, match="changed since the critic revision was proposed"):
        approve_critic(root)
