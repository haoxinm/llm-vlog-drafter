from __future__ import annotations

import json
from pathlib import Path

import pytest
from speech_helpers import FakeBackend, add_av, install, make_speech_ctx, moments, speech_cfg, tr

from trip_vlog.config import parse_config
from trip_vlog.logging_utils import Status
from trip_vlog.speech import audio as au
from trip_vlog.speech import stage as sp


@pytest.fixture(autouse=True)
def _reset():
    yield
    sp.backend_factory = None


def run(ctx, backend):
    sp.backend_factory = lambda c: backend
    return sp.transcribe_stage(ctx)


# ---------------------------------------------------------------- chunking and temporary audio


def test_chunk_plan():
    assert sp.chunk_plan(20.0, 600) == [(0.0, 20.0)]
    assert sp.chunk_plan(100.0, 30) == [(0.0, 30), (30.0, 30), (60.0, 30), (90.0, 10.0)]
    assert sp.chunk_plan(62.0, 30) == [(0.0, 30), (30.0, 32.0)]            # a 2 s tail is folded into the previous chunk
    assert sp.chunk_plan(60.0, 30) == [(0.0, 30), (30.0, 30)]


def test_http_backend_gets_chunks_with_offsets_applied(tmp_path, monkeypatch):
    ex = install(monkeypatch)
    ctx = make_speech_ctx(tmp_path, speech_cfg(speech={"chunk_seconds": 30}))
    add_av(ctx, "long.mp4", 75.0, window=30.0)
    be = FakeBackend(script=lambda n, p, d: tr((1.0, 2.0, f"chunk {n}")))
    res = run(ctx, be)
    assert res.status == Status.SUCCEEDED and [c["start"] for c in ex.calls] == [0.0, 30.0, 60.0] and [c["duration"] for c in ex.calls] == [30.0, 30.0, 15.0]
    assert [d for _, d, _, _ in be.calls] == [30.0, 30.0, 15.0]
    rows = [json.loads(r["data_json"]) for r in ctx.catalog.list_transcripts()]
    assert [(r["start_seconds"], r["end_seconds"], r["text"]) for r in rows] == [(1.0, 2.0, "chunk 1"), (31.0, 32.0, "chunk 2"), (61.0, 62.0, "chunk 3")]


def test_command_backend_gets_the_whole_file(tmp_path, monkeypatch):
    ex = install(monkeypatch)
    ctx = make_speech_ctx(tmp_path, speech_cfg("command", speech={"chunk_seconds": 30}))
    add_av(ctx, "long.mp4", 75.0, window=30.0)
    run(ctx, FakeBackend("command"))
    assert len(ex.calls) == 1 and ex.calls[0]["start"] is None and ex.calls[0]["duration"] is None


def test_audio_format_and_keep_audio(tmp_path, monkeypatch):
    ex = install(monkeypatch)
    ctx = make_speech_ctx(tmp_path, speech_cfg(speech={"audio_format": "flac", "keep_audio": True}))
    aid, _ = add_av(ctx, "a.mp4", 6.0)
    run(ctx, FakeBackend())
    kept = list((ctx.workspace.audio_dir / aid).glob("*.flac"))
    assert ex.calls[0]["fmt"] == "flac" and len(kept) == 1
    # a kept file is reused when the transcript has to be redone
    ctx.config = speech_cfg(speech={"audio_format": "flac", "keep_audio": True, "language": "de"})
    run(ctx, FakeBackend())
    assert len(ex.calls) == 1
    # without keep_audio the temporary files and their folder are gone after use
    ctx2 = make_speech_ctx(tmp_path / "x")
    add_av(ctx2, "b.mp4", 6.0)
    run(ctx2, FakeBackend())
    assert not ctx2.workspace.audio_dir.exists()


def test_extraction_failure_is_per_asset_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "extract_audio", lambda *a, **k: "audio extraction failed: Invalid data")
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    res = run(ctx, FakeBackend())
    assert res.status == Status.PARTIAL and "Invalid data" in res.summary["errors"][0]["error"]


