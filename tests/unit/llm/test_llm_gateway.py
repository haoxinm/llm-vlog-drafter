from __future__ import annotations

import pytest
from llm_helpers import img, BAD, GOOD, SCHEMA, ScriptedProvider, make_image, make_request, presp

from trip_vlog.llm import Gateway, ResponseStore
from trip_vlog.llm.types import (
    LLMProviderError,
    LLMRefusalError,
    LLMSchemaError,
    LLMTruncatedError,
    TextPart,
    Usage,
)
from trip_vlog.logging_utils import RunLogger, read_jsonl


def test_valid_first_try(img):
    p = ScriptedProvider(presp())
    resp = Gateway(p).complete(make_request([img]))
    assert resp.parsed == {"label": "beach", "score": 7}
    assert resp.text == GOOD and resp.model == "test-model-resolved" and resp.stop_reason == "end_turn"
    assert resp.attempts == 1 and resp.request_id == "req_1" and resp.latency_ms >= 0 and not resp.cached
    assert resp.usage == Usage(10, 5, 1, 2)
    assert len(p.requests) == 1 and p.requests[0].model == "test-model"


def test_schema_retry_sends_errors_not_more_media(img):
    p = ScriptedProvider(presp(BAD), presp(GOOD))
    resp = Gateway(p, schema_retries=2).complete(make_request([img]))
    assert resp.attempts == 2 and resp.parsed["score"] == 7
    assert resp.usage == Usage(20, 10, 2, 4)  # summed over attempts
    first, second = p.requests
    assert len(first.messages) == 1 and len(second.messages) == 3
    n_images = lambda r: sum(1 for m in r.messages for part in m.parts if not isinstance(part, TextPart))
    assert n_images(first) == n_images(second) == 1  # the retry adds no media
    assert second.messages[1].role == "assistant" and second.messages[1].parts[0].text == BAD
    last = second.messages[2]
    assert last.role == "user" and len(last.parts) == 1 and isinstance(last.parts[0], TextPart)
    assert "$.score" in last.parts[0].text and "99" in last.parts[0].text
    assert first.fingerprint == second.fingerprint


def test_invalid_json_is_retried_then_fails_with_typed_error(img):
    p = ScriptedProvider(presp("not json"), presp("{}"), presp("[]"))
    with pytest.raises(LLMSchemaError) as ei:
        Gateway(p, schema_retries=2).complete(make_request([img]))
    assert ei.value.attempts == 3 and len(p.requests) == 3 and ei.value.errors
    assert ei.value.raw_text == "[]"


def test_zero_schema_retries(img):
    p = ScriptedProvider(presp(BAD), presp(GOOD))
    with pytest.raises(LLMSchemaError):
        Gateway(p, schema_retries=0).complete(make_request([img]))
    assert len(p.requests) == 1


def test_provider_without_retry_support_is_called_once(img):
    p = ScriptedProvider(presp(BAD), presp(GOOD), supports_schema_retry=False)
    with pytest.raises(LLMSchemaError):
        Gateway(p, schema_retries=2).complete(make_request([img]))
    assert len(p.requests) == 1


def test_fenced_json_is_accepted(img):
    resp = Gateway(ScriptedProvider(presp("```json\n" + GOOD + "\n```"))).complete(make_request([img]))
    assert resp.parsed["label"] == "beach"


def test_refusal_is_typed_and_not_retried(img):
    p = ScriptedProvider(presp("", stop="refusal", detail="general_harms: nope"), presp(GOOD))
    with pytest.raises(LLMRefusalError) as ei:
        Gateway(p).complete(make_request([img]))
    assert "general_harms" in str(ei.value) and ei.value.detail == "general_harms: nope"
    assert len(p.requests) == 1


@pytest.mark.parametrize("stop", ["max_tokens", "model_context_window_exceeded"])
def test_truncation_is_typed_even_if_text_parses(img, stop):
    p = ScriptedProvider(presp(GOOD, stop=stop), presp(GOOD))
    with pytest.raises(LLMTruncatedError) as ei:
        Gateway(p).complete(make_request([img]))
    assert ei.value.stop_reason == stop and len(p.requests) == 1


def test_unexpected_stop_reason(img):
    with pytest.raises(LLMProviderError, match="tool_use"):
        Gateway(ScriptedProvider(presp(stop="tool_use"))).complete(make_request([img]))


def test_provider_error_propagates(img):
    err = LLMProviderError("down", status_code=500)
    with pytest.raises(LLMProviderError):
        Gateway(ScriptedProvider(err)).complete(make_request([img]))


