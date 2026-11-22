import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from planning_helpers import make_catalog, mid, sel_clip, sel_plan
from pydantic import ValidationError

from trip_vlog.planning.models import EditPlan, SelectionPlan, json_schema, to_json_dict, write_schemas
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
CAT = make_catalog()
SEL = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("c2", mid(CAT, "day1/c.jpg"), 3.0, dissolve=1.0)])
EDIT = resolve_selection_plan(SEL, CAT, PlanSettings(frame_rate="30000/1001", target_duration_seconds=6))
MODELS = {"selection_plan": (SelectionPlan, SEL), "edit_plan": (EditPlan, EDIT)}


@pytest.mark.parametrize("name", MODELS)
def test_committed_schema_is_in_sync_with_models(name):
    committed = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    assert committed == json_schema(name), "run: python -m trip_vlog.planning.models"


@pytest.mark.parametrize("name", MODELS)
def test_schema_is_valid_2020_12_and_strict(name):
    schema = json_schema(name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)
    objects = [schema, *schema["$defs"].values()]
    for o in objects:
        assert o["type"] == "object" and o["additionalProperties"] is False and o["required"]
    text = json.dumps(schema).lower()
    assert "tick" not in text and "filmcraft" not in text  # no FilmCraft ids or time units
    assert set(schema["properties"]) >= {"schema_version"}


def test_required_fields_match_spec():
    sel = json_schema("selection_plan")["$defs"]["SelectionClip"]
    assert set(sel["required"]) == {"clip_instance_id", "moment_id", "target_duration_seconds", "anchor", "track_role", "audio_policy",
                                    "transition_in", "rationale"}  # intentional_repeat_reason is optional
    assert sel["properties"]["anchor"]["enum"] == ["start", "center", "end", "best"]
    assert sel["properties"]["track_role"]["enum"] == ["primary_video", "overlay", "natural_audio", "music", "narration"]
    assert json_schema("selection_plan")["$defs"]["SelectionTransition"]["properties"]["type"]["enum"] == ["cut", "dissolve"]
    assert "source_in_seconds" not in sel["properties"]  # the LLM never writes source timestamps
    edit = json_schema("edit_plan")["$defs"]["EditClip"]
    assert {"asset_id", "source_in_seconds", "source_out_seconds", "requested_duration_seconds", "resolver_notes", "timeline_start_frame",
            "duration_frames"} <= set(edit["required"])


def test_write_schemas(tmp_path):
    paths = write_schemas(tmp_path)
    assert sorted(p.name for p in paths) == ["edit_plan.schema.json", "selection_plan.schema.json"]
    assert json.loads(paths[0].read_text(encoding="utf-8")) == json_schema("selection_plan") and b"\r" not in paths[0].read_bytes()


@pytest.mark.parametrize("name", MODELS)
def test_sample_documents_validate_with_both(name):
    model, plan = MODELS[name]
    doc = to_json_dict(plan)
    assert list(Draft202012Validator(json_schema(name)).iter_errors(doc)) == []
    assert to_json_dict(model.model_validate(doc)) == doc


def _paths(doc, name):
    if name == "selection_plan":
        return {"clip": doc["scenes"][0]["clips"][0], "scene": doc["scenes"][0], "root": doc}
    return {"clip": doc["scenes"][0]["clips"][0], "scene": doc["scenes"][0], "root": doc, "seq": doc["sequence"]}


MUTATIONS = {
    "selection_plan": [
        ("clip", "anchor", "middle"), ("clip", "track_role", "titles"), ("clip", "audio_policy", "loud"), ("clip", "target_duration_seconds", 0),
        ("clip", "target_duration_seconds", -2), ("clip", "target_duration_seconds", "3"), ("clip", "moment_id", "has space"), ("clip", "extra", 1),
        ("clip", "transition_in", {"type": "wipe", "duration_seconds": 1}), ("clip", "transition_in", {"type": "cut", "duration_seconds": 9}),
        ("clip", "source_in_seconds", 1.0), ("root", "schema_version", "2.0"), ("root", "scenes", []), ("scene", "clips", []),
    ],
    "edit_plan": [
        ("clip", "asset_id", "asset_xyz"), ("clip", "asset_id", "day1/a.mp4"), ("clip", "duration_frames", 0), ("clip", "timeline_start_frame", -1),
        ("clip", "source_in_seconds", -1.0), ("clip", "media_type", "gif"), ("clip", "resolver_notes", "none"), ("clip", "extra", 1),
        ("clip", "transition_in", {"type": "dissolve", "duration_seconds": 1.0, "duration_frames": 30, "alignment": "left"}),
        ("seq", "fps", "30.0"), ("seq", "fps", "0"), ("seq", "width", 8), ("root", "plan_hash", "abc"), ("root", "filmcraft_id", "x"),
        ("root", "scenes", []), ("root", "total_duration_frames", 0),
    ],
}


@pytest.mark.parametrize("name", MODELS)
def test_invalid_documents_rejected_by_both(name):
    model, plan = MODELS[name]
    validator = Draft202012Validator(json_schema(name))
    for where, key, value in MUTATIONS[name]:
        doc = copy.deepcopy(to_json_dict(plan))
        _paths(doc, name)[where][key] = value
        assert list(validator.iter_errors(doc)), f"schema accepted {where}.{key}={value!r}"
        with pytest.raises(ValidationError):
            model.model_validate(doc)
    doc = copy.deepcopy(to_json_dict(plan))
    del _paths(doc, name)["clip"]["rationale"]
    assert list(validator.iter_errors(doc))
    with pytest.raises(ValidationError):
        model.model_validate(doc)


def test_non_finite_numbers_rejected():
    doc = to_json_dict(SEL)
    doc["scenes"][0]["clips"][0]["target_duration_seconds"] = float("nan")
    with pytest.raises(ValidationError):
        SelectionPlan.model_validate(doc)
    doc["scenes"][0]["clips"][0]["target_duration_seconds"] = float("inf")
    with pytest.raises(ValidationError):
        SelectionPlan.model_validate(doc)
