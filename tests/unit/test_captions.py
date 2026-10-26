"""Captions from transcripts (DESIGN §13.4): verbatim words, time mapped through the plan, skip rules, SRT bytes, config."""
from __future__ import annotations

import pytest

from trip_vlog.config import parse_config
from trip_vlog.handoff.captions import (CaptionSettings, Cue, build_captions, render_srt, srt_timestamp, wrap_words, write_srt)
from trip_vlog.planning.models import EditPlan
from trip_vlog.qc.checks import TranscriptSpan

H = "a" * 64
A1, A2 = "asset_" + "1" * 16, "asset_" + "2" * 16


def clip(n: int, asset: str, start_frame: int, frames: int, s_in: float, *, media="video", audio="natural_sound", role="primary_video", out=None):
    fps = 30
    s_out = s_in + frames / fps if out is None else out
    still = media == "image"
    return {"clip_instance_id": f"clip_{n:03d}", "asset_id": asset, "moment_id": f"moment_{n:016d}", "media_type": media, "anchor": "center", "track_role": role,
            "audio_policy": audio, "requested_duration_seconds": frames / fps, "source_in_seconds": None if still else s_in, "source_out_seconds": None if still else s_out,
            "source_in_frame": None if still else round(s_in * fps), "source_out_frame": None if still else round(s_out * fps), "timeline_start_frame": start_frame,
            "duration_frames": frames, "transition_in": {"type": "cut", "duration_seconds": 0, "duration_frames": 0, "alignment": "center"},
            "resolver_notes": [], "rationale": "test", "intentional_repeat_reason": None}


def plan(*clips) -> EditPlan:
    total = max(c["timeline_start_frame"] + c["duration_frames"] for c in clips)
    return EditPlan.model_validate({
        "schema_version": "1.1", "plan_hash": H, "selection_plan_hash": H,
        "project": {"title": "T", "target_duration_seconds": total / 30, "aspect_ratio": "16:9", "frame_rate": "30"},
        "sequence": {"width": 1920, "height": 1080, "fps": "30", "aspect_ratio": "16:9", "still_duration_seconds": 3.0},
        "audio": {"music_policy": "none", "preserve_natural_audio": True, "duck_music_under_speech": False},
        "scenes": [{"scene_id": "scene_1", "title": "S", "purpose": "p", "target_duration_seconds": total / 30, "start_frame": 0, "duration_frames": total, "clips": list(clips)}],
        "titles": [], "markers": [], "uncertainties": [], "total_duration_frames": total, "total_duration_seconds": total / 30})


def span(a, b, text, coarse=False, tid=None):
    return TranscriptSpan(a, b, text, coarse, tid or f"t_{a}")


S = CaptionSettings(max_chars_per_line=42, max_lines=2, min_duration_seconds=0.0)


def test_time_is_mapped_from_the_source_to_the_timeline_and_the_words_are_verbatim():
    p = plan(clip(1, A1, 0, 90, 0.0), clip(2, A1, 90, 90, 10.0))      # the second clip plays source 10.0-13.0 from timeline second 3
    r = build_captions(p, {A1: [span(0.5, 1.5, "Hello, wörld — it's ünï-code!"), span(11.0, 12.0, "second  clip\ttext")]}, S)
    assert [(round(c.start, 3), round(c.end, 3), c.text) for c in r.cues] == [(0.5, 1.5, "Hello, wörld — it's ünï-code!"), (4.0, 5.0, "second clip text")]
    assert r.clips_captioned == 2 and r.segments_used == 2 and r.segments_cut_by_clip == 0 and r.clips_skipped == []


def test_a_segment_cut_by_the_clip_keeps_only_the_words_inside_and_never_adds_any():
    p = plan(clip(1, A1, 30, 60, 5.0))                                  # plays source 5.0-7.0 at timeline 1.0
    text = "one two three four five six seven eight"
    r = build_captions(p, {A1: [span(4.0, 8.0, text)]}, S)             # segment runs 4-8, the clip uses the middle half
    (c,) = r.cues
    assert c.text == "three four five six" and r.segments_cut_by_clip == 1
    assert c.start == pytest.approx(1.0) and c.end == pytest.approx(3.0)       # clipped to the clip's used range
    assert set(c.text.split()) <= set(text.split())


def test_clips_without_audible_speech_or_a_usable_transcript_are_skipped_with_a_reason():
    p = plan(clip(1, A1, 0, 30, 0.0, audio="muted"), clip(2, A1, 30, 30, 0.0, audio="ambient_low"), clip(3, A1, 60, 30, 0.0, media="image"),
             clip(4, A2, 90, 30, 0.0), clip(5, A1, 120, 30, 0.0, role="music"), clip(6, A1, 150, 60, 0.0, out=1.0),   # 2 s on the timeline from 1 s of source: slow motion
             clip(7, A1, 210, 30, 0.0))
    r = build_captions(p, {A1: [span(0.0, 0.9, "hi", coarse=False), span(0.0, 1.0, "whole file", coarse=True)]}, S)
    why = {s["clip_instance_id"]: s["reason"] for s in r.clips_skipped}
    assert "audio_policy muted" in why["clip_001"] and "audio_policy ambient_low" in why["clip_002"] and "clip_003" not in why     # stills are not even listed
    assert "no transcript" in why["clip_004"] and "carries no speech" in why["clip_005"] and "speed" in why["clip_006"]
    assert [c.clip_instance_id for c in r.cues] == ["clip_007"] and r.segments_skipped_coarse >= 1