# ---------------------------------------------------------------- dry run


def test_dry_run_reports_counts_minutes_and_where(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path, speech_cfg(speech={"chunk_seconds": 30, "min_speech_seconds": 2}))
    add_av(ctx, "a.mp4", 90.0, window=30.0)
    add_av(ctx, "b.mp4", 30.0, window=30.0)
    add_av(ctx, "none.mp4", 30.0, audio=False)
    add_av(ctx, "quiet.mp4", 30.0, silent=True)
    run(ctx, FakeBackend())                                                   # a.mp4 and b.mp4 are now transcribed
    add_av(ctx, "c.mp4", 120.0, window=30.0)
    rep = sp.dry_run_report(ctx)
    assert (rep["backend"], rep["local"], rep["assets"], rep["cached"], rep["to_transcribe"]) == ("openai_transcriptions", True, 5, 2, 1)
    assert rep["skipped"] == {"no_audio_stream": 1, "silent": 1} and rep["audio_minutes"] == 2.0 and rep["requests"] == 4
    assert rep["endpoint"] == "http://127.0.0.1:8000/v1/audio/transcriptions" and rep["model"] == "test-model"
    text = sp.format_dry_run(rep)
    assert "local" in text and "2 min" in text and "no_audio_stream" in text and "leave this machine" not in text
    assert not any(p.exists() for p in (ctx.workspace.audio_dir,)) and len(FakeBackend().calls) == 0


def test_dry_run_remote_endpoint_warns_and_hides_credentials(tmp_path):
    cfg = speech_cfg(speech={"endpoint": {"base_url": "https://asr.example.com/base?token=SECRETQ", "model": "test-model", "api_key_env": "SOME_KEY"}})
    ctx = make_speech_ctx(tmp_path, cfg)
    add_av(ctx, "a.mp4", 60.0)
    rep = sp.dry_run_report(ctx)
    text = sp.format_dry_run(rep)
    assert rep["local"] is False and "remote" in text and "leave this machine" in text and "SECRETQ" not in text and "SOME_KEY" not in text


def test_dry_run_with_backend_none_or_disabled(tmp_path):
    ctx = make_speech_ctx(tmp_path, parse_config({}))
    add_av(ctx, "a.mp4", 60.0)
    rep = sp.dry_run_report(ctx)
    assert rep["backend"] == "none" and "none" in rep["reason"] and "skipped" in sp.format_dry_run(rep)
    off = make_speech_ctx(tmp_path / "off", speech_cfg(speech={"enabled": False}))
    assert "enabled is false" in sp.dry_run_report(off)["reason"]


def test_dry_run_respects_limit(tmp_path):
    ctx = make_speech_ctx(tmp_path)
    for n in "abc":
        add_av(ctx, f"{n}.mp4", 60.0)
    ctx.data["transcribe_limit"] = 2
    rep = sp.dry_run_report(ctx)
    assert rep["assets"] == 2 and rep["limit"] == 2


# ---------------------------------------------------------------- audio stage


def profile(lufs=-21.5, wind=False):
    lo = -20.2 if wind else -40.0
    return {"windows": [[0.25 * i, -20.0, -10.0, lo] for i in range(48)], "lufs": lufs, "window_seconds": 0.25}


