from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from outline_helpers import OutlineTransport, _scene, api_reply, default_draft, draft, make_outline_ctx, outline_config, trip_items

from trip_vlog.llm.schema import sanitize_schema_for_provider, validation_errors
from trip_vlog.llm.types import LLMSchemaError
from trip_vlog.planning import outline as ol
from trip_vlog.planning.outline import OutlineSettings, document_from_response, validate_outline
from trip_vlog.planning.outline_catalog import build_outline_catalog
from trip_vlog.planning.outline_llm import OutlineError, generate_outline

SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"


# ---- schema shape ------------------------------------------------------------------------------------------
def all_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for v in node.values():
            yield from all_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from all_objects(v)


@pytest.mark.parametrize("name", ["story_outline", "story_outline_response"])
def test_schema_is_strict_every_property_required_and_committed(name):
    schema = ol.SCHEMAS[name]()
    Draft202012Validator.check_schema(schema)
    objs = list(all_objects(schema["properties"])) + [schema]
    assert len(objs) >= 3
    for o in objs:
        assert o["additionalProperties"] is False and set(o["required"]) == set(o["properties"])   # strict mode: no optional properties
    assert json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8")) == schema, "run: python -m trip_vlog.planning.outline"
    again = sanitize_schema_for_provider(schema)                                                      # what an OpenAI-strict backend receives
    assert all(set(o["required"]) == set(o["properties"]) for o in all_objects(again))


def test_schema_fields_roles_and_nullable_members():
    resp = ol.response_schema()
    scene = resp["properties"]["scenes"]["items"]
    assert "scene_id" not in scene["properties"] and "scene_id" in ol.outline_schema()["properties"]["scenes"]["items"]["properties"]
    assert scene["properties"]["role"]["enum"] == ["opening_hook", "arrival_context", "establishing_place", "activity_action", "personal_reaction",
                                                   "culture_food", "scenic_breathing_room", "transition", "reflection_ending", "other"]
    assert set(scene["required"]) == {"role", "title", "purpose", "tone", "target_duration_seconds", "candidate_moment_ids", "chronology_note"}
    assert scene["properties"]["chronology_note"]["type"] == ["string", "null"]
    hook = resp["properties"]["hook"]
    assert hook["properties"]["moment_id"]["type"] == ["string", "null"] and hook["properties"]["justification"]["type"] == ["string", "null"]
    assert set(resp["required"]) == {"premise", "story_mode_used", "scenes", "hook", "missing_coverage", "uncertainties"}
    assert "schema_version" in ol.outline_schema()["required"]
    text = json.dumps([resp, ol.outline_schema()]).lower()
    assert "claude" not in text and "gpt" not in text and "timestamp_seconds" not in text            # no model names, no timestamps


def test_schema_and_models_agree():
    d = default_draft(["moment_a", "moment_b"], 60)
    assert validation_errors(d, ol.response_schema()) == []
    doc = document_from_response(d)
    assert validation_errors(doc, ol.outline_schema()) == []
    ol.StoryOutline.model_validate(doc)
    bad = copy.deepcopy(d)
    bad["scenes"][0]["target_duration_seconds"] = 0
    assert validation_errors(bad, ol.response_schema())                                              # exclusiveMinimum 0
    with pytest.raises(Exception):
        ol.StoryOutline.model_validate(document_from_response(bad))
    del bad["scenes"][0]["chronology_note"]
    assert any("chronology_note" in e for e in validation_errors(bad, ol.response_schema()))          # required, only nullable


# ---- validation + repair -----------------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path):
    ctx, _, ids = make_outline_ctx(tmp_path)
    oc = build_outline_catalog(ctx.catalog, ctx.config)
    flat = [i for rel in ("day1/arrival.mp4", "day1/market.mp4", "day1/p1.jpg", "day1/p2.jpg", "day2/hike.mp4") for i in ids[rel]]
    return ctx, oc, ids, flat, OutlineSettings.from_config(ctx.config)


def check(world, d, **kw):
    ctx, oc, ids, flat, s = world
    return validate_outline(document_from_response(d), kw.pop("oc", oc), kw.pop("settings", s), repair=kw.pop("repair", True))


def codes(items):
    return [i.code for i in items]


def test_clean_outline_passes_with_stable_scene_ids(world):
    _, oc, ids, flat, s = world
    rep = check(world, default_draft(flat, 60))
    assert rep.ok and rep.repairs == [] and rep.plan is not None
    assert [sc.scene_id for sc in rep.plan.scenes] == ["scene_01", "scene_02", "scene_03"] and rep.plan.total_seconds() == 60.0


