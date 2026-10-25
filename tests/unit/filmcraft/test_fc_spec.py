from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "fixtures" / "filmcraft"))
import fcsupport  # noqa: E402
from fcsupport import fc  # noqa: E402,F401
from trip_vlog.filmcraft.errors import SpecValidationError
from trip_vlog.filmcraft.timeline_spec import (
    MarkerSpec, Placement, TimelineSpec, TitleSpec, TransitionIn, check_media_bounds, downgrade_dissolves, ensure_valid,
    validate_spec)


@pytest.fixture
def spec(tmp_path) -> TimelineSpec:
    return fcsupport.sample_spec(fcsupport.make_fake_media(tmp_path / "m"))


def with_placement(spec: TimelineSpec, cid: str, **changes) -> TimelineSpec:
    return replace(spec, placements=tuple(replace(p, **changes) if p.clip_instance_id == cid else p for p in spec.placements))


def issues(spec) -> str:
    return "\n".join(validate_spec(spec))


def test_sample_spec_is_valid(spec):
    assert validate_spec(spec) == []
    assert spec.video_track_count == 2 and spec.audio_track_count == 2
    assert spec.end_frame == 195
    assert len(spec.asset_paths) == 4


def test_dict_roundtrip_and_hash(spec):
    again = TimelineSpec.from_dict(spec.to_dict())
    assert again == spec and again.spec_hash() == spec.spec_hash()
    assert replace(spec, markers=(MarkerSpec(61, "Scene A", "first scene"),)).spec_hash() != spec.spec_hash()
    assert spec.to_dict()["fps"] == "30/1"
    assert TimelineSpec.from_dict({**spec.to_dict(), "fps": "30000/1001"}).fps == Fraction(30000, 1001)


def test_nonpositive_fps_rejected_at_construction(spec):
    with pytest.raises(ValueError):
        replace(spec, fps=0)


def test_fps_normalised(spec):
    assert replace(spec, fps=29.97).fps == Fraction(30000, 1001)


@pytest.mark.parametrize("mutate,needle", [
    (lambda s: with_placement(s, "c2", clip_instance_id="c1"), "duplicate clip_instance_id"),
    (lambda s: with_placement(s, "c1", track="A1"), "video must be placed on a V track"),
    (lambda s: with_placement(s, "c4", track="V1"), "audio must be placed on an A track"),
    (lambda s: with_placement(s, "c1", track="V9"), "not in the spec's tracks"),
    (lambda s: with_placement(s, "c1", track="X1"), "track must look like"),
    (lambda s: with_placement(s, "c1", media_kind="gif"), "media_kind"),
    (lambda s: with_placement(s, "c1", duration_frames=0), "duration_frames must be an integer > 0"),
    (lambda s: with_placement(s, "c1", timeline_start_frame=-1), "timeline_start_frame"),
    (lambda s: with_placement(s, "c1", source_out_seconds=0.4), "greater than source_in_seconds"),
    (lambda s: with_placement(s, "c1", source_out_seconds=9.0), "source range is"),
    (lambda s: with_placement(s, "c1", source_out_seconds=None), "source_out_seconds is required"),
    (lambda s: with_placement(s, "c2", source_out_seconds=3.0), "stills take no source range"),
    (lambda s: with_placement(s, "c3", audio_gain_db=-3.0), "no audio"),
    (lambda s: with_placement(s, "c4", audio_gain_db=-200.0), "audio_gain_db"),
    (lambda s: with_placement(s, "c4", audio_gain_db=float("nan")), "audio_gain_db"),
    (lambda s: with_placement(s, "c2", transition_in=TransitionIn("wipe", 5)), "must be 'cut' or 'dissolve'"),
    (lambda s: with_placement(s, "c2", transition_in=TransitionIn("dissolve", 1)), "frames >= 2"),
    (lambda s: with_placement(s, "c1", transition_in=TransitionIn("cut", 3)), "a cut has frames=0"),
    (lambda s: with_placement(s, "c2", transition_in=TransitionIn("dissolve", 400)), "longer than the adjacent clips"),
    (lambda s: with_placement(s, "c3", transition_in=TransitionIn("dissolve", 10)), "ending exactly where it starts"),
    (lambda s: with_placement(s, "c2", timeline_start_frame=110, transition_in=TransitionIn("dissolve", 10)), "ending exactly"),
    (lambda s: with_placement(s, "c1", linked_audio_track="V1"), "linked_audio_track must be an A track"),
    (lambda s: with_placement(s, "c1", linked_audio_track="A7"), "not in the spec's tracks"),
    (lambda s: replace(s, tracks=("V2", "A1", "A2")), "tracks must include V1"),
    (lambda s: replace(s, tracks=("V1", "V3", "A1", "A2")), "contiguous"),
    (lambda s: replace(s, tracks=("V1", "V1", "V2", "A1", "A2")), "duplicate track"),
    (lambda s: replace(s, width=0), "width"),
    (lambda s: replace(s, titles=(TitleSpec("  ", 0, 10),)), "text must be a non-empty string"),
    (lambda s: replace(s, titles=(TitleSpec("x\x00y", 0, 10),)), "control characters"),
    (lambda s: replace(s, titles=(TitleSpec("t", 0, 10, "A1"),)), "titles go on a V track"),
    (lambda s: replace(s, markers=(MarkerSpec(-1, "m"),)), "markers[0]: frame"),
    (lambda s: replace(s, markers=(MarkerSpec(5, ""),)), "name must be"),
])
def test_invalid_specs_are_reported(spec, mutate, needle):
    assert needle in issues(mutate(spec))


