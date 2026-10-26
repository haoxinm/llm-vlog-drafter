"""Captions in the handoff end to end: transcripts (fake local speech tool) -> draft with captions.enabled -> timeline.srt next to timeline.xml.
Needs ffmpeg and $FILMCRAFT_CLI (the handoff follows a real FilmCraft build)."""
from __future__ import annotations

import json
import os
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import hash_tree

from trip_vlog import cli
from trip_vlog.orchestrator import run_draft, run_transcribe
from trip_vlog.planning.models import EditPlan

CLI = os.environ.get("FILMCRAFT_CLI")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")]

TOOL = """
import pathlib, sys
inp, out_dir = sys.argv[1:3]
pathlib.Path(out_dir, "transcript.srt").write_text("1\\n00:00:00,500 --> 00:00:01,500\\nCaf\\u00e9 stop, hello\\n\\n2\\n00:00:02,000 --> 00:00:03,000\\nsecond line\\n", encoding="utf-8")
"""


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def test_captions_are_written_next_to_the_timeline_and_reported(media_dir, workspace, tmp_path, capsys):
    tool = tmp_path / "fake_asr.py"
    tool.write_text(TOOL, encoding="utf-8")
    speech = {"speech.backend": "command", "speech.command": {"executable": sys.executable, "args": [str(tool), "{input}", "{output_dir}"], "output_format": "srt"}}
    before = hash_tree(media_dir)
    assert run_transcribe(media_dir, workspace, None, speech, echo=lambda *_: None).exit_code == 0
    ov = {**speech, "project.target_duration_seconds": 12, "workflow.approval": "auto_approve", "filmcraft.executable": CLI}
    off = run_draft(media_dir, workspace, None, ov, echo=lambda *_: None)
    assert off.exit_code == 0
    hd = workspace / "resolve_handoff"
    assert not (hd / "timeline.srt").exists() and not (hd / "captions_report.json").exists() and "timeline.srt" not in (hd / "handoff_report.md").read_text(encoding="utf-8")

    on = run_draft(media_dir, workspace, None, {**ov, "captions.enabled": True}, echo=lambda *_: None)
    assert on.exit_code == 0 and on.stage_status["handoff"] == "succeeded"
    data = (hd / "timeline.srt").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and data.count(b"\n") == data.count(b"\r\n") > 0 and b"\xc3\xa9" in data       # UTF-8, CRLF, accents intact
    text = data.decode("utf-8")
    plan = EditPlan.model_validate(rj(workspace / "plans" / "edit_plan.json"))
    fps = Fraction(plan.sequence.fps)
    expect = []
    for c in (c for s in plan.scenes for c in s.clips if c.media_type == "video" and c.audio_policy == "natural_sound"):
        rec0 = float(Fraction(c.timeline_start_frame) / fps) - c.source_in_seconds
        end = c.source_in_seconds + c.duration_frames / float(fps)
        for a, b, words in ((0.5, 1.5, "Café stop, hello"), (2.0, 3.0, "second line")):
            if a >= c.source_in_seconds and b <= end:
                expect.append((round(rec0 + a, 3), words))
    got = []
    for block in text.split("\r\n\r\n"):
        if block.strip():
            lines = block.split("\r\n")
            h, m, rest = lines[1].split(" --> ")[0].split(":")
            sec, ms = rest.split(",")
            got.append((round(int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000, 3), " ".join(lines[2:])))
    assert expect and got == sorted(expect), (got, expect)
    rep = rj(hd / "captions_report.json")
    assert rep["cues"] == len(expect) and rep["file"] == "timeline.srt" and rep["encoding"].startswith("UTF-8 without BOM")
    handoff = (hd / "handoff_report.md").read_text(encoding="utf-8")
    assert "timeline.srt" in handoff and "MANUAL step" in handoff and "has not been verified in Resolve" in handoff
    assert (hd / "timeline.xml").is_file() and rj(workspace / "manifest.json")["captions"]["cues"] == len(expect)
    assert hash_tree(media_dir) == before

    # a re-run changes nothing; a hand-polished file is backed up, not lost; turning captions off sets the file aside
    again = run_draft(media_dir, workspace, None, {**ov, "captions.enabled": True}, echo=lambda *_: None)
    assert again.stage_status["handoff"] == "skipped" or rj(workspace / "manifest.json")["stages"]["handoff"]["decision"] == "cache_hit"
    (hd / "timeline.srt").write_bytes(b"1\r\n00:00:00,000 --> 00:00:01,000\r\nmy own edit\r\n\r\n")
    run_draft(media_dir, workspace, None, {**ov, "captions.enabled": True, "captions.max_lines": 1}, echo=lambda *_: None)
    assert any(b"my own edit" in p.read_bytes() for p in hd.glob("timeline.*.bak.srt")) and b"my own edit" not in (hd / "timeline.srt").read_bytes()
    run_draft(media_dir, workspace, None, {**ov, "captions.enabled": False}, echo=lambda *_: None)      # overrides are saved in the workspace's project.yaml
    assert not (hd / "timeline.srt").exists() and (hd / "timeline.stale.srt").is_file()
    assert "captions" not in rj(workspace / "manifest.json") or rj(workspace / "manifest.json")["captions"] is None
    assert cli.main(["status", "--output", str(workspace)]) == 0
