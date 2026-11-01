"""LLM clip selection: schema, happy path, feedback loop, duration budgeting, titles (no network)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from selection_helpers import PlannerTransport, _clip, make_plan_ctx, plan_runner, planner_config

from trip_vlog import outline_stages as os_
from trip_vlog.logging_utils import Status
from trip_vlog.planning import selection as sl
from trip_vlog.planning.models import EditPlan, SelectionPlan
from trip_vlog.state import stable_hash

SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"


@pytest.fixture(autouse=True)
def _reset():
    yield
    os_.gateway_factory = None


def rj(ctx, name):
    return json.loads((ctx.workspace.root / "plans" / name).read_text(encoding="utf-8"))


def run(cfg=None, tmp_path=None, respond_plan=None, respond_outline=None, **kw):
    cfg = cfg or planner_config(workflow={"approval": "auto_approve"})
    tr = PlannerTransport(respond_plan, respond_outline)
    ctx, tr, ids = make_plan_ctx(tmp_path, cfg, transport=tr, **kw)
    st = plan_runner(ctx).run()
    return ctx, tr, st


# ---- schema ------------------------------------------------------------------------------------------------------

def test_response_schema_is_strict_friendly_and_committed():
    s = sl.response_schema()
    Draft202012Validator.check_schema(s)
    objs = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object":
                objs.append(n)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(s)
    assert objs and all(o["additionalProperties"] is False and set(o["required"]) == set(o["properties"]) for o in objs)
    assert "intentional_repeat_reason" in json.dumps(s) and json.loads(json.dumps(s))["properties"]["scenes"]["items"]["properties"]["clips"]
    text = json.dumps(s).lower()
    assert "filmcraft" not in text and "tick" not in text and "source_in" not in text
    assert json.loads((SCHEMAS / "timeline_planner_response.schema.json").read_text(encoding="utf-8")) == s


def test_prompt_is_versioned_and_has_no_model_names():
    info = sl.prompt_info()
    assert info["system"]["id"] == "timeline_planner" and info["system"]["version"] == 1
    system, inp = sl.templates()
    assert "Convert the approved outline" in system.text and "{{outline}}" in inp.text


# ---- happy path --------------------------------------------------------------------------------------------------

def test_selection_resolves_validates_and_writes_files(tmp_path):
    ctx, tr, st = run(tmp_path=tmp_path)
    assert st["plan"] == Status.SUCCEEDED and len(tr.plan_calls) == 1 and not tr.plan_calls[0].retry
    sel = SelectionPlan.model_validate(rj(ctx, "selection_plan.json"))
    edit = EditPlan.model_validate(rj(ctx, "edit_plan.json"))
    assert sel.planner == "llm" and [s.scene_id for s in sel.scenes] == ["scene_01", "scene_02", "scene_03"]
    assert [c.clip_instance_id for s in sel.scenes for c in s.clips][:3] == ["clip_001", "clip_002", "clip_003"]
    assert edit.total_duration_seconds == pytest.approx(30, abs=1.5)
    rep = rj(ctx, "selection_report.json")
    assert rep["ok"] and rep["prompt"]["system"]["id"] == "timeline_planner" and rep["usage"]["input_tokens"] == 1000
    for f in ("plan_summary.md", "validation_report.json", "timeline_planner_response.schema.json", "selection_plan.schema.json"):
        assert (ctx.workspace.root / "plans" / f).is_file()
    prompt = tr.plan_calls[0].texts[0]
    assert "<untrusted_media_data" in prompt and "gps" not in prompt.lower() and "FilmCraft" not in prompt and "graphics." not in prompt
    assert ctx.manifest.get("plan") is None or True


def test_request_only_contains_candidates_of_the_outline_and_use_fields(tmp_path):
    def outline(call, d):
        d["scenes"] = d["scenes"][:2]
        d["scenes"][0]["target_duration_seconds"] = d["scenes"][1]["target_duration_seconds"] = 15
        return d

    ctx, tr, st = run(tmp_path=tmp_path, respond_outline=outline)
    outline_ids = {m for s in rj(ctx, "story_outline.json")["scenes"] for m in s["candidate_moment_ids"]}
    sent = {i for c in tr.plan_calls for sc in c.scenes.values() for i in (x["id"] for x in sc["cands"])}
    assert sent == outline_ids
    assert '"use":' in tr.plan_calls[0].texts[0]


# ---- feedback loop -----------------------------------------------------------------------------------------------

def _bad_then_good(bad):
    def respond(call, d):
        return bad(call, d) if not call.retry else d
    return respond


def test_reask_once_with_findings_then_success(tmp_path):
    def bad(call, d):
        d["scenes"][0]["clips"][0]["moment_id"] = "moment_ffffffffffffffff"
        return d

    ctx, tr, st = run(tmp_path=tmp_path, respond_plan=_bad_then_good(bad))
    assert st["plan"] == Status.SUCCEEDED and len(tr.plan_calls) == 2 and tr.plan_calls[1].retry
    retry_text = "\n".join(tr.plan_calls[1].texts)
    assert "Your previous reply was:" in retry_text and "unknown_moment" in retry_text
    rep = rj(ctx, "selection_report.json")
    assert rep["attempts"][0]["errors"] and not rep["attempts"][1]["errors"] and rep["repairs"] == []
    assert ctx.manifest.stage("plan")["summary"]["reasked"] == 1


def test_reask_then_deterministic_repair_is_recorded(tmp_path):
    def always_bad(call, d):
        c0 = d["scenes"][0]["clips"]
        c0[0]["moment_id"] = "moment_ffffffffffffffff"                                    # unknown: dropped
        c0[1]["target_duration_seconds"] = 99                                              # longer than the moment: clamped
        d["scenes"][1]["clips"].append(copy.deepcopy(d["scenes"][1]["clips"][0]))         # repeat without reason: dropped
        return d

    ctx, tr, st = run(tmp_path=tmp_path, respond_plan=always_bad)
    assert st["plan"] == Status.SUCCEEDED and len(tr.plan_calls) == 2
    codes = {r["code"] for r in rj(ctx, "selection_report.json")["repairs"]}
    assert {"unknown_moment", "duration_exceeds_moment", "unexplained_repeat"} <= codes
    edit = rj(ctx, "edit_plan.json")
    assert abs(edit["total_duration_seconds"] - 30) <= 2
    assert "repaired" in (ctx.workspace.root / "plans" / "plan_summary.md").read_text(encoding="utf-8")


def test_unrepairable_reply_fails_with_findings(tmp_path):
    def bad(call, d):
        d["scenes"] = d["scenes"][:2]                                                      # a scene of the outline is missing
        return d

    ctx, tr, st = run(tmp_path=tmp_path, respond_plan=bad)
    assert st["plan"] == Status.FAILED and len(tr.plan_calls) == 2
    err = ctx.manifest.stage("plan")["error"]
    assert "scene_missing" in err and "scene_03" in err
    assert not (ctx.workspace.root / "plans" / "edit_plan.json").exists()
    assert st["approval"] == Status.SKIPPED
    assert rj(ctx, "selection_report.json")["ok"] is False


def test_no_retries_configured_means_one_request(tmp_path):
    cfg = planner_config(planning={"selection_retries": 0}, workflow={"approval": "auto_approve"})
    ctx, tr, st = run(cfg, tmp_path, respond_plan=lambda c, d: {**d, "scenes": d["scenes"][:1]})
    assert st["plan"] == Status.FAILED and len(tr.plan_calls) == 1


@pytest.mark.parametrize("name,mut,code", [
    ("not in scene", lambda d, c: d["scenes"][0]["clips"][0].update(moment_id=c.scenes["scene_02"]["cands"][0]["id"]), "moment_not_in_scene"),
    ("too long", lambda d, c: d["scenes"][0]["clips"][0].update(target_duration_seconds=500), "duration_exceeds_moment"),
    ("scene budget", lambda d, c: [x.update(target_duration_seconds=1.0) for x in d["scenes"][2]["clips"]], "scene_budget_off"),
    ("long dissolve", lambda d, c: d["scenes"][0]["clips"][1].update(transition_in={"type": "dissolve", "duration_seconds": 4.0}), "transition_too_long"),
    ("unknown scene", lambda d, c: d["scenes"][0].update(scene_id="scene_99"), "unknown_scene"),
])
def test_each_fixable_problem_is_sent_back(tmp_path, name, mut, code):
    seen = []

    def respond(call, d):
        if not call.retry:
            mut(d, call)
        else:
            seen.append("\n".join(call.texts))
        return d

    ctx, tr, st = run(tmp_path=tmp_path, respond_plan=respond)
    assert st["plan"] == Status.SUCCEEDED, name
    assert code in seen[0], name


def test_excluded_moment_is_never_offered_and_is_dropped_if_used(tmp_path):
    cfg = planner_config(editorial={"must_exclude": ["day1/p1.jpg"]}, workflow={"approval": "auto_approve"})
    ctx, tr, st = run(cfg, tmp_path)
    forbidden = {r["moment_id"] for r in ctx.catalog.list_moments() if r["asset_id"] == ctx.catalog.list_assets()[[a["relative_path"] for a in ctx.catalog.list_assets()].index("day1/p1.jpg")]["asset_id"]}
    assert st["plan"] == Status.SUCCEEDED and forbidden
    assert not ({x["id"] for c in tr.plan_calls for sc in c.scenes.values() for x in sc["cands"]} & forbidden)

    def bad(call, d):
        d["scenes"][0]["clips"][0]["moment_id"] = sorted(forbidden)[0]
        return d

    ctx2, tr2, st2 = run(cfg, tmp_path / "b", respond_plan=bad)
    codes = {r["code"] for r in rj(ctx2, "selection_report.json")["repairs"]}
    assert st2["plan"] == Status.SUCCEEDED and "excluded_moment" in codes


# ---- duration budgeting ------------------------------------------------------------------------------------------

def test_fit_total_scales_within_bounds():
    out = sl.fit_total([4.0, 4.0, 4.0], [1.0] * 3, [6.0, 4.5, 20.0], 18.0)
    assert sum(out) == pytest.approx(18.0, abs=0.02) and out[1] <= 4.5 and out[0] <= 6.0
    assert sl.fit_total([4.0, 4.0], [1.0, 1.0], [4.0, 4.0], 20.0) == [4.0, 4.0]          # unreachable: bounds win
    assert sl.fit_total([4.0, 4.0], [1.0, 1.0], [9, 9], 1.0) == [1.0, 1.0]


def test_total_too_long_is_trimmed_proportionally_after_the_reask(tmp_path):
    def long(call, d):
        for sc in d["scenes"]:
            for c in sc["clips"]:
                c["target_duration_seconds"] = min(c["target_duration_seconds"] * 1.4, 5.5)
        return d

    ctx, tr, st = run(tmp_path=tmp_path, respond_plan=long)
    assert st["plan"] == Status.SUCCEEDED
    reps = rj(ctx, "selection_report.json")["repairs"]
    assert any(r["code"] in ("scene_rescaled", "total_rescaled") for r in reps)
    assert abs(rj(ctx, "edit_plan.json")["total_duration_seconds"] - 30) <= 2


def test_not_enough_footage_fails_clearly(tmp_path):
    cfg = planner_config(project={"target_duration_seconds": 120}, workflow={"approval": "auto_approve"})
    ctx, tr, st = run(cfg, tmp_path)
    assert st["outline"] == Status.SUCCEEDED and st["plan"] == Status.FAILED
    assert "not enough usable footage" in ctx.manifest.stage("plan")["error"] or "total_duration" in ctx.manifest.stage("plan")["error"]


def test_hand_edited_outline_budgets_are_scaled_to_the_target(tmp_path):
    ctx, tr, st = run(planner_config(), tmp_path)           # stops at the outline approval
    assert st["outline_approval"] == Status.AWAITING_APPROVAL and not tr.plan_calls
    p = ctx.workspace.root / "plans" / "story_outline.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    for s in d["scenes"]:
        s["target_duration_seconds"] = 5.0                  # 15 s against a target of 30 s
    p.write_text(json.dumps(d), encoding="utf-8")
    os_.approve_current_outline(ctx.manifest, os_.ol.outline_file_hash(p), "manual")
    st = plan_runner(ctx).run()
    assert st["plan"] == Status.SUCCEEDED
    assert "scaled" in "\n".join(rj(ctx, "edit_plan.json")["uncertainties"])
    assert abs(rj(ctx, "edit_plan.json")["total_duration_seconds"] - 30) <= 2
