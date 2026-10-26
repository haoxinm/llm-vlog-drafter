"""Deterministic QC checks added in Phase 5a (DESIGN 14.1): speech truncation, audio discontinuity, music vs speech, scene order, decode."""
from __future__ import annotations

from qc_helpers import A1, A2, A3, AM, clip, make_plan

from trip_vlog.planning.catalog_view import AssetInfo, MomentInfo, PlanningCatalog
from trip_vlog.qc import checks as C
from trip_vlog.qc.checks import TranscriptSpan as T


def tr(start, end, text="we finally got to the top of the hill", **kw):
    return T(start, end, text, **kw)


# ---- speech truncated by a cut -----------------------------------------------------------------------------

def plan_speech(**kw):
    return make_plan(("scene_01", [clip("clip_a", 0, 90, src_in=2.0, **kw), clip("clip_b", 90, 90, asset=A2, src_in=1.0)]))   # a: source 2.0-5.0


def test_speech_cut_through_a_segment_names_clip_and_words():
    r = C.check_speech_truncation(plan_speech(), {A1: [tr(1.0, 6.0, "one two three four five six")]})
    assert r.status == C.WARN and r.name == "speech_truncation"
    edges = {(f["clip_instance_id"], f["edge"]): f for f in r.data["findings"]}
    start, end = edges[("clip_a", "in")], edges[("clip_a", "out")]
    assert start["scene_id"] == "scene_01" and start["transcript_id"] is None and start["words_cut_estimated"] is True
    assert start["segment_text"] == "one two three four five six" and start["words_cut"] and "one" in start["words_cut"]
    assert "clip_a" in r.detail and "cut through speech" in r.detail
    assert end["segment_start"] == 1.0 and end["source_seconds"] == 5.0 and "six" in end["words_cut"] and "one" not in end["words_cut"].split()


def test_speech_cut_out_edge_reports_the_words_after_the_cut():
    r = C.check_speech_truncation(plan_speech(), {A1: [tr(4.0, 7.0, "alpha beta gamma delta epsilon zeta")]})
    f = r.data["findings"][0]
    assert r.status == C.WARN and f["edge"] == "out" and f["clip_instance_id"] == "clip_a"
    assert f["words_cut"].split()[-1] == "zeta" and "alpha" not in f["words_cut"].split()


def test_speech_cut_within_tolerance_or_between_segments_passes():
    p = plan_speech()
    assert C.check_speech_truncation(p, {A1: [tr(0.5, 2.1)]}).status == C.PASS            # ends 0.1 s after the in point (tolerance 0.25)
    assert C.check_speech_truncation(p, {A1: [tr(0.5, 1.9), tr(5.1, 6.5)]}).status == C.PASS  # edges fall in the pauses
    assert C.check_speech_truncation(p, {A1: [tr(1.0, 4.0)]}, tolerance_seconds=1.5).status == C.PASS
    assert C.check_speech_truncation(p, {A1: [tr(1.0, 4.0)]}, tolerance_seconds=0.0).status == C.WARN


def test_speech_ignores_muted_clips_and_continuing_takes():
    seg = {A1: [tr(1.0, 4.0)]}
    assert C.check_speech_truncation(plan_speech(policy="muted"), seg).status == C.SKIPPED
    cont = make_plan(("s", [clip("c1", 0, 60, src_in=1.0), clip("c2", 60, 60, src_in=3.0)]))   # c2 continues exactly where c1 stopped
    r = C.check_speech_truncation(cont, {A1: [tr(0.5, 5.5)]})
    assert {(f["clip_instance_id"], f["edge"]) for f in r.data["findings"]} == {("c1", "in"), ("c2", "out")}  # only the outer edges are cuts


def test_speech_skipped_without_transcripts_or_timing():
    assert C.check_speech_truncation(plan_speech(), None).data["skipped"] is True
    assert C.check_speech_truncation(plan_speech(), {A3: [tr(0, 1)]}).data["skipped"] is True      # an asset the plan does not use
    r = C.check_speech_truncation(plan_speech(), {A1: [tr(0, 9, coarse=True)]})
    assert r.status == C.SKIPPED and "coarse" in r.detail and r.data["skipped"] is True


# ---- source-audio discontinuity ------------------------------------------------------------------------------

def audio(mean, active=0.9, **kw):
    return {"has_audio": True, "silent": False, "mean_db": mean, "active_ratio": active, **kw}


def two_clips(**kw):
    return make_plan(("s", [clip("c1", 0, 60, **kw), clip("c2", 60, 60, asset=A2)]))


def test_audio_discontinuity_big_jump_between_active_clips_warns():
    r = C.check_audio_discontinuity(two_clips(), {"c1": audio(-45.0), "c2": audio(-18.0)}, jump_db=12)
    assert r.status == C.WARN and r.data["findings"][0]["between"] == ["c1", "c2"] and r.data["findings"][0]["jump_db"] == 27.0
    assert r.data["findings"][0]["scenes"] == ["s", "s"] and "c1 -> c2" in r.detail