def test_unknown_duplicate_and_unoffered_moments_are_removed_with_findings(world):
    _, oc, ids, flat, s = world
    d = default_draft(flat, 60)
    d["scenes"][0]["candidate_moment_ids"] += ["moment_deadbeef00000000", flat[0]]      # unknown + duplicate within the scene
    d["scenes"][1]["candidate_moment_ids"].append(flat[1])                               # already used by scene 1
    rep = check(world, d)
    assert rep.ok and codes(rep.repairs) == ["unknown_moment", "duplicate_moment", "duplicate_moment"]
    flat_out = rep.plan.moment_ids()
    assert len(flat_out) == len(set(flat_out)) == len(flat)
    assert all("removed" in r.message for r in rep.repairs)


def test_excluded_and_unavailable_moments_are_removed(tmp_path):
    cfg = outline_config(editorial={"must_exclude": ["day1/p1.jpg"]})
    ctx, _, ids = make_outline_ctx(tmp_path, cfg=cfg)
    oc = build_outline_catalog(ctx.catalog, cfg)
    s = OutlineSettings.from_config(cfg)
    flat = [i for rel in ("day1/arrival.mp4", "day1/p1.jpg", "day2/hike.mp4") for i in ids[rel]]
    rep = validate_outline(document_from_response(default_draft(flat, 60)), oc, s, repair=True)
    assert rep.ok and "excluded_moment" in codes(rep.repairs) and ids["day1/p1.jpg"][0] not in rep.plan.moment_ids()
    with ctx.catalog.conn:
        ctx.catalog.conn.execute("UPDATE assets SET present=0 WHERE relative_path='day2/hike.mp4'")
    oc2 = build_outline_catalog(ctx.catalog, cfg)
    rep2 = validate_outline(document_from_response(default_draft(flat, 60)), oc2, s, repair=True)
    assert "unavailable_moment" in codes(rep2.repairs)


def test_moment_dropped_by_preselection_is_not_offered(tmp_path):
    cfg = outline_config(planning={"max_moments_in_prompt": 3})
    ctx, _, ids = make_outline_ctx(tmp_path, cfg=cfg)
    oc = build_outline_catalog(ctx.catalog, cfg)
    dropped = next(m for m in oc.eligible if m not in oc.offered)
    d = default_draft(sorted(oc.offered), 60)
    d["scenes"][0]["candidate_moment_ids"].append(dropped)
    rep = validate_outline(document_from_response(d), oc, OutlineSettings.from_config(cfg), repair=True)
    assert rep.ok and "moment_not_offered" in codes(rep.repairs)
    user = validate_outline(document_from_response(d), oc, OutlineSettings.from_config(cfg), repair=False)    # the user may pick any usable moment
    assert user.ok


def test_scene_left_empty_is_an_error_not_a_silent_change(world):
    _, oc, ids, flat, s = world
    d = default_draft(flat, 60)
    d["scenes"][1]["candidate_moment_ids"] = ["moment_0000000000000000"]
    rep = check(world, d)
    assert not rep.ok and codes(rep.errors) == ["empty_scene"] and "unknown_moment" in codes(rep.repairs)


def test_duration_rescale_is_proportional_recorded_and_exact(world):
    _, oc, ids, flat, s = world
    d = default_draft(flat, 60)
    for sc, v in zip(d["scenes"], (10, 20, 30)):
        sc["target_duration_seconds"] = v                  # 60 total... make it clearly off: x2
        sc["target_duration_seconds"] = v * 2
    rep = check(world, d)
    assert rep.ok and codes(rep.repairs) == ["duration_rescaled"]
    r = rep.repairs[0]
    assert r.before == [20, 40, 60] and sum(r.after) == pytest.approx(60.0) and rep.plan.total_seconds() == pytest.approx(60.0, abs=0.01)
    assert [round(x, 2) for x in r.after[:2]] == [10.0, 20.0]                                         # proportions kept (1:2:3)
    d2 = default_draft(flat, 60)
    d2["scenes"][0]["target_duration_seconds"] += 2.5      # inside the tolerance (max(5%, 2 s) = 3 s): untouched
    assert check(world, d2).repairs == []


