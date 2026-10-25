from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "fixtures" / "filmcraft"))
import fcsupport  # noqa: E402
from fcsupport import fc  # noqa: E402,F401

from dataclasses import replace

import pytest

from trip_vlog.filmcraft.inspect import verify_against_spec
from trip_vlog.filmcraft.timeline_spec import TimelineSpec


@pytest.fixture
def built(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    res = adapter.build_project(spec)
    return spec, adapter.inspect_sequence(), adapter.inspect_project(), res.media_map


def mutate_clip(seq, clip_id, **ch):
    tracks = tuple(replace(t, clips=tuple(replace(c, **ch) if c.clip_id == clip_id else c for c in t.clips)) for t in seq.tracks)
    return replace(seq, tracks=tracks)


def kinds(rep):
    return {m.kind for m in rep.mismatches}


def run(spec, seq, proj, mm):
    return verify_against_spec(seq, spec, project=proj, media_map=mm)


def test_matching_timeline_passes_and_maps_clips(built):
    spec, seq, proj, mm = built
    rep = run(spec, seq, proj, mm)
    assert rep.ok, rep.summary()
    assert set(rep.clip_map.refs) == {"c1", "c2", "c3", "c4"} and rep.clip_map["c1"].audio_clip and rep.clip_map.transitions
    assert verify_against_spec(seq, spec).ok  # also works without project / media map (name matching)


def test_one_frame_tolerance_but_not_two(built):
    spec, seq, proj, mm = built
    c3 = run(spec, seq, proj, mm).clip_map["c3"].clip
    assert run(spec, mutate_clip(seq, c3, start_frame=seq.find_clip(c3).start_frame + 1), proj, mm).ok
    assert "start" in kinds(run(spec, mutate_clip(seq, c3, start_frame=seq.find_clip(c3).start_frame + 2), proj, mm))
    assert "duration" in kinds(run(spec, mutate_clip(seq, c3, duration_frames=seq.find_clip(c3).duration_frames + 2), proj, mm))


def test_source_in_out_and_gain_mismatches(built):
    spec, seq, proj, mm = built
    ref = run(spec, seq, proj, mm).clip_map
    c1 = seq.find_clip(ref["c1"].clip)
    assert "source_in" in kinds(run(spec, mutate_clip(seq, c1.clip_id, source_in_ticks=c1.source_in_ticks + 254016000000), proj, mm))
    assert "source_out" in kinds(run(spec, mutate_clip(seq, c1.clip_id, source_out_ticks=c1.source_out_ticks + 254016000000), proj, mm))
    assert "gain" in kinds(run(spec, mutate_clip(seq, ref["c4"].clip, gain_db=0.0), proj, mm))


def test_wrong_track_missing_and_duplicate_clips(built):
    spec, seq, proj, mm = built
    ref = run(spec, seq, proj, mm).clip_map
    moved = spec.placements[0]
    other = replace(spec, placements=(replace(moved, track="V2"),) + spec.placements[1:])
    assert {"missing_clip"} & kinds(run(other, seq, proj, mm))
    # a duplicated clip (what a re-applied plan would leave behind) is flagged
    v1 = seq.track("V1")
    dup = replace(v1.clips[0], clip_id=999)
    seq2 = replace(seq, tracks=(replace(v1, clips=v1.clips + (dup,)),) + seq.tracks[1:])
    rep = run(spec, seq2, proj, mm)
    assert "unexpected_clip" in kinds(rep) and not rep.ok
    # remove a clip
    seq3 = replace(seq, tracks=tuple(replace(t, clips=tuple(c for c in t.clips if c.clip_id != ref["c2"].clip)) for t in seq.tracks))
    assert "missing_clip" in kinds(run(spec, seq3, proj, mm))


def test_order_swap_detected(built):
    spec, seq, proj, mm = built
    ref = run(spec, seq, proj, mm).clip_map
    a, b = seq.find_clip(ref["c1"].clip), seq.find_clip(ref["c2"].clip)
    swapped = mutate_clip(mutate_clip(seq, a.clip_id, start_frame=b.start_frame, start_ticks=b.start_ticks), b.clip_id,
                          start_frame=a.start_frame, start_ticks=a.start_ticks)
    assert kinds(run(spec, swapped, proj, mm)) & {"order", "start"}


def test_media_offline_wrong_path_unknown_item(built):
    spec, seq, proj, mm = built
    m0 = proj.media[0]
    off = replace(proj, media=(replace(m0, status="offline"),) + proj.media[1:])
    assert "media_offline" in kinds(run(spec, seq, off, mm))
    wrong = replace(proj, media=(replace(m0, path=str(_P(m0.path).with_name("other.mp4"))),) + proj.media[1:])
    assert "media_path" in kinds(run(spec, seq, wrong, mm))
    gone = replace(proj, media=proj.media[1:])
    assert "media" in kinds(run(spec, seq, gone, mm))


def test_sequence_markers_transitions_titles(built):
    spec, seq, proj, mm = built
    assert "sequence" in kinds(run(replace(spec, width=1280), seq, proj, mm))
    assert "unexpected_marker" in kinds(run(replace(spec, markers=()), seq, proj, mm))
    assert "marker" in kinds(run(spec, replace(seq, markers=()), proj, mm))
    no_tr = replace(seq, tracks=tuple(replace(t, transitions=()) for t in seq.tracks))
    assert "transition" in kinds(run(spec, no_tr, proj, mm))
    assert "unexpected_transition" in kinds(run(replace(spec, placements=tuple(
        replace(p, transition_in=type(p.transition_in)()) for p in spec.placements)), seq, proj, mm))
    assert "missing_title" in kinds(run(replace(spec, titles=(replace(spec.titles[0], text="Other"),)), seq, proj, mm))
    assert "sequence" in kinds(run(replace(spec, fps=25), seq, proj, mm))
