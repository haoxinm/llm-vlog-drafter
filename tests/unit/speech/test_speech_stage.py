from __future__ import annotations

import json

import pytest
from speech_helpers import FakeBackend, add_av, install, make_speech_ctx, moments, speech_cfg, tr

from trip_vlog.config import parse_config
from trip_vlog.logging_utils import Status
from trip_vlog.speech import stage as sp
from trip_vlog.speech.types import (
    SpeechAuthError,
    SpeechBadRequestError,
    SpeechCommandError,
    SpeechConnectionError,
)


@pytest.fixture(autouse=True)
def _reset():
    yield
    sp.backend_factory = None


def run(ctx, backend=None, **data):
    ctx.data.update(data)
    sp.backend_factory = (lambda c: backend) if backend is not None else None
    return sp.transcribe_stage(ctx)


def transcripts(ctx):
    return [json.loads(r["data_json"]) for r in ctx.catalog.list_transcripts()]


# ---------------------------------------------------------------- backend none / disabled


def test_backend_none_is_a_clear_skip_not_an_error(tmp_path):
    ctx = make_speech_ctx(tmp_path, parse_config({}))
    add_av(ctx, "a.mp4", 6.0)
    res = sp.transcribe_stage(ctx)
    assert res.status == Status.SKIPPED and res.error is None
    assert res.summary["skipped"] is True and "speech.backend is none" in res.summary["reason"]
    assert transcripts(ctx) == [] and not ctx.workspace.transcripts_jsonl.exists()
    off = make_speech_ctx(tmp_path / "off", speech_cfg(speech={"enabled": False}))
    r2 = sp.transcribe_stage(off)
    assert r2.status == Status.SKIPPED and "speech.enabled is false" in r2.summary["reason"]
    legacy = make_speech_ctx(tmp_path / "legacy", speech_cfg(media={"transcribe_speech": False}))
    assert sp.transcribe_stage(legacy).status == Status.SKIPPED
    assert sp.transcribe_inputs(ctx)["active"] is False


# ---------------------------------------------------------------- storing and mapping


def test_transcripts_are_stored_exported_and_mapped_onto_moments(tmp_path, monkeypatch):
    ex = install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    aid, segs = add_av(ctx, "trip/a.mp4", 12.0)                       # two 6 s segments: [0,6) and [6,12)
    be = FakeBackend(script=lambda n, p, d: tr((1.0, 2.0, "first words"), (5.5, 7.0, "across the cut"), (10.0, 11.0, "last words")))
    res = run(ctx, be)
    assert res.status == Status.SUCCEEDED and res.summary["transcribed"] == 1 and res.summary["segments"] == 3
    assert be.calls[0][0].endswith(".wav") and be.calls[0][1] == 12.0 and be.calls[0][2] is True
    rows = transcripts(ctx)
    assert [(r["start_seconds"], r["end_seconds"], r["text"]) for r in rows] == [(1.0, 2.0, "first words"), (5.5, 7.0, "across the cut"), (10.0, 11.0, "last words")]
    assert all(r["kind"] == "verbatim" and r["language"] == "en" and r["asset_id"] == aid and r["backend"] == "openai_transcriptions" for r in rows)
    ids = [r["transcript_id"] for r in rows]
    assert len(set(ids)) == 3
    ms = moments(ctx)
    first, second = (ms["moment_" + s["segment_id"][4:]] for s in segs)
    assert first["transcript_refs"] == ids[:2] and second["transcript_refs"] == ids[1:]
    assert first["speech_summary"] is None and second["speech_summary"] is None          # verbatim words only, no inferred summary
    lines = ctx.workspace.transcripts_jsonl.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["transcript_id"] for x in lines] == ids
    assert any(json.loads(x)["transcript_refs"] for x in ctx.workspace.moments_jsonl.read_text(encoding="utf-8").splitlines())
    # temporary audio is deleted by default
    assert not list((ctx.workspace.audio_dir).rglob("*.wav")) and ex.calls[0]["fmt"] == "wav" and ex.calls[0]["start"] is None


def test_overlap_rules_are_half_open():
    from trip_vlog.speech.context import refs_for, text_for

    rows = [{"transcript_id": "a", "start_seconds": 0.0, "end_seconds": 2.0, "text": "A"},
            {"transcript_id": "b", "start_seconds": 2.0, "end_seconds": 4.0, "text": "B"},
            {"transcript_id": "c", "start_seconds": 4.0, "end_seconds": 4.0, "text": "C"}]     # zero-length cue
    assert refs_for(rows, 2.0, 4.0) == ["b"] and refs_for(rows, 0.0, 2.0) == ["a"] and refs_for(rows, 4.0, 5.0) == ["c"]
    assert refs_for(rows, None, None) == [] and text_for(rows, 1.0, 3.0) == "A B" and text_for(rows, 9, 10) == ""


