import copy
import json
from dataclasses import replace

import pytest
from planning_helpers import aid, make_catalog, mid, mk_asset, mk_moments, sel_clip, sel_plan

from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan, EditTransition, edit_plan_hash, to_json_dict, with_hash
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.planning.validate import ValidationSettings, validate_edit_plan, validate_selection_plan

CAT = make_catalog()
S = ValidationSettings(frame_rate="30", target_duration_seconds=9.0)


def base_selection():
    return sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0),
                     sel_clip("c3", mid(CAT, "day1/c.jpg"), 3.0)])


def base_edit(cat=CAT, selection=None) -> EditPlan:
    return resolve_selection_plan(selection or base_selection(), cat, PlanSettings(target_duration_seconds=9.0))


def mutate(plan: EditPlan, fn, rehash=True) -> EditPlan:
    d = copy.deepcopy(to_json_dict(plan))
    fn(d)
    p = EditPlan.model_validate(d)
    return with_hash(p) if rehash else p


def clip(d, i):
    return d["scenes"][0]["clips"][i]


def check(plan, cat=CAT, settings=S):
    return validate_edit_plan(plan, cat, settings)


def test_valid_plan_is_clean_and_report_is_json():
    r = check(base_edit())
    assert r.ok and r.errors == [] and r.warnings == [] and r.repairs == []
    json.dumps(r.to_dict())
    assert "errors: 0" in r.format()


def test_schema_errors_for_malformed_documents():
    d = to_json_dict(base_edit())
    d["scenes"][0]["clips"][0]["duration_frames"] = 0
    d["scenes"][0]["clips"][1]["asset_id"] = "not-an-id"
    r = validate_edit_plan(d, CAT, S)
    assert not r.ok and set(r.codes()) == {"schema"} and len(r.errors) == 2 and r.plan is None
    assert not validate_selection_plan({"schema_version": "1.0"}, CAT, S).ok


# ---- referential integrity (deliberately invalid IDs, DESIGN 19.3 item 3) ---------------------------------

def test_unknown_asset_and_moment_ids():
    r = check(mutate(base_edit(), lambda d: clip(d, 0).update(asset_id="asset_" + "0" * 16)))
    assert "asset_not_found" in r.codes() and "moment_asset_mismatch" in r.codes()
    r = check(mutate(base_edit(), lambda d: clip(d, 1).update(moment_id="moment_doesnotexist")))
    assert r.codes() == ["moment_not_found"] and r.errors[0].clip_instance_id == "c2"


def test_moment_must_belong_to_asset():
    other = mid(CAT, "day2/p.mp4", 0)
    r = check(mutate(base_edit(), lambda d: clip(d, 0).update(moment_id=other)))
    assert "moment_asset_mismatch" in r.codes()


def test_duplicate_ids():
    r = check(mutate(base_edit(), lambda d: clip(d, 1).update(clip_instance_id="c1")))
    assert "duplicate_clip_instance_id" in r.codes()
    r = check(mutate(base_edit(), lambda d: d["scenes"].append({**copy.deepcopy(d["scenes"][0]), "clips": [copy.deepcopy(clip(d, 0))], "start_frame": 270,
                                                                 "duration_frames": 90})))
    assert "duplicate_scene_id" in r.codes() and "duplicate_clip_instance_id" in r.codes()


def test_missing_and_unreadable_assets_stale_warning():
    a = CAT.assets[aid("day1/a.mp4")]
    for changed, code in ((replace(a, present=False), "asset_missing"), (replace(a, probe_ok=False), "asset_unreadable")):
        cat = PlanningCatalog([changed, *[x for x in CAT.assets.values() if x is not a]], CAT.moments.values())
        assert code in check(base_edit(), cat).codes()
    cat = PlanningCatalog([replace(a, analysis_status="stale"), *[x for x in CAT.assets.values() if x is not a]], CAT.moments.values())
    r = check(base_edit(), cat)
    assert r.ok and "asset_stale" in r.codes("warnings")


