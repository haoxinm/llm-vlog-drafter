from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest
from analysis_helpers import entry

from trip_vlog.analysis import labels as lb
from trip_vlog.llm.schema import sanitize_schema_for_provider, validation_errors
from trip_vlog.media.segment import build_segments, moments_from_segments, SegmentSettings

SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
F30 = Fraction(30)


def seg(start=0, end=150, fps="30"):
    return {"segment_id": "seg_" + "a" * 16, "asset_id": "asset_x", "media_type": "video", "start_frame": start, "end_frame": end,
            "start_seconds": start / 30, "end_seconds": end / 30, "fps": fps, "features": {"has_audio": False, "silent": True, "scene_cut_score": 0.0}}


def test_schema_files_match_generated():
    for name, build in lb.SCHEMAS.items():
        assert json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8")) == build(), f"regenerate: python -m trip_vlog.analysis.labels schemas"


def _strict_ok(node, path="$"):
    """OpenAI strict mode: every object lists all its properties as required and forbids extras."""
    if isinstance(node, dict):
        if "properties" in node:
            assert node.get("additionalProperties") is False, path
            assert set(node["required"]) == set(node["properties"]), path
        for k, v in node.items():
            _strict_ok(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _strict_ok(v, f"{path}[{i}]")


@pytest.mark.parametrize("build", [lb.segment_labels_schema, lb.photo_labels_schema])
def test_provider_schema_is_strict_and_has_no_numeric_bounds(build):
    provider = sanitize_schema_for_provider(build())
    _strict_ok(provider)
    text = json.dumps(provider)
    assert '"minimum"' not in text and '"maximum"' not in text and "$schema" not in text
    assert "best_frames" in json.dumps(lb.segment_labels_schema()) and "best_frames" not in json.dumps(lb.photo_labels_schema())


def test_full_schema_still_validates_bounds_client_side():
    good = {"segments": [entry("seg_" + "a" * 16, 3)]}
    assert validation_errors(good, lb.segment_labels_schema()) == []
    for bad in ({"interest": 1.5}, {"confidence": -0.1}, {"editorial_tags": ["bogus"]}, {"quality": {"sharpness": "great", "stability": None, "exposure": None}},
                {"best_frames": {"first_index": -1, "last_index": 2}}):
        doc = {"segments": [entry("seg_" + "a" * 16, 3, **bad)]}
        assert validation_errors(doc, lb.segment_labels_schema()), bad
    # every property is required (nullable instead of optional)
    partial = entry("seg_" + "a" * 16, 3)
    del partial["event"]
    assert validation_errors({"segments": [partial]}, lb.segment_labels_schema())


def test_tags_are_a_closed_vocabulary():
    assert set(lb.EDITORIAL_TAGS) >= {"establishing", "people", "food", "nature", "city", "transport", "activity", "detail",
                                      "reaction", "transition", "scenic", "night", "other"}


@pytest.mark.parametrize("patch,msg", [
    ({"description": "   "}, "empty description"),
    ({"best_frames": {"first_index": 0, "last_index": 5}}, "outside the 4 frame"),
    ({"best_frames": {"first_index": 2, "last_index": 1}}, "before first_index"),
    ({"best_frames": {"first_index": -1, "last_index": 1}}, "outside"),
    ({"interest": 2}, "interest"),
    ({"confidence": float("nan")}, "confidence"),
])
def test_validate_label_rejects(patch, msg):
    item, err = lb.validate_label(entry("s", 4, **patch), n_frames=4)
    assert item is None and msg in err


def test_validate_label_normalises_and_photo_rules():
    item, err = lb.validate_label(entry("s", 4, visible_entities=["Sea", "sea", " boat "], editorial_tags=["scenic", "scenic"],
                                        best_frames={"first_index": 1, "last_index": 1}), n_frames=4)
    assert err is None and item["visible_entities"] == ["Sea", "boat"] and item["editorial_tags"] == ["scenic"]
    assert item["best_frames"] == {"first_index": 1, "last_index": 1}
    photo, err = lb.validate_label(entry("p", None), n_frames=None)
    assert err is None and photo["best_frames"] is None
    _, err = lb.validate_label(entry("p", 3, best_frames={"first_index": 0, "last_index": 1}), n_frames=None)
    assert "photo" in err


def test_best_range_index_to_seconds_conversion_and_clamping():
    s = seg(0, 150)
    frames = [15, 45, 75, 105, 135]  # 5 samples, one per second slice
    assert lb.best_range(s, frames, 0, 4) is None                      # everything -> whole segment
    assert lb.best_range(s, frames, 1, 3) == (30, 120)                 # territory of frame 1 starts halfway to frame 0
    assert lb.best_range(s, frames, 0, 1) == (0, 60)                   # first index starts at the segment start
    assert lb.best_range(s, frames, 3, 4) == (90, 150)                 # last index ends at the segment end
    assert lb.best_range(s, frames, 2, 2) == (60, 90)
    assert lb.best_range(s, frames, 3, 1) is None and lb.best_range(s, frames, 0, 9) is None  # invalid -> whole
    # a segment that does not start at frame 0: result stays inside [start, end)
    s2 = seg(300, 450)
    lo, hi = lb.best_range(s2, [315, 345, 375, 405, 435], 1, 2)
    assert 300 <= lo < hi <= 450 and (lo, hi) == (330, 390)
    # a single sample covering the segment is the whole segment; degenerate sampled frames are clamped to >= 1 frame
    assert lb.best_range(seg(10, 11), [10], 0, 0) is None
    lo, hi = lb.best_range(seg(0, 150), [0, 149], 1, 1)
    assert 0 <= lo < hi <= 150


def test_labeled_moment_keeps_ids_and_uses_frame_rounded_ntsc_seconds():
    segs = build_segments("asset_x", "video", 10.0, Fraction(30000, 1001), {"cuts": [], "silences": [], "windows": [], "has_audio": False},
                          SegmentSettings(window_seconds=20))
    s = segs[0]
    base = moments_from_segments([s], "fast:1")[0]
    frames = [{"index": i, "frame": f, "timestamp_seconds": round(f * 1001 / 30000, 6), "artifact": f"media_index/analysis/frames/a/{i}.jpg"}
              for i, f in enumerate([30, 90, 150, 210, 270])]
    label, _ = lb.validate_label(entry(s["segment_id"], 5, best_frames={"first_index": 1, "last_index": 3}), n_frames=5)
    m = lb.labeled_moment(base, s, label, frames=frames, content_version="fast:1", model="test-model", prompt={"id": "segment_labels", "version": 1})
    assert m["moment_id"] == base["moment_id"] and m["segment_id"] == s["segment_id"] and m["source"] == "llm" and m["labeled"] is True
    assert (m["segment_start_seconds"], m["segment_end_seconds"]) == (s["start_seconds"], s["end_seconds"])
    fps = Fraction(30000, 1001)
    for t in (m["source_in_seconds"], m["source_out_seconds"]):
        assert abs(Fraction(t).limit_denominator(10**7) * fps - round(Fraction(t) * fps)) < Fraction(1, 100)  # on the frame grid
    assert s["start_seconds"] < m["source_in_seconds"] < m["source_out_seconds"] < s["end_seconds"]
    assert m["best_frames"]["first_index"] == 1 and [e["frame_index"] for e in m["evidence"]] == [0, 1, 2, 3, 4]
    assert m["evidence"][0] == {"type": "frame", "timestamp_seconds": frames[0]["timestamp_seconds"], "artifact": frames[0]["artifact"], "frame_index": 0}
    assert "gps" not in json.dumps(m).lower()


def test_labeled_moment_without_best_frames_is_the_whole_segment():
    s = seg()
    base = moments_from_segments([s], "fast:1")[0]
    label, _ = lb.validate_label(entry(s["segment_id"], 3), n_frames=3)
    m = lb.labeled_moment(base, s, label, frames=[], content_version="fast:1", model="m", prompt={})
    assert (m["source_in_seconds"], m["source_out_seconds"]) == (s["start_seconds"], s["end_seconds"]) and m["best_frames"] is None


def test_unlabeled_moment_is_the_baseline_flagged():
    s = seg()
    base = moments_from_segments([s], "fast:1")[0]
    m = lb.unlabeled_moment(base, "boom")
    assert m["labeled"] is False and m["label_error"] == "boom" and m["source"] == "baseline" and m["moment_id"] == base["moment_id"]
