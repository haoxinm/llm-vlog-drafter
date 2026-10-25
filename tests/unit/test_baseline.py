import json
import random
from dataclasses import replace

import pytest
from planning_helpers import aid, make_catalog, mid, mk_asset, mk_moments

from trip_vlog.config import parse_config
from trip_vlog.planning.baseline import BaselinePlanError, BaselineSettings, build_baseline_selection
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import to_json_dict
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.planning.validate import ValidationSettings, validate_edit_plan, validate_selection_plan

SET = BaselineSettings(target_duration_seconds=100.0)


def clips(plan):
    return [c for s in plan.scenes for c in s.clips]


def paths(plan, cat):
    return [cat.assets[cat.moments[c.moment_id].asset_id].relative_path for c in clips(plan)]


def trip(n_assets=6, per=3, start_hour=9, folder="day1", length=6.0, **kw):
    assets, moments = [], []
    for i in range(n_assets):
        a = mk_asset(f"{folder}/v{i:02d}.mp4", duration=per * length, capture=f"2026-10-01T{start_hour + i // 6:02d}:{(i * 5) % 60:02d}:00+00:00")
        assets.append(a)
        moments += mk_moments(a, [(j * length, (j + 1) * length) for j in range(per)], **kw)
    return PlanningCatalog(assets, moments)


def test_baseline_uses_hard_cuts_and_center_anchor_and_chronological_order():
    cat = make_catalog()
    plan = build_baseline_selection(cat, replace(SET, max_similar_shots_per_scene=5))
    assert plan.planner == "baseline" and all(c.transition_in.type == "cut" and c.transition_in.duration_seconds == 0 for c in clips(plan))
    assert all(c.anchor == "center" and c.track_role == "primary_video" for c in clips(plan))
    ps = paths(plan, cat)
    assert "music/song.wav" not in ps and ps[0] == "day1/a.mp4"
    order = [ps.index(p) for p in ("day1/a.mp4", "day1/b.mp4", "day1/c.jpg", "day2/p.mp4", "day2/silent.mp4") if p in ps]
    assert order == sorted(order)
    assert any("audio-only" in u for u in plan.uncertainties)
    assert [c.clip_instance_id for c in clips(plan)] == [f"clip_{i:03d}" for i in range(1, len(clips(plan)) + 1)]


def test_scenes_split_by_folder_day_and_time_gap():
    cat = make_catalog()
    plan = build_baseline_selection(cat, replace(SET, max_similar_shots_per_scene=5))
    assert [s.title for s in plan.scenes] == ["day1", "day2"]
    assert len({s.scene_id for s in plan.scenes}) == 2
    gap = [mk_asset("f/a.mp4", capture="2026-10-01T09:00:00+00:00"), mk_asset("f/b.mp4", capture="2026-10-01T09:20:00+00:00"),
           mk_asset("f/c.mp4", capture="2026-10-01T10:30:00+00:00"), mk_asset("f/d.mp4", capture="2026-10-02T00:10:00+00:00")]
    cat = PlanningCatalog(gap, [m for a in gap for m in mk_moments(a, [(0, 6)])])
    plan = build_baseline_selection(cat, SET)
    assert [len(s.clips) for s in plan.scenes] == [2, 1, 1]  # 20 min gap joins; 70 min gap splits; new day splits


def test_missing_capture_times_sort_by_path_after_dated_assets():
    dated = mk_asset("z/dated.mp4", capture="2026-10-01T09:00:00+00:00")
    undated = [mk_asset("b/two.mp4"), mk_asset("a/one.mp4")]
    cat = PlanningCatalog([*undated, dated], [m for a in (*undated, dated) for m in mk_moments(a, [(0, 6)])])
    plan = build_baseline_selection(cat, SET)
    assert paths(plan, cat) == ["z/dated.mp4", "a/one.mp4", "b/two.mp4"]
    assert any("no capture time" in u for u in plan.uncertainties)


def test_deterministic_regardless_of_catalog_insertion_order():
    cat = trip(8, 4)
    ref = json.dumps(to_json_dict(build_baseline_selection(cat, replace(SET, target_duration_seconds=60))), sort_keys=True)
    for seed in range(5):
        a, m = list(cat.assets.values()), list(cat.moments.values())
        random.Random(seed).shuffle(a)
        random.Random(seed).shuffle(m)
        again = build_baseline_selection(PlanningCatalog(a, m), replace(SET, target_duration_seconds=60))
        assert json.dumps(to_json_dict(again), sort_keys=True) == ref


