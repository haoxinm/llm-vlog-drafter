from dataclasses import replace
from fractions import Fraction

import pytest
from planning_helpers import NTSC, aid, make_catalog, mid, mk_asset, mk_moments, sel_clip, sel_plan

from trip_vlog.planning.catalog_view import MomentInfo, PlanningCatalog
from trip_vlog.planning.models import EditPlan, edit_plan_hash
from trip_vlog.planning.resolve_plan import PlanResolutionError, PlanSettings, resolve_selection_plan

CAT = make_catalog()


def resolve(*scenes, settings=None, cat=CAT):
    return resolve_selection_plan(sel_plan(*scenes), cat, settings or PlanSettings(target_duration_seconds=30))


def only_clip(plan):
    return plan.scenes[0].clips[0]


@pytest.mark.parametrize("anchor,rng", [("start", (0.0, 3.0)), ("center", (1.5, 4.5)), ("end", (3.0, 6.0))])
def test_anchors_inside_moment(anchor, rng):
    c = only_clip(resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0, anchor)]))
    assert (c.source_in_seconds, c.source_out_seconds) == rng and c.duration_frames == 90
    assert (c.source_in_frame, c.source_out_frame) == (int(rng[0] * 30), int(rng[1] * 30)) and c.resolver_notes == []


def test_anchor_best_without_subrange_centers_with_note():
    c = only_clip(resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 2.0, "best")]))
    assert (c.source_in_seconds, c.source_out_seconds) == (2.0, 4.0)
    assert any("no best sub-range" in n for n in c.resolver_notes)


def test_anchor_best_uses_subrange():
    a = CAT.assets[aid("day1/a.mp4")]
    base = CAT.moments_for_asset(a.asset_id)[0]
    m = replace(base, best_in_seconds=3.0, best_out_seconds=5.0)
    cat = PlanningCatalog([a], [m])
    c = only_clip(resolve([sel_clip("c1", m.moment_id, 2.0, "best")], cat=cat))
    assert (c.source_in_seconds, c.source_out_seconds) == (3.0, 5.0) and c.resolver_notes == []
    # sub-range shorter than the clip: centred on the sub-range, clamped inside the moment
    c = only_clip(resolve([sel_clip("c1", m.moment_id, 4.0, "best")], cat=cat))
    assert (c.source_in_seconds, c.source_out_seconds) == (2.0, 6.0) and any("shorter than the clip" in n for n in c.resolver_notes)
    m2 = replace(base, best_in_seconds=0.0, best_out_seconds=0.5)  # near the moment start: clamp keeps it inside
    c = only_clip(resolve([sel_clip("c1", m.moment_id, 4.0, "best")], cat=PlanningCatalog([a], [m2])))
    assert c.source_in_seconds == 0.0 and c.source_out_seconds == 4.0


def test_shortfall_uses_whole_moment_and_notes_it():
    c = only_clip(resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 10.0, "end")]))
    assert (c.source_in_seconds, c.source_out_seconds) == (0.0, 6.0) and c.duration_frames == 180 and c.requested_duration_seconds == 10.0
    assert any(n.startswith("shortfall") for n in c.resolver_notes)


def test_moment_clamped_to_asset_duration():
    a = CAT.assets[aid("day1/a.mp4")]  # 20 s
    m = MomentInfo("moment_over", a.asset_id, None, 18.0, 22.0)
    plan = resolve([sel_clip("c1", "moment_over", 4.0, "end")], cat=PlanningCatalog([a], [m]))
    c = only_clip(plan)
    assert c.source_out_seconds == 20.0 and c.source_in_seconds == 18.0 and any("clamped to asset duration" in n for n in c.resolver_notes)


def test_ntsc_frame_rounding_and_bounds():
    plan = resolve([sel_clip("c1", mid(CAT, "day1/b.mp4", 1), 3.0, "end")], settings=PlanSettings(frame_rate="30000/1001"))
    c = only_clip(plan)
    assert plan.sequence.fps == "30000/1001"
    assert c.duration_frames == 90  # 3 s * 29.97 = 89.91 -> 90
    assert c.source_out_frame == 299  # asset is 10.0 s = 299.7 frames: last whole frame
    assert c.source_in_frame == 299 - 90 and c.source_out_seconds == pytest.approx(299 / float(NTSC), abs=1e-6)
    assert c.source_out_seconds <= 10.0
    # moment [5, 10] starts at ceil(149.85) = 150 frames: never earlier than the moment start
    c = only_clip(resolve([sel_clip("c1", mid(CAT, "day1/b.mp4", 1), 100.0, "start")], settings=PlanSettings(frame_rate="30000/1001")))
    assert c.source_in_frame == 150 and c.source_in_seconds >= 5.0