def test_coarse_timestamps_never_become_captions():
    r = build_captions(plan(clip(1, A1, 0, 90, 0.0)), {A1: [span(0.0, 3.0, "a block for the whole file", coarse=True)]}, S)
    assert r.cues == [] and r.segments_skipped_coarse == 1 and "no usable transcript segment" in r.clips_skipped[0]["reason"]


def test_long_text_is_split_at_word_boundaries_by_line_and_line_count_limits():
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
    p = plan(clip(1, A1, 0, 300, 0.0))
    r = build_captions(p, {A1: [span(0.0, 10.0, " ".join(words))]}, CaptionSettings(max_chars_per_line=16, max_lines=2, min_duration_seconds=0.0))
    assert len(r.cues) > 1 and all(len(ln) <= 16 for c in r.cues for ln in c.lines) and all(len(c.lines) <= 2 for c in r.cues)
    assert " ".join(w for c in r.cues for w in " ".join(c.lines).split()) == " ".join(words)       # same words, same order, nothing added or lost
    assert r.cues[0].start == pytest.approx(0.0) and r.cues[-1].end == pytest.approx(10.0)
    assert all(a.end <= b.start + 1e-9 for a, b in zip(r.cues, r.cues[1:]))


def test_a_word_longer_than_the_line_is_kept_whole():
    assert wrap_words(["a", "supercalifragilistic", "b"], 8) == ["a", "supercalifragilistic", "b"]
    assert wrap_words("no spaces here at all".split(), 100) == ["no spaces here at all"]
    assert wrap_words(["日本語のテキストは分割しない"], 5) == ["日本語のテキストは分割しない"]


def test_short_cues_are_shown_longer_but_never_into_the_next_cue_or_past_the_clip():
    p = plan(clip(1, A1, 0, 60, 0.0))                                   # a 2 s clip
    r = build_captions(p, {A1: [span(0.2, 0.4, "tiny one"), span(0.6, 0.7, "tiny two"), span(1.9, 2.0, "tiny end")]},
                       CaptionSettings(42, 2, min_duration_seconds=1.0))
    c1, c2, c3 = r.cues
    assert c1.end == pytest.approx(0.6) and c2.end == pytest.approx(1.6) and c3.end == pytest.approx(2.0)
    assert [c.text for c in r.cues] == ["tiny one", "tiny two", "tiny end"]


def test_srt_bytes_are_utf8_without_bom_with_crlf_and_exact_timestamps():
    assert srt_timestamp(3723.4567) == "01:02:03,457" and srt_timestamp(0) == "00:00:00,000" and srt_timestamp(-1) == "00:00:00,000"
    data = render_srt([Cue(0.5, 1.5, ("Grüße", "日本語"), "c"), Cue(2, 3, ("two",), "c")])
    assert data == "1\r\n00:00:00,500 --> 00:00:01,500\r\nGrüße\r\n日本語\r\n\r\n2\r\n00:00:02,000 --> 00:00:03,000\r\ntwo\r\n\r\n".encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r\r" not in data and data.count(b"\n") == data.count(b"\r\n")
    assert render_srt([]) == b""


def test_write_srt_keeps_an_edited_file_and_sets_a_stale_one_aside(tmp_path):
    f = tmp_path / "timeline.srt"
    cues = [Cue(0, 1, ("one",), "c")]
    w1 = write_srt(f, cues, None)
    assert w1["backup"] is None and f.read_bytes() == render_srt(cues)
    w2 = write_srt(f, cues, w1["sha256"])                               # the file is the one we wrote: replaced in place, no backup
    assert w2["backup"] is None and list(tmp_path.glob("*.bak.srt")) == []
    f.write_bytes(b"1\r\n00:00:00,000 --> 00:00:01,000\r\nhand polished\r\n\r\n")
    w3 = write_srt(f, [Cue(0, 1, ("two",), "c")], w2["sha256"])
    assert w3["backup"] and (tmp_path / w3["backup"]).read_bytes().endswith(b"hand polished\r\n\r\n") and b"two" in f.read_bytes()
    w4 = write_srt(f, [], w3["sha256"])
    assert w4["sha256"] is None and not f.exists() and (tmp_path / "timeline.stale.srt").read_bytes().count(b"two") == 1


def test_captions_config_defaults_alias_and_conflict():
    c = parse_config({})
    assert c.captions.enabled is False and (c.captions.max_chars_per_line, c.captions.max_lines, c.captions.min_duration_seconds) == (42, 2, 1.0)
    assert parse_config({"project": {"include_subtitles": True}}).captions.enabled is True
    assert parse_config({"captions": {"enabled": True, "max_lines": 1}}).captions.max_lines == 1
    with pytest.raises(Exception, match="include_subtitles"):
        parse_config({"project": {"include_subtitles": True}, "captions": {"enabled": False}})
    with pytest.raises(Exception):
        parse_config({"captions": {"max_lines": 9}})
    with pytest.raises(Exception):
        parse_config({"captions": {"rewrite": True}})