def test_media_type_mismatch():
    assert "media_type_mismatch" in check(mutate(base_edit(), lambda d: clip(d, 0).update(media_type="image"))).codes()


# ---- time correctness -----------------------------------------------------------------------------------

def test_source_out_far_beyond_asset_is_error_not_repaired():
    a = CAT.assets[aid("day1/a.mp4")]  # 20 s
    m = mk_moments(a, [(17, 20)])[0]
    cat = PlanningCatalog([a], [m])
    sel = sel_plan([sel_clip("c1", m.moment_id, 3.0, "end")])
    plan = resolve_selection_plan(sel, cat, PlanSettings(target_duration_seconds=3.0))

    def push(d):
        c = clip(d, 0)
        c.update(source_out_seconds=23.0, source_out_frame=690, duration_frames=690 - c["source_in_frame"])
    r = check(mutate(plan, push), cat, ValidationSettings(target_duration_seconds=3.0))
    assert "source_out_of_range" in r.codes() and "clamp_to_asset_end" not in [x.code for x in r.repairs]
    assert r.plan.scenes[0].clips[0].source_out_seconds == 23.0  # left as the planner wrote it


def test_source_out_slightly_past_asset_end_is_repaired_and_recorded():
    a = CAT.assets[aid("day1/a.mp4")]
    m = mk_moments(a, [(17, 20)])[0]
    cat = PlanningCatalog([a], [m])
    plan = resolve_selection_plan(sel_plan([sel_clip("c1", m.moment_id, 3.0, "end")]), cat, PlanSettings(target_duration_seconds=3.0))

    def push(d):
        c = clip(d, 0)
        c.update(source_out_seconds=20.033333, source_out_frame=601, duration_frames=91)  # one frame past the end of the file
        d["total_duration_frames"], d["total_duration_seconds"] = 91, 3.033333
        d["scenes"][0]["duration_frames"] = 91
    r = check(mutate(plan, push), cat, ValidationSettings(target_duration_seconds=3.0))
    assert r.ok
    assert "clamp_to_asset_end" in [x.code for x in r.repairs]
    fixed = r.plan.scenes[0].clips[0]
    assert fixed.source_out_seconds == 20.0 and fixed.source_out_frame == 600 and fixed.duration_frames == 90 and r.plan.total_duration_frames == 90
    assert r.plan.plan_hash == edit_plan_hash(r.plan)
    again = check(r.plan, cat, ValidationSettings(target_duration_seconds=3.0))
    assert again.ok and again.repairs == []  # repairs are idempotent


def test_out_not_after_in_and_outside_moment():
    r = check(mutate(base_edit(), lambda d: clip(d, 0).update(source_out_seconds=clip(d, 0)["source_in_seconds"])))
    assert "source_out_not_after_in" in r.codes()
    r = check(mutate(base_edit(), lambda d: clip(d, 0).update(source_in_seconds=7.0, source_out_seconds=10.0, source_in_frame=210, source_out_frame=300)))
    assert "outside_moment" in r.codes()  # inside the asset (20 s) but outside the 0-6 s moment


def test_missing_source_range_for_video():
    assert "missing_source_range" in check(mutate(base_edit(), lambda d: clip(d, 0).update(source_in_seconds=None))).codes()


def test_frame_alignment_repair_and_warning():
    plan = mutate(base_edit(), lambda d: clip(d, 0).update(source_in_seconds=1.51))
    r = check(plan)
    assert r.ok and [x.code for x in r.repairs] == ["frame_rounding"] and r.plan.scenes[0].clips[0].source_in_seconds == 1.5
    r = check(plan, settings=replace(S, repair=False))
    assert "not_frame_aligned" in r.codes("warnings") and r.repairs == []


def test_duration_frames_mismatch():
    r = check(mutate(base_edit(), lambda d: clip(d, 0).update(duration_frames=95)))
    assert "duration_mismatch" in r.codes()
    r = check(mutate(base_edit(), lambda d: clip(d, 0).update(duration_frames=91)))
    assert [x.code for x in r.repairs][:1] == ["duration_frames"] and r.plan.scenes[0].clips[0].duration_frames == 90 and r.ok