def test_25fps_sequence():
    c = only_clip(resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 2.0, "start")], settings=PlanSettings(frame_rate="25")))
    assert c.duration_frames == 50 and c.source_out_frame == 50 and c.source_out_seconds == 2.0


def test_still_duration():
    plan = resolve([sel_clip("c1", mid(CAT, "day1/c.jpg"), 3.0)])
    c = only_clip(plan)
    assert c.media_type == "image" and c.duration_frames == 90 and c.source_in_seconds is None and c.source_out_frame is None
    assert plan.sequence.still_duration_seconds == 3.0


def test_layout_markers_totals_hash_and_determinism():
    s1 = [sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("c2", mid(CAT, "day1/c.jpg"), 2.0)]
    s2 = [sel_clip("c3", mid(CAT, "day2/p.mp4", 0), 4.0)]
    plan = resolve(s1, s2)
    starts = [(c.clip_instance_id, c.timeline_start_frame, c.duration_frames) for s in plan.scenes for c in s.clips]
    assert starts == [("c1", 0, 90), ("c2", 90, 60), ("c3", 150, 120)]
    assert [(s.start_frame, s.duration_frames) for s in plan.scenes] == [(0, 150), (150, 120)]
    assert [(m.frame, m.scene_id) for m in plan.markers] == [(0, "scene_001"), (150, "scene_002")]
    assert plan.total_duration_frames == 270 and plan.total_duration_seconds == 9.0
    assert plan.plan_hash == edit_plan_hash(plan)
    again = resolve(s1, s2)
    assert again.model_dump_json() == plan.model_dump_json()
    EditPlan.model_validate(plan.model_dump(mode="json"))


def test_sequence_settings_from_aspect_and_rate():
    plan = resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0))], settings=PlanSettings(aspect_ratio="9:16", frame_rate="30000/1001"))
    assert (plan.sequence.width, plan.sequence.height, plan.sequence.fps) == (1080, 1920, "30000/1001")


def test_non_primary_tracks_chain_from_scene_start():
    plan = resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("m1", mid(CAT, "music/song.wav", 0), 4.0, role="music", audio="natural_sound"),
                    sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0), sel_clip("m2", mid(CAT, "music/song.wav", 1), 4.0, role="music")])
    by = {c.clip_instance_id: c for c in plan.scenes[0].clips}
    assert (by["c1"].timeline_start_frame, by["c2"].timeline_start_frame) == (0, 90)
    assert (by["m1"].timeline_start_frame, by["m2"].timeline_start_frame) == (0, 120)
    assert plan.total_duration_frames == 180  # music does not extend the primary timeline


def test_unknown_references_are_all_reported():
    with pytest.raises(PlanResolutionError) as e:
        resolve([sel_clip("c1", "moment_nope"), sel_clip("c2", "moment_nope2")])
    assert len(e.value.issues) == 2 and "moment_nope" in e.value.issues[0]
    with pytest.raises(PlanResolutionError):
        resolve([sel_clip("m1", mid(CAT, "music/song.wav", 0), role="music")])  # no primary clip at all


def test_moment_shorter_than_one_frame_is_unresolvable():
    a = CAT.assets[aid("day1/a.mp4")]
    with pytest.raises(PlanResolutionError, match="shorter than one frame"):
        resolve([sel_clip("c1", "tiny")], cat=PlanningCatalog([a], [MomentInfo("tiny", a.asset_id, None, 1.0, 1.01)]))


# ---- transitions and handles --------------------------------------------------------------------------

def test_center_dissolve_with_handles():
    plan = resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0, dissolve=1.0)])
    t = plan.scenes[0].clips[1].transition_in
    assert (t.type, t.duration_frames, t.duration_seconds, t.alignment) == ("dissolve", 30, 1.0, "center")
    assert plan.scenes[0].clips[1].resolver_notes == [] and plan.total_duration_frames == 180  # dissolve does not change length


def test_dissolve_alignment_shifts_when_incoming_has_no_head_handle():
    plan = resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0, "start"), sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0, "center", dissolve=1.0),
                    sel_clip("c3", mid(CAT, "day1/a.mp4", 0), 3.0, "start", dissolve=1.0)])
    # c3 starts at source 0: no head handle -> start_at_cut needs the outgoing clip's tail (c2 has plenty)
    t = plan.scenes[0].clips[2].transition_in
    assert t.type == "dissolve" and t.alignment == "start_at_cut" and t.duration_frames == 30
    assert any("alignment changed to start_at_cut" in n for n in plan.scenes[0].clips[2].resolver_notes)