@pytest.mark.parametrize("target", [12.0, 40.0, 100.0, 180.0])
def test_budget_close_to_target_and_clip_lengths(target):
    cat = trip(12, 4)  # 48 segments of 6 s, N=2 per asset -> 24 candidates x 5 s = 120 s of available footage
    s = replace(SET, target_duration_seconds=target, max_similar_shots_per_scene=2)
    plan = build_baseline_selection(cat, s)
    total = sum(c.target_duration_seconds for c in clips(plan))
    assert 3.0 <= min(c.target_duration_seconds for c in clips(plan)) and max(c.target_duration_seconds for c in clips(plan)) <= 5.0 + 1e-9
    available = 120.0
    assert total <= target + 1.0 + 5.0  # opening/closing may push slightly
    if target < available:
        assert total >= target - 5.0
    else:
        assert any("shorter than" in u for u in plan.uncertainties) and total == available


def test_opening_and_closing_clips_are_kept_under_a_tight_budget():
    cat = trip(10, 1)
    plan = build_baseline_selection(cat, replace(SET, target_duration_seconds=10.0))
    ps = paths(plan, cat)
    assert ps[0] == "day1/v00.mp4" and ps[-1] == "day1/v09.mp4" and len(ps) >= 2


def test_budget_spreads_across_scenes():
    a = trip(6, 1, folder="d1")
    b = trip(6, 1, folder="d2")
    cat = PlanningCatalog([*a.assets.values(), *b.assets.values()], [*a.moments.values(), *b.moments.values()])
    plan = build_baseline_selection(cat, replace(SET, target_duration_seconds=20.0))
    assert {s.title for s in plan.scenes} == {"d1", "d2"}


def test_segment_length_rules_and_stills():
    a = mk_asset("f/v.mp4", duration=20.0, capture="2026-10-01T09:00:00+00:00")
    moms = mk_moments(a, [(0, 6), (6, 8), (8, 8.5), (9, 12)])
    s = mk_asset("f/p.jpg", media_type="image", capture="2026-10-01T09:30:00+00:00")
    cat = PlanningCatalog([a, s], moms + mk_moments(s, []))
    plan = build_baseline_selection(cat, replace(SET, max_similar_shots_per_scene=10, still_duration_seconds=3.0))
    durs = [c.target_duration_seconds for c in clips(plan)]
    assert durs == [5.0, 2.0, 3.0, 3.0]  # 6 s -> 5; 2 s kept whole; 0.5 s skipped; 3 s; still 3 s


def test_similar_shot_cap_for_one_video_and_for_still_bursts():
    cat = trip(1, 8)
    plan = build_baseline_selection(cat, replace(SET, max_similar_shots_per_scene=2))
    assert len(clips(plan)) == 2
    stills = [mk_asset(f"f/p{i}.jpg", media_type="image", capture=f"2026-10-01T09:00:{i:02d}+00:00") for i in range(5)]
    later = mk_asset("f/q.jpg", media_type="image", capture="2026-10-01T09:10:00+00:00")
    cat = PlanningCatalog([*stills, later], [m for a in (*stills, later) for m in mk_moments(a, [])])
    plan = build_baseline_selection(cat, replace(SET, max_similar_shots_per_scene=2))
    assert len(clips(plan)) == 3  # burst of 5 -> 2, plus the later photo


def test_silent_dropouts_skipped_but_wholly_silent_clips_kept():
    a = mk_asset("f/v.mp4", duration=24.0, capture="2026-10-01T09:00:00+00:00")
    moms = mk_moments(a, [(0, 6), (6, 12), (12, 18), (18, 24)])
    moms[1] = replace(moms[1], features={**moms[1].features, "silent": True})
    cat = PlanningCatalog([a], moms)
    plan = build_baseline_selection(cat, replace(SET, max_similar_shots_per_scene=10))
    assert [c.moment_id for c in clips(plan)] == [moms[0].moment_id, moms[2].moment_id, moms[3].moment_id]
    assert all(c.audio_policy == "natural_sound" for c in clips(plan))
    b = mk_asset("f/quiet.mp4", duration=12.0, audio=1, capture="2026-10-01T10:00:00+00:00")
    cat = PlanningCatalog([b], mk_moments(b, [(0, 6), (6, 12)], silent=True))
    plan = build_baseline_selection(cat, replace(SET, max_similar_shots_per_scene=10))
    assert len(clips(plan)) == 2 and all(c.audio_policy == "muted" for c in clips(plan))
    muted = trip(1, 1)
    muted = PlanningCatalog([replace(a, audio_streams=0) for a in muted.assets.values()], muted.moments.values())
    assert clips(build_baseline_selection(muted, SET))[0].audio_policy == "muted"


