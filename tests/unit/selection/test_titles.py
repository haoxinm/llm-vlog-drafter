"""Titles in the plans: rules (DESIGN §13.4), resolution to frames, mapping to TitleSpec, baseline opening title, LLM scene titles."""
from __future__ import annotations

import json

import pytest
from planning_helpers import make_catalog, mid, sel_clip, sel_plan
from selection_helpers import PlannerTransport, make_plan_ctx, plan_runner, planner_config

from trip_vlog import outline_stages as os_
from trip_vlog.filmcraft.plan_mapping import edit_plan_to_spec
from trip_vlog.logging_utils import Status
from trip_vlog.planning.duration import parse_fps
from trip_vlog.planning.models import EditPlan, SelectionTitle, TitleStart
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.planning.titles import TitleSettings, default_titles, text_problems, with_default_titles
from trip_vlog.planning.validate import ValidationSettings, validate_edit_plan, validate_selection_plan

CAT = make_catalog()
VS = ValidationSettings(target_duration_seconds=12, opening_title=True, scene_titles=True, closing_title="Thanks for watching", project_title="My Trip")


@pytest.fixture(autouse=True)
def _reset():
    yield
    os_.gateway_factory = None


def base():
    return sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 6.0), sel_clip("c2", mid(CAT, "day1/c.jpg"), 3.0)],
                    [sel_clip("c3", mid(CAT, "day1/a.mp4", 1), 3.0)])


def title(tid="title_01", text="My Trip", role="opening_title", scene="scene_001", src="project_title", rel="scene_start", clip=None, off=0.0, dur=3.0):
    return SelectionTitle(title_id=tid, text=text, role=role, scene_id=scene, text_source=src,
                          start=TitleStart(relative_to=rel, clip_instance_id=clip, offset_seconds=off), duration_seconds=dur)


def with_titles(*ts, unc=()):
    p = base()
    return p.model_copy(update={"schema_version": "1.1", "titles": list(ts), "uncertainties": list(unc)})


def codes(p, s=VS):
    return validate_selection_plan(p, CAT, s).codes("errors")


def test_text_rules():
    assert text_problems("My Trip") == [] and text_problems("Two\nlines")
    assert text_problems("x" * 61) and text_problems("a\nb\nc") and text_problems("") and text_problems("a\n\nb") and text_problems("bell\x07")


def test_allowed_titles_validate():
    p = with_titles(title(), title("title_02", "Thanks for watching", "closing", "scene_002", "user_config", "sequence_end"))
    assert "title_text_not_allowed" not in codes(p) and not [c for c in codes(p) if c.startswith("title")]


@pytest.mark.parametrize("t,code", [
    (title(text="Lisbon, Portugal, 2019", role="scene_title", src="outline_scene_title"), "title_text_not_allowed"),
    (title(text="Other Name"), "title_text_not_allowed"),
    (title(text="a\nb\nc"), "title_text"),
    (title(dur=0.5), "title_duration"),
    (title(dur=20), "title_duration"),
    (title(scene="scene_099"), "title_scene_unknown"),
    (title(rel="clip_start", clip="nope"), "title_clip_unknown"),
    (title(src="user"), "title_text_not_allowed"),
    (title(src="outline_scene_title"), "title_text_source"),
])
def test_invalid_titles_are_rejected(t, code):
    assert code in codes(with_titles(t))


def test_titles_must_be_enabled_and_capped():
    off = ValidationSettings(target_duration_seconds=12, project_title="My Trip")
    assert "title_not_enabled" in codes(with_titles(title()), off)
    many = with_titles(*[title(f"title_{i:02d}") for i in range(1, 8)])
    assert "too_many_titles" in codes(many)


def test_folder_name_text_needs_a_flag():
    t = title(text="day1", role="scene_title", src="folder_name")
    assert "title_unflagged" in codes(with_titles(t))
    assert not [c for c in codes(with_titles(t, unc=["title_01: the text 'day1' is from folder name; confirm"])) if c.startswith("title")]