def test_frame_fields_and_layout_repairs_or_errors():
    plan = mutate(base_edit(), lambda d: (clip(d, 1).update(timeline_start_frame=100), clip(d, 0).update(source_in_frame=44)))
    r = check(plan)
    assert r.ok and {x.code for x in r.repairs} == {"timeline_position", "frame_fields"}
    assert r.plan.scenes[0].clips[1].timeline_start_frame == 90 and r.plan.scenes[0].clips[0].source_in_frame == 45
    r = check(plan, settings=replace(S, repair=False))
    assert {"timeline_position", "frame_field_mismatch"} <= set(r.codes())


def test_scene_marker_and_total_repairs():
    plan = mutate(base_edit(), lambda d: (d["scenes"][0].update(duration_frames=10), d["markers"][0].update(frame=5), d.update(total_duration_frames=100)))
    r = check(plan)
    assert r.ok and {x.code for x in r.repairs} == {"scene_span", "marker_position", "total_frames"} and r.plan.total_duration_frames == 270


def test_total_duration_tolerance():
    assert S.total_tolerance_seconds() == 2.0
    assert ValidationSettings(target_duration_seconds=180).total_tolerance_seconds() == 9.0  # 5% beats 2 s
    plan = base_edit()  # 9 s
    assert check(plan, settings=replace(S, target_duration_seconds=10.9)).ok
    r = check(plan, settings=replace(S, target_duration_seconds=11.5))
    assert "total_duration_out_of_tolerance" in r.codes()
    assert check(plan, settings=replace(S, target_duration_seconds=9.5, duration_tolerance_seconds=0.1, duration_tolerance_pct=1)).ok is False


def test_still_durations_positive():
    d = to_json_dict(base_edit())
    d["scenes"][0]["clips"][2]["duration_frames"] = 0
    assert "schema" in validate_edit_plan(d, CAT, S).codes()
    r = check(mutate(base_edit(), lambda dd: clip(dd, 2).update(source_in_seconds=1.0, source_out_seconds=2.0)))
    assert "still_has_source_range" in r.codes("warnings")


def test_unexplained_shortfall_warning():
    plan = mutate(base_edit(), lambda d: clip(d, 0).update(requested_duration_seconds=5.0))
    assert "unexplained_shortfall" in check(plan).codes("warnings")


# ---- transitions ----------------------------------------------------------------------------------------

def test_transition_longer_than_adjacent_clip():
    t = {"type": "dissolve", "duration_seconds": 4.0, "duration_frames": 120, "alignment": "center"}
    assert "transition_exceeds_clip" in check(mutate(base_edit(), lambda d: clip(d, 1).update(transition_in=t))).codes()


def test_transition_needs_handles():
    sel = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0, "start"), sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0, "start")])
    plan = resolve_selection_plan(sel, CAT, PlanSettings(target_duration_seconds=6))
    d = {"type": "dissolve", "duration_seconds": 1.0, "duration_frames": 30, "alignment": "center"}
    ok = check(mutate(plan, lambda x: clip(x, 1).update(transition_in=d)), settings=replace(S, target_duration_seconds=6))
    assert ok.ok  # c2 starts at 6 s of its asset: plenty of head handle; c1 has tail handle
    # an incoming clip at the very start of its asset has no head handle
    sel2 = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 1), 3.0), sel_clip("c2", mid(CAT, "day1/a.mp4", 0), 3.0, "start")])
    plan2 = resolve_selection_plan(sel2, CAT, PlanSettings(target_duration_seconds=6))
    r = check(mutate(plan2, lambda x: clip(x, 1).update(transition_in=d)), settings=replace(S, target_duration_seconds=6))
    assert "insufficient_handles" in r.codes()
    shifted = dict(d, alignment="start_at_cut")  # needs only the outgoing clip's tail
    assert check(mutate(plan2, lambda x: clip(x, 1).update(transition_in=shifted)), settings=replace(S, target_duration_seconds=6)).ok


