"""Phase 7b: where the music is ducked, the keyframe envelope, the mapping of fit/ducking into the spec, and the QC checks that measure them."""
from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from planning_helpers import aid, make_catalog, mid, sel_clip, sel_plan

from trip_vlog.filmcraft.plan_mapping import MappingSettings, edit_plan_to_spec
from trip_vlog.filmcraft.timeline_spec import GainKey, validate_spec
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.qc.checks import PASS, SKIPPED, WARN, TranscriptSpan, check_music_vs_speech, clip_audio_features
from trip_vlog.qc.ducking import (DuckSettings, DuckWindow, build_duck_plan, check_music_ducking, envelope, merge_windows)
from trip_vlog.qc.framing import bar_regions, check_framing

CAT = make_catalog()
ROOT = Path("/media/trip")
ON = DuckSettings(True, -12.0, 0.3, 1.0, "speech", 0.5)


def make_plan(*clips, duration=40):
    return resolve_selection_plan(sel_plan(list(clips)), CAT, PlanSettings(target_duration_seconds=duration))


def speech(asset_path, a, b, coarse=False):
    return {aid(asset_path): [TranscriptSpan(a, b, "we made it to the top", coarse)]}


def with_music(video_dur=6.0):
    return make_plan(sel_clip("v1", mid(CAT, "day1/a.mp4", 0), video_dur, "start"), sel_clip("v2", mid(CAT, "day1/a.mp4", 1), video_dur, "start"),
                     sel_clip("m1", mid(CAT, "music/song.wav", 0), video_dur * 2, role="music"))


def test_speech_windows_follow_the_transcript_and_map_to_record_time():
    plan = with_music()
    tr = speech("day1/a.mp4", 2.0, 4.0)          # v1 plays source 0..6 s; v2 plays source 6..12 s (no speech)
    d = build_duck_plan(plan, {}, tr, ON)
    assert [(w.start_frame, w.end_frame, w.reason) for w in d.windows] == [(56, 124, "speech")]       # 2.0 s .. 4.0 s +- 4 frames of padding at 30 fps
    keys = d.keys["m1"]
    assert [(k.frame, k.db) for k in keys] == [(47, 0.0), (56, -12.0), (124, -12.0), (133, 0.0)]
    assert all(plan_clip.timeline_start_frame <= k.frame < plan_clip.timeline_start_frame + plan_clip.duration_frames
               for plan_clip in [c for s in plan.scenes for c in s.clips if c.clip_instance_id == "m1"] for k in keys)


def test_modes_coarse_transcripts_and_no_speech():
    plan = with_music()
    # coarse transcript: timing unknown, so the whole clip counts, never a guessed position
    d = build_duck_plan(plan, {}, speech("day1/a.mp4", 0, 0, coarse=True), ON)
    assert {w.reason for w in d.windows} == {"speech_untimed"} and d.windows[0].start_frame == 0
    # no speech anywhere: no keys, and the reason is recorded
    d = build_duck_plan(plan, {}, {}, ON)
    assert d.windows == [] and d.keys == {} and "no speech" in d.notes[0]
    # active natural sound only counts in natural_sound mode
    feats = {c.clip_instance_id: {"mean_db": -20.0, "active_ratio": 0.9, "silent": False} for s in plan.scenes for c in s.clips}
    assert build_duck_plan(plan, feats, {}, ON).keys == {}
    d = build_duck_plan(plan, feats, {}, replace(ON, mode="natural_sound"))
    assert d.windows and d.windows[0].reason == "active_sound" and "m1" in d.keys


def test_off_no_music_and_plan_that_does_not_want_ducking():
    plan = with_music()
    assert build_duck_plan(plan, {}, speech("day1/a.mp4", 2, 4), replace(ON, enabled=False)).keys == {}
    no_music = make_plan(sel_clip("v1", mid(CAT, "day1/a.mp4", 0), 6.0, "start"))
    assert "no music" in build_duck_plan(no_music, {}, speech("day1/a.mp4", 2, 4), ON).notes[0]
    off = plan.model_copy(update={"audio": plan.audio.model_copy(update={"duck_music_under_speech": False})})
    assert "duck_music_under_speech" in build_duck_plan(off, {}, speech("day1/a.mp4", 2, 4), ON).notes[0]