def test_resolution_to_frames_and_move_inside_sequence():
    p = with_titles(title(dur=3.0),
                    title("title_02", "Thanks for watching", "closing", "scene_002", "user_config", "sequence_end", off=0.0, dur=3.0),
                    title("title_03", "S2", "scene_title", "scene_002", "outline_scene_title", "scene_start", off=100.0, dur=2.0))
    edit = resolve_selection_plan(p, CAT, PlanSettings(frame_rate="30", target_duration_seconds=12))
    t = {x.title_id: x for x in edit.titles}
    assert (t["title_01"].start_frame, t["title_01"].duration_frames) == (0, 90)
    assert edit.total_duration_frames == 360 and (t["title_02"].start_frame, t["title_02"].duration_frames) == (270, 90)
    assert t["title_03"].start_frame + t["title_03"].duration_frames == 360 and t["title_03"].resolver_notes   # moved, noted
    assert edit.schema_version == "1.1"


def test_edit_title_validation_overlap_outside_and_text():
    p = with_titles(title(), title("title_02", "S1", "scene_title", "scene_001", "outline_scene_title", "scene_start", off=1.0))
    edit = resolve_selection_plan(p, CAT, PlanSettings(frame_rate="30", target_duration_seconds=12))
    rep = validate_edit_plan(edit, CAT, VS)
    assert "title_overlap" in rep.codes("errors")
    ok = resolve_selection_plan(with_titles(title()), CAT, PlanSettings(frame_rate="30", target_duration_seconds=12))
    assert validate_edit_plan(ok, CAT, VS).ok
    bad = ok.model_copy(deep=True)
    bad.titles[0].start_frame = 350
    bad.titles[0].duration_frames = 90
    bad.plan_hash = "0" * 64
    r = validate_edit_plan(bad, CAT, VS)
    assert "title_outside_sequence" in r.codes("errors")
    typed = ok.model_copy(deep=True)               # a person may type their own text into the edit plan
    typed.titles[0].text, typed.titles[0].text_source = "Day one in Lisbon", "user"
    from trip_vlog.planning.models import with_hash
    assert validate_edit_plan(with_hash(typed), CAT, VS).ok


def test_overlay_clip_on_v2_conflicts_with_a_title():
    p = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 6.0), sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0, role="overlay", audio="muted")])
    p = p.model_copy(update={"schema_version": "1.1", "titles": [title()]})
    edit = resolve_selection_plan(p, CAT, PlanSettings(frame_rate="30", target_duration_seconds=6))
    assert "title_overlaps_overlay" in validate_edit_plan(edit, CAT, ValidationSettings(target_duration_seconds=6, opening_title=True, project_title="My Trip")).codes("errors")


def test_mapping_to_title_spec_is_frame_exact():
    p = with_titles(title(text="My Trip second line"))
    edit = resolve_selection_plan(p, CAT, PlanSettings(frame_rate="30000/1001", target_duration_seconds=12))
    spec = edit_plan_to_spec(edit, CAT, "/media").spec
    assert len(spec.titles) == 1 and spec.titles[0].text == "My Trip second line" and spec.titles[0].track == "V2"
    assert (spec.titles[0].start_frame, spec.titles[0].duration_frames) == (edit.titles[0].start_frame, edit.titles[0].duration_frames)
    assert spec.titles[0].size is None and spec.titles[0].position is None and "V2" in spec.tracks
    assert edit_plan_to_spec(resolve_selection_plan(base(), CAT, PlanSettings(frame_rate="30")), CAT, "/media").spec.titles == ()


def test_baseline_opening_title_on_and_off():
    off = with_default_titles(base(), TitleSettings(project_title="My Trip"))
    assert off.titles == [] and off.schema_version == base().schema_version == "1.0"      # nothing enabled: the plan is returned as it was
    on = with_default_titles(base(), TitleSettings(opening_title=True, closing_title="Bye", project_title="My Trip", title_seconds=2.5))
    assert [(t.role, t.text, t.duration_seconds) for t in on.titles] == [("opening_title", "My Trip", 2.5), ("closing", "Bye", 2.5)]
    assert with_default_titles(on, TitleSettings(opening_title=True, closing_title="Bye", project_title="My Trip")).titles == on.titles   # idempotent
    notes: list[str] = []
    assert default_titles("a", "b", TitleSettings(opening_title=True, project_title="x" * 80), notes) == [] and "skipped" in notes[0]
    assert default_titles("a", "b", TitleSettings(opening_title=True, project_title="T", max_titles=0), []) == []