def test_transition_repairs_to_cut():
    d = {"type": "dissolve", "duration_seconds": 1.0, "duration_frames": 30, "alignment": "center"}
    r = check(mutate(base_edit(), lambda x: clip(x, 0).update(transition_in=d)))
    assert r.ok and [x.code for x in r.repairs] == ["dissolve_to_cut"] and r.plan.scenes[0].clips[0].transition_in.type == "cut"
    r = check(mutate(base_edit(), lambda x: clip(x, 1).update(transition_in={**d, "type": "cut"})))
    assert [x.code for x in r.repairs] == ["cut_duration"]
    r = check(mutate(base_edit(), lambda x: clip(x, 1).update(transition_in={**d, "duration_frames": 1, "duration_seconds": 0.033})))
    assert "transition_too_short" in r.codes()


# ---- duplicates, orientation, audio, budgets ---------------------------------------------------------------

def test_duplicate_source_ranges():
    m0 = mid(CAT, "day1/a.mp4", 0)
    sel = sel_plan([sel_clip("c1", m0, 3.0, "start"), sel_clip("c2", m0, 3.0, "start")])
    s6 = replace(S, target_duration_seconds=6)
    plan = resolve_selection_plan(sel, CAT, PlanSettings(target_duration_seconds=6))
    r = check(plan, settings=s6)
    assert "unexplained_repeat" in r.codes()
    explained = sel_plan([sel_clip("c1", m0, 3.0, "start"), sel_clip("c2", m0, 3.0, "start", repeat="callback to the opening")])
    assert check(resolve_selection_plan(explained, CAT, PlanSettings(target_duration_seconds=6)), settings=s6).ok
    partial = sel_plan([sel_clip("c1", m0, 3.0, "start"), sel_clip("c2", m0, 3.0, "end")])  # 0-3 vs 3-6: touching, no overlap
    assert check(resolve_selection_plan(partial, CAT, PlanSettings(target_duration_seconds=6)), settings=s6).warnings == []
    slight = sel_plan([sel_clip("c1", m0, 4.0, "start"), sel_clip("c2", m0, 4.0, "end")])  # 0-4 vs 2-6: 50% overlap -> error
    assert "unexplained_repeat" in check(resolve_selection_plan(slight, CAT, PlanSettings(target_duration_seconds=8)), settings=replace(S, target_duration_seconds=8)).codes()
    small = sel_plan([sel_clip("c1", m0, 3.5, "start"), sel_clip("c2", m0, 3.5, "end")])  # 0-3.5 vs 2.5-6: 1 s overlap of 3.5 s -> warning
    r = check(resolve_selection_plan(small, CAT, PlanSettings(target_duration_seconds=7)), settings=replace(S, target_duration_seconds=7))
    assert r.ok and "overlapping_source_ranges" in r.codes("warnings")


def test_same_still_twice_and_duplicate_content_fingerprint():
    still = mid(CAT, "day1/c.jpg")
    sel = sel_plan([sel_clip("c1", still, 3.0), sel_clip("c2", still, 3.0)])
    assert "unexplained_repeat" in check(resolve_selection_plan(sel, CAT, PlanSettings(target_duration_seconds=6)), settings=replace(S, target_duration_seconds=6)).codes()
    a1, a2 = mk_asset("x/one.mp4", cv="fast:same"), mk_asset("y/two.mp4", cv="fast:same")
    cat = PlanningCatalog([a1, a2], mk_moments(a1, [(0, 6)]) + mk_moments(a2, [(0, 6)]))
    sel = sel_plan([sel_clip("c1", mid(cat, "x/one.mp4"), 3.0), sel_clip("c2", mid(cat, "y/two.mp4"), 3.0)])
    r = check(resolve_selection_plan(sel, cat, PlanSettings(target_duration_seconds=6)), cat, replace(S, target_duration_seconds=6))
    assert r.ok and "duplicate_content" in r.codes("warnings")


