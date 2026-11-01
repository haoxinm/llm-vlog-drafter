"""`transcribe` end to end on the ffmpeg-generated fixtures with a fake local transcription tool (skipped without ffmpeg)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from analysis_helpers import LabelingTransport, gateway_for
from conftest import hash_tree

from trip_vlog import cli
from trip_vlog.analysis import stage as st
from trip_vlog.media.catalog import Catalog
from trip_vlog.orchestrator import run_analyze, run_transcribe

pytestmark = pytest.mark.integration

TOOL = """
import pathlib, sys
inp, out_dir, log = sys.argv[1:4]
with open(log, "a", encoding="utf-8") as f:
    f.write(pathlib.Path(inp).name + "\\n")
data = pathlib.Path(inp).read_bytes()
assert data[:4] == b"RIFF" and data[22:24] == b"\\x01\\x00" and int.from_bytes(data[24:28], "little") == 16000, "expected 16 kHz mono WAV"
pathlib.Path(out_dir, "transcript.srt").write_text("1\\n00:00:00,500 --> 00:00:01,500\\nhello from the clip\\n", encoding="utf-8")
"""


@pytest.fixture
def speech(tmp_path):
    tool = tmp_path / "fake_asr.py"
    tool.write_text(TOOL, encoding="utf-8")
    log = tmp_path / "calls.log"
    ov = {"speech.backend": "command",
          "speech.command": {"executable": sys.executable, "args": [str(tool), "{input}", "{output_dir}", str(log)], "output_format": "srt"}}
    return ov, log


def calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def transcribe(media, ws, ov, **kw):
    return run_transcribe(media, ws, None, ov, echo=lambda *_: None, **kw)


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def test_end_to_end_cache_and_untouched_originals(media_dir, workspace, speech):
    ov, log = speech
    before = hash_tree(media_dir)
    dry = transcribe(media_dir, workspace, ov, dry_run=True)
    assert dry.exit_code == 0 and dry.dry_run["to_transcribe"] == 4 and dry.dry_run["local"] is True and calls(log) == []
    assert "audio" not in dry.stage_status and not (workspace / "media_index" / "transcripts.jsonl").exists()

    res = transcribe(media_dir, workspace, ov)
    assert res.exit_code == 0 and res.stage_status["audio"] == "succeeded" and res.stage_status["transcribe"] == "succeeded"
    assert sorted(calls(log)) and len(calls(log)) == 4 and hash_tree(media_dir) == before
    man = rj(workspace / "manifest.json")
    s = man["stages"]["transcribe"]["summary"]
    assert man["pipeline"] == "transcribe" and s["transcribed"] == 4 and s["segments"] == 4 and s["local"] is True and s["skipped_assets"] == {}
    assert not (workspace / "media_index" / "audio").exists()                      # temporary audio was deleted
    lines = [json.loads(x) for x in (workspace / "media_index" / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 4 and all(x["text"] == "hello from the clip" and x["start_seconds"] == 0.5 for x in lines)
    with Catalog(workspace / "media_index" / "catalog.sqlite") as cat:
        ms = [json.loads(r["data_json"]) for r in cat.list_moments()]
        trs = {r["transcript_id"] for r in cat.list_transcripts()}
    media_moments = [m for m in ms if m["media_type"] in ("video", "audio")]
    assert len(media_moments) == 4 and all(m["transcript_refs"] and set(m["transcript_refs"]) <= trs for m in media_moments)
    assert all(m["speech_summary"] is None and m["audio"]["has_audio"] and "asset_loudness_lufs" in m["audio"] for m in media_moments)
    still = [m for m in ms if m["media_type"] == "image"]
    assert still and still[0].get("transcript_refs") == []

    # a re-run (the first one settles the derived-state hash) neither extracts audio nor starts the tool again
    transcribe(media_dir, workspace, ov)
    res3 = transcribe(media_dir, workspace, ov)
    assert len(calls(log)) == 4 and res3.stage_status["transcribe"] == "skipped"
    assert rj(workspace / "manifest.json")["stages"]["transcribe"]["decision"] == "cache_hit"
    # the target duration is not an input of transcription
    transcribe(media_dir, workspace, {**ov, "project.target_duration_seconds": 45})
    assert len(calls(log)) == 4
    # a different language changes the request, so it is done again
    transcribe(media_dir, workspace, {**ov, "speech.language": "de"})
    assert len(calls(log)) == 8


def test_backend_none_still_measures_audio_and_reports_a_skip(media_dir, workspace):
    res = transcribe(media_dir, workspace, {})
    assert res.exit_code == 0 and res.stage_status["audio"] == "succeeded" and res.stage_status["transcribe"] == "skipped"
    st_ = rj(workspace / "manifest.json")["stages"]["transcribe"]
    assert st_["status"] == "skipped" and "speech.backend is none" in st_["summary"]["reason"]
    assert not (workspace / "media_index" / "transcripts.jsonl").exists()
    with Catalog(workspace / "media_index" / "catalog.sqlite") as cat:
        ms = [json.loads(r["data_json"]) for r in cat.list_moments()]
    assert all("audio" in m for m in ms if m["media_type"] == "video")


def test_limit_covers_only_the_first_assets(media_dir, workspace, speech):
    ov, log = speech
    res = transcribe(media_dir, workspace, ov, limit=1)
    assert res.exit_code == 0 and len(calls(log)) == 1


def test_analyze_runs_transcribe_first_and_sends_transcripts(media_dir, workspace, speech):
    ov, log = speech
    tr = LabelingTransport()
    st.gateway_factory = lambda c: gateway_for(c.config, c.workspace.root, tr)
    try:
        res = run_analyze(media_dir, workspace, None, {**ov, "llm.endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}},
                          echo=lambda *_: None)
    finally:
        st.gateway_factory = None
    assert res.exit_code == 0
    events = [json.loads(x) for x in (workspace / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    order = [e["stage"] for e in events if e.get("status") == "running" and e.get("stage") in ("segment", "audio", "transcribe", "analyze")]
    assert order == ["segment", "audio", "transcribe", "analyze"]
    body = "\n".join(t for c in tr.calls for t in c.texts)
    assert body.count('kind="transcript"') == 2 and "hello from the clip" in body       # the two videos; photos and audio-only files get none
    with Catalog(workspace / "media_index" / "catalog.sqlite") as cat:
        ms = [json.loads(r["data_json"]) for r in cat.list_moments()]
    video = [m for m in ms if m["media_type"] == "video"]
    assert all(m["source"] == "llm" and m["transcript_refs"] and m["audio"]["has_audio"] for m in video)


def test_cli_transcribe_dry_run_status_and_errors(media_dir, workspace, speech, capsys):
    ov, log = speech
    cfg = workspace.parent / "project.yaml"
    import yaml

    cfg.write_text(yaml.safe_dump({"speech": {"backend": "command", "command": ov["speech.command"]}}), encoding="utf-8")
    base = ["transcribe", "--media", str(media_dir), "--output", str(workspace), "--config", str(cfg)]
    assert cli.main([*base, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "transcribe dry run" in out and "backend command" in out and "(local)" in out and "to transcribe         : 4" in out and calls(log) == []
    assert cli.main([*base, "--limit", "0"]) == 2
    capsys.readouterr()
    assert cli.main(base) == 0
    out = capsys.readouterr().out
    assert "transcribed 4 new" in out and "audio features: 4 measured" in out and "transcripts:" in out
    assert cli.main(["status", "--output", str(workspace)]) == 0
    status = capsys.readouterr().out
    assert "transcribe" in status and "audio" in status and "unfinished" not in status
    assert cli.main(["resume", "--output", str(workspace), "--config", str(cfg)]) == 0


def test_cli_status_does_not_call_a_skipped_transcribe_unfinished(media_dir, workspace, capsys):
    assert cli.main(["transcribe", "--media", str(media_dir), "--output", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert "transcription skipped: speech.backend is none" in out
    assert cli.main(["status", "--output", str(workspace)]) == 0
    assert "unfinished" not in capsys.readouterr().out


def test_llm_check_speech_with_the_command_backend(tmp_path, speech, capsys):
    ov, log = speech
    import yaml

    cfg = tmp_path / "p.yaml"
    cfg.write_text(yaml.safe_dump({"speech": {"backend": "command", "command": ov["speech.command"]}}), encoding="utf-8")
    assert cli.main(["llm-check", "--config", str(cfg), "--speech", "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert "speech" in out and "command" in out and "local" in out and "all checks passed" in out and calls(log) == ["tone.wav"]
    assert cli.main(["llm-check", "--config", str(cfg), "--no-llm"]) == 2        # --no-llm alone is a usage error