def test_audio_stage_writes_features_to_segments_and_moments(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(au, "measure_audio", lambda ff, src, dur: calls.append(src.name) or profile(wind=src.name == "windy.mp4"))
    ctx = make_speech_ctx(tmp_path)
    windy_id, _ = add_av(ctx, "windy.mp4", 12.0)
    add_av(ctx, "calm.mp4", 6.0)
    add_av(ctx, "mute.mp4", 6.0, audio=False)
    res = sp.audio_stage(ctx)
    assert res.status == Status.SUCCEEDED and res.summary["assets_measured"] == 2 and res.summary["no_audio_stream"] == 1 and sorted(calls) == ["calm.mp4", "windy.mp4"]
    ms = moments(ctx)
    by_file = {}
    for m in ms.values():
        by_file.setdefault(ctx.catalog.get_asset(m["asset_id"])["relative_path"], []).append(m)
    w = by_file["windy.mp4"][0]
    assert w["audio"]["wind_likely"] is True and w["audio_tags"] == ["wind_likely"] and w["audio"]["asset_loudness_lufs"] == -21.5
    assert w["features"]["audio"] == w["audio"] and "audio_mean_db" in w["features"]            # existing features are kept
    c = by_file["calm.mp4"][0]
    assert c["audio"]["wind_likely"] is False and c["audio_tags"] == ["clean_audio"] and c["audio"]["active_ratio"] == 1.0
    m0 = by_file["mute.mp4"][0]
    assert m0["audio"]["has_audio"] is False and m0["audio_tags"] == ["silent"]
    seg = json.loads(ctx.catalog.list_segments(windy_id)[0]["data_json"])
    assert seg["features"]["audio"]["wind_likely"] is True
    jl = [json.loads(x) for x in ctx.workspace.moments_jsonl.read_text(encoding="utf-8").splitlines()]
    assert all("audio" in m for m in jl)
    # second run: profiles come from the cache
    res2 = sp.audio_stage(ctx)
    assert res2.summary["cache_hits"] == 2 and res2.summary["assets_measured"] == 0 and len(calls) == 2


def test_audio_stage_survives_moment_relabeling_and_resegmentation_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "measure_audio", lambda *a: profile())
    ctx = make_speech_ctx(tmp_path)
    aid, segs = add_av(ctx, "a.mp4", 6.0)
    before = sp.audio_inputs(ctx)
    sp.audio_stage(ctx)
    after = sp.audio_inputs(ctx)
    assert before != after                                                    # segments now carry audio
    # an LLM-labelled moment (source != baseline) gets the audio fields patched, not replaced
    mid = "moment_" + segs[0]["segment_id"][4:]
    ctx.catalog.update_moment_fields(mid, {"source": "llm", "description": "waves"})
    ctx.catalog.replace_segments(aid, "fast:a.mp4", [{k: v for k, v in segs[0].items()}])      # segmentation rebuilt the segment without audio
    assert sp.audio_inputs(ctx) == before
    sp.audio_stage(ctx)
    m = moments(ctx)[mid]
    assert m["description"] == "waves" and m["audio"]["silent"] is False


def test_audio_stage_disabled_failures_and_missing_ffmpeg(tmp_path, monkeypatch):
    ctx = make_speech_ctx(tmp_path, parse_config({"media": {"audio_features": False}}))
    add_av(ctx, "a.mp4", 6.0)
    res = sp.audio_stage(ctx)
    assert res.status == Status.SKIPPED and "audio_features" in res.summary["reason"] and "audio" not in moments(ctx)[next(iter(moments(ctx)))]
    ctx2 = make_speech_ctx(tmp_path / "2")
    add_av(ctx2, "a.mp4", 6.0)

    def boom(*a):
        raise RuntimeError("audio measurement failed: Invalid data found")

    monkeypatch.setattr(au, "measure_audio", boom)
    r = sp.audio_stage(ctx2)
    assert r.status == Status.PARTIAL and "Invalid data" in r.summary["errors"][0]["error"]
    ctx2.ffmpeg = None
    assert sp.audio_stage(ctx2).status == Status.PARTIAL


def test_transcription_works_without_the_audio_stage(tmp_path, monkeypatch):
    """Skipping flags come from the segmenter's silence detection, not from the audio stage."""
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path, parse_config({"media": {"audio_features": False},
                                                  "speech": {"backend": "command", "command": {"executable": "x", "args": ["{input}", "{output_dir}"]}}}))
    add_av(ctx, "a.mp4", 6.0)
    assert run(ctx, FakeBackend("command")).summary["transcribed"] == 1