def test_preserve_natural_audio_false_mutes():
    plan = build_baseline_selection(trip(1, 1), replace(SET, preserve_natural_audio=False))
    assert clips(plan)[0].audio_policy == "muted"


def test_must_include_and_exclude():
    cat = trip(10, 2)
    tight = replace(SET, target_duration_seconds=10.0, max_similar_shots_per_scene=2)
    plan = build_baseline_selection(cat, replace(tight, must_include=("day1/v05.mp4",)))
    assert "day1/v05.mp4" in paths(plan, cat)
    target_moment = mid(cat, "day1/v07.mp4", 1)
    plan = build_baseline_selection(cat, replace(tight, must_include=(target_moment,)))
    assert target_moment in [c.moment_id for c in clips(plan)] and [c.moment_id for c in clips(plan)].count(target_moment) == 1
    plan = build_baseline_selection(cat, replace(tight, must_exclude=("day1/v0[0-4].mp4",)))
    assert paths(plan, cat) and all(int(p[-6:-4]) >= 5 for p in paths(plan, cat))
    plan = build_baseline_selection(cat, replace(SET, must_include=("day1/v03.mp4", "ghost.mp4"), must_exclude=("day1/v03.mp4",)))
    assert "day1/v03.mp4" not in paths(plan, cat)
    assert any("ghost.mp4" in u for u in plan.uncertainties) and any("conflicts with must_exclude" in u for u in plan.uncertainties)
    # a must_include asset is forced in even if its only segment is very short
    short = mk_asset("f/blip.mp4", duration=0.6, capture="2026-10-01T09:00:00+00:00")
    cat2 = PlanningCatalog([short, *cat.assets.values()], [*mk_moments(short, [(0, 0.6)]), *cat.moments.values()])
    assert "f/blip.mp4" not in paths(build_baseline_selection(cat2, SET), cat2)
    assert "f/blip.mp4" in paths(build_baseline_selection(cat2, replace(SET, must_include=("f/blip.mp4",))), cat2)


def test_nothing_usable_raises():
    with pytest.raises(BaselinePlanError):
        build_baseline_selection(PlanningCatalog([], []), SET)
    music = mk_asset("m/song.wav", media_type="audio")
    with pytest.raises(BaselinePlanError):
        build_baseline_selection(PlanningCatalog([music], mk_moments(music, [(0, 15)])), SET)
    cat = trip(2, 1)
    with pytest.raises(BaselinePlanError):
        build_baseline_selection(cat, replace(SET, must_exclude=("day1/",)))
    missing = PlanningCatalog([replace(a, present=False) for a in cat.assets.values()], cat.moments.values())
    with pytest.raises(BaselinePlanError):
        build_baseline_selection(missing, SET)


def test_baseline_output_passes_validation_and_resolves_within_tolerance():
    cat = make_catalog()
    cfg = parse_config({"project": {"target_duration_seconds": 14, "frame_rate": "30000/1001"}, "editorial": {"max_similar_shots_per_scene": 3}})
    sel = build_baseline_selection(cat, BaselineSettings.from_config(cfg))
    vs = ValidationSettings.from_config(cfg)
    assert validate_selection_plan(sel, cat, vs).ok
    edit = resolve_selection_plan(sel, cat, PlanSettings.from_config(cfg))
    r = validate_edit_plan(edit, cat, vs)
    assert r.ok, r.format()
    assert abs(edit.total_duration_seconds - 14) <= vs.total_tolerance_seconds()
    assert edit.sequence.fps == "30000/1001"


def test_settings_from_config():
    cfg = parse_config({"project": {"target_duration_seconds": 90, "preserve_natural_audio": False},
                        "editorial": {"max_similar_shots_per_scene": 4, "must_include": ["a/"], "must_exclude": ["b.mp4"], "opening_hook_seconds": 6}})
    s = BaselineSettings.from_config(cfg)
    assert (s.target_duration_seconds, s.max_similar_shots_per_scene, s.must_include, s.must_exclude, s.preserve_natural_audio, s.opening_hook_seconds) == (
        90.0, 4, ("a/",), ("b.mp4",), False, 6.0)
    assert s.still_duration_seconds == 3.0 and (s.clip_min_seconds, s.clip_max_seconds) == (3.0, 5.0)
    assert PlanSettings.from_config(cfg).still_duration_seconds == 3.0