def test_dissolve_end_at_cut_when_outgoing_has_no_tail():
    b = CAT.assets[aid("day1/b.mp4")]
    m_end = CAT.moments_for_asset(b.asset_id)[1]  # ends at the asset end
    plan = resolve([sel_clip("c1", m_end.moment_id, 3.0, "end"), sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0, "center", dissolve=1.0)])
    t = plan.scenes[0].clips[1].transition_in
    assert t.alignment == "end_at_cut" and t.duration_frames == 30


def test_dissolve_becomes_cut_when_neither_side_has_handles():
    a = replace(CAT.assets[aid("day1/a.mp4")], duration_seconds=6.0)
    cat = PlanningCatalog([a], [MomentInfo("m_a", a.asset_id, None, 0.0, 6.0)])
    plan = resolve_selection_plan(sel_plan([sel_clip("c1", "m_a", 6.0, "start"), sel_clip("c2", "m_a", 6.0, "start", dissolve=1.0)]), cat,
                                  PlanSettings(target_duration_seconds=12))
    c = plan.scenes[0].clips[1]  # outgoing ends at the asset end, incoming starts at the asset start
    assert c.transition_in.type == "cut" and any("insufficient source handles" in n and "used a cut" in n for n in c.resolver_notes)


def test_dissolve_shrinks_to_what_the_handles_allow():
    a = replace(CAT.assets[aid("day1/a.mp4")], duration_seconds=10.0)
    cat = PlanningCatalog([a], [MomentInfo("m_a", a.asset_id, None, 0.0, 10.0)])
    # c1 covers 0-9.667 s (tail handle 10f); c2 covers 0.333-4.333 s? -> use end anchor of a 5 s window: head handle = 10f via "start" of a window in-moment
    plan = resolve_selection_plan(sel_plan([sel_clip("c1", "m_a", 9.0 + 2 / 3, "start"), sel_clip("c2", "m_a", 9.0 + 2 / 3, "end", dissolve=1.0)]), cat,
                                  PlanSettings(target_duration_seconds=20))
    c2 = plan.scenes[0].clips[1]
    # c2 source = [0.333, 10.0]: head handle 10f, tail irrelevant; c1 tail handle = 300-290 = 10f -> centre needs 15 -> shortened to 20f
    assert c2.transition_in.type == "dissolve" and c2.transition_in.alignment == "center" and c2.transition_in.duration_frames == 20
    assert any("shortened to" in n for n in c2.resolver_notes)


def test_dissolve_downgraded_with_notes():
    cat = CAT
    # first clip of the timeline
    p = resolve([sel_clip("c1", mid(cat, "day1/a.mp4", 0), 3.0, dissolve=1.0)])
    assert p.scenes[0].clips[0].transition_in.type == "cut" and any("first clip" in n for n in p.scenes[0].clips[0].resolver_notes)
    # non-primary track
    p = resolve([sel_clip("c1", mid(cat, "day1/a.mp4", 0), 3.0), sel_clip("o1", mid(cat, "day1/a.mp4", 1), 2.0, role="overlay", dissolve=1.0)])
    o = p.scenes[0].clips[1]
    assert o.transition_in.type == "cut" and any("non-primary" in n for n in o.resolver_notes)
    # transition longer than the adjacent clip is shortened to the clip
    p = resolve([sel_clip("c1", mid(cat, "day1/a.mp4", 0), 3.0), sel_clip("c2", mid(cat, "day1/a.mp4", 1), 0.5, dissolve=2.0)])
    c2 = p.scenes[0].clips[1]
    assert c2.transition_in.duration_frames <= c2.duration_frames and any("shortened" in n for n in c2.resolver_notes)
    # a single frame is not a dissolve
    p = resolve([sel_clip("c1", mid(cat, "day1/a.mp4", 0), 3.0), sel_clip("c2", mid(cat, "day1/a.mp4", 1), 3.0, dissolve=0.02)])
    assert p.scenes[0].clips[1].transition_in.type == "cut"


def test_dissolve_into_still_has_unlimited_handles():
    p = resolve([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0, "end"), sel_clip("c2", mid(CAT, "day1/c.jpg"), 3.0, dissolve=1.0)])
    assert p.scenes[0].clips[1].transition_in.alignment == "center"