def test_invalid_schema_rejected(img):
    from trip_vlog.llm.types import LLMInputError

    with pytest.raises(LLMInputError):
        Gateway(ScriptedProvider(presp())).complete(make_request([img], schema={"type": "nonsense"}))


def test_cache_hit_avoids_provider_call(img, tmp_path):
    cache = ResponseStore(tmp_path / "cache")
    p = ScriptedProvider(presp(), presp("{}"))
    gw = Gateway(p, cache=cache)
    first = gw.complete(make_request([img]))
    second = gw.complete(make_request([img]))
    assert len(p.requests) == 1
    assert not first.cached and second.cached and second.attempts == 0
    assert second.parsed == first.parsed and second.model == first.model and second.usage == first.usage
    # a changed prompt version is a different cache entry
    p.queue = [presp()]
    gw.complete(make_request([img], version="2"))
    assert len(p.requests) == 2


def test_cache_entry_invalid_for_schema_or_corrupt_is_recomputed(img, tmp_path):
    cache = ResponseStore(tmp_path / "cache")
    p = ScriptedProvider(presp(), presp(), presp())
    gw = Gateway(p, cache=cache)
    req = make_request([img])
    fp = gw.fingerprint(req)
    gw.complete(req)
    path = cache.path_for(fp)
    path.write_text("{ not json", encoding="utf-8")
    gw.complete(req)
    assert len(p.requests) == 2  # corrupt entry ignored, then rewritten
    assert gw.complete(req).cached


def test_recorder_saves_validated_response_and_backfills_on_cache_hit(img, tmp_path):
    cache, rec = ResponseStore(tmp_path / "cache"), ResponseStore(tmp_path / "rec")
    p = ScriptedProvider(presp(BAD), presp(GOOD))
    gw = Gateway(p, cache=cache, recorder=rec)
    req = make_request([img])
    gw.complete(req)
    stored = rec.get(gw.fingerprint(req))
    assert stored["response"]["parsed"] == {"label": "beach", "score": 7}  # the validated answer, not the bad attempt
    assert "image_bytes" not in str(stored) and stored["request"]["parts"][0]["type"] == "image"
    # fresh recorder + warm cache: a hit still populates the recorder
    rec2 = ResponseStore(tmp_path / "rec2")
    Gateway(p, cache=cache, recorder=rec2).complete(req)
    assert rec2.has(gw.fingerprint(req)) and len(p.requests) == 2


def test_structured_log_has_metadata_but_no_secrets_or_image_bytes(img, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecretvalue123")
    logger = RunLogger(tmp_path / "run.jsonl", tmp_path / "err.jsonl", "run1")
    p = ScriptedProvider(presp())
    Gateway(p, logger=logger, cache=ResponseStore(tmp_path / "c")).complete(make_request([img]))
    Gateway(p, logger=logger, cache=ResponseStore(tmp_path / "c")).complete(make_request([img]))
    rows = read_jsonl(tmp_path / "run.jsonl")
    assert [r["cache"] for r in rows] == ["miss", "hit"]
    r = rows[0]
    assert r["stage"] == "llm" and r["status"] == "succeeded"
    assert r["prompt_id"] == "media_analyst" and r["prompt_version"] == "1" and r["model"] == "test-model-resolved"
    assert (r["input_tokens"], r["output_tokens"], r["cache_read_tokens"], r["cache_creation_tokens"]) == (10, 5, 1, 2)
    assert r["duration_ms"] >= 0 and r["attempts"] == 1 and r["request_id"] == "req_1"
    assert "input_tokens" not in rows[1]  # cache hit costs nothing
    blob = (tmp_path / "run.jsonl").read_text()
    assert "supersecret" not in blob and "/9j/" not in blob  # no key, no base64 JPEG header


def test_failure_is_logged_sanitized(img, tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "topsecret-value-1")
    logger = RunLogger(tmp_path / "run.jsonl", tmp_path / "err.jsonl", "run1")
    with pytest.raises(LLMRefusalError):
        Gateway(ScriptedProvider(presp("", stop="refusal", detail="topsecret-value-1")), logger=logger).complete(make_request([img]))
    rows = read_jsonl(tmp_path / "err.jsonl")
    assert rows and rows[0]["status"] == "failed" and "LLMRefusalError" in rows[0]["error"]
    assert "topsecret" not in (tmp_path / "err.jsonl").read_text()