def test_windows_close_together_are_joined_and_the_envelope_never_goes_backwards():
    ws = [DuckWindow(100, 130, "speech", ("a",)), DuckWindow(150, 200, "speech", ("b",)), DuckWindow(400, 430, "speech", ("c",))]
    merged = merge_windows(ws, 30)
    assert [(w.start_frame, w.end_frame) for w in merged] == [(100, 200), (400, 430)]
    keys = envelope(merged, 0, 600, -12.0, 9)
    frames = [k.frame for k in keys]
    assert frames == sorted(set(frames)) and [k.db for k in keys] == [0.0, -12.0, -12.0, 0.0, 0.0, -12.0, -12.0, 0.0]
    # clamped to the clip: a window at the very start has no ramp-in key; one at the very end no ramp-out key
    keys = envelope([DuckWindow(0, 20, "speech", ("a",)), DuckWindow(580, 700, "speech", ("b",))], 0, 600, -12.0, 9)
    assert keys[0].frame == 0 and keys[0].db == -12.0 and keys[-1].frame == 599 and keys[-1].db == -12.0
    assert envelope([DuckWindow(700, 800, "speech", ("a",))], 0, 600, -12.0, 9) == ()


def test_mapping_puts_keys_and_fit_sizes_into_the_spec():
    plan = make_plan(sel_clip("v1", mid(CAT, "day1/a.mp4", 0), 6.0, "start"), sel_clip("p1", mid(CAT, "day2/p.mp4", 0), 4.0, "start"),
                     sel_clip("s1", mid(CAT, "day1/c.jpg"), 2.0), sel_clip("m1", mid(CAT, "music/song.wav", 0), 12.0, role="music"))
    d = build_duck_plan(plan, {}, speech("day1/a.mp4", 2, 4), ON)
    spec = edit_plan_to_spec(plan, CAT, ROOT, MappingSettings(fit_mixed_aspect=True, duck_keys=d.keys)).spec
    by = {p.clip_instance_id: p for p in spec.placements}
    assert by["p1"].fit_source_size == (1080, 1920) and by["v1"].fit_source_size is None and by["s1"].fit_source_size is None
    assert by["m1"].gain_keys == d.keys["m1"] and validate_spec(spec) == []
    off = edit_plan_to_spec(plan, CAT, ROOT).spec
    assert all(p.fit_source_size is None and not p.gain_keys for p in off.placements)
    assert "gain_keys" not in {k for p in off.placements for k in p.to_dict()}


def test_music_vs_speech_counts_ducking_only_when_it_is_generated():
    plan = with_music()
    tr = speech("day1/a.mp4", 2.0, 4.0)
    feats = clip_audio_features(plan, CAT)
    assert check_music_vs_speech(plan, feats, tr).status == WARN
    d = build_duck_plan(plan, feats, tr, ON)
    # the overlap with v1 is the whole clip (0..180); the window only covers part of it, so it is still not lowered
    assert check_music_vs_speech(plan, feats, tr, duck_windows=[(w.start_frame, w.end_frame) for w in d.windows], duck_db=-12.0).status == WARN
    whole = [(0, 400)]
    assert check_music_vs_speech(plan, feats, tr, duck_windows=whole, duck_db=-12.0).status == PASS


# ---- measuring the rendered music bed (needs ffmpeg) ---------------------------------------------------------------