def test_hook_rules(world):
    _, oc, ids, flat, s = world
    hook_id = ids["day2/hike.mp4"][0]

    def with_hook(dur, moment=hook_id):
        d = default_draft(flat, 60)
        hook_scene = _scene("opening_hook", "Teaser", [moment], dur)
        d["scenes"].insert(0, hook_scene)
        d["scenes"][1]["candidate_moment_ids"] = [m for m in d["scenes"][1]["candidate_moment_ids"] if m != moment]
        d["hook"] = {"used": True, "moment_id": moment, "justification": "The best view of the trip."}
        return d

    rep = check(world, with_hook(6))
    assert rep.ok and rep.plan.hook.used and rep.plan.scenes[0].role == "opening_hook"
    assert hook_id in rep.plan.scenes[0].candidate_moment_ids
    long = check(world, with_hook(20))                                                                 # longer than opening_hook_seconds (8)
    assert long.ok and "hook_too_long" in codes(long.repairs) and long.plan.scenes[0].target_duration_seconds == 8
    assert long.plan.total_seconds() == pytest.approx(60.0, abs=0.02)                                   # the rest is rescaled around the hook
    # the hook moment may appear again in its chronological scene, but not a third time
    d = with_hook(6)
    d["scenes"][-1]["candidate_moment_ids"].append(hook_id)
    assert check(world, d).ok
    d["scenes"][1]["candidate_moment_ids"].append(hook_id)
    assert "duplicate_moment" in codes(check(world, d).repairs)
    # not allowed by the settings
    cfg = outline_config(editorial={"allow_nonlinear_hook": False})
    rep = check(world, with_hook(6), settings=OutlineSettings.from_config(cfg))
    assert codes(rep.errors) == ["hook_not_allowed"]
    # inconsistencies are errors (intent would change), never silently fixed
    d = with_hook(6)
    d["hook"]["moment_id"] = flat[0]
    assert codes(check(world, d).errors) == ["hook_moment_not_in_scene"]
    d = with_hook(6)
    d["hook"] = {"used": False, "moment_id": None, "justification": None}
    assert codes(check(world, d).errors) == ["hook_scene_without_hook"]
    d = default_draft(flat, 60)
    d["hook"] = {"used": True, "moment_id": flat[0], "justification": "x"}
    assert codes(check(world, d).errors) == ["hook_scene_missing"]
    d = default_draft(flat, 60)
    d["hook"] = {"used": False, "moment_id": flat[0], "justification": "left over"}
    rep = check(world, d)
    assert rep.ok and codes(rep.repairs) == ["hook_cleared"] and rep.plan.hook.moment_id is None
    d = with_hook(6)
    d["scenes"].insert(1, d["scenes"].pop(0))
    assert "hook_not_first" in codes(check(world, d).errors)


def test_must_include_covered_or_reported(tmp_path):
    cfg = outline_config(editorial={"must_include": ["day2/hike.mp4", "ghost/none.mp4"]})
    ctx, _, ids = make_outline_ctx(tmp_path, cfg=cfg)
    oc = build_outline_catalog(ctx.catalog, cfg)
    s = OutlineSettings.from_config(cfg)
    flat = [i for rel in ("day1/arrival.mp4", "day1/market.mp4") for i in ids[rel]]
    rep = validate_outline(document_from_response(default_draft(flat, 60)), oc, s, repair=True)
    assert codes(rep.errors) == ["must_include_missing"] and "day2/hike.mp4" in rep.errors[0].message and ids["day2/hike.mp4"][0] in rep.errors[0].message
    flat += ids["day2/hike.mp4"]
    rep = validate_outline(document_from_response(default_draft(flat, 60)), oc, s, repair=True)
    assert rep.ok and codes(rep.warnings) == ["must_include_unavailable"]
    assert rep.plan.missing_coverage == ["must_include 'ghost/none.mp4' matches no available moment"]


def test_nonlinear_order_is_flagged_only_for_chronological_mode(tmp_path):
    items = trip_items()
    ctx, _, ids = make_outline_ctx(tmp_path, items=items)
    flat = [i for rel in ("day2/hike.mp4", "day1/arrival.mp4", "day1/market.mp4") for i in ids[rel]]   # day 2 first
    for mode, expect in (("chronological", ["nonlinear_order"]), ("mostly_chronological", []), ("thematic", [])):
        cfg = outline_config(project={"story_mode": mode})
        oc = build_outline_catalog(ctx.catalog, cfg)
        d = default_draft(flat, 60, n_scenes=3)
        d["story_mode_used"] = mode
        d["scenes"] = [_scene("activity_action", f"S{i}", ids[rel], 20) for i, rel in enumerate(("day2/hike.mp4", "day1/arrival.mp4", "day1/market.mp4"))]
        rep = validate_outline(document_from_response(d), oc, OutlineSettings.from_config(cfg), repair=True)
        assert rep.ok and [w for w in codes(rep.warnings) if w != "story_mode_differs"] == expect, mode
    d["story_mode_used"] = "chronological"
    rep = validate_outline(document_from_response(d), oc, OutlineSettings.from_config(cfg), repair=True)
    assert "story_mode_differs" in codes(rep.warnings)


