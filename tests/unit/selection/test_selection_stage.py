"""The `plan` stage of the llm draft: approval flow, derived edit-plan approval, cache key."""
from __future__ import annotations

import json

import pytest
from selection_helpers import PlannerTransport, make_plan_ctx, plan_runner, planner_config

from trip_vlog import outline_stages as os_
from trip_vlog.logging_utils import Status
from trip_vlog.planning import outline as ol

S = Status


@pytest.fixture(autouse=True)
def _reset():
    yield
    os_.gateway_factory = None


def setup(tmp_path, cfg=None):
    ctx, tr, ids = make_plan_ctx(tmp_path, cfg or planner_config())
    return ctx, tr, plan_runner(ctx)


def outline_path(ctx):
    return ctx.workspace.root / "plans" / "story_outline.json"


def approve(ctx):
    os_.approve_current_outline(ctx.manifest, ol.outline_file_hash(outline_path(ctx)), "manual")


def test_outline_stop_then_approve_then_plan_then_derived_edit_plan_approval(tmp_path):
    ctx, tr, r = setup(tmp_path)
    st = r.run()
    assert st["outline_approval"] == S.AWAITING_APPROVAL and st["plan"] == S.SKIPPED and st["approval"] == S.SKIPPED
    assert len(tr.outline_calls) == 1 and tr.plan_calls == [] and not (ctx.workspace.root / "plans" / "edit_plan.json").exists()
    assert ctx.manifest.stage("plan")["decision"] == "awaiting_approval"
    approve(ctx)
    st = plan_runner(ctx).run()
    assert st["plan"] == S.SUCCEEDED and st["approval"] == S.SUCCEEDED and len(tr.plan_calls) == 1 and len(tr.outline_calls) == 1
    ap = ctx.manifest.get("approval")
    plan = ctx.manifest.get("plan")
    assert ap["mode"] == "derived_from_outline" and ap["outline_approval_mode"] == "manual" and ap["outline_hash"] == plan["outline_hash"]
    st = plan_runner(ctx).run()                                                      # nothing recomputed
    assert ctx.manifest.stage("plan")["decision"] == "cache_hit" and len(tr.plan_calls) == 1 and st["approval"] == S.SUCCEEDED


def test_auto_approve_records_automatic_for_both(tmp_path):
    ctx, tr, r = setup(tmp_path, planner_config(workflow={"approval": "auto_approve"}))
    st = r.run()
    assert st["approval"] == S.SUCCEEDED and ctx.manifest.get("approval")["mode"] == "automatic"
    assert ctx.manifest.get("outline_approval")["mode"] == "automatic"


def test_plan_review_stops_at_the_edit_plan(tmp_path):
    ctx, tr, r = setup(tmp_path, planner_config(workflow={"plan_review": True}))
    r.run()
    approve(ctx)
    st = plan_runner(ctx).run()
    assert st["plan"] == S.SUCCEEDED and st["approval"] == S.AWAITING_APPROVAL


def test_hand_edited_edit_plan_needs_its_own_approval(tmp_path):
    ctx, tr, r = setup(tmp_path)
    r.run()
    approve(ctx)
    plan_runner(ctx).run()
    p = ctx.workspace.root / "plans" / "edit_plan.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["scenes"][0]["clips"][0]["rationale"] = "edited by hand"
    p.write_text(json.dumps(d), encoding="utf-8")
    st = plan_runner(ctx).run()
    assert st["plan"] == S.SKIPPED and st["approval"] == S.AWAITING_APPROVAL and len(tr.plan_calls) == 1


def test_editing_the_outline_replans_after_reapproval(tmp_path):
    ctx, tr, r = setup(tmp_path)
    r.run()
    approve(ctx)
    plan_runner(ctx).run()
    d = json.loads(outline_path(ctx).read_text(encoding="utf-8"))
    d["scenes"][1]["title"] = "Edited by hand"
    outline_path(ctx).write_text(json.dumps(d), encoding="utf-8")
    st = plan_runner(ctx).run()
    assert st["outline_approval"] == S.AWAITING_APPROVAL and st["plan"] == S.SKIPPED and len(tr.plan_calls) == 1
    approve(ctx)
    st = plan_runner(ctx).run()
    assert st["plan"] == S.SUCCEEDED and ctx.manifest.stage("plan")["decision"] == "ran" and len(tr.plan_calls) == 2
    assert "Edited by hand" in (ctx.workspace.root / "plans" / "plan_summary.md").read_text(encoding="utf-8")


def test_changing_the_model_replans(tmp_path):
    ctx, tr, r = setup(tmp_path, planner_config(workflow={"approval": "auto_approve"}))
    r.run()
    assert len(tr.plan_calls) == 1
    ctx.config = planner_config(workflow={"approval": "auto_approve"}, llm={"endpoints": {"main": {"api": "anthropic_messages", "model": "other-test-model"}}})
    st = plan_runner(ctx).run()
    assert st["plan"] == S.SUCCEEDED and ctx.manifest.stage("plan")["decision"] == "ran" and len(tr.plan_calls) == 2 and len(tr.outline_calls) == 2


def test_changing_the_target_duration_makes_a_new_outline_and_plan(tmp_path):
    ctx, tr, r = setup(tmp_path, planner_config(workflow={"approval": "auto_approve"}))
    r.run()
    ctx.config = planner_config(project={"target_duration_seconds": 24}, workflow={"approval": "auto_approve"})
    st = plan_runner(ctx).run()
    assert st["plan"] == S.SUCCEEDED and len(tr.outline_calls) == 2 and len(tr.plan_calls) == 2
    assert abs(json.loads((ctx.workspace.root / "plans" / "edit_plan.json").read_text(encoding="utf-8"))["total_duration_seconds"] - 24) <= 2


def test_changing_title_settings_replans_but_not_the_outline(tmp_path):
    ctx, tr, r = setup(tmp_path, planner_config(workflow={"approval": "auto_approve"}))
    r.run()
    ctx.config = planner_config(editorial={"opening_title": True}, workflow={"approval": "auto_approve"})
    st = plan_runner(ctx).run()
    assert st["plan"] == S.SUCCEEDED and len(tr.outline_calls) == 1
    assert ctx.manifest.stage("plan")["decision"] == "ran" and len(tr.plan_calls) == 1      # same request: the stored reply is reused
    assert [t["role"] for t in json.loads((ctx.workspace.root / "plans" / "edit_plan.json").read_text(encoding="utf-8"))["titles"]] == ["opening_title"]


def test_invalid_outline_fails_the_plan_stage_clearly(tmp_path):
    ctx, tr, r = setup(tmp_path, planner_config(workflow={"approval": "auto_approve"}))
    r.run()
    d = json.loads(outline_path(ctx).read_text(encoding="utf-8"))
    d["scenes"][0]["candidate_moment_ids"] = ["moment_ffffffffffffffff"]
    outline_path(ctx).write_text(json.dumps(d), encoding="utf-8")
    st = plan_runner(ctx).run()
    assert st["outline_approval"] == S.FAILED and st["plan"] == S.SKIPPED