def test_audio_discontinuity_passes_small_jump_silence_muted_or_inactive():
    p = two_clips()
    assert C.check_audio_discontinuity(p, {"c1": audio(-30.0), "c2": audio(-25.0)}).status == C.PASS
    assert C.check_audio_discontinuity(p, {"c1": audio(-60.0, silent=True), "c2": audio(-18.0)}).status == C.PASS
    assert C.check_audio_discontinuity(p, {"c1": audio(-45.0, active=0.2), "c2": audio(-18.0)}).status == C.PASS
    assert C.check_audio_discontinuity(two_clips(policy="muted"), {"c1": audio(-45.0), "c2": audio(-18.0)}).status == C.PASS


def test_audio_discontinuity_counts_the_ambient_gain_and_skips_continuing_takes():
    ambient = make_plan(("s", [clip("c1", 0, 60, policy="ambient_low"), clip("c2", 60, 60, asset=A2)]))
    # -18 dB at -12 dB gain is -30 dB, 3 dB from c2: no jump. Without the gain it would be 12 dB.
    assert C.check_audio_discontinuity(ambient, {"c1": audio(-18.0), "c2": audio(-33.0)}, jump_db=12, ambient_gain_db=-12.0).status == C.PASS
    cont = make_plan(("s", [clip("c1", 0, 60, src_in=0.0), clip("c2", 60, 60, src_in=2.0)]))
    assert C.check_audio_discontinuity(cont, {"c1": audio(-45.0), "c2": audio(-18.0)}).status == C.PASS


def test_audio_discontinuity_skipped_without_measurements():
    r = C.check_audio_discontinuity(two_clips(), {})
    assert r.status == C.SKIPPED and r.data["skipped"] is True


def test_clip_audio_features_come_from_the_moment():
    plan = two_clips()
    m1 = MomentInfo("mom_c1", A1, features={"audio": audio(-20.0)})
    m2 = MomentInfo("mom_c2", A2, features={"audio": {"has_audio": False}})
    cat = PlanningCatalog([AssetInfo(A1, "a.mp4", "video"), AssetInfo(A2, "b.mp4", "video")], [m1, m2])
    assert C.clip_audio_features(plan, cat) == {"c1": m1.features["audio"]}


# ---- music vs speech ---------------------------------------------------------------------------------------------

def music_plan(music_policy="natural_sound", video_policy="natural_sound"):
    return make_plan(("s", [clip("v1", 0, 150, policy=video_policy, src_in=0.0),
                            clip("m1", 30, 90, asset=AM, media="audio", role="music", policy=music_policy)]))


def test_music_over_speech_warns_unless_lowered():
    seg = {A1: [tr(1.0, 3.0, "hello there")]}
    r = C.check_music_vs_speech(music_plan(), {}, seg)
    f = r.data["findings"][0]
    assert r.status == C.WARN and f["music_clip_instance_id"] == "m1" and f["clip_instance_id"] == "v1" and f["reason"] == "speech"
    assert f["scene_id"] == "s" and f["overlap_seconds"] == 3.0 and f["music_gain_db"] == 0.0 and "no ducking" in r.detail
    assert C.check_music_vs_speech(music_plan("ambient_low"), {}, seg, ambient_gain_db=-12.0).status == C.PASS      # -12 dB <= -6 dB
    assert C.check_music_vs_speech(music_plan("ambient_low"), {}, seg, ambient_gain_db=-3.0).status == C.WARN       # only -3 dB
    assert C.check_music_vs_speech(music_plan("muted"), {}, seg).status == C.PASS


def test_music_over_active_natural_sound_warns_but_silence_passes():
    assert C.check_music_vs_speech(music_plan(), {"v1": audio(-25.0)}).data["findings"][0]["reason"] == "active_sound"
    assert C.check_music_vs_speech(music_plan(), {"v1": audio(-60.0, silent=True)}).status == C.PASS
    assert C.check_music_vs_speech(music_plan(), {"v1": audio(-25.0, active=0.1)}).status == C.PASS
    assert C.check_music_vs_speech(music_plan(video_policy="muted"), {"v1": audio(-25.0)}).status == C.PASS


def test_music_speech_outside_the_overlap_does_not_count_and_relative_level_matters():
    # music plays source 1.0-4.0 of v1; speech at 4.5-6.0 is after it
    assert C.check_music_vs_speech(music_plan(), {}, {A1: [tr(4.5, 6.0)]}).status == C.PASS
    # the clip itself is quiet (ambient_low, -12 dB) and the music is at unity: the music is 12 dB LOUDER than the speech
    r = C.check_music_vs_speech(music_plan(video_policy="ambient_low"), {}, {A1: [tr(1.0, 3.0)]}, ambient_gain_db=-12.0)
    assert r.status == C.WARN and r.data["findings"][0]["relative_db"] == 12.0


