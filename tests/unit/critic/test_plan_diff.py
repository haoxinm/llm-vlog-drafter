"""planning.plan_diff: the closed operation set, application through the resolver and validators, refusals, diff text (no ffmpeg, no FilmCraft)."""
from __future__ import annotations

from dataclasses import replace

import pytest
from planning_helpers import make_catalog, mid, sel_clip, sel_plan

from trip_vlog.planning.models import SelectionTitle, TitleStart, selection_plan_hash
from trip_vlog.planning.plan_diff import (OPERATIONS, OpError, PlanBaseError, PlanContext, PlanOp, apply_ops, diff_plans, layout_signature,
                                         selection_from_edit)
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.planning.validate import ValidationSettings, validate_edit_plan

CAT = make_catalog()
A0, A1, A2 = (mid(CAT, "day1/a.mp4", i) for i in range(3))
B0, B1 = (mid(CAT, "day1/b.mp4", i) for i in range(2))
P0 = mid(CAT, "day2/p.mp4", 0)
STILL = mid(CAT, "day1/c.jpg")
VS = ValidationSettings(frame_rate="30", target_duration_seconds=18.5)
PS = PlanSettings(frame_rate="30", target_duration_seconds=18.5)


def base(titles=None, vset=VS):
    sel = sel_plan([sel_clip("c1", A0, 5), sel_clip("c2", A1, 4), sel_clip("c3", B0, 3)], [sel_clip("c4", P0, 3.5, audio="muted"), sel_clip("c5", STILL, 3)])
    sel = sel.model_copy(update={"schema_version": "1.1", "titles": titles or []})
    edit = resolve_selection_plan(sel, CAT, PS)
    return sel, edit, PlanContext(CAT, vset, PS, {"scene_001": [A0, A1, A2], "scene_002": [P0, mid(CAT, "day2/p.mp4", 1)]})


def op(name, **kw):
    return PlanOp.from_dict({"op": name, **kw})


def clip_of(plan, cid):
    return next(c for s in plan.scenes for c in s.clips if c.clip_instance_id == cid)


def refused_codes(res):
    return [r.code for r in res.refused]


def test_flat_records_with_nulls_parse_and_unused_fields_are_dropped():
    o = PlanOp.from_dict({"operation": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 3, "anchor": None, "moment_id": "ignored", "scene_id": None,
                          "to_position": None, "transition_type": None, "transition_seconds": None, "audio": None, "shorten_by_seconds": None, "reason": "  Too\nlong. "})
    assert o.to_dict() == {"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 3.0, "reason": "Too long."}
    assert set(OPERATIONS) == {"trim_clip", "swap_moment", "remove_clip", "move_clip", "set_transition", "set_clip_audio", "shorten_scene"}


@pytest.mark.parametrize("rec, why", [
    ({"op": "rewrite_plan"}, "unknown operation"),
    ({"op": "trim_clip", "clip_instance_id": "c1"}, "duration_seconds or anchor"),
    ({"op": "trim_clip", "duration_seconds": 3}, "clip_instance_id"),
    ({"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": -1}, "more than 0"),
    ({"op": "trim_clip", "clip_instance_id": "c1", "anchor": "middle"}, "anchor"),
    ({"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": float("nan")}, "finite"),
    ({"op": "move_clip", "clip_instance_id": "c1", "to_position": 0}, "from 1"),
    ({"op": "set_transition", "clip_instance_id": "c1", "transition_type": "wipe"}, "cut or dissolve"),
    ({"op": "set_transition", "clip_instance_id": "c1", "transition_type": "dissolve"}, "transition_seconds"),
    ({"op": "set_transition", "clip_instance_id": "c1", "transition_type": "dissolve", "transition_seconds": 30}, "0.1 to 5"),
    ({"op": "set_clip_audio", "clip_instance_id": "c1", "audio": "loud"}, "audio must be"),
    ({"op": "shorten_scene", "scene_id": "scene_001", "shorten_by_seconds": 0}, "more than 0"),
    ({"op": "swap_moment", "clip_instance_id": "c1"}, "moment_id"),
])
def test_malformed_operations_are_rejected(rec, why):
    with pytest.raises(OpError, match=why):
        PlanOp.from_dict(rec)


def test_trim_changes_duration_in_frames_and_keeps_the_rest_valid():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("trim_clip", clip_instance_id="c1", duration_seconds=4.0)], ctx, base_selection=sel)
    assert not res.refused and len(res.applied) == 1
    assert clip_of(res.edit, "c1").duration_frames == 120 and res.edit.total_duration_frames == edit.total_duration_frames - 30
    assert res.edit.plan_hash != edit.plan_hash and res.edit.selection_plan_hash == selection_plan_hash(res.selection)
    again = validate_edit_plan(res.edit, CAT, VS)
    assert again.ok and again.repairs == []
    assert res.diff and res.diff.total_delta_seconds == -1.0 and res.diff.scenes[0].delta_seconds == -1.0
    assert "c1: 5.00 s -> 4.00 s" in "\n".join(res.diff.lines())


