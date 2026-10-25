from fractions import Fraction

import pytest

from trip_vlog.media.catalog import Catalog
from trip_vlog.media.segment import (Bounds, SegmentSettings, audio_features, build_segments, combine_boundaries, moment_id_for,
                                     moments_from_segments, parse_astats_output, parse_scene_output, parse_silence_output, segment_id_for)

F30, NTSC = Fraction(30), Fraction(30000, 1001)
ASSET = "asset_0123456789abcdef"


def spans(bs):
    return [(b.start_frame, b.end_frame) for b in bs]


def test_no_boundaries_long_clip_splits_evenly_to_window():
    bs = combine_boundaries(30 * 20, F30, [], [], window_seconds=6, min_seconds=1.5)
    assert spans(bs) == [(0, 150), (150, 300), (300, 450), (450, 600)]
    assert all(b.length <= 6 * 30 for b in bs) and sum(b.length for b in bs) == 600 and bs[0].start_frame == 0 and bs[-1].end_frame == 600
    assert [b.start_frame for b in bs[1:]] == [b.end_frame for b in bs[:-1]]  # contiguous


def test_scene_cuts_and_silence_edges_become_boundaries_with_scores():
    bs = combine_boundaries(300, F30, [(3.0, 0.8)], [(6.0, 7.0)], window_seconds=6, min_seconds=1.0)
    assert spans(bs) == [(0, 90), (90, 180), (180, 210), (210, 300)]
    assert [b.cut_score for b in bs] == [0.0, 0.8, 0.0, 0.0]


def test_short_segment_merges_into_neighbour_with_shorter_result():
    # cut at 1.0 s leaves a 1 s head (< 1.5 s min): merged into the only neighbour
    assert spans(combine_boundaries(180, F30, [(1.0, 0.5)], [], 6, 1.5)) == [(0, 180)]
    # a 1 s middle piece joins the shorter neighbour (previous is 2 s, next is 6 s)
    bs = combine_boundaries(270, F30, [(2.0, 0.5), (3.0, 0.5)], [], 6, 1.5)
    assert spans(bs) == [(0, 90), (90, 270)]


def test_short_merge_tie_prefers_weaker_cut():
    # middle piece 1 s between two 3 s neighbours: equal merged lengths; the weaker boundary (0.2) is removed
    bs = combine_boundaries(210, F30, [(3.0, 0.9), (4.0, 0.2)], [], 6, 1.5)
    assert spans(bs) == [(0, 90), (90, 210)]  # boundary at 4.0 s (score 0.2) removed, strong cut at 3.0 s kept


def test_whole_asset_shorter_than_min_stays_one_segment():
    assert spans(combine_boundaries(30, F30, [], [], 6, 1.5)) == [(0, 30)]
    assert combine_boundaries(0, F30, [], [], 6, 1.5) == []


def test_cuts_outside_range_and_duplicates_ignored():
    bs = combine_boundaries(300, F30, [(0.0, 1.0), (10.0, 1.0), (4.0, 0.3), (4.0, 0.6)], [(0.0, 10.0)], 6, 1.5)
    assert spans(bs) == [(0, 120), (120, 300)] and bs[1].cut_score == 0.6


def test_ntsc_frame_rounding_of_boundaries():
    bs = combine_boundaries(299, NTSC, [(5.005, 0.7)], [], 6, 1.5)  # 5.005 s = 150 frames at 30000/1001
    assert spans(bs) == [(0, 150), (150, 299)]


def test_window_may_be_exceeded_only_by_merge_and_stays_close():
    bs = combine_boundaries(30 * 13, F30, [(12.0, 0.5)], [], 6, 1.5)  # 12 s + 1 s tail -> tail merges into the last window
    assert all(b.length <= (6 + 1.5) * 30 for b in bs) and sum(b.length for b in bs) == 390


def test_determinism():
    args = (1234, NTSC, [(3.2, 0.5), (9.9, 0.4)], [(5.0, 5.8)], 6.0, 1.5)
    assert combine_boundaries(*args) == combine_boundaries(*args)