def test_user_edited_outline_is_checked_not_rewritten(world):
    _, oc, ids, flat, s = world
    doc = document_from_response(default_draft(flat, 60))
    doc["scenes"][0]["candidate_moment_ids"].append("moment_0000000000000000")
    doc["scenes"][1]["target_duration_seconds"] = 5
    doc["scenes"][1]["candidate_moment_ids"].append(flat[0])
    before = copy.deepcopy(doc)
    rep = validate_outline(doc, oc, s, repair=False)
    assert doc == before and rep.repairs == []
    assert codes(rep.errors) == ["unknown_moment", "duplicate_moment"]
    assert "duration_off_target" in codes(rep.warnings)                                                  # a warning: the user may mean it
    broken = copy.deepcopy(before)
    broken["scenes"][0]["scene_id"] = broken["scenes"][1]["scene_id"]
    assert "duplicate_scene_id" in codes(validate_outline(broken, oc, s, repair=False).errors)
    broken = copy.deepcopy(before)
    broken["scenes"][0]["role"] = "dance"
    rep = validate_outline(broken, oc, s, repair=False)
    assert codes(rep.errors) == ["schema"] and "role" in rep.errors[0].message


# ---- generate with one corrected re-ask -----------------------------------------------------------------------
def test_generate_repairs_without_a_second_request(tmp_path):
    def respond(call, d):
        d["scenes"][0]["candidate_moment_ids"].append("moment_0000000000000000")
        return d

    ctx, tr, ids = make_outline_ctx(tmp_path, transport=OutlineTransport(respond))
    from outline_helpers import gateway_for
    res = generate_outline(gateway_for(ctx.config, ctx.workspace.root, tr), ctx.config, build_outline_catalog(ctx.catalog, ctx.config))
    assert len(tr.calls) == 1 and res.requests == 1 and codes(res.report.repairs) == ["unknown_moment"] and res.usage.input_tokens == 1000


def test_generate_sends_findings_back_once_then_succeeds(tmp_path):
    def respond(call, d):
        if not call.retry:
            d["scenes"][1]["candidate_moment_ids"] = ["moment_0000000000000000"]       # empty scene after removal: ambiguous
        return d

    ctx, tr, ids = make_outline_ctx(tmp_path, transport=OutlineTransport(respond))
    from outline_helpers import gateway_for
    res = generate_outline(gateway_for(ctx.config, ctx.workspace.root, tr), ctx.config, build_outline_catalog(ctx.catalog, ctx.config))
    assert len(tr.calls) == 2 and res.requests == 2 and res.usage.input_tokens == 2000
    retry = tr.calls[1]
    assert any("empty_scene" in t for t in retry.texts) and any("Your previous reply was" in t for t in retry.texts)
    assert retry.texts[0] == tr.calls[0].texts[0]                                           # the catalog is the same first part, not rewritten
    assert [a["errors"] and a["errors"][0]["code"] for a in res.attempts] == ["empty_scene", []]


def test_generate_fails_clearly_when_the_re_ask_does_not_help(tmp_path):
    def respond(call, d):
        d["scenes"][1]["candidate_moment_ids"] = ["moment_0000000000000000"]
        return d

    ctx, tr, _ = make_outline_ctx(tmp_path, transport=OutlineTransport(respond))
    from outline_helpers import gateway_for
    with pytest.raises(OutlineError) as e:
        generate_outline(gateway_for(ctx.config, ctx.workspace.root, tr), ctx.config, build_outline_catalog(ctx.catalog, ctx.config))
    assert len(tr.calls) == 2 and "empty_scene" in str(e.value) and len(e.value.history) == 2
    cfg0 = outline_config(planning={"outline_retries": 0})
    tr0 = OutlineTransport(respond)
    ctx0, _, _ = make_outline_ctx(tmp_path / "z", cfg=cfg0, transport=tr0)
    with pytest.raises(OutlineError):
        generate_outline(gateway_for(cfg0, ctx0.workspace.root, tr0), cfg0, build_outline_catalog(ctx0.catalog, cfg0))
    assert len(tr0.calls) == 1


def test_generate_schema_violation_is_retried_by_the_gateway_then_raises(tmp_path):
    def respond(call, d):
        d["scenes"][0]["role"] = "dance"
        return d

    ctx, tr, _ = make_outline_ctx(tmp_path, transport=OutlineTransport(respond))
    from outline_helpers import gateway_for
    with pytest.raises(LLMSchemaError):
        generate_outline(gateway_for(ctx.config, ctx.workspace.root, tr), ctx.config, build_outline_catalog(ctx.catalog, ctx.config))
    assert len(tr.calls) == 3                                                                 # 1 + llm.schema_retries (2), errors fed back


def test_generate_without_usable_moments_fails_before_any_request(tmp_path):
    ctx, tr, _ = make_outline_ctx(tmp_path, items=[])
    from outline_helpers import gateway_for
    with pytest.raises(OutlineError, match="no usable candidate moments"):
        generate_outline(gateway_for(ctx.config, ctx.workspace.root, tr), ctx.config, build_outline_catalog(ctx.catalog, ctx.config))
    assert tr.calls == []