def test_trim_by_anchor_moves_the_source_range():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("trim_clip", clip_instance_id="c1", anchor="start", duration_seconds=4.0)], ctx, base_selection=sel)
    c = clip_of(res.edit, "c1")
    assert c.anchor == "start" and c.source_in_seconds == 0.0 and c.duration_frames == 120


def test_trim_bounds_and_stills():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("trim_clip", clip_instance_id="c1", duration_seconds=9.0), op("trim_clip", clip_instance_id="c1", duration_seconds=0.4),
                           op("trim_clip", clip_instance_id="c5", anchor="start"), op("trim_clip", clip_instance_id="c5", duration_seconds=1.0),
                           op("trim_clip", clip_instance_id="c1", duration_seconds=5.0), op("trim_clip", clip_instance_id="nope", duration_seconds=2)],
                    ctx, base_selection=sel)
    assert refused_codes(res) == ["duration_exceeds_moment", "duration_too_short", "anchor_on_still", "duration_too_short", "no_change", "unknown_clip"]
    assert not res.applied and res.edit is edit and res.diff is None


def test_the_total_duration_tolerance_refuses_a_change_and_later_ops_still_run():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("trim_clip", clip_instance_id="c1", duration_seconds=1.5),      # -3.5 s: 15 s against 18.5 s +/- 2 s
                           op("trim_clip", clip_instance_id="c2", duration_seconds=3.0)], ctx, base_selection=sel)   # -1 s: fine
    assert refused_codes(res) == ["validation"] and "total_duration_out_of_tolerance" in res.refused[0].message
    assert [a.op.clip_instance_id for a in res.applied] == ["c2"]


def test_atomic_discards_everything_on_the_first_refusal():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("trim_clip", clip_instance_id="c2", duration_seconds=3.0), op("trim_clip", clip_instance_id="c1", duration_seconds=1.5)],
                    ctx, base_selection=sel, atomic=True)
    assert res.applied == [] and len(res.refused) == 1 and res.edit is edit


def test_must_include_blocks_a_removal_and_titles_block_a_removal_under_them():
    vs17 = replace(VS, target_duration_seconds=17.0)
    sel, edit, ctx = base(vset=vs17)
    res = apply_ops(edit, [op("remove_clip", clip_instance_id="c3")], replace(ctx, vset=replace(vs17, must_include=(B0,))), base_selection=sel)
    assert refused_codes(res) == ["validation"] and "must_include_missing" in res.refused[0].message
    ok = apply_ops(edit, [op("remove_clip", clip_instance_id="c3")], ctx, base_selection=sel)
    assert not ok.refused and ok.diff and "c3 removed" in "\n".join(ok.diff.lines())
    title = SelectionTitle(title_id="title_001", text="S1", role="scene_title", scene_id="scene_001", text_source="outline_scene_title",
                           start=TitleStart(relative_to="clip_start", clip_instance_id="c2", offset_seconds=0.0), duration_seconds=2.0)
    vset = replace(vs17, scene_titles=True)
    sel2, edit2, ctx2 = base([title], vset)
    assert [t.title_id for t in edit2.titles] == ["title_001"]
    res2 = apply_ops(edit2, [op("remove_clip", clip_instance_id="c2")], ctx2, base_selection=sel2)
    assert refused_codes(res2) == ["title_anchored"]
    res3 = apply_ops(edit2, [op("move_clip", clip_instance_id="c3", to_position=1)], ctx2, base_selection=sel2)   # titles follow their clip
    assert not res3.refused and res3.edit.titles[0].start_frame == clip_of(res3.edit, "c2").timeline_start_frame
    assert res3.diff.title_changes and "title_001" in res3.diff.title_changes[0]