def test_segment_id_stability_and_sensitivity():
    a = segment_id_for(ASSET, 0, 150)
    assert a == segment_id_for(ASSET, 0, 150) and a.startswith("seg_") and len(a) == 20
    assert a != segment_id_for(ASSET, 0, 151) and a != segment_id_for("asset_ffffffffffffffff", 0, 150)
    assert segment_id_for(ASSET, None, None) == segment_id_for(ASSET, None, None) != a
    assert moment_id_for(a) == "moment_" + a[4:]


DET = {"cuts": [[2.0, 0.6]], "silences": [[3.0, 4.5]], "has_audio": True,
       "windows": [[i * 0.25, -20.0 if i * 0.25 < 3 else -80.0, -10.0] for i in range(24)]}


def test_build_segments_features_and_ids_are_stable():
    s = SegmentSettings(window_seconds=6, min_segment_seconds=1.0)
    a = build_segments(ASSET, "video", 6.0, F30, DET, s)
    b = build_segments(ASSET, "video", 6.0, F30, DET, s)
    assert a == b and [x["segment_id"] for x in a] == [x["segment_id"] for x in b]
    assert [(x["start_frame"], x["end_frame"]) for x in a] == [(0, 60), (60, 90), (90, 135), (135, 180)]
    first, second, third = a[0], a[1], a[2]
    assert first["features"]["scene_cut_score"] == 0.0 and a[1]["features"]["scene_cut_score"] == 0.6
    assert first["features"]["silent"] is False and first["features"]["audio_mean_db"] == pytest.approx(-20.0)
    assert second["features"]["silent"] is False  # 2.0-3.0 s is still loud
    assert third["features"]["silent"] is True and third["features"]["audio_mean_db"] == pytest.approx(-80.0)
    assert all(x["fps"] == "30" and x["segmenter_version"] == 1 for x in a)


def test_build_segments_window_change_keeps_detection_but_changes_segments():
    s6, s2 = SegmentSettings(window_seconds=6), SegmentSettings(window_seconds=2, min_segment_seconds=1.0)
    long_det = {"cuts": [], "silences": [], "windows": [], "has_audio": False}
    assert len(build_segments(ASSET, "video", 12.0, F30, long_det, s6)) == 2
    assert len(build_segments(ASSET, "video", 12.0, F30, long_det, s2)) == 6


def test_stills_and_audio_only_and_no_detection():
    s = SegmentSettings()
    still = build_segments(ASSET, "image", None, None, None, s)
    assert len(still) == 1 and still[0]["start_seconds"] is None and still[0]["features"] == {"still": True}
    assert still[0]["segment_id"] == segment_id_for(ASSET, None, None)
    audio = build_segments(ASSET, "audio", 14.0, None, {"cuts": [], "silences": [[6.0, 7.0]], "windows": [], "has_audio": True}, s)
    assert [(x["start_seconds"], x["end_seconds"]) for x in audio] == [(0.0, 6.0), (6.0, 10.5), (10.5, 14.0)]  # 1 s piece merged into the shorter neighbour
    assert all(x["fps"] == "1000" for x in audio)
    plain = build_segments(ASSET, "video", 7.0, F30, None, s)  # ffmpeg unavailable: window-only, no features
    assert [(x["start_seconds"], x["end_seconds"]) for x in plain] == [(0.0, 3.5), (3.5, 7.0)]
    assert plain[0]["features"]["silent"] is None and plain[0]["features"]["audio_mean_db"] is None
    assert build_segments(ASSET, "video", None, F30, None, s) == [] and build_segments(ASSET, "video", 0, F30, None, s) == []


def test_video_without_audio_is_flagged_silent():
    seg = build_segments(ASSET, "video", 4.0, F30, {"cuts": [], "silences": [], "windows": [], "has_audio": False}, SegmentSettings())[0]
    assert seg["features"]["silent"] is True and seg["features"]["has_audio"] is False and seg["features"]["clipped_audio_hint"] is False