def tone(ffmpeg, path: Path, seconds: float, gain_segments=()):
    """A 220 Hz tone; gain_segments: ((start, end, dB), ...) applied with volume filters."""
    af = ",".join(f"volume=enable='between(t,{a},{b})':volume={db}dB" for a, b, db in gain_segments) or "anull"
    subprocess.run([str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}", "-af", af, str(path)], check=True)


def test_ducking_check_compares_the_rendered_bed_with_the_source(tmp_path, ffmpeg_tools):
    ffmpeg = ffmpeg_tools[0]
    plan = with_music()
    d = build_duck_plan(plan, {}, speech("day1/a.mp4", 2.0, 4.0), ON)
    src = tmp_path / "song.wav"
    tone(ffmpeg, src, 12)
    good = tmp_path / "good.wav"
    tone(ffmpeg, good, 12, [(1.87, 4.13, -12)])      # the window is frames 56..124 = 1.87..4.13 s
    r = check_music_ducking(plan, d, good, {"m1": str(src)}, ffmpeg)
    assert r.status == PASS, r.detail
    flat = tmp_path / "flat.wav"
    tone(ffmpeg, flat, 12)                              # a bed that was not ducked
    r = check_music_ducking(plan, d, flat, {"m1": str(src)}, ffmpeg)
    assert r.status == WARN and r.data["findings"][0]["kind"] == "ducked" and "differ" in r.detail


def test_ducking_check_skips_and_warns_instead_of_failing(tmp_path, ffmpeg_tools):
    plan = with_music()
    d = build_duck_plan(plan, {}, speech("day1/a.mp4", 2.0, 4.0), ON)
    assert check_music_ducking(plan, build_duck_plan(plan, {}, {}, replace(ON, enabled=False)), None, {}, None).status == SKIPPED
    assert check_music_ducking(plan, build_duck_plan(plan, {}, {}, ON), None, {}, None).status == SKIPPED
    r = check_music_ducking(plan, d, None, {}, ffmpeg_tools[0], bed_error="mute failed")
    assert r.status == WARN and "mute failed" in r.detail


# ---- framing ---------------------------------------------------------------------------------------------------

def test_bar_regions():
    assert bar_regions(1920, 1080, 1080, 1920, 56.25) == [(0, 0, 652, 1080), (1268, 0, 652, 1080)]
    assert bar_regions(1920, 1080, 3840, 2160, 50.0) == []                    # same aspect: nothing to measure
    top = bar_regions(1080, 1920, 1920, 1080, 56.25)
    assert [r[3] for r in top] == [top[0][3], top[0][3]] and top[0][2] == 1080


def test_framing_check_finds_black_bars_in_a_real_frame(tmp_path, ffmpeg_tools):
    from trip_vlog.filmcraft.timeline_spec import Placement, TimelineSpec
    from fractions import Fraction

    ffmpeg = ffmpeg_tools[0]
    spec = TimelineSpec("S", 1920, 1080, Fraction(30), ("V1", "V2", "A1", "A2"), placements=(
        Placement("p", "/m/p.mp4", "video", "V1", 0, 60, 0.0, 2.0, False, fit_source_size=(1080, 1920)),))
    ok, bad = tmp_path / "ok.mp4", tmp_path / "bad.mp4"
    # white 1080x607-wide picture centred on black = a correct pillarbox; a full-frame white picture = a clip that was cropped instead of fitted
    subprocess.run([str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=608x1080:r=30:d=2", "-vf", "pad=1920:1080:656:0:black",
                    "-pix_fmt", "yuv420p", str(ok)], check=True)
    subprocess.run([str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=1920x1080:r=30:d=2", "-pix_fmt", "yuv420p", str(bad)], check=True)
    r = check_framing(spec, None, ok, ffmpeg)
    assert r.status == PASS and r.data["preview_frames"] == 1, r.detail
    r = check_framing(spec, None, bad, ffmpeg)
    assert r.status == WARN and r.data["findings"][0]["where"] == "preview"
    plain = replace(spec, placements=(replace(spec.placements[0], fit_source_size=None),))
    assert check_framing(plain, None, ok, ffmpeg).status == SKIPPED