def test_the_only_primary_clip_of_a_scene_stays():
    sel, edit, ctx = base(vset=replace(VS, target_duration_seconds=16.5))
    res = apply_ops(edit, [op("remove_clip", clip_instance_id="c4"), op("remove_clip", clip_instance_id="c5")], ctx, base_selection=sel)
    assert [a.op.clip_instance_id for a in res.applied] == ["c4"] and refused_codes(res) == ["last_primary_clip"]


def test_move_clip():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("move_clip", clip_instance_id="c3", to_position=1), op("move_clip", clip_instance_id="c1", to_position=7),
                           op("move_clip", clip_instance_id="c3", to_position=1)], ctx, base_selection=sel)
    assert [c.clip_instance_id for c in res.edit.scenes[0].clips] == ["c3", "c1", "c2"]
    assert refused_codes(res) == ["position_out_of_range", "no_change"]
    assert "order of the clips: c1, c2, c3 -> c3, c1, c2" in "\n".join(res.diff.lines())


def test_set_transition_rules():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("set_transition", clip_instance_id="c2", transition_type="dissolve", transition_seconds=0.5),
                           op("set_transition", clip_instance_id="c1", transition_type="dissolve", transition_seconds=0.5),
                           op("set_transition", clip_instance_id="c3", transition_type="dissolve", transition_seconds=4.5),
                           op("set_transition", clip_instance_id="c3", transition_type="cut"),
                           op("set_transition", clip_instance_id="c2", transition_type="dissolve", transition_seconds=0.5)], ctx, base_selection=sel)
    t = clip_of(res.edit, "c2").transition_in
    assert t.type == "dissolve" and t.duration_frames == 15
    assert refused_codes(res) == ["dissolve_on_first_clip", "transition_too_long", "no_change", "no_change"]


def test_a_dissolve_the_resolver_cannot_make_is_refused_not_silently_a_cut():
    # a whole 5 s moment at the end of its file (no source after it) cut into a whole 4 s moment at the start of its file (none before it)
    sel = sel_plan([sel_clip("c1", B1, 5.0, anchor="start"), sel_clip("c2", P0, 4.0, anchor="start")])
    sel = sel.model_copy(update={"schema_version": "1.1"})
    edit = resolve_selection_plan(sel, CAT, PS)
    ctx = PlanContext(CAT, ValidationSettings(frame_rate="30", target_duration_seconds=9.0), PS)
    res = apply_ops(edit, [op("set_transition", clip_instance_id="c2", transition_type="dissolve", transition_seconds=0.5)], ctx, base_selection=sel)
    assert refused_codes(res) == ["not_honoured"] and "would have become a cut" in res.refused[0].message


def test_set_clip_audio_maps_to_the_policies_and_not_for_photos():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("set_clip_audio", clip_instance_id="c1", audio="lower"), op("set_clip_audio", clip_instance_id="c2", audio="mute"),
                           op("set_clip_audio", clip_instance_id="c4", audio="keep"), op("set_clip_audio", clip_instance_id="c5", audio="mute"),
                           op("set_clip_audio", clip_instance_id="c3", audio="keep")], ctx, base_selection=sel)
    assert (clip_of(res.edit, "c1").audio_policy, clip_of(res.edit, "c2").audio_policy, clip_of(res.edit, "c4").audio_policy) == ("ambient_low", "muted", "natural_sound")
    assert refused_codes(res) == ["audio_on_still", "no_change"]