def test_audio_features_clipping_and_silence():
    w = [[i * 0.25, -3.0, 0.0] for i in range(8)]
    f = audio_features(w, 0, 2, -35)
    assert f["clipped_audio_hint"] is True and f["silent"] is False and f["audio_peak_db"] == 0.0
    quiet = audio_features([[0.0, -60.0, -50.0]], 0, 1, -35)
    assert quiet["silent"] is True and quiet["clipped_audio_hint"] is False
    assert audio_features([], 0, 1, -35)["audio_mean_db"] is None
    assert audio_features([[0.0, float("-inf"), float("-inf")]], 0, 1, -35)["audio_mean_db"] == -120.0


def test_moments_from_segments():
    s = SegmentSettings()
    segs = build_segments(ASSET, "video", 4.0, F30, {"cuts": [], "silences": [], "windows": [[0.0, -25.0, -3.0]], "has_audio": True}, s)
    [m] = moments_from_segments(segs, "fast:abc")
    assert m["moment_id"] == moment_id_for(segs[0]["segment_id"]) and m["segment_id"] == segs[0]["segment_id"]
    assert m["description"] is None and m["source_in_seconds"] == 0.0 and m["source_out_seconds"] == 4.0 and m["source"] == "baseline"
    assert "video" in m["editorial_tags"] and "has_sound" in m["editorial_tags"] and m["content_version"] == "fast:abc"
    still = moments_from_segments(build_segments(ASSET, "image", None, None, None, s), None)[0]
    assert still["source_in_seconds"] is None and "still" in still["editorial_tags"]


def test_parsers():
    scene = "frame:0    pts:60060   pts_time:2.002\nlavfi.scene_score=0.558625\nframe:1 pts:1 pts_time:7.5\nlavfi.scene_score=0.31\r\n"
    assert parse_scene_output(scene) == [(2.002, 0.5586), (7.5, 0.31)]
    astats = ("frame:0    pts:0       pts_time:0\nlavfi.astats.Overall.Peak_level=-20.2\nlavfi.astats.Overall.RMS_level=-24.1\n"
              "frame:1    pts:4000    pts_time:0.25\nlavfi.astats.Overall.Peak_level=-inf\nlavfi.astats.Overall.RMS_level=-inf\n")
    assert parse_astats_output(astats) == [[0.0, -24.1, -20.2], [0.25, -120.0, -120.0]]
    log = "[silencedetect @ 0x1] silence_start: 1.5\n[silencedetect @ 0x1] silence_end: 2.5 | silence_duration: 1\n[silencedetect @ 0x1] silence_start: 8"
    assert parse_silence_output(log, 10.0) == [(1.5, 2.5), (8.0, 10.0)] and parse_silence_output(log, None) == [(1.5, 2.5)]
    assert parse_scene_output("") == [] and parse_astats_output("garbage") == []


def test_catalog_segments_and_moments_roundtrip(tmp_path):
    s = SegmentSettings()
    segs = build_segments(ASSET, "video", 12.0, F30, {"cuts": [], "silences": [], "windows": [], "has_audio": False}, s)
    with Catalog(tmp_path / "c.sqlite") as cat:
        cat.replace_segments(ASSET, "fast:1", segs)
        cat.replace_moments(ASSET, "fast:1", moments_from_segments(segs, "fast:1"))
        assert [r["segment_id"] for r in cat.list_segments(ASSET)] == [x["segment_id"] for x in segs]
        assert len(cat.list_moments(ASSET)) == 2 and cat.get_moment(moment_id_for(segs[0]["segment_id"]))["segment_id"] == segs[0]["segment_id"]
        # re-segmenting with a different window replaces baseline moments but keeps enriched ones
        enriched = dict(moments_from_segments(segs[:1], "fast:1")[0], source="vision", description="waves")
        cat.replace_moments(ASSET, "fast:1", [enriched, moments_from_segments(segs[1:], "fast:1")[0]])
        cat.replace_moments(ASSET, "fast:1", [])
        left = cat.list_moments(ASSET)
        assert len(left) == 1 and left[0]["moment_id"] == enriched["moment_id"]
        cat.replace_segments(ASSET, "fast:2", [])
        assert cat.list_segments(ASSET) == []
