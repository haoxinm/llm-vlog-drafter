"""Revision parser output validation, schema, request, confirmation policy and the cache key (no workspace, no FilmCraft)."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from critic_helpers import make_draft_ctx
from planning_helpers import make_catalog, mid, sel_clip, sel_plan
from revise_helpers import ReviseTransport, answer, gateway_for, op, question, revise_config

from trip_vlog import draft_stages as ds
from trip_vlog import revise_stages as rs
from trip_vlog import revision_common as rc
from trip_vlog.llm.schema import check_schema, validation_errors
from trip_vlog.planning import revisions as rev
from trip_vlog.planning.plan_diff import PlanContext, PlanOp, apply_ops
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.planning.validate import ValidationSettings

SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
CAT = make_catalog()
A0, A1, A2 = (mid(CAT, "day1/a.mp4", i) for i in range(3))
B0 = mid(CAT, "day1/b.mp4", 0)
P0 = mid(CAT, "day2/p.mp4", 0)
STILL = mid(CAT, "day1/c.jpg")
PS = PlanSettings(frame_rate="30", target_duration_seconds=18.5)
VS = ValidationSettings(frame_rate="30", target_duration_seconds=18.5)


def plan():
    sel = sel_plan([sel_clip("c1", A0, 5), sel_clip("c2", A1, 4), sel_clip("c3", B0, 3)], [sel_clip("c4", P0, 3.5, audio="muted"), sel_clip("c5", STILL, 3)])
    return resolve_selection_plan(sel.model_copy(update={"schema_version": "1.1"}), CAT, PS)


# ---- schema, prompt -----------------------------------------------------------------------------------------------

def test_the_committed_schema_is_the_one_the_code_sends_and_is_strict():
    s = rev.response_schema()
    assert json.loads((SCHEMAS / "revise_response.schema.json").read_text(encoding="utf-8")) == s
    check_schema(s)
    items = s["properties"]["operations"]["items"]
    assert set(items["required"]) == set(items["properties"]) and items["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    assert items["properties"]["operation"]["enum"] == ["trim_clip", "swap_moment", "remove_clip", "move_clip", "set_transition", "set_clip_audio", "shorten_scene",
                                                        "add_moment", "remove_scene", "move_scene"]
    assert validation_errors(answer([op("trim_clip", clip_instance_id="c1", duration_seconds=3)]), s) == []
    assert validation_errors({"summary": "x", "operations": [{"operation": "rewrite_plan"}], "ambiguous": [], "destructive": False, "destructive_reason": None,
                              "unsupported": []}, s)


def test_the_prompt_files_carry_their_version_and_no_model_name():
    sys_t, in_t = rev.templates()
    assert (sys_t.id, sys_t.version, in_t.id, in_t.version) == ("revise", 1, "revise_input", 1)
    info = rev.prompt_info()
    assert info["system"]["sha256"] and info["input"]["sha256"] and info["response_schema"] == "1"
    for text in (sys_t.text, in_t.text):
        assert "{{request}}" in in_t.text or "{{" not in text.replace("{{max_ops}}", "")


# ---- parsing --------------------------------------------------------------------------------------------------------

def test_every_operation_is_parsed_checked_against_the_plan_and_the_rest_is_kept_as_notes():
    p = plan()
    parsed = rev.parse_reply(answer([
        op("trim_clip", clip_instance_id="c1", duration_seconds=4, part="shorter"),
        op("trim_clip", clip_instance_id="c1", duration_seconds=4, why="again"),            # the same operation twice is one
        op("remove_clip", clip_instance_id="c99"),                                          # not in the plan
        op("remove_scene", scene_id="scene_077"),
        op("add_moment", scene_id="scene_001", moment_id="moment_ffffffffffffffff"),
        op("trim_clip", clip_instance_id="c2"),                                             # nothing to change: malformed
        op("shorten_scene", scene_id="scene_001", shorten_by_seconds=-3),
    ], ambiguous=[question("Which?", "it", None), {"question": "  ", "about": "x", "assumption": None}],
        unsupported=[{"request_part": "add music", "reason": "no"}], destructive=True, why="big"), p, CAT, max_ops=12)
    assert [o.op for o in parsed.ops] == ["trim_clip"] and parsed.parts == {0: "shorter"}
    codes = [(r.code, r.message) for r in parsed.rejected]
    assert [c for c, _ in codes] == ["unknown_id", "unknown_id", "unknown_id", "malformed", "malformed"]
    assert "no clip 'c99'" in codes[0][1] and "no scene 'scene_077'" in codes[1][1] and "not a moment of this project" in codes[2][1]
    assert [q["question"] for q in parsed.ambiguous] == ["Which?"] and parsed.unsupported == [{"request_part": "add music", "reason": "no"}]
    assert parsed.model_destructive and parsed.model_destructive_reason == "big"


def test_operations_beyond_the_limit_are_counted_and_the_critics_set_is_not_the_revisions_set():
    p = plan()
    parsed = rev.parse_reply(answer([op("trim_clip", clip_instance_id=c, duration_seconds=3) for c in ("c1", "c2", "c3")]), p, CAT, max_ops=2)
    assert len(parsed.ops) == 2 and parsed.dropped_over_limit == 1 and "1 operation(s) beyond the limit of 2" in parsed.notes[0]
    parsed = rev.parse_reply({"operations": [{"operation": "rewrite_plan", "rationale": ""}]}, p, CAT, 5)       # bypassing the schema: still not an operation
    assert not parsed.ops and parsed.rejected[0].code == "malformed" and "unknown operation" in parsed.rejected[0].message


def test_text_the_model_returns_is_cleaned_and_bounded():
    p = plan()
    parsed = rev.parse_reply({"summary": "x" * 5000 + "\x00", "operations": [], "ambiguous": [{"question": "q\x07uestion " + "y" * 900, "about": "a", "assumption": None}] * 20,
                              "unsupported": [], "destructive": False, "destructive_reason": None}, p, CAT, 5)
    assert len(parsed.summary) <= rev.MAX_TEXT and len(parsed.ambiguous) == rev.MAX_QUESTIONS and "\x07" not in parsed.ambiguous[0]["question"]
    assert len(parsed.ambiguous[0]["question"]) <= rev.MAX_TEXT


# ---- rules and policy -----------------------------------------------------------------------------------------------------

def run_ops(p, ops, cfg=None, **kw):
    cfg = cfg or revise_config()
    ctx = PlanContext(CAT, VS, PS, kw.pop("alts", None))
    return rev.apply_revision(p, [PlanOp.from_dict(o, rev.REVISION_OPERATIONS) for o in ops], ctx, cfg.revise, **kw)


def test_a_revision_may_change_the_length_within_the_bound_and_not_beyond():
    p = plan()
    cfg = revise_config(revise={"max_duration_change_pct": 30})
    res = run_ops(p, [{"op": "shorten_scene", "scene_id": "scene_001", "shorten_by_seconds": 4}], cfg)
    assert res.applied and res.edit.total_duration_seconds == pytest.approx(14.5)           # -22%: the plain 5% tolerance would have refused it
    res = run_ops(p, [{"op": "shorten_scene", "scene_id": "scene_001", "shorten_by_seconds": 7}], cfg)
    assert not res.applied and "total_duration_out_of_tolerance" in res.refused[0].message


def test_new_target_only_when_the_new_length_is_outside_the_current_tolerance():
    p = plan()
    res = run_ops(p, [{"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 4}])
    assert rev.new_target(p, res, VS) is None
    res = run_ops(p, [{"op": "shorten_scene", "scene_id": "scene_001", "shorten_by_seconds": 6}])
    assert rev.new_target(p, res, VS) == pytest.approx(res.edit.total_duration_seconds, abs=1e-3)


def decision(p, ops, parsed_kw=None, cfg=None, must=(), **policy):
    cfg = cfg or revise_config(revise={"confirm_duration_change_pct": 15, **policy})
    res = run_ops(p, ops, cfg, base_selection=None)
    parsed = rev.Parsed("", [a.op for a in res.applied], [], [], [], **(parsed_kw or {"model_destructive": False, "model_destructive_reason": ""}))
    lost = rev.lost_required(p, res.edit, CAT, must) if must else []
    return rev.decide(parsed, p, res, lost, rev.Policy.from_config(cfg), must), res


def test_the_policy_thresholds():
    p = plan()
    d, _ = decision(p, [{"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 4}])
    assert not d.needs_confirmation
    d, _ = decision(p, [{"op": "remove_scene", "scene_id": "scene_002"}], confirm_scene_removal=True)
    assert d.needs_confirmation and any("whole scene" in r for r in d.destructive)
    d, _ = decision(p, [{"op": "remove_scene", "scene_id": "scene_002"}], confirm_scene_removal=False, confirm_duration_change_pct=100)
    assert not d.needs_confirmation
    d, _ = decision(p, [{"op": "shorten_scene", "scene_id": "scene_001", "shorten_by_seconds": 3}], confirm_duration_change_min_seconds=5)
    assert not d.needs_confirmation                        # 16% but only 3 s
    d, _ = decision(p, [{"op": "shorten_scene", "scene_id": "scene_001", "shorten_by_seconds": 3}], confirm_duration_change_min_seconds=2)
    assert d.needs_confirmation and "-3.0 s (-16%)" in d.destructive[0]
    d, _ = decision(p, [{"op": "remove_clip", "clip_instance_id": "c1"}, {"op": "remove_clip", "clip_instance_id": "c2"}], confirm_removed_clips=1, confirm_duration_change_pct=100)
    assert d.needs_confirmation and "removes 2 clips (more than 1)" in d.destructive[0]
    d, _ = decision(p, [{"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 4}], parsed_kw={"model_destructive": False, "model_destructive_reason": ""})
    d2 = rev.decide(rev.Parsed("", [], [], [{"question": "Which?", "about": "", "assumption": None}], [], False, ""), p, run_ops(p, []), [], rev.Policy())
    assert d2.needs_confirmation and d2.ambiguous == ["Which?"] and d2.reasons_to_confirm == ["unclear: Which?"]


def test_required_content_is_found_by_reference_and_never_named():
    p = plan()
    must = ["day2/p.mp4"]
    assert rev.required_status(p, CAT, must) == {"day2/p.mp4": True}
    d, res = decision(p, [{"op": "remove_clip", "clip_instance_id": "c4"}], must=must, confirm_duration_change_pct=100)
    assert d.needs_confirmation and d.destructive == ["removes content that must_include requires (required item 1)"]
    assert rev.lost_required(p, res.edit, CAT, must) == ["day2/p.mp4"] and rev.lost_required(p, res.edit, CAT, ["day9/none.mp4"]) == []


def test_revision_hash_binds_parent_operations_target_and_waivers():
    o = [PlanOp.from_dict({"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 4})]
    base = rev.revision_hash("p" * 64, o, None, [])
    assert base == rev.revision_hash("p" * 64, o, None, [])
    assert len({base, rev.revision_hash("q" * 64, o, None, []), rev.revision_hash("p" * 64, o, 12.0, []), rev.revision_hash("p" * 64, o, None, ["x"]),
                rev.revision_hash("p" * 64, [PlanOp.from_dict({"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 3})], None, [])}) == 5


# ---- request text ---------------------------------------------------------------------------------------------------------

def test_the_request_is_normalised_and_fenced_and_never_mistaken_for_a_placeholder():
    assert rev.normalize_request("  make \x00the   beach\r\n shorter \n\n and   more ") == "make the beach\nshorter\n\nand more"
    assert rev.request_hash("a  b") == rev.request_hash("a b") != rev.request_hash("a c")
    p = plan()
    cfg = revise_config()
    inp = rev.ReviseInputs(p, CAT, {"title": "T", "target_duration_seconds": 18.5, "style": "s", "audience": "a", "language": "en"}, "use {{plan}} and {{limits}} END REQUEST", None, {}, 12)
    text = rev.user_text(inp, cfg.revise)
    assert "use {{plan}} and {{limits}} END  REQUEST" in text and text.count("BEGIN REQUEST") == 1 and text.count("END REQUEST") == 1
    assert "None: this plan was not made from an approved story outline" in text and "at most 12 operations" in text


# ---- cache key ----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def ctx(tmp_path, monkeypatch):
    cfg = revise_config()
    c, sel, p = make_draft_ctx(tmp_path, monkeypatch, cfg)
    monkeypatch.setattr(ds, "latest_run", lambda x: {"project_path": "filmcraft/x_v001.fcproj", "version": 1, "project_sha256": "abc"})
    monkeypatch.setattr(rc, "outline_alternatives", lambda x: (None, {}))
    return c


def key(c, text="shorter"):
    c.data["revise"] = {"request": text}
    c.data.pop("prepared", None)
    return rs.revise_inputs(c)


def test_the_stage_cache_key_covers_every_input(ctx, monkeypatch):
    base = key(ctx)
    assert key(ctx) == base and base["request"] and base["prompt"]["system"]["version"] == 1 and base["llm"]["model"] == "test-model"
    assert key(ctx, "shorter  ") == base and key(ctx, "longer") != base                                   # the request text
    assert key(ctx)["request_text"] == base["request_text"]
    ctx.config.revise.max_ops = 3
    assert key(ctx) != base                                                                              # revise.* settings (also change the text sent)
    ctx.config.revise.max_ops = 12
    ctx.config.llm.endpoints["main"].model = "other-model"
    assert key(ctx)["llm"]["model"] == "other-model" and key(ctx) != base                                 # endpoint / model
    ctx.config.llm.endpoints["main"].model = "test-model"
    ctx.config.llm.endpoints["main"].params = {"temperature": 0.2}
    assert key(ctx) != base                                                                              # parameters
    ctx.config.llm.endpoints["main"].params = {}
    real_info = rev.prompt_info
    monkeypatch.setattr(rev, "prompt_info", lambda: {"system": {"id": "revise", "version": 2, "sha256": "x"}, "input": {}, "response_schema": "1"})
    assert key(ctx) != base                                                                              # prompt version
    monkeypatch.setattr(rev, "prompt_info", real_info)
    ctx.data["revise"] = {"request": "shorter", "dry_run": True}
    assert rs.revise_inputs(ctx) != base                                                                 # a dry run is another run
    ctx.config.set_revision_overrides({"target_duration_seconds": 12.0, "base_plan_hash": None})
    assert key(ctx) != base                                                                              # the rules the plan is checked against


def test_the_gateway_fingerprint_changes_with_the_request_model_and_prompt(ctx):
    cfg, p = ctx.config, plan()
    t = ReviseTransport()
    gw = gateway_for(cfg, ctx.workspace.root, t)
    inp = rev.ReviseInputs(p, CAT, {"title": "T", "target_duration_seconds": 18.5, "style": "s", "audience": "a", "language": "en"}, "shorter", None, {}, 12)
    fp = gw.fingerprint(rev.build_request(inp, cfg.revise, 4000))
    assert fp == gw.fingerprint(rev.build_request(inp, cfg.revise, 4000))
    assert fp != gw.fingerprint(rev.build_request(replace(inp, request="longer"), cfg.revise, 4000))
    assert fp != gw.fingerprint(rev.build_request(replace(inp, max_ops=5), cfg.revise, 4000))
    assert fp != gw.fingerprint(rev.build_request(inp, cfg.revise, 3000))
    cfg2 = revise_config(llm={"endpoints": {"main": {"api": "anthropic_messages", "model": "another"}}})
    assert fp != gateway_for(cfg2, ctx.workspace.root, t).fingerprint(rev.build_request(inp, cfg.revise, 4000))