def test_old_1_0_plans_still_load_and_keep_their_hash():
    ed = resolve_selection_plan(base(), CAT, PlanSettings(frame_rate="30", target_duration_seconds=12))
    d = ed.model_dump(mode="json")
    d.pop("titles")
    d["schema_version"] = "1.0"
    d["selection_plan_hash"] = "0" * 64
    from trip_vlog.planning.models import edit_plan_hash
    old = EditPlan.model_validate(d)
    assert old.titles == [] and edit_plan_hash(old) == edit_plan_hash(EditPlan.model_validate(d))
    assert "titles" not in json.dumps({k: v for k, v in d.items()})
    d["schema_version"], d["titles"] = "1.0", [ed.model_dump(mode="json")["scenes"] and {}]
    with pytest.raises(Exception):
        EditPlan.model_validate(d)


# ---- LLM scene titles --------------------------------------------------------------------------------------------

def titled(text_for, offset=0):
    def respond(call, d):
        d["titles"] = [{"scene_id": "scene_01", "text": text_for(call), "start": {"relative_to": "scene_start", "clip_number": None, "offset_seconds": offset},
                        "duration_seconds": 2.0, "rationale": "Scene opener."}]
        return d
    return respond


def run(cfg, tmp_path, respond):
    ctx, tr, _ = make_plan_ctx(tmp_path, cfg, transport=PlannerTransport(respond))
    return ctx, tr, plan_runner(ctx).run()


def test_scene_title_from_the_outline_is_placed(tmp_path):
    cfg = planner_config(editorial={"scene_titles": True, "opening_title": True}, project={"title": "Trip"}, workflow={"approval": "auto_approve"})
    ctx, tr, st = run(cfg, tmp_path, titled(lambda c: "Scene 1", offset=4))
    assert st["plan"] == Status.SUCCEEDED and len(tr.plan_calls) == 1
    edit = json.loads((ctx.workspace.root / "plans" / "edit_plan.json").read_text(encoding="utf-8"))
    assert [(t["role"], t["text"]) for t in edit["titles"]] == [("opening_title", "Trip"), ("scene_title", "Scene 1")]
    assert edit["titles"][0]["start_frame"] == 0 and edit["titles"][0]["track"] == "V2" and edit["titles"][1]["start_frame"] == 120
    md = (ctx.workspace.root / "plans" / "plan_summary.md").read_text(encoding="utf-8")
    assert "## Titles" in md and "outline scene title" in md and "project.title" in md


def test_overlapping_scene_title_is_reasked_then_dropped(tmp_path):
    cfg = planner_config(editorial={"scene_titles": True, "opening_title": True}, project={"title": "Trip"}, workflow={"approval": "auto_approve"})
    ctx, tr, st = run(cfg, tmp_path, titled(lambda c: "Scene 1", offset=1))          # inside the opening title (0-3 s)
    assert st["plan"] == Status.SUCCEEDED and len(tr.plan_calls) == 2 and "title_overlap" in "\n".join(tr.plan_calls[1].texts)
    rep = json.loads((ctx.workspace.root / "plans" / "selection_report.json").read_text(encoding="utf-8"))
    assert any(r["code"] == "title_dropped" for r in rep["repairs"])
    assert [t["role"] for t in json.loads((ctx.workspace.root / "plans" / "edit_plan.json").read_text(encoding="utf-8"))["titles"]] == ["opening_title"]


def test_invented_title_text_is_rejected_then_dropped(tmp_path):
    cfg = planner_config(editorial={"scene_titles": True}, workflow={"approval": "auto_approve"})
    ctx, tr, st = run(cfg, tmp_path, titled(lambda c: "Lisbon, 2019"))
    assert st["plan"] == Status.SUCCEEDED and len(tr.plan_calls) == 2
    assert "title_text_not_allowed" in "\n".join(tr.plan_calls[1].texts)
    rep = json.loads((ctx.workspace.root / "plans" / "selection_report.json").read_text(encoding="utf-8"))
    assert any(r["code"] == "title_text_not_allowed" for r in rep["repairs"])
    assert json.loads((ctx.workspace.root / "plans" / "edit_plan.json").read_text(encoding="utf-8"))["titles"] == []


def test_titles_are_ignored_when_scene_titles_are_off(tmp_path):
    ctx, tr, st = run(planner_config(workflow={"approval": "auto_approve"}), tmp_path, titled(lambda c: "Scene 1"))
    assert st["plan"] == Status.SUCCEEDED and len(tr.plan_calls) == 1
    assert json.loads((ctx.workspace.root / "plans" / "edit_plan.json").read_text(encoding="utf-8"))["titles"] == []
