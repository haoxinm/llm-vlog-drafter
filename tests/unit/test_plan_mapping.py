from pathlib import Path

import pytest
from planning_helpers import aid, make_catalog, mid, sel_clip, sel_plan

from trip_vlog.filmcraft.plan_mapping import (AMBIENT_LOW_GAIN_DB, MappingError, MappingSettings, edit_plan_to_spec,
                                              source_path_for)
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan

CAT = make_catalog()
ROOT = Path("/media/trip")


def spec_for(*clips, settings=None, scenes=None):
    plan = resolve_selection_plan(sel_plan(*(scenes or [list(clips)])), CAT, PlanSettings(target_duration_seconds=30))
    return plan, edit_plan_to_spec(plan, CAT, ROOT, settings)


def test_tracks_audio_and_stills():
    plan, res = spec_for(sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0),
                         sel_clip("c2", mid(CAT, "day1/c.jpg"), 2.0, audio="muted"),
                         sel_clip("c3", mid(CAT, "day1/a.mp4", 1), 3.0, audio="muted"),
                         sel_clip("c4", mid(CAT, "day1/a.mp4", 2), 3.0, audio="ambient_low"))
    p = {x.clip_instance_id: x for x in res.spec.placements}
    assert res.spec.tracks == ("V1", "V2", "A1", "A2")
    assert p["c1"].track == "V1" and p["c1"].include_linked_audio and p["c1"].audio_gain_db is None
    assert p["c2"].media_kind == "image" and p["c2"].source_out_seconds is None and not p["c2"].include_linked_audio
    assert not p["c3"].include_linked_audio
    assert p["c4"].audio_gain_db == AMBIENT_LOW_GAIN_DB == -12.0
    assert p["c1"].source_path == str(ROOT / "day1" / "a.mp4")
    assert res.used_asset_ids == [aid("day1/a.mp4"), aid("day1/c.jpg")]
    assert res.spec.markers and res.spec.markers[0].frame == 0


def test_silent_source_gets_no_linked_audio_and_a_warning():
    _, res = spec_for(sel_clip("c1", mid(CAT, "day2/silent.mp4", 0), 3.0))
    assert not res.spec.placements[0].include_linked_audio
    assert [w.code for w in res.warnings] == ["no_source_audio"]


def test_music_and_narration_tracks():
    _, res = spec_for(sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0),
                      sel_clip("m1", mid(CAT, "music/song.wav", 0), 3.0, role="music"),
                      sel_clip("n1", mid(CAT, "music/song.wav", 1), 3.0, role="narration"))
    by = {p.clip_instance_id: p for p in res.spec.placements}
    assert by["m1"].track == "A2" and by["n1"].track == "A3" and "A3" in res.spec.tracks and "A2" in res.spec.tracks


def test_unsupported_alignment_becomes_recorded_cut():
    plan, res = spec_for(sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0, "start"),
                         sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0, "center", dissolve=1.0),
                         sel_clip("c3", mid(CAT, "day1/a.mp4", 0), 3.0, "start", dissolve=1.0))
    assert plan.scenes[0].clips[2].transition_in.alignment == "start_at_cut"
    by = {p.clip_instance_id: p for p in res.spec.placements}
    assert by["c2"].transition_in.type == "dissolve" and by["c2"].transition_in.frames == 30
    assert by["c3"].transition_in.type == "cut"
    [w] = res.warnings
    assert w.code == "dissolve_alignment_unsupported" and w.clip_instance_id == "c3" and "start_at_cut" in w.message


def test_alignment_support_can_be_extended():
    _, res = spec_for(sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0, "start"),
                      sel_clip("c2", mid(CAT, "day1/a.mp4", 1), 3.0, "center", dissolve=1.0),
                      sel_clip("c3", mid(CAT, "day1/a.mp4", 0), 3.0, "start", dissolve=1.0),
                      settings=MappingSettings(supported_alignments=("center", "start_at_cut")))
    assert res.spec.placements[2].transition_in.type == "dissolve" and not res.warnings


def test_ntsc_frame_rate_and_exact_source_range():
    plan = resolve_selection_plan(sel_plan([sel_clip("c1", mid(CAT, "day1/b.mp4", 1), 3.0, "start")]), CAT,
                                  PlanSettings(frame_rate="30000/1001", target_duration_seconds=3))
    spec = edit_plan_to_spec(plan, CAT, ROOT).spec
    p = spec.placements[0]
    assert str(spec.fps) == "30000/1001" and (spec.width, spec.height) == (1920, 1080)
    assert round((p.source_out_seconds - p.source_in_seconds) * 30000 / 1001) == p.duration_frames == 90


def test_unknown_asset_is_a_mapping_error():
    plan, _ = spec_for(sel_clip("c1", mid(CAT, "day1/a.mp4", 0), 3.0))
    plan.scenes[0].clips[0].asset_id = "asset_0000000000000000"
    with pytest.raises(MappingError, match="not in the catalog"):
        edit_plan_to_spec(plan, CAT, ROOT)


def test_source_path_joins_posix_relative_path():
    assert source_path_for(ROOT, "a/b/c.mp4") == str(ROOT / "a" / "b" / "c.mp4")