def test_zero_segment_result_is_cached_as_no_speech(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    be = FakeBackend(script=lambda *a: tr())
    res = run(ctx, be)
    assert res.status == Status.SUCCEEDED and res.summary["segments"] == 0 and transcripts(ctx) == []
    run(ctx, be)
    assert len(be.calls) == 1


def test_coarse_timestamps_are_recorded(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    run(ctx, FakeBackend(script=lambda *a: tr((0.0, 6.0, "whole clip text"), ts="coarse")))
    assert transcripts(ctx)[0]["timestamps"] == "coarse"


# ---------------------------------------------------------------- what is skipped


def test_assets_without_audio_or_with_silence_are_skipped(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path, speech_cfg(speech={"min_speech_seconds": 2.0}))
    add_av(ctx, "noaudio.mp4", 6.0, audio=False)
    add_av(ctx, "silent.mp4", 6.0, silent=True)
    add_av(ctx, "talk.mp4", 6.0)
    be = FakeBackend()
    res = run(ctx, be)
    assert res.summary["skipped_assets"] == {"no_audio_stream": 1, "silent": 1} and len(be.calls) == 1 and res.summary["transcribed"] == 1


def test_min_speech_seconds_threshold(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path, speech_cfg(speech={"min_speech_seconds": 3.0}))
    aid, segs = add_av(ctx, "mixed.mp4", 6.0, window=3.0)              # two 3 s segments
    row = json.loads(ctx.catalog.list_segments(aid)[0]["data_json"])
    row["features"]["silent"] = True                                    # the first half is silent -> 3 s active
    ctx.catalog.replace_segments(aid, ctx.catalog.get_asset(aid)["content_version"], [row, json.loads(ctx.catalog.list_segments(aid)[1]["data_json"])])
    assert sp.active_seconds(ctx.catalog, ctx.catalog.get_asset(aid)) == 3.0
    be = FakeBackend()
    assert run(ctx, be).summary["transcribed"] == 1
    ctx2 = make_speech_ctx(tmp_path / "two", speech_cfg(speech={"min_speech_seconds": 3.5}))
    add_av(ctx2, "mixed.mp4", 6.0, window=3.0)
    ctx2.catalog.conn.execute("UPDATE segments SET data_json=replace(data_json, '\"silent\": false', '\"silent\": true') WHERE start_seconds=0")
    ctx2.catalog.conn.commit()
    assert run(ctx2, FakeBackend()).summary["skipped_assets"] == {"silent": 1}


def test_stale_transcripts_are_cleared_when_an_asset_becomes_skipped(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    aid, _ = add_av(ctx, "a.mp4", 6.0)
    run(ctx, FakeBackend())
    assert transcripts(ctx)
    ctx.catalog.conn.execute("UPDATE assets SET audio_streams=0")
    ctx.catalog.conn.commit()
    run(ctx, FakeBackend())
    assert transcripts(ctx) == [] and all(m["transcript_refs"] == [] for m in moments(ctx).values())


# ---------------------------------------------------------------- cache key


def test_cache_hit_on_rerun_and_no_audio_extraction(tmp_path, monkeypatch):
    ex = install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    be = FakeBackend()
    run(ctx, be)
    res = run(ctx, be)
    assert len(be.calls) == 1 and len(ex.calls) == 1 and res.summary["cached"] == 1 and res.summary["transcribed"] == 0
    assert len(transcripts(ctx)) == 1


def key(**speech_or_project):
    kw = {k: speech_or_project.pop(k) for k in ("media", "project") if k in speech_or_project}
    return sp.cache_key(speech_cfg(speech=speech_or_project or None, **kw).speech)


def test_cache_key_changes_for_transcript_relevant_settings_only():
    base = key()
    for changed in (key(language="de"), key(endpoint={"base_url": "http://127.0.0.1:8000", "api_key_env": None, "model": "other-model"}),
                    key(endpoint={"base_url": "http://127.0.0.1:8000", "api_key_env": None, "model": "test-model", "params": {"temperature": 0.2}}),
                    key(endpoint={"base_url": "http://127.0.0.1:8000", "api_key_env": None, "model": "test-model", "response_format": "json"}),
                    key(audio_format="flac"), key(chunk_seconds=300)):
        assert changed != base
    same = [key(media={"segment_window_seconds": 3.0, "silence_threshold_db": -40.0}), key(project={"target_duration_seconds": 30}),
            key(keep_audio=True, min_speech_seconds=5.0, context_max_chars=10),
            key(endpoint={"base_url": "http://localhost:9999", "api_key_env": None, "model": "test-model", "timeout_seconds": 5, "max_retries": 0})]
    assert all(k == base for k in same)
    c1 = sp.cache_key(speech_cfg("command").speech)
    c2 = sp.cache_key(speech_cfg("command", speech={"command": {"executable": "whisper-cli", "args": ["-f", "{input}", "-of", "{output_base}", "-l", "{language}"]}}).speech)
    assert c1 != c2 != base and sp.cache_key(speech_cfg("command", speech={"chunk_seconds": 120}).speech) == c1   # chunking is HTTP-only


def test_model_from_environment_is_part_of_the_key(monkeypatch):
    cfg = speech_cfg(speech={"endpoint": {"base_url": "http://127.0.0.1:1", "api_key_env": None, "model": None, "model_env": "SPEECH_TEST_MODEL"}})
    a = sp.cache_key(cfg.speech, {"SPEECH_TEST_MODEL": "m1"})
    assert a != sp.cache_key(cfg.speech, {"SPEECH_TEST_MODEL": "m2"}) and a == sp.cache_key(cfg.speech, {"SPEECH_TEST_MODEL": "m1"})


def test_changing_language_or_model_reruns_but_duration_change_does_not(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    be = FakeBackend()
    run(ctx, be)
    ctx.config = speech_cfg(project={"target_duration_seconds": 45}, media={"segment_window_seconds": 3.0})
    run(ctx, be)
    assert len(be.calls) == 1
    ctx.config = speech_cfg(speech={"language": "de"})
    run(ctx, be)
    assert len(be.calls) == 2 and len(transcripts(ctx)) == 1                       # replaced, not duplicated
    ctx.config = speech_cfg(speech={"language": "de", "endpoint": {"base_url": "http://127.0.0.1:8000", "api_key_env": None, "model": "new-model"}})
    run(ctx, be)
    assert len(be.calls) == 3


def test_content_change_reruns_only_that_asset(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    add_av(ctx, "b.mp4", 6.0)
    be = FakeBackend()
    run(ctx, be)
    assert len(be.calls) == 2
    add_av(ctx, "b.mp4", 6.0, cv="fast:b-edited")
    run(ctx, be)
    assert len(be.calls) == 3
    assert {r["content_version"] for r in transcripts(ctx)} == {"fast:a.mp4", "fast:b-edited"}


def test_stage_inputs_react_to_derived_state(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    before = sp.transcribe_inputs(ctx)
    run(ctx, FakeBackend())
    assert sp.transcribe_inputs(ctx)["refs"] != before["refs"]                       # moments now carry transcript refs
    settled = sp.transcribe_inputs(ctx)
    run(ctx, FakeBackend())
    assert sp.transcribe_inputs(ctx) == settled                                      # stable once nothing changes
    ctx.config = speech_cfg(speech={"language": "fr"})
    assert sp.transcribe_inputs(ctx)["key"] != settled["key"]


# ---------------------------------------------------------------- failures


def test_per_asset_failure_is_partial_and_others_are_kept(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    add_av(ctx, "b.mp4", 6.0)
    add_av(ctx, "c.mp4", 6.0)
    be = FakeBackend(script=lambda n, p, d: SpeechBadRequestError("audio rejected") if n == 2 else None)
    res = run(ctx, be)
    assert res.status == Status.PARTIAL and res.summary["transcribed"] == 2 and res.summary["failed"] == 1
    assert res.summary["errors"][0]["path"] == "b.mp4" and "SpeechBadRequestError" in res.summary["errors"][0]["error"]
    assert len(transcripts(ctx)) == 2
    be2 = FakeBackend()
    res2 = run(ctx, be2)                                                              # the retry only does the failed asset
    assert res2.status == Status.SUCCEEDED and len(be2.calls) == 1 and len(transcripts(ctx)) == 3


def test_auth_error_is_fatal_and_stops_the_run(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    for n in "abc":
        add_av(ctx, f"{n}.mp4", 6.0)
    be = FakeBackend(script=lambda n, p, d: SpeechAuthError("bad key", status_code=401))
    res = run(ctx, be)
    assert res.status == Status.FAILED and "SpeechAuthError" in res.error and len(be.calls) == 1


def test_three_consecutive_connection_failures_abort(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    for n in "abcde":
        add_av(ctx, f"{n}.mp4", 6.0)
    be = FakeBackend(script=lambda n, p, d: SpeechConnectionError("refused"))
    res = run(ctx, be)
    assert res.status == Status.FAILED and "3 consecutive" in res.error and len(be.calls) == 3
    ok_then_fail = FakeBackend(script=lambda n, p, d: SpeechConnectionError("x") if n in (1, 3) else None)
    ctx2 = make_speech_ctx(tmp_path / "2")
    for n in "abcd":
        add_av(ctx2, f"{n}.mp4", 6.0)
    assert run(ctx2, ok_then_fail).status == Status.PARTIAL                           # non-consecutive failures do not abort


def test_command_failure_is_per_asset(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path, speech_cfg("command"))
    add_av(ctx, "a.mp4", 6.0)
    res = run(ctx, FakeBackend("command", script=lambda *a: SpeechCommandError("exited with status 3")))
    assert res.status == Status.PARTIAL and "status 3" in res.summary["errors"][0]["error"]


def test_missing_ffmpeg_and_missing_source_are_reported(tmp_path, monkeypatch):
    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    add_av(ctx, "a.mp4", 6.0)
    ctx.ffmpeg = None
    assert run(ctx, FakeBackend()).status == Status.FAILED
    ctx.ffmpeg = __import__("pathlib").Path("ffmpeg")
    (ctx.paths.media_root / "a.mp4").unlink()
    res = run(ctx, FakeBackend())
    assert res.status == Status.PARTIAL and "missing" in res.summary["errors"][0]["error"]


def test_missing_credentials_fail_the_stage_with_a_clear_message(tmp_path, monkeypatch):
    install(monkeypatch)
    monkeypatch.delenv("SPEECH_TEST_KEY", raising=False)
    cfg = speech_cfg(speech={"endpoint": {"base_url": "http://127.0.0.1:8000", "api_key_env": "SPEECH_TEST_KEY", "model": "test-model"}})
    ctx = make_speech_ctx(tmp_path, cfg)
    add_av(ctx, "a.mp4", 6.0)
    sp.backend_factory = None
    res = sp.transcribe_stage(ctx)
    assert res.status == Status.FAILED and "SPEECH_TEST_KEY" in res.error and "not set" in res.error


def test_early_bad_requests_abort_the_run_with_the_server_message(tmp_path, monkeypatch):
    from trip_vlog.speech.types import SpeechBadRequestError

    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    for n in "abcde":
        add_av(ctx, f"{n}.mp4", 6.0)
    be = FakeBackend(script=lambda n, p, d: SpeechBadRequestError("rejected the request (400): unknown model 'x'", status_code=400))
    res = run(ctx, be)
    assert res.status == Status.FAILED and len(be.calls) == 2 and "unknown model 'x'" in res.error and "configuration" in res.error


def test_item_specific_bad_request_after_a_success_stays_recoverable(tmp_path, monkeypatch):
    from trip_vlog.speech.types import SpeechBadRequestError

    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    for n in "abcde":
        add_av(ctx, f"{n}.mp4", 6.0)
    be = FakeBackend(script=lambda n, p, d: SpeechBadRequestError("bad audio", status_code=400) if n in (2, 3, 4) else None)
    res = run(ctx, be)
    assert res.status == Status.PARTIAL and len(be.calls) == 5


@pytest.mark.parametrize("status", [413, 415, None])
def test_size_and_media_type_rejections_stay_per_item(tmp_path, monkeypatch, status):
    from trip_vlog.speech.types import SpeechBadRequestError

    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    for n in "abcd":
        add_av(ctx, f"{n}.mp4", 6.0)
    be = FakeBackend(script=lambda n, p, d: SpeechBadRequestError("too big", status_code=status) if n < 4 else None)
    res = run(ctx, be)
    assert res.status == Status.PARTIAL and len(be.calls) == 4


def test_422_counts_toward_the_early_abort(tmp_path, monkeypatch):
    from trip_vlog.speech.types import SpeechBadRequestError

    install(monkeypatch)
    ctx = make_speech_ctx(tmp_path)
    for n in "abc":
        add_av(ctx, f"{n}.mp4", 6.0)
    be = FakeBackend(script=lambda n, p, d: SpeechBadRequestError("bad field", status_code=422))
    assert run(ctx, be).status == Status.FAILED and len(be.calls) == 2