def test_overlap_on_same_track(spec):
    bad = with_placement(spec, "c2", timeline_start_frame=100)
    assert "overlap on V1" in issues(bad)


def test_linked_audio_counts_as_occupying_its_audio_track(spec):
    # music wav (A2) overlaps the linked audio of a V2 video that defaults to A2
    clash = with_placement(spec, "c3", include_linked_audio=True, source_out_seconds=3.0)
    assert "overlap on A2" in issues(clash)
    # moving the linked audio to A1 resolves the clash? A1 is free apart from c1 (0-105): c3 starts at 60 -> still overlaps
    assert "overlap on A1" in issues(with_placement(clash, "c3", linked_audio_track="A1"))
    ok = with_placement(clash, "c3", linked_audio_track="A1", timeline_start_frame=105)
    assert "overlap on A1" not in issues(ok)


def test_title_overlapping_clip_on_same_track(spec):
    bad = replace(spec, titles=(TitleSpec("Over", 70, 20, "V2"),))
    assert "overlap on V2" in issues(bad)


def test_ensure_valid_lists_all_problems(spec):
    bad = with_placement(with_placement(spec, "c1", track="A1"), "c4", audio_gain_db=500.0)
    with pytest.raises(SpecValidationError) as ei:
        ensure_valid(bad)
    assert len(ei.value.issues) >= 2 and "invalid timeline spec" in str(ei.value)


def test_source_range_may_differ_from_duration_by_one_frame(spec):
    c1 = next(p for p in spec.placements if p.clip_instance_id == "c1")
    nudged = with_placement(spec, "c1", source_out_seconds=c1.source_out_seconds + 1 / 30)
    assert validate_spec(nudged) == []
    assert "source range is" in issues(with_placement(spec, "c1", source_out_seconds=c1.source_out_seconds + 3 / 30))


def test_media_bounds_past_end_and_unknown_asset(spec):
    durations = {p: 5.0 for p in spec.asset_paths}
    errors, handles = check_media_bounds(spec, durations)
    assert any("c4" in e and "past the end" in e for e in errors)  # wav is 6 s in reality but 5 s here
    errors, _ = check_media_bounds(spec, {})
    assert len(errors) == 4 and all("no duration known" in e for e in errors)


def test_dissolve_handle_checks_and_downgrade(spec):
    durations = {p.source_path: 5.0 for p in spec.placements}
    durations[next(p for p in spec.placements if p.clip_instance_id == "c4").source_path] = 6.0
    errors, handles = check_media_bounds(spec, durations)
    assert errors == [] and handles == []  # outgoing c1 ends at 4.0 of 5.0: 1.0 s handle >= 0.25 s
    # c1 used up to its very last frame -> no handle after the cut
    tight = with_placement(spec, "c1", source_in_seconds=0.0, source_out_seconds=3.5, duration_frames=105)
    tight = with_placement(tight, "c1", source_in_seconds=1.5, source_out_seconds=5.0)
    errors, handles = check_media_bounds(tight, durations)
    assert [h.clip_instance_id for h in handles] == ["c2"] and "after its out point" in handles[0].message
    down = downgrade_dissolves(tight, ["c2"])
    assert next(p for p in down.placements if p.clip_instance_id == "c2").transition_in == TransitionIn("cut", 0)
    assert check_media_bounds(down, durations)[1] == []


def test_incoming_video_without_pre_roll_is_a_handle_issue(spec):
    # a video clip dissolving in from source_in=0 has no media before its in point
    base = replace(spec, placements=(
        Placement("a", str(Path("/m/a.mp4")), "video", "V1", 0, 60, 1.0, 3.0, False),
        Placement("b", str(Path("/m/b.mp4")), "video", "V1", 60, 60, 0.0, 2.0, False, None, TransitionIn("dissolve", 10)),
    ), titles=(), markers=(), tracks=("V1", "A1"))
    assert validate_spec(base) == []
    _, handles = check_media_bounds(base, {"/m/a.mp4": 10.0, "/m/b.mp4": 10.0}) if Path("/m").anchor == "/" else (None, [])
    if handles is not None and Path("/m").anchor == "/":
        assert [h.clip_instance_id for h in handles] == ["b"] and "before its in point" in handles[0].message