def test_orientation_review_notes():
    sel = sel_plan([sel_clip("c1", mid(CAT, "day2/p.mp4", 0), 3.0)])
    r = check(resolve_selection_plan(sel, CAT, PlanSettings(target_duration_seconds=3)), settings=replace(S, target_duration_seconds=3))
    assert r.ok and "orientation_mismatch" in r.codes("warnings") and "portrait" in r.warnings[0].message
    sel = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0)])
    plan = resolve_selection_plan(sel, CAT, PlanSettings(target_duration_seconds=3, aspect_ratio="9:16"))
    assert "orientation_mismatch" in check(plan, settings=replace(S, target_duration_seconds=3)).codes("warnings")


def test_audio_roles_and_policies():
    s3 = replace(S, target_duration_seconds=6)
    sel = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0),
                    sel_clip("m1", mid(CAT, "music/song.wav", 0), 3.0, role="music")])
    plan = resolve_selection_plan(sel, CAT, PlanSettings(target_duration_seconds=6))
    assert check(plan, settings=s3).ok
    assert "music_not_allowed" in check(plan, settings=replace(s3, music_policy="none")).codes()
    nar = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("n1", mid(CAT, "music/song.wav", 0), 3.0, role="narration")])
    assert "narration_not_allowed" in check(resolve_selection_plan(nar, CAT, PlanSettings(target_duration_seconds=3)), settings=replace(S, target_duration_seconds=3)).codes()
    silent = mid(CAT, "day2/silent.mp4", 0)
    bad = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("n1", silent, 3.0, role="natural_audio")])
    assert "no_audio" in check(resolve_selection_plan(bad, CAT, PlanSettings(target_duration_seconds=3)), settings=replace(S, target_duration_seconds=3)).codes()
    warn = sel_plan([sel_clip("c1", silent, 3.0, audio="natural_sound")])
    assert "silent_source" in check(resolve_selection_plan(warn, CAT, PlanSettings(target_duration_seconds=3)), settings=replace(S, target_duration_seconds=3)).codes("warnings")
    track = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("o1", mid(CAT, "music/song.wav", 0), 3.0, role="overlay")])
    assert "track_media_mismatch" in check(resolve_selection_plan(track, CAT, PlanSettings(target_duration_seconds=3)), settings=replace(S, target_duration_seconds=3)).codes()


def test_non_primary_overlap_and_overhang_warnings():
    sel = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("m1", mid(CAT, "music/song.wav", 0), 5.0, role="music"),
                    sel_clip("m2", mid(CAT, "music/song.wav", 1), 5.0, role="music")])
    plan = resolve_selection_plan(sel, CAT, PlanSettings(target_duration_seconds=3))
    s3 = replace(S, target_duration_seconds=3)
    assert check(plan, settings=s3).codes("warnings") == ["beyond_timeline_end", "beyond_timeline_end"]
    overlapping = mutate(plan, lambda d: clip(d, 2).update(timeline_start_frame=60))
    assert "track_overlap" in check(overlapping, settings=s3).codes("warnings")


def test_scene_over_budget_warning():
    plan = mutate(base_edit(), lambda d: d["scenes"][0].update(target_duration_seconds=5.0))
    assert "scene_over_budget" in check(plan).codes("warnings")


def test_plan_hash():
    plan = mutate(base_edit(), lambda d: clip(d, 0).update(rationale="changed after resolution"), rehash=False)
    assert "plan_hash_mismatch" in check(plan).codes()
    assert check(plan, settings=replace(S, check_hash=False)).ok


# ---- must_include / must_exclude ---------------------------------------------------------------------------

def test_must_include_exclude_on_edit_plans():
    plan = base_edit()
    assert check(plan, settings=replace(S, must_include=("day1/c.jpg", "day1/*.mp4"))).ok
    r = check(plan, settings=replace(S, must_include=("day2/p.mp4",)))
    assert "must_include_missing" in r.codes()
    r = check(plan, settings=replace(S, must_include=("nope/missing.mp4",)))
    assert r.ok and "must_include_unavailable" in r.codes("warnings")
    r = check(plan, settings=replace(S, must_exclude=("day1/c.jpg",)))
    assert r.codes() == ["excluded_included"] and r.errors[0].clip_instance_id == "c3"
    assert "excluded_included" in check(plan, settings=replace(S, must_exclude=(mid(CAT, "day1/a.mp4", 0),))).codes()
    assert "excluded_included" in check(plan, settings=replace(S, must_exclude=("day1/",))).codes()
    assert "excluded_included" in check(plan, settings=replace(S, must_exclude=(aid("day1/a.mp4"),))).codes()


