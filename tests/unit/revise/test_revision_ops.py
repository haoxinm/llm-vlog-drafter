"""The operations only revisions use (add_moment, remove_scene, move_scene) and their diff text (no ffmpeg, no FilmCraft)."""
from __future__ import annotations

import pytest
from planning_helpers import make_catalog, mid, sel_clip, sel_plan

from trip_vlog.planning.models import SelectionTitle, TitleStart
from trip_vlog.planning.plan_diff import OPERATIONS, REVISION_OPERATIONS, OpError, PlanContext, PlanOp, apply_ops, diff_plans
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.planning.validate import ValidationSettings

CAT = make_catalog()
A0, A1, A2 = (mid(CAT, "day1/a.mp4", i) for i in range(3))
B0 = mid(CAT, "day1/b.mp4", 0)
P0, P1 = (mid(CAT, "day2/p.mp4", i) for i in range(2))
STILL = mid(CAT, "day1/c.jpg")
VS = ValidationSettings(frame_rate="30", target_duration_seconds=18.5, duration_tolerance_pct=60, duration_tolerance_seconds=0)
PS = PlanSettings(frame_rate="30", target_duration_seconds=18.5)


def base(titles=None, candidates=True):
    sel = sel_plan([sel_clip("clip_001", A0, 5), sel_clip("clip_002", A1, 4), sel_clip("clip_003", B0, 3)],
                   [sel_clip("clip_004", P0, 3.5, audio="muted"), sel_clip("clip_005", STILL, 3)])
    sel = sel.model_copy(update={"schema_version": "1.1", "titles": titles or []})
    edit = resolve_selection_plan(sel, CAT, PS)
    cands = {"scene_001": [A0, A1, A2], "scene_002": [P0, P1]} if candidates else None
    return sel, edit, PlanContext(CAT, VS, PS, cands)


def op(name, **kw):
    return PlanOp.from_dict({"op": name, **kw}, REVISION_OPERATIONS)


def test_the_critics_closed_set_is_unchanged_and_revision_ops_are_only_accepted_when_allowed():
    assert "add_moment" not in OPERATIONS and {"add_moment", "remove_scene", "move_scene"} <= set(REVISION_OPERATIONS)
    with pytest.raises(OpError, match="unknown operation"):
        PlanOp.from_dict({"op": "remove_scene", "scene_id": "scene_001"})
    assert op("move_scene", scene_id="scene_001", to_position=2).describe() == "move scene scene_001 to position 2"
    with pytest.raises(OpError, match="to_position"):
        op("move_scene", scene_id="scene_001", to_position=0)
    with pytest.raises(OpError, match="duration_seconds"):
        op("add_moment", scene_id="scene_001", moment_id=A2, duration_seconds=-1)


def test_add_moment_adds_a_cut_clip_with_a_new_id_inside_the_scene():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("add_moment", scene_id="scene_001", moment_id=A2, to_position=2, duration_seconds=2.0, reason="more waves")], ctx, base_selection=sel)
    assert not res.refused and res.edit.total_duration_seconds == pytest.approx(edit.total_duration_seconds + 2.0)
    ids = [c.clip_instance_id for c in res.edit.scenes[0].clips]
    assert ids == ["clip_001", "clip_006", "clip_002", "clip_003"]
    new = res.edit.scenes[0].clips[1]
    assert new.moment_id == A2 and new.transition_in.type == "cut" and new.audio_policy == "natural_sound" and new.rationale == "more waves"
    assert any("clip_006 added" in c.text for c in res.diff.scenes[0].changes)


@pytest.mark.parametrize("kw, code", [
    ({"scene_id": "scene_001", "moment_id": A0}, "moment_in_use"),
    ({"scene_id": "scene_001", "moment_id": P1}, "moment_not_candidate"),
    ({"scene_id": "scene_009", "moment_id": A2}, "unknown_scene"),
    ({"scene_id": "scene_001", "moment_id": A2, "to_position": 9}, "position_out_of_range"),
    ({"scene_id": "scene_001", "moment_id": A2, "duration_seconds": 9}, "duration_exceeds_moment"),
])
def test_add_moment_refusals(kw, code):
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("add_moment", **kw)], ctx, base_selection=sel)
    assert [r.code for r in res.refused] == [code] and not res.applied


def test_add_moment_needs_an_approved_outline():
    sel, edit, ctx = base(candidates=False)
    assert [r.code for r in apply_ops(edit, [op("add_moment", scene_id="scene_001", moment_id=A2)], ctx, base_selection=sel).refused] == ["add_unavailable"]


def test_remove_scene_drops_the_scene_and_the_diff_says_so():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("remove_scene", scene_id="scene_002")], ctx, base_selection=sel)
    assert not res.refused and [s.scene_id for s in res.edit.scenes] == ["scene_001"]
    assert res.diff.total_delta_seconds == pytest.approx(-6.5) and res.diff.scenes[0].scene_id == "scene_002" and res.diff.scenes[0].changes[0].kind == "scene_removed"
    assert "scene scene_002 removed (2 clip(s): clip_004, clip_005)" in "\n".join(res.diff.lines())


def test_remove_scene_is_refused_for_the_last_scene_and_for_a_scene_with_a_title():
    sel, edit, ctx = base()
    one = sel.model_copy(update={"scenes": sel.scenes[:1]})
    e1 = resolve_selection_plan(one, CAT, PS)
    assert [r.code for r in apply_ops(e1, [op("remove_scene", scene_id="scene_001")], ctx, base_selection=one).refused] == ["last_scene"]
    t = SelectionTitle(title_id="title_01", text="Day two", role="scene_title", scene_id="scene_002", text_source="outline_scene_title",
                       start=TitleStart(relative_to="scene_start", clip_instance_id=None, offset_seconds=0.0), duration_seconds=2.0)
    sel2, edit2, _ = base(titles=[t])
    assert [r.code for r in apply_ops(edit2, [op("remove_scene", scene_id="scene_002")], ctx, base_selection=sel2).refused] == ["title_anchored"]


def test_move_scene_reorders_and_the_diff_has_the_new_order():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("move_scene", scene_id="scene_002", to_position=1)], ctx, base_selection=sel)
    assert not res.refused and [s.scene_id for s in res.edit.scenes] == ["scene_002", "scene_001"]
    assert res.edit.scenes[0].clips[0].timeline_start_frame == 0 and res.diff.total_delta_seconds == 0
    assert res.diff.scene_order == ["scene_001, scene_002 -> scene_002, scene_001"] and "Scene order: scene_001, scene_002 -> scene_002, scene_001" in res.diff.lines()
    assert res.diff.to_dict()["scene_order"]
    assert [r.code for r in apply_ops(edit, [op("move_scene", scene_id="scene_002", to_position=2)], ctx, base_selection=sel).refused] == ["no_change"]
    assert [r.code for r in apply_ops(edit, [op("move_scene", scene_id="scene_002", to_position=5)], ctx, base_selection=sel).refused] == ["position_out_of_range"]


def test_a_diff_without_scene_changes_has_no_scene_order_key():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("trim_clip", clip_instance_id="clip_001", duration_seconds=4)], ctx, base_selection=sel)
    assert "scene_order" not in diff_plans(edit, res.edit).to_dict()