def test_music_skipped_without_a_music_track():
    r = C.check_music_vs_speech(two_clips(), {})
    assert r.status == C.SKIPPED and r.data["skipped"] is True


def test_music_ignores_coarse_transcripts_and_reports_skipped_when_only_coarse_exists():
    coarse = {A1: [tr(0.0, 9.0, coarse=True)]}      # one segment spanning the whole file: no real timing
    r = C.check_music_vs_speech(music_plan(), {}, coarse)
    assert r.status == C.SKIPPED and "segment timing" in r.detail and r.data["skipped"] is True
    # with audio measurements the check still runs, but coarse text is not treated as speech
    quiet = C.check_music_vs_speech(music_plan(), {"v1": audio(-60.0, silent=True)}, coarse)
    assert quiet.status == C.PASS and "without segment timing" in quiet.detail
    loud = C.check_music_vs_speech(music_plan(), {"v1": audio(-25.0)}, coarse)
    assert loud.status == C.WARN and loud.data["findings"][0]["reason"] == "active_sound"
    # real timing next to coarse segments still counts
    assert C.check_music_vs_speech(music_plan(), {}, {A1: [tr(1.0, 3.0)]}).data["findings"][0]["reason"] == "speech"


def test_music_names_assets_that_were_not_checked_for_speech():
    coarse = {A1: [tr(0.0, 9.0, coarse=True)]}
    r = C.check_music_vs_speech(music_plan(), {"v1": audio(-60.0, silent=True)}, coarse)
    assert r.status == C.PASS and A1 in r.detail and "not checked for speech" in r.detail and r.data["coarse_transcript_assets"] == [A1]
    w = C.check_music_vs_speech(music_plan(), {"v1": audio(-25.0)}, coarse)
    assert w.status == C.WARN and A1 in w.detail and "not checked for speech" in w.detail
    assert "not checked for speech" not in C.check_music_vs_speech(music_plan(), {"v1": audio(-60.0, silent=True)}, {A1: [tr(1.0, 3.0)]}).detail


def test_skipped_status_does_not_change_the_overall_status():
    sk = C.CheckResult("x", C.SKIPPED, "skipped: nothing")
    assert C.overall_status([sk]) == C.PASS and C.overall_status([sk, C.CheckResult("y", C.WARN)]) == C.WARN


# ---- scene order -------------------------------------------------------------------------------------------------

def scenes_plan(*ids):
    return make_plan(*[(sid, [clip(f"c{i}", i * 60, 60)]) for i, sid in enumerate(ids)])


def test_scene_order_follows_the_outline():
    r = C.check_scene_order(scenes_plan("scene_01", "scene_02", "scene_03"), ["scene_01", "scene_02", "scene_03"])
    assert r.status == C.PASS and "3 scene(s)" in r.detail
    dropped = C.check_scene_order(scenes_plan("scene_01", "scene_03"), ["scene_01", "scene_02", "scene_03"])
    assert dropped.status == C.PASS and dropped.data["outline_scenes_not_in_plan"] == ["scene_02"]


def test_scene_order_warns_on_swaps_duplicates_and_unknown_scenes():
    r = C.check_scene_order(scenes_plan("scene_02", "scene_01"), ["scene_01", "scene_02"])
    assert r.status == C.WARN and r.data["inversions"] == [["scene_02", "scene_01"]] and "scene_02 before scene_01" in r.detail
    r = C.check_scene_order(scenes_plan("scene_01", "scene_09"), ["scene_01", "scene_02"])
    assert r.status == C.WARN and "scene_09" in r.detail
    assert C.check_scene_order(scenes_plan("scene_01", "scene_01"), ["scene_01"]).status == C.WARN


def test_scene_order_skipped_without_outline():
    r = C.check_scene_order(scenes_plan("scene_01"), None)
    assert r.status == C.SKIPPED and r.data["skipped"] is True


# ---- decode ------------------------------------------------------------------------------------------------------

def test_decode_evaluation_fails_only_on_a_nonzero_exit():
    assert C.evaluate_decode(0, "").status == C.PASS
    assert C.evaluate_decode(1, "moov atom not found\n").status == C.FAIL
    r = C.evaluate_decode(0, "[aac] Number of bands exceeds limit\n")
    assert r.status == C.WARN and r.data["stderr"]
    assert C.evaluate_decode(0, "", "ffmpeg not found").status == C.WARN
    assert C.check_preview_decodes(None, __import__("pathlib").Path("x.mp4")).status == C.WARN


def test_heuristic_checks_never_fail():
    for r in (C.check_speech_truncation(plan_speech(), {A1: [tr(1.0, 4.0)]}), C.check_audio_discontinuity(two_clips(), {"c1": audio(-60), "c2": audio(-10)}),
              C.check_music_vs_speech(music_plan(), {"v1": audio(-20)}), C.check_scene_order(scenes_plan("b", "a"), ["a", "b"])):
        assert r.status in (C.PASS, C.WARN)