# ---- selection plans ----------------------------------------------------------------------------------------

def sel_report(sel, **kw):
    return validate_selection_plan(sel, CAT, replace(S, **kw))


def test_selection_plan_valid():
    r = sel_report(base_selection())
    assert r.ok and r.warnings == []


def test_selection_unknown_moment_and_duplicates():
    sel = sel_plan([sel_clip("c1", "moment_missing"), sel_clip("c1", mid(CAT, "day1/a.mp4", 0))])
    r = sel_report(sel)
    assert {"moment_not_found", "duplicate_clip_instance_id"} <= set(r.codes())
    twice = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0)), sel_clip("c2", mid(CAT, "day1/a.mp4", 0))])
    assert "unexplained_repeat" in sel_report(twice).codes()
    explained = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0)), sel_clip("c2", mid(CAT, "day1/a.mp4", 0), repeat="running gag")])
    assert "unexplained_repeat" not in sel_report(explained).codes()
    dup_scene = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0))], [sel_clip("c2", mid(CAT, "day1/a.mp4", 1))])
    dup_scene.scenes[1].scene_id = "scene_001"
    assert "duplicate_scene_id" in sel_report(dup_scene).codes()


def test_selection_duration_feasibility_and_budgets():
    long = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 30.0)])
    r = sel_report(long, target_duration_seconds=30.0)
    assert r.ok and "duration_exceeds_moment" in r.codes("warnings")
    tiny = PlanningCatalog(CAT.assets.values(), [replace(CAT.moments[mid(CAT, "day1/a.mp4", 0)], source_out_seconds=0.01)])
    assert "moment_too_short" in validate_selection_plan(sel_plan([sel_clip("c1", mid(tiny, "day1/a.mp4", 0), 1.0)]), tiny, S).codes()
    off = sel_report(base_selection(), target_duration_seconds=60.0)
    assert off.ok and "total_duration_off_target" in off.codes("warnings")
    over = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 5.0)], budgets=[2.0])
    assert "scene_over_budget" in sel_report(over, target_duration_seconds=5.0).codes("warnings")


def test_selection_roles_transitions_and_constraints():
    bad_role = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0)), sel_clip("m1", mid(CAT, "day2/silent.mp4", 0), role="music")])
    assert "no_audio" in sel_report(bad_role).codes()
    odd = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), dissolve=1.0)])
    odd.scenes[0].clips[0].transition_in.type = "cut"
    assert "transition_inconsistent" in sel_report(odd, target_duration_seconds=3.0).codes("warnings")
    r = sel_report(base_selection(), must_include=("day2/p.mp4",), must_exclude=("day1/c.jpg",))
    assert {"must_include_missing", "excluded_included"} <= set(r.codes())
    near = sel_plan([sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0), sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0)])
    cat = PlanningCatalog(CAT.assets.values(), [replace(m, source_in_seconds=m.source_in_seconds - 1) if m.moment_id == mid(CAT, "day1/a.mp4", 1) else m
                                                 for m in CAT.moments.values()])
    assert "overlapping_source_ranges" in validate_selection_plan(near, cat, replace(S, target_duration_seconds=6)).codes("warnings")


def test_selection_schema_errors_from_raw_dict():
    d = to_json_dict(base_selection())
    d["scenes"][0]["clips"][0]["anchor"] = "middle"
    d["scenes"][0]["clips"][0]["source_in_seconds"] = 3.0
    r = validate_selection_plan(d, CAT, S)
    assert not r.ok and set(r.codes()) == {"schema"} and r.plan is None