def test_swap_moment_only_for_the_scene_candidates_and_the_same_kind():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("swap_moment", clip_instance_id="c2", moment_id=A2), op("swap_moment", clip_instance_id="c1", moment_id=B0),
                           op("swap_moment", clip_instance_id="c1", moment_id=A2), op("swap_moment", clip_instance_id="c4", moment_id=mid(CAT, "day2/p.mp4", 1)),
                           op("swap_moment", clip_instance_id="c5", moment_id=P0)], ctx, base_selection=sel)
    assert clip_of(res.edit, "c2").moment_id == A2 and clip_of(res.edit, "c4").moment_id == mid(CAT, "day2/p.mp4", 1)
    assert refused_codes(res) == ["moment_not_candidate", "moment_in_use", "media_kind_differs"]
    none = apply_ops(edit, [op("swap_moment", clip_instance_id="c2", moment_id=A2)], replace(ctx, scene_candidates=None), base_selection=sel)
    assert refused_codes(none) == ["swap_unavailable"]


def test_shorten_scene_scales_clips_together_and_respects_minimums():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("shorten_scene", scene_id="scene_001", shorten_by_seconds=1.5)], ctx, base_selection=sel)
    s1 = next(s for s in res.edit.scenes if s.scene_id == "scene_001")
    assert abs(s1.duration_frames - (12 * 30 - 45)) <= 2 and all(c.duration_frames < o.duration_frames for c, o in zip(s1.clips, edit.scenes[0].clips))
    big = apply_ops(edit, [op("shorten_scene", scene_id="scene_001", shorten_by_seconds=11.0), op("shorten_scene", scene_id="scene_009", shorten_by_seconds=1)],
                    ctx, base_selection=sel)
    assert refused_codes(big) == ["scene_too_short_to_shorten", "unknown_scene"]


def test_changes_chain_and_each_sees_the_result_of_the_one_before():
    sel, edit, ctx = base()
    res = apply_ops(edit, [op("trim_clip", clip_instance_id="c1", duration_seconds=4.0), op("set_transition", clip_instance_id="c2", transition_type="dissolve",
                                                                                         transition_seconds=0.5),
                           op("trim_clip", clip_instance_id="c1", duration_seconds=4.0)], ctx, base_selection=sel)
    assert [a.op.op for a in res.applied] == ["trim_clip", "set_transition"] and refused_codes(res) == ["no_change"]
    assert clip_of(res.edit, "c1").duration_frames == 120 and clip_of(res.edit, "c2").transition_in.type == "dissolve"


def test_the_selection_plan_is_rebuilt_from_the_edit_plan_when_there_is_none_on_disk():
    sel, edit, ctx = base()
    rebuilt = selection_from_edit(edit)
    assert [c.model_dump() for s in rebuilt.scenes for c in s.clips] == [c.model_dump() for s in sel.scenes for c in s.clips]
    assert layout_signature(resolve_selection_plan(rebuilt, CAT, PS)) == layout_signature(edit)
    res = apply_ops(edit, [op("trim_clip", clip_instance_id="c1", duration_seconds=4.0)], ctx)      # no base_selection given
    assert res.applied and not res.refused


def test_a_plan_that_no_longer_resolves_to_itself_is_not_changed():
    sel, edit, ctx = base()
    doc = edit.model_dump()
    doc["scenes"][0]["clips"][0]["source_in_frame"] += 3
    doc["scenes"][0]["clips"][0]["source_out_frame"] += 3
    doc["scenes"][0]["clips"][0]["source_in_seconds"] += 0.1
    doc["scenes"][0]["clips"][0]["source_out_seconds"] += 0.1
    hand_edited = type(edit).model_validate(doc)
    with pytest.raises(PlanBaseError, match="differs from what its selection plan resolves to"):
        apply_ops(hand_edited, [op("trim_clip", clip_instance_id="c1", duration_seconds=4.0)], ctx)


def test_malformed_records_are_reported_as_refusals_not_raised():
    sel, edit, ctx = base()
    res = apply_ops(edit, [{"op": "explode"}, {"op": "trim_clip", "clip_instance_id": "c1", "duration_seconds": 4}], ctx, base_selection=sel)
    assert refused_codes(res) == ["malformed"] and len(res.applied) == 1
    assert res.refused[0].to_dict()["op"] == {"op": "explode"}


def test_diff_of_identical_plans_is_empty():
    sel, edit, ctx = base()
    d = diff_plans(edit, edit)
    assert d.lines() == ["No change."] and d.total_delta_seconds == 0 and d.changed_scene_ids == []
